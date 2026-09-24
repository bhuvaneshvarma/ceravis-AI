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

Via MediaMTX the reader opens its path only once MediaMTX is RECEIVING it,
with the depayloader for the codec negotiated on that path; direct reads try
both. Every open is time-bounded: a GStreamer open inside OpenCV can block
forever (2026-09-23: at boot, with the cameras still joining the hotspot, a
cascade of failed opens left both readers stuck in open() for 7 hours — the AI
got no frame, so nothing was detected or recorded, silently).
"""

import logging
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import cv2

from common import clock
from config.settings import settings
from ingestion.camera_status import CameraHealthState
from ingestion.frame_buffer import FrameBuffer
from schemas.cameras import Camera


logger = logging.getLogger("ingestion")

# Longest a single capture open may take. A healthy loopback open prerolls on
# the first keyframe (~1-2 s at these cameras' GOP); past this it is hung.
_OPEN_TIMEOUT_SECS = 20.0
# How often a reader re-asks MediaMTX whether its path is receiving yet — a
# local HTTP call, so it is cheap and the AI starts within seconds of the camera.
_READY_POLL_SECS = 2.0
_SKIP_CHECK_SECS = 10.0    # window over which a decoder skip must still feed the AI


def _ai_fps() -> float:
    """The most frames per second any AI stage takes from a camera."""
    return max(settings.detection_fps, settings.pose_fps, 1.0)
# Longest any caller waits on a capture release. Releasing is also the only way
# to unblock a read() stuck on a silent stream, so it has to happen from another
# thread — and a GStreamer capture torn down while its reader is inside read()
# can block in release() itself. On the bench (2026-09-23 19:20) that pinned the
# shutdown thread until systemd SIGKILLed the service, MediaMTX and the
# recorders 90 s later. Past this bound the release is abandoned, not awaited.
_RELEASE_TIMEOUT_SECS = 3.0


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
        self._mtx_path = (urlparse(self._source_url).path.lstrip("/")
                          if self._via_mediamtx else None)
        # An open abandoned after _OPEN_TIMEOUT_SECS that has STILL not returned.
        # While it lives, GStreamer is not tried again for this camera (at most
        # one stuck thread per camera, never a pile-up).
        self._hung_open: threading.Thread | None = None
        self._was_ready: bool | None = None     # last logged path readiness
        self._decode_every: int | None = None   # hw decoder frame skip, learned once

        self._capture: cv2.VideoCapture | None = None
        # The capture already handed to _release — held so stop(), the watchdog
        # and the read loop never release the same one twice, concurrently.
        self._released_cap: cv2.VideoCapture | None = None
        self._release_lock = threading.Lock()
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
        self._release(self._capture, "stop")

    def _release(self, cap: cv2.VideoCapture | None, why: str) -> None:
        """Release `cap` once — whichever of stop / watchdog / the read loop gets
        there first — on a helper thread, waiting at most _RELEASE_TIMEOUT_SECS.
        A release that does not come back is abandoned (logged), so no caller —
        least of all the shutdown path — can be held by a wedged pipeline."""
        if cap is None:
            return
        with self._release_lock:
            if cap is self._released_cap:
                return
            self._released_cap = cap
        done = threading.Event()

        def work() -> None:
            try:
                cap.release()
            except Exception:
                logger.exception("capture release (%s) failed camera=%s",
                                 why, self.camera_id)
            finally:
                done.set()

        threading.Thread(target=work, daemon=True,
                         name=f"rtsp-release-{self.camera_id}").start()
        if not done.wait(_RELEASE_TIMEOUT_SECS):
            logger.error("camera=%s capture release (%s) HUNG for %.0fs — "
                         "abandoned; carrying on", self.camera_id, why,
                         _RELEASE_TIMEOUT_SECS)

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
            self._release(self._capture, "stall")   # unblocks the hung read()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- connection -------------------------------------------------------
    def _gst_pipeline(self, codec: str, hw: bool) -> str:
        depay = ("rtph265depay ! h265parse" if codec == "h265"
                 else "rtph264depay ! h264parse")
        skip = self._decode_every or 1
        decode = (("nvv4l2decoder" + (f" drop-frame-interval={skip}" if skip > 1 else ""))
                  + " ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert"
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

    def _open(self, name: str, pipeline: str | None) -> cv2.VideoCapture | None:
        """Open a capture, but never unboundedly: the open runs on a helper
        thread and is ABANDONED after _OPEN_TIMEOUT_SECS (logged loudly). If the
        abandoned open ever returns, it releases its own capture."""
        box: dict = {}
        lock = threading.Lock()
        done = threading.Event()

        def work() -> None:
            cap = None
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
            with lock:
                box["cap"] = cap
                done.set()
                abandoned = box.get("abandoned")
            if abandoned and cap is not None:
                cap.release()

        t = threading.Thread(target=work, daemon=True,
                             name=f"rtsp-open-{self.camera_id}")
        t.start()
        done.wait(_OPEN_TIMEOUT_SECS)
        with lock:
            if done.is_set():
                return box["cap"]
            box["abandoned"] = True
        self._hung_open = t
        logger.error("camera=%s open via %s HUNG for %.0fs — abandoned; the reader "
                     "carries on", self.camera_id, name, _OPEN_TIMEOUT_SECS)
        return None

    def _connect(self, codec: str | None = None) -> bool:
        self._health_state = CameraHealthState.CONNECTING
        # The plain-FFmpeg fallback honors this env var; interleaved TCP avoids
        # UDP loss artifacts on every link we use (loopback or direct).
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

        codecs = [codec] if codec else ["h264", "h265"]
        # hw GStreamer first (per codec), then sw GStreamer, then plain FFmpeg —
        # so ingestion still works where the NVIDIA plugins aren't available.
        # A GStreamer open still stuck from an earlier attempt means GStreamer is
        # not trusted for this camera until it returns: FFmpeg only.
        gst_ok = self._hung_open is None or not self._hung_open.is_alive()
        attempts: list[tuple[str, str | None]] = []
        if settings.is_production and gst_ok:
            attempts += [(f"hw-gst-{c}", self._gst_pipeline(c, hw=True)) for c in codecs]
            attempts += [(f"sw-gst-{c}", self._gst_pipeline(c, hw=False)) for c in codecs]
        attempts.append(("ffmpeg", None))

        for name, pipeline in attempts:
            cap = self._open(name, pipeline)
            if cap is not None and cap.isOpened():
                if name.startswith("hw-gst") and self._learn_decode_skip(cap):
                    self._release(cap, "decode skip")   # reopen with the skip applied
                    return self._connect(codec)
                self._capture = cap
                self._health_state = CameraHealthState.RUNNING
                logger.info("Connected camera=%s via %s (%s)", self.camera_id, name,
                            "mediamtx" if self._via_mediamtx else "direct")
                return True
            if cap is not None:
                cap.release()
                logger.warning("Open via %s failed camera=%s", name, self.camera_id)
        return False

    def _learn_decode_skip(self, cap: cv2.VideoCapture) -> bool:
        """Once per reader: let the hardware decoder hand over only every Nth
        frame when the camera sends far more than the AI takes (at most
        max(detection_fps, pose_fps)). Every frame handed over is copied out of
        video memory and colour-converted on the CPU whether or not anyone uses
        it — measured 2026-09-24: a 25 fps 4K camera cost 0.82 of a core, 0.46
        at every 2nd frame. An unreported camera rate (0) skips nothing. True
        when the capture must be reopened for the skip to apply."""
        if self._decode_every is not None:
            return False
        src = cap.get(cv2.CAP_PROP_FPS) or 0.0
        need = _ai_fps()
        self._decode_every = max(1, int(src // need))
        if self._decode_every > 1:
            logger.info("camera=%s decoding every %d frames (camera %.0f fps, AI needs %.0f)",
                        self.camera_id, self._decode_every, src, need)
        return self._decode_every > 1

    def _wait_for_source(self) -> tuple[bool, str | None]:
        """Via MediaMTX: (ready, codec) of this reader's path. Direct: always
        ready, codec unknown. Logs each ready/not-ready TRANSITION once."""
        if not self._via_mediamtx:
            return True, None
        from livestream.mediamtx_client import source_state
        ready, codec = source_state(self._mtx_path)
        if ready != self._was_ready:
            self._was_ready = ready
            if ready:
                logger.info("camera=%s MediaMTX is receiving %s (%s) — opening",
                            self.camera_id, self._mtx_path, codec or "codec unknown")
            else:
                logger.warning("camera=%s waiting — MediaMTX is not receiving %s "
                               "yet (camera offline/joining, or MediaMTX down)",
                               self.camera_id, self._mtx_path)
        return ready, codec

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
            ready, codec = self._wait_for_source()
            if not ready:
                self._health_state = CameraHealthState.RECONNECTING
                time.sleep(_READY_POLL_SECS)
                continue
            if not self._connect(codec):
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
            skip_window, skip_frames = time.monotonic(), 0

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
                # The camera's reported rate is nominal: in low light it sends
                # far fewer frames (the C260: 25 -> ~7.6 fps at night), and a skip
                # learned from 25 then starved the AI at 3.8 fps (2026-09-24).
                # A skip that no longer feeds the AI is dropped for good.
                skip_frames += 1
                span = time.monotonic() - skip_window
                if (self._decode_every or 1) > 1 and span >= _SKIP_CHECK_SECS:
                    if skip_frames / span < 0.9 * _ai_fps():
                        logger.warning("camera=%s delivers %.1f fps while decoding every %d "
                                       "frames (camera slowed down, e.g. low light) — "
                                       "decoding every frame from now on", self.camera_id,
                                       skip_frames / span, self._decode_every)
                        self._decode_every = 1
                        break
                    skip_window, skip_frames = time.monotonic(), 0
                self._frame_buffer.update(
                    camera_id=self.camera_id, frame=frame,
                    frame_id=self._frame_id, timestamp=self._last_frame_time,
                    fps=self.current_fps,
                )
                self._log_stats()

            self._frame_buffer.clear(self.camera_id)
            self._release(self._capture, "reconnect")
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
