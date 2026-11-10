#!/usr/bin/env python3
"""
Manual end-to-end test — fire the posture TRANSITION events NOW, against the
real cloud, exactly the way the pipeline sends them:

    sitting_down, standing_up, walking_started, walking_stopped,
    no_transition_snapshot (the posture held for a long time)

⚠ NOT an automated test: each one is a REAL saveSnapshot that shows up in the
app. That is why its name does not start with test_ — no test runner picks it
up. Run it only on purpose, on a bench device with a test account.

Production sends these as SNAPSHOT-ONLY events (settings.
cloud_snapshot_event_types): one saveSnapshot each — the live still, the
production wording (alerts.alert_format.describe) and category = the event
type in capitals. No saveAlert (only fall and no_motion raise alerts).

PRECONDITIONS (on the edge): account verified (account.json has
ceravisUserId), one enabled camera, CERAVIS_API_BASE_URL set, and the
app-server API key — a shell does not get the service's key, so pass it in.

Run from the repo root, on the device:
    export CERAVIS_API_KEY=$(sudo cat /etc/ceravis/credstore/ceravis_api_key)
    python3 tests/manual_fire_transitions.py                  # all five, first camera
    python3 tests/manual_fire_transitions.py LIVING_ROOM      # a specific camera
    python3 tests/manual_fire_transitions.py --only sitting_down,no_transition_snapshot
    unset CERAVIS_API_KEY
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

from alerts.alert_format import describe
from common import clock
from common.rtsp import grab_one_frame
from config.account_config import AccountConfig
from config.camera_config import CameraConfig
from events.event_enricher import _ALERT_MAP
from integration.ceravis_api import (
    CeravisApiError, api_key_problem, is_configured, room_to_enum, save_snapshot,
)
from schemas.event import Event

# event type -> (detail, duration_secs) as the rules fill them
EVENTS = {
    "sitting_down": (None, None),
    "standing_up": (None, None),
    "walking_started": (None, None),
    "walking_stopped": (None, None),
    "no_transition_snapshot": ("sitting", 45 * 60.0),    # held 45 min, sitting
}


def _die(msg: str):
    print(f"[test-transitions] ABORT: {msg}")
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
    frame = grab_one_frame(cam.rtsp_url)
    if frame is None:
        return None
    import cv2
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Fire the posture transition events now.")
    ap.add_argument("label", nargs="?", help="camera room / label (default: first enabled)")
    ap.add_argument("--only", help="comma list of: " + ",".join(EVENTS))
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between events (default 3)")
    args = ap.parse_args()

    chosen = list(EVENTS)
    if args.only:
        chosen = [e.strip() for e in args.only.split(",") if e.strip()]
        bad = [e for e in chosen if e not in EVENTS]
        if bad:
            _die(f"unknown event(s) {bad}; choose from {list(EVENTS)}")

    if not is_configured():
        _die("CERAVIS_API_BASE_URL not set — cloud is not configured")
    key_problem = api_key_problem()
    if key_problem:
        _die(f"{key_problem}. A shell does not get the service's key: first\n"
             "    export CERAVIS_API_KEY=$(sudo cat /etc/ceravis/credstore/ceravis_api_key)")
    pid = AccountConfig().get().get("ceravisUserId")
    if not pid:
        _die("no verified account (account.json has no ceravisUserId) — run setup step 1")

    cam = _pick_camera(args.label)
    camera_number = room_to_enum(cam.room_name)
    print(f"[test-transitions] camera={cam.camera_id} room={cam.room_name!r} "
          f"cameraNumber={camera_number} patient={pid}")
    jpeg = _live_jpeg(cam)
    if not jpeg:
        _die("could not grab a live frame from the camera (is it streaming?)")
    print(f"[test-transitions] live still: {len(jpeg)} bytes")

    sent = 0
    for i, etype in enumerate(chosen):
        if i:
            time.sleep(args.gap)
        detail, held = EVENTS[etype]
        event = Event(event_id=str(uuid.uuid4()), event_type=etype,
                      title=_ALERT_MAP[etype][1], camera_id=cam.camera_id,
                      room_name=cam.room_name, timestamp=clock.now().isoformat(),
                      detail=detail, duration_secs=held)
        text = describe(event)                    # the production wording
        category = etype.upper()                  # = cloud_alert_publisher._category_of
        try:
            save_snapshot(pid, text, camera_number, image=jpeg, category=category)
            sent += 1
            print(f"[test-transitions] {category:<24} OK    {text.splitlines()[0]}")
        except CeravisApiError as exc:
            print(f"[test-transitions] {category:<24} FAIL  {exc}")
    print(f"[test-transitions] done: {sent}/{len(chosen)} sent")
    raise SystemExit(0 if sent == len(chosen) else 1)


if __name__ == "__main__":
    main()
