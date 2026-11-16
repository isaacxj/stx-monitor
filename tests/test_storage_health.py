import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from monitor.storage_health import DAY, StorageHealth, fit_growth, forecast, verdict
from monitor.store import Store

HELPER_FILE = Path(__file__).resolve().parent.parent / "host" / "stx-helper.py"
GB = 10 ** 9


def load_helper():
    spec = importlib.util.spec_from_file_location("stx_helper", HELPER_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def setup(answer=None, installed=True):
    events, broadcasts, calls = [], [], []

    async def call(cmd, args=None, timeout=0):
        calls.append(cmd)
        return answer

    col = SimpleNamespace(
        store=Store(":memory:"),
        actions=SimpleNamespace(helper=SimpleNamespace(installed=installed, call=call)),
        add_event=lambda ev: events.append(ev) or ev,
        broadcast=lambda kind, data: broadcasts.append((kind, data)),
    )
    return StorageHealth(col), events, broadcasts, calls


def mount(used, total=1000 * GB, mp="/srv/data"):
    return {"mountpoint": mp, "used": used, "total": total, "free": total - used, "percent": used / total * 100}


def test_fit_needs_a_few_days():
    assert fit_growth([(1, 10.0)]) is None
    assert fit_growth([(1, 10.0), (1, 12.0), (2, 14.0)]) is None  # three readings but only one day apart
    assert fit_growth([(1, 10.0), (2, 20.0), (3, 30.0)]) == 10.0


def test_forecast_days_left():
    points = [(d, 500 * GB + d * 5 * GB) for d in range(10)]
    f = forecast(points, free=100 * GB, total=1000 * GB)
    assert f["growth_per_day"] == 5 * GB and f["days_left"] == 20.0 and f["days_of_data"] == 10
    # Flat or shrinking usage never fills up.
    assert forecast([(d, 500 * GB) for d in range(10)], 100 * GB, 1000 * GB)["days_left"] is None
    assert forecast([(d, 500 * GB - d * GB) for d in range(10)], 100 * GB, 1000 * GB)["days_left"] is None


def test_record_keeps_one_row_a_day_and_warns_once():
    sh, events, broadcasts, _ = setup()
    start = 20000 * DAY
    for d in range(8):
        # 900 GB and growing 10 GB a day: full in about 3 days by the end
        sh.record({"time": start + d * DAY, "mounts": [mount(900 * GB + d * 10 * GB)]})
        sh.record({"time": start + d * DAY + 3600, "mounts": [mount(900 * GB + d * 10 * GB + GB)]})
    rows = sh.store.disk_usage_since(0)
    assert len(rows) == 8
    f = sh.forecasts(start + 8 * DAY)["/srv/data"]
    assert 9 * GB < f["growth_per_day"] < 11 * GB and f["days_left"] < 14
    warnings = [e for e in events if "will be full" in e["title"]]
    assert len(warnings) == 1 and warnings[0]["kind"] == "storage" and warnings[0]["level"] == "warn"
    assert any(kind == "storage" for kind, _ in broadcasts)


def test_verdicts():
    assert verdict({"passed": True, "reallocated": 0, "pending": 0})[0] == "ok"
    assert verdict({"passed": False})[0] == "failing"
    assert verdict({"passed": True, "pending": 3})[0] == "warn"
    assert verdict({"passed": True, "reallocated": 1})[1].startswith("1 sector has been replaced by a spare.")
    assert verdict({"passed": True, "reallocated": 8})[1].startswith("8 sectors have been replaced by spares.")
    assert verdict({"passed": True, "wear": 95})[0] == "warn"
    assert verdict({"passed": True, "critical_warning": 4})[0] == "failing"
    assert verdict({"supported": False, "error": "Unknown USB bridge"}) == ("unknown", "Unknown USB bridge")


async def test_check_smart_events_fire_on_change_only():
    disks = [{"name": "sda", "model": "SanDisk X600", "supported": True, "passed": True, "reallocated": 0},
             {"name": "sdb", "model": "ST1000", "supported": True, "passed": True, "reallocated": 8}]
    sh, events, _, calls = setup((True, json.dumps({"disks": disks})))
    ok, detail = await sh.check_smart()
    assert ok and detail == "2 drives checked, 1 needs attention" and calls == ["smart"]
    assert [e["title"] for e in events] == ["sdb (ST1000) shows wear"]
    await sh.check_smart()
    assert len(events) == 1  # same state, no second event
    view = sh.smart()
    assert [d["state"] for d in view["disks"]] == ["ok", "warn"] and view["checked"]


async def test_check_smart_without_helper_or_with_an_old_one():
    sh, *_ = setup(installed=False)
    ok, detail = await sh.check_smart()
    assert not ok and "stx-helper" in detail
    sh, *_ = setup((False, "Unknown command 'smart'"))
    ok, detail = await sh.check_smart()
    assert not ok and "out of date" in detail and sh.smart()["error"] == detail


def test_helper_summarizes_smartctl_json():
    mod = load_helper()
    ata = {"model_name": "ST1000LM035", "smart_status": {"passed": True}, "temperature": {"current": 34},
           "power_on_time": {"hours": 21480},
           "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 8}}, {"id": 197, "raw": {"value": 0}},
                                              {"id": 198, "raw": {"value": 0}}]}}
    out = mod.summarize_smart("sdb", ata)
    assert out == {"name": "sdb", "model": "ST1000LM035", "supported": True, "passed": True, "temp": 34,
                   "hours": 21480, "reallocated": 8, "pending": 0, "uncorrectable": 0}
    nvme = {"model_name": "WD SN770", "smart_status": {"passed": True},
            "nvme_smart_health_information_log": {"percentage_used": 3, "media_errors": 0, "critical_warning": 0}}
    assert mod.summarize_smart("nvme0n1", nvme)["wear"] == 3
    usb = {"smartctl": {"messages": [{"string": "/dev/sdc: Unknown USB bridge [0x1234:0x5678]"}]}}
    out = mod.summarize_smart("sdc", usb)
    assert out["supported"] is False and "Unknown USB bridge" in out["error"]
