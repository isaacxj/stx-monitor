"""Actions: start, stop, restart and update apps; clean up, restart Docker, reboot.

Two narrow doors, so no single part can do everything:
- Container start, stop and restart go through a second socket proxy
  (stx-docker-control) that allows only those POST calls.
- Anything that needs the host itself (shutdown, reboot, restarting Docker,
  image cleanup, pulling and recreating a compose service) goes to stx-helper,
  a small systemd service on the host that runs a fixed list of jobs.

Every action, including refused ones, is written to the history table.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx

from .docker_client import parse_docker_host

if TYPE_CHECKING:
    from .collector import Collector

log = logging.getLogger(__name__)

PROTECTED = {"stx-monitor", "stx-docker-proxy", "stx-docker-control"}
SELF_NAME = "stx-monitor"
HELPER_SOCKET = "run/stx-helper/helper.sock"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
EXPECTED_STOP_WINDOW = 120.0
UPDATE_CHECK_EVERY = 12 * 3600
DEFAULT_AUTOPILOT = {"enabled": True, "threshold": 20}

CONTAINER_ACTIONS = ("start", "stop", "restart", "update")
SERVER_ACTIONS = ("prune-images", "restart-docker", "reboot")


class ActionError(Exception):
    """The request was refused before anything happened."""


class HelperClient:
    def __init__(self, root: Path):
        self.path = root / HELPER_SOCKET

    @property
    def installed(self) -> bool:
        return self.path.exists()

    async def call(self, cmd: str, args: dict | None = None, on_log: Callable[[str], None] | None = None,
                   timeout: float = 900.0) -> tuple[bool, str]:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.path)), 5.0)
        try:
            writer.write((json.dumps({"cmd": cmd, "args": args or {}}) + "\n").encode())
            await writer.drain()
            deadline = time.monotonic() + timeout
            while True:
                line = await asyncio.wait_for(reader.readline(), max(1.0, deadline - time.monotonic()))
                if not line:
                    return False, "The helper closed the connection"
                msg = json.loads(line)
                if msg.get("type") == "log":
                    if on_log:
                        on_log(str(msg.get("text", "")))
                elif msg.get("type") == "done":
                    return bool(msg.get("ok")), str(msg.get("detail", ""))
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class ControlClient:
    """POST start/stop/restart through the stx-docker-control proxy."""

    def __init__(self, docker_host: str, transport: httpx.AsyncBaseTransport | None = None):
        base_url, uds = parse_docker_host(docker_host)
        if transport is None:
            transport = httpx.AsyncHTTPTransport(uds=uds) if uds else httpx.AsyncHTTPTransport()
        self.http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=90.0)

    async def post(self, cid: str, verb: str, wait: int = 20) -> None:
        params = {"t": wait} if verb in ("stop", "restart") else None
        r = await self.http.post(f"/containers/{cid}/{verb}", params=params)
        if r.status_code in (204, 304):  # 304: it was already in that state
            return
        try:
            message = r.json().get("message")
        except ValueError:
            message = r.text.strip()
        raise RuntimeError(message or f"Docker answered {r.status_code}")

    async def close(self) -> None:
        await self.http.aclose()


class Actions:
    def __init__(self, collector: "Collector", control_host: str):
        self.c = collector
        self.store = collector.store
        self.helper = HelperClient(collector.settings.host_root)
        self.control = ControlClient(control_host) if control_host.lower() not in ("", "none", "off") else None
        self.expected: dict[str, float] = {}
        self.tasks: set[asyncio.Task] = set()
        self.updates: dict[str, dict] = {}
        self._low_battery_samples = 0
        self.autopilot_fired = False
        self.store.fail_running_actions("Cut off: stx-monitor restarted while it was running")

    async def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        if self.control:
            await self.control.close()

    # ------------------------------------------------------------ what's allowed
    def capabilities(self) -> dict:
        return {"control": self.control is not None and bool(self.c.docker), "helper": self.helper.installed}

    @staticmethod
    def is_protected(c: dict) -> bool:
        return c["name"] in PROTECTED or (c.get("labels") or {}).get("stx.protected") == "true"

    def allowed(self, c: dict) -> list[str]:
        caps = self.capabilities()
        if not caps["control"]:
            return []
        out = []
        if c["state"] == "running":
            out.append("restart")
            if not self.is_protected(c):
                out.append("stop")
        else:
            out.append("start")
        if caps["helper"] and not self.is_protected(c) and c.get("compose_project"):
            out.append("update")
        return out

    def expect_stop(self, cid: str) -> None:
        self.expected[cid[:12]] = time.time()

    def expected_stop(self, cid: str) -> bool:
        t = self.expected.get(cid[:12])
        return t is not None and time.time() - t < EXPECTED_STOP_WINDOW

    def autopilot(self) -> dict:
        return {**DEFAULT_AUTOPILOT, **(self.store.get("autopilot") or {})}

    # --------------------------------------------------------------- requests
    def request(self, action: str, target: str, actor: str, device: str, confirm: str | None = None) -> dict:
        """Validate, record and start an action. Returns the history row straight away."""
        try:
            job, target_name = self._prepare(action, target, confirm)
        except ActionError as exc:
            row = self.store.add_action(actor, device, action, target[:128], "refused", str(exc))
            self.c.broadcast("action", row)
            raise
        row = self.store.add_action(actor, device, action, target_name, "running")
        self.c.broadcast("action", row)
        task = asyncio.create_task(self._run(row, job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return row

    def _prepare(self, action: str, target: str, confirm: str | None):
        caps = self.capabilities()
        if action in CONTAINER_ACTIONS:
            if not NAME_RE.match(target or ""):
                raise ActionError("Bad container name")
            c = self.c.find_container(target)
            if not c:
                raise ActionError(f"No container called {target}")
            if action not in self.allowed(c):
                if action == "stop" and self.is_protected(c):
                    raise ActionError(f"{c['name']} keeps the console running, so it can't be stopped from here")
                if action == "update" and not caps["helper"]:
                    raise ActionError("Updates need stx-helper on the server")
                raise ActionError(f"Can't {action} {c['name']} right now")
            return (lambda row: self._container(row, action, c)), c["name"]
        if action in SERVER_ACTIONS:
            if not caps["helper"]:
                raise ActionError("This needs stx-helper on the server. Run scripts/install-helper.sh with sudo.")
            if action == "reboot":
                hostname = self.c.host.identity.get("hostname") or ""
                if (confirm or "").strip() != hostname:
                    raise ActionError(f"Type {hostname} to confirm the reboot")
            return (lambda row: self._server(row, action)), "server"
        raise ActionError(f"Unknown action {action}")

    async def _run(self, row: dict, job) -> None:
        try:
            ok, detail = await job(row)
        except Exception as exc:  # noqa: BLE001
            log.exception("action %s failed", row["action"])
            ok, detail = False, str(exc) or type(exc).__name__
        current = self.store.get_action(row["id"])
        if current and current["status"] == "running":
            current = self.store.finish_action(row["id"], "ok" if ok else "failed", detail)
            self.c.broadcast("action", current)
        self.c.request_docker_refresh()

    def _finish_now(self, row: dict, detail: str) -> None:
        """For actions that end with us being restarted: record success before it happens."""
        done = self.store.finish_action(row["id"], "ok", detail)
        self.c.broadcast("action", done)

    # -------------------------------------------------------------- containers
    async def _container(self, row: dict, action: str, c: dict) -> tuple[bool, str]:
        cid, name = c["id"], c["name"]
        started = time.monotonic()
        if action == "update":
            self.expect_stop(cid)
            lines: list[str] = []
            ok, detail = await self.helper.call("update", {"container": name}, on_log=lines.append)
            self.updates.pop(cid, None)
            return ok, detail
        if action in ("stop", "restart"):
            self.expect_stop(cid)
        if name == SELF_NAME and action == "restart":
            self._finish_now(row, "stx-monitor restarts itself; the page reconnects in a few seconds")
        await self.control.post(cid, action)
        if action == "stop":
            return True, f"Stopped in {time.monotonic() - started:.0f} s"
        # Give it a few seconds and check it stayed up.
        verb = "Started" if action == "start" else "Restarted"
        await asyncio.sleep(3)
        if await self._running(cid):
            return True, f"{verb} in {time.monotonic() - started:.0f} s"
        state = await self.c.docker.inspect(cid, fresh=True) if self.c.docker else {}
        return False, f"It stopped again right away (exit code {state.get('exit_code', '?')})"

    async def _running(self, cid: str) -> bool:
        try:
            for c in await self.c.docker.containers():
                if c["id"] == cid[:12]:
                    return c["state"] == "running"
        except Exception:  # noqa: BLE001
            pass
        return False

    # ------------------------------------------------------------------ server
    async def _server(self, row: dict, action: str) -> tuple[bool, str]:
        if action == "prune-images":
            return await self.helper.call("prune-images")
        if action == "restart-docker":
            self._finish_now(row, "Docker and every app restart; this page reconnects in about 30 seconds")
            return await self.helper.call("restart-docker")
        if action == "reboot":
            self._finish_now(row, "Rebooting; the console is back in about 90 seconds")
            return await self.helper.call("reboot")
        return False, "Unknown action"

    # ---------------------------------------------------------------- autopilot
    def set_autopilot(self, enabled: bool, threshold: int, actor: str, device: str) -> dict:
        threshold = max(5, min(80, int(threshold)))
        self.store.set("autopilot", {"enabled": bool(enabled), "threshold": threshold})
        what = f"on, shut down at {threshold}%" if enabled else "off"
        row = self.store.add_action(actor, device, "autopilot", "server", "ok", f"Power-outage autopilot {what}")
        self.c.broadcast("action", row)
        return self.autopilot()

    def check_autopilot(self, sample: dict) -> None:
        """Called with every host sample. Two low readings in a row on battery trigger the shutdown."""
        cfg = self.autopilot()
        ps = sample.get("power_supply") or {}
        battery = ps.get("battery") or {}
        capacity = battery.get("capacity")
        if not cfg["enabled"] or ps.get("ac_online") is not False or capacity is None:
            self._low_battery_samples = 0
            return
        if capacity > cfg["threshold"]:
            self._low_battery_samples = 0
            return
        self._low_battery_samples += 1
        if self._low_battery_samples >= 2 and not self.autopilot_fired:
            self.autopilot_fired = True
            task = asyncio.create_task(self._autopilot_shutdown(capacity, cfg["threshold"]))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _autopilot_shutdown(self, capacity: int, threshold: int) -> None:
        row = self.store.add_action("Autopilot", "", "shutdown", "server", "running",
                                    f"Battery at {capacity}%, below {threshold}%")
        self.c.broadcast("action", row)
        self.c.add_event({"kind": "power", "level": "warn", "title": f"Battery at {capacity}%: shutting down cleanly",
                          "detail": "Power-outage autopilot is stopping the apps, then the server."})
        if not self.helper.installed:
            detail = "Could not shut down: stx-helper is not installed"
            self.c.broadcast("action", self.store.finish_action(row["id"], "failed", detail))
            self.c.add_event({"kind": "power", "level": "error", "title": "Autopilot could not shut down", "detail": detail})
            return
        stopped = []
        if self.control:
            running = [c for c in self.c.containers if c["state"] == "running" and not self.is_protected(c)]
            for c in running:
                self.expect_stop(c["id"])
            results = await asyncio.gather(*(self.control.post(c["id"], "stop", wait=30) for c in running),
                                           return_exceptions=True)
            stopped = [c for c, r in zip(running, results) if not isinstance(r, Exception)]
        ps = (self.c.host_sample or {}).get("power_supply") or {}
        if ps.get("ac_online"):
            # Wall power came back while the apps were stopping: undo instead of shutting down.
            for c in stopped:
                with contextlib.suppress(Exception):
                    await self.control.post(c["id"], "start")
            self.autopilot_fired = False
            self.c.broadcast("action", self.store.finish_action(
                row["id"], "ok", "Wall power came back, so the server stayed on and the apps were started again"))
            return
        self.c.broadcast("action", self.store.finish_action(
            row["id"], "ok", f"Stopped {len(stopped)} apps, then shut the server down"))
        await self._persist_before_poweroff()
        await self.helper.call("poweroff")

    async def _persist_before_poweroff(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.c.history.save, self.store)

    # ------------------------------------------------------------------ updates
    async def check_updates(self) -> None:
        """Ask the registry whether each running image has a newer version under the same tag."""
        docker = self.c.docker
        if not docker:
            return
        for c in list(self.c.containers):
            ref = c.get("image") or ""
            if c["state"] != "running" or not ref or ref.startswith("sha256:") or self.is_protected(c):
                continue
            entry = {"checked": time.time(), "available": None, "error": None}
            try:
                local = await docker.image_digests(ref)
                if not local:
                    entry["error"] = "Built locally"
                else:
                    remote = await docker.remote_digest(ref)
                    entry["available"] = bool(remote) and not any(d.endswith(remote) for d in local)
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)[:200]
            self.updates[c["id"]] = entry
            await asyncio.sleep(1)  # be gentle with registry rate limits

    # ------------------------------------------------------------------- view
    async def control_view(self) -> dict:
        prune = None
        if self.c.docker:
            with contextlib.suppress(Exception):
                prune = await self.c.docker.unused_image_bytes()
        containers = []
        for c in self.c.containers:
            app = self.c.app_for_container(c["id"])
            containers.append({
                "id": c["id"], "name": c["name"], "app": app["id"] if app else None,
                "display": app["name"] if app else c["name"], "state": c["state"], "status": c["status"],
                "health": c.get("health"), "protected": self.is_protected(c), "actions": self.allowed(c),
                "update": self.updates.get(c["id"]),
            })
        return {
            "capabilities": self.capabilities(),
            "containers": containers,
            "autopilot": self.autopilot(),
            "prune_bytes": prune,
            "history": self.store.list_actions(40),
            "hostname": self.c.host.identity.get("hostname"),
        }
