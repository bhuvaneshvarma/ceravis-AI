from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time

from datetime import datetime, timezone
from urllib.parse import urlparse

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

        # RTSP transport/latency, resolved per-connect by _resolve_rtsp().
        self._rtsp_transport: str = "tcp"
        self._rtsp_latency: int = int(settings.rtsp_latency_ms)

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

    # RTSP transport + jitter buffer, AUTO-DETECTED at connect time and never
    # persisted. A camera on a WIRED egress (clean, like direct Ethernet) gets
    # UDP + a low jitter buffer = minimal lag; a WIRELESS egress (lossy WiFi)
    # gets TCP, which re-sends lost packets and kills macroblock corruption. An
    # explicit per-camera override or a non-"auto" global wins over detection.
    def _resolve_rtsp(self) -> None:
        cam_t = (getattr(self._camera, "transport", None) or "").lower()
        cam_l = getattr(self._camera, "rtsp_latency_ms", None)
        glob = (settings.rtsp_transport or "auto").lower()

        if cam_t in ("tcp", "udp"):
            transport = cam_t
        elif glob in ("tcp", "udp"):
            transport = glob
        else:                                    # "auto"
            transport = self._auto_transport()

        if cam_l is not None:
            latency = int(cam_l)
        elif transport == "udp":
            latency = int(settings.rtsp_udp_latency_ms)
        else:
            latency = int(settings.rtsp_latency_ms)

        self._rtsp_transport, self._rtsp_latency = transport, latency

    def _auto_transport(self) -> str:
        """Wired egress -> 'udp' (low lag); wireless/unknown -> 'tcp' (safe)."""
        dev = self._egress_dev(self._camera_ip())
        wireless = self._is_wireless(dev)
        transport = "udp" if wireless is False else "tcp"
        logger.info("camera=%s auto RTSP transport=%s (egress=%s wireless=%s)",
                    self.camera_id, transport, dev, wireless)
        return transport

    def _camera_ip(self) -> str | None:
        host = urlparse(self._camera.rtsp_url).hostname
        if not host:
            return None
        try:
            return socket.gethostbyname(host)
        except Exception:
            return host                          # already an IP / unresolved

    @staticmethod
    def _egress_dev(ip: str | None) -> str | None:
        """Egress interface the kernel would use to reach `ip` (ip route get)."""
        if not ip:
            return None
        try:
            toks = subprocess.run(
                ["ip", "route", "get", ip],
                capture_output=True, text=True, timeout=2,
            ).stdout.split()
            if "dev" in toks:
                return toks[toks.index("dev") + 1]
        except Exception:
            pass
        return None

    @staticmethod
    def _is_wireless(dev: str | None) -> bool | None:
        if not dev:
            return None                          # unknown -> treat as wireless (safe TCP)
        if os.path.exists(f"/sys/class/net/{dev}/wireless"):
            return True
        return dev.startswith(("wl", "wlan"))    # name fallback

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
            f"protocols={self._rtsp_transport} "
            f"latency={self._rtsp_latency} drop-on-latency=true ! "
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
            f"protocols={self._rtsp_transport} "
            f"latency={self._rtsp_latency} drop-on-latency=true ! "
            f"{depay} ! videoconvert ! "
            f"video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false"
        )

    def _connect(self) -> bool:

        self._health_state = (
            CameraHealthState.CONNECTING
        )

        # Auto-detect transport/latency for THIS camera (wired->udp, wifi->tcp),
        # then make the FFmpeg fallback use the same (set per-connect so two
        # cameras with different transports don't clobber each other).
        self._resolve_rtsp()
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            f"rtsp_transport;{self._rtsp_transport}"
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