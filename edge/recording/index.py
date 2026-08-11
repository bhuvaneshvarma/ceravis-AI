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

    segments() -> every stored segment, chronological, with its true duration
    ranges()   -> the contiguous stretches (a person-present period) for the
                  timeline bar
    playlist() -> ONE HLS playlist over the whole retention window that POINTS
                  AT THOSE SAME FILES, tags each run with its real wall-clock
                  start (EXT-X-PROGRAM-DATE-TIME) and joins runs across the
                  empty gaps (EXT-X-DISCONTINUITY). The client loads it once and
                  seeks to any instant by date.

THE PLAYLIST IS A LIVE (SLIDING-WINDOW) MEDIA PLAYLIST — this is the whole
reason playback keeps itself current:

  * it NEVER carries EXT-X-ENDLIST. A player only stops reloading a media
    playlist once it sees ENDLIST, so leaving it off means every player
    (hls.js, AVPlayer, ExoPlayer) re-fetches this URL on its own every few
    seconds, forever, and picks up clips recorded AFTER the link was opened —
    with no new call from the app. An NVR archive is never "finished": the next
    person can walk in a second from now, so the manifest must never say it is.
  * because the retention window slides, segments also fall OFF the front. A
    live playlist is allowed to do that (an EXT-X-PLAYLIST-TYPE:EVENT one is
    not), but only if EXT-X-MEDIA-SEQUENCE / EXT-X-DISCONTINUITY-SEQUENCE
    advance by exactly what was dropped — that is how a player re-identifies
    the segments it already holds across a reload. Getting this wrong is not
    cosmetic: the player mis-aligns the reloaded list against its buffer and
    the picture jumps or seek-by-date drifts. `_sequence()` below keeps them
    honest.

Durations come from the next segment's start, which is exact inside a recording
run; a segment that was closed EARLY (the person left mid-segment) is measured
from its last write instead, so a run's real end — and therefore every
wall-clock anchor after it — stays exact. A gap between runs becomes an
EXT-X-DISCONTINUITY so players re-sync cleanly across it.
"""

import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from common import clock
from config.settings import settings


logger = logging.getLogger("media")

_EDGE_ROOT = Path(__file__).resolve().parents[1]

# MediaMTX recordPath template: %Y-%m-%d_%H-%M-%S-%f
_STEM = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})-(\d{6})$")

# Only the current recording format is served; stale files from an older format
# are ignored (they age out with the retention window).
SEGMENT_SUFFIX = ".ts"

# A segment MediaMTX is still writing has a just-updated mtime (it writes every
# few frames), so a last write older than this means the file is closed.
_OPEN_GRACE_SECS = 2.0

# Two segments are part of the same run when the next one starts (essentially)
# where the previous one ended. Tolerance covers rounding + writer jitter.
_RUN_GAP_SECS = 1.0


@dataclass
class Segment:
    start: datetime          # edge-local, timezone-aware
    duration: float          # seconds
    file: Path
    starts_run: bool         # a gap precedes it -> HLS discontinuity
    mtime: float             # last write (epoch) — the file's close time once final

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
    found: list[tuple[datetime, Path, float]] = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                if not e.name.lower().endswith(SEGMENT_SUFFIX):
                    continue
                start = _parse_start(e.name[:-len(SEGMENT_SUFFIX)])
                if start is None:
                    continue
                try:
                    mtime = e.stat().st_mtime
                except OSError:
                    continue
                found.append((start, Path(e.path), mtime))
    except OSError:
        return []
    found.sort(key=lambda sf: sf[0])

    nominal = float(settings.record_segment_secs)
    contiguous = nominal * 1.5          # bigger gap than this = the run ended
    out: list[Segment] = []
    prev_end: datetime | None = None
    for i, (start, f, mtime) in enumerate(found):
        to_next = ((found[i + 1][0] - start).total_seconds()
                   if i + 1 < len(found) else None)
        duration = to_next if (to_next is not None and to_next <= contiguous) else nominal
        # A file whose writing stopped well short of that is the LAST segment of
        # a run that ended early (the person left mid-segment). Its final write
        # IS its end, so mtime measures it exactly. Without this the run appears
        # up to a full segment longer than it is and the timeline over-reports.
        closed = mtime - start.timestamp()
        if 0.5 <= closed < duration - 1.0:
            duration = closed
        duration = round(duration, 3)
        # Run boundaries off the real END of the previous segment (not its
        # start), so a run that ended early is still seen as ended.
        starts_run = (prev_end is None
                      or (start - prev_end).total_seconds() > _RUN_GAP_SECS)
        seg = Segment(start, duration, f, starts_run, mtime)
        out.append(seg)
        prev_end = seg.end
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


# ---- sliding-window bookkeeping -------------------------------------------
# What we published last time, per recorded path: {file name: (msn, dsn)}.
# Self-pruning — it only ever holds the segments currently in the window.
_seq_lock = threading.Lock()
_seq_state: dict[str, dict[str, tuple[int, int]]] = {}


def _sequence(rec_path: str, segs: list[Segment], nominal: float) -> tuple[int, int]:
    """EXT-X-MEDIA-SEQUENCE / EXT-X-DISCONTINUITY-SEQUENCE for this playlist.

    A player identifies segments across a reload by their sequence number, so
    the number of the FIRST listed segment must grow by exactly the number of
    segments that fell off the front (RFC 8216 §6.2.2) — never restart at 0.
    We therefore remember the numbers we handed out and keep counting: a
    segment we published before keeps its number, a new one takes the next.

    The first segment we ever see anchors on wall clock (its start / segment
    length) instead of 0. Every dropped segment costs at least one segment
    length of elapsed time, so that anchor still only ever moves forward — the
    numbering stays monotonic across an edge restart too."""
    with _seq_lock:
        prev = _seq_state.get(rec_path, {})
        cur: dict[str, tuple[int, int]] = {}
        msn = dsn = 0
        for i, seg in enumerate(segs):
            name = seg.file.name
            if i == 0:
                msn, dsn = prev.get(
                    name,
                    (int(seg.start.timestamp() // max(nominal, 1.0)),
                     # never let a client see the discontinuity count go back
                     max((d for _, d in prev.values()), default=0)))
            else:
                msn += 1
                dsn += 1 if seg.starts_run else 0
            cur[name] = (msn, dsn)
        _seq_state[rec_path] = cur
        return cur[segs[0].file.name]


def playlist(rec_path: str, since: datetime, uri_prefix: str = "segment/",
             uri_suffix: str = "") -> str:
    """The one seekable HLS playlist over the STORED segments from `since`
    onward — the files themselves, never a copy. Empty string when there's no
    footage. Callers pass `since = now - retention` to get the WHOLE window as a
    single time-addressable timeline; the client then seeks to any moment by
    date rather than re-requesting per moment.

    Each run carries an EXT-X-PROGRAM-DATE-TIME wall-clock anchor and runs are
    separated by EXT-X-DISCONTINUITY, so seeking by date lands on the exact
    instant and playback rolls across the empty gaps on its own.

    It is a LIVE playlist: no EXT-X-ENDLIST, ever. That is the contract that
    makes the link self-updating — the player keeps reloading this URL by
    itself and appends footage recorded long after it was opened, whether or not
    anyone was being recorded when it was first fetched. Segments that age out
    of the retention window drop off the front, and MEDIA-SEQUENCE /
    DISCONTINUITY-SEQUENCE account for them so the player re-identifies what it
    already holds. The segment currently being written is never advertised: it
    is still open on disk, so handing it over would give the player a truncated
    file."""
    nominal = float(settings.record_segment_secs)
    now = clock.now().timestamp()
    segs = [s for s in segments(rec_path) if s.end > since]
    # Only the NEWEST file can still be open (any older one was closed the moment
    # its successor was created). Publish it once BOTH signals agree it is
    # closed: its last write has gone quiet AND its nominal length has elapsed,
    # after which MediaMTX has certainly rolled over or stopped. Requiring both
    # is what keeps a stalled camera — whose open file also goes quiet — from
    # putting a truncated segment in front of a player.
    if segs:
        newest = segs[-1]
        if ((now - newest.mtime) < _OPEN_GRACE_SECS
                or (now - newest.start.timestamp()) < nominal):
            segs.pop()
    if not segs:
        return ""

    msn, dsn = _sequence(rec_path, segs, nominal)
    target = max(1, math.ceil(max(s.duration for s in segs)))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        # Where this window starts in the stream's own numbering, and how many
        # discontinuities already scrolled past it. Both only ever go up.
        f"#EXT-X-MEDIA-SEQUENCE:{msn}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{dsn}",
    ]
    for i, seg in enumerate(segs):
        new_run = seg.starts_run and i > 0
        if new_run:
            lines.append("#EXT-X-DISCONTINUITY")
        # A real wall-clock anchor at the first segment and at the start of every
        # run (right after a gap). Players interpolate the exact time of every
        # segment in between from the EXTINF durations, so the client can seek to
        # ANY instant BY DATE (hls.js `playingDate`, iOS `seekToDate:`) and paint
        # true wall-clock time on the scrubber. This one tag is what turns a plain
        # HLS stream into a time-addressable NVR timeline.
        if i == 0 or new_run:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg.start.isoformat()}")
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        # uri_suffix carries the edge_id query onto every segment URI, so the
        # player (hls.js AND native HLS) authenticates each segment fetch too.
        lines.append(f"{uri_prefix}{seg.file.name}{uri_suffix}")
    # No EXT-X-ENDLIST — see the docstring: the list must stay open so players
    # keep reloading it and pick up footage recorded from here on.
    return "\n".join(lines) + "\n"
