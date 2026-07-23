from __future__ import annotations

"""
ceravis-recordings — inspect what the recorder ACTUALLY wrote to disk.

Answers the questions you need before trusting playback:
  * are the segment LABELS (filenames) valid, chronological, correctly spaced?
  * are the runs/gaps what person-triggered recording should produce?
  * how big is a segment really, and what bitrate is the camera emitting?
  * is anything stale, malformed, or past the retention window?
  * (--probe) what codec/resolution/fps/audio is actually inside a segment?

It reads the SAME index that playback uses (recording.index), so a clean
report here means the playlist is built from sound data — it is not a parallel
implementation that could disagree.

    python -m tools.recordings                # all cameras (run from edge/)
    python -m tools.recordings --probe        # + ffprobe a real segment
    python -m tools.recordings --path cam_001-rec-aac
    python -m tools.recordings --json

Exit code: 0 clean, 1 problems found.
"""

import argparse
import json
import subprocess
import sys
from datetime import timedelta

from config.settings import settings
from recording import index as ri


def _folders(only: str | None):
    root = ri.record_root()
    if not root.exists():
        return []
    out = [d for d in sorted(root.iterdir()) if d.is_dir()]
    return [d for d in out if d.name == only] if only else out


def _probe(path) -> dict:
    """Real codec/resolution/duration from the file itself (needs ffprobe)."""
    cmd = [settings.ffmpeg_binary.replace("ffmpeg", "ffprobe"),
           "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return {"error": (out.stderr or "ffprobe failed").strip()[:200]}
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"error": str(exc)[:200]}
    info = {"duration": None, "video": None, "audio": None}
    try:
        info["duration"] = round(float(data.get("format", {}).get("duration", 0)), 2)
    except (TypeError, ValueError):
        pass
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and info["video"] is None:
            fps = st.get("avg_frame_rate", "0/1")
            try:
                n, d = fps.split("/")
                fps = round(int(n) / int(d), 1) if int(d) else 0
            except (ValueError, ZeroDivisionError):
                fps = 0
            info["video"] = {"codec": st.get("codec_name"),
                             "size": f"{st.get('width')}x{st.get('height')}",
                             "fps": fps}
        elif st.get("codec_type") == "audio" and info["audio"] is None:
            info["audio"] = {"codec": st.get("codec_name"),
                             "rate": st.get("sample_rate"),
                             "channels": st.get("channels")}
    return info


def inspect(folder, do_probe: bool) -> dict:
    """Everything worth knowing about one recorded path."""
    segs = ri.segments(folder.name)
    nominal = float(settings.record_segment_secs)
    problems: list[str] = []

    # --- what's physically in the folder vs what the index accepted ---
    files = [f for f in folder.iterdir() if f.is_file()]
    stale = [f.name for f in files if f.suffix.lower() == ".mp4"]
    unparsed = [f.name for f in files
                if f.suffix.lower() == ri.SEGMENT_SUFFIX
                and not ri.is_segment_name(f.name)]
    if stale:
        problems.append(f"{len(stale)} stale .mp4 file(s) from the old format "
                        f"- ignored by playback; delete them")
    if unparsed:
        problems.append(f"{len(unparsed)} .ts file(s) with unreadable labels "
                        f"(e.g. {unparsed[0]}) - playback skips these")
    if not segs:
        problems.append("no usable segments on disk")
        return {"path": folder.name, "segments": 0, "problems": problems,
                "stale_mp4": len(stale), "bad_labels": len(unparsed)}

    # --- size / bitrate reality ---
    total_bytes = 0
    for s in segs:
        try:
            total_bytes += s.file.stat().st_size
        except OSError:
            pass
    total_secs = sum(s.duration for s in segs)
    kbps = (total_bytes * 8 / total_secs / 1000) if total_secs else 0
    avg_mb = (total_bytes / len(segs)) / 1e6

    # --- label sanity: chronological + correctly spaced inside a run ---
    odd_spacing = 0
    for a, b in zip(segs, segs[1:]):
        if b.starts_run:
            continue                       # a real gap, expected
        delta = (b.start - a.start).total_seconds()
        if abs(delta - nominal) > 1.0:     # inside a run they must be ~nominal apart
            odd_spacing += 1
    if odd_spacing:
        problems.append(f"{odd_spacing} segment(s) unevenly spaced inside a run "
                        f"(expected ~{nominal:.0f}s apart)")

    # --- retention window ---
    from common import clock
    now = clock.now()
    window_start = now - timedelta(hours=settings.record_retention_hours)
    overdue = [s for s in segs if s.end < window_start]
    if overdue:
        problems.append(f"{len(overdue)} segment(s) older than the "
                        f"{settings.record_retention_hours}h window - retention "
                        f"is not deleting (check MediaMTX recordDeleteAfter)")

    runs = ri.ranges(folder.name)
    cap = settings.record_target_bitrate_kbps
    if kbps > cap * 1.25:
        problems.append(f"~{kbps:.0f} kbps is well above the {cap} kbps target "
                        f"- the camera's record profile likely wasn't applied "
                        f"(re-probe the camera)")

    out = {
        "path": folder.name,
        "segments": len(segs),
        "runs": len(runs),
        "total_mb": round(total_bytes / 1e6, 1),
        "recorded_minutes": round(total_secs / 60, 1),
        "avg_segment_mb": round(avg_mb, 2),
        "measured_kbps": round(kbps),
        "target_kbps": cap,
        "projected_mb_per_hour": round(kbps * 1000 / 8 * 3600 / 1e6, 1),
        "first_label": segs[0].file.name,
        "last_label": segs[-1].file.name,
        "span": f"{segs[0].start:%Y-%m-%d %H:%M:%S} -> {segs[-1].end:%H:%M:%S}",
        "stale_mp4": len(stale),
        "bad_labels": len(unparsed),
        "problems": problems,
    }
    if do_probe:
        out["probe"] = _probe(segs[-1].file)
    return out


def _print(rep: dict) -> None:
    print(f"\n{rep['path']}")
    if rep.get("segments", 0) == 0:
        for p in rep["problems"]:
            print(f"   ! {p}")
        return
    print(f"   segments        {rep['segments']} in {rep['runs']} run(s), "
          f"{rep['recorded_minutes']} min recorded")
    print(f"   labels          {rep['first_label']}")
    print(f"                   {rep['last_label']}")
    print(f"   span            {rep['span']}")
    print(f"   size            {rep['total_mb']} MB total, "
          f"{rep['avg_segment_mb']} MB per {settings.record_segment_secs}s segment")
    print(f"   bitrate         ~{rep['measured_kbps']} kbps measured "
          f"(target {rep['target_kbps']}) -> ~{rep['projected_mb_per_hour']} MB "
          f"per hour of footage")
    if "probe" in rep:
        p = rep["probe"]
        if p.get("error"):
            print(f"   probe           ffprobe failed: {p['error']}")
        else:
            v, a = p.get("video") or {}, p.get("audio") or {}
            print(f"   inside          video {v.get('codec')} {v.get('size')} "
                  f"@{v.get('fps')}fps | audio "
                  f"{a.get('codec') or 'NONE'} {a.get('rate') or ''} "
                  f"{'mono' if a.get('channels') == 1 else a.get('channels') or ''}")
            print(f"   real duration   {p.get('duration')}s "
                  f"(label spacing says {settings.record_segment_secs}s)")
    for p in rep["problems"]:
        print(f"   ! {p}")
    if not rep["problems"]:
        print("   OK              labels valid, spacing even, retention healthy")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ceravis-recordings",
        description="Inspect the recorded segments on disk (labels, size, health).")
    ap.add_argument("--path", help="only this recording path (e.g. cam_001-rec-aac)")
    ap.add_argument("--probe", action="store_true",
                    help="also ffprobe a real segment for codec/resolution/audio")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    folders = _folders(args.path)
    if not folders:
        print(f"No recordings found under {ri.record_root()}")
        return 1
    reports = [inspect(f, args.probe) for f in folders]
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        print(f"Recordings under {ri.record_root()}  "
              f"(retention {settings.record_retention_hours}h)")
        for rep in reports:
            _print(rep)
        print()
    return 1 if any(r["problems"] for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
