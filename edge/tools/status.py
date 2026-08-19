from __future__ import annotations

"""
ceravis-status — live health of the RUNNING edge service.

Talks to the local API (the same /api/v1/system/status the monitor uses), so it
reports what the service is ACTUALLY doing right now: media backbone, per-camera
stream + recording health, disk headroom + retention window, the NTP-disciplined
edge clock, and recent cloud activity. This is a production health probe, not a
unit test — it needs the service to be up.

    python -m tools.status                 # human-readable  (run from edge/)
    python -m tools.status --json          # machine-readable (monitoring/systemd)
    python -m tools.status --url http://127.0.0.1:8000

Exit code: 0 healthy, 1 degraded (a subsystem is down), 2 unreachable (service
not running) — so it drops straight into a cron or systemd health check.
"""

import argparse
import json
import sys

import requests


def _line(label: str, value) -> str:
    return f"  {label:<22} {value}"


def _fmt(d: dict) -> str:
    out = [f"CERAVIS edge — {str(d.get('status', '?')).upper()}   "
           f"v{d.get('version', '?')}   device={d.get('device_id', '?')}   "
           f"edge_id={d.get('edge_id') or '-'}"]
    for p in d.get("problems", []):
        out.append(f"  ! {p}")

    t = d.get("time", {})
    ntp = t.get("ntp_synchronized")
    out += ["", "TIME",
            _line("local", t.get("local")),
            _line("timezone", t.get("timezone")),
            _line("ntp synced",
                  "unknown" if ntp is None else ("yes" if ntp else "NO — clock may drift"))]

    mb = d.get("media_backbone", {})
    out += ["", "MEDIA BACKBONE",
            _line("mediamtx up", "yes" if mb.get("up") else "NO — links & recording dead")]

    out += ["", "CAMERAS"]
    for c in d.get("cameras", []) or [{"camera_id": "(none)"}]:
        if c.get("camera_id") == "(none)":
            out.append(_line("(none registered)", ""))
            break
        state = "ready" if c.get("path_ready") else "NOT READY"
        out.append(_line(c.get("camera_id", ""),
                         f"{state}  {c.get('resolution') or '-'}  "
                         f"codec={c.get('codec') or '-'}"
                         f"{'/' + c['profile'] if c.get('profile') else ''}  "
                         f"readers={c.get('readers')}  ptz={'y' if c.get('ptz') else 'n'}"))

    rec = d.get("recording", {})
    out += ["", "RECORDING",
            _line("enabled", "yes" if rec.get("enabled") else "no (disk-saving OFF)"),
            _line("recording now", ", ".join(rec.get("recording_now") or []) or "-")]
    for c in rec.get("cameras", []):
        age = c.get("newest_segment_age_secs")
        age_s = f"{age}s ago" if age is not None else "no segments yet"
        flag = "" if c.get("writing_ok", True) else "   <-- NOT WRITING"
        if not c.get("recordable", True):
            flag = "   <-- BLOCKED: unplayable codec"
        out.append(_line(c.get("camera_id", ""),
                         f"{'REC' if c.get('recording') else 'idle'}  last={age_s}  "
                         f"{c.get('segments_last_hour')} seg/h  "
                         f"{c.get('megabytes_last_hour')} MB/h{flag}"))

    s = d.get("storage", {})
    out += ["", "STORAGE",
            _line("recordings", f"{s.get('recordings_gb')} GB "
                                f"(rolling {s.get('retention_hours')}h window)"),
            _line("disk", f"{s.get('disk_used_gb')}/{s.get('disk_total_gb')} GB used "
                          f"({s.get('disk_used_pct')}%), {s.get('disk_free_gb')} GB free"),
            _line("span", f"{s.get('oldest_segment') or '-'}  ->  "
                          f"{s.get('newest_segment') or '-'}")]

    cl = d.get("cloud", {})
    out += ["", "CLOUD",
            _line("configured", "yes" if cl.get("configured") else "no"),
            _line("recent errors",
                  f"{cl.get('recent_errors', 0)} of {cl.get('recent_calls', 0)} calls")]
    q = cl.get("outbox")
    if q:
        age = q.get("oldest_pending_age_secs")
        waiting = (f"{q['pending']} waiting, oldest {age / 60:.0f} min "
                   f"({q.get('attempts_on_head', 0)} attempts)"
                   if q.get("pending") and age is not None else "empty (all delivered)")
        out += [_line("upload queue", waiting),
                _line("window", f"{q.get('window_hours')}h / {q.get('max_items')} jobs, "
                                f"{q.get('sent', 0)} sent, {q.get('dropped', 0)} dropped")]
        if q.get("pending") and q.get("last_error"):
            out.append(_line("last error", str(q["last_error"])[:70]))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ceravis-status",
        description="Live health of the running CERAVIS edge service.")
    ap.add_argument("--url", default="http://127.0.0.1:8000",
                    help="edge API base URL (default: %(default)s)")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()

    try:
        r = requests.get(args.url.rstrip("/") + "/api/v1/system/status",
                         timeout=args.timeout)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as exc:
        if args.json:
            print(json.dumps({"status": "unreachable", "error": str(exc)}))
        else:
            print(f"CERAVIS edge UNREACHABLE at {args.url}\n  {exc}\n"
                  f"  Is the service running?   sudo systemctl status ceravis")
        return 2

    print(json.dumps(data, indent=2) if args.json else _fmt(data))
    return 0 if data.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
