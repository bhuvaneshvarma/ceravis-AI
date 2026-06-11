from __future__ import annotations

import logging
import threading
import time

from datetime import datetime, UTC

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
            f"rtspsrc location={self._camera.rtsp_url} latency=100 ! "
            f"{depay} ! "
            f"nvv4l2decoder ! "
            f"nvvidconv ! "
            f"video/x-raw,format=BGRx ! "
            f"videoconvert ! "
            f"video/x-raw,format=BGR ! "
            f"appsink drop=1"
        )

    def _connect(self) -> bool:

        self._health_state = (
            CameraHealthState.CONNECTING
        )

        try:

            if settings.is_production:

                pipeline = (
                    self._build_gstreamer_pipeline()
                )

                self._capture = cv2.VideoCapture(
                    pipeline,
                    cv2.CAP_GSTREAMER,
                )

            else:

                self._capture = cv2.VideoCapture(
                    self._camera.rtsp_url
                )

            if (
                self._capture is None
                or not self._capture.isOpened()
            ):

                logger.warning(
                    "Failed opening stream camera=%s",
                    self.camera_id,
                )

                return False

            logger.info(
                "Connected camera=%s",
                self.camera_id,
            )

            self._health_state = (
                CameraHealthState.RUNNING
            )

            return True

        except (
            RuntimeError,
            ValueError,
            OSError,
            cv2.error,
        ):

            logger.exception(
                "Connection error camera=%s",
                self.camera_id,
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
                    datetime.now(UTC)
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