"""SQLite store: caches, sent notices, last plan set, last run."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS notified (k TEXT PRIMARY KEY, ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS overrides (k TEXT PRIMARY KEY, mode TEXT, target INTEGER,
                                                      title TEXT, start TEXT, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (h TEXT PRIMARY KEY, expires REAL NOT NULL);
                """
            )
            self._db.commit()

    # --- kv
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
            self._db.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE k=?", (key,))
            self._db.commit()

    # --- cache (geocoding, routes)
    def cache_get(self, key: str, max_age_days: float = 90) -> Optional[Any]:
        with self._lock:
            row = self._db.execute("SELECT v, ts FROM cache WHERE k=?", (key,)).fetchone()
        if not row or time.time() - row[1] > max_age_days * 86400:
            return None
        return json.loads(row[0])

    def cache_put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value), time.time()))
            self._db.commit()

    # --- Per-event overrides
    def overrides(self) -> dict:
        from .models import Override
        with self._lock:
            rows = self._db.execute("SELECT k, mode, target FROM overrides").fetchall()
        return {k: Override(mode, target) for k, mode, target in rows}

    def set_override(self, key: str, mode, target, title: str = "", start: str = "") -> None:
        with self._lock:
            if mode is None and target is None:
                self._db.execute("DELETE FROM overrides WHERE k=?", (key,))
            else:
                self._db.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?,?,?)",
                                 (key, mode, target, title[:200], start, time.time()))
            self._db.commit()

    def prune_overrides(self, before_iso: str) -> None:
        """Remove overrides of past events (start is ISO UTC and therefore sortable)."""
        with self._lock:
            self._db.execute("DELETE FROM overrides WHERE start<>'' AND start<?", (before_iso,))
            self._db.commit()

    # --- Sessions (only the token hash is stored)
    def session_add(self, token_hash: str, expires: float) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (token_hash, expires))
            self._db.commit()

    def session_valid(self, token_hash: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT expires FROM sessions WHERE h=?", (token_hash,)).fetchone()
        return bool(row) and row[0] > time.time()

    def session_delete(self, token_hash: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE h=?", (token_hash,))
            self._db.commit()

    def sessions_prune(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            self._db.commit()

    # --- Send notices only once
    def was_notified(self, key: str, ttl_hours: Optional[float]) -> bool:
        with self._lock:
            row = self._db.execute("SELECT ts FROM notified WHERE k=?", (key,)).fetchone()
        if not row:
            return False
        return ttl_hours is None or time.time() - row[0] < ttl_hours * 3600

    def mark_notified(self, key: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO notified VALUES (?,?)", (key, time.time()))
            self._db.commit()

    def prune(self, max_age_days: float = 60) -> None:
        cutoff = time.time() - max_age_days * 86400
        with self._lock:
            self._db.execute("DELETE FROM notified WHERE ts<?", (cutoff,))
            self._db.commit()
