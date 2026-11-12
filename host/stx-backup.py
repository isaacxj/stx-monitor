#!/usr/bin/env python3
"""stx-backup: nightly backups of every app's data on stx-server.

Finds what to back up on its own: every folder or Docker volume a container
mounts read-write is that app's state (settings, databases, uploads). The media
drive and read-only mounts are left out. A new app is backed up the night after
it starts, without anyone remembering to add it.

Backups go to a restic repository on the backup drive. restic encrypts
everything, and stores each file once, so a nightly backup only adds what
changed. Apps that keep a SQLite database are paused for the minute the backup
takes, so their database is never copied halfway through a write; everything
else keeps running.

After each run it writes /var/lib/stx-backup/status.json, which stx-monitor
reads for its Backups panel and its alerts.

    stx-backup run                    back up now (the timer runs this at 3 AM)
    stx-backup snapshots              list the backups
    stx-backup restore NAME [--snapshot ID] [--to DIR]
                                      copy an app's files out of a backup
    stx-backup init                   create the repository on the backup drive

Settings live in /etc/stx-backup/env, the repository password in
/etc/stx-backup/password. Uses only the Python standard library and restic.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

VERSION = 1
ENV_FILE = Path("/etc/stx-backup/env")
STATE_DIR = Path("/var/lib/stx-backup")
STATUS_FILE = STATE_DIR / "status.json"
PAUSED_FILE = STATE_DIR / "paused.json"
LOCK_FILE = STATE_DIR / "lock"

DEFAULTS = {
    "STX_BACKUP_MOUNT": "/mnt/backup",          # where the backup drive is mounted
    "STX_BACKUP_REPO": "/mnt/backup/stx-restic",
    "RESTIC_PASSWORD_FILE": "/etc/stx-backup/password",
    "STX_BACKUP_SKIP_PATHS": "/srv/data",       # the media drive: re-downloadable, far too big
    "STX_BACKUP_NO_PAUSE": "adguard",           # never pause these (the TV needs DNS)
    "STX_BACKUP_KEEP_DAILY": "7",
    "STX_BACKUP_KEEP_WEEKLY": "4",
    "STX_BACKUP_KEEP_MONTHLY": "6",
}

# Never app data: the OS, devices, and stx-monitor's read-only view of the host.
SYSTEM_PATHS = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run",
                "/sbin", "/sys", "/usr", "/var/run", "/var/lib/docker/containers")
# Caches and transcodes are rebuilt by the apps on their own.
EXCLUDES = ["**/cache", "**/Cache", "**/.cache", "**/transcodes", "**/MediaCover",
            "**/logs", "**/*.log", "**/*.log.*"]
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def log(msg: str) -> None:
    print(msg, flush=True)  # systemd puts this in the journal


def load_settings() -> dict[str, str]:
    cfg = dict(DEFAULTS)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def restic_env(cfg: dict) -> dict:
    return {**os.environ, "RESTIC_REPOSITORY": cfg["STX_BACKUP_REPO"],
            "RESTIC_PASSWORD_FILE": cfg["RESTIC_PASSWORD_FILE"],
            "RESTIC_CACHE_DIR": str(STATE_DIR / "cache")}


def restic(cfg: dict, *args: str, timeout: float = 6 * 3600) -> subprocess.CompletedProcess:
    return subprocess.run(["restic", *args], env=restic_env(cfg), capture_output=True,
                          text=True, timeout=timeout, check=False)


def docker(*args: str) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def under(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


# ------------------------------------------------------------------ discovery
def discover(cfg: dict) -> tuple[list[dict], list[dict]]:
    """Every container's read-write data folders, and what was skipped and why."""
    names = [n for n in docker("ps", "-a", "--format", "{{.Names}}").split() if n]
    if not names:
        return [], []
    skip_paths = [p for p in cfg["STX_BACKUP_SKIP_PATHS"].split(":") if p]
    skip_paths.append(cfg["STX_BACKUP_MOUNT"])
    apps, skipped, seen = [], [], set()
    for info in json.loads(docker("inspect", "--type", "container", *names)):
        name = info["Name"].lstrip("/")
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get("stx.backup") == "false":
            skipped.append({"name": name, "reason": "turned off with the stx.backup label"})
            continue
        paths = []
        for m in info.get("Mounts") or []:
            src = m.get("Source") or ""
            if m.get("Type") not in ("bind", "volume") or not m.get("RW") or not src:
                continue
            if src == "/" or under(src, SYSTEM_PATHS[1:]) or not os.path.isdir(src):
                continue
            if under(src, skip_paths):
                skipped.append({"name": name, "path": src, "reason": "on the media or backup drive"})
                continue
            paths.append(src)
        paths = sorted(set(paths) - seen)
        if not paths:
            continue
        seen.update(paths)
        apps.append({
            "name": name,
            "running": info.get("State", {}).get("Running", False),
            "paths": paths,
            "sqlite": has_sqlite(paths),
            "pause": labels.get("stx.backup.pause") != "false"
                     and name not in cfg["STX_BACKUP_NO_PAUSE"].split(","),
        })
    # A folder inside another app's folder is already covered.
    all_paths = [p for a in apps for p in a["paths"]]
    for a in apps:
        a["paths"] = [p for p in a["paths"] if not any(p != q and under(p, [q]) for q in all_paths)]
    return [a for a in apps if a["paths"]], skipped


SKIP_DIRS = {"cache", ".cache", "transcodes", "mediacover", "logs"}  # the folder side of EXCLUDES


def walk(root: str):
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        yield dirpath, files


def has_sqlite(paths: list[str]) -> bool:
    return any(f.endswith(SQLITE_SUFFIXES) for root in paths for _, files in walk(root) for f in files)


def folder_size(paths: list[str]) -> int:
    """Roughly what gets backed up: the same folders restic leaves out are left out here."""
    total = 0
    for root in paths:
        for dirpath, files in walk(root):
            for f in files:
                if ".log" in f:
                    continue
                try:
                    total += os.lstat(os.path.join(dirpath, f)).st_size
                except OSError:
                    pass
    return total


# ------------------------------------------------------------------ pausing
def pause(names: list[str]) -> list[str]:
    paused = []
    PAUSED_FILE.write_text(json.dumps(names))  # written first, so a crash can still undo it
    for n in names:
        try:
            docker("pause", n)
            paused.append(n)
        except RuntimeError as exc:
            log(f"could not pause {n}: {exc}")
    return paused


def unpause_all() -> None:
    """Resume everything a backup paused, including after a crash or power cut."""
    if not PAUSED_FILE.exists():
        return
    for n in json.loads(PAUSED_FILE.read_text() or "[]"):
        try:
            docker("unpause", n)
        except RuntimeError:
            pass  # not paused (or gone): nothing to undo
    PAUSED_FILE.unlink(missing_ok=True)


# ------------------------------------------------------------------ status
def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def write_status(status: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=1))
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS_FILE)


def drive_ready(cfg: dict) -> str | None:
    mount = cfg["STX_BACKUP_MOUNT"]
    if not os.path.ismount(mount):
        return f"The backup drive isn't mounted at {mount}. Is the external SSD plugged in?"
    if not Path(cfg["RESTIC_PASSWORD_FILE"]).exists():
        return f"No repository password at {cfg['RESTIC_PASSWORD_FILE']}. Run the installer again."
    if not (Path(cfg["STX_BACKUP_REPO"]) / "config").exists():
        return f"No backup repository at {cfg['STX_BACKUP_REPO']} yet. Run: sudo stx-backup init"
    return None


def repo_numbers(cfg: dict) -> dict:
    out = {}
    snaps = restic(cfg, "snapshots", "--json", "--tag", "stx", timeout=600)
    if snaps.returncode == 0:
        items = json.loads(snaps.stdout or "[]")
        out["snapshots"] = len(items)
        out["oldest"] = items[0]["time"] if items else None
    stats = restic(cfg, "stats", "--mode", "raw-data", "--json", timeout=1800)
    if stats.returncode == 0:
        out["repo_size"] = json.loads(stats.stdout).get("total_size")
    st = os.statvfs(cfg["STX_BACKUP_MOUNT"])
    out["drive_free"] = st.f_bavail * st.f_frsize
    out["drive_total"] = st.f_blocks * st.f_frsize
    return out


# ------------------------------------------------------------------ commands
def cmd_run(cfg: dict) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("A backup is already running.")
        return 0
    unpause_all()  # leftovers from an interrupted run

    prev = read_status()
    status = {
        "version": VERSION, "host": socket.gethostname(), "repo": cfg["STX_BACKUP_REPO"],
        "running": True, "started": time.time(), "finished": None, "ok": None, "error": None,
        "last_ok": prev.get("last_ok"), "last_ok_summary": prev.get("last_ok_summary"),
        "check": prev.get("check"), "apps": prev.get("apps", []), "skipped": [],
    }
    write_status(status)

    def fail(msg: str) -> int:
        log(msg)
        status.update(running=False, finished=time.time(), ok=False, error=msg)
        write_status(status)
        return 1

    problem = drive_ready(cfg)
    if problem:
        return fail(problem)
    try:
        apps, skipped = discover(cfg)
    except Exception as exc:  # noqa: BLE001
        return fail(f"Couldn't list the apps: {exc}")
    if not apps:
        return fail("Found no app data to back up. Is Docker running?")
    status["skipped"] = skipped
    to_pause = [a["name"] for a in apps if a["running"] and a["sqlite"] and a["pause"]]
    status["paused"] = to_pause
    write_status(status)

    paths = sorted({p for a in apps for p in a["paths"]})
    args = ["backup", "--json", "--tag", "stx", "--host", status["host"], "--exclude-caches"]
    for pattern in EXCLUDES:
        args += ["--exclude", pattern]
    log(f"Backing up {len(apps)} apps ({len(paths)} folders), pausing {', '.join(to_pause) or 'nothing'}")
    t0 = time.time()
    try:
        pause(to_pause)
        result = restic(cfg, *args, *paths)
    finally:
        unpause_all()
    paused_for = round(time.time() - t0)

    summary = None
    for line in result.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("message_type") == "summary":
            summary = msg
    # restic exits 3 when a few files couldn't be read but the snapshot was saved.
    if result.returncode not in (0, 3) or not summary:
        err = (result.stderr.strip().splitlines() or ["restic failed"])[-1]
        return fail(f"Backup failed: {err[:300]}")

    for a in apps:
        a["size"] = folder_size(a["paths"])
        del a["running"]
    warnings = [ln for ln in result.stderr.splitlines() if ln.strip()][:5] if result.returncode == 3 else []

    forget = restic(cfg, "forget", "--tag", "stx", "--prune",
                    "--keep-daily", cfg["STX_BACKUP_KEEP_DAILY"],
                    "--keep-weekly", cfg["STX_BACKUP_KEEP_WEEKLY"],
                    "--keep-monthly", cfg["STX_BACKUP_KEEP_MONTHLY"])
    if forget.returncode != 0:
        warnings.append("Couldn't clear out old backups: " + (forget.stderr.strip().splitlines() or ["?"])[-1])

    # Once a week, read back a slice of the stored data to prove it's intact.
    check = status.get("check")
    if not check or time.time() - check.get("time", 0) > 6.5 * 86400:
        c = restic(cfg, "check", "--read-data-subset", "5%")
        check = {"time": time.time(), "ok": c.returncode == 0,
                 "detail": "" if c.returncode == 0 else (c.stderr.strip().splitlines() or ["?"])[-1][:300]}
        if not check["ok"]:
            warnings.append("The weekly integrity check found a problem: " + check["detail"])

    done = time.time()
    status.update(
        running=False, finished=done, ok=True, error=None, last_ok=done, apps=apps, check=check,
        warnings=warnings, paused_seconds=paused_for if to_pause else 0,
        last_ok_summary={
            "snapshot": summary.get("snapshot_id"),
            "files_new": summary.get("files_new"),
            "files_changed": summary.get("files_changed"),
            "data_added": summary.get("data_added"),
            "total_bytes": summary.get("total_bytes_processed"),
            "duration": round(done - status["started"]),
        },
        **repo_numbers(cfg),
    )
    write_status(status)
    s = status["last_ok_summary"]
    log(f"Done: {s['files_new']} new and {s['files_changed']} changed files, "
        f"{s['data_added']} bytes added, snapshot {s['snapshot'][:8]}")
    return 0


def cmd_init(cfg: dict) -> int:
    mount = cfg["STX_BACKUP_MOUNT"]
    if not os.path.ismount(mount):
        log(f"The backup drive isn't mounted at {mount}. Mount it first (see the README).")
        return 1
    if (Path(cfg["STX_BACKUP_REPO"]) / "config").exists():
        log(f"There's already a repository at {cfg['STX_BACKUP_REPO']}.")
        return 0
    r = restic(cfg, "init")
    log(r.stdout.strip() or r.stderr.strip())
    return r.returncode


def cmd_snapshots(cfg: dict) -> int:
    problem = drive_ready(cfg)
    if problem:
        log(problem)
        return 1
    r = restic(cfg, "snapshots", "--tag", "stx", "--compact")
    print(r.stdout or r.stderr)
    return r.returncode


def cmd_restore(cfg: dict, name: str, snapshot: str, to: str | None) -> int:
    problem = drive_ready(cfg)
    if problem:
        log(problem)
        return 1
    apps = {a["name"]: a for a in read_status().get("apps", [])}
    if name in apps:
        includes = apps[name]["paths"]
    elif name.startswith("/"):
        includes = [name]
    else:
        log(f"No app called {name} in the last backup. Apps: {', '.join(sorted(apps)) or 'none yet'}")
        return 1
    target = to or str(STATE_DIR / "restore" / f"{name.strip('/').replace('/', '_')}-{time.strftime('%Y%m%d-%H%M%S')}")
    args = ["restore", snapshot, "--target", target, "--tag", "stx"]
    for p in includes:
        args += ["--include", p]
    r = restic(cfg, *args)
    if r.returncode != 0:
        log(r.stderr.strip())
        return r.returncode
    log(f"Restored to {target}")
    log("The files keep their full paths inside that folder. Stop the app, copy them back, start it:")
    for p in includes:
        log(f"  sudo rsync -a --delete {target}{p}/ {p}/")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="stx-backup", description="Back up every app's data on this server.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("init")
    sub.add_parser("snapshots")
    r = sub.add_parser("restore")
    r.add_argument("name", help="an app name from the last backup, or an absolute path")
    r.add_argument("--snapshot", default="latest")
    r.add_argument("--to", help="folder to restore into (default: /var/lib/stx-backup/restore/...)")
    args = ap.parse_args()
    if os.geteuid() != 0:
        log("Run this with sudo.")
        return 1
    cfg = load_settings()
    if args.cmd == "run":
        return cmd_run(cfg)
    if args.cmd == "init":
        return cmd_init(cfg)
    if args.cmd == "snapshots":
        return cmd_snapshots(cfg)
    return cmd_restore(cfg, args.name, args.snapshot, args.to)


if __name__ == "__main__":
    sys.exit(main())
