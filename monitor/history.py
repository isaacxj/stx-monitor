"""Short-term history for the sparklines and charts, kept in memory.

Two ring buffers (fixed-size lists where the oldest entry falls off):
- live: one point every 3 s for the last hour
- day:  one-minute averages for the last 24 hours, saved to disk so a restart
        does not wipe the chart
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

FIELDS = ("cpu", "mem", "temp", "rx", "tx", "watts")
LIVE_SIZE = 1200
DAY_SIZE = 1440
DAY_SECONDS = 24 * 3600


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


class History:
    def __init__(self) -> None:
        self.live: deque[list] = deque(maxlen=LIVE_SIZE)
        self.day: deque[list] = deque(maxlen=DAY_SIZE)
        self._minute: int | None = None
        self._bucket: list[list] = []

    def add(self, t: float, values: dict) -> list:
        point = [round(t, 1)] + [values.get(f) for f in FIELDS]
        self.live.append(point)
        minute = int(t // 60)
        if self._minute is not None and minute != self._minute:
            self._flush()
        self._minute = minute
        self._bucket.append(point)
        return point

    def _flush(self) -> None:
        if not self._bucket or self._minute is None:
            return
        columns = list(zip(*self._bucket))
        self.day.append([self._minute * 60] + [_avg(list(col)) for col in columns[1:]])
        self._bucket = []

    def snapshot(self) -> dict:
        return {"fields": ["t", *FIELDS], "live": list(self.live), "day": list(self.day)}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fields": ["t", *FIELDS], "day": list(self.day)}))
        os.replace(tmp, path)  # atomic: a crash mid-write never leaves half a file

    def load(self, path: Path, now: float | None = None) -> int:
        now = time.time() if now is None else now
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        if data.get("fields") != ["t", *FIELDS]:
            log.info("history file has different fields, ignoring it")
            return 0
        cutoff = now - DAY_SECONDS
        kept = [p for p in data.get("day") or [] if isinstance(p, list) and p and p[0] >= cutoff]
        self.day.extend(kept)
        return len(kept)
