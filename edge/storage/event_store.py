from __future__ import annotations

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


class EventStore:
    """Persists Events for offline buffering until cloud sync."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._store.execute(stmt)

    def save_event(self, event: Event) -> None:
        self._store.execute(
            """INSERT OR REPLACE INTO events
               (event_id, event_type, camera_id, room_name, zone_name,
                recipient_id, timestamp, snapshot_path, video_path, synced)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (
                event.event_id, event.event_type, event.camera_id,
                event.room_name, event.zone_name, event.recipient_id,
                event.timestamp, event.snapshot_path, event.video_path,
            ),
        )

    def recent(self, limit: int = 50,
               camera_id: str | None = None) -> list[dict]:
        """Most recent events, newest first — consumed by the monitor UI."""
        sql = ("SELECT event_id, event_type, camera_id, room_name, zone_name,"
               " recipient_id, timestamp, synced FROM events")
        params: tuple = ()
        if camera_id:
            sql += " WHERE camera_id=?"
            params = (camera_id,)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params += (int(limit),)
        cols = ("event_id", "event_type", "camera_id", "room_name",
                "zone_name", "recipient_id", "timestamp", "synced")
        return [dict(zip(cols, row))
                for row in self._store.fetchall(sql, params)]

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
