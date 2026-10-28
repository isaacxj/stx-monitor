#!/usr/bin/env python3
"""stx-helper: the few root-only jobs stx-monitor may ask the host to do.

stx-monitor runs in a container and should never be root on the host. A few
jobs genuinely need the host, though: shutting down, rebooting, restarting
Docker, cleaning up images, and pulling and recreating a compose service. This
small service runs those jobs, and nothing else, for whoever connects to its
Unix socket at /run/stx-helper/helper.sock. Only root can open that socket.

Protocol: the client sends one JSON line, {"cmd": "...", "args": {...}}. The
helper answers with JSON lines: {"type": "log", "text": ...} while working,
then one {"type": "done", "ok": true|false, "detail": "..."}.

Installed by scripts/install-helper.sh, run by systemd (stx-helper.service).
Uses only the Python standard library.
"""

import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

VERSION = 1
SOCKET_DIR = "/run/stx-helper"
SOCKET_PATH = os.path.join(SOCKET_DIR, "helper.sock")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PROTECTED = {"stx-monitor", "stx-docker-proxy", "stx-docker-control"}
BUSY = threading.Lock()


def log(msg):
    print(msg, file=sys.stderr, flush=True)  # systemd puts this in the journal


def run(cmd, emit, timeout=900):
    """Run a command, passing each output line to emit. Returns (exit code, last lines)."""
    emit(" ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    deadline = time.monotonic() + timeout
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            emit(line)
            tail = (tail + [line])[-20:]
        if time.monotonic() > deadline:
            proc.kill()
            emit("Timed out")
            break
    return proc.wait(), tail


def later(cmd, delay=2.0):
    """Run cmd after answering, so the caller hears "ok" before the machine goes down."""
    threading.Timer(delay, lambda: subprocess.run(cmd, check=False)).start()


def cmd_ping(args, emit):
    return True, f"stx-helper {VERSION}"


def cmd_poweroff(args, emit):
    later(["systemctl", "poweroff"])
    return True, "Shutting down"


def cmd_reboot(args, emit):
    later(["systemctl", "reboot"])
    return True, "Rebooting"


def cmd_restart_docker(args, emit):
    later(["systemctl", "restart", "docker"], delay=1.0)
    return True, "Restarting Docker"


def cmd_prune_images(args, emit):
    code, tail = run(["docker", "image", "prune", "--all", "--force"], emit, timeout=600)
    freed = next((x.split(":", 1)[1].strip() for x in tail if x.startswith("Total reclaimed space")), None)
    return code == 0, f"Freed {freed}" if freed else ("Done" if code == 0 else "\n".join(tail[-3:]))


def _inspect(name):
    out = subprocess.run(["docker", "inspect", "--type", "container", name],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return None
    data = json.loads(out.stdout)
    return data[0] if data else None


def cmd_update(args, emit):
    name = str(args.get("container", ""))
    if not NAME_RE.match(name):
        return False, "Bad container name"
    info = _inspect(name)
    if not info:
        return False, f"No container named {name}"
    labels = info.get("Config", {}).get("Labels") or {}
    if name in PROTECTED or labels.get("stx.protected") == "true":
        return False, f"{name} is protected; update it from the server"
    project = labels.get("com.docker.compose.project")
    workdir = labels.get("com.docker.compose.project.working_dir")
    files = [f for f in (labels.get("com.docker.compose.project.config_files") or "").split(",") if f]
    service = labels.get("com.docker.compose.service")
    if not (project and workdir and files and service):
        return False, f"{name} was not started by docker compose, so it can't be recreated from here"
    if not all(os.path.isabs(p) and os.path.exists(p) for p in [workdir, *files]):
        return False, f"Its compose files are not where Docker last saw them ({workdir})"
    base = ["docker", "compose", "-p", project, "--project-directory", workdir]
    for f in files:
        base += ["-f", f]
    before = info.get("Image")
    code, tail = run(base + ["pull", service], emit)
    if code != 0:
        return False, "Pull failed: " + " ".join(tail[-2:])
    code, tail = run(base + ["up", "-d", "--no-deps", service], emit)
    if code != 0:
        return False, "Recreate failed: " + " ".join(tail[-2:])
    after = (_inspect(name) or {}).get("Image")
    return True, "Updated to a new image" if after and after != before else "Already up to date"


COMMANDS = {
    "ping": (cmd_ping, False),
    "poweroff": (cmd_poweroff, True),
    "reboot": (cmd_reboot, True),
    "restart-docker": (cmd_restart_docker, True),
    "prune-images": (cmd_prune_images, True),
    "update": (cmd_update, True),
}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        if uid != 0:
            self.reply({"type": "done", "ok": False, "detail": "Only root may use stx-helper"})
            return
        try:
            req = json.loads(self.rfile.readline(4096))
            cmd, args = req.get("cmd"), req.get("args") or {}
        except (ValueError, AttributeError):
            self.reply({"type": "done", "ok": False, "detail": "Bad request"})
            return
        if cmd not in COMMANDS:
            self.reply({"type": "done", "ok": False, "detail": f"Unknown command {cmd!r}"})
            return
        fn, exclusive = COMMANDS[cmd]
        if exclusive and not BUSY.acquire(blocking=False):
            self.reply({"type": "done", "ok": False, "detail": "Busy with another job, try again in a moment"})
            return
        log(f"{cmd} {json.dumps(args)} from pid {pid}")
        try:
            ok, detail = fn(args, lambda text: self.reply({"type": "log", "text": text}))
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        finally:
            if exclusive:
                BUSY.release()
        log(f"{cmd} -> {'ok' if ok else 'failed'}: {detail}")
        self.reply({"type": "done", "ok": ok, "detail": detail})

    def reply(self, msg):
        try:
            self.wfile.write((json.dumps(msg) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass  # the client went away; the job still finishes


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    os.makedirs(SOCKET_DIR, mode=0o700, exist_ok=True)
    os.chmod(SOCKET_DIR, 0o700)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    old = os.umask(0o177)
    server = Server(SOCKET_PATH, Handler)
    os.umask(old)
    log(f"stx-helper {VERSION} listening on {SOCKET_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
