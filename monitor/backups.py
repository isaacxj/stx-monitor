"""Backups panel and alerts, from the status file stx-backup writes on the host.

stx-backup (host/stx-backup.py) runs as root on the host every night at 3 AM and
writes /var/lib/stx-backup/status.json when it starts and when it finishes.
stx-monitor reads that file through its read-only view of the host, so it
never needs access to the backups themselves or their password.

What turns into an event (and so maybe a phone alert):
  - a run that failed, with restic's reason
  - no successful backup for STALE_AFTER, even if nothing reported a failure
    (the timer could be off, or the server asleep every night at 3)
  - the first good run after either of those
A normal nightly success is a quiet info event in Activity.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import Collector

log = logging.getLogger(__name__)

STATUS_PATH = "var/lib/stx-backup/status.json"
TIMER_PATH = "etc/systemd/system/stx-backup.timer"
STALE_AFTER = 36 * 3600
CHECK_EVERY = 60


def human_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" or n >= 100 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_age(seconds: float) -> str:
    hours = seconds / 3600
    return f"{hours:.0f} hours" if hours < 72 else f"{hours / 24:.0f} days"


class Backups:
    def __init__(self, collector: "Collector"):
        self.c = collector
        root = Path(collector.settings.host_root)
        self.status_path = root / STATUS_PATH
        self.timer_path = root / TIMER_PATH
        self.status: dict | None = None
        self._mtime: float | None = None
        self._view: dict | None = None

    # ------------------------------------------------------------- reading
    def load(self) -> bool:
        """Re-read the status file if it changed. Returns True if it did."""
        try:
            mtime = self.status_path.stat().st_mtime
        except OSError:
            changed = self.status is not None
            self.status, self._mtime = None, None
            return changed
        if mtime == self._mtime:
            return False
        try:
            self.status = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return False  # caught it mid-write; the next check will read it
        self._mtime = mtime
        return True

    @property
    def running(self) -> bool:
        return bool(self.status and self.status.get("running"))

    def paused_by_backup(self, name: str | None) -> bool:
        return self.running and name in (self.status.get("paused") or [])

    def state(self, now: float | None = None) -> str:
        now = now or time.time()
        s = self.status
        if not s:
            return "not-installed" if not self.timer_path.exists() else "never"
        if s.get("running"):
            return "running"
        if s.get("ok") is False:
            return "failed"
        last = s.get("last_ok")
        if not last:
            return "never"
        return "stale" if now - last > STALE_AFTER else "ok"

    def view(self) -> dict:
        s = self.status or {}
        keep = ("started", "finished", "error", "last_ok", "last_ok_summary", "check", "apps", "skipped",
                "warnings", "snapshots", "oldest", "repo_size", "drive_free", "drive_total", "repo",
                "paused", "paused_seconds")
        return {
            "state": self.state(),
            "installed": self.timer_path.exists() or bool(self.status),
            "helper": self.c.actions.helper.installed,
            **{k: s.get(k) for k in keep},
        }

    # ------------------------------------------------------------- checking
    async def check(self) -> None:
        self.load()
        state = self.state()
        seen = self.c.store.get("backups_seen") or {}
        finished = (self.status or {}).get("finished")
        events = []
        if finished and finished != seen.get("finished"):
            s = self.status
            if s.get("ok"):
                summ = s.get("last_ok_summary") or {}
                n = len(s.get("apps") or [])
                detail = (f"{human_bytes(summ.get('data_added'))} new, took {summ.get('duration', 0)} s."
                          + (f" {s['warnings'][0]}" if s.get("warnings") else ""))
                if seen.get("state") in ("failed", "stale"):
                    events.append(("ok", "Backups are working again", f"Backed up {n} apps. {detail}"))
                else:
                    events.append(("info", f"Backed up {n} apps", detail))
            else:
                events.append(("error", "Backup failed", s.get("error") or "No reason given."))
        elif state == "stale" and seen.get("state") != "stale":
            age = time.time() - (self.status or {}).get("last_ok", 0)
            events.append(("warn", f"No backup for {human_age(age)}",
                           "The last good one finished then. Is the backup drive plugged in, "
                           "and was the server on at 3 AM?"))
        if finished != seen.get("finished") or state != seen.get("state"):
            self.c.store.set("backups_seen", {"finished": finished, "state": state})
        from .collector import event  # here, not at the top: collector imports this module
        for level, title, detail in events:
            self.c.add_event(event("backup", level, title, detail))
        view = self.view()
        if view != self._view:
            self._view = view
            self.c.broadcast("backups", view)

    async def run_now(self, actor: str, device: str) -> dict:
        if not self.c.actions.helper.installed:
            raise RuntimeError("Backing up from here needs stx-helper. Run scripts/install-helper.sh with sudo.")
        if self.running:
            raise RuntimeError("A backup is already running")
        ok, detail = await self.c.actions.helper.call("backup")
        row = self.c.store.add_action(actor, device, "backup", "server", "ok" if ok else "failed", detail)
        self.c.broadcast("action", row)
        if not ok:
            raise RuntimeError(detail)
        return self.view()
