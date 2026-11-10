#!/usr/bin/env python3
"""
Manual end-to-end test — fire the posture-change and no-transition events NOW,
against the real cloud, exactly the way the pipeline sends them.

⚠ NOT an automated test: it sends REAL snapshots that appear in the carers' app.
That is why its name does not start with test_ — no test runner picks it up.
Run it only on purpose, on a bench device with a test account.

These events are SNAPSHOT-ONLY in production (settings.cloud_snapshot_event_types:
no saveAlert), so each one is ONE call, the same one CloudAlertPublisher makes:

    saveSnapshot(image=<live frame>, text=describe(event),
                 cameraNumber=<room enum>, category=<EVENT_TYPE>)

    standing_up             SITTING  -> STANDING
    sitting_down            STANDING -> SITTING
    walking_started         *        -> WALKING
    walking_stopped         WALKING  -> STANDING
    no_transition_snapshot  the same posture held for a long time

PRECONDITIONS (on the edge): account verified (account.json has ceravisUserId),
one enabled camera, the cloud configured, and the app-server API key — the
service gets it from the root-only credstore; a shell does not, so pass it in.

Run from the repo root, on the device:
    export CERAVIS_API_KEY=$(sudo cat /etc/ceravis/credstore/ceravis_api_key)
    python3 tests/manual_fire_posture_events.py                    # all five
    python3 tests/manual_fire_posture_events.py --camera LIVING_ROOM
    python3 tests/manual_fire_posture_events.py --only sitting_down standing_up
    python3 tests/manual_fire_posture_events.py --dry-run          # print, send nothing
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

from alerts.alert_format import describe                       # noqa: E402
from common import clock                                       # noqa: E402
from common.rtsp import grab_one_frame                         # noqa: E402
from config.account_config import AccountConfig                # noqa: E402
from config.camera_config import CameraConfig                  # noqa: E402
from events.event_enricher import _ALERT_MAP                   # noqa: E402
from integration.ceravis_api import (                          # noqa: E402
    CeravisApiError, api_key_problem, is_configured, room_to_enum, save_snapshot,
)
from schemas.event import Event                                # noqa: E402

EVENTS = ("standing_up", "sitting_down", "walking_started", "walking_stopped",
          "no_transition_snapshot")


def _die(msg: str):
    print(f"[test-posture] ABORT: {msg}")
    raise SystemExit(1)


def _pick_camera(label: str | None):
    cams = [c for c in CameraConfig().get_all() if c.is_enabled]
    if not cams:
        _die("no enabled cameras registered (cameras.json is empty)")
    if label:
        cam = CameraConfig().get_by_label(label)
        if not cam:
            _die(f"no camera matches {label!r}; cameras: "
                 + ", ".join(c.camera_id for c in cams))
        return cam
    return cams[0]


def _live_jpeg(cam) -> bytes | None:
    frame = grab_one_frame(cam.rtsp_url)
    if frame is None:
        return None
    import cv2
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def event_for(etype: str, cam, held_secs: float, posture: str) -> Event:
    """The event as the rules build it (posture_rule / stillness_rule), with the
    room, severity and title the pipeline's enricher gives it (its own table)."""
    severity, title = _ALERT_MAP[etype]
    extra = ({"duration_secs": held_secs, "detail": posture}
             if etype == "no_transition_snapshot" else {})
    return Event(event_id=str(uuid.uuid4()), event_type=etype, camera_id=cam.camera_id,
                 room_name=cam.room_name, recipient_id="manual-test",
                 timestamp=clock.now().isoformat(), severity=severity, title=title, **extra)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fire posture / no-transition events now.")
    ap.add_argument("--camera", help="camera room / label (default: first enabled)")
    ap.add_argument("--only", nargs="+", choices=EVENTS, help="send only these events")
    ap.add_argument("--held-mins", type=float, default=60.0,
                    help="no_transition: how long the posture 'held' (default 60)")
    ap.add_argument("--posture", default="sitting",
                    help="no_transition: the posture that held (default sitting)")
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between events")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent")
    args = ap.parse_args()

    if not args.dry_run:
        if not is_configured():
            _die("CERAVIS_API_BASE_URL not set — cloud is not configured")
        problem = api_key_problem()
        if problem:
            _die(f"{problem}. A shell does not get the service's key: first\n"
                 "    export CERAVIS_API_KEY=$(sudo cat /etc/ceravis/credstore/ceravis_api_key)")
    pid = AccountConfig().get().get("ceravisUserId")
    if not pid and not args.dry_run:
        _die("no verified account (account.json has no ceravisUserId)")

    cam = _pick_camera(args.camera)
    camera_number = room_to_enum(cam.room_name)
    print(f"[test-posture] camera={cam.camera_id} room={cam.room_name!r} "
          f"cameraNumber={camera_number} patient={pid}")
    jpeg = None if args.dry_run else _live_jpeg(cam)
    if not args.dry_run and not jpeg:
        _die("could not grab a live frame from the camera — is it streaming?")

    sent = failed = 0
    for i, etype in enumerate(args.only or EVENTS):
        ev = event_for(etype, cam, args.held_mins * 60, args.posture)
        text, category = describe(ev), etype.upper()
        if args.dry_run:
            print(f"[test-posture] {etype:<23} DRY  category={category}  text={text!r}")
            continue
        if i:
            time.sleep(args.gap)
        try:
            save_snapshot(pid, text, camera_number, image=jpeg, category=category)
            sent += 1
            print(f"[test-posture] {etype:<23} OK   category={category}  text={text!r}")
        except CeravisApiError as exc:
            failed += 1
            print(f"[test-posture] {etype:<23} FAIL {exc}")
    if not args.dry_run:
        print(f"[test-posture] done: {sent} sent, {failed} failed"
              + (f"  (image {len(jpeg)} bytes each)" if jpeg else ""))
        raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
