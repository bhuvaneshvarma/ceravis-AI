from __future__ import annotations

import logging
import queue
import threading

from events.event_bus import EventBus
from storage.event_store import EventStore


logger = logging.getLogger("events")


class EventWriter:
    """Drains EventBus -> SQLite EventStore in a background thread."""

    def __init__(self, bus: EventBus, store: EventStore) -> None:
        self._queue = bus.subscribe()
        self._store = store
        self._running = False
        self._thread: threading.Thread | None = None

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
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._store.save_event(event)
            except Exception:
                logger.exception("event save failed")
