#!/usr/bin/env python3
"""
CERAVIS cloud connectivity sanity check — run ON THE DEVICE.

    cd ~/ceravis/edge
    python scripts/test_cloud.py                 # inspect config + payload (no send)
    python scripts/test_cloud.py --send          # actually call userDetails + saveCamera
    python scripts/test_cloud.py --alert         # send a test FALL alert
    python scripts/test_cloud.py --snapshot      # send a test snapshot
    python scripts/test_cloud.py --fall          # simulate a real fall: LIVE snapshot
                                                 # + matching FALL alert, together

Prints the base URL, the verified account, the exact saveCamera payload (with the
room normalized to the server's CameraName enum), and — with --send — fires the
real calls and prints the HTTP result, so you can see whether they hit.
"""
import base64
import glob
import json
import logging
import os
import socket
import sys
import urllib.request
from datetime import datetime, timezone

# make `config`, `integration`, … importable and resolve data/ no matter the cwd
_EDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _EDGE)
os.chdir(_EDGE)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

from config.settings import settings                                      # noqa: E402
from configuration.account_config import AccountConfig                    # noqa: E402
from configuration.camera_config import CameraConfig                      # noqa: E402
from integration.ceravis_api import (                                     # noqa: E402
    CeravisApiError, get_user_details, is_configured, room_to_enum,
    save_alert, save_cameras, save_snapshot,
)


def _stream_base() -> str:
    b = settings.device_stream_base.strip()
    if b:
        return b.rstrip("/").replace("wss://", "https://").replace("ws://", "http://")
    ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
    except Exception:
        pass
    return f"http://{ip}:8000"


def _latest_snapshot() -> str | None:
    files = glob.glob(os.path.join("data", "events", "**", "*.jpg"), recursive=True)
    return max(files, key=os.path.getmtime) if files else None


def _target_camera_id() -> str | None:
    """Ask the RUNNING service which camera the enrolled recipient is on."""
    try:
        with urllib.request.urlopen("http://localhost:8000/api/v1/ai/state", timeout=5) as r:
            state = json.loads(r.read())
    except Exception:
        return None
    for cam, info in (state or {}).items():
        if info.get("target_track_id") is not None:
            return cam
        if any(t.get("is_target") for t in info.get("tracks", [])):
            return cam
    return None


def _pick_camera(cams):
    """The camera the recipient is currently on; else the first as a fallback."""
    tid = _target_camera_id()
    if tid:
        for c in cams:
            if c.camera_id == tid:
                print(f"    target recipient is on camera '{tid}'")
                return c
    print("    (no recipient locked right now — falling back to the first camera)")
    return cams[0] if cams else None


def _live_jpeg(camera_id: str) -> bytes | None:
    """Grab a current frame from the RUNNING service's snapshot endpoint."""
    url = f"http://localhost:8000/api/v1/cameras/{camera_id}/snapshot?quality=80"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.read()
    except Exception as exc:
        print("    (live snapshot fetch failed:", exc, ")")
        return None


def _target_camera(cams):
    """The camera the enrolled target is currently detected on (from live AI
    state) — so a fall test snapshots the right room, not just camera #1."""
    try:
        with urllib.request.urlopen("http://localhost:8000/api/v1/ai/state", timeout=5) as r:
            state = json.loads(r.read())
    except Exception:
        return None
    for camid, s in (state or {}).items():
        if s.get("target_track_id") is not None or \
           any(t.get("is_target") for t in s.get("tracks", [])):
            for c in cams:
                if c.camera_id == camid:
                    return c
    return None


def _fall_label(acct: dict, cam) -> str:
    """The same labeled line a real fall alert carries."""
    who = acct.get("firstName") or "recipient"
    sym = ("*" if cam.has_bathroom_entrance else "") + ("&" if cam.is_main_egress else "")
    head = f"Camera {cam.camera_number}{sym} " if cam.camera_number else ""
    ts = datetime.now(timezone.utc).strftime("%I:%M %p, %d %b %Y").lstrip("0")
    return f"CRITICAL · Fall detected · {head}{cam.room_name} · {who} · {ts}"


def main() -> int:
    send = "--send" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    print("=== CERAVIS cloud sanity check ===")
    print("base_url :", settings.ceravis_api_base_url or "(NOT SET)")
    print("timeout  :", settings.ceravis_api_timeout_secs, "s")
    if not is_configured():
        print("\nERROR: CERAVIS_API_BASE_URL is empty — set it in infra/env/jetson.env")
        return 1

    acct = AccountConfig().get()
    pid = acct.get("ceravisUserId")
    email = args[0] if args else acct.get("email")
    print(f"account  : user #{pid}  {acct.get('email') or '(none verified)'}")

    # [1] userDetails
    if email:
        print(f"\n[1] userDetails  email={email}")
        try:
            u = get_user_details(email)
            print("    ->", u if u else "NOT FOUND (no account for this email)")
        except CeravisApiError as exc:
            print("    ERROR:", exc)
    else:
        print("\n[1] userDetails skipped — verify an account first, or pass an email")

    # [2] saveCamera payload
    cams = CameraConfig().get_all()
    base = _stream_base()
    cameras = [{
        "device": c.camera_id, "model": "", "supplier": "",
        "room": room_to_enum(c.room_name),
        "url": f"{base}/stream.mjpeg/{c.camera_id}",
    } for c in cams]
    print(f"\n[2] saveCamera payload — {len(cameras)} camera(s), patientUserId={pid}")
    print(json.dumps({"patientUserId": pid, "cameras": cameras}, indent=2))

    # [3] optionally send
    if send:
        if not pid:
            print("\n    cannot send: no verified account (run the setup verify first)")
            return 1
        if not cameras:
            print("\n    cannot send: no cameras registered")
            return 1
        print("\n[3] sending saveCamera…")
        try:
            res = save_cameras(pid, cameras)
            print("    server ->", res, "  ✓ CALL HIT")
        except CeravisApiError as exc:
            print("    ERROR:", exc)
            return 1
    else:
        print("\n(add --send to actually call userDetails + saveCamera)")

    # [4] optional test alert
    if "--alert" in sys.argv:
        if not pid:
            print("\n[4] cannot send alert: no verified account")
            return 1
        print("\n[4] sending test saveAlert (FALL)…")
        try:
            res = save_alert(pid, "FALL", "Test alert from device sanity check")
            print("    server ->", res, "  ✓ CALL HIT")
        except CeravisApiError as exc:
            print("    ERROR:", exc)
            return 1

    # [5] optional test snapshot
    if "--snapshot" in sys.argv:
        if not pid:
            print("\n[5] cannot send snapshot: no verified account")
            return 1
        snap = _latest_snapshot()
        if snap:
            b64 = base64.b64encode(open(snap, "rb").read()).decode("ascii")
            print(f"\n[5] sending saveSnapshot using {snap} ({len(b64)} b64 bytes)…")
        else:
            import numpy as np  # placeholder when no real snapshot exists yet
            import cv2
            ok, buf = cv2.imencode(".jpg", np.full((240, 320, 3), 60, np.uint8))
            b64 = base64.b64encode(buf.tobytes()).decode("ascii")
            print(f"\n[5] no saved snapshot — sending a placeholder ({len(b64)} b64 bytes)…")
        picked = _pick_camera(cams) if cams else None
        cam = room_to_enum(picked.room_name) if picked else "LIVE_FEED"
        try:
            res = save_snapshot(pid, b64, "CRITICAL · Fall detected · test", cam)
            print("    server ->", res, "  ✓ CALL HIT")
        except CeravisApiError as exc:
            print("    ERROR:", exc)
            return 1

    # [6] simulate a real fall: live snapshot + matching FALL alert, together
    if "--fall" in sys.argv:
        if not pid:
            print("\n[6] cannot simulate fall: no verified account")
            return 1
        if not cams:
            print("\n[6] cannot simulate fall: no cameras registered")
            return 1
        cam = _pick_camera(cams)                 # the recipient's camera, not cams[0]
        label = _fall_label(acct, cam)
        room = room_to_enum(cam.room_name)
        jpg = _live_jpeg(cam.camera_id)
        if jpg is None:
            snap = _latest_snapshot()
            jpg = open(snap, "rb").read() if snap else None
        if jpg is None:
            print("\n[6] no frame available (service running? camera up?)")
            return 1
        b64 = base64.b64encode(jpg).decode("ascii")
        print(f"\n[6] simulating FALL on {cam.camera_id} ({cam.room_name})")
        print("    label:", label)
        try:
            a = save_alert(pid, "FALL", label)
            print("    saveAlert  ->", a, "  ✓")
            s = save_snapshot(pid, b64, label, room)
            print("    saveSnapshot ->", s, "  ✓")
        except CeravisApiError as exc:
            print("    ERROR:", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
