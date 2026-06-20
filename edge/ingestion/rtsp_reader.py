from __future__ import annotations

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
from schemas.cameras import CameraCodec


logger = logging.getLogger("ingestion")


class RTSPReader:
    """
    Production-grade RTSP camera reader.

    One instance per camera.
    One daemon thread per instance.
    """

    def __init__(
        self,
        camera: Camera,
        frame_buffer: FrameBuffer,
        target_fps: int | None = None,
    ) -> None:

        self._camera = camera

        self._frame_buffer = frame_buffer

        self._target_fps = (
            target_fps
            or settings.target_camera_fps
        )

        self._capture: cv2.VideoCapture | None = None

        self._thread: threading.Thread | None = None

        self._running = False

        self._frame_id = 0

        self._frames_captured = 0

        self._reconnect_count = 0

        self._last_frame_time: datetime | None = None

        self._health_state = (
            CameraHealthState.OFFLINE
        )

        self._fps_lock = threading.Lock()

        self._fps_counter = 0

        self._fps_window_start = (
            time.perf_counter()
        )

        self._current_fps = 0.0

        self._stats_timestamp = (
            time.perf_counter()
        )

    # =====================================================
    # Properties
    # =====================================================

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
    def last_frame_time(
        self
    ) -> datetime | None:
        return self._last_frame_time

    @property
    def health_state(
        self
    ) -> CameraHealthState:
        return self._health_state

    @property
    def current_fps(self) -> float:

        with self._fps_lock:
            return self._current_fps

    # =====================================================
    # Lifecycle
    # =====================================================

    def start(self) -> None:

        if self._running:
            return

        self._running = True

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"rtsp-{self.camera_id}",
        )

        self._thread.start()

    def stop(self) -> None:

        self._running = False

        self._health_state = (
            CameraHealthState.OFFLINE
        )

        if self._capture is not None:

            try:
                self._capture.release()
            except Exception:
                logger.exception(
                    "Capture release failed camera=%s",
                    self.camera_id,
                )

    def join(
        self,
        timeout: float | None = None
    ) -> None:

        if self._thread is not None:
            self._thread.join(timeout)

    # =====================================================
    # Connection
    # =====================================================

    # Per-camera RTSP transport / jitter buffer, falling back to the globals.
    # A clean direct-Ethernet camera -> "udp" + low latency = minimal lag; a
    # lossy WiFi camera -> "tcp".
    @property
    def _transport(self) -> str:
        return (getattr(self._camera, "transport", None)
                or settings.rtsp_transport)

    @property
    def _latency_ms(self) -> int:
        v = getattr(self._camera, "rtsp_latency_ms", None)
        return int(v) if v is not None else int(settings.rtsp_latency_ms)

    def _build_gstreamer_pipeline(
        self
    ) -> str:

        if (
            self._camera.codec
            == CameraCodec.H265
        ):

            depay = (
                "rtph265depay ! h265parse"
            )

        else:

            depay = (
                "rtph264depay ! h264parse"
            )

        return (
            f"rtspsrc location={self._camera.rtsp_url} "
            f"protocols={self._transport} "
            f"latency={self._latency_ms} drop-on-latency=true ! "
            f"{depay} ! "
            f"nvv4l2decoder ! "
            f"nvvidconv ! "
            f"video/x-raw,format=BGRx ! "
            f"videoconvert ! "
            f"video/x-raw,format=BGR ! "
            f"appsink drop=true max-buffers=1 sync=false"
        )

    def _build_sw_pipeline(self) -> str:

        # Software-decode fallback, used when the NVIDIA hardware decoder
        # elements (nvv4l2decoder / nvvidconv) aren't available inside the
        # container. Relies on gstreamer1.0-libav (avdec_*), installed in
        # the image.
        if self._camera.codec == CameraCodec.H265:
            depay = "rtph265depay ! h265parse ! avdec_h265"
        else:
            depay = "rtph264depay ! h264parse ! avdec_h264"

        return (
            f"rtspsrc location={self._camera.rtsp_url} "
            f"protocols={self._transport} "
            f"latency={self._latency_ms} drop-on-latency=true ! "
            f"{depay} ! videoconvert ! "
            f"video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false"
        )

    def _connect(self) -> bool:

        self._health_state = (
            CameraHealthState.CONNECTING
        )

        # Make the FFmpeg fallback use THIS camera's transport (set per-connect
        # so two cameras with different transports don't clobber each other).
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            f"rtsp_transport;{self._transport}"
        )

        # Try, in order: hardware GStreamer (nvv4l2decoder), software
        # GStreamer (avdec via libav), then plain FFmpeg. The first that
        # opens wins — so ingestion keeps working even if the NVIDIA
        # GStreamer plugins aren't mounted inside the container.
        attempts = (
            [
                ("hw-gstreamer", self._build_gstreamer_pipeline),
                ("sw-gstreamer", self._build_sw_pipeline),
                ("ffmpeg", None),
            ]
            if settings.is_production
            else [("ffmpeg", None)]
        )

        for name, builder in attempts:

            try:
                if builder is None:
                    cap = cv2.VideoCapture(self._camera.rtsp_url)
                else:
                    cap = cv2.VideoCapture(builder(), cv2.CAP_GSTREAMER)
            except (RuntimeError, ValueError, OSError, cv2.error):
                logger.warning(
                    "Open via %s raised camera=%s", name, self.camera_id
                )
                continue

            if cap is not None and cap.isOpened():
                self._capture = cap
                self._health_state = CameraHealthState.RUNNING
                logger.info(
                    "Connected camera=%s via %s", self.camera_id, name
                )
                return True

            if cap is not None:
                cap.release()

            logger.warning(
                "Open via %s failed camera=%s", name, self.camera_id
            )

        return False

    # =====================================================
    # Main Loop
    # =====================================================

    def _run(self) -> None:

        reconnect_delay = (
            settings.reconnect_delay_secs
        )

        frame_interval = (
            1.0 / self._target_fps
        )

        while self._running:

            if not self._connect():

                self._reconnect_count += 1

                self._health_state = (
                    CameraHealthState.RECONNECTING
                )

                logger.warning(
                    "Reconnect camera=%s delay=%ss",
                    self.camera_id,
                    reconnect_delay,
                )

                time.sleep(reconnect_delay)

                reconnect_delay = min(
                    reconnect_delay * 2,
                    settings.max_reconnect_delay_secs,
                )

                continue

            reconnect_delay = (
                settings.reconnect_delay_secs
            )

            next_frame_time = (
                time.perf_counter()
            )

            while (
                self._running
                and self._capture is not None
            ):

                now = time.perf_counter()

                if now < next_frame_time:
                    time.sleep(
                        next_frame_time - now
                    )

                next_frame_time += (
                    frame_interval
                )

                read_started = (
                    time.perf_counter()
                )

                success, frame = (
                    self._capture.read()
                )

                read_duration = (
                    time.perf_counter()
                    - read_started
                )

                if (
                    read_duration
                    > settings.read_timeout_secs
                ):

                    logger.warning(
                        "Read timeout camera=%s duration=%.2f",
                        self.camera_id,
                        read_duration,
                    )

                    break

                if not success:

                    logger.warning(
                        "Read failure camera=%s",
                        self.camera_id,
                    )

                    break

                self._frame_id += 1

                self._frames_captured += 1

                self._last_frame_time = (
                    datetime.now(timezone.utc)
                )

                self._update_fps()

                self._frame_buffer.update(
                    camera_id=self.camera_id,
                    frame=frame,
                    frame_id=self._frame_id,
                    timestamp=self._last_frame_time,
                    fps=self.current_fps,
                )

                self._log_stats()

            self._frame_buffer.clear(
                self.camera_id
            )

            if self._capture is not None:

                self._capture.release()

            self._capture = None

            if self._running:
                self._reconnect_count += 1
                self._health_state = (CameraHealthState.RECONNECTING)

    # =====================================================
    # Metrics
    # =====================================================

    def _update_fps(self) -> None:

        with self._fps_lock:

            self._fps_counter += 1

            elapsed = (
                time.perf_counter()
                - self._fps_window_start
            )

            if elapsed >= 1.0:

                self._current_fps = (
                    self._fps_counter
                    / elapsed
                )

                self._fps_counter = 0

                self._fps_window_start = (
                    time.perf_counter()
                )

    def _log_stats(self) -> None:

        elapsed = (
            time.perf_counter()
            - self._stats_timestamp
        )

        if elapsed < 60:
            return

        logger.info(
            "camera=%s fps=%.2f frames=%s reconnects=%s",
            self.camera_id,
            self.current_fps,
            self._frames_captured,
            self._reconnect_count,
        )

        self._stats_timestamp = (
            time.perf_counter()
        )