import time

from monitor.store import Store


def test_settings_roundtrip(tmp_path):
    s = Store(tmp_path / "m.db")
    assert s.get("missing", 5) == 5
    s.set("autopilot", {"enabled": True, "threshold": 20})
    s.set("autopilot", {"enabled": False, "threshold": 25})
    assert s.get("autopilot") == {"enabled": False, "threshold": 25}
    s.close()
    assert Store(tmp_path / "m.db").get("autopilot")["threshold"] == 25  # survives a reopen


def test_events_reports_actions(tmp_path):
    s = Store(tmp_path / "m.db")
    ev = s.add_event({"kind": "app", "level": "error", "title": "radarr stopped", "container": "abc"})
    assert ev["id"] == 1
    assert s.recent_events()[0]["title"] == "radarr stopped"
    rep = s.add_report("container", "radarr", "Radarr ran out of memory", "It hit 2 GB.", {"exit_code": 137})
    assert rep["data"]["exit_code"] == 137 and rep["seen"] is False
    assert s.unseen_reports() == 1
    s.mark_report_seen(rep["id"])
    assert s.unseen_reports() == 0
    assert s.count_reports_since("radarr", time.time() - 60) == 1
    a = s.add_action("isaac", "iPhone", "restart", "jellyfin", "running")
    assert a["finished"] is None
    done = s.finish_action(a["id"], "ok", "Restarted in 3 s")
    assert done["status"] == "ok" and done["finished"] is not None
    s.add_action("isaac", "iPhone", "stop", "radarr", "running")
    s.fail_running_actions("stx-monitor restarted")
    assert [x["status"] for x in s.list_actions()] == ["unknown", "ok"]


def test_sessions_and_passkeys(tmp_path):
    s = Store(tmp_path / "m.db")
    now = time.time()
    for sid in ("a", "b"):
        s.add_session({"id": sid, "csrf": "c", "created": now, "expires": now + 60, "last_seen": now, "remember": False, "method": "password"})
    s.add_session({"id": "old", "csrf": "c", "created": now, "expires": now - 1, "last_seen": now, "remember": False, "method": "password"})
    assert {x["id"] for x in s.list_sessions()} == {"a", "b"}  # expired ones are swept
    assert s.delete_other_sessions("a") == 1
    s.add_passkey("cred1", b"key", 0, "stx-server.ts.net", "iPhone")
    s.use_passkey("cred1", 5)
    assert s.get_passkey("cred1")["sign_count"] == 5
    assert s.list_passkeys()[0]["name"] == "iPhone"
