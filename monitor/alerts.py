"""Push alerts to my phone through ntfy (https://ntfy.sh).

ntfy is a tiny publish/subscribe service: anything POSTed to a topic shows up
as a notification on every phone subscribed to that topic. The topic name is
the only secret, so it should be long and random.

Every event the collector records passes through `Alerts.notify`. Most are
ignored; the ones worth a buzz in my pocket (an app crashing, power loss,
heat, a full disk, a sign-in) are sent in the background, never blocking the
collector. Two brakes stop a crash loop from flooding the phone: the same
alert waits ALERT_COOLDOWN before it can fire again, and at most HOURLY_LIMIT
alerts go out in any hour.

Where alerts go is set only on the server (STX_NTFY_URL), never from the web
page, so a stolen session can't redirect them somewhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from .collector import Collector

log = logging.getLogger(__name__)

ALERT_COOLDOWN = 600.0
HOURLY_LIMIT = 20
SEND_TIMEOUT = 10.0

# ntfy priorities: 1 min, 2 low, 3 default, 4 high, 5 urgent (breaks through Focus).
CATEGORIES = {
    "apps": "An app crashes, runs out of memory or fails its health check",
    "power": "Wall power is lost or back, and autopilot shutdowns",
    "heat": "The CPU runs hot",
    "storage": "A drive is over 90% full",
    "security": "Sign-ins, failed sign-ins and passkey changes",
    "system": "Docker can't be reached",
    "backups": "A nightly backup fails, or none has finished in 36 hours",
}
DEFAULT_CATEGORIES = {name: True for name in CATEGORIES}


def parse_destination(url: str) -> tuple[str, str] | None:
    """Split https://ntfy.sh/my-topic into ("https://ntfy.sh", "my-topic")."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.strip("/")
    if not path:
        return None
    base_path, _, topic = path.rpartition("/")
    if not topic:
        return None
    base = f"{parts.scheme}://{parts.netloc}" + (f"/{base_path}" if base_path else "")
    return base, topic


def classify(ev: dict) -> dict | None:
    """Which category an event belongs to and how loudly to send it, or None to stay quiet."""
    kind, level, title = ev.get("kind"), ev.get("level"), ev.get("title") or ""
    if kind == "app":
        if level != "error":
            return None
        return {"category": "apps", "priority": 4, "tags": ["rotating_light"], "key": f"app:{ev.get('name') or title}"}
    if kind == "power":
        if level == "ok":
            return {"category": "power", "priority": 3, "tags": ["electric_plug"], "key": f"power:{title}"}
        urgent = level == "error" or "shutting down" in title.lower()
        return {"category": "power", "priority": 5 if urgent else 4, "tags": ["battery"], "key": f"power:{title}"}
    if kind == "heat":
        if level == "ok":
            return {"category": "heat", "priority": 2, "tags": ["snowflake"], "key": "heat:ok"}
        return {"category": "heat", "priority": 4, "tags": ["fire"], "key": "heat:hot"}
    if kind == "storage":
        mount = title.split(" ", 1)[0]
        if level == "ok":
            return {"category": "storage", "priority": 2, "tags": ["floppy_disk"], "key": f"storage:{mount}:ok"}
        return {"category": "storage", "priority": 4, "tags": ["floppy_disk", "warning"], "key": f"storage:{mount}"}
    if kind == "security":
        priority = {"error": 5, "warn": 4}.get(level, 3)
        return {"category": "security", "priority": priority, "tags": ["key"], "key": f"security:{title}"}
    if kind == "backup":
        if level in ("error", "warn"):
            return {"category": "backups", "priority": 4, "tags": ["floppy_disk", "x"], "key": f"backup:{title}"}
        if level == "ok":
            return {"category": "backups", "priority": 2, "tags": ["white_check_mark"], "key": "backup:ok"}
        return None
    if kind == "system":
        if level == "error":
            return {"category": "system", "priority": 4, "tags": ["warning"], "key": f"system:{title}"}
        if level == "ok":
            return {"category": "system", "priority": 2, "tags": ["white_check_mark"], "key": f"system:{title}"}
    return None


class Alerts:
    def __init__(self, collector: "Collector", url: str, token: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.c = collector
        self.store = collector.store
        self.destination = parse_destination(url)
        if url and not self.destination:
            log.warning("STX_NTFY_URL is not a topic URL like https://ntfy.sh/my-topic; alerts are off")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.AsyncClient(timeout=SEND_TIMEOUT, headers=headers, transport=transport)
        self.last_fired: dict[str, float] = {}
        self.sent_times: deque[float] = deque()
        self.last_sent: dict | None = None
        self.last_error: dict | None = None
        self.tasks: set[asyncio.Task] = set()

    @property
    def configured(self) -> bool:
        return self.destination is not None

    async def close(self) -> None:
        for t in list(self.tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, 2)
        await self.http.aclose()

    # -------------------------------------------------------------- settings
    def categories(self) -> dict[str, bool]:
        saved = self.store.get("alerts") or {}
        return {**DEFAULT_CATEGORIES, **{k: bool(v) for k, v in (saved.get("categories") or {}).items() if k in CATEGORIES}}

    def set_categories(self, changes: dict[str, bool], actor: str, device: str) -> dict:
        cats = self.categories()
        for name, on in changes.items():
            if name not in CATEGORIES:
                raise ValueError(f"Unknown alert type: {name}")
            cats[name] = bool(on)
        self.store.set("alerts", {"categories": cats})
        on = [k for k, v in cats.items() if v]
        detail = f"Phone alerts for {', '.join(on)}" if on else "Phone alerts all off"
        row = self.store.add_action(actor, device, "alerts", "server", "ok", detail)
        self.c.broadcast("action", row)
        return self.view()

    def view(self) -> dict:
        server, topic = self.destination or ("", "")
        return {
            "configured": self.configured,
            "server": urlsplit(server).netloc if server else "",
            # Enough of the topic to recognise it, not enough to subscribe to it.
            "topic_hint": f"{topic[:3]}…{topic[-2:]}" if len(topic) > 8 else ("…" if topic else ""),
            "categories": self.categories(),
            "descriptions": CATEGORIES,
            "last_sent": self.last_sent,
            "last_error": self.last_error,
        }

    # ---------------------------------------------------------------- events
    def notify(self, ev: dict) -> bool:
        """Called with every recorded event. Returns True if an alert was queued."""
        if not self.configured:
            return False
        rule = classify(ev)
        if not rule or not self.categories().get(rule["category"]):
            return False
        if ev.get("kind") == "app" and ev.get("container") and self.c.actions.expected_stop(ev["container"]):
            return False  # I stopped it from the console; no need to tell me
        now = time.time()
        if now - self.last_fired.get(rule["key"], 0.0) < ALERT_COOLDOWN:
            return False
        while self.sent_times and now - self.sent_times[0] > 3600:
            self.sent_times.popleft()
        if len(self.sent_times) >= HOURLY_LIMIT:
            return False
        self.last_fired[rule["key"]] = now
        self.sent_times.append(now)
        message = {
            "title": ev.get("title") or "stx-server",
            "message": ev.get("detail") or ev.get("title") or "",
            "priority": rule["priority"],
            "tags": rule["tags"],
        }
        self._spawn(self.send(message))
        return True

    async def send_test(self) -> dict:
        if not self.configured:
            raise RuntimeError("Alerts aren't set up. Set STX_NTFY_URL on the server first.")
        host = self.c.host.identity.get("hostname") or "stx-server"
        ok = await self.send({"title": "Test alert from stx-monitor",
                              "message": f"Alerts from {host} reach this phone.",
                              "priority": 3, "tags": ["wave"]})
        if not ok:
            raise RuntimeError((self.last_error or {}).get("detail") or "The alert could not be sent")
        return self.view()

    async def send(self, message: dict[str, Any]) -> bool:
        server, topic = self.destination  # type: ignore[misc]
        body = {"topic": topic, **message}
        click = self._console_url()
        if click:
            body["click"] = click
        try:
            r = await self.http.post(server, json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            detail = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else str(exc) or type(exc).__name__
            self.last_error = {"time": time.time(), "title": message["title"], "detail": detail[:200]}
            log.warning("could not send alert %r: %s", message["title"], detail)
            return False
        self.last_sent = {"time": time.time(), "title": message["title"]}
        return True

    def _console_url(self) -> str:
        if self.c.settings.public_url:
            return self.c.settings.public_url
        for d in (self.c.tailnet or {}).get("devices", []):
            if d.get("self") and d.get("dns_name"):
                return f"https://{d['dns_name']}"
        return ""

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (a plain unit test): nothing to send with
            coro.close()
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
