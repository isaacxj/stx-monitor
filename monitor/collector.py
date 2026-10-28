"""Background loops that keep the latest state in memory and fan it out to browsers.

asyncio runs all of these on one thread, taking turns whenever one is waiting.
The host sample does blocking file reads, so it runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections import deque
from typing import Any

from . import __version__
from . import tailscale
from .apps import AppsFile, build_map
from .config import Settings
from .docker_client import DockerClient
from .history import History
from .host import HostCollector
from .journal import Journal
from .logs import LogHub
from .reports import FailureReports, read_boot_id
from .store import Store

log = logging.getLogger(__name__)

HOST_EVERY = 3.0
DOCKER_EVERY = 6.0
TAILSCALE_EVERY = 30.0
PERSIST_EVERY = 300.0
HOT_C = 90.0
COOL_C = 85.0
FULL_PCT = 90.0
FREED_PCT = 88.0
MAX_EVENTS = 200


class HostEdges:
    """Turns a stream of samples into events, firing only when something crosses a line.

    Each check remembers the previous state ("edge detection"), so a CPU that
    sits at 95 °C for an hour produces one event, not 1,200.
    """

    def __init__(self) -> None:
        self.ac: bool | None = None
        self.on_battery_since: float | None = None
        self.hot = False
        self.full: set[str] = set()

    def check(self, s: dict) -> list[dict]:
        out = []
        ps = s.get("power_supply") or {}
        ac = ps.get("ac_online")
        battery = ps.get("battery") or {}
        if ac is not None and self.ac is not None and ac != self.ac:
            if not ac:
                self.on_battery_since = s["time"]
                out.append(event("power", "warn", "Running on battery",
                                 f"Wall power may be out. Battery at {battery.get('capacity', '?')}%."))
            else:
                mins = None
                if self.on_battery_since:
                    mins = max(1, round((s["time"] - self.on_battery_since) / 60))
                detail = f"The server ran on battery for {mins} min." if mins else "Back on wall power."
                out.append(event("power", "ok", "Wall power is back", detail))
                self.on_battery_since = None
        if ac is not None:
            self.ac = ac

        temp = s.get("cpu_temp")
        if temp is not None:
            if not self.hot and temp >= HOT_C:
                self.hot = True
                out.append(event("heat", "warn", f"CPU is hot: {temp:.0f} °C",
                                 "It will slow itself down to cool off. Check the fans and vents."))
            elif self.hot and temp <= COOL_C:
                self.hot = False
                out.append(event("heat", "ok", f"CPU cooled down to {temp:.0f} °C", ""))

        for m in s.get("mounts") or []:
            mp, pct = m["mountpoint"], m.get("percent")
            if pct is None:
                continue
            if mp not in self.full and pct >= FULL_PCT:
                self.full.add(mp)
                out.append(event("storage", "warn", f"{mp} is {pct:.0f}% full",
                                 f"{human_bytes(m.get('free'))} left."))
            elif mp in self.full and pct <= FREED_PCT:
                self.full.discard(mp)
                out.append(event("storage", "ok", f"{mp} has room again", f"Now {pct:.0f}% full."))
        return out


_ids = itertools.count(1)


def event(kind: str, level: str, title: str, detail: str = "", **extra: Any) -> dict:
    return {"id": next(_ids), "time": time.time(), "kind": kind, "level": level,
            "title": title, "detail": detail, **extra}


def human_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def docker_event_to_event(ev: dict) -> dict | None:
    name = ev.get("name") or ev.get("id")
    action = ev.get("action") or ""
    extra = {"container": ev.get("id"), "name": name}
    if action == "start":
        return event("app", "ok", f"{name} started", "", **extra)
    if action == "die":
        code = str(ev.get("exit_code") or "0")
        detail = {
            "0": "It exited normally.",
            "137": "Exit code 137: it was killed, usually for running out of memory or a forced stop.",
            "143": "Exit code 143: it was asked to stop.",
        }.get(code, f"Exit code {code}: it crashed or quit with an error.")
        level = "info" if code in ("0", "143") else "error"
        return event("app", level, f"{name} stopped", detail, **extra)
    if action == "oom":
        return event("app", "error", f"{name} ran out of memory", "Linux ended it because it hit its memory limit.", **extra)
    if action == "restart":
        return event("app", "info", f"{name} restarted", "", **extra)
    if action.startswith("health_status"):
        state = action.split(":", 1)[-1].strip()
        if state == "unhealthy":
            return event("app", "error", f"{name} is unhealthy", "Its health check is failing.", **extra)
        if state == "healthy":
            return event("app", "ok", f"{name} is healthy", "", **extra)
        return None
    if action == "destroy":
        return event("app", "info", f"{name} was removed", "", **extra)
    if action in ("pause", "unpause"):
        return event("app", "info", f"{name} {'paused' if action == 'pause' else 'resumed'}", "", **extra)
    return None


class Collector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.host = HostCollector(settings.host_root, settings.power_rate)
        self.docker = DockerClient(settings.docker_host) if settings.docker_enabled else None
        self.apps_file = AppsFile(settings.apps_file)
        self.store = Store(settings.data_dir / "monitor.db")
        self.history = History()
        self.edges = HostEdges()
        self.journal = Journal(settings.host_root)
        self.logs = LogHub()
        self.reports = FailureReports(self)
        self.actions = None  # set by main once the action runner exists
        self._readings_at = 0.0
        self.boot_id: str | None = None
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self.subscribers: set[asyncio.Queue] = set()
        self.host_sample: dict | None = None
        self.containers: list[dict] = []
        self.stats: dict[str, dict] = {}
        self.docker_info: dict | None = None
        self.docker_ok: bool | None = None
        self.docker_error: str | None = None
        self.events_connected = False
        self.tailnet: dict | None = None
        self.started = time.time()
        self._tasks: list[asyncio.Task] = []
        self._docker_refresh = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        loaded = self.history.load(self.store)
        old_json = self.settings.data_dir / "history.json"
        if not loaded and old_json.exists():
            loaded = self.history.import_json(old_json)
            old_json.rename(old_json.with_suffix(".json.imported"))
        if loaded:
            log.info("restored %d minutes of history", loaded)
        self.events.extend(self.store.recent_events(MAX_EVENTS))
        self.add_event(event("system", "info", "stx-monitor started",
                             f"Build {self.settings.git_sha}, version {__version__}."))
        # Fill everything once before serving so the first page load is complete.
        await self._host_once()
        await asyncio.gather(self._docker_once(), self._tailscale_once(), return_exceptions=True)
        self._tasks = [
            asyncio.create_task(self._every(HOST_EVERY, self._host_once), name="host"),
            asyncio.create_task(self._docker_loop(), name="docker"),
            asyncio.create_task(self._every(TAILSCALE_EVERY, self._tailscale_once), name="tailscale"),
            asyncio.create_task(self._every(PERSIST_EVERY, self._persist), name="persist"),
            asyncio.create_task(self.logs.follow_journal(self.journal), name="journal"),
            asyncio.create_task(self._check_previous_boot(), name="previous-boot"),
        ]
        if self.docker:
            self._tasks.append(asyncio.create_task(
                self.docker.watch_events(self._on_docker_event, self._on_events_state), name="events"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.logs.stop()
        await self._persist()
        if self.docker:
            await self.docker.close()
        self.store.close()

    async def _every(self, seconds: float, fn) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("%s failed", getattr(fn, "__name__", fn))

    # ---------------------------------------------------------------- host
    async def _host_once(self) -> None:
        s = await asyncio.to_thread(self.host.sample)
        self.host_sample = s
        nics = [n for n in s.get("network") or [] if n.get("physical")]
        rx = sum(n["rx_rate"] or 0 for n in nics) if nics and any(n["rx_rate"] is not None for n in nics) else None
        tx = sum(n["tx_rate"] or 0 for n in nics) if nics and any(n["tx_rate"] is not None for n in nics) else None
        point = self.history.add(s["time"], {
            "cpu": (s.get("cpu") or {}).get("percent"),
            "mem": (s.get("memory") or {}).get("percent"),
            "temp": s.get("cpu_temp"),
            "rx": rx,
            "tx": tx,
            "watts": (s.get("power") or {}).get("watts"),
        })
        for ev in self.edges.check(s):
            self.add_event(ev)
        if s["time"] - self._readings_at >= 30:
            self._save_readings(s)
        self.apps_file.reload_if_changed()
        self.broadcast("tick", {
            "host": s,
            "containers": self.container_list(),
            "apps": self.apps_map(),
            "point": point,
            "docker": self.docker_state(),
        })

    # -------------------------------------------------------------- docker
    async def _docker_loop(self) -> None:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._docker_refresh.wait(), DOCKER_EVERY)
            self._docker_refresh.clear()
            try:
                await self._docker_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("docker poll failed")

    async def _docker_once(self) -> None:
        if not self.docker:
            return
        try:
            containers = await self.docker.containers()
            running = [c["id"] for c in containers if c["state"] == "running"]
            self.stats = await self.docker.all_stats(running)
            self.containers = containers
            self.logs.sync(self.docker, containers, self._tty_of)
            if self.docker_info is None or not self.docker_ok:
                self.docker_info = await self.docker.info()
            if self.docker_ok is False:
                self.add_event(event("system", "ok", "Docker is reachable again", ""))
            self.docker_ok, self.docker_error = True, None
        except Exception as exc:  # noqa: BLE001
            if self.docker_ok is not False:
                log.warning("docker unreachable: %s", exc)
                self.add_event(event("system", "error", "Can't reach Docker",
                                     "The socket proxy is not answering. Is stx-docker-proxy running?"))
            self.docker_ok, self.docker_error = False, str(exc)

    async def _on_docker_event(self, ev: dict) -> None:
        nice = docker_event_to_event(ev)
        if nice:
            self.add_event(nice)
        if ev.get("action") == "die":
            try:
                await self.reports.container_died(ev)
            except Exception:  # noqa: BLE001
                log.exception("could not save a failure report for %s", ev.get("name"))
        self._docker_refresh.set()  # refresh the container list now rather than in 6 s

    async def _tty_of(self, cid: str) -> bool:
        return bool((await self.docker.inspect(cid)).get("tty"))

    def _on_events_state(self, connected: bool) -> None:
        self.events_connected = connected

    def container_list(self) -> list[dict]:
        return [{**c, "stats": self.stats.get(c["id"])} for c in self.containers]

    def docker_state(self) -> dict:
        return {
            "enabled": self.docker is not None,
            "ok": self.docker_ok,
            "error": self.docker_error,
            "events": self.events_connected,
            "info": self.docker_info,
        }

    def apps_map(self) -> dict:
        m = build_map(self.apps_file.groups, self.apps_file.apps, self.containers, self.stats)
        m["error"] = self.apps_file.error
        return m

    def app_for_container(self, cid: str) -> dict | None:
        for g in self.apps_map()["groups"]:
            for a in g["apps"]:
                if a.get("container") and a["container"]["id"] == cid[:12]:
                    return a
        return None

    def app_name(self, app_id: str) -> str:
        for a in self.apps_file.apps:
            if a["id"] == app_id:
                return a["name"]
        return app_id

    def _save_readings(self, s: dict) -> None:
        """The last battery, power and heat readings, so a crash report can say what led up to it."""
        self._readings_at = s["time"]
        if self.boot_id is None:
            self.boot_id = read_boot_id(self)
        ps = s.get("power_supply") or {}
        self.store.set("last_readings", {
            "time": s["time"],
            "boot": self.boot_id,
            "ac": ps.get("ac_online"),
            "battery": (ps.get("battery") or {}).get("capacity"),
            "temp": s.get("cpu_temp"),
            "watts": (s.get("power") or {}).get("watts"),
        })

    async def _check_previous_boot(self) -> None:
        try:
            report = await self.reports.check_previous_boot()
            if report:
                log.warning("previous boot ended without a clean shutdown: %s", report["summary"])
        except Exception:  # noqa: BLE001
            log.exception("could not check the previous boot")

    def find_container(self, key: str) -> dict | None:
        for c in self.containers:
            if key in (c["id"], c["full_id"], c["name"]):
                return c
        if len(key) >= 4:
            for c in self.containers:
                if (c["full_id"] or "").startswith(key):
                    return c
        return None

    # ----------------------------------------------------------- tailscale
    async def _tailscale_once(self) -> None:
        try:
            self.tailnet = await tailscale.read_status(self.settings.host_root)
        except Exception as exc:  # noqa: BLE001
            log.debug("tailscale status failed: %s", exc)
            self.tailnet = None

    # ------------------------------------------------------------- persist
    async def _persist(self) -> None:
        try:
            await asyncio.to_thread(self.history.save, self.store)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save history: %s", exc)

    # -------------------------------------------------------------- events
    def add_event(self, ev: dict) -> dict:
        ev = self.store.add_event(ev)
        self.events.append(ev)
        self.broadcast("event", ev)
        return ev

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def broadcast(self, kind: str, data: Any) -> None:
        for q in list(self.subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # a slow browser loses the oldest message, not the newest
            q.put_nowait((kind, data))

    # ------------------------------------------------------------ overview
    def overview(self) -> dict:
        return {
            "identity": self.host.identity,
            "host": self.host_sample,
            "containers": self.container_list(),
            "apps": self.apps_map(),
            "history": self.history.snapshot(),
            "events": list(self.events),
            "tailnet": self.tailnet,
            "docker": self.docker_state(),
            "build": {"sha": self.settings.git_sha, "version": __version__, "started": self.started},
            "reports": self.reports.summary(),
            "power_rate": self.settings.power_rate,
            "owner": self.settings.owner,
        }
