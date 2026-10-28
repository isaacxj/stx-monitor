"""HTTP API and static web UI.

Everything here is read-only: there is no endpoint that changes the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .collector import Collector
from .config import settings
from .logs import to_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("monitor")
# httpx logs every request at INFO, which is one line per container every few seconds.
logging.getLogger("httpx").setLevel(logging.WARNING)

STARTED = time.time()
KEEPALIVE = 15.0
CONTAINER_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    collector = Collector(settings)
    app.state.collector = collector
    await collector.start()
    log.info("stx-monitor %s (%s) on %s:%s, host root %s, docker %s",
             __version__, settings.git_sha, settings.bind, settings.port, settings.host_root, settings.docker_host)
    try:
        yield
    finally:
        await collector.stop()


app = FastAPI(title="stx-monitor", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)


def collector(request: Request) -> Collector:
    return request.app.state.collector


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": __version__,
        "sha": settings.git_sha,
        "uptime_s": round(time.time() - STARTED),
    }


@app.get("/api/overview")
async def overview(request: Request) -> dict:
    return collector(request).overview()


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-Sent Events: one long HTTP response the server keeps writing to.

    Browsers reconnect on their own if it drops (EventSource).
    """
    col = collector(request)
    queue = col.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), KEEPALIVE)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        finally:
            col.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _resolve(request: Request, key: str) -> tuple[Collector, dict]:
    col = collector(request)
    if not col.docker:
        raise HTTPException(503, "Docker is not configured")
    if not CONTAINER_KEY.match(key):
        raise HTTPException(400, "bad container id")
    c = col.find_container(key)
    if not c:
        raise HTTPException(404, "no such container")
    return col, c


@app.get("/api/containers/{key}")
async def container_detail(request: Request, key: str) -> dict:
    col, c = _resolve(request, key)
    try:
        detail = await col.docker.inspect(c["id"])
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"docker: {exc}") from exc
    app_entry = next((a for g in col.apps_map()["groups"] for a in g["apps"]
                      if a.get("container") and a["container"]["id"] == c["id"]), None)
    events = [e for e in col.events if e.get("container") == c["id"]][-20:]
    return {**c, "stats": col.stats.get(c["id"]), "inspect": detail, "app": app_entry, "events": events}


@app.get("/api/containers/{key}/logs")
async def container_logs(request: Request, key: str, tail: int = Query(200, ge=1)) -> dict:
    col, c = _resolve(request, key)
    try:
        lines = await col.docker.logs(c["id"], tail=min(tail, 2000))
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"docker: {exc}") from exc
    return {"id": c["id"], "name": c["name"], "lines": lines}


@app.get("/api/reports")
async def reports(request: Request) -> dict:
    return collector(request).reports.summary()


@app.get("/api/reports/{report_id}")
async def report(request: Request, report_id: int) -> dict:
    col = collector(request)
    found = col.store.get_report(report_id)
    if not found:
        raise HTTPException(404, "no such report")
    if not found["seen"]:
        col.store.mark_report_seen(report_id)
        col.broadcast("reports", {"unseen": col.store.unseen_reports()})
    return found


LOG_SOURCE = re.compile(r"^(all|server|docker|[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
LEVELS = ("all", "warn", "error")


def _log_filter(source: str, level: str, q: str) -> tuple[str, str, str]:
    if not LOG_SOURCE.match(source) or level not in LEVELS:
        raise HTTPException(400, "bad filter")
    return source, level, q[:200]


@app.get("/api/logs")
async def logs(request: Request, source: str = "all", level: str = "all", q: str = "",
               limit: int = Query(500, ge=1, le=5000)) -> dict:
    col = collector(request)
    result = col.logs.query(*_log_filter(source, level, q), limit=limit)
    return {**result, **col.logs.sources()}


@app.get("/api/logs/download")
async def logs_download(request: Request, source: str = "all", level: str = "all", q: str = "") -> PlainTextResponse:
    col = collector(request)
    result = col.logs.query(*_log_filter(source, level, q), limit=5000)
    name = f"stx-logs-{source}-{time.strftime('%Y%m%d-%H%M')}.txt"
    return PlainTextResponse(to_text(result["lines"]), headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/logs/stream")
async def logs_stream(request: Request, source: str = "all", level: str = "all", q: str = "") -> StreamingResponse:
    """New log lines as they arrive, sent in small batches."""
    col = collector(request)
    queue = col.logs.subscribe(*_log_filter(source, level, q))

    async def gen():
        try:
            yield "retry: 3000\n\n"
            idle = 0.0
            while True:
                batch = []
                try:
                    batch.append(await asyncio.wait_for(queue.get(), 1.0))
                    idle = 0.0
                except asyncio.TimeoutError:
                    idle += 1.0
                while not queue.empty() and len(batch) < 500:
                    batch.append(queue.get_nowait())
                if batch:
                    yield f"event: lines\ndata: {json.dumps(batch, separators=(',', ':'))}\n\n"
                elif idle >= KEEPALIVE:
                    idle = 0.0
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
        finally:
            col.logs.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


if settings.web_dir.is_dir():
    app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
