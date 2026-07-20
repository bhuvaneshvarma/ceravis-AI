from __future__ import annotations

"""
The index over the recorded segments on disk — the missing metadata layer that
ties recording and playback together.

MediaMTX already writes exactly what HLS needs: self-contained MPEG-TS segments
of `record_segment_secs`, each NAMED with its own start time:

    data/recordings/<path>/2026-07-16_14-30-00-000000.ts
                           └─────────┬─────────┘
                              the segment's start instant (edge-local)

So there is nothing to re-cut for playback: this module reads that directory,
turns the filenames back into timestamps, and hands out

    segments() -> every stored segment, chronological, with its duration
    ranges()   -> the contiguous stretches (a person-present period) for the
                  timeline bar
    playlist() -> an HLS-VOD playlist that POINTS AT THOSE SAME FILES

Durations come from the next segment's start, which is exact inside a recording
run; the last segment of a run falls back to the nominal segment length (the
recorder may have stopped it early when the person left). A gap between runs
becomes an EXT-X-DISCONTINUITY so players re-sync cleanly across it.
"""

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import settings


logger = logging.getLogger("media")

_EDGE_ROOT = Path(__file__).resolve().parents[1]

# MediaMTX recordPath template: %Y-%m-%d_%H-%M-%S-%f
_STEM = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})-(\d{6})$")

# Only the current recording format is served; stale files from an older format
# are ignored (they age out with the retention window).
SEGMENT_SUFFIX = ".ts"


@dataclass
class Segment:
    start: datetime          # edge-local, timezone-aware
    duration: float          # seconds
    file: Path
    starts_run: bool         # a gap precedes it -> HLS discontinuity

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)


def record_root() -> Path:
    p = Path(settings.record_dir)
    return p if p.is_absolute() else (_EDGE_ROOT / p)


def is_segment_name(name: str) -> bool:
    """Whether a filename is one of our recorded segments (used to validate the
    name a player asks for, so nothing can escape the recordings directory)."""
    return name.endswith(SEGMENT_SUFFIX) and bool(_STEM.match(name[:-len(SEGMENT_SUFFIX)]))


def segment_file(rec_path: str, name: str) -> Path | None:
    """Resolve one stored segment for serving, or None if the name is not ours."""
    if not is_segment_name(name):
        return None
    f = record_root() / rec_path / name
    return f if f.is_file() else None


def _parse_start(stem: str) -> datetime | None:
    m = _STEM.match(stem)
    if not m:
        return None
    y, mo, d, h, mi, s, us = (int(x) for x in m.groups())
    try:
        # Naive .astimezone() reads the value as LOCAL time and attaches the
        # offset that applied ON THAT DATE — correct across a DST change, unlike
        # stamping today's offset onto an old segment.
        return datetime(y, mo, d, h, mi, s, us).astimezone()
    except ValueError:
        return None


def segments(rec_path: str) -> list[Segment]:
    """Every stored segment for a MediaMTX path, chronological."""
    folder = record_root() / rec_path
    found: list[tuple[datetime, Path]] = []
    try:
        for f in folder.iterdir():
            if f.suffix.lower() != SEGMENT_SUFFIX:
                continue
            start = _parse_start(f.stem)
            if start is not None:
                found.append((start, f))
    except OSError:
        return []
    found.sort(key=lambda sf: sf[0])

    nominal = float(settings.record_segment_secs)
    contiguous = nominal * 1.5          # bigger gap than this = the run ended
    out: list[Segment] = []
    for i, (start, f) in enumerate(found):
        to_next = ((found[i + 1][0] - start).total_seconds()
                   if i + 1 < len(found) else None)
        duration = to_next if (to_next is not None and to_next <= contiguous) else nominal
        from_prev = ((start - found[i - 1][0]).total_seconds() if i else None)
        starts_run = from_prev is None or from_prev > contiguous
        out.append(Segment(start, round(duration, 3), f, starts_run))
    return out


def ranges(rec_path: str) -> list[tuple[datetime, datetime]]:
    """Contiguous recorded stretches — one per period a person was present."""
    out: list[tuple[datetime, datetime]] = []
    run_start: datetime | None = None
    run_end: datetime | None = None
    for seg in segments(rec_path):
        if seg.starts_run and run_start is not None:
            out.append((run_start, run_end))
            run_start = None
        if run_start is None:
            run_start = seg.start
        run_end = seg.end
    if run_start is not None:
        out.append((run_start, run_end))
    return out


def playlist(rec_path: str, since: datetime, uri_prefix: str = "segment/") -> str:
    """An HLS-VOD playlist over the STORED segments from `since` onward — the
    files themselves, never a copy. Empty string when there's no footage.

    Playback begins at the start of the segment CONTAINING `since`, so the
    viewer naturally gets up to one segment of lead-up before the moment they
    asked for (the behaviour we want for an alert)."""
    segs = [s for s in segments(rec_path) if s.end > since]
    if not segs:
        return ""
    target = max(1, math.ceil(max(s.duration for s in segs)))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i, seg in enumerate(segs):
        if seg.starts_run and i > 0:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(f"{uri_prefix}{seg.file.name}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
