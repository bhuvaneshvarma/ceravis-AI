from __future__ import annotations

from threading import RLock

from tracking.track_schema import TrackResult


class TrackBuffer:
    """Thread-safe latest-track-result store, one slot per camera."""

    __slots__ = ("_lock", "_tracks")

    def __init__(self) -> None:
        self._lock = RLock()
        self._tracks: dict[str, TrackResult] = {}

    def update(self, result: TrackResult) -> None:
        with self._lock:
            self._tracks[result.camera_id] = result

    def get(self, camera_id: str) -> TrackResult | None:
        with self._lock:
            return self._tracks.get(camera_id)

    def get_all(self) -> dict[str, TrackResult]:
        with self._lock:
            return dict(self._tracks)

    def clear(self, camera_id: str) -> None:
        with self._lock:
            self._tracks.pop(camera_id, None)
