from __future__ import annotations

import logging
import queue
import threading
import time

from events.event_bus import EventBus
from storage.event_store import EventStore


logger = logging.getLogger("events")

# How often the retention sweep runs. Only the CADENCE lives here — the policy
# is settings.event_retention_days. Hourly is ample for a window measured in
# days, and costs one indexed DELETE plus one directory listing.
_SWEEP_INTERVAL_SECS = 3600.0


class EventWriter:
    """Drains EventBus -> SQLite EventStore in a background thread, and expires
    the old end of that record on the same thread.

    Writing events and deleting expired ones are one job on one thread on
    purpose: the store owns both halves of the record (rows + snapshot JPEGs),
    so there is nothing for a separate sweeper to add except a second thread
    that could disagree with this one."""

    def __init__(self, bus: EventBus, store: EventStore) -> None:
        self._queue = bus.subscribe()
        self._store = store
        self._running = False
        self._thread: threading.Thread | None = None
        self._swept_at: float | None = None      # None = sweep on first tick

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="event-writer",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while self._running:
            self._maybe_sweep()
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._store.save_event(event)
            except Exception:
                logger.exception("event save failed")

    def _maybe_sweep(self) -> None:
        """Apply the retention policy, first tick then hourly. Best-effort:
        housekeeping never takes the event writer down with it, and the
        timestamp is stamped BEFORE the sweep so a persistently failing one
        retries hourly rather than on every 0.5s tick."""
        now = time.monotonic()
        if (self._swept_at is not None
                and now - self._swept_at < _SWEEP_INTERVAL_SECS):
            return
        self._swept_at = now
        try:
            self._store.sweep_retention()
        except Exception:
            logger.exception("event retention sweep failed")
