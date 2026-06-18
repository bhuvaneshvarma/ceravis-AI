from __future__ import annotations

from datetime import datetime, timezone

from schemas.event import Event
from storage.sqlite_store import SqliteStore


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL,
    camera_id     TEXT NOT NULL,
    room_name     TEXT NOT NULL,
    zone_name     TEXT,
    recipient_id  TEXT,
    timestamp     TEXT NOT NULL,
    snapshot_path TEXT,
    video_path    TEXT,
    synced        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_sync ON events(synced);
"""

# Columns added after the original schema — applied on existing DBs too.
_MIGRATIONS = [
    ("track_id", "INTEGER"),
    ("severity", "TEXT"),
    ("title", "TEXT"),
    ("message", "TEXT"),
    ("acknowledged", "INTEGER NOT NULL DEFAULT 0"),
    ("ack_by", "TEXT"),
    ("ack_at", "TEXT"),
]


class EventStore:
    """Persists Events for offline buffering until cloud sync."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._store.execute(stmt)
        self._migrate()

    def _migrate(self) -> None:
        existing = {row[1] for row in
                    self._store.fetchall("PRAGMA table_info(events)")}
        for col, decl in _MIGRATIONS:
            if col not in existing:
                self._store.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")

    # ---- write -------------------------------------------------------
    def save_event(self, event: Event) -> None:
        self._store.execute(
            """INSERT OR REPLACE INTO events
               (event_id, event_type, camera_id, room_name, zone_name,
                recipient_id, timestamp, snapshot_path, video_path,
                track_id, severity, title, message,
                acknowledged, ack_by, ack_at, synced)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                event.event_id, event.event_type, event.camera_id,
                event.room_name, event.zone_name, event.recipient_id,
                event.timestamp, event.snapshot_path, event.video_path,
                event.track_id, event.severity, event.title, event.message,
                1 if event.acknowledged else 0, event.ack_by, event.ack_at,
            ),
        )

    def acknowledge(self, event_id: str, ack_by: str | None = None) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        self._store.execute(
            "UPDATE events SET acknowledged=1, ack_by=?, ack_at=? WHERE event_id=?",
            (ack_by, now, event_id),
        )
        return True

    # ---- read --------------------------------------------------------
    _COLS = ("event_id", "event_type", "camera_id", "room_name", "zone_name",
             "recipient_id", "timestamp", "snapshot_path", "track_id",
             "severity", "title", "message", "acknowledged", "synced")

    def recent(self, limit: int = 50,
               camera_id: str | None = None) -> list[dict]:
        """Most recent events, newest first — consumed by the monitor UI."""
        sql = "SELECT " + ", ".join(self._COLS) + " FROM events"
        params: tuple = ()
        if camera_id:
            sql += " WHERE camera_id=?"
            params = (camera_id,)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params += (int(limit),)
        return [dict(zip(self._COLS, row))
                for row in self._store.fetchall(sql, params)]

    def snapshot_path(self, event_id: str) -> str | None:
        rows = self._store.fetchall(
            "SELECT snapshot_path FROM events WHERE event_id=?", (event_id,))
        return rows[0][0] if rows and rows[0][0] else None

    def unsynced(self, limit: int = 100) -> list[tuple]:
        return self._store.fetchall(
            "SELECT * FROM events WHERE synced=0 ORDER BY timestamp LIMIT ?",
            (limit,),
        )

    def mark_synced(self, event_id: str) -> None:
        self._store.execute(
            "UPDATE events SET synced=1 WHERE event_id=?",
            (event_id,),
        )
