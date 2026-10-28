"""A small, read-only Docker Engine API client.

Docker is controlled through an HTTP API. Normally that API listens on a Unix
socket (/var/run/docker.sock), and anything that can open that socket is
effectively root on the host. So stx-monitor never touches the socket itself:
it talks to tecnativa/docker-socket-proxy, which only forwards the read-only
GET endpoints we allow (containers, info, events) and refuses everything else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

# No /v1.xx prefix: an unversioned path gets the daemon's own API version, which
# keeps working when Docker raises its minimum supported version.
API = ""
INSPECT_TTL = 60.0
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def parse_docker_host(docker_host: str) -> tuple[str, str | None]:
    """Return (base_url, unix_socket_path) for a DOCKER_HOST value."""
    if docker_host.startswith("unix://"):
        return "http://docker", docker_host[len("unix://"):]
    if docker_host.startswith("tcp://"):
        return "http://" + docker_host[len("tcp://"):].rstrip("/"), None
    if docker_host.startswith(("http://", "https://")):
        return docker_host.rstrip("/"), None
    raise ValueError(f"unsupported DOCKER_HOST: {docker_host!r}")


# ------------------------------------------------------------------------ logs

class StreamDemux:
    """Split Docker's log stream into (stream, line) pairs as bytes arrive.

    Without a TTY, Docker multiplexes stdout and stderr onto one connection.
    Every chunk starts with an 8-byte header: one byte for the stream
    (1 = stdout, 2 = stderr), three zero bytes, then the payload length as a
    big-endian 32-bit number. With a TTY there is one stream and no headers.
    A header or a line can be split across network reads, so this keeps
    whatever is incomplete until the next feed().
    """

    def __init__(self, tty: bool = False):
        self.tty = tty
        self.raw = False
        self.buf = b""
        self.partial: dict[str, str] = {}

    def _lines(self, stream: str, text: str) -> list[tuple[str, str]]:
        buf = self.partial.get(stream, "") + text
        *complete, rest = buf.split("\n")
        self.partial[stream] = rest
        return [(stream, line) for line in complete]

    def feed(self, data: bytes) -> list[tuple[str, str]]:
        if self.tty or self.raw:
            return self._lines("stdout", data.decode("utf-8", errors="replace"))
        self.buf += data
        out: list[tuple[str, str]] = []
        while len(self.buf) >= 8:
            kind = self.buf[0]
            if kind not in (0, 1, 2) or self.buf[1:4] != b"\x00\x00\x00":
                # Not a multiplexed stream after all; treat everything as plain text.
                self.raw = True
                rest, self.buf = self.buf, b""
                out.extend(self._lines("stdout", rest.decode("utf-8", errors="replace")))
                return out
            size = struct.unpack(">I", self.buf[4:8])[0]
            if len(self.buf) < 8 + size:
                break
            chunk = self.buf[8:8 + size].decode("utf-8", errors="replace")
            self.buf = self.buf[8 + size:]
            out.extend(self._lines("stderr" if kind == 2 else "stdout", chunk))
        return out

    def flush(self) -> list[tuple[str, str]]:
        out = [(stream, rest) for stream, rest in self.partial.items() if rest]
        self.partial = {}
        return out


def demux(data: bytes, tty: bool = False) -> list[tuple[str, str]]:
    """Split a complete log response into (stream, line) pairs."""
    d = StreamDemux(tty)
    return d.feed(data) + d.flush()


def parse_log_lines(pairs: list[tuple[str, str]]) -> list[dict]:
    """Split off the RFC 3339 timestamp Docker adds with timestamps=1."""
    lines = []
    for stream, raw in pairs:
        raw = ANSI_RE.sub("", raw.rstrip("\r"))
        ts, sep, text = raw.partition(" ")
        if not sep or not re.match(r"^\d{4}-\d{2}-\d{2}T", ts):
            ts, text = None, raw
        lines.append({"ts": ts, "stream": stream, "text": text})
    return lines


# ----------------------------------------------------------------------- stats

def memory_used(mem: dict) -> int | None:
    """Memory a container really uses: total usage minus reclaimable file cache.

    cgroups are the kernel feature that meters and limits a group of processes;
    every container is one. cgroup v1 reports the cache as total_inactive_file,
    cgroup v2 as inactive_file. This matches what `docker stats` shows.
    """
    usage = mem.get("usage")
    if usage is None:
        return None
    stats = mem.get("stats") or {}
    if "total_inactive_file" in stats:  # cgroup v1
        cache = stats["total_inactive_file"]
    elif "inactive_file" in stats:  # cgroup v2
        cache = stats["inactive_file"]
    else:
        cache = 0
    return max(0, usage - cache) if cache < usage else usage


def _blkio_totals(blkio: dict) -> tuple[int, int] | None:
    entries = (blkio or {}).get("io_service_bytes_recursive")
    if not entries:
        return None
    read = sum(e.get("value", 0) for e in entries if str(e.get("op", "")).lower() == "read")
    write = sum(e.get("value", 0) for e in entries if str(e.get("op", "")).lower() == "write")
    return read, write


def _net_totals(networks: dict | None) -> tuple[int, int] | None:
    if not networks:
        return None
    return (sum(n.get("rx_bytes", 0) for n in networks.values()),
            sum(n.get("tx_bytes", 0) for n in networks.values()))


class StatsTracker:
    """Turns one-shot stats snapshots into rates by remembering the previous poll.

    We ask Docker for a single snapshot (one-shot=true) because the default
    waits a full second to fill in "precpu_stats". So we compute deltas between
    our own polls instead.
    """

    def __init__(self) -> None:
        self.prev: dict[str, dict] = {}

    def forget(self, keep: set[str]) -> None:
        for cid in set(self.prev) - keep:
            del self.prev[cid]

    def update(self, cid: str, raw: dict, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        cpu = raw.get("cpu_stats") or {}
        total = (cpu.get("cpu_usage") or {}).get("total_usage")
        system = cpu.get("system_cpu_usage")
        ncpu = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
        mem = raw.get("memory_stats") or {}
        net = _net_totals(raw.get("networks"))
        blk = _blkio_totals(raw.get("blkio_stats"))
        snap = {"t": now, "total": total, "system": system, "net": net, "blk": blk}
        prev = self.prev.get(cid)
        self.prev[cid] = snap

        cpu_pct = None
        net_rx = net_tx = blk_r = blk_w = None
        if prev and total is not None and prev["total"] is not None:
            d_total = total - prev["total"]
            dt = now - prev["t"]
            if system is not None and prev["system"] is not None and system > prev["system"]:
                # system_cpu_usage adds up every core, so this is already "% of the whole machine".
                cpu_pct = d_total / (system - prev["system"]) * 100
            elif dt > 0:
                cpu_pct = d_total / (dt * 1e9 * ncpu) * 100
            if cpu_pct is not None:
                cpu_pct = round(max(0.0, cpu_pct), 2)
            if dt > 0:
                if net and prev["net"]:
                    net_rx = max(0, net[0] - prev["net"][0]) / dt
                    net_tx = max(0, net[1] - prev["net"][1]) / dt
                if blk and prev["blk"]:
                    blk_r = max(0, blk[0] - prev["blk"][0]) / dt
                    blk_w = max(0, blk[1] - prev["blk"][1]) / dt

        used = memory_used(mem)
        limit = mem.get("limit")
        return {
            "cpu": cpu_pct,
            "mem": used,
            "mem_limit": limit,
            "mem_percent": round(used / limit * 100, 1) if used is not None and limit else None,
            "net_rx": round(net_rx) if net_rx is not None else None,
            "net_tx": round(net_tx) if net_tx is not None else None,
            "blk_read": round(blk_r) if blk_r is not None else None,
            "blk_write": round(blk_w) if blk_w is not None else None,
            "pids": (raw.get("pids_stats") or {}).get("current"),
        }


# ------------------------------------------------------------------- summaries

_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)")


def summarize_container(c: dict) -> dict:
    """Trim a /containers/json entry down to what the UI needs."""
    labels = c.get("Labels") or {}
    status = c.get("Status") or ""
    m = _HEALTH_RE.search(status)
    health = m.group(1).replace("health: ", "") if m else None
    ports = sorted({p["PublicPort"] for p in c.get("Ports") or [] if p.get("PublicPort")})
    names = c.get("Names") or []
    return {
        "id": c.get("Id", "")[:12],
        "full_id": c.get("Id"),
        "name": names[0].lstrip("/") if names else c.get("Id", "")[:12],
        "image": c.get("Image"),
        "state": c.get("State"),
        "status": status,
        "health": health,
        "created": c.get("Created"),
        "ports": ports,
        "labels": {k: v for k, v in labels.items() if k.startswith(("stx.", "com.docker.compose."))},
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
    }


def summarize_inspect(i: dict) -> dict:
    state = i.get("State") or {}
    cfg = i.get("Config") or {}
    hc = i.get("HostConfig") or {}
    return {
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "exit_code": state.get("ExitCode"),
        "oom_killed": state.get("OOMKilled"),
        "error": state.get("Error") or None,
        "health": (state.get("Health") or {}).get("Status"),
        "restart_count": i.get("RestartCount"),
        "restart_policy": (hc.get("RestartPolicy") or {}).get("Name"),
        "memory_limit": hc.get("Memory") or None,
        "network_mode": hc.get("NetworkMode"),
        "image": cfg.get("Image"),
        "tty": bool(cfg.get("Tty")),
        "cmd": cfg.get("Cmd"),
        "mounts": [
            {"source": m.get("Source") or m.get("Name"), "destination": m.get("Destination"),
             "type": m.get("Type"), "rw": m.get("RW")}
            for m in i.get("Mounts") or []
        ],
    }


def summarize_event(e: dict) -> dict | None:
    if e.get("Type") != "container":
        return None
    actor = e.get("Actor") or {}
    attrs = actor.get("Attributes") or {}
    action = e.get("Action") or e.get("status") or ""
    return {
        "time": e.get("time") or int(time.time()),
        "action": action,
        "id": (actor.get("ID") or e.get("id") or "")[:12],
        "name": attrs.get("name"),
        "image": attrs.get("image"),
        "exit_code": attrs.get("exitCode"),
    }


# ---------------------------------------------------------------------- client

class DockerClient:
    def __init__(self, docker_host: str, transport: httpx.AsyncBaseTransport | None = None):
        base_url, uds = parse_docker_host(docker_host)
        if transport is None:
            transport = httpx.AsyncHTTPTransport(uds=uds) if uds else httpx.AsyncHTTPTransport()
        self.http = httpx.AsyncClient(base_url=base_url + API, transport=transport, timeout=10.0)
        self.tracker = StatsTracker()
        self._inspect: dict[str, tuple[float, dict]] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self.http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def info(self) -> dict:
        i = await self._get("/info")
        return {
            "version": i.get("ServerVersion"),
            "containers": i.get("Containers"),
            "running": i.get("ContainersRunning"),
            "images": i.get("Images"),
            "storage_driver": i.get("Driver"),
            "cgroup_version": i.get("CgroupVersion"),
        }

    async def containers(self) -> list[dict]:
        return [summarize_container(c) for c in await self._get("/containers/json", all=1)]

    async def inspect(self, cid: str, fresh: bool = False) -> dict:
        hit = self._inspect.get(cid)
        if hit and not fresh and time.monotonic() - hit[0] < INSPECT_TTL:
            return hit[1]
        data = summarize_inspect(await self._get(f"/containers/{cid}/json"))
        self._inspect[cid] = (time.monotonic(), data)
        return data

    def invalidate(self, cid: str) -> None:
        for key in [k for k in self._inspect if k.startswith(cid) or cid.startswith(k)]:
            self._inspect.pop(key, None)

    async def image_digests(self, ref: str) -> list[str]:
        """Registry digests of a local image ("repo@sha256:..."). Empty for locally built images."""
        data = await self._get(f"/images/{ref}/json")
        return data.get("RepoDigests") or []

    async def remote_digest(self, ref: str) -> str | None:
        """The digest the registry currently serves for this tag (Docker asks the registry for us)."""
        data = await self._get(f"/distribution/{ref}/json")
        return (data.get("Descriptor") or {}).get("digest")

    async def unused_image_bytes(self) -> int | None:
        """Roughly how much an image cleanup would free: images no container uses."""
        data = await self._get("/system/df")
        total = 0
        for img in data.get("Images") or []:
            if img.get("Containers", 0) == 0:
                shared = max(0, img.get("SharedSize") or 0)
                total += max(0, (img.get("Size") or 0) - shared)
        return total

    async def stats(self, cid: str) -> dict:
        raw = await self._get(f"/containers/{cid}/stats", stream="false", **{"one-shot": "true"})
        return self.tracker.update(cid, raw)

    async def all_stats(self, ids: list[str]) -> dict[str, dict]:
        results = await asyncio.gather(*(self.stats(i) for i in ids), return_exceptions=True)
        self.tracker.forget(set(ids))
        return {i: r for i, r in zip(ids, results) if isinstance(r, dict)}

    async def logs(self, cid: str, tail: int = 200) -> list[dict]:
        tail = max(1, min(int(tail), 2000))
        tty = (await self.inspect(cid)).get("tty", False)
        r = await self.http.get(
            f"/containers/{cid}/logs",
            params={"stdout": 1, "stderr": 1, "timestamps": 1, "tail": tail},
        )
        r.raise_for_status()
        return parse_log_lines(demux(r.content, tty=tty))

    async def stream_logs(self, cid: str, tty: bool, since: float | None = None, tail: int = 100) -> AsyncIterator[dict]:
        """Follow a container's output, yielding parsed lines until the stream ends."""
        params: dict[str, Any] = {"stdout": 1, "stderr": 1, "timestamps": 1, "follow": 1}
        if since:
            params["since"] = f"{since:.6f}"
        else:
            params["tail"] = tail
        async with self.http.stream("GET", f"/containers/{cid}/logs", params=params, timeout=None) as r:
            r.raise_for_status()
            d = StreamDemux(tty)
            async for chunk in r.aiter_bytes():
                for line in parse_log_lines(d.feed(chunk)):
                    yield line
            for line in parse_log_lines(d.flush()):
                yield line

    async def events(self) -> AsyncIterator[dict]:
        """Yield container events as they happen. Ends when the connection drops."""
        filters = json.dumps({"type": ["container"]})
        async with self.http.stream("GET", "/events", params={"filters": filters}, timeout=None) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    ev = summarize_event(json.loads(line))
                except ValueError:
                    continue
                if ev:
                    yield ev

    async def watch_events(self, handle: Callable[[dict], Awaitable[None]], on_state: Callable[[bool], None] | None = None) -> None:
        """Follow the event stream forever, reconnecting with exponential backoff."""
        delay = 1.0
        while True:
            try:
                if on_state:
                    on_state(True)
                async for ev in self.events():
                    delay = 1.0
                    if ev["id"]:
                        self.invalidate(ev["id"])
                    await handle(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("docker events stream dropped: %s; retrying in %.0fs", exc, delay)
            if on_state:
                on_state(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
