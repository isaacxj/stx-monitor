"""Failure reports: what was going on when something went wrong, and what to try.

Two kinds:
- container: an app died with an error or ran out of memory. Saved the moment
  Docker reports it, with the exit code, Docker's view of its state, its last
  resource readings and its last 200 log lines.
- host: the server itself went down without shutting down cleanly (power cut,
  battery ran out, freeze). Detected at the next start by reading the end of
  the previous boot's journal.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

from .journal import ended_cleanly
from .logs import parse_ts

if TYPE_CHECKING:
    from .collector import Collector

log = logging.getLogger(__name__)

REPEAT_WINDOW = 600  # seconds: a crash loop updates one report instead of filing dozens
GiB = 1024 ** 3


def _gb(n: float | None) -> str:
    if not n:
        return "?"
    return f"{n / GiB:.1f} GB" if n < 10 * GiB else f"{n / GiB:.0f} GB"


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} day{'s' * (days != 1)}, {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min"
    return f"{seconds} s"


def what_happened(exit_code: int | None, oom: bool, mem_limit: int | None) -> tuple[str, str]:
    """(short cause, sentence) for a container that stopped."""
    if oom:
        if mem_limit:
            return "Out of memory", f"It went over its {_gb(mem_limit)} memory limit, so Linux stopped it."
        return "Out of memory", "The server ran low on memory and Linux stopped it to free some."
    if exit_code == 137:
        return "Killed", "It received a kill signal (exit code 137): a forced stop, or Linux ending it for memory."
    if exit_code == 139:
        return "Crashed", "It crashed inside the program (exit code 139, a segmentation fault)."
    if exit_code == 143:
        return "Stopped", "It was asked to stop (exit code 143)."
    if exit_code in (126, 127):
        return "Could not start", f"Its start command could not run (exit code {exit_code})."
    if exit_code == 125:
        return "Could not start", "Docker could not start it (exit code 125)."
    return "Exited with an error", f"It quit with exit code {exit_code}."


def suggest(name: str, exit_code: int | None, oom: bool, mem_limit: int | None, logs_text: str,
            repeats: int = 1, depends_on: list[str] | None = None) -> list[str]:
    """Rule-based suggestions, most likely fix first."""
    out: list[str] = []
    text = logs_text.lower()
    if oom and mem_limit:
        bigger = math.ceil(mem_limit * 1.5 / (GiB / 2)) * 0.5
        out.append(f"Raise {name}'s memory limit to {bigger:g} GB in its compose file (mem_limit), "
                   "or find what pushes it over the limit. Big library scans are a common cause.")
    elif oom:
        out.append("The whole server ran out of memory. Check the busiest processes on the System page, "
                   "give the hungriest container a mem_limit, or add swap.")
    elif exit_code == 137:
        out.append("If nobody stopped it, search the Server log around this time for "
                   "\"Out of memory\" or \"Killed process\".")
    elif exit_code == 139:
        out.append("Update the image, or go back to the previous version if this started after an update.")
    elif exit_code in (126, 127):
        out.append("Check the image name and the command or entrypoint in its compose file.")
    elif exit_code == 125:
        out.append("Check its compose file for typos in volumes, ports or devices.")

    rules = [
        (("permission denied", "operation not permitted"),
         "Check the permissions of its volume folders. linuxserver.io images choose their user with PUID and PGID."),
        (("address already in use", "bind: address"),
         "Another app is already using its port. Change one of the ports in compose."),
        (("no space left on device",),
         "A disk is full. Free some space (see the Storage page)."),
        (("database is locked", "database disk image is malformed", "corrupt"),
         "Its database looks locked or damaged. Make sure no other container shares its config folder, "
         "or restore that folder from a backup."),
        (("temporary failure in name resolution", "could not resolve", "name or service not known"),
         "It could not look up a name on the network. Check DNS, and that the VPN is up if it goes through Gluetun."),
        (("connection refused", "econnrefused"),
         "It could not reach something it depends on" + (f" ({', '.join(depends_on)})" if depends_on else "") +
         ". Check that it is running."),
        (("exec format error",),
         "The image was built for a different kind of processor. Use an amd64 image."),
    ]
    for needles, advice in rules:
        if any(n in text for n in needles) and advice not in out:
            out.append(advice)
    if repeats >= 3:
        out.append(f"This has happened {repeats} times today. Fix the cause before starting it again, "
                   "or it will keep failing.")
    if not out:
        out.append("The last log lines usually say why. Start it again once you have read them.")
    return out


def read_boot_id(collector: "Collector") -> str | None:
    try:
        raw = (collector.settings.host_root / "proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return raw.replace("-", "")


class FailureReports:
    def __init__(self, collector: "Collector"):
        self.c = collector
        self.store = collector.store

    # ---------------------------------------------------------------- summary
    def summary(self) -> dict:
        return {"unseen": self.store.unseen_reports(), "recent": self.store.list_reports(20)}

    # ------------------------------------------------------------- containers
    async def container_died(self, ev: dict) -> dict | None:
        """Called for every Docker "die" event."""
        c = self.c
        cid = ev.get("id") or ""
        if not c.docker or not cid:
            return None
        if c.actions and c.actions.expected_stop(cid):
            return None  # we stopped it on purpose
        stats = dict(c.stats.get(cid) or {})
        try:
            ins = await c.docker.inspect(cid, fresh=True)
        except Exception:  # noqa: BLE001
            ins = {}
        exit_code = ins.get("exit_code")
        if exit_code is None:
            try:
                exit_code = int(ev.get("exit_code"))
            except (TypeError, ValueError):
                exit_code = None
        oom = bool(ins.get("oom_killed"))
        if exit_code in (0, 143) and not oom:
            return None  # a normal exit or stop is not a failure
        name = ev.get("name") or cid
        app = c.app_for_container(cid)
        display = app["name"] if app else name

        now = time.time()
        finished = _epoch(ins.get("finished_at")) or now
        recent = [r for r in self.store.list_reports(10)
                  if r["target"] == name and -60 < finished - r["time"] < REPEAT_WINDOW]
        if recent:
            report = self.store.get_report(recent[0]["id"])
            data = report["data"]
            data["repeats_in_window"] = data.get("repeats_in_window", 1) + 1
            data["last_seen"] = now
            self.store.update_report(report["id"], data)
            return None

        try:
            lines = await c.docker.logs(cid, tail=200)
        except Exception:  # noqa: BLE001
            lines = []
        cause, sentence = what_happened(exit_code, oom, ins.get("memory_limit"))
        today = now - (now % 86400)
        repeats = self.store.count_reports_since(name, today) + 1
        depends_on = [c.app_name(x) for x in (app or {}).get("links", [])]
        started = _epoch(ins.get("started_at"))
        summary = sentence
        if repeats > 1:
            summary += f" It has happened {repeats} times today."
        data = {
            "container": {"id": cid, "name": name, "image": ins.get("image") or ev.get("image")},
            "app": app["id"] if app else None,
            "cause": cause,
            "exit_code": exit_code,
            "oom": oom,
            "error": ins.get("error"),
            "started_at": started,
            "finished_at": finished,
            "ran_for": finished - started if started else None,
            "restart_count": ins.get("restart_count"),
            "restart_policy": ins.get("restart_policy"),
            "memory_limit": ins.get("memory_limit"),
            "stats": stats,
            "logs": lines,
            "suggestions": suggest(display, exit_code, oom, ins.get("memory_limit"),
                                   "\n".join(x["text"] for x in lines[-200:]), repeats, depends_on),
            "repeats_today": repeats,
        }
        title = f"{display} ran out of memory" if oom else f"{display} {'crashed' if exit_code == 139 else 'stopped'}"
        report = self.store.add_report("container", name, title, summary, data, when=finished)
        c.add_event({"time": now, "kind": "app", "level": "info", "title": f"Saved a failure report for {name}",
                     "detail": cause, "container": cid, "name": name, "report": report["id"]})
        c.broadcast("report", _brief(report))
        return report

    # ------------------------------------------------------------------- host
    async def check_previous_boot(self) -> dict | None:
        """Look for a previous boot that ended without a clean shutdown."""
        c = self.c
        if not c.journal.available:
            return None
        entries = await c.journal.previous_boot_tail(300)
        if not entries:
            return None
        prev_boot = entries[-1].get("boot")
        if not prev_boot or self.store.get("reports.boot_checked") == prev_boot:
            return None
        self.store.set("reports.boot_checked", prev_boot)
        if ended_cleanly(entries):
            return None
        return self._host_report(entries, prev_boot)

    def _host_report(self, entries: list[dict], prev_boot: str) -> dict:
        c = self.c
        last_ts = entries[-1]["ts"]
        back_ts = c.host.identity.get("boot_time") or time.time()
        offline = max(0, back_ts - last_ts)
        readings = self.store.get("last_readings") or {}
        if readings.get("boot") and readings["boot"] != prev_boot:
            readings = {}  # those readings are from some other boot
        on_battery = readings.get("ac") is False
        capacity = readings.get("battery")
        temp = readings.get("temp")
        text = "\n".join(e["text"] for e in entries[-80:]).lower()
        autopilot = (self.store.get("autopilot") or {}).get("enabled", False)

        if "kernel panic" in text or "oops:" in text:
            cause = "The Linux kernel crashed"
        elif on_battery and capacity is not None and capacity <= 10:
            cause = "The battery ran out"
        elif on_battery:
            cause = "It lost power while running on battery"
        elif temp is not None and temp >= 95:
            cause = "It overheated"
        else:
            cause = "It lost power, froze, or was switched off at the power button"

        suggestions = []
        if on_battery:
            suggestions.append("Turn on power-outage autopilot so it shuts down cleanly instead of losing power mid-write."
                               if not autopilot else
                               "Autopilot was on but the battery emptied first. Raise the autopilot shutdown level.")
        if "kernel" in cause.lower():
            suggestions.append("Run `journalctl -k -b -1 -e` on the server to see the crash details.")
        if temp is not None and temp >= 90:
            suggestions.append("Clean the fans and vents, and keep the laptop on a hard surface with room to breathe.")
        if not suggestions:
            suggestions.append("If it happens again, run `journalctl -b -1 -e` on the server and check the power cable and adapter.")

        timeline = []
        for ev in self.store.events_between(last_ts - 12 * 3600, last_ts, ("power", "heat")):
            timeline.append({"time": ev["time"], "what": f"{ev['title']}. {ev.get('detail') or ''}".strip(),
                             "tone": "amber" if ev["level"] == "warn" else "cyan"})
        timeline.append({"time": last_ts, "what": "Last log line. No clean shutdown.", "tone": "red"})
        timeline.append({"time": back_ts, "what": "Server started again.", "tone": "cyan"})

        data = {
            "cause": cause,
            "offline_seconds": offline,
            "went_dark": last_ts,
            "back": back_ts,
            "readings": readings,
            "timeline": timeline,
            "last_lines": [{"ts": e["ts"], "source": e["source"], "level": e["level"], "text": e["text"]}
                           for e in entries[-40:]],
            "suggestions": suggestions,
            "boot": prev_boot,
        }
        summary = f"{cause}. It was off for {duration(offline)}."
        report = self.store.add_report("host", "server", "Server went offline", summary, data, when=last_ts)
        c.add_event({"time": time.time(), "kind": "power", "level": "error", "title": "The server went offline",
                     "detail": summary, "report": report["id"]})
        c.broadcast("report", _brief(report))
        return report


def _brief(report: dict) -> dict:
    out = {k: report[k] for k in ("id", "time", "kind", "target", "title", "summary", "seen")}
    out["cause"] = (report.get("data") or {}).get("cause")
    return out


def _epoch(value) -> float | None:
    if not value or str(value).startswith("0001-"):
        return None
    return parse_ts(str(value))
