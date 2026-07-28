from __future__ import annotations

"""
Still-frame snapshot for the mobile / cloud live view — the PHOTO twin of
playback.m3u8.

The mobile app plays the camera over WebRTC and can't grab a still from that
video surface (hardware-decoded / tainted canvas), so the edge produces the
still server-side. Two tiers, ONE timestamp authority — the device's local NTP
clock (common.clock), which the camera's burned-in OSD is disciplined to, so the
instant we report lines up with the time painted into the pixels:

    live      -> the freshest decoded frame. The in-memory ingestion FrameBuffer
                 when it is fresh (zero decode, lowest possible latency); else a
                 one-shot grab off the MediaMTX loopback (always live, even for a
                 camera the AI has idled under active_camera_only). This is what
                 the "snapshot" button calls — it captures ~now.

    recording -> a frame-accurate still pulled from the STORED segment that
                 covers a requested PAST instant (ffmpeg seek). Exact time match
                 within the retention window — the still equivalent of seeking
                 the playback timeline. Only exists where footage exists (we
                 record on person-detection), so an empty stretch has no still.

No parallel state is introduced: the live tier reuses the ingestion FrameBuffer
and the shared common.rtsp one-shot grab; the recording tier reuses
recording.index to map an instant to its segment + offset — the SAME index that
builds the playlist, so a still and the timeline can never disagree.
"""

import logging
import subprocess
from datetime import datetime, timedelta

from common import clock
from config.settings import settings
from livestream.mediamtx_client import local_rtsp_url, record_path_name
from recording import index as recording_index


logger = logging.getLogger("media")

# A buffered frame older than this counts as stale for a live snapshot, so we
# re-grab rather than hand back a frozen frame from an AI-idled camera. Tied to
# the same freshness bound the rest of the pipeline uses.
_LIVE_FRESH_SECS = float(settings.frame_stale_secs)


class SnapshotError(Exception):
    """A snapshot could not be produced (no frame / no footage / encode fail)."""


def _encode_jpeg(frame, quality: int) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", frame,
                           [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise SnapshotError("JPEG encode failed")
    return buf.tobytes()


def live_snapshot(camera, frame_buffer, *, mediamtx_active: bool,
                  quality: int = 80) -> tuple[bytes, datetime, str]:
    """A JPEG of the camera RIGHT NOW, with the instant it represents.

    Prefers the freshest buffered frame (in-memory, no decode) and falls back to
    a one-shot live grab so a camera the AI isn't actively processing still
    yields a current still. Returns (jpeg_bytes, actual_time, source)."""
    frame = frame_buffer.get(camera.camera_id) if frame_buffer else None
    if frame is not None:
        age = (clock.now() - frame.timestamp.astimezone()).total_seconds()
        if age <= _LIVE_FRESH_SECS:
            # frame.timestamp is the edge capture instant (UTC-aware); report it
            # in device-local time so it matches the OSD and every other stamp.
            return (_encode_jpeg(frame.frame, quality),
                    frame.timestamp.astimezone(clock.local_tz()), "live")

    # No fresh buffered frame (idled camera / just started) — pull one live frame
    # straight off the backbone. Loopback when MediaMTX is up (one camera pull
    # shared with the AI + WebRTC); the camera directly on a dev box without it.
    from common.rtsp import grab_one_frame
    source = local_rtsp_url(camera.camera_id) if mediamtx_active else \
        (camera.rtsp_url or "")
    grabbed = grab_one_frame(source, timeout_secs=settings.read_timeout_secs + 5)
    if grabbed is None:
        raise SnapshotError("camera did not yield a live frame")
    return _encode_jpeg(grabbed, quality), clock.now(), "live"


def _segment_covering(camera, ts: datetime):
    """The stored segment whose [start, end) contains `ts`, or None."""
    for seg in recording_index.segments(record_path_name(camera)):
        if seg.start <= ts < seg.end:
            return seg
    return None


def archive_snapshot(camera, ts: datetime,
                     quality: int = 80) -> tuple[bytes, datetime, str]:
    """A frame-accurate JPEG at a PAST instant, decoded from the stored segment
    that covers it. Raises SnapshotError if no footage covers `ts` (an empty
    stretch — nobody was present) or ffmpeg is unavailable. Returns
    (jpeg_bytes, actual_time, "recording")."""
    seg = _segment_covering(camera, ts)
    if seg is None:
        raise SnapshotError("no footage recorded at that instant")
    offset = max(0.0, (ts - seg.start).total_seconds())
    # Output-seek (-ss AFTER -i) is frame-accurate: it decodes the (short, 15s)
    # segment to `offset` and emits exactly that frame, so the still lands on the
    # requested instant, not the nearest prior keyframe. One MJPEG frame to
    # stdout — no temp file, no re-encode of anything else.
    q = max(2, min(31, round(31 - (int(quality) / 100.0) * 29)))  # 0-100 -> ffmpeg 31..2
    cmd = [settings.ffmpeg_binary, "-v", "error", "-nostdin",
           "-i", str(seg.file), "-ss", f"{offset:.3f}",
           "-frames:v", "1", "-q:v", str(q), "-f", "mjpeg", "pipe:1"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=20)
    except FileNotFoundError:
        raise SnapshotError("ffmpeg not available for archive snapshots")
    except subprocess.SubprocessError as exc:
        raise SnapshotError(f"ffmpeg failed: {exc}")
    if out.returncode != 0 or not out.stdout:
        raise SnapshotError((out.stderr or b"ffmpeg produced no frame")
                            .decode("utf-8", "replace").strip()[:200]
                            or "ffmpeg produced no frame")
    return out.stdout, seg.start + timedelta(seconds=offset), "recording"
