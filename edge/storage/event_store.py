from __future__ import annotations

import logging
import shutil
from datetime import date, datetime, timedelta

from common import clock, event_snapshots
from config.settings import settings
from schemas.event import Event
from storage.sqlite_store import SqliteStore


logger = logging.getLogger("storage")


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
]


class EventStore:
    """Persists every Event the rules raise — the device's own record of what
    happened, independent of the cloud.

    It is NOT the delivery mechanism: getting an event to the app server is the
    cloud outbox's job (storage/outbox_store.py), which tracks its own state per
    upload. This table answers "what did this device see", the outbox answers
    "did it get out". The `synced` column is a leftover of an earlier design that
    was never wired up; it is left in the schema so existing device databases
    still open unchanged, and is no longer read or written.

    It also owns how long that record is KEPT — rows and their snapshot JPEGs
    together (see sweep_retention). Same reason the two are written by one
    thing: they are one record in two places."""

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
                track_id, severity, title, message)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id, event.event_type, event.camera_id,
                event.room_name, event.zone_name, event.recipient_id,
                event.timestamp, event.snapshot_path, event.video_path,
                event.track_id, event.severity, event.title, event.message,
            ),
        )

    # ---- retention ---------------------------------------------------
    def sweep_retention(self) -> tuple[int, int]:
        """Expire everything this store owns past `event_retention_days`: the
        rows, and the snapshot JPEGs under events_dir they point at. Returns
        (rows deleted, day folders deleted). 0 days = keep forever.

        BOTH HALVES, ONE OWNER. A sweep that took only the rows would leave
        files nothing can name; one that took only the files would leave rows
        pointing at nothing. There is deliberately no second sweeper thread
        either — EventWriter, which already owns writing events to storage,
        calls this on its own thread.

        NOTHING HERE COORDINATES WITH THE CLOUD OUTBOX, and nothing needs to.
        The outbox never references a file under events_dir: CloudAlertPublisher
        reads the JPEG the instant the event fires and hands the BYTES to
        OutboxStore.enqueue, which spools its OWN copy under data/outbox/ and
        releases it when the job leaves the queue. So a pending upload already
        survives its source snapshot being deleted, and adding a "is this
        referenced?" check would couple two stores to protect against something
        that cannot happen. The windows say the same thing from the other side:
        a job is given up on after outbox_window_secs (24h), so at the default
        14 days nothing here is even old enough to still be queued.
        """
        days = int(settings.event_retention_days)
        if days <= 0:
            return 0, 0
        cutoff = clock.now() - timedelta(days=days)
        rows = self._expire_rows(cutoff.isoformat())
        folders = self._expire_snapshots(cutoff.date())
        if rows or folders:
            logger.info("event retention: dropped %d row(s) and %d day "
                        "folder(s) of snapshots older than %dd",
                        rows, folders, days)
        return rows, folders

    def _expire_rows(self, cutoff_iso: str) -> int:
        """Rows stamped before the cutoff, counted then deleted (SqliteStore
        reports rowids, not rowcounts). Compared as ISO-8601 TEXT, which sorts
        chronologically here because every event is stamped by the one edge
        clock (common.clock) at one offset — and the window is measured in days,
        so even a device whose offset moved is at most a couple of hours off at
        the boundary. Indexed by idx_events_ts."""
        rows = self._store.fetchall(
            "SELECT COUNT(*) FROM events WHERE timestamp<?", (cutoff_iso,))
        n = int(rows[0][0]) if rows else 0
        if n:
            self._store.execute("DELETE FROM events WHERE timestamp<?",
                                (cutoff_iso,))
        return n

    @staticmethod
    def _expire_snapshots(cutoff: date) -> int:
        """Delete whole `<device_id>/<YYYY-MM-DD>/` folders dated before the
        cutoff — the exact layout common.event_snapshots writes, so a day is the
        natural unit to expire and not one file has to be stat'ed.

        Sweeping by FOLDER rather than by row is also what collects the strays:
        a JPEG written just before a crash that lost its INSERT, and every
        folder left behind by a previous device_id, both age out on their own
        instead of living forever. A folder whose name is not a date is left
        alone — never delete what we cannot read — and an emptied device folder
        is removed only because it is empty (the writer recreates it)."""
        root = event_snapshots.events_root()
        if not root.is_dir():
            return 0
        removed = 0
        for device_dir in root.iterdir():
            if not device_dir.is_dir():
                continue
            for day_dir in device_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day >= cutoff:
                    continue
                try:
                    shutil.rmtree(day_dir)
                    removed += 1
                except OSError as exc:
                    logger.warning("could not expire snapshots %s: %s",
                                   day_dir, exc)
            try:
                device_dir.rmdir()        # only if it emptied — a stale device_id
            except OSError:
                pass
        return removed

    # ---- read --------------------------------------------------------
    _COLS = ("event_id", "event_type", "camera_id", "room_name", "zone_name",
             "recipient_id", "timestamp", "snapshot_path", "track_id",
             "severity", "title", "message")

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
