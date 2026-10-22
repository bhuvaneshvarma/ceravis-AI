from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from common.zone_resolver import ZoneResolver
from config.settings import settings
from detection.detection_buffer import DetectionBuffer
from detection.yolo_detector import YOLODetector
from ingestion.frame_buffer import FrameBuffer
from common import clock


logger = logging.getLogger("detection")

# How early a camera may join a batch that is going to the GPU anyway.
_COALESCE_SECS = 0.05


class DetectionRunner:
    """
    Single-thread detection loop.

    Optimisations:
      - Activity-paced: each camera runs at detection_fps while a person is in
        view (or was within detection_active_hold_secs), at detection_idle_fps
        while its room is empty
      - Skip cameras whose frame_id hasn't changed (no duplicate inference)
      - Cameras due at the same moment share ONE GPU call (up to the engine's
        batch size) — fewer launches and copies than one call per camera
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
        # Drawn IGNORE regions (a TV, a monitor wall, a framed photo, a mirror):
        # a "person" whose FOOT point lands in one is dropped HERE, at the source,
        # so it never becomes a track, a recording, a visitor snapshot or any
        # event — one mechanism protecting the whole chain. Opt-in (no ignore
        # zone drawn = no effect); foot-point, so a real person standing in FRONT
        # of a wall screen is untouched.
        self._zones = ZoneResolver()
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
        self._next_due: dict[str, float] = {}      # camera -> monotonic due time
        self._last_person: dict[str, float] = {}   # camera -> monotonic last person
        self._batching = True                      # off after a batch fails once

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
        while self._running:
            try:
                wait = self._process_all_frames()
            except Exception:
                logger.exception("Detection loop failure")
                wait = 0.1
            time.sleep(wait)

    def _interval(self, camera_id: str, now: float) -> float:
        """Seconds between detections on this camera: full rate while a person
        is (or just was) in view, idle rate while the room is empty."""
        active = (now - self._last_person.get(camera_id, -1e9)
                  <= settings.detection_active_hold_secs)
        fps = settings.detection_fps if active else settings.detection_idle_fps
        return 1.0 / max(0.1, fps)

    def _process_all_frames(self) -> float:
        """Detect on every camera that is DUE; return how long to sleep."""
        assert self._detector is not None
        frames = self._frame_buffer.get_all_latest()
        if not frames:
            return 0.1
        # Detection runs on EVERY camera: it is the cheap "person present?" signal
        # that (a) drives per-camera recording (RecordingController) and (b) lets
        # the system (re)find the recipient on whichever camera they are on. So
        # does tracking downstream; the GPU is saved instead by pose being
        # target-only, ReID verifying only the locked track, and an EMPTY camera
        # being checked at the idle rate.
        now = time.monotonic()
        size = max(1, self._detector.max_batch) if self._batching else 1
        # With room in the batch, a camera that is due within a few tens of ms
        # rides along now instead of costing its own GPU call a moment later;
        # from then on they are rescheduled together and stay in step. The
        # window is capped so an empty room's slow rate is barely touched.
        early = min(_COALESCE_SECS, 0.5 / max(0.1, settings.detection_fps)) if size > 1 else 0.0
        due = []
        for camera_id, fd in frames.items():
            if now < self._next_due.get(camera_id, 0.0):
                continue
            if self._last_seen_frame.get(camera_id) == fd.frame_id:
                continue
            due.append(fd)
        if due and early:
            due += [fd for camera_id, fd in frames.items()
                    if fd not in due
                    and 0 < self._next_due.get(camera_id, 0.0) - now <= early
                    and self._last_seen_frame.get(camera_id) != fd.frame_id]
        for fd in due:
            self._last_seen_frame[fd.camera_id] = fd.frame_id
        for start in range(0, len(due), size):
            self._detect_group(due[start:start + size])
        now = time.monotonic()
        due = min((self._next_due.get(c, now) for c in frames), default=now)
        # Never busy-spin on a camera whose next frame has not arrived yet.
        return min(0.1, max(0.01, due - now))

    def _detect_group(self, group) -> None:
        """One GPU call for these frames; each result is then handled exactly as
        a single detection always was. A failed batch falls back to one call
        per camera, so one bad frame cannot blind the others."""
        try:
            t = time.perf_counter()
            results = self._detector.detect_batch(
                [(fd.image, fd.camera_id, fd.frame_id, fd.timestamp) for fd in group])
            per_frame = (time.perf_counter() - t) / len(group)
        except Exception:
            if len(group) == 1:
                logger.exception("Detection failed camera=%s", group[0].camera_id)
                self._schedule(group[0].camera_id, False)
                return
            logger.exception("Batched detection failed — detecting one camera at a "
                             "time from now on")
            self._batching = False
            for fd in group:
                self._detect_group([fd])
            return
        for fd, result in zip(group, results):
            if self._metrics:
                self._metrics.record(per_frame)
            result = self._drop_excluded(result)
            self._detection_buffer.update(result)
            self._frames_processed += 1
            self._detections_generated += len(result.detections)
            self._last_inference_time = clock.now()
            self._schedule(fd.camera_id, bool(result.detections))

    def _schedule(self, camera_id: str, person: bool) -> None:
        done = time.monotonic()
        if person:
            self._last_person[camera_id] = done
        self._next_due[camera_id] = done + self._interval(camera_id, done)

    def _drop_excluded(self, result):
        """Remove person boxes whose FOOT point falls in a drawn ignore zone —
        the single, source-level place a TV/photo/mirror region is masked from
        every downstream consumer. No zones drawn -> returns the result
        unchanged (the common case, and a cheap cached lookup)."""
        kept = [d for d in result.detections
                if not self._zones.is_excluded(
                    result.camera_id, (d.bbox.x1 + d.bbox.x2) / 2.0, d.bbox.y2)]
        if len(kept) != len(result.detections):
            result.detections = kept
        return result
