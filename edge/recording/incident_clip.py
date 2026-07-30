from __future__ import annotations

"""
Merge a short incident video clip out of the already-recorded segments.

On a fall the alert + still snapshot fire immediately; this stitches the MOVING
footage around the instant — every stored segment overlapping [at - pre, at + post]
(typically the one before, the one containing the fall, and the one after) — into
a single MP4 so a reviewer sees the fall itself, not just a frame.

The stored files are self-contained MPEG-TS segments (see recording/index), so
they are concatenated with `-c copy`: NO re-encode, nothing touches the Orin
encoder. Best-effort by design — returns None when ffmpeg is missing or no
footage covers the window (recording was off, or nobody was in frame), so the
caller simply skips the clip; the alert and snapshot have already gone out.
"""

import logging
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import settings
from livestream.mediamtx_client import ffmpeg_available
from recording.index import segments


logger = logging.getLogger("media")


def _overlapping(rec_path: str, start: datetime, end: datetime) -> list:
    """Stored segments that overlap the window [start, end] (a segment overlaps
    when it begins before the window ends and ends after the window begins)."""
    return [s for s in segments(rec_path) if s.start < end and s.end > start]


def build_incident_clip(rec_path: str, at: datetime, pre_secs: float,
                        post_secs: float) -> bytes | None:
    """The MP4 bytes for the footage around `at`, or None when it can't be built.

    `rec_path` is the camera's recorded MediaMTX path (record_path_name); `at` is
    the incident instant (timezone-aware, edge-local). Segments overlapping
    [at - pre, at + post] are concatenated in order with no re-encode."""
    if not ffmpeg_available():
        logger.warning("fall clip skipped — ffmpeg not on this device")
        return None
    window_start = at - timedelta(seconds=max(0.0, pre_secs))
    window_end = at + timedelta(seconds=max(0.0, post_secs))
    segs = _overlapping(rec_path, window_start, window_end)
    if not segs:
        logger.info("fall clip: no footage for %s in [%s, %s]",
                    rec_path, window_start.isoformat(), window_end.isoformat())
        return None

    # concat demuxer + a list file: safe for any path, keeps segment order, and
    # -c copy stays off the encoder; +faststart makes the MP4 seek instantly in
    # a browser. The TS->MP4 Annex-B/AVCC fix is applied by the muxer on its own.
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "segments.txt"
        listing.write_text(
            "".join(f"file '{s.file.as_posix()}'\n" for s in segs),
            encoding="utf-8")
        out = Path(tmp) / "incident.mp4"
        cmd = [settings.ffmpeg_binary, "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(listing),
               "-c", "copy", "-movflags", "+faststart", str(out)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            data = out.read_bytes()
        except (subprocess.SubprocessError, OSError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            logger.warning("fall clip merge failed (%s): %s", rec_path,
                           detail.decode("utf-8", "replace")[:300] or exc)
            return None
    if not data:
        return None
    logger.info("fall clip built: %s segment(s), %d bytes (%s)",
                len(segs), len(data), rec_path)
    return data
