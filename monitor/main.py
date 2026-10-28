"""HTTP API and static web UI."""

import time

from fastapi import FastAPI

from . import __version__
from .config import settings

STARTED = time.time()

app = FastAPI(title="stx-monitor", version=__version__, docs_url=None, redoc_url=None)


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": __version__,
        "sha": settings.git_sha,
        "uptime_s": round(time.time() - STARTED),
    }
