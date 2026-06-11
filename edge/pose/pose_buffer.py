from __future__ import annotations

from threading import RLock

from pose.pose_schema import PoseResult


class PoseBuffer:
    """Thread-safe latest pose-result store per camera."""

    __slots__ = ("_lock", "_poses")

    def __init__(self) -> None:
        self._lock = RLock()
        self._poses: dict[str, PoseResult] = {}

    def update(self, result: PoseResult) -> None:
        with self._lock:
            self._poses[result.camera_id] = result

    def get(self, camera_id: str) -> PoseResult | None:
        with self._lock:
            return self._poses.get(camera_id)

    def get_all(self) -> dict[str, PoseResult]:
        with self._lock:
            return dict(self._poses)
