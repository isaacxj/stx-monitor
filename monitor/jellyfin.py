"""Jellyfin now playing: who is watching, and whether the video is being converted.

Jellyfin can send a file three ways:

- Direct play: the file goes out untouched. Costs almost nothing.
- Direct stream: the video is untouched, but the audio or the container is
  converted on the way. Cheap.
- Transcode: the video itself is re-encoded for a device that can't play the
  original. On this server that should happen on the Intel GPU (Quick Sync).
  If it falls back to the CPU, one stream can use most of the processor, so
  that gets an event.

Reads one endpoint, /Sessions, with an API key made in Jellyfin's dashboard
(Dashboard, API Keys). The key is set only on the server (STX_JELLYFIN_KEY).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

from . import __version__

if TYPE_CHECKING:
    from .collector import Collector

log = logging.getLogger(__name__)

POLL_EVERY = 10.0
DEFAULT_URL = "http://127.0.0.1:8096"
TICKS = 10_000_000  # Jellyfin counts time in 100-nanosecond ticks
HARDWARE = {"qsv": "Quick Sync", "vaapi": "the Intel GPU (VAAPI)", "nvenc": "the NVIDIA GPU", "amf": "the AMD GPU",
            "videotoolbox": "VideoToolbox", "v4l2m2m": "V4L2", "rkmpp": "Rockchip MPP"}
MODE_LABEL = {"direct": "Direct play", "remux": "Direct stream", "software": "Transcoding on the CPU"}


def _seconds(ticks) -> float | None:
    return ticks / TICKS if isinstance(ticks, (int, float)) else None


def parse_session(s: dict) -> dict | None:
    """One row for the Now playing panel, or None if this session isn't playing anything."""
    item = s.get("NowPlayingItem")
    if not item:
        return None
    kind = item.get("Type") or ""
    title, subtitle = item.get("Name") or "Unknown", ""
    if kind == "Episode" and item.get("SeriesName"):
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        number = f" S{season:02d}E{episode:02d}" if isinstance(season, int) and isinstance(episode, int) else ""
        title, subtitle = f"{item['SeriesName']}{number}", item.get("Name") or ""
    elif kind == "Movie" and item.get("ProductionYear"):
        subtitle = str(item["ProductionYear"])
    elif kind == "Audio":
        subtitle = ", ".join(item.get("Artists") or []) or item.get("Album") or ""

    play = s.get("PlayState") or {}
    info = s.get("TranscodingInfo") or None
    is_video = (item.get("MediaType") or "").lower() == "video" or kind in ("Movie", "Episode", "Video", "MusicVideo", "TvChannel")
    hardware = ""
    if info and is_video and info.get("IsVideoDirect") is False:
        hardware = str(info.get("HardwareAccelerationType") or "none").lower()
        mode = "hardware" if hardware not in ("", "none") else "software"
    elif info or play.get("PlayMethod") == "DirectStream":
        mode = "remux"
    else:
        mode = "direct"
    label = f"Transcoding on {HARDWARE.get(hardware, hardware)}" if mode == "hardware" else MODE_LABEL[mode]
    reasons = info.get("TranscodeReasons") if info else None
    if isinstance(reasons, str):
        reasons = [r.strip() for r in reasons.split(",") if r.strip()]
    return {
        "id": s.get("Id") or "",
        "user": s.get("UserName") or "Someone",
        "device": s.get("DeviceName") or s.get("Client") or "",
        "client": s.get("Client") or "",
        "title": title, "subtitle": subtitle, "kind": kind,
        "paused": bool(play.get("IsPaused")),
        "position": _seconds(play.get("PositionTicks")),
        "runtime": _seconds(item.get("RunTimeTicks")),
        "mode": mode, "label": label,
        "video_codec": (info or {}).get("VideoCodec"),
        "bitrate": (info or {}).get("Bitrate"),
        "reasons": reasons or [],
    }


class Jellyfin:
    def __init__(self, collector: "Collector", url: str, key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.c = collector
        self.key = key.strip()
        self.url = (url.strip() or DEFAULT_URL).rstrip("/")
        auth = (f'MediaBrowser Client="stx-monitor", Device="stx-server", DeviceId="stx-monitor", '
                f'Version="{__version__}", Token="{self.key}"')
        self.http = httpx.AsyncClient(base_url=self.url, timeout=8.0, transport=transport,
                                      headers={"Authorization": auth, "Accept": "application/json"})
        self.sessions: list[dict] = []
        self.ok: bool | None = None
        self.error: str | None = None
        self.checked: float | None = None
        self._software: set[str] = set()
        self._view: dict | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def close(self) -> None:
        await self.http.aclose()

    async def poll(self) -> None:
        if not self.configured:
            return
        try:
            r = await self.http.get("/Sessions", params={"ActiveWithinSeconds": 120})
            r.raise_for_status()
            sessions = [p for p in (parse_session(s) for s in r.json()) if p]
            self.ok, self.error = True, None
        except (httpx.HTTPError, ValueError) as exc:
            sessions = []
            self.ok = False
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                self.error = "Jellyfin refused the API key. Make a new one in its Dashboard under API Keys."
            elif isinstance(exc, httpx.HTTPStatusError):
                self.error = f"Jellyfin answered HTTP {exc.response.status_code}"
            else:
                self.error = "Jellyfin isn't answering"
        self.checked = time.time()
        self.sessions = sessions
        self._watch_software(sessions)
        view = self.view()
        if view != self._view:
            self._view = view
            self.c.broadcast("jellyfin", view)

    def _watch_software(self, sessions: list[dict]) -> None:
        """One event when a stream starts transcoding on the CPU, not one per poll."""
        now_software = {s["id"] for s in sessions if s["mode"] == "software"}
        from .collector import event  # here, not at the top: collector imports this module
        for s in sessions:
            if s["id"] in now_software and s["id"] not in self._software:
                why = f" Reason: {', '.join(s['reasons'])}." if s["reasons"] else ""
                self.c.add_event(event(
                    "media", "warn", "Jellyfin is transcoding on the CPU",
                    f"{s['title']} for {s['user']} on {s['device']}.{why} "
                    "Quick Sync isn't being used: check Dashboard, Playback, Transcoding in Jellyfin."))
        self._software = now_software

    def view(self) -> dict:
        return {"configured": self.configured, "ok": self.ok, "error": self.error, "sessions": self.sessions}
