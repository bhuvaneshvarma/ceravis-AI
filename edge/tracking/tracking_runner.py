from __future__ import annotations

import logging
import threading
import time

from datetime import datetime, timezone

import numpy as np

from config.settings import settings
from detection.detection_buffer import DetectionBuffer
from detection.detection_schema import BoundingBox
from tracking.bytetrack_adapter import ByteTrackAdapter
from tracking.track_buffer import TrackBuffer
from tracking.track_schema import Track, TrackResult


logger = logging.getLogger("tracking")

# supervision ships ByteTrack — light, pure-python
try:
    import supervision as sv  # type: ignore
    _SV_AVAILABLE = True
except ImportError:  # pragma: no cover
    sv = None  # type: ignore
    _SV_AVAILABLE = False


class TrackingRunner:
    """
    Per-camera ByteTrack instance, polled from DetectionBuffer.

    Cheap: ByteTrack is pure CPU and runs in microseconds.
    """

    def __init__(
        self,
        detection_buffer: DetectionBuffer,
        track_buffer: TrackBuffer,
    ) -> None:
        if not _SV_AVAILABLE:
            raise RuntimeError("supervision not installed")

        self._detections = detection_buffer
        self._tracks = track_buffer
        self._trackers: dict[str, sv.ByteTrack] = {}
        self._last_seen_frame: dict[str, int] = {}

        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="tracking-runner",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- internals ---------------------------------------------------
    def _tracker(self, camera_id: str) -> "sv.ByteTrack":
        if camera_id not in self._trackers:
            self._trackers[camera_id] = sv.ByteTrack(
                track_activation_threshold=settings.tracker_high_thresh,
                lost_track_buffer=settings.tracker_track_buffer,
                minimum_matching_threshold=settings.tracker_match_thresh,
            )
        return self._trackers[camera_id]

    def _run(self) -> None:
        interval = 1.0 / settings.detection_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                logger.exception("tracking tick failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _tick(self) -> None:
        for camera_id, det_result in self._detections.get_all().items():
            if self._last_seen_frame.get(camera_id) == det_result.frame_id:
                continue
            self._last_seen_frame[camera_id] = det_result.frame_id

            xyxy_conf = ByteTrackAdapter.to_bytetrack(det_result)
            if xyxy_conf.size == 0:
                self._tracks.update(
                    TrackResult(
                        camera_id=camera_id,
                        frame_id=det_result.frame_id,
                        timestamp=det_result.timestamp,
                        tracks=[],
                    )
                )
                continue

            detections = sv.Detections(
                xyxy=xyxy_conf[:, :4],
                confidence=xyxy_conf[:, 4],
                class_id=np.zeros(len(xyxy_conf), dtype=int),
            )
            tracked = self._tracker(camera_id).update_with_detections(detections)

            out: list[Track] = []
            for i in range(len(tracked)):
                x1, y1, x2, y2 = tracked.xyxy[i]
                out.append(
                    Track(
                        track_id=int(tracked.tracker_id[i]),
                        camera_id=camera_id,
                        frame_id=det_result.frame_id,
                        timestamp=det_result.timestamp,
                        bbox=BoundingBox(x1=float(x1), y1=float(y1),
                                         x2=float(x2), y2=float(y2)),
                        confidence=float(tracked.confidence[i]),
                    )
                )
            self._tracks.update(
                TrackResult(
                    camera_id=camera_id,
                    frame_id=det_result.frame_id,
                    timestamp=det_result.timestamp,
                    tracks=out,
                )
            )
