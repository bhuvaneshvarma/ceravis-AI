from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path


logger = logging.getLogger("storage")


class SqliteStore:
    """
    Single-writer SQLite wrapper.

    On Jetson we keep this tiny + WAL-mode for low fsync pressure.
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        # A writer that finds the file locked waits instead of raising — the
        # outbox writes from the event thread while the sender updates rows.
        self._conn.execute("PRAGMA busy_timeout=5000")
        logger.info("SQLite store: %s", self._path)

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Run one statement. Returns the new rowid for an INSERT (0 otherwise),
        which is what gives the outbox its FIFO sequence number."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or 0

    def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
