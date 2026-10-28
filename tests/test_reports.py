import time
from pathlib import Path
from types import SimpleNamespace

from monitor.reports import FailureReports, duration, suggest, what_happened
from monitor.store import Store

GiB = 1024 ** 3


def test_what_happened():
    assert what_happened(137, True, 2 * GiB) == ("Out of memory", "It went over its 2.0 GB memory limit, so Linux stopped it.")
    assert what_happened(137, True, None)[1].startswith("The server ran low on memory")
    assert what_happened(137, False, None)[0] == "Killed"
    assert what_happened(139, False, None)[0] == "Crashed"
    assert what_happened(1, False, None)[1] == "It quit with exit code 1."


def test_suggestions():
    s = suggest("Radarr", 137, True, 2 * GiB, "")
    assert s[0].startswith("Raise Radarr's memory limit to 3 GB")
    s = suggest("Sonarr", 1, False, None, "System.UnauthorizedAccessException: Permission denied\nbind: address already in use")
    assert any("PUID" in x for x in s) and any("port" in x for x in s)
    assert "Prowlarr" in suggest("Sonarr", 1, False, None, "Connection refused (prowlarr:9696)", depends_on=["Prowlarr"])[0]
    assert any("3 times today" in x for x in suggest("x", 1, False, None, "", repeats=3))
    assert suggest("x", 1, False, None, "") == ["The last log lines usually say why. Start it again once you have read them."]
    assert duration(17 * 60 + 5) == "17 min" and duration(90000) == "1 day, 1 h"


class FakeDocker:
    def __init__(self, inspect):
        self._inspect = inspect

    async def inspect(self, cid, fresh=False):
        return self._inspect

    async def logs(self, cid, tail=200):
        return [{"ts": "2026-10-28T10:12:08Z", "stream": "stderr", "text": "Out of memory while reading media info"}]


def fake_collector(tmp_path, inspect=None, journal=None, readings=None):
    store = Store(tmp_path / "m.db")
    if readings:
        store.set("last_readings", readings)
    events = []
    broadcasts = []
    col = SimpleNamespace(
        store=store,
        docker=FakeDocker(inspect or {}),
        stats={"abc123": {"cpu": 34.0, "mem": 1.98 * GiB, "mem_limit": 2 * GiB}},
        actions=None,
        journal=journal,
        host=SimpleNamespace(identity={"boot_time": 1_800_001_020}),
        settings=SimpleNamespace(host_root=tmp_path),
        add_event=lambda ev: events.append(ev) or ev,
        broadcast=lambda kind, data: broadcasts.append((kind, data)),
        app_for_container=lambda cid: {"id": "radarr", "name": "Radarr", "links": ["prowlarr"]},
        app_name=lambda x: x.title(),
    )
    return col, events, broadcasts


async def test_container_report(tmp_path):
    inspect = {"exit_code": 137, "oom_killed": True, "memory_limit": 2 * GiB, "restart_count": 3,
               "started_at": "2026-10-27T07:00:00Z", "finished_at": "2026-10-28T10:12:09Z", "image": "lscr.io/linuxserver/radarr"}
    col, events, broadcasts = fake_collector(tmp_path, inspect)
    reports = FailureReports(col)
    report = await reports.container_died({"action": "die", "id": "abc123", "name": "radarr", "exit_code": "137"})
    assert report["title"] == "Radarr ran out of memory"
    data = report["data"]
    assert data["exit_code"] == 137 and data["oom"] is True
    assert data["stats"]["cpu"] == 34.0
    assert data["logs"][0]["text"].startswith("Out of memory")
    assert data["suggestions"][0].startswith("Raise Radarr's memory limit")
    assert data["ran_for"] > 0
    assert events[0]["report"] == report["id"] and broadcasts[0][0] == "report"
    # A crash loop updates the same report instead of filing a new one
    assert await reports.container_died({"action": "die", "id": "abc123", "name": "radarr"}) is None
    assert col.store.get_report(report["id"])["data"]["repeats_in_window"] == 2
    assert len(col.store.list_reports()) == 1


async def test_clean_exit_is_not_a_failure(tmp_path):
    col, _, _ = fake_collector(tmp_path, {"exit_code": 0, "oom_killed": False})
    assert await FailureReports(col).container_died({"action": "die", "id": "abc123", "name": "x"}) is None
    col.actions = SimpleNamespace(expected_stop=lambda cid: True)
    col.docker = FakeDocker({"exit_code": 137})
    assert await FailureReports(col).container_died({"action": "die", "id": "abc123", "name": "x"}) is None


class FakeJournal:
    available = True

    def __init__(self, entries):
        self.entries = entries

    async def previous_boot_tail(self, lines=300):
        return self.entries


def entry(ts, text, source="kernel"):
    return {"ts": ts, "source": source, "level": "info", "text": text, "boot": "prevboot", "identifier": source}


async def test_unclean_shutdown_report(tmp_path):
    went_dark = 1_800_000_000
    journal = FakeJournal([entry(went_dark - 100, "ACPI: battery low"), entry(went_dark, "Battery critically low")])
    col, events, _ = fake_collector(tmp_path, journal=journal,
                                    readings={"boot": "prevboot", "ac": False, "battery": 3, "temp": 51})
    col.store.add_event({"time": went_dark - 4000, "kind": "power", "level": "warn",
                         "title": "Running on battery", "detail": "Wall power may be out. Battery at 80%."})
    report = await FailureReports(col).check_previous_boot()
    assert report["title"] == "Server went offline"
    assert report["summary"] == "The battery ran out. It was off for 17 min."
    data = report["data"]
    assert [t["tone"] for t in data["timeline"]] == ["amber", "red", "cyan"]
    assert "autopilot" in data["suggestions"][0]
    assert data["last_lines"][-1]["text"] == "Battery critically low"
    # The same boot is only reported once
    assert await FailureReports(col).check_previous_boot() is None


async def test_clean_shutdown_files_nothing(tmp_path):
    journal = FakeJournal([entry(1, "Reached target System Power Off.", "systemd")])
    col, _, _ = fake_collector(tmp_path, journal=journal)
    assert await FailureReports(col).check_previous_boot() is None
    assert col.store.list_reports() == []
