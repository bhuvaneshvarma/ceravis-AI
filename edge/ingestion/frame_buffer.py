from __future__ import annotations

from datetime import datetime
from threading import RLock

import numpy as np

from common import clock
from ingestion.frame_data import FrameData


class FrameBuffer:
    """
    Thread-safe latest-frame store.

    One slot per camera (drop-oldest semantics for free).
    Writers: RTSPReader threads.
    Readers: the AI runners (detection/tracking/pose/reid) + snapshots.
    No viewer ever reads it — live video comes off MediaMTX over WebRTC.
    """

    __slots__ = ("_lock", "_latest_frames")

    def __init__(self) -> None:
        self._lock = RLock()
        self._latest_frames: dict[str, FrameData] = {}

    # ---- write -------------------------------------------------------
    def update(
        self,
        camera_id: str,
        frame: np.ndarray,
        frame_id: int,
        timestamp: datetime,
        fps: float,
    ) -> None:
        height, width = frame.shape[:2]
        frame_data = FrameData(
            camera_id=camera_id,
            frame=frame,
            frame_id=frame_id,
            timestamp=timestamp,
            width=width,
            height=height,
            fps=fps,
        )
        with self._lock:
            self._latest_frames[camera_id] = frame_data

    # ---- read --------------------------------------------------------
    def get(self, camera_id: str) -> FrameData | None:
        with self._lock:
            return self._latest_frames.get(camera_id)

    def get_all_latest(self) -> dict[str, FrameData]:
        with self._lock:
            return dict(self._latest_frames)

    def is_stale(
        self,
        camera_id: str,
        max_age_secs: float = 5.0,
    ) -> bool:
        frame = self.get(camera_id)
        if frame is None:
            return True
        # frame.timestamp is tz-aware (common.clock); aware-minus-aware is offset-
        # correct even if a caller ever stamped one in UTC.
        age = (clock.now() - frame.timestamp).total_seconds()
        return age > max_age_secs

    def clear(self, camera_id: str) -> None:
        with self._lock:
            self._latest_frames.pop(camera_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._latest_frames.clear()
