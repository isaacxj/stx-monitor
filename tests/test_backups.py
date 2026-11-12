import json
import os
import time
from types import SimpleNamespace

from monitor.alerts import classify
from monitor.backups import STALE_AFTER, Backups
from monitor.store import Store


def setup(tmp_path, status=None, timer=True):
    root = tmp_path / "host"
    (root / "var/lib/stx-backup").mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)
    if timer:
        (root / "etc/systemd/system/stx-backup.timer").write_text("[Timer]\n")
    events, broadcasts, helper_calls = [], [], []

    async def call(cmd, args=None):
        helper_calls.append(cmd)
        return True, "Backup started"

    col = SimpleNamespace(
        settings=SimpleNamespace(host_root=str(root)),
        store=Store(":memory:"),
        actions=SimpleNamespace(helper=SimpleNamespace(installed=True, call=call)),
        add_event=lambda ev: events.append(ev) or ev,
        broadcast=lambda kind, data: broadcasts.append((kind, data)),
    )
    b = Backups(col)
    if status is not None:
        write(b, status)
    return b, events, broadcasts, helper_calls


def write(b, status):
    b.status_path.write_text(json.dumps(status))
    st = b.status_path.stat()
    os.utime(b.status_path, (st.st_atime, st.st_mtime + 1))  # make sure the change is seen


def good(finished):
    return {"running": False, "ok": True, "finished": finished, "last_ok": finished, "error": None,
            "apps": [{"name": "sonarr"}, {"name": "api"}],
            "last_ok_summary": {"data_added": 2_500_000, "duration": 42}}


async def test_states(tmp_path):
    b, *_ = setup(tmp_path, timer=False)
    assert b.state() == "not-installed"
    b, *_ = setup(tmp_path / "2")
    assert b.state() == "never"
    write(b, {**good(time.time()), "running": True})
    b.load()
    assert b.state() == "running"
    write(b, good(time.time() - STALE_AFTER - 60))
    b.load()
    assert b.state() == "stale"
    write(b, {**good(time.time() - 3600), "ok": False, "error": "drive missing", "last_ok": time.time() - 7200})
    b.load()
    assert b.state() == "failed"


async def test_events_once_per_run_and_recovery(tmp_path):
    now = time.time()
    b, events, broadcasts, _ = setup(tmp_path, good(now - 100))
    await b.check()
    assert [(e["level"], e["title"]) for e in events] == [("info", "Backed up 2 apps")]
    assert "2.4 MB new" in events[0]["detail"]
    assert broadcasts[-1][0] == "backups" and broadcasts[-1][1]["state"] == "ok"
    await b.check()
    assert len(events) == 1  # the same run never repeats

    write(b, {**good(now - 50), "ok": False, "error": "Backup failed: repository is locked", "last_ok": now - 100})
    await b.check()
    assert (events[-1]["level"], events[-1]["title"]) == ("error", "Backup failed")
    assert classify(events[-1])["category"] == "backups"

    write(b, good(now))
    await b.check()
    assert (events[-1]["level"], events[-1]["title"]) == ("ok", "Backups are working again")


async def test_stale_warns_once(tmp_path):
    old = time.time() - STALE_AFTER - 3600
    b, events, *_ = setup(tmp_path, good(old))
    b.c.store.set("backups_seen", {"finished": old, "state": "ok"})  # that run was already reported
    await b.check()
    await b.check()
    assert [(e["level"], e["title"]) for e in events] == [("warn", "No backup for 37 hours")]
    assert classify(events[0])["priority"] == 4


async def test_run_now_and_pause_noise(tmp_path):
    b, _, _, calls = setup(tmp_path, good(time.time()))
    b.load()
    await b.run_now("isaac", "iPhone")
    assert calls == ["backup"]
    assert b.c.store.list_actions(1)[0]["action"] == "backup"
    write(b, {**good(time.time()), "running": True, "paused": ["sonarr"]})
    b.load()
    assert b.paused_by_backup("sonarr") and not b.paused_by_backup("radarr")
    try:
        await b.run_now("isaac", "iPhone")
        raise AssertionError("should refuse while running")
    except RuntimeError as exc:
        assert "already running" in str(exc)
