"""Storage health: when each drive will fill up, and whether it is wearing out.

Two separate questions:

- Forecast. Once an hour the collector hands over how full each filesystem is,
  and one row per filesystem per day is kept in SQLite. A straight line fitted
  through the last 30 days gives the growth per day, and from that the number
  of days until it is full.
- SMART. Drives keep their own health record (Self-Monitoring, Analysis and
  Reporting Technology): a pass/fail verdict, the temperature, hours powered
  on, and counts of sectors that have gone bad. Reading it needs root and the
  raw device, so stx-helper runs `smartctl` on the host and hands back a short
  summary.

Both turn into events only when something crosses a line, so a drive that is
failing produces one alert, not one a day.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import Collector

log = logging.getLogger(__name__)

DAY = 86400
RECORD_EVERY = 1800.0
FIT_DAYS = 30
MIN_POINTS = 3
MIN_SPAN_DAYS = 2
WARN_DAYS = 14
CLEAR_DAYS = 21
# Growth below this share of the drive per day is noise, not a trend.
MIN_GROWTH_SHARE = 0.00005


def fit_growth(points: list[tuple[int, float]]) -> float | None:
    """Bytes per day from (day number, bytes used) points, by least squares.

    Returns None when there isn't enough history to call it a trend.
    """
    if len(points) < MIN_POINTS:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if max(xs) - min(xs) < MIN_SPAN_DAYS:
        return None
    n = len(points)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    var = sum((x - mean_x) ** 2 for x in xs)
    if var == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var


def forecast(points: list[tuple[int, float]], free: float | None, total: float | None) -> dict:
    """What the usage history says about one filesystem."""
    out = {"days_of_data": (max(p[0] for p in points) - min(p[0] for p in points) + 1) if points else 0,
           "growth_per_day": None, "days_left": None}
    growth = fit_growth(points)
    if growth is None:
        return out
    out["growth_per_day"] = round(growth)
    if free is not None and total and growth > total * MIN_GROWTH_SHARE:
        out["days_left"] = round(free / growth, 1)
    return out


def verdict(disk: dict) -> tuple[str, str]:
    """("ok" | "warn" | "failing" | "unknown", one sentence) for a drive's SMART summary."""
    if disk.get("error") or not disk.get("supported", True):
        return "unknown", disk.get("error") or "This drive doesn't report its health."
    if disk.get("passed") is False:
        return "failing", "The drive's own health check says it is failing. Copy anything important off it and replace it."
    if (disk.get("critical_warning") or 0) > 0:
        return "failing", "The drive is raising a critical warning. Copy anything important off it and replace it."
    bad = [(disk.get("uncorrectable") or 0, "can't be read", "can't be read"),
           (disk.get("pending") or 0, "is waiting to be remapped", "are waiting to be remapped"),
           (disk.get("reallocated") or 0, "has been replaced by a spare", "have been replaced by spares")]
    for count, one, many in bad:
        if count > 0:
            return "warn", (f"{count} sector{'s' * (count != 1)} {one if count == 1 else many}. "
                            "It still works, but drives often get worse from here.")
    if (disk.get("media_errors") or 0) > 0:
        return "warn", f"{disk['media_errors']} media error{'s' * (disk['media_errors'] != 1)} recorded."
    if (disk.get("wear") or 0) >= 90:
        return "warn", f"It has used {disk['wear']}% of its rated lifetime writes."
    if disk.get("passed") is None:
        return "unknown", "The drive gave no health verdict."
    return "ok", "Healthy."


def human_days(days: float) -> str:
    if days < 1:
        return "less than a day"
    if days < 60:
        return f"about {round(days)} day{'s' * (round(days) != 1)}"
    if days < 730:
        return f"about {round(days / 30)} months"
    return f"about {round(days / 365)} years"


class StorageHealth:
    def __init__(self, collector: "Collector"):
        self.c = collector
        self.store = collector.store
        self._recorded_at = 0.0
        self._mounts: list[dict] = []
        self._view: dict | None = None
        self.checking = False

    # ------------------------------------------------------------- forecast
    def record(self, sample: dict, now: float | None = None) -> None:
        """Called with every host sample; keeps one row per filesystem per day."""
        now = now or sample.get("time") or time.time()
        self._mounts = [m for m in sample.get("mounts") or [] if m.get("total")]
        if now - self._recorded_at < RECORD_EVERY:
            return
        self._recorded_at = now
        day = int(now // DAY)
        rows = [(day, m["mountpoint"], int(m.get("used") or 0), int(m["total"])) for m in self._mounts
                if not m.get("readonly")]
        if rows:
            self.store.save_disk_usage(rows, keep_from=day - 120)
        self._check_forecasts(now)
        self._publish()

    def forecasts(self, now: float | None = None) -> dict[str, dict]:
        day = int((now or time.time()) // DAY)
        history: dict[str, list[tuple[int, float]]] = {}
        for r in self.store.disk_usage_since(day - FIT_DAYS):
            history.setdefault(r["mount"], []).append((r["day"], r["used"]))
        out = {}
        for m in self._mounts:
            points = history.get(m["mountpoint"])
            if points:
                out[m["mountpoint"]] = forecast(points, m.get("free"), m.get("total"))
        return out

    def _check_forecasts(self, now: float) -> None:
        warned: list[str] = self.store.get("forecast_warned") or []
        changed = False
        from .collector import event  # here, not at the top: collector imports this module
        for mount, f in self.forecasts(now).items():
            days = f["days_left"]
            if days is not None and days <= WARN_DAYS and mount not in warned:
                warned.append(mount)
                changed = True
                self.c.add_event(event("storage", "warn", f"{mount} will be full in {human_days(days)}",
                                       f"It is growing by {_gb(f['growth_per_day'])} a day."))
            elif mount in warned and (days is None or days >= CLEAR_DAYS):
                warned.remove(mount)
                changed = True
        if changed:
            self.store.set("forecast_warned", warned)

    # ----------------------------------------------------------------- SMART
    def smart(self) -> dict:
        saved = self.store.get("smart") or {}
        disks = []
        for d in saved.get("disks") or []:
            state, sentence = verdict(d)
            disks.append({**d, "state": state, "sentence": sentence})
        return {"checked": saved.get("checked"), "error": saved.get("error"), "disks": disks,
                "helper": self.c.actions.helper.installed, "checking": self.checking}

    async def check_smart(self) -> tuple[bool, str]:
        """Ask stx-helper to read every drive's SMART record. Returns (ok, one line for History)."""
        helper = self.c.actions.helper
        if not helper.installed:
            return False, "Drive health needs stx-helper. Run scripts/install-helper.sh with sudo."
        self.checking = True
        self._publish()
        try:
            ok, detail = await helper.call("smart", timeout=180.0)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc) or type(exc).__name__
        finally:
            self.checking = False
        if not ok:
            if "Unknown command" in detail:
                detail = "stx-helper is out of date. Run scripts/install-helper.sh with sudo."
            self.store.set("smart", {**(self.store.get("smart") or {}), "error": detail, "checked": time.time()})
            self._publish()
            return False, detail
        try:
            disks = json.loads(detail)["disks"]
        except (ValueError, KeyError, TypeError):
            return False, "stx-helper gave an answer that could not be read"
        before = {d["name"]: verdict(d)[0] for d in (self.store.get("smart") or {}).get("disks") or []}
        self.store.set("smart", {"checked": time.time(), "error": None, "disks": disks})
        from .collector import event
        states = []
        for d in disks:
            state, sentence = verdict(d)
            states.append(state)
            label = f"{d['name']} ({d['model']})" if d.get("model") else d["name"]
            was = before.get(d["name"])
            if state in ("failing", "warn") and was != state:
                self.c.add_event(event("storage", "error" if state == "failing" else "warn",
                                       f"{label} {'is failing' if state == 'failing' else 'shows wear'}", sentence))
            elif state == "ok" and was in ("failing", "warn"):
                self.c.add_event(event("storage", "ok", f"{label} reports healthy again", ""))
        self._publish()
        bad = sum(s in ("failing", "warn") for s in states)
        unknown = states.count("unknown")
        summary = f"{len(disks)} drive{'s' * (len(disks) != 1)} checked, " + (f"{bad} need{'s' * (bad == 1)} attention" if bad else "all healthy")
        if unknown and not bad:
            summary = f"{len(disks) - unknown} healthy, {unknown} don't report health"
        return True, summary

    # ------------------------------------------------------------------ view
    def view(self) -> dict:
        return {"forecast": self.forecasts(), "smart": self.smart()}

    def _publish(self) -> None:
        view = self.view()
        if view != self._view:
            self._view = view
            self.c.broadcast("storage", view)


def _gb(n: float | None) -> str:
    if n is None:
        return "?"
    if abs(n) >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.0f} MB"
