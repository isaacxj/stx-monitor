"""HTTP API and static web UI.

Everything under /api needs a signed-in session, except the health check and
the sign-in endpoints themselves. Every request that changes something must
come from our own page (Origin check) and carry the session's CSRF token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .actions import ActionError
from .auth import COOKIE, REMEMBER_SECONDS, Auth, AuthError, Client, device_name
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
    app.state.auth = Auth(collector.store, disabled=settings.auth_disabled)
    if settings.auth_disabled:
        log.warning("STX_AUTH=off: sign-in is disabled. Never run like this outside development.")
    elif not app.state.auth.has_password():
        log.warning("No password set yet. Run: docker exec -it stx-monitor python -m monitor set-password")
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


PUBLIC_API = {"/api/health", "/api/session", "/api/auth/login", "/api/auth/passkey/begin", "/api/auth/passkey/finish"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def allowed_origins(request: Request) -> set[str]:
    """The addresses this console is legitimately served from."""
    out = set()
    if settings.public_url:
        out.add(settings.public_url.lower())
    tailnet = getattr(request.app.state, "collector", None) and request.app.state.collector.tailnet
    for d in (tailnet or {}).get("devices", []):
        if d.get("self") and d.get("dns_name"):
            out.add(f"https://{d['dns_name']}".lower())
    for header in ("host", "x-forwarded-host"):
        host = request.headers.get(header)
        if host:
            out.add(f"https://{host}".lower())
            if settings.insecure_cookie:
                out.add(f"http://{host}".lower())
    return out


def checked_origin(request: Request) -> str | None:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin or origin.lower() not in allowed_origins(request):
        return None
    return origin


def client_of(request: Request) -> Client:
    ip = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and ip in ("127.0.0.1", "::1"):
        ip = forwarded.split(",")[0].strip()  # tailscale serve passes the real address along
    device = device_name(request.headers.get("user-agent", ""))
    login = request.headers.get("tailscale-user-login")
    if login:
        device = f"{device} ({login})"
    return Client(ip=ip[:64], device=device[:120])


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    auth: Auth = request.app.state.auth
    unsafe = request.method not in SAFE_METHODS
    if unsafe and not checked_origin(request):
        return JSONResponse({"detail": "This request did not come from the console's own page."}, status_code=403)
    if auth.disabled:
        request.state.user, request.state.device = "dev", client_of(request).device
        return await call_next(request)
    session = auth.session(request.cookies.get(COOKIE))
    if session:
        request.state.session = session
        request.state.user = auth.username() or "you"
        request.state.device = session["device"]
    if path not in PUBLIC_API:
        if not session:
            return JSONResponse({"detail": "Sign in first."}, status_code=401)
        if unsafe and not auth.csrf_ok(session, request.headers.get("x-csrf-token")):
            return JSONResponse({"detail": "Missing or wrong CSRF token. Reload the page."}, status_code=403)
    return await call_next(request)


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


# ------------------------------------------------------------------- sign-in
class LoginBody(BaseModel):
    username: str
    password: str
    remember: bool = False


class PasskeyFinishBody(BaseModel):
    flow: str
    credential: dict
    remember: bool = False
    name: str = ""


def _set_cookie(response: Response, token: str, remember: bool) -> None:
    response.set_cookie(
        COOKIE, token, max_age=REMEMBER_SECONDS if remember else None, httponly=True,
        secure=not settings.insecure_cookie, samesite="strict", path="/",
    )


def _signed_in(request: Request, method: str, remember: bool, detail: str = "") -> JSONResponse:
    auth: Auth = request.app.state.auth
    client = client_of(request)
    token, session = auth.create_session(remember, method, client)
    user = auth.username() or "you"
    via = "a passkey" if method == "passkey" else "the password"
    collector(request).add_event({"kind": "security", "level": "info", "title": f"{user} signed in from {client.device}",
                                  "detail": f"With {via}{f' ({detail})' if detail else ''}, from {client.ip}."})
    response = JSONResponse({"ok": True, "user": user, "csrf": session["csrf"]})
    _set_cookie(response, token, remember)
    return response


def _failed(request: Request, exc: AuthError) -> JSONResponse:
    client = client_of(request)
    level = "error" if exc.status == 429 else "warn"
    title = "Sign-in paused for 15 minutes" if exc.status == 429 else f"Failed sign-in from {client.device}"
    collector(request).add_event({"kind": "security", "level": level, "title": title, "detail": f"{exc} From {client.ip}."})
    return JSONResponse({"detail": str(exc), "locked_for": request.app.state.auth.locked_for()}, status_code=exc.status)


@app.get("/api/session")
async def session_info(request: Request) -> dict:
    auth: Auth = request.app.state.auth
    session = getattr(request.state, "session", None)
    base = {
        "auth_disabled": auth.disabled,
        "has_password": auth.has_password(),
        "passkeys": len(auth.store.list_passkeys()),
        "locked_for": auth.locked_for(),
        "hostname": collector(request).host.identity.get("hostname"),
    }
    if auth.disabled:
        return {**base, "authenticated": True, "user": "dev", "csrf": "dev", "device": client_of(request).device}
    if not session:
        return {**base, "authenticated": False}
    return {**base, "authenticated": True, "user": auth.username(), "csrf": session["csrf"], "device": session["device"],
            "method": session["method"], "remember": bool(session["remember"]), "expires": session["expires"]}


@app.post("/api/auth/login")
async def login(request: Request, body: LoginBody):
    auth: Auth = request.app.state.auth
    if not auth.has_password():
        return JSONResponse({"detail": "No password is set yet. Set one on the server with: "
                                       "docker exec -it stx-monitor python -m monitor set-password"}, status_code=409)
    try:
        auth.password_sign_in(body.username[:64], body.password[:256], client_of(request))
    except AuthError as exc:
        return _failed(request, exc)
    return _signed_in(request, "password", body.remember)


@app.post("/api/auth/logout")
async def logout(request: Request):
    request.app.state.auth.end_session(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/", secure=not settings.insecure_cookie, httponly=True, samesite="strict")
    return response


def _rp(request: Request) -> tuple[str, str]:
    origin = checked_origin(request)
    if not origin:
        raise HTTPException(403, "Unknown origin")
    host = urlsplit(origin).hostname or ""
    return host, origin


@app.post("/api/auth/passkey/begin")
async def passkey_begin(request: Request):
    rp_id, _ = _rp(request)
    try:
        return request.app.state.auth.begin_authentication(rp_id)
    except AuthError as exc:
        return _failed(request, exc)


@app.post("/api/auth/passkey/finish")
async def passkey_finish(request: Request, body: PasskeyFinishBody):
    _, origin = _rp(request)
    try:
        name = request.app.state.auth.finish_authentication(body.flow, body.credential, origin, client_of(request))
    except AuthError as exc:
        return _failed(request, exc)
    return _signed_in(request, "passkey", body.remember, name)


@app.post("/api/auth/passkeys/register/begin")
async def passkey_register_begin(request: Request) -> dict:
    rp_id, _ = _rp(request)
    return request.app.state.auth.begin_registration(rp_id)


@app.post("/api/auth/passkeys/register/finish")
async def passkey_register_finish(request: Request, body: PasskeyFinishBody) -> dict:
    _, origin = _rp(request)
    try:
        added = request.app.state.auth.finish_registration(body.flow, body.credential, origin,
                                                           body.name or client_of(request).device)
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    collector(request).add_event({"kind": "security", "level": "info", "title": f"Added a passkey: {added['name']}",
                                  "detail": f"From {client_of(request).device}."})
    return added


@app.get("/api/auth/account")
async def account(request: Request) -> dict:
    store = request.app.state.auth.store
    current = getattr(request.state, "session", None)
    sessions = [{"current": bool(current and s["id"] == current["id"]), "device": s["device"], "ip": s["ip"],
                 "created": s["created"], "last_seen": s["last_seen"], "method": s["method"],
                 "remember": bool(s["remember"])} for s in store.list_sessions()]
    passkeys = [{k: p[k] for k in ("id", "name", "created", "last_used")} for p in store.list_passkeys()]
    return {"user": request.app.state.auth.username(), "passkeys": passkeys, "sessions": sessions,
            "sign_ins": store.recent_sign_ins(15)}


@app.post("/api/auth/passkeys/{cred_id}/delete")
async def passkey_delete(request: Request, cred_id: str) -> dict:
    if not request.app.state.auth.store.delete_passkey(cred_id[:256]):
        raise HTTPException(404, "No such passkey")
    collector(request).add_event({"kind": "security", "level": "info", "title": "Removed a passkey", "detail": ""})
    return {"ok": True}


@app.post("/api/auth/sessions/sign-out-others")
async def sign_out_others(request: Request) -> dict:
    current = getattr(request.state, "session", None)
    n = request.app.state.auth.store.delete_other_sessions(current["id"] if current else "")
    collector(request).add_event({"kind": "security", "level": "info", "title": f"Signed out {n} other device{'s' * (n != 1)}", "detail": ""})
    return {"signed_out": n}


# -------------------------------------------------------------------- actions
def actor(request: Request) -> tuple[str, str]:
    """Who is asking, for the history: the signed-in user and their device."""
    return getattr(request.state, "user", "you"), getattr(request.state, "device", "")


class ActionBody(BaseModel):
    action: str
    target: str = ""
    confirm: str | None = None


class AutopilotBody(BaseModel):
    enabled: bool
    threshold: int = 20


@app.get("/api/control")
async def control(request: Request) -> dict:
    return await collector(request).actions.control_view()


@app.post("/api/actions", status_code=202)
async def run_action(request: Request, body: ActionBody) -> dict:
    col = collector(request)
    user, device = actor(request)
    try:
        return col.actions.request(body.action, body.target, user, device, body.confirm)
    except ActionError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/autopilot")
async def set_autopilot(request: Request, body: AutopilotBody) -> dict:
    user, device = actor(request)
    return collector(request).actions.set_autopilot(body.enabled, body.threshold, user, device)


@app.post("/api/updates/check", status_code=202)
async def check_updates(request: Request) -> dict:
    col = collector(request)
    task = asyncio.create_task(col.actions.check_updates())
    col.actions.tasks.add(task)
    task.add_done_callback(col.actions.tasks.discard)
    return {"started": True}


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
