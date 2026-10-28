"""Everything stx-monitor keeps on disk, in one SQLite file (/data/monitor.db).

SQLite is a whole SQL database in a single file, with no server to run. WAL
mode ("write-ahead log") lets the web requests read while the collector writes.
Every table is small, so plain synchronous calls behind one lock are fast
enough to make straight from the event loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    container TEXT,
    name TEXT,
    report INTEGER
);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE TABLE IF NOT EXISTS failure_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    data TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reports_time ON failure_reports(time);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    finished REAL,
    actor TEXT NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS actions_started ON actions(started);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    last_seen REAL NOT NULL,
    remember INTEGER NOT NULL,
    method TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS passkeys (
    id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL,
    rp_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created REAL NOT NULL,
    last_used REAL
);
CREATE TABLE IF NOT EXISTS sign_ins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    ok INTEGER NOT NULL,
    method TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sign_ins_time ON sign_ins(time);
CREATE TABLE IF NOT EXISTS history (
    t INTEGER PRIMARY KEY,
    cpu REAL, mem REAL, temp REAL, rx REAL, tx REAL, watts REAL
);
"""

KEEP_EVENTS = 5000
KEEP_REPORTS = 300
KEEP_ACTIONS = 2000
KEEP_SIGN_INS = 2000
HISTORY_FIELDS = ("cpu", "mem", "temp", "rx", "tx", "watts")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _exec(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    def _all(self, sql: str, args: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple | list = ()) -> dict | None:
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- settings
    def get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def delete_setting(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key = ?", (key,))

    # ------------------------------------------------------------------ events
    def add_event(self, ev: dict) -> dict:
        cur = self._exec(
            "INSERT INTO events(time, kind, level, title, detail, container, name, report) VALUES(?,?,?,?,?,?,?,?)",
            (ev.get("time") or time.time(), ev["kind"], ev["level"], ev["title"], ev.get("detail") or "",
             ev.get("container"), ev.get("name"), ev.get("report")),
        )
        out = {**ev, "id": cur.lastrowid, "time": ev.get("time") or time.time()}
        if cur.lastrowid % 200 == 0:
            self._exec("DELETE FROM events WHERE id <= ?", (cur.lastrowid - KEEP_EVENTS,))
        return out

    def recent_events(self, limit: int = 200) -> list[dict]:
        rows = self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [_clean(r) for r in reversed(rows)]

    def events_between(self, start: float, end: float, kinds: tuple[str, ...] = ()) -> list[dict]:
        sql = "SELECT * FROM events WHERE time BETWEEN ? AND ?"
        args: list = [start, end]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        return [_clean(r) for r in self._all(sql + " ORDER BY time", args)]

    # ----------------------------------------------------------------- reports
    def add_report(self, kind: str, target: str, title: str, summary: str, data: dict, when: float | None = None) -> dict:
        when = when or time.time()
        cur = self._exec(
            "INSERT INTO failure_reports(time, kind, target, title, summary, data) VALUES(?,?,?,?,?,?)",
            (when, kind, target, title, summary, json.dumps(data)),
        )
        self._exec("DELETE FROM failure_reports WHERE id <= ?", (cur.lastrowid - KEEP_REPORTS,))
        return self.get_report(cur.lastrowid)

    def list_reports(self, limit: int = 50) -> list[dict]:
        rows = self._all(
            "SELECT id, time, kind, target, title, summary, seen FROM failure_reports ORDER BY time DESC LIMIT ?",
            (limit,),
        )
        return [{**r, "seen": bool(r["seen"])} for r in rows]

    def get_report(self, report_id: int) -> dict | None:
        row = self._one("SELECT * FROM failure_reports WHERE id = ?", (report_id,))
        if not row:
            return None
        row["data"] = json.loads(row["data"])
        row["seen"] = bool(row["seen"])
        return row

    def mark_report_seen(self, report_id: int) -> None:
        self._exec("UPDATE failure_reports SET seen = 1 WHERE id = ?", (report_id,))

    def unseen_reports(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM failure_reports WHERE seen = 0")
        return row["n"] if row else 0

    def count_reports_since(self, target: str, since: float) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM failure_reports WHERE target = ? AND time >= ?", (target, since))
        return row["n"] if row else 0

    # ----------------------------------------------------------------- actions
    def add_action(self, actor: str, device: str, action: str, target: str, status: str, detail: str = "") -> dict:
        now = time.time()
        finished = None if status == "running" else now
        cur = self._exec(
            "INSERT INTO actions(started, finished, actor, device, action, target, status, detail) VALUES(?,?,?,?,?,?,?,?)",
            (now, finished, actor, device, action, target, status, detail),
        )
        self._exec("DELETE FROM actions WHERE id <= ?", (cur.lastrowid - KEEP_ACTIONS,))
        return self.get_action(cur.lastrowid)

    def finish_action(self, action_id: int, status: str, detail: str = "") -> dict:
        self._exec("UPDATE actions SET finished = ?, status = ?, detail = ? WHERE id = ?",
                   (time.time(), status, detail, action_id))
        return self.get_action(action_id)

    def get_action(self, action_id: int) -> dict | None:
        return self._one("SELECT * FROM actions WHERE id = ?", (action_id,))

    def list_actions(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))

    def fail_running_actions(self, detail: str) -> None:
        """Actions still "running" when we start were cut off by a restart."""
        self._exec("UPDATE actions SET status = 'unknown', finished = ?, detail = ? WHERE status = 'running'",
                   (time.time(), detail))

    # ---------------------------------------------------------------- sessions
    def add_session(self, row: dict) -> None:
        self._exec(
            "INSERT INTO sessions(id, csrf, created, expires, last_seen, remember, method, ip, device) VALUES(?,?,?,?,?,?,?,?,?)",
            (row["id"], row["csrf"], row["created"], row["expires"], row["last_seen"], int(row["remember"]),
             row["method"], row.get("ip", ""), row.get("device", "")),
        )

    def get_session(self, session_id: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def touch_session(self, session_id: str, now: float) -> None:
        self._exec("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, session_id))

    def delete_session(self, session_id: str) -> None:
        self._exec("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_other_sessions(self, keep_id: str) -> int:
        return self._exec("DELETE FROM sessions WHERE id != ?", (keep_id,)).rowcount

    def delete_all_sessions(self) -> int:
        return self._exec("DELETE FROM sessions").rowcount

    def list_sessions(self) -> list[dict]:
        self._exec("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        return self._all("SELECT * FROM sessions ORDER BY last_seen DESC")

    # ---------------------------------------------------------------- passkeys
    def add_passkey(self, cred_id: str, public_key: bytes, sign_count: int, rp_id: str, name: str) -> None:
        self._exec(
            "INSERT INTO passkeys(id, public_key, sign_count, rp_id, name, created) VALUES(?,?,?,?,?,?)",
            (cred_id, public_key, sign_count, rp_id, name, time.time()),
        )

    def get_passkey(self, cred_id: str) -> dict | None:
        return self._one("SELECT * FROM passkeys WHERE id = ?", (cred_id,))

    def list_passkeys(self) -> list[dict]:
        return self._all("SELECT id, rp_id, name, created, last_used, sign_count FROM passkeys ORDER BY created")

    def use_passkey(self, cred_id: str, sign_count: int) -> None:
        self._exec("UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?", (sign_count, time.time(), cred_id))

    def delete_passkey(self, cred_id: str) -> int:
        return self._exec("DELETE FROM passkeys WHERE id = ?", (cred_id,)).rowcount

    def delete_all_passkeys(self) -> int:
        return self._exec("DELETE FROM passkeys").rowcount

    # ---------------------------------------------------------------- sign-ins
    def add_sign_in(self, ok: bool, method: str, ip: str, device: str, detail: str = "") -> None:
        cur = self._exec(
            "INSERT INTO sign_ins(time, ok, method, ip, device, detail) VALUES(?,?,?,?,?,?)",
            (time.time(), int(ok), method, ip, device, detail),
        )
        self._exec("DELETE FROM sign_ins WHERE id <= ?", (cur.lastrowid - KEEP_SIGN_INS,))

    def recent_sign_ins(self, limit: int = 20) -> list[dict]:
        return self._all("SELECT * FROM sign_ins ORDER BY id DESC LIMIT ?", (limit,))

    def clear_failed_sign_ins(self) -> None:
        """Used by the `unlock` command: forget failures so the lockout ends now."""
        self._exec("DELETE FROM sign_ins WHERE ok = 0")

    # ----------------------------------------------------------------- history
    def save_history(self, rows: list[list]) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT OR REPLACE INTO history(t, cpu, mem, temp, rx, tx, watts) VALUES(?,?,?,?,?,?,?)",
                [r[:7] for r in rows],
            )
            self.db.execute("DELETE FROM history WHERE t < ?", (time.time() - 24 * 3600,))
            self.db.execute("COMMIT")

    def load_history(self, since: float) -> list[list]:
        rows = self._all("SELECT * FROM history WHERE t >= ? ORDER BY t", (since,))
        return [[r["t"], *(r[f] for f in HISTORY_FIELDS)] for r in rows]


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None or k in ("detail",)}
