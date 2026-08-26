from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from config.settings import settings
from detection.detection_buffer import DetectionBuffer
from detection.yolo_detector import YOLODetector
from ingestion.frame_buffer import FrameBuffer
from common import clock


logger = logging.getLogger("detection")


class DetectionRunner:
    """
    Single-thread detection loop.

    Optimisations:
      - Skip cameras whose frame_id hasn't changed (no duplicate inference)
      - Soft frame-stale guard
      - Record latency for the metrics dashboard
    """

    def __init__(
        self,
        frame_buffer: FrameBuffer,
        detection_buffer: DetectionBuffer,
        metrics_registry=None,
    ) -> None:
        self._frame_buffer = frame_buffer
        self._detection_buffer = detection_buffer
        self._metrics = (
            metrics_registry.get_or_create("detection")
            if metrics_registry else None
        )

        self._detector: YOLODetector | None = None
        self._running = False
        self._thread: threading.Thread | None = None

        self._frames_processed = 0
        self._detections_generated = 0
        self._last_inference_time: datetime | None = None
        self._last_seen_frame: dict[str, int] = {}

    # ---- properties --------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def frames_processed(self) -> int:
        return self._frames_processed

    @property
    def detections_generated(self) -> int:
        return self._detections_generated

    @property
    def last_inference_time(self) -> datetime | None:
        return self._last_inference_time

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        logger.info("Starting DetectionRunner")
        self._detector = YOLODetector()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="detection-runner",
        )
        self._thread.start()

    def stop(self) -> None:
        logger.info("Stopping DetectionRunner")
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- loop --------------------------------------------------------
    def _run(self) -> None:
        interval = 1.0 / settings.detection_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._process_all_frames()
            except Exception:
                logger.exception("Detection loop failure")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _process_all_frames(self) -> None:
        assert self._detector is not None
        frames = self._frame_buffer.get_all_latest()
        if not frames:
            return
        # Detection runs on EVERY camera: it is the cheap "person present?" signal
        # that (a) drives per-camera recording (RecordingController) and (b) lets
        # the system (re)find the recipient on whichever camera they are on. So
        # does tracking downstream; the GPU is saved instead by pose being
        # target-only and ReID verifying only the locked track (not re-matching
        # the bystanders it already knows are not the recipient).
        for camera_id, fd in frames.items():
            if self._last_seen_frame.get(camera_id) == fd.frame_id:
                continue
            self._last_seen_frame[camera_id] = fd.frame_id
            try:
                t = time.perf_counter()
                result = self._detector.detect(
                    frame=fd.frame,
                    camera_id=fd.camera_id,
                    frame_id=fd.frame_id,
                    timestamp=fd.timestamp,
                )
                latency = time.perf_counter() - t
                if self._metrics:
                    self._metrics.record(latency)
                self._detection_buffer.update(result)
                self._frames_processed += 1
                self._detections_generated += len(result.detections)
                self._last_inference_time = clock.now()
            except Exception:
                logger.exception("Detection failed camera=%s", camera_id)
