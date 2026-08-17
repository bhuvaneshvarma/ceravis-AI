from __future__ import annotations

"""
Decodes ONE camera's stream into the FrameBuffer for the AI pipeline.

With the MediaMTX backbone up, the source is the rock-solid localhost
restream (rtsp://127.0.0.1:8554/<camera>) — MediaMTX owns the actual camera
connection, its flaky WiFi transport and its reconnects, and fans the same
compressed stream out to live view / recording without re-encoding. This
reader is then purely: pull loopback RTSP -> hardware-decode -> BGR frames.

These frames feed the AI and nothing else: no viewer is ever served from
here, so the path is tuned solely for handing YOLO the freshest frame — no
jitterbuffer, no pacing, no queue (see _gst_pipeline).

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
from datetime import datetime

import cv2

from common import clock
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
        self._watchdog_thread: threading.Thread | None = None
        self._running = False

        self._frame_id = 0
        self._frames_captured = 0
        self._reconnect_count = 0
        self._last_frame_time: datetime | None = None
        # Monotonic instant of the last delivered frame (and of each connect), for
        # the stall watchdog — monotonic so a clock step can't misfire it. 0 until
        # the first connection, so the watchdog stays disarmed before then.
        self._last_frame_monotonic = 0.0
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
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, daemon=True, name=f"rtsp-wd-{self.camera_id}")
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._running = False
        self._health_state = CameraHealthState.OFFLINE
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                logger.exception("Capture release failed camera=%s", self.camera_id)

    def _watchdog(self) -> None:
        """Self-heal the SILENT stall: a loopback session that stops delivering
        frames while cv2.read() stays blocked (no error, no EOS) — so the read
        loop is stuck INSIDE read() and its post-read timeout never runs, and the
        live view keeps working (MediaMTX still serves the path) while the AI
        starves. This independent thread notices frames have stopped and releases
        the capture; that unblocks the hung read(), the main loop sees the failed
        read and reconnects on its own. No manual restart. Armed only while
        RUNNING and after the first connection, so a normal first-frame wait is
        never cut short."""
        timeout = max(1.0, settings.camera_stall_reconnect_secs)
        while self._running:
            time.sleep(1.0)
            if self._health_state != CameraHealthState.RUNNING:
                continue
            last = self._last_frame_monotonic
            if not last:
                continue
            idle = time.monotonic() - last
            if idle <= timeout:
                continue
            logger.warning(
                "camera=%s STALLED — %.1fs with no frame while the backbone still "
                "serves the path; forcing a reader reconnect", self.camera_id, idle)
            # Re-arm the window first so we don't fire again during the reconnect.
            self._last_frame_monotonic = time.monotonic()
            cap = self._capture
            if cap is not None:
                try:
                    cap.release()          # unblocks the hung read() -> reconnect
                except Exception:
                    logger.exception("watchdog release failed camera=%s",
                                     self.camera_id)

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
        # ZERO added buffering between the camera and YOLO, on purpose:
        #   latency=0        the loopback pull is interleaved TCP — every packet
        #                    arrives, in order, so the jitterbuffer has nothing
        #                    to absorb and any depth is pure delay. (MediaMTX
        #                    already handled the real network jitter camera-side.)
        #   NO drop-on-latency — the flag DROPS packets when reassembly exceeds
        #                    the budget, which is exactly what movement's big
        #                    P-frames do: incomplete frames the AI reads as noise.
        #   appsink drop=true max-buffers=1 — freshness is owned here: always the
        #                    newest COMPLETE frame, backlog discarded.
        # So the reader is integrity AND immediacy, not a trade-off between them.
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
            logged_size = False        # announce the real resolution once per connect
            # Arm the stall watchdog from the connection instant, so "connected but
            # never delivered a first frame" is caught too, not just mid-stream stalls.
            self._last_frame_monotonic = time.monotonic()

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

                if not logged_size:
                    # What the AI is REALLY being fed. A camera configured with
                    # its sub-stream URL looks perfectly healthy on every other
                    # metric, so the resolution has to be said out loud.
                    logged_size = True
                    logger.info("camera=%s decoding %dx%d", self.camera_id,
                                frame.shape[1], frame.shape[0])

                self._frame_id += 1
                self._frames_captured += 1
                # Stamp on the ONE edge clock (common.clock, device-local and
                # tz-aware) — the same clock alerts/snapshots/recordings use and
                # that the camera OSD is NTP-disciplined to, so a frame's time
                # reads the same everywhere. This is the frame ARRIVAL instant;
                # the true CAPTURE instant is earlier by the (LAN, ~tens of ms)
                # transport delay, which is below one frame interval and cannot be
                # recovered from OpenCV (no RTP/RTCP capture time is exposed).
                self._last_frame_time = clock.now()
                self._last_frame_monotonic = time.monotonic()   # feed the watchdog
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
