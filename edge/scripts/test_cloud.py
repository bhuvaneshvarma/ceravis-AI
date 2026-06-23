#!/usr/bin/env python3
"""
CERAVIS cloud connectivity sanity check — run ON THE DEVICE.

    cd ~/ceravis/edge
    python scripts/test_cloud.py                 # inspect config + payload (no send)
    python scripts/test_cloud.py --send          # actually POST userDetails + saveCamera
    python scripts/test_cloud.py someone@x.com --send

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
        cam = room_to_enum(cams[0].room_name) if cams else "LIVE_FEED"
        try:
            res = save_snapshot(pid, b64, "CRITICAL · Fall detected · test", cam)
            print("    server ->", res, "  ✓ CALL HIT")
        except CeravisApiError as exc:
            print("    ERROR:", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
