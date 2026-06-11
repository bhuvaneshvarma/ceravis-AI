from __future__ import annotations

import time
from collections import deque
from threading import Lock


class PipelineMetrics:
    """
    Lightweight rolling FPS + latency tracker.

    Each runner calls record(latency_secs) after every inference.
    """

    WINDOW = 50

    __slots__ = ("_name", "_latencies", "_timestamps", "_lock")

    def __init__(self, name: str) -> None:
        self._name = name
        self._latencies: deque[float] = deque(maxlen=self.WINDOW)
        self._timestamps: deque[float] = deque(maxlen=self.WINDOW)
        self._lock = Lock()

    def record(self, latency_secs: float) -> None:
        with self._lock:
            self._latencies.append(latency_secs)
            self._timestamps.append(time.perf_counter())

    def snapshot(self) -> dict:
        with self._lock:
            if not self._latencies:
                return {
                    "name": self._name,
                    "fps": 0.0,
                    "latency_ms_avg": 0.0,
                    "latency_ms_p95": 0.0,
                    "samples": 0,
                }
            n = len(self._timestamps)
            span = self._timestamps[-1] - self._timestamps[0]
            fps = (n - 1) / span if span > 0 else 0.0
            lat = sorted(self._latencies)
            return {
                "name": self._name,
                "fps": round(fps, 2),
                "latency_ms_avg": round(sum(lat) * 1000 / n, 2),
                "latency_ms_p95": round(lat[int(0.95 * (n - 1))] * 1000, 2),
                "samples": n,
            }


class MetricsRegistry:
    """Holds metrics for every pipeline."""

    def __init__(self) -> None:
        self._metrics: dict[str, PipelineMetrics] = {}

    def get_or_create(self, name: str) -> PipelineMetrics:
        if name not in self._metrics:
            self._metrics[name] = PipelineMetrics(name)
        return self._metrics[name]

    def snapshot_all(self) -> list[dict]:
        return [m.snapshot() for m in self._metrics.values()]
