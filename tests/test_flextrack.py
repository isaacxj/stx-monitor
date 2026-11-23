"""FlexTrack (my gym tracker) gets the same treatment as the media apps: alerts, failure
reports, Control actions and nightly backups. Its containers are named flextrack-web and
flextrack-api, belong to the compose project "opengym" in ~/opengym, and keep their data
as JSON files in ~/opengym/data (no database)."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from monitor.alerts import Alerts, classify
from monitor.collector import docker_event_to_event
from monitor.reports import FailureReports
from test_actions import load_helper, make_collector
from test_alerts import fake_collector as alerts_collector
from test_alerts import recorder
from test_reports import FakeDocker
from test_reports import fake_collector as reports_collector

BACKUP_FILE = Path(__file__).resolve().parent.parent / "host" / "stx-backup.py"
COMPOSE = "/home/static/opengym/docker-compose.yml"


def flextrack(service, state="running"):
    name = f"flextrack-{service}"
    return {"id": name[:12].ljust(12, "0"), "name": name, "state": state, "status": "",
            "labels": {"stx.app": name, "stx.group": "personal"}, "compose_project": "opengym"}


@pytest.mark.parametrize("raw", [
    {"action": "die", "exit_code": "1"},
    {"action": "die", "exit_code": "137"},
    {"action": "oom"},
    {"action": "health_status: unhealthy"},
])
async def test_crashes_oom_and_unhealthy_buzz_the_phone(raw):
    sent, transport = recorder()
    a = Alerts(alerts_collector(), "https://ntfy.sh/stx-secret", transport=transport)
    ev = docker_event_to_event({**raw, "name": "flextrack-api", "id": "f1"})
    assert classify(ev)["category"] == "apps"
    assert a.notify(ev)
    await asyncio.gather(*a.tasks)
    assert len(sent) == 1 and sent[0]["body"]["title"].startswith("flextrack-api")
    assert sent[0]["body"]["priority"] == 4
    await a.close()


async def test_a_stop_from_the_console_stays_quiet():
    sent, transport = recorder()
    a = Alerts(alerts_collector(stopped_by_me={"f1"}), "https://ntfy.sh/stx-secret", transport=transport)
    assert not a.notify(docker_event_to_event({"action": "die", "name": "flextrack-web", "id": "f1", "exit_code": "137"}))
    assert sent == []
    await a.close()


@pytest.mark.parametrize("inspect,title", [
    ({"exit_code": 1, "oom_killed": False}, "FlexTrack API stopped"),
    ({"exit_code": 137, "oom_killed": True, "memory_limit": 512 * 1024 ** 2}, "FlexTrack API ran out of memory"),
])
async def test_a_crash_files_a_failure_report(tmp_path, inspect, title):
    col, events, _ = reports_collector(tmp_path, inspect)
    col.app_for_container = lambda cid: {"id": "flextrack-api", "name": "FlexTrack API", "links": []}
    report = await FailureReports(col).container_died({"action": "die", "id": "abc123", "name": "flextrack-api"})
    assert report["title"] == title
    assert events[0]["report"] == report["id"]


async def test_a_clean_stop_files_nothing(tmp_path):
    col, _, _ = reports_collector(tmp_path, {"exit_code": 0, "oom_killed": False})
    col.docker = FakeDocker({"exit_code": 0, "oom_killed": False})
    assert await FailureReports(col).container_died({"action": "die", "id": "abc123", "name": "flextrack-web"}) is None


async def test_start_stop_restart_go_through_the_control_proxy(tmp_path):
    posted = []

    def handler(request):
        posted.append((request.url.path, request.url.params.get("t")))
        return httpx.Response(204)

    web, api = flextrack("web"), flextrack("api", "exited")
    col, actions, _, _ = make_collector(tmp_path, [web, api], handler)
    assert actions.allowed(web) == ["restart", "stop"]
    assert actions.allowed(api) == ["start"]
    (tmp_path / "run/stx-helper").mkdir(parents=True)
    (tmp_path / "run/stx-helper/helper.sock").write_text("")  # the helper is installed
    assert actions.allowed(web) == ["restart", "stop", "update"]
    assert actions.allowed(api) == ["start", "update"]

    async def running(cid):
        return True
    actions._running = running
    actions.request("stop", "flextrack-web", "isaac", "iPhone")
    actions.request("restart", "flextrack-web", "isaac", "iPhone")
    actions.request("start", "flextrack-api", "isaac", "iPhone")
    await asyncio.gather(*actions.tasks)
    assert [p for p, _ in posted] == [f"/containers/{web['id']}/stop", f"/containers/{web['id']}/restart",
                                      f"/containers/{api['id']}/start"]
    assert actions.expected_stop(web["id"])  # so the stop doesn't buzz the phone
    assert [h["status"] for h in col.store.list_actions()] == ["ok", "ok", "ok"]


def test_update_recreates_the_compose_service(monkeypatch, tmp_path):
    """FlexTrack's compose file sets pull_policy: build, so the pull skips it and up rebuilds it."""
    mod = load_helper()
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    labels = {"com.docker.compose.project": "opengym", "com.docker.compose.service": "api",
              "com.docker.compose.project.working_dir": str(tmp_path),
              "com.docker.compose.project.config_files": str(compose)}
    images = iter(["sha256:old", "sha256:new"])
    monkeypatch.setattr(mod, "_inspect", lambda name: {"Config": {"Labels": labels}, "Image": next(images)})
    ran = []
    monkeypatch.setattr(mod, "run", lambda cmd, emit, timeout=900: (ran.append(cmd), (0, []))[1])
    assert mod.cmd_update({"container": "flextrack-api"}, print) == (True, "Updated to a new image")
    base = ["docker", "compose", "-p", "opengym", "--project-directory", str(tmp_path), "-f", str(compose)]
    assert ran == [base + ["pull", "api"], base + ["up", "-d", "--no-deps", "api"]]


def load_backup():
    spec = importlib.util.spec_from_file_location("stx_backup", BACKUP_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nightly_backup_includes_flextrack_data_without_pausing_it(monkeypatch, tmp_path):
    mod = load_backup()
    data, coach = tmp_path / "opengym/data", tmp_path / "opengym/coach-auth"
    (data / "uploads").mkdir(parents=True)
    coach.mkdir()
    (data / "db.json").write_text("{}")
    (data / "state-u1.json").write_text("{}")
    sonarr = tmp_path / "media/sonarr"
    sonarr.mkdir(parents=True)
    (sonarr / "sonarr.db").write_text("")

    def mount(src, dst, rw=True):
        return {"Type": "bind", "Source": str(src), "Destination": dst, "RW": rw}
    inspect = [
        {"Name": "/flextrack-api", "State": {"Running": True}, "Config": {"Labels": {"stx.app": "flextrack-api"}},
         "Mounts": [mount(data, "/data"), mount(coach, "/coach-auth")]},
        {"Name": "/flextrack-web", "State": {"Running": True}, "Config": {"Labels": {"stx.app": "flextrack-web"}},
         "Mounts": []},
        {"Name": "/sonarr", "State": {"Running": True}, "Config": {"Labels": {}}, "Mounts": [mount(sonarr, "/config")]},
    ]

    def docker(*args):
        if args[0] == "ps":
            return "flextrack-api\nflextrack-web\nsonarr\n"
        return json.dumps(inspect)
    monkeypatch.setattr(mod, "docker", docker)
    apps, skipped = mod.discover(dict(mod.DEFAULTS))
    by_name = {a["name"]: a for a in apps}
    assert set(by_name) == {"flextrack-api", "sonarr"}  # the web container keeps nothing
    assert by_name["flextrack-api"]["paths"] == sorted([str(coach), str(data)])
    # JSON written atomically, no SQLite: copied as it is, never paused (unlike Sonarr)
    assert by_name["flextrack-api"]["sqlite"] is False
    assert by_name["sonarr"]["sqlite"] is True
    assert skipped == []
