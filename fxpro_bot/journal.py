"""Durable order deduplication and audit log; never stores credentials."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .domain import utc_now


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                account TEXT NOT NULL, symbol TEXT NOT NULL, bar_time TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT, PRIMARY KEY(account, symbol, bar_time)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT, kind TEXT, message TEXT);
        """)
        self.db.commit()

    def reserve(self, account: str, symbol: str, bar_time: str) -> bool:
        with self.lock, self.db:
            result = self.db.execute("INSERT OR IGNORE INTO signals VALUES (?, ?, ?, 'reserved', NULL)", (account, symbol, bar_time))
            return result.rowcount == 1

    def finish(self, account: str, symbol: str, bar_time: str, status: str, detail: str):
        with self.lock, self.db:
            self.db.execute("UPDATE signals SET status=?, detail=? WHERE account=? AND symbol=? AND bar_time=?", (status, detail, account, symbol, bar_time))

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def event(self, kind: str, message: str):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(time, kind, message) VALUES (?, ?, ?)", (utc_now().isoformat(), kind, message))

    def events(self, count: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT time,kind,message FROM events ORDER BY id DESC LIMIT ?", (count,)).fetchall()
        return [dict(zip(["time", "kind", "message"], row)) for row in rows]

    def close(self):
        with self.lock:
            self.db.close()
