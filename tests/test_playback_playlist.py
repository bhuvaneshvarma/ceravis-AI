#!/usr/bin/env python3
"""
Sanity check for the playback playlist (recording/index) — the self-updating
HLS timeline the app plays back.

It builds a throw-away recordings directory of fake segment FILES (the real
names + a real mtime — the two things the index reads), then asserts the
manifest the edge would serve:

  * two runs with a gap between them -> EXT-X-DISCONTINUITY + a re-anchored
    EXT-X-PROGRAM-DATE-TIME on the second run
  * the segment still being written is NOT advertised, and appears in the very
    next build once its writing stops
  * NO EXT-X-ENDLIST, ever — that is what makes every player keep reloading the
    URL and pick up newer clips with no second call
  * a segment ageing off the front advances EXT-X-MEDIA-SEQUENCE by exactly one
    (and EXT-X-DISCONTINUITY-SEQUENCE by the discontinuities that went with it),
    so a player re-identifies what it already holds across a reload
  * a run that ended early is measured to its real length, not a full segment

Pure filesystem + string assertions: no camera, no MediaMTX, no network — runs
on the dev box as well as on the device.

    python tests/test_playback_playlist.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))

from config.settings import settings          # noqa: E402
from recording import index                   # noqa: E402


SEG = int(settings.record_segment_secs)       # nominal segment length


def write_segment(folder: Path, start: datetime, *, length: float | None = None,
                  open_now: bool = False) -> Path:
    """One fake segment file. The index reads two things off it: the start time
    in the NAME, and the last-write time — which is when the recorder closed it,
    i.e. `start + length`. `open_now` leaves that write time at this instant,
    which is exactly how a segment still being written looks on disk."""
    f = folder / (start.strftime("%Y-%m-%d_%H-%M-%S-") + f"{start.microsecond:06d}.ts")
    f.write_bytes(b"\x47" + b"\0" * 187)      # one TS packet — content is irrelevant
    closed = (datetime.now().timestamp() if open_now
              else start.timestamp() + (SEG if length is None else length))
    os.utime(f, (closed, closed))
    return f


def build(folder: Path) -> str:
    since = datetime.now().astimezone() - timedelta(hours=settings.record_retention_hours)
    return index.playlist(folder.name, since)


def tag(body: str, name: str) -> str | None:
    for line in body.splitlines():
        if line.split(":", 1)[0] == name:
            return line.split(":", 1)[1]
    return None


def discontinuities(body: str) -> int:
    return sum(1 for l in body.splitlines() if l == "#EXT-X-DISCONTINUITY")


def check(cond: bool, what: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {what}")
    if not cond:
        raise SystemExit(1)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ceravis-playlist-"))
    settings.record_dir = str(root)            # point the index at the fake tree
    folder = root / "cam_test-aac"
    folder.mkdir(parents=True)

    now = datetime.now().astimezone().replace(microsecond=0)
    # Run A (~10 min ago): 2 full segments + a third the recorder closed early
    # when the person left. Run B (now): 2 full segments + one still being
    # written this second.
    a0 = now - timedelta(minutes=10) - timedelta(seconds=3 * SEG)
    for i in range(2):
        write_segment(folder, a0 + timedelta(seconds=i * SEG))
    write_segment(folder, a0 + timedelta(seconds=2 * SEG), length=SEG * 0.4)
    b0 = now - timedelta(seconds=3 * SEG)
    for i in range(2):
        write_segment(folder, b0 + timedelta(seconds=i * SEG))
    open_seg = write_segment(folder, b0 + timedelta(seconds=2 * SEG), open_now=True)

    print("\n1. two runs, one open segment")
    body = build(folder)
    print("\n".join("      " + l for l in body.splitlines()))
    check(len([l for l in body.splitlines() if l.startswith("segment/")]) == 5,
          "5 finished segments listed, the open one held back")
    check(open_seg.name not in body, "the segment being written is NOT advertised")
    check(discontinuities(body) == 1, "one discontinuity between the runs")
    check(body.count("#EXT-X-PROGRAM-DATE-TIME") == 2, "each run re-anchored to wall clock")
    check("#EXT-X-ENDLIST" not in body, "NO ENDLIST — players keep reloading it")
    check(tag(body, "#EXT-X-MEDIA-SEQUENCE") is not None
          and tag(body, "#EXT-X-DISCONTINUITY-SEQUENCE") == "0",
          "sequence tags present, discontinuity-sequence starts at 0")
    msn0 = int(tag(body, "#EXT-X-MEDIA-SEQUENCE"))
    # run A's early-ended segment is measured to its real length, not padded
    durs = [float(l.split(":")[1].rstrip(",")) for l in body.splitlines()
            if l.startswith("#EXTINF")]
    check(abs(durs[2] - SEG * 0.4) < 0.5,
          f"run A's last segment is its REAL length ({durs[2]:.3f}s, not {SEG}s)")

    print("\n2. the open segment finishes -> next build picks it up (no new call)")
    t = datetime.now().timestamp() - 5
    os.utime(open_seg, (t, t))
    body2 = build(folder)
    check(open_seg.name in body2, "the newly finished clip appended by itself")
    check(int(tag(body2, "#EXT-X-MEDIA-SEQUENCE")) == msn0,
          "appending does NOT move the media sequence")
    check("#EXT-X-ENDLIST" not in body2, "still open")

    print("\n3. the oldest segment ages out -> the window slides correctly")
    oldest = sorted(folder.glob("*.ts"))[0]
    oldest.unlink()
    body3 = build(folder)
    check(int(tag(body3, "#EXT-X-MEDIA-SEQUENCE")) == msn0 + 1,
          "media sequence advanced by exactly the one segment dropped")
    check(tag(body3, "#EXT-X-DISCONTINUITY-SEQUENCE") == "0",
          "no discontinuity was dropped, so its sequence held")

    print("\n4. all of run A ages out -> the dropped discontinuity is accounted")
    for f in sorted(folder.glob("*.ts"))[:2]:
        f.unlink()
    body4 = build(folder)
    check(int(tag(body4, "#EXT-X-MEDIA-SEQUENCE")) == msn0 + 3,
          "media sequence advanced by all three dropped segments")
    check(tag(body4, "#EXT-X-DISCONTINUITY-SEQUENCE") == "1",
          "the discontinuity that scrolled off the front was counted")
    check(discontinuities(body4) == 0,
          "only run B remains, so no discontinuity tag is left in the body")

    print("\n5. a stalled camera's open file is not handed out truncated")
    for f in folder.glob("*.ts"):
        f.unlink()
    # Started 4 s ago and its writes went quiet 4 s ago: the source stalled, but
    # MediaMTX has NOT rolled the file over yet — it is still open.
    stalled = write_segment(folder, now - timedelta(seconds=4), length=0)
    check(build(folder) == "", "a segment younger than one segment length is held back")
    # Once its whole nominal length has passed, MediaMTX has closed it for sure.
    stalled.unlink()
    write_segment(folder, now - timedelta(seconds=SEG + 5), length=SEG)
    check(build(folder) != "", "and released once its length has certainly elapsed")

    print("\n6. no footage at all -> empty (the API turns this into a 404)")
    for f in folder.glob("*.ts"):
        f.unlink()
    check(build(folder) == "", "empty playlist for an empty camera")

    shutil.rmtree(root, ignore_errors=True)
    print("\nAll playback-playlist checks passed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
