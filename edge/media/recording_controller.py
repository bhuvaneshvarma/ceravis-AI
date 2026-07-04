from __future__ import annotations

"""
Person-triggered local recording.

Watches the DetectionBuffer (the same YOLO output the tracker consumes) and
flips MediaMTX recording per camera:

    person detected on a camera  -> record ON  (15 s fMP4 segments, native
                                    camera quality — remux only, no re-encode)
    nobody for POST_ROLL seconds -> record OFF

Completely independent of alerts/snapshots — this is the playback archive the
ceravishealth frontend browses via /api/v1/recordings. Detections older than
a couple of poll intervals are treated as "nobody" (a camera the AI focus has
idled produces no fresh results, so its recording winds down naturally).
"""

import logging
import threading
import time
from datetime import datetime, timezone

from config.settings import settings
from configuration.camera_config import CameraConfig
from detection.detection_buffer import DetectionBuffer
from media import mediamtx_client
from media.mediamtx_client import MediaMTXError, record_path_name


logger = logging.getLogger("media")

# a detection result older than this is stale — its camera counts as empty
_FRESH_SECS = 3.0


class RecordingController:
    def __init__(self, detections: DetectionBuffer) -> None:
        self._detections = detections
        self._cameras = CameraConfig()
        self._running = False
        self._thread: threading.Thread | None = None
        self._recording: dict[str, bool] = {}      # camera_id -> currently recording
        self._last_person: dict[str, float] = {}   # camera_id -> monotonic time

    def start(self) -> None:
        if not settings.record_enabled:
            logger.info("Recording disabled (RECORD_ENABLED=false)")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="recording-controller")
        self._thread.start()
        logger.info("Recording on person detection — %ds segments, %.0fs post-roll, "
                    "keep %dd", settings.record_segment_secs,
                    settings.record_post_roll_secs, settings.record_retention_days)

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- loop ----------------------------------------------------------
    def _run(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("recording tick failed")
            time.sleep(settings.record_poll_secs)
        # service stopping — close any open recordings
        for cam, on in list(self._recording.items()):
            if on:
                self._set(cam, False)

    def _tick(self) -> None:
        now = time.monotonic()
        wall = datetime.now(timezone.utc)
        for cam, result in self._detections.get_all().items():
            fresh = (wall - result.timestamp).total_seconds() <= _FRESH_SECS
            if fresh and result.detections:          # YOLO only emits persons
                self._last_person[cam] = now
            last = self._last_person.get(cam)
            want = last is not None and \
                (now - last) <= settings.record_post_roll_secs
            if want != self._recording.get(cam, False):
                self._set(cam, want)

    def _set(self, camera_id: str, on: bool) -> None:
        # Record the camera's dedicated -rec path (standardized second ONVIF
        # profile) when it has one; otherwise its main path, native quality.
        cam = self._cameras.get_by_id(camera_id)
        path = record_path_name(cam) if cam else camera_id
        try:
            mediamtx_client.set_record(path, on)
        except MediaMTXError as exc:
            logger.warning("record %s %s failed: %s",
                           "start" if on else "stop", camera_id, exc)
            return
        self._recording[camera_id] = on
        logger.info("recording %s: %s (path %s)",
                    "started" if on else "stopped", camera_id, path)
