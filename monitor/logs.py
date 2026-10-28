"""One place for every log line: the host journal plus each running container.

The hub keeps the most recent lines in memory (a ring buffer), follows every
source live, and hands new lines to whichever browsers have the Logs page open.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import logging
import re
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

MAX_LINES = 5000
DOCKER_SOURCES = {"dockerd", "containerd", "docker"}
DOCKER_UNITS = {"docker.service", "containerd.service", "docker.socket"}

_ERROR = re.compile(r"\b(error|err|fatal|panic|critical|crit|exception|traceback|failed|failure)\b|\[(ERR|ERROR|FTL|FATAL|CRIT)\]", re.I)
_WARN = re.compile(r"\b(warn|warning|wrn)\b|\[(WRN|WARN)\]", re.I)
_DEBUG = re.compile(r"\[(DBG|DEBUG|VRB|TRACE)\]|\b(debug|trace)\b\s*[:|\]]", re.I)


def guess_level(text: str, stream: str = "stdout") -> str:
    """Containers don't label their lines consistently, so read the text."""
    head = text[:160]
    if _ERROR.search(head):
        return "error"
    if _WARN.search(head):
        return "warn"
    if _DEBUG.search(head):
        return "debug"
    return "info"


def parse_ts(ts: str | None) -> float:
    if not ts:
        return time.time()
    # Docker writes nanoseconds; Python handles up to microseconds.
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?", ts)
    if not m:
        return time.time()
    frac = (m.group(2) or ".0")[:7]
    zone = m.group(3) or "Z"
    try:
        return dt.datetime.fromisoformat(m.group(1) + frac + ("+00:00" if zone == "Z" else zone)).timestamp()
    except ValueError:
        return time.time()


def matches(line: dict, source: str = "all", level: str = "all", query: str = "") -> bool:
    if source == "server":
        if line["origin"] != "host":
            return False
    elif source == "docker":
        if line["origin"] != "host" or not (line["source"] in DOCKER_SOURCES or line.get("unit") in DOCKER_UNITS):
            return False
    elif source not in ("all", ""):
        if line["origin"] != "container" or line["source"] != source:
            return False
    if level == "error" and line["level"] != "error":
        return False
    if level == "warn" and line["level"] not in ("warn", "error"):
        return False
    if query and query.lower() not in line["text"].lower() and query.lower() not in line["source"].lower():
        return False
    return True


class LogHub:
    def __init__(self, maxlen: int = MAX_LINES):
        self.lines: deque[dict] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self.subscribers: dict[asyncio.Queue, dict] = {}
        self.followers: dict[str, asyncio.Task] = {}
        self.last_ts: dict[str, float] = {}
        self.journal_ok: bool | None = None
        self.journal_error: str | None = None

    # ------------------------------------------------------------ add/query
    def add(self, line: dict) -> dict:
        line = {**line, "id": next(self._ids)}
        line.pop("boot", None)
        line.pop("identifier", None)
        self.lines.append(line)
        for q, flt in list(self.subscribers.items()):
            if not matches(line, **flt):
                continue
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(line)
        return line

    def query(self, source: str = "all", level: str = "all", query: str = "", limit: int = 500) -> dict:
        found = [x for x in self.lines if matches(x, source, level, query)]
        found.sort(key=lambda x: (x["ts"], x["id"]))
        return {"lines": found[-limit:], "matched": len(found), "total": len(self.lines)}

    def sources(self) -> dict:
        containers = sorted({x["source"] for x in self.lines if x["origin"] == "container"} | set(self.followers_names()))
        return {"containers": containers, "journal": self.journal_ok, "journal_error": self.journal_error}

    def followers_names(self) -> list[str]:
        return [t.get_name().removeprefix("logs:") for t in self.followers.values()]

    def subscribe(self, source: str, level: str, query: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers[q] = {"source": source, "level": level, "query": query}
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.pop(q, None)

    # ---------------------------------------------------------------- journal
    async def follow_journal(self, journal) -> None:
        self.journal_ok = journal.available
        self.journal_error = journal.error
        if not journal.available:
            return

        def handle(entry: dict) -> None:
            if entry["text"]:
                self.add(entry)

        await journal.follow(handle)

    # ------------------------------------------------------------- containers
    def sync(self, docker, containers: list[dict], tty_of) -> None:
        """Follow every running container; forget the ones that stopped."""
        running = {c["id"]: c for c in containers if c["state"] == "running"}
        for cid in list(self.followers):
            task = self.followers[cid]
            if task.done() or cid not in running:
                if not task.done():
                    task.cancel()
                del self.followers[cid]
        for cid, c in running.items():
            if cid not in self.followers:
                self.followers[cid] = asyncio.create_task(
                    self._follow_container(docker, cid, c["name"], tty_of), name=f"logs:{c['name']}")

    async def _follow_container(self, docker, cid: str, name: str, tty_of) -> None:
        try:
            tty = await tty_of(cid)
        except Exception:  # noqa: BLE001
            tty = False
        since = self.last_ts.get(cid)
        try:
            async for item in docker.stream_logs(cid, tty, since=since + 1e-6 if since else None, tail=100):
                ts = parse_ts(item["ts"])
                self.last_ts[cid] = ts
                text = item["text"]
                if not text.strip():
                    continue
                self.add({"ts": ts, "origin": "container", "source": name, "container": cid,
                          "level": guess_level(text, item["stream"]), "stream": item["stream"], "text": text})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # The proxy closes idle streams after 10 minutes; the next sync reconnects.
            log.debug("log stream for %s ended: %s", name, exc)

    async def stop(self) -> None:
        for t in self.followers.values():
            t.cancel()
        for t in self.followers.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self.followers.clear()


def to_text(lines: list[dict[str, Any]]) -> str:
    out = []
    for x in lines:
        stamp = dt.datetime.fromtimestamp(x["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"{stamp}  {x['source']:<16} {x['level']:<5}  {x['text']}")
    return "\n".join(out) + "\n"
