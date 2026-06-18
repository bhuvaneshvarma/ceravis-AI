from __future__ import annotations

import asyncio
import logging
import queue
import threading

from events.event_bus import EventBus


logger = logging.getLogger("alerts")


def _safe_put(q: "asyncio.Queue", payload: dict) -> None:
    try:
        q.put_nowait(payload)
    except asyncio.QueueFull:
        pass                       # slow client — drop rather than block


class AlertBroadcaster:
    """
    Bridges the threaded EventBus to async WebSocket clients.

    A background thread drains the bus subscription and pushes each enriched
    event (as a dict) into every connected client's asyncio.Queue via the event
    loop, so the monitor sees alerts in real time instead of polling every 5 s.
    """

    def __init__(self, bus: EventBus) -> None:
        self._src = bus.subscribe()
        self._clients: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="alert-broadcaster",
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
                event = self._src.get(timeout=0.5)
            except queue.Empty:
                continue
            loop = self._loop
            if loop is None:
                continue
            try:
                payload = event.model_dump()
            except Exception:
                continue
            with self._lock:
                clients = list(self._clients)
            for q in clients:
                try:
                    loop.call_soon_threadsafe(_safe_put, q, payload)
                except Exception:
                    pass

    # ---- async side (WebSocket handler) -----------------------------
    async def register(self) -> "asyncio.Queue":
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._clients.add(q)
        return q

    async def unregister(self, q: "asyncio.Queue") -> None:
        with self._lock:
            self._clients.discard(q)
