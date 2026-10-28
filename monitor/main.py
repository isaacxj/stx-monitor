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
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .collector import Collector
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("monitor")

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
async def container_logs(request: Request, key: str, tail: int = Query(200, ge=1, le=2000)) -> dict:
    col, c = _resolve(request, key)
    try:
        lines = await col.docker.logs(c["id"], tail=tail)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"docker: {exc}") from exc
    return {"id": c["id"], "name": c["name"], "lines": lines}


if settings.web_dir.is_dir():
    app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
