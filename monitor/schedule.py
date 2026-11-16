"""Scheduled tasks: the housekeeping the server does by itself, and a record of how each run went.

Each task has a fixed slot in the week (server local time). A loop wakes every
half minute and runs whatever has come due. Three rules keep it predictable:

- A task that was due while the server was off still runs if the server comes
  back within CATCH_UP; after that it waits for its next slot.
- Turning a task on never makes it run on the spot.
- Every run, automatic or "Run now", goes in History with its result, the same
  as an action taken by hand.

The nightly backup isn't run from here. It belongs to a systemd timer on the
host (see host/stx-backup.timer) so it works even when stx-monitor is down. It
is listed with the others so there is one place to see everything scheduled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .storage_health import human_days

if TYPE_CHECKING:
    from .collector import Collector

log = logging.getLogger(__name__)

TICK = 30.0
CATCH_UP = 6 * 3600
WEEK = 7 * 86400
DAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
ACTOR = "Schedule"


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    description: str
    hour: int
    minute: int = 0
    weekday: int | None = None  # 0 is Monday; None means every day
    enabled: bool = True
    needs_helper: bool = False

    @property
    def when(self) -> str:
        clock = datetime(2000, 1, 1, self.hour, self.minute).strftime("%I:%M %p").lstrip("0")
        return f"{DAYS[self.weekday]} at {clock}" if self.weekday is not None else f"Every day at {clock}"


TASKS = (
    Task("check-updates", "Check for app updates", "Asks each registry whether a newer image exists. Nothing is installed.",
         hour=4, minute=30),
    Task("update-apps", "Install app updates", "Pulls the newer image and recreates each app that has one. Off unless you want apps to update themselves.",
         hour=5, weekday=0, enabled=False, needs_helper=True),
    Task("prune-images", "Clean up unused images", "Deletes Docker images that no container uses.",
         hour=4, weekday=6, needs_helper=True),
    Task("disk-health", "Check drive health", "Reads every drive's own health record (SMART).",
         hour=6, needs_helper=True),
    Task("weekly-report", "Weekly report", "A summary of the week sent to your phone: crashes, backups, disk space, updates.",
         hour=9, weekday=6),
)
BY_ID = {t.id: t for t in TASKS}


def host_timezone(root: Path) -> tzinfo:
    """The server's own time zone, read from the host rather than the container (which is UTC)."""
    link = Path(root) / "etc/localtime"
    try:
        target = os.readlink(link)
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (OSError, ValueError, KeyError):
        pass
    try:
        name = (Path(root) / "etc/timezone").read_text().strip()
        if name:
            return ZoneInfo(name)
    except (OSError, ValueError, KeyError):
        pass
    try:
        with open(link, "rb") as f:
            return ZoneInfo.from_file(f)
    except (OSError, ValueError):
        return timezone.utc


def last_due(task: Task, now: datetime) -> datetime:
    """The most recent slot at or before now."""
    slot = now.replace(hour=task.hour, minute=task.minute, second=0, microsecond=0)
    if task.weekday is not None:
        slot -= timedelta(days=(slot.weekday() - task.weekday) % 7)
    step = timedelta(days=7 if task.weekday is not None else 1)
    while slot > now:
        slot -= step
    return slot


def next_due(task: Task, now: datetime) -> datetime:
    return last_due(task, now) + timedelta(days=7 if task.weekday is not None else 1)


class Scheduler:
    def __init__(self, collector: "Collector"):
        self.c = collector
        self.store = collector.store
        self.tz = host_timezone(collector.settings.host_root)
        self.running: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.last_report: dict | None = self.store.get("weekly_report")

    async def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()

    # -------------------------------------------------------------- settings
    def enabled(self, task: Task) -> bool:
        saved = (self.store.get("schedule") or {}).get(task.id) or {}
        return bool(saved.get("enabled", task.enabled))

    def unavailable(self, task: Task) -> str:
        if task.needs_helper and not self.c.actions.helper.installed:
            return "Needs stx-helper on the server"
        return ""

    def set_enabled(self, task_id: str, enabled: bool, actor: str, device: str) -> dict:
        task = self._task(task_id)
        saved = self.store.get("schedule") or {}
        saved[task.id] = {"enabled": bool(enabled)}
        self.store.set("schedule", saved)
        if enabled:
            # Start from the next slot: switching it on must not trigger a catch-up run.
            self._mark(task, handled=last_due(task, self._now()).timestamp())
        row = self.store.add_action(actor, device, "schedule", "server", "ok",
                                    f"{task.name}: {'on, ' + task.when[0].lower() + task.when[1:] if enabled else 'off'}")
        self.c.broadcast("action", row)
        self._publish()
        return self.view()

    def _task(self, task_id: str) -> Task:
        if task_id not in BY_ID:
            raise ValueError(f"Unknown task: {task_id}")
        return BY_ID[task_id]

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _state(self) -> dict:
        return self.store.get("schedule_state") or {}

    def _mark(self, task: Task, **changes) -> None:
        state = self._state()
        state[task.id] = {**(state.get(task.id) or {}), **changes}
        self.store.set("schedule_state", state)

    # ----------------------------------------------------------------- loop
    async def tick(self, now: datetime | None = None) -> list[str]:
        """Start whatever has come due. Returns the ids started (for tests)."""
        now = now or self._now()
        state = self._state()
        started = []
        for task in TASKS:
            due = last_due(task, now).timestamp()
            handled = (state.get(task.id) or {}).get("handled")
            if handled is None:
                self._mark(task, handled=due)  # first sight of this task: wait for its next slot
                continue
            if handled >= due:
                continue
            self._mark(task, handled=due)
            if not self.enabled(task) or self.unavailable(task) or task.id in self.running:
                continue
            if now.timestamp() - due > CATCH_UP:
                continue
            started.append(task.id)
            self._spawn(self.run(task.id, ACTOR, ""))
        return started

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def request(self, task_id: str, actor: str, device: str) -> dict:
        """Run now, from the console."""
        task = self._task(task_id)
        reason = self.unavailable(task)
        if reason:
            raise ValueError(reason)
        if task.id in self.running:
            raise ValueError(f"{task.name} is already running")
        self._spawn(self.run(task.id, actor, device))
        return {"started": True}

    async def run(self, task_id: str, actor: str, device: str) -> dict:
        task = self._task(task_id)
        self.running.add(task.id)
        row = self.store.add_action(actor, device, task.id, "server", "running")
        self.c.broadcast("action", row)
        self._publish()
        try:
            ok, detail = await getattr(self, "_" + task.id.replace("-", "_"))()
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduled task %s failed", task.id)
            ok, detail = False, str(exc) or type(exc).__name__
        finally:
            self.running.discard(task.id)
        self._mark(task, last_run=time.time(), ok=ok, detail=detail[:300])
        row = self.store.finish_action(row["id"], "ok" if ok else "failed", detail[:300])
        self.c.broadcast("action", row)
        self._publish()
        return row

    # ---------------------------------------------------------------- tasks
    async def _check_updates(self) -> tuple[bool, str]:
        n = await self.c.actions.check_updates()
        return True, f"{n} update{'s' * (n != 1)} available" if n else "Everything is up to date"

    async def _update_apps(self) -> tuple[bool, str]:
        actions = self.c.actions
        await actions.check_updates()
        todo = [c for c in self.c.containers
                if (actions.updates.get(c["id"]) or {}).get("available") and "update" in actions.allowed(c)]
        if not todo:
            return True, "Nothing to update"
        done, failed = [], []
        for c in todo:
            actions.expect_stop(c["id"])
            ok, _ = await actions.helper.call("update", {"container": c["name"]})
            (done if ok else failed).append(c["name"])
            actions.updates.pop(c["id"], None)
        self.c.request_docker_refresh()
        detail = f"Updated {', '.join(done)}" if done else "No app was updated"
        if failed:
            detail += f". Failed: {', '.join(failed)}"
        return not failed, detail

    async def _prune_images(self) -> tuple[bool, str]:
        return await self.c.actions.helper.call("prune-images")

    async def _disk_health(self) -> tuple[bool, str]:
        return await self.c.storage.check_smart()

    async def _weekly_report(self) -> tuple[bool, str]:
        report = self.build_report()
        self.last_report = report
        self.store.set("weekly_report", report)
        from .collector import event  # here, not at the top: collector imports this module
        self.c.add_event(event("report", "info", report["title"], " ".join(report["lines"])))
        alerts = self.c.alerts
        if not alerts.configured:
            return True, "Saved here. Phone alerts are off, so it wasn't sent."
        if not alerts.categories().get("reports"):
            return True, "Saved here. Weekly report alerts are switched off."
        sent = await alerts.send({"title": report["title"], "message": "\n".join(report["lines"]),
                                  "priority": 3 if report["problems"] else 2, "tags": ["bar_chart"]})
        return sent, "Sent to your phone" if sent else "Could not send it: " + ((alerts.last_error or {}).get("detail") or "unknown error")

    # --------------------------------------------------------------- report
    def build_report(self, now: float | None = None) -> dict:
        """The week in a few plain lines, with anything that needs attention first."""
        now = now or time.time()
        since = now - WEEK
        c = self.c
        problems, lines = [], []

        reports = c.store.reports_since(since)
        crashes: dict[str, int] = {}
        for r in reports:
            if r["kind"] == "container":
                crashes[r["target"]] = crashes.get(r["target"], 0) + 1
        unclean = sum(r["kind"] == "host" for r in reports)
        counts = c.apps_map().get("counts") or {}
        apps = f"Apps: {counts.get('running', 0)} of {counts.get('deployed', 0)} running"
        if crashes:
            worst = sorted(crashes.items(), key=lambda kv: -kv[1])[:3]
            total = sum(crashes.values())
            apps += f", {total} crash{'es' * (total != 1)} this week ({', '.join(f'{n} {k}' for k, n in worst)})"
            problems.append("crashes")
        else:
            apps += ", no crashes this week"
        if counts.get("down"):
            problems.append("apps down")
        lines.append(apps + ".")

        uptime = (c.host_sample or {}).get("uptime_s")
        if uptime is not None:
            line = f"Server: up {human_days(uptime / 86400).replace('about ', '')}"
            if unclean:
                line += f", {unclean} unclean shutdown{'s' * (unclean != 1)} this week"
                problems.append("unclean shutdown")
            lines.append(line + ".")

        b = c.backups.view()
        if b["state"] == "not-installed":
            lines.append("Backups: not set up.")
            problems.append("no backups")
        elif b["state"] in ("failed", "stale", "never"):
            lines.append("Backups: " + {"failed": "the last one failed", "stale": "overdue", "never": "none yet"}[b["state"]] + ".")
            problems.append("backups")
        else:
            nights = len([e for e in c.store.events_between(since, now, ("backup",)) if e["level"] in ("info", "ok")])
            lines.append(f"Backups: {nights} good run{'s' * (nights != 1)} this week.")

        storage = c.storage.view()
        parts = []
        for m in (c.host_sample or {}).get("mounts") or []:
            if m.get("percent") is None:
                continue
            part = f"{m['mountpoint']} {m['percent']:.0f}% full"
            days = (storage["forecast"].get(m["mountpoint"]) or {}).get("days_left")
            if days is not None and days <= 60:
                part += f", full in {human_days(days)}"
                if days <= 30:
                    problems.append("disk filling")
            if m["percent"] >= 90:
                problems.append("disk nearly full")
            parts.append(part)
        if parts:
            lines.append("Disks: " + "; ".join(parts) + ".")
        bad = [d for d in storage["smart"]["disks"] if d["state"] in ("failing", "warn")]
        if bad:
            lines.append("Drive health: " + "; ".join(f"{d['name']} {'is failing' if d['state'] == 'failing' else 'shows wear'}" for d in bad) + ".")
            problems.append("drive health")
        elif storage["smart"]["disks"]:
            lines.append("Drive health: all drives healthy.")

        updates = sum(1 for u in c.actions.updates.values() if u.get("available"))
        if updates:
            lines.append(f"Updates: {updates} app{'s' * (updates != 1)} can be updated.")

        problems = list(dict.fromkeys(problems))
        title = "Weekly report: all good" if not problems else \
            f"Weekly report: {len(problems)} thing{'s' * (len(problems) != 1)} to look at"
        return {"time": now, "title": title, "lines": lines, "problems": problems}

    # ----------------------------------------------------------------- view
    def view(self) -> dict:
        now = self._now()
        state = self._state()
        tasks = []
        for task in TASKS:
            s = state.get(task.id) or {}
            enabled = self.enabled(task)
            reason = self.unavailable(task)
            tasks.append({
                "id": task.id, "name": task.name, "description": task.description, "when": task.when,
                "enabled": enabled, "unavailable": reason, "running": task.id in self.running,
                "next_run": next_due(task, now).timestamp() if enabled and not reason else None,
                "last_run": s.get("last_run"), "ok": s.get("ok"), "detail": s.get("detail") or "",
            })
        b = self.c.backups.view()
        backup = {
            "id": "backup", "name": "Back up app data", "description": "Encrypted copy of every app's data to the backup drive. Run by the server itself, so it works even if this console is down.",
            "when": "Every day at 3:00 AM", "managed": True, "installed": b["state"] != "not-installed",
            "running": b["state"] == "running", "last_run": b.get("finished"),
            "ok": None if not b.get("finished") else b["state"] != "failed",
            "detail": (b.get("error") or "") if b["state"] == "failed" else "",
        }
        return {"tasks": [backup, *tasks], "report": self.last_report,
                "timezone": getattr(self.tz, "key", None) or "UTC"}

    def _publish(self) -> None:
        self.c.broadcast("schedule", self.view())
