#!/usr/bin/env python3
"""
Manual end-to-end test — fire ONE simulated FALL incident NOW, against the real
cloud, exactly the way the pipeline would.

It exercises the same integration path a real fall takes, so you can confirm the
alert, the still and the incident clip all land in the app / S3 without waiting
for an actual fall:

    saveAlert("FALL")                                  -> alertId
    saveSnapshot(image=<live frame>, video=<clip>,     -> the still AND the clip
                 alertId, category="FALL")                in ONE multipart call

Both artifacts are available at test time (the live still and the clip merged
from the segments already on disk around "now"), so — unlike production, where the
clip trails the alert by a post-roll — the test sends them together in a single
saveSnapshot. Nothing here touches the running pipeline; it calls the shared
integration + recording modules (recording/incident_clip is the same merge the
CloudAlertPublisher uses).

PRECONDITIONS (on the edge):
  • account verified            (account.json has ceravisUserId)
  • at least one enabled camera (cameras.json)
  • CERAVIS_API_BASE_URL set    (cloud configured)
  • for the clip: recording ON and someone was in frame recently, else the clip
    is skipped (the alert + still still go out).

Run from the repo root, on the device:
    python tests/test_fall_incident.py                 # first enabled camera
    python tests/test_fall_incident.py KITCHEN         # a specific room / label
    python tests/test_fall_incident.py KITCHEN --no-clip   # alert + still only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the edge package importable no matter where this is launched from.
EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

from alerts.alert_format import format_line
from common import clock
from common.rtsp import grab_one_frame
from config.settings import settings
from configuration.account_config import AccountConfig
from configuration.camera_config import CameraConfig
from integration.ceravis_api import (
    CeravisApiError, alert_id_of, is_configured, room_to_enum, save_alert,
    save_snapshot,
)
from livestream.mediamtx_client import record_path_name
from recording.incident_clip import build_incident_clip


def _die(msg: str):
    print(f"[test-fall] ABORT: {msg}")
    raise SystemExit(1)


def _pick_camera(label: str | None):
    cams = [c for c in CameraConfig().get_all() if c.is_enabled]
    if not cams:
        _die("no enabled cameras registered (cameras.json is empty)")
    if label:
        cam = CameraConfig().get_by_label(label)
        if not cam:
            _die(f"no camera matches label {label!r}")
        return cam
    return cams[0]


def _live_jpeg(cam) -> bytes | None:
    """One fresh JPEG off the camera's main stream — the fall 'still'."""
    frame = grab_one_frame(cam.rtsp_url)
    if frame is None:
        return None
    import cv2                               # lazy: only when we actually grab
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Fire a simulated FALL incident now.")
    ap.add_argument("label", nargs="?", help="camera room / label (default: first enabled)")
    ap.add_argument("--no-clip", action="store_true", help="send the alert + still only")
    args = ap.parse_args()

    if not is_configured():
        _die("CERAVIS_API_BASE_URL not set — cloud is not configured")
    acct = AccountConfig().get()
    pid = acct.get("ceravisUserId")
    if not pid:
        _die("no verified account (account.json has no ceravisUserId) — run setup step 1")

    cam = _pick_camera(args.label)
    at = clock.now()
    who = acct.get("firstName") or "recipient"
    text = format_line("CRITICAL · Fall detected", cam, cam.room_name, who, at)
    camera_number = room_to_enum(cam.room_name)
    print(f"[test-fall] camera={cam.camera_id} room={cam.room_name!r} "
          f"cameraNumber={camera_number} patient={pid} at={at.isoformat()}")
    print(f"[test-fall] annotation: {text}")

    # 1) the alert
    try:
        alert_id = alert_id_of(save_alert(pid, "FALL", text))
        print(f"[test-fall] saveAlert  OK   -> alertId={alert_id}")
    except CeravisApiError as exc:
        _die(f"saveAlert failed: {exc}")

    # 2) gather BOTH artifacts now — in the test both are available immediately:
    #    the live still, and the clip merged from the segments already on disk.
    jpeg = _live_jpeg(cam)
    print("[test-fall] still     " + (f"OK   -> image {len(jpeg)} bytes" if jpeg
          else "SKIP -> could not grab a live frame"))

    clip = None
    if args.no_clip:
        print("[test-fall] clip      SKIP -> --no-clip")
    else:
        clip = build_incident_clip(record_path_name(cam), at,
                                   settings.fall_clip_pre_secs,
                                   settings.fall_clip_post_secs)
        print("[test-fall] clip      " + (f"OK   -> video {len(clip)} bytes" if clip
              else "SKIP -> no footage around now (recording ON? someone in frame recently?)"))

    # 3) ONE saveSnapshot carrying BOTH the image and the video (+ alertId,
    #    category) — the SnapshotRequest DTO takes both, so a fall is a single
    #    call, not two.
    if not jpeg and not clip:
        _die("nothing to send — no still and no clip available")
    try:
        save_snapshot(pid, text, camera_number, image=jpeg, video=clip,
                      alert_id=alert_id, category="FALL")
        print(f"[test-fall] saveSnapshot OK -> image={'yes' if jpeg else 'no'} "
              f"video={'yes' if clip else 'no'} category=FALL  — DONE")
    except CeravisApiError as exc:
        _die(f"saveSnapshot failed: {exc}")


if __name__ == "__main__":
    main()
