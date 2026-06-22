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
    CeravisApiError, get_user_details, is_configured, room_to_enum, save_cameras,
)


def _ws_base() -> str:
    b = settings.device_stream_base.strip()
    if b:
        return b.rstrip("/")
    ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
    except Exception:
        pass
    return f"ws://{ip}:8000"


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
    ws = _ws_base()
    cameras = [{
        "device": c.camera_id, "model": "", "supplier": "",
        "room": room_to_enum(c.room_name),
        "url": f"{ws}/stream/{c.camera_id}",
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
        print("\n[3] POSTing saveCamera…")
        try:
            res = save_cameras(pid, cameras)
            print("    server ->", res, "  ✓ CALL HIT")
        except CeravisApiError as exc:
            print("    ERROR:", exc)
            return 1
    else:
        print("\n(add --send to actually POST userDetails + saveCamera)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
