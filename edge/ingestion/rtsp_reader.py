from __future__ import annotations

"""
Decodes ONE camera's stream into the FrameBuffer for the AI pipeline.

With the MediaMTX backbone up, the source is the rock-solid localhost
restream (rtsp://127.0.0.1:8554/<camera>) — MediaMTX owns the actual camera
connection, its flaky WiFi transport and its reconnects, and fans the same
compressed stream out to live view / recording without re-encoding. This
reader is then purely: pull loopback RTSP -> hardware-decode -> BGR frames.

Fallback (dev box / MediaMTX missing): reads the camera's RTSP URL directly
over TCP. Same decode ladder either way:
    hw GStreamer (nvv4l2decoder) -> sw GStreamer (avdec) -> plain FFmpeg.

The stream codec (h264/h265) is auto-detected from MediaMTX's path info when
available; otherwise both depayloaders are tried — no manual codec setting.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

import cv2

from config.settings import settings
from ingestion.camera_status import CameraHealthState
from ingestion.frame_buffer import FrameBuffer
from schemas.cameras import Camera


logger = logging.getLogger("ingestion")


class RTSPReader:
    """One camera, one daemon thread, frames into the FrameBuffer."""

    def __init__(
        self,
        camera: Camera,
        frame_buffer: FrameBuffer,
        source_url: str | None = None,
        target_fps: int | None = None,
    ) -> None:
        self._camera = camera
        self._frame_buffer = frame_buffer
        # MediaMTX localhost restream when the backbone is up, else the camera.
        self._source_url = source_url or camera.rtsp_url
        self._target_fps = target_fps or settings.target_camera_fps
        self._via_mediamtx = source_url is not None

        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        self._frame_id = 0
        self._frames_captured = 0
        self._reconnect_count = 0
        self._last_frame_time: datetime | None = None
        self._health_state = CameraHealthState.OFFLINE

        self._fps_lock = threading.Lock()
        self._fps_counter = 0
        self._fps_window_start = time.perf_counter()
        self._current_fps = 0.0
        self._stats_timestamp = time.perf_counter()

    # ---- properties ----------------------------------------------------
    @property
    def camera_id(self) -> str:
        return self._camera.camera_id

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def frames_captured(self) -> int:
        return self._frames_captured

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def last_frame_time(self) -> datetime | None:
        return self._last_frame_time

    @property
    def health_state(self) -> CameraHealthState:
        return self._health_state

    @property
    def current_fps(self) -> float:
        with self._fps_lock:
            return self._current_fps

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"rtsp-{self.camera_id}")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._health_state = CameraHealthState.OFFLINE
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                logger.exception("Capture release failed camera=%s", self.camera_id)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- connection -------------------------------------------------------
    def _gst_pipeline(self, codec: str, hw: bool) -> str:
        depay = ("rtph265depay ! h265parse" if codec == "h265"
                 else "rtph264depay ! h264parse")
        decode = ("nvv4l2decoder ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert"
                  if hw else
                  ("avdec_h265" if codec == "h265" else "avdec_h264") + " ! videoconvert")
        # NO drop-on-latency. On a lossless loopback pull there is no network
        # jitter to bound — the flag only DROPS packets when reassembly briefly
        # exceeds the latency budget, which is exactly what happens on movement
        # (big P-frames): incomplete frames -> decode smearing the AI reads as
        # noise. Let the jitterbuffer keep every packet and emit only COMPLETE
        # frames. Freshness is owned downstream by the appsink (drop=true
        # max-buffers=1), which always yields the newest complete frame and
        # discards the backlog — so we get integrity AND low latency, not a
        # trade-off. latency= is just reassembly headroom now (see settings).
        return (
            f"rtspsrc location={self._source_url} protocols=tcp "
            f"latency={int(settings.rtsp_latency_ms)} ! "
            f"{depay} ! {decode} ! "
            f"video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false"
        )

    def _detect_codec(self) -> str | None:
        """Ask MediaMTX what the path is actually carrying (h264/h265)."""
        if not self._via_mediamtx:
            return None
        try:
            from livestream.mediamtx_client import path_codec
            return path_codec(self.camera_id)
        except Exception:
            return None

    def _connect(self) -> bool:
        self._health_state = CameraHealthState.CONNECTING
        # The plain-FFmpeg fallback honors this env var; interleaved TCP avoids
        # UDP loss artifacts on every link we use (loopback or direct).
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

        codec = self._detect_codec()
        codecs = [codec] if codec else ["h264", "h265"]
        # hw GStreamer first (per codec), then sw GStreamer, then plain FFmpeg —
        # so ingestion still works where the NVIDIA plugins aren't available.
        attempts: list[tuple[str, str | None]] = []
        if settings.is_production:
            attempts += [(f"hw-gst-{c}", self._gst_pipeline(c, hw=True)) for c in codecs]
            attempts += [(f"sw-gst-{c}", self._gst_pipeline(c, hw=False)) for c in codecs]
        attempts.append(("ffmpeg", None))

        for name, pipeline in attempts:
            try:
                if pipeline is None:
                    cap = cv2.VideoCapture(self._source_url)
                    # FFmpeg has no appsink to drop old frames: cap its internal
                    # queue to 1 so an uncapped reader can't build a latency
                    # backlog (the GStreamer paths bound this at the appsink).
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                else:
                    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            except (RuntimeError, ValueError, OSError, cv2.error):
                logger.warning("Open via %s raised camera=%s", name, self.camera_id)
                continue
            if cap is not None and cap.isOpened():
                self._capture = cap
                self._health_state = CameraHealthState.RUNNING
                logger.info("Connected camera=%s via %s (%s)", self.camera_id, name,
                            "mediamtx" if self._via_mediamtx else "direct")
                return True
            if cap is not None:
                cap.release()
            logger.warning("Open via %s failed camera=%s", name, self.camera_id)
        return False

    # ---- main loop ---------------------------------------------------------
    def _run(self) -> None:
        reconnect_delay = settings.reconnect_delay_secs
        # target_fps <= 0 => UNCAPPED: drain the decoder at the camera's native
        # rate and publish every frame to the latest-frame buffer, so consumers
        # (detection/pose/reid) always see the freshest frame and motion is
        # smooth. Decimating capture below native never helps latency — both
        # backends already hand back only the newest frame (GStreamer appsink
        # drop / FFmpeg BUFFERSIZE=1) — it only makes movement choppy. A positive
        # value re-imposes a soft ceiling for weak / many-camera boxes.
        frame_interval = 1.0 / self._target_fps if self._target_fps > 0 else 0.0

        while self._running:
            if not self._connect():
                self._reconnect_count += 1
                self._health_state = CameraHealthState.RECONNECTING
                logger.warning("Reconnect camera=%s delay=%ss",
                               self.camera_id, reconnect_delay)
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2,
                                      settings.max_reconnect_delay_secs)
                continue

            reconnect_delay = settings.reconnect_delay_secs
            next_frame_time = time.perf_counter()

            while self._running and self._capture is not None:
                if frame_interval:                       # 0 => uncapped, no pacing
                    now = time.perf_counter()
                    if now < next_frame_time:
                        time.sleep(next_frame_time - now)
                    next_frame_time += frame_interval

                read_started = time.perf_counter()
                success, frame = self._capture.read()
                if time.perf_counter() - read_started > settings.read_timeout_secs:
                    logger.warning("Read timeout camera=%s", self.camera_id)
                    break
                if not success:
                    logger.warning("Read failure camera=%s", self.camera_id)
                    break

                self._frame_id += 1
                self._frames_captured += 1
                self._last_frame_time = datetime.now(timezone.utc)
                self._update_fps()
                self._frame_buffer.update(
                    camera_id=self.camera_id, frame=frame,
                    frame_id=self._frame_id, timestamp=self._last_frame_time,
                    fps=self.current_fps,
                )
                self._log_stats()

            self._frame_buffer.clear(self.camera_id)
            if self._capture is not None:
                self._capture.release()
            self._capture = None
            if self._running:
                self._reconnect_count += 1
                self._health_state = CameraHealthState.RECONNECTING

    # ---- metrics -------------------------------------------------------
    def _update_fps(self) -> None:
        with self._fps_lock:
            self._fps_counter += 1
            elapsed = time.perf_counter() - self._fps_window_start
            if elapsed >= 1.0:
                self._current_fps = self._fps_counter / elapsed
                self._fps_counter = 0
                self._fps_window_start = time.perf_counter()

    def _log_stats(self) -> None:
        if time.perf_counter() - self._stats_timestamp < 60:
            return
        logger.info("camera=%s fps=%.2f frames=%s reconnects=%s",
                    self.camera_id, self.current_fps,
                    self._frames_captured, self._reconnect_count)
        self._stats_timestamp = time.perf_counter()
