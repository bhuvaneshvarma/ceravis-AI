from __future__ import annotations

"""
Person-triggered local recording.

Watches the DetectionBuffer (the same YOLO output the tracker consumes) and
flips MediaMTX recording per camera:

    person detected on a camera  -> record ON  (MPEG-TS segments of the camera's
                                    MAIN stream at native quality — remux only,
                                    no re-encode, no second camera connection)
    nobody for POST_ROLL seconds -> record OFF

Completely independent of alerts/snapshots — this is the playback archive the
ceravishealth frontend browses via /api/v1/recordings. Detections older than
a couple of poll intervals are treated as "nobody" (a camera the AI focus has
idled produces no fresh results, so its recording winds down naturally).
"""

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from config.settings import settings
from configuration.camera_config import CameraConfig
from detection.detection_buffer import DetectionBuffer
from livestream import mediamtx_client
from common import clock
from livestream.mediamtx_client import (
    MediaMTXError, path_codec, record_path_name, recorded_path_names,
)


logger = logging.getLogger("media")

_EDGE_ROOT = Path(__file__).resolve().parents[1]

# a detection result older than this is stale — its camera counts as empty
_FRESH_SECS = 3.0

# how often to re-ask what codec a camera is really sending, so a camera
# reconfigured while we run starts (or stops) being recorded on its own
_CODEC_RECHECK_SECS = 60.0


def _record_root() -> Path:
    root = Path(settings.record_dir)
    return root if root.is_absolute() else (_EDGE_ROOT / root)


class RecordingController:
    def __init__(self, detections: DetectionBuffer) -> None:
        self._detections = detections
        self._cameras = CameraConfig()
        self._running = False
        self._thread: threading.Thread | None = None
        self._recording: dict[str, bool] = {}      # camera_id -> currently recording
        self._last_person: dict[str, float] = {}   # camera_id -> monotonic time
        # camera_id -> (checked_at_monotonic, is_h264). Recording is a REMUX, so
        # the clip inherits the camera's codec; see _recordable().
        self._codec_seen: dict[str, tuple[float, bool]] = {}
        # Runtime ON/OFF (monitor button) — persisted so a deliberate "stop
        # filling the disk" choice survives restarts. Default from the env.
        self._lock = threading.Lock()
        base = settings.data_path
        base = base if base.is_absolute() else (_EDGE_ROOT / base)
        self._mode_file = base / "recording_mode.json"
        self._enabled = self._load_enabled()

    # ---- runtime enable/disable (monitor button) ---------------------
    def _load_enabled(self) -> bool:
        try:
            return bool(json.loads(self._mode_file.read_text())["enabled"])
        except Exception:
            return bool(settings.record_enabled)

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, on: bool) -> bool:
        """Flip recording; turning OFF also stops every active recording now."""
        with self._lock:
            self._enabled = bool(on)
            try:
                self._mode_file.parent.mkdir(parents=True, exist_ok=True)
                self._mode_file.write_text(json.dumps({"enabled": self._enabled}))
            except Exception:
                logger.exception("recording mode persist failed")
        if not on:
            self._stop_all()
        logger.info("recording %s (runtime toggle)", "ENABLED" if on else "DISABLED")
        return self.is_enabled()

    def recording_now(self) -> list[str]:
        return [cam for cam, on in self._recording.items() if on]

    # ---- live health (status tooling) --------------------------------
    def health(self) -> list[dict]:
        """Per-camera recording health for the status tools. Verifies that a
        camera which SHOULD be recording is actually landing segments on disk —
        a camera flagged recording but producing no fresh file is a write fault
        (bad path, disk full, MediaMTX hiccup), the kind of silent failure an
        NVR alarms on."""
        out: list[dict] = []
        root = _record_root()
        for cam in self._cameras.get_all():
            path = record_path_name(cam)
            recording = self._recording.get(cam.camera_id, False)
            newest, count_1h, bytes_1h = self._segment_stats(root / path)
            age = (time.time() - newest) if newest else None
            # recording but no fresh segment within ~3 segment durations = fault
            gap_limit = max(30.0, settings.record_segment_secs * 3)
            writing_ok = (not recording) or (age is not None and age <= gap_limit)
            out.append({
                "camera_id": cam.camera_id,
                "path": path,
                "recording": recording,
                # False = deliberately NOT recording: the codec is unplayable
                "recordable": self._codec_seen.get(cam.camera_id, (0, True))[1],
                "newest_segment_age_secs": round(age, 1) if age is not None else None,
                "segments_last_hour": count_1h,
                "megabytes_last_hour": round(bytes_1h / 1e6, 1),
                "writing_ok": writing_ok,
            })
        return out

    # ---- start-up housekeeping ---------------------------------------
    def _prune_orphans(self) -> None:
        """Delete recording folders no live camera writes to any more.

        MediaMTX's `recordDeleteAfter` only expires footage under paths it
        currently knows about, so footage left behind when a path name changes
        (an earlier release recorded a separate `<cam>-rec-aac` sub-stream; a
        removed camera does the same) would sit on disk forever. Runs once at
        start-up, and deletes only a folder that BOTH no camera writes to and
        whose newest file is already past the retention window — so it can never
        touch footage still inside the window a viewer might be playing.

        Two guards make it safe rather than clever: a recorded path can contain
        a slash (the no-ffmpeg fallback records the live `<edge_id>/<cam>` path
        straight), so a top-level entry may be a PARENT of a live folder rather
        than a stale one — those are skipped whole, deliberately leaving any
        orphan nested inside them rather than risking a live tree."""
        root = _record_root()
        if not root.is_dir():
            return
        live: set[Path] = set()
        for cam in self._cameras.get_all():
            live |= {root / name for name in recorded_path_names(cam)}
        cutoff = time.time() - settings.record_retention_hours * 3600
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            # in use, or an ancestor of something in use
            if any(d == folder or folder in d.parents for d in live):
                continue
            newest, _, _ = self._segment_stats(folder)
            if newest is not None and newest > cutoff:
                continue                       # still inside the window — leave it
            try:
                shutil.rmtree(folder)
                logger.info("pruned orphaned recording folder: %s", folder.name)
            except OSError as exc:
                logger.warning("could not prune %s: %s", folder.name, exc)

    def _recordable(self, camera_id: str) -> bool:
        """Whether footage from this camera would be PLAYABLE if we wrote it.

        Recording is a straight remux, so a clip inherits the camera's codec —
        and playback is in-browser, where H.265 does not decode. Writing HEVC
        segments is worse than writing nothing: it burns disk (hundreds of MB an
        hour) and leaves an operator believing there is footage to review, right
        up until the moment they need it and get a black player. So a camera we
        cannot play back is simply not recorded, loudly, and /system/status
        already alarms on the codec.

        The codec is re-checked periodically rather than once at start-up: a
        camera can be reconfigured while we are running, and the answer should
        follow it without a restart. An unknown codec (path not ready yet) counts
        as recordable — never withhold footage on missing information."""
        now = time.monotonic()
        seen = self._codec_seen.get(camera_id)
        if seen and (now - seen[0]) < _CODEC_RECHECK_SECS:
            return seen[1]
        codec = path_codec(camera_id)
        ok = codec is None or codec == "h264"
        if not seen or seen[1] != ok:
            if ok:
                logger.info("camera=%s codec=%s — recording enabled",
                            camera_id, codec or "unknown")
            else:
                logger.warning(
                    "camera=%s streams %s — NOT recording it. Clips are remuxed "
                    "as-is and no browser can play %s, so the footage would be "
                    "unreviewable. Point this camera at an H.264 profile.",
                    camera_id, codec.upper(), codec.upper())
        self._codec_seen[camera_id] = (now, ok)
        return ok

    @staticmethod
    def _segment_stats(folder: Path) -> tuple[float | None, int, int]:
        """(newest_mtime_epoch|None, count_last_hour, bytes_last_hour) for a
        recorded path's segment files."""
        try:
            files = list(folder.glob("*.mp4")) + list(folder.glob("*.ts"))
        except OSError:
            return None, 0, 0
        if not files:
            return None, 0, 0
        hour_ago = time.time() - 3600
        newest = 0.0
        count = 0
        total = 0
        for f in files:
            try:
                stat = f.stat()
            except OSError:
                continue
            newest = max(newest, stat.st_mtime)
            if stat.st_mtime >= hour_ago:
                count += 1
                total += stat.st_size
        return (newest or None), count, total

    def start(self) -> None:
        for step in (self._prune_orphans,):
            try:
                step()
            except Exception:                  # housekeeping never blocks start
                logger.exception("recording start-up check failed: %s", step.__name__)
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="recording-controller")
        self._thread.start()
        logger.info("Recording on person detection — native main stream, %ds "
                    "segments, %.0fs post-roll, keep %dh, enabled=%s",
                    settings.record_segment_secs, settings.record_post_roll_secs,
                    settings.record_retention_hours, self.is_enabled())

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- loop ----------------------------------------------------------
    def _run(self) -> None:
        while self._running:
            try:
                if self.is_enabled():
                    self._tick()
                else:
                    self._stop_all()          # idempotent — no-op once all off
            except Exception:
                logger.exception("recording tick failed")
            time.sleep(settings.record_poll_secs)
        self._stop_all()                       # service stopping — close segments

    def _stop_all(self) -> None:
        for cam, on in list(self._recording.items()):
            if on:
                self._set(cam, False)
        self._last_person.clear()

    def _tick(self) -> None:
        now = time.monotonic()
        wall = clock.now()
        for cam, result in self._detections.get_all().items():
            fresh = (wall - result.timestamp).total_seconds() <= _FRESH_SECS
            if fresh and result.detections:          # YOLO only emits persons
                self._last_person[cam] = now
            last = self._last_person.get(cam)
            want = last is not None and \
                (now - last) <= settings.record_post_roll_secs
            if want and not self._recordable(cam):
                want = False               # unplayable codec — see _recordable
            if want != self._recording.get(cam, False):
                self._set(cam, want)

    def _set(self, camera_id: str, on: bool) -> None:
        # One path per camera: the AAC republish of its main stream (or the main
        # stream itself when ffmpeg is absent). Native quality either way.
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
