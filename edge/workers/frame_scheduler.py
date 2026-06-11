from __future__ import annotations

"""
Frame scheduler.

The current design has each runner polling FrameBuffer directly
(simple, works fine for ≤4 cameras on the Orin Nano Super).

This module is a placeholder for when we exceed that — it provides
single-slot drop-oldest queues per runner so we can decouple
inference rates without backpressure on the RTSP reader.

Use it by wiring:
    scheduler = FrameScheduler(camera_manager.frame_buffer)
    scheduler.register("detection", target_fps=5)
    scheduler.register("pose", target_fps=2)
    scheduler.register("reid", target_fps=2)
    scheduler.start()
    ...
    fd = scheduler.consume("detection", camera_id)
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue

from ingestion.frame_buffer import FrameBuffer
from ingestion.frame_data import FrameData


logger = logging.getLogger("workers")


@dataclass(slots=True)
class _Subscriber:
    target_fps: float
    queues: dict[str, "Queue[FrameData]"] = field(default_factory=dict)
    next_due: dict[str, float] = field(default_factory=dict)


class FrameScheduler:
    """Multi-rate fan-out from FrameBuffer to per-runner queues."""

    POLL_HZ = 30.0

    def __init__(self, frame_buffer: FrameBuffer) -> None:
        self._frames = frame_buffer
        self._subs: dict[str, _Subscriber] = {}
        self._running = False
        self._thread: threading.Thread | None = None

    def register(self, name: str, target_fps: float) -> None:
        self._subs[name] = _Subscriber(target_fps=target_fps)

    def consume(self, name: str, camera_id: str, block: bool = False) -> FrameData | None:
        sub = self._subs.get(name)
        if sub is None:
            return None
        q = sub.queues.get(camera_id)
        if q is None:
            return None
        try:
            return q.get(block=block, timeout=0.001 if block else None)
        except Empty:
            return None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="frame-scheduler",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        interval = 1.0 / self.POLL_HZ
        while self._running:
            t0 = time.perf_counter()
            self._tick(t0)
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _tick(self, now: float) -> None:
        latest = self._frames.get_all_latest()
        for camera_id, fd in latest.items():
            for sub in self._subs.values():
                due = sub.next_due.get(camera_id, 0.0)
                if now < due:
                    continue
                sub.next_due[camera_id] = now + 1.0 / sub.target_fps
                q = sub.queues.setdefault(camera_id, Queue(maxsize=1))
                # drop-oldest
                if q.full():
                    try:
                        q.get_nowait()
                    except Empty:
                        pass
                q.put_nowait(fd)
