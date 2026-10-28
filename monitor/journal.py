"""The host's system journal, read with journalctl.

systemd-journald collects log output from the kernel and every service on the
host (Docker, Tailscale, SSH, cron...) into binary files under
/var/log/journal. journalctl is the tool that reads them. `--directory` points
it at a folder of journal files instead of the running system's own, and
`-b -1` means "the previous boot".

Inside the container we run the host's own journalctl through `chroot` (run a
program with /host as its root folder), so its version always matches the
journal files the host wrote. Running straight on the host we just call
journalctl.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

JOURNAL_DIRS = ("var/log/journal", "run/log/journal")

# Lines that only appear when the previous boot ended with a clean shutdown or reboot.
CLEAN_SHUTDOWN = re.compile(
    r"Journal stopped"
    r"|Reached target .*(Shutdown|Power[- ]?Off|Reboot|Halt|Kexec)"
    r"|System is (rebooting|powering down|halting)"
    r"|Shutting down\.$"
    r"|reboot: (Power down|Restarting system)",
    re.IGNORECASE,
)


def level_of(priority: int | None) -> str:
    """syslog priority: 0-3 are errors, 4 a warning, 5-6 information, 7 debug."""
    if priority is None:
        return "info"
    if priority <= 3:
        return "error"
    if priority == 4:
        return "warn"
    if priority == 7:
        return "debug"
    return "info"


def _message(value) -> str:
    if isinstance(value, list):  # journald stores non-UTF-8 messages as a byte array
        return bytes(v for v in value if isinstance(v, int) and 0 <= v < 256).decode("utf-8", "replace")
    return str(value or "")


def parse_entry(entry: dict) -> dict:
    try:
        ts = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
    except (TypeError, ValueError):
        ts = 0.0
    try:
        priority = int(entry.get("PRIORITY"))
    except (TypeError, ValueError):
        priority = None
    if entry.get("_TRANSPORT") == "kernel":
        source = "kernel"
    else:
        source = entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or "journal"
    return {
        "ts": ts,
        "origin": "host",
        "source": source,
        "unit": entry.get("_SYSTEMD_UNIT"),
        "level": level_of(priority),
        "text": _message(entry.get("MESSAGE")).rstrip(),
        "boot": entry.get("_BOOT_ID"),
        "identifier": entry.get("SYSLOG_IDENTIFIER"),
    }


def parse_lines(output: str) -> list[dict]:
    out = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(parse_entry(json.loads(line)))
        except ValueError:
            continue
    return out


def ended_cleanly(entries: list[dict]) -> bool:
    """True when the tail of a boot's journal shows an orderly shutdown."""
    for e in entries:
        if e.get("identifier") == "systemd-shutdown" or CLEAN_SHUTDOWN.search(e.get("text") or ""):
            return True
    return False


def journal_command(root: Path) -> list[str] | None:
    """The journalctl command line to use, or None when there is no journal to read."""
    for rel in JOURNAL_DIRS:
        folder = root / rel
        try:
            if not folder.is_dir() or not any(folder.iterdir()):
                continue
        except OSError:
            continue
        if root != Path("/") and (root / "usr/bin/journalctl").exists() and shutil.which("chroot"):
            return ["chroot", str(root), "journalctl", f"--directory=/{rel}"]
        if shutil.which("journalctl"):
            return ["journalctl", f"--directory={folder}"]
    return None


class Journal:
    def __init__(self, root: Path):
        self.root = root
        self.command = journal_command(root)
        self.error: str | None = None if self.command else "No journal found under /var/log/journal"

    @property
    def available(self) -> bool:
        return self.command is not None

    async def query(self, *args: str, timeout: float = 20.0) -> list[dict]:
        if not self.command:
            return []
        proc = await asyncio.create_subprocess_exec(
            *self.command, "--no-pager", "-o", "json", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode not in (0, None) and not out:
            # journalctl exits 1 for "no entries" too; only report real errors.
            message = err.decode(errors="replace").strip()
            if message and "No entries" not in message and "Specifying boot ID" not in message:
                self.error = message.splitlines()[-1]
                log.warning("journalctl failed: %s", self.error)
        return parse_lines(out.decode("utf-8", "replace"))

    async def previous_boot_tail(self, lines: int = 300) -> list[dict]:
        return await self.query("-b", "-1", "-n", str(lines))

    async def follow(self, handle: Callable[[dict], Awaitable[None] | None], backlog: int = 300) -> None:
        """Stream new journal entries forever, restarting journalctl if it exits."""
        if not self.command:
            return
        delay = 1.0
        first = True
        while True:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command, "--no-pager", "-o", "json", "-f", "-n", str(backlog if first else 0),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=1024 * 1024,
                )
                first = False
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    delay = 1.0
                    try:
                        entry = parse_entry(json.loads(raw))
                    except ValueError:
                        continue
                    result = handle(entry)
                    if asyncio.iscoroutine(result):
                        await result
            except asyncio.CancelledError:
                if proc and proc.returncode is None:
                    proc.kill()
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("journal follow failed: %s", exc)
            finally:
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
