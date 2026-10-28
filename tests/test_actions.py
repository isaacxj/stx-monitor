import asyncio
import importlib.util
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from monitor.actions import ActionError, Actions, ControlClient, HelperClient
from monitor.store import Store

HELPER_FILE = Path(__file__).resolve().parent.parent / "host" / "stx-helper.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("stx_helper", HELPER_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def helper_server(tmp_path):
    """Run the real helper on a socket inside tmp_path."""
    mod = load_helper()
    sock = tmp_path / "run/stx-helper/helper.sock"
    sock.parent.mkdir(parents=True)
    calls = []
    mod.cmd_poweroff = lambda args, emit: (calls.append("poweroff"), (True, "Shutting down"))[1]
    mod.cmd_prune_images = lambda args, emit: (emit("deleting"), (True, "Freed 1.4GB"))[1]
    mod.COMMANDS["poweroff"] = (mod.cmd_poweroff, True)
    mod.COMMANDS["prune-images"] = (mod.cmd_prune_images, True)
    server = mod.Server(str(sock), mod.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield SimpleNamespace(root=tmp_path, calls=calls)
    server.shutdown()
    server.server_close()


@pytest.mark.skipif(os.geteuid() != 0, reason="the helper only answers root")
async def test_helper_protocol(helper_server):
    client = HelperClient(helper_server.root)
    assert client.installed
    assert await client.call("ping") == (True, "stx-helper 1")
    logs = []
    assert await client.call("prune-images", on_log=logs.append) == (True, "Freed 1.4GB")
    assert logs == ["deleting"]
    ok, detail = await client.call("rm -rf /")
    assert not ok and "Unknown command" in detail
    ok, detail = await client.call("update", {"container": "../etc"})
    assert (ok, detail) == (False, "Bad container name")
    ok, detail = await client.call("update", {"container": "stx-monitor"})
    assert not ok  # no docker here, or protected


def test_helper_refuses_protected_update(monkeypatch):
    mod = load_helper()
    monkeypatch.setattr(mod, "_inspect", lambda name: {"Config": {"Labels": {"com.docker.compose.project": "stx"}}})
    assert mod.cmd_update({"container": "stx-monitor"}, print) == (False, "stx-monitor is protected; update it from the server")
    monkeypatch.setattr(mod, "_inspect", lambda name: {"Config": {"Labels": {}}})
    ok, detail = mod.cmd_update({"container": "jellyfin"}, print)
    assert not ok and "docker compose" in detail


def make_collector(tmp_path, containers, control_handler=None):
    store = Store(tmp_path / "m.db")
    events, broadcasts = [], []
    col = SimpleNamespace(
        store=store,
        settings=SimpleNamespace(host_root=tmp_path),
        containers=containers,
        docker=SimpleNamespace(),
        host=SimpleNamespace(identity={"hostname": "stx-server"}),
        host_sample={"power_supply": {"ac_online": False}},
        history=SimpleNamespace(save=lambda store: None),
        add_event=lambda ev: events.append(ev) or ev,
        broadcast=lambda kind, data: broadcasts.append((kind, data)),
        request_docker_refresh=lambda: None,
        app_for_container=lambda cid: None,
    )
    col.find_container = lambda key: next((c for c in containers if key in (c["id"], c["name"])), None)
    actions = Actions(col, "none")
    if control_handler:
        actions.control = ControlClient("tcp://127.0.0.1:2376", transport=httpx.MockTransport(control_handler))
    return col, actions, events, broadcasts


def container(name, state="running", labels=None):
    return {"id": name[:12].ljust(12, "0"), "name": name, "state": state, "status": "", "labels": labels or {},
            "compose_project": "stx"}


def test_rules_for_what_is_allowed(tmp_path):
    seen = []
    _, actions, _, _ = make_collector(tmp_path, [], lambda r: seen.append(r) or httpx.Response(204))
    assert actions.allowed(container("jellyfin")) == ["restart", "stop"]
    assert actions.allowed(container("radarr", "exited")) == ["start"]
    assert actions.allowed(container("stx-monitor")) == ["restart"]  # can't stop itself
    assert actions.allowed(container("stx-docker-control")) == ["restart"]
    assert actions.allowed(container("x", labels={"stx.protected": "true"})) == ["restart"]
    _, no_control, _, _ = make_collector(tmp_path / "b", [])
    assert no_control.allowed(container("jellyfin")) == []


async def test_requests_are_validated_and_recorded(tmp_path):
    posted = []

    def handler(request):
        posted.append((request.method, request.url.path, request.url.params.get("t")))
        return httpx.Response(204)

    col, actions, _, broadcasts = make_collector(tmp_path, [container("stx-monitor"), container("radarr", "exited")], handler)
    with pytest.raises(ActionError, match="can't be stopped"):
        actions.request("stop", "stx-monitor", "isaac", "iPhone")
    with pytest.raises(ActionError, match="stx-helper"):
        actions.request("reboot", "", "isaac", "iPhone", confirm="stx-server")
    (tmp_path / "run/stx-helper").mkdir(parents=True)
    (tmp_path / "run/stx-helper/helper.sock").write_text("")  # pretend the helper is installed
    with pytest.raises(ActionError, match="Type stx-server"):
        actions.request("reboot", "", "isaac", "iPhone", confirm="stx")
    with pytest.raises(ActionError, match="Bad container"):
        actions.request("start", "../../etc", "isaac", "iPhone")

    async def running(cid):
        return True
    actions._running = running
    row = actions.request("start", "radarr", "isaac", "iPhone")
    assert row["status"] == "running"
    await asyncio.gather(*actions.tasks)
    assert posted == [("POST", "/containers/radarr000000/start", None)]
    history = col.store.list_actions()
    assert [h["status"] for h in history] == ["ok", "refused", "refused", "refused", "refused"]
    assert history[0]["actor"] == "isaac" and history[0]["device"] == "iPhone"
    assert any(kind == "action" for kind, _ in broadcasts)


async def test_stop_marks_expected_and_uses_timeout(tmp_path):
    posted = []
    _, actions, _, _ = make_collector(tmp_path, [container("jellyfin")],
                                      lambda r: posted.append(r.url.params.get("t")) or httpx.Response(204))
    actions.request("stop", "jellyfin", "isaac", "laptop")
    await asyncio.gather(*actions.tasks)
    assert posted == ["20"]
    assert actions.expected_stop("jellyfin0000")


async def test_autopilot_shuts_down_after_two_low_readings(tmp_path):
    stops = []
    col, actions, events, _ = make_collector(
        tmp_path, [container("jellyfin"), container("stx-monitor")],
        lambda r: stops.append(r.url.path) or httpx.Response(204))
    calls = []

    async def fake_call(cmd, args=None, on_log=None, timeout=900):
        calls.append(cmd)
        return True, "ok"
    (tmp_path / "run/stx-helper").mkdir(parents=True)
    (tmp_path / "run/stx-helper/helper.sock").write_text("")
    actions.helper.call = fake_call
    low = {"power_supply": {"ac_online": False, "battery": {"capacity": 19}}}
    actions.check_autopilot({"power_supply": {"ac_online": True, "battery": {"capacity": 10}}})
    actions.check_autopilot(low)
    assert not actions.tasks  # one low reading is not enough
    actions.check_autopilot(low)
    await asyncio.gather(*actions.tasks)
    assert stops == ["/containers/jellyfin0000/stop"]  # never stops itself
    assert calls == ["poweroff"]
    assert col.store.list_actions()[0]["actor"] == "Autopilot"
    assert events[0]["title"] == "Battery at 19%: shutting down cleanly"


async def test_autopilot_off_does_nothing(tmp_path):
    _, actions, _, _ = make_collector(tmp_path, [])
    actions.set_autopilot(False, 20, "isaac", "laptop")
    for _ in range(3):
        actions.check_autopilot({"power_supply": {"ac_online": False, "battery": {"capacity": 5}}})
    assert not actions.tasks
    assert actions.autopilot() == {"enabled": False, "threshold": 20}
