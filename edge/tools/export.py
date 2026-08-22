from __future__ import annotations

"""
ceravis-export — pull recorded footage OFF the rolling window and keep it.

`data/recordings/` is a 12-hour ring: everything in it is deleted on schedule.
This is the one way to take a stretch of that footage and put it somewhere
permanent, OUTSIDE the ceravis tree (default `~/Videos/ceravis`), before the
retention sweep reaches it.

It reads the SAME index playback reads (recording.index), so what you export is
exactly what the timeline showed — same segments, same edge-local clock, same
run boundaries. No second scan of the directory that could disagree.

    # run from edge/  — both cameras, one MP4 each
    python -m tools.export --from "2026-08-20 14:00" --to "2026-08-20 14:30"

    # one camera (camera_id or the PTZ-style label)
    python -m tools.export --from 14:00 --to 14:30 --camera LIVING_ROOM

    # one file per contiguous run instead of one joined file
    python -m tools.export --from 14:00 --to 14:30 --per-run

    # the raw .ts segments, copied untouched (no ffmpeg)
    python -m tools.export --from 14:00 --to 14:30 --segments

Times are EDGE-LOCAL (the clock the camera OSD is disciplined to). Accepted:
"YYYY-MM-DD HH:MM[:SS]", ISO-8601 (offset or Z), a bare "HH:MM[:SS]" meaning
today, or a Unix epoch.

Recording is person-triggered, so a range usually contains GAPS. The default
joins the runs into one continuous file and prints every gap it closed; use
--per-run when the wall-clock gaps must stay visible as separate files.

Exit code: 0 exported, 1 nothing found / export failed.
"""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import clock
from config.settings import settings
from configuration.camera_config import CameraConfig
from livestream.mediamtx_client import record_path_name
from recording import index as ri


DEFAULT_OUT = Path.home() / "Videos" / "ceravis"


# ---- the time range -------------------------------------------------

def parse_when(text: str) -> datetime:
    """One instant on the edge-local clock, in the shapes a human actually
    types. Naive values are edge-local — the same rule playback and snapshot
    use, so a time read off the timeline exports the footage it names."""
    t = (text or "").strip()
    if not t:
        raise ValueError("empty time")
    if t.lstrip("+-").isdigit():                      # epoch seconds or millis
        n = int(t)
        secs = n / 1000.0 if abs(n) >= 1_000_000_000_000 else float(n)
        return datetime.fromtimestamp(secs, tz=timezone.utc).astimezone()
    iso = t[:-1] + "+00:00" if t[-1] in "Zz" else t
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        # bare clock time -> today on the edge
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                hm = datetime.strptime(t, fmt).time()
            except ValueError:
                continue
            return datetime.combine(clock.now().date(), hm).astimezone()
        raise ValueError(f"bad time {text!r} — try \"2026-08-20 14:30\", "
                         f"\"14:30\", or an ISO-8601 stamp")
    return dt if dt.tzinfo else dt.astimezone()


# ---- what is on disk for that range ---------------------------------

def _cameras(only: list[str] | None):
    cfg = CameraConfig()
    if not only:
        return cfg.get_all()
    out = []
    for name in only:
        cam = cfg.get_by_id(name) or cfg.get_by_label(name)
        if cam is None:
            raise SystemExit(f"ceravis-export: no camera for {name!r} — known: "
                             f"{', '.join(c.camera_id for c in cfg.get_all())}")
        out.append(cam)
    return out


def _overlapping(rec_path: str, start: datetime, end: datetime) -> list[ri.Segment]:
    """Every stored segment that carries any footage inside [start, end)."""
    return [s for s in ri.segments(rec_path) if s.end > start and s.start < end]


def _runs(segs: list[ri.Segment]) -> list[list[ri.Segment]]:
    """Split the selection back into its contiguous recorded stretches."""
    out: list[list[ri.Segment]] = []
    for s in segs:
        if s.starts_run or not out:
            out.append([s])
        else:
            out[-1].append(s)
    return out


# ---- writing it out -------------------------------------------------

def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d_%H-%M-%S")


def _video_codec(path: Path) -> str:
    """Codec inside a segment — only needed to tag HEVC so Apple players open
    the MP4. Unknown is fine; the copy still works."""
    probe = settings.ffmpeg_binary.replace("ffmpeg", "ffprobe")
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15)
        return (out.stdout or "").strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""


def _mux(segs: list[ri.Segment], start: datetime, end: datetime,
         out_file: Path, scratch: Path) -> tuple[bool, str]:
    """Join these segments into one MP4 trimmed to [start, end), COPYING the
    streams — never re-encoding. The camera's own bytes land in the file, so an
    export costs no quality and no GPU.

    The cut is keyframe-accurate: ffmpeg seeks to the keyframe at or before the
    requested instant, so a file can begin up to one GOP early. That is the
    price of not re-encoding, and it never loses footage you asked for."""
    listing = scratch / f"{out_file.stem}.txt"
    # ffmpeg's concat demuxer reads these paths verbatim; a single quote is the
    # only character it treats specially.
    lines = ["file '%s'\n" % str(s.file).replace("'", "'\\''") for s in segs]
    listing.write_text("".join(lines), encoding="utf-8")

    # How much MEDIA to keep — not how much wall clock the range spans. Where a
    # selection covers a gap, the joined stream is shorter than the range, so
    # measuring it in wall clock would over-ask; ffmpeg would stop at EOF and
    # the number would just be fiction. Sum what the segments actually hold,
    # less the head we seek past and the tail past `end`.
    offset = max((start - segs[0].start).total_seconds(), 0.0)
    tail = max((segs[-1].end - end).total_seconds(), 0.0)
    duration = sum(s.duration for s in segs) - offset - tail
    if duration <= 0:
        listing.unlink(missing_ok=True)
        return False, "nothing inside the requested range"

    cmd = [settings.ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-ss", f"{offset:.3f}",
           "-i", str(listing), "-t", f"{duration:.3f}",
           "-c", "copy", "-avoid_negative_ts", "make_zero"]
    if _video_codec(segs[0].file) in ("hevc", "h265"):
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-movflags", "+faststart", str(out_file)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:300]
    finally:
        listing.unlink(missing_ok=True)
    if run.returncode != 0 or not out_file.exists():
        return False, (run.stderr or "ffmpeg failed").strip()[:300]
    return True, ""


def _copy_segments(segs: list[ri.Segment], folder: Path) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    for s in segs:
        shutil.copy2(s.file, folder / s.file.name)
    return len(segs)


def export_camera(cam, start: datetime, end: datetime, out_dir: Path,
                  mode: str) -> dict:
    """One camera's share of the range -> file(s) under out_dir."""
    rec_path = record_path_name(cam)
    label = (cam.camera_name or cam.camera_id).strip() or cam.camera_id
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)
    segs = _overlapping(rec_path, start, end)
    rep = {"camera_id": cam.camera_id, "label": label, "path": rec_path,
           "segments": len(segs), "files": [], "gaps": [], "errors": []}
    if not segs:
        return rep

    runs = _runs(segs)
    for a, b in zip(runs, runs[1:]):
        rep["gaps"].append((a[-1].end, b[0].start))

    if mode == "segments":
        folder = out_dir / f"{safe}_{_stamp(start)}_to_{_stamp(end)}"
        rep["files"].append((folder, _copy_segments(segs, folder)))
        return rep

    out_dir.mkdir(parents=True, exist_ok=True)
    groups = runs if mode == "per-run" else [segs]
    for group in groups:
        g_start = max(start, group[0].start)
        g_end = min(end, group[-1].end)
        out_file = out_dir / f"{safe}_{_stamp(g_start)}_to_{_stamp(g_end)}.mp4"
        ok, err = _mux(group, g_start, g_end, out_file, out_dir)
        if ok:
            rep["files"].append((out_file, out_file.stat().st_size))
        else:
            rep["errors"].append(f"{out_file.name}: {err}")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ceravis-export",
        description="Save recorded footage for a time range to a permanent "
                    "folder outside the ceravis tree.")
    ap.add_argument("--from", dest="start", required=True,
                    help='start, edge-local ("2026-08-20 14:00", "14:00", ISO, epoch)')
    ap.add_argument("--to", dest="end", required=True, help="end, same formats")
    ap.add_argument("--camera", action="append",
                    help="camera_id or label; repeat for several. Default: ALL cameras")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"destination folder (default {DEFAULT_OUT})")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--per-run", action="store_true",
                       help="one file per contiguous recorded run instead of one joined file")
    group.add_argument("--segments", action="store_true",
                       help="copy the raw .ts segments untouched (no ffmpeg)")
    args = ap.parse_args()

    try:
        start, end = parse_when(args.start), parse_when(args.end)
    except ValueError as exc:
        print(f"ceravis-export: {exc}", file=sys.stderr)
        return 1
    if end <= start:
        print("ceravis-export: --to must be after --from", file=sys.stderr)
        return 1

    mode = "segments" if args.segments else ("per-run" if args.per_run else "joined")
    if mode != "segments" and not shutil.which(settings.ffmpeg_binary):
        print(f"ceravis-export: {settings.ffmpeg_binary} not found — "
              f"use --segments to copy the raw .ts files instead", file=sys.stderr)
        return 1

    out_dir = Path(args.out).expanduser()
    root = ri.record_root().resolve()
    resolved = out_dir.resolve()
    if resolved == root or root in resolved.parents:
        print(f"ceravis-export: --out is inside {root} — the retention sweep "
              f"would delete it. Pick a folder outside the recordings tree.",
              file=sys.stderr)
        return 1

    # What is even still on disk — a range older than the window is simply gone.
    oldest = clock.now() - timedelta(hours=settings.record_retention_hours)
    if start < oldest:
        print(f"note: the rolling window only reaches back to "
              f"{oldest:%Y-%m-%d %H:%M:%S}; anything before that is already deleted.")

    cams = _cameras(args.camera)
    if not cams:
        print("ceravis-export: no cameras configured", file=sys.stderr)
        return 1

    print(f"exporting {start:%Y-%m-%d %H:%M:%S} -> {end:%Y-%m-%d %H:%M:%S} "
          f"({(end - start).total_seconds() / 60:.1f} min) into {out_dir}")
    wrote = failed = 0
    for cam in cams:
        rep = export_camera(cam, start, end, out_dir, mode)
        print(f"\n{rep['label']}  ({rep['camera_id']})")
        if not rep["segments"]:
            print("   no footage recorded in that range (nobody was present, "
                  "or recording was off)")
            continue
        for g0, g1 in rep["gaps"]:
            print(f"   gap             {g0:%H:%M:%S} -> {g1:%H:%M:%S} "
                  f"({(g1 - g0).total_seconds():.0f}s with no footage)")
        for target, size in rep["files"]:
            wrote += 1
            if mode == "segments":
                print(f"   wrote           {size} segment(s) -> {target}")
            else:
                print(f"   wrote           {target.name}  ({size / 1e6:.1f} MB)")
        for err in rep["errors"]:
            failed += 1
            print(f"   ! {err}")

    print(f"\n{wrote} file(s)/folder(s) in {out_dir}"
          + (f", {failed} failed" if failed else ""))
    return 0 if wrote and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
