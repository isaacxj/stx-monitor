import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from monitor.schedule import ACTOR, BY_ID, Scheduler, host_timezone, last_due, next_due
from monitor.store import Store

UTC = timezone.utc


def setup(tmp_path, helper=True, alerts_on=True):
    events, broadcasts, sent, helper_calls = [], [], [], []
    store = Store(":memory:")

    async def helper_call(cmd, args=None, **kw):
        helper_calls.append((cmd, args))
        return True, "Freed 1.4GB" if cmd == "prune-images" else "Updated to a new image"

    async def check_updates():
        return 2

    async def check_smart():
        return True, "2 drives checked, all healthy"

    async def send(message):
        sent.append(message)
        return True

    containers = [{"id": "aaa", "name": "jellyfin", "state": "running"}, {"id": "bbb", "name": "sonarr", "state": "running"}]
    actions = SimpleNamespace(
        helper=SimpleNamespace(installed=helper, call=helper_call), check_updates=check_updates,
        updates={"aaa": {"available": True}, "bbb": {"available": False}},
        allowed=lambda c: ["restart", "stop", "update"], expect_stop=lambda cid: None)
    col = SimpleNamespace(
        settings=SimpleNamespace(host_root=str(tmp_path)), store=store, actions=actions, containers=containers,
        storage=SimpleNamespace(check_smart=check_smart, view=lambda: {
            "forecast": {"/srv/data": {"days_left": 20}},
            "smart": {"disks": [{"name": "sdb", "state": "warn"}]}}),
        backups=SimpleNamespace(view=lambda: {"state": "ok", "finished": 1.0}),
        alerts=SimpleNamespace(configured=alerts_on, categories=lambda: {"reports": True}, send=send, last_error=None),
        apps_map=lambda: {"counts": {"running": 5, "deployed": 6, "down": 1}},
        host_sample={"uptime_s": 3 * 86400, "mounts": [{"mountpoint": "/srv/data", "percent": 70.4}]},
        add_event=lambda ev: events.append(ev) or ev,
        broadcast=lambda kind, data: broadcasts.append((kind, data)),
        request_docker_refresh=lambda: None,
    )
    s = Scheduler(col)
    s.tz = UTC
    return s, SimpleNamespace(events=events, broadcasts=broadcasts, sent=sent, helper_calls=helper_calls, store=store)


def test_slots():
    daily, weekly = BY_ID["check-updates"], BY_ID["prune-images"]  # 4:30 every day; Sundays at 4:00
    wed = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    assert last_due(daily, wed) == datetime(2026, 11, 18, 4, 30, tzinfo=UTC)
    assert last_due(daily, wed.replace(hour=3)) == datetime(2026, 11, 17, 4, 30, tzinfo=UTC)
    assert next_due(daily, wed) == datetime(2026, 11, 19, 4, 30, tzinfo=UTC)
    assert last_due(weekly, wed) == datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
    assert next_due(weekly, wed) == datetime(2026, 11, 22, 4, 0, tzinfo=UTC)
    assert daily.when == "Every day at 4:30 AM" and weekly.when == "Sundays at 4:00 AM"


def test_host_timezone(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/localtime").symlink_to("/usr/share/zoneinfo/America/Chicago")
    assert host_timezone(tmp_path).key == "America/Chicago"
    assert host_timezone(tmp_path / "missing") is UTC


async def test_tick_runs_what_is_due_once(tmp_path):
    s, t = setup(tmp_path)
    before = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)
    assert await s.tick(before) == []  # first sight: nothing runs, every task waits for its next slot
    assert await s.tick(datetime(2026, 11, 18, 4, 31, tzinfo=UTC)) == ["check-updates"]
    await asyncio.gather(*s.tasks)
    assert await s.tick(datetime(2026, 11, 18, 4, 32, tzinfo=UTC)) == []
    row = t.store.list_actions(1)[0]
    assert (row["actor"], row["action"], row["status"], row["detail"]) == (ACTOR, "check-updates", "ok", "2 updates available")
    task = next(x for x in s.view()["tasks"] if x["id"] == "check-updates")
    assert task["ok"] is True and task["detail"] == "2 updates available" and task["last_run"]


async def test_tick_skips_disabled_and_long_overdue(tmp_path):
    s, t = setup(tmp_path)
    await s.tick(datetime(2026, 11, 15, 0, 0, tzinfo=UTC))
    # update-apps (Mondays at 5) is off by default; a slot missed by more than six hours is dropped.
    started = await s.tick(datetime(2026, 11, 16, 23, 0, tzinfo=UTC))
    assert "update-apps" not in started and "check-updates" not in started
    await asyncio.gather(*s.tasks)
    assert not any(r["action"] == "check-updates" for r in t.store.list_actions(20))


async def test_turning_a_task_on_does_not_run_it(tmp_path):
    s, t = setup(tmp_path)
    await s.tick(datetime.now(UTC))
    view = s.set_enabled("update-apps", True, "isaac", "iPhone")
    task = next(x for x in view["tasks"] if x["id"] == "update-apps")
    assert task["enabled"] and task["next_run"] > time.time()
    assert await s.tick(datetime.now(UTC)) == []
    with pytest.raises(ValueError):
        s.set_enabled("rm -rf", True, "isaac", "")


async def test_helper_tasks_need_the_helper(tmp_path):
    s, _ = setup(tmp_path, helper=False)
    task = next(x for x in s.view()["tasks"] if x["id"] == "prune-images")
    assert task["unavailable"] and task["next_run"] is None
    with pytest.raises(ValueError):
        s.request("prune-images", "isaac", "")


async def test_update_apps_only_touches_apps_with_updates(tmp_path):
    s, t = setup(tmp_path)
    row = await s.run("update-apps", "isaac", "iPhone")
    assert row["status"] == "ok" and row["detail"] == "Updated jellyfin"
    assert t.helper_calls == [("update", {"container": "jellyfin"})]


async def test_weekly_report(tmp_path):
    s, t = setup(tmp_path)
    now = time.time()
    t.store.add_report("container", "radarr", "radarr stopped", "Out of memory", {}, when=now - 3600)
    t.store.add_report("container", "radarr", "radarr stopped", "Out of memory", {}, when=now - 7200)
    t.store.add_report("container", "old", "old stopped", "x", {}, when=now - 30 * 86400)
    report = s.build_report(now)
    assert report["title"] == "Weekly report: 4 things to look at"
    assert report["problems"] == ["crashes", "apps down", "disk filling", "drive health"]
    text = "\n".join(report["lines"])
    assert "2 crashes this week (2 radarr)" in text and "full in about 20 days" in text and "sdb shows wear" in text
    assert "1 app can be updated" in text
    row = await s.run("weekly-report", ACTOR, "")
    assert row["status"] == "ok" and row["detail"] == "Sent to your phone"
    assert t.sent[0]["title"] == report["title"] and t.sent[0]["priority"] == 3
    assert s.view()["report"]["title"] == report["title"]


async def test_weekly_report_is_kept_when_alerts_are_off(tmp_path):
    s, t = setup(tmp_path, alerts_on=False)
    row = await s.run("weekly-report", ACTOR, "")
    assert row["status"] == "ok" and "wasn't sent" in row["detail"] and not t.sent
    assert t.store.get("weekly_report")["lines"]
