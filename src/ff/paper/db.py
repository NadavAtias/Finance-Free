"""SQLite ledger for paper trading: one file, append-only journal.

SQLite instead of the Postgres planned in the architecture doc: one user, one
writer (the daily loop) and a read-mostly API fit a single file, and backups are
a file copy. The SQL is plain enough to move to Postgres if that ever changes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sleeve_state (
    sleeve TEXT PRIMARY KEY,
    portfolio TEXT NOT NULL,
    last_session TEXT NOT NULL,
    session_index INTEGER NOT NULL,
    max_order_id INTEGER NOT NULL,
    blob BLOB NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    sleeve TEXT NOT NULL,
    portfolio TEXT NOT NULL,
    date TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    gross REAL NOT NULL,
    PRIMARY KEY (sleeve, date)
);
CREATE TABLE IF NOT EXISTS positions (
    sleeve TEXT NOT NULL,
    portfolio TEXT NOT NULL,
    ticker TEXT NOT NULL,
    shares INTEGER NOT NULL,
    entry REAL NOT NULL,
    last REAL NOT NULL,
    prev REAL,
    stop REAL,
    opened TEXT,
    PRIMARY KEY (sleeve, ticker)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve TEXT NOT NULL,
    portfolio TEXT NOT NULL,
    ticker TEXT NOT NULL,
    entry_date TEXT,
    exit_date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY,
    sleeve TEXT NOT NULL,
    portfolio TEXT NOT NULL,
    session TEXT NOT NULL,
    created TEXT NOT NULL,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    shares INTEGER NOT NULL,
    entry REAL NOT NULL,
    stop REAL,
    take_profit REAL,
    confidence INTEGER NOT NULL,
    horizon TEXT NOT NULL,
    signals TEXT NOT NULL,
    risk TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    handled_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    detail TEXT NOT NULL,
    extra TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class PaperDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        with self.connect() as c:
            cur = c.execute(sql, args)
            return int(cur.lastrowid or 0)

    def backup(self, keep: int = 30) -> Path:
        """Consistent copy (SQLite online backup) into ``backups/``; keeps the newest ``keep``."""
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        dest = folder / f"{self.path.stem}-{stamp}.sqlite"
        src, dst = sqlite3.connect(self.path), sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        for old in sorted(folder.glob(f"{self.path.stem}-*.sqlite"))[:-keep]:
            old.unlink()
        return dest

    # --- key/value (settings, snapshots) ------------------------------------
    def get_json(self, key: str, default: Any = None) -> Any:
        r = self.rows("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set_json(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # --- journal and alerts ---------------------------------------------------
    def event(
        self,
        ts: str,
        type_: str,
        detail: str,
        ticker: str = "—",
        strategy: str = "—",
        **extra: Any,
    ) -> None:
        self.execute(
            "INSERT INTO events(ts, type, ticker, strategy, detail, extra) VALUES(?,?,?,?,?,?)",
            (ts, type_, ticker, strategy, detail, json.dumps(extra) if extra else None),
        )

    def alert(self, ts: str, severity: str, title: str, body: str) -> None:
        self.execute(
            "INSERT INTO alerts(ts, severity, title, body) VALUES(?,?,?,?)",
            (ts, severity, title, body),
        )
