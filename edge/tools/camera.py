from __future__ import annotations

"""
ceravis-camera — what a camera CLAIMS versus what it actually sends.

Every label in this system has lied at least once. ONVIF's ver10 encoder schema
has no H.265 element, so an HEVC camera can only report "H264"; the same camera
reported H264Profile "Main" on a stream ffprobe read as "High". Those lies are
not cosmetic — an H.265 profile is a black screen in every browser and
recordings nobody can play back, and it looks perfectly healthy from every other
angle.

So this asks the camera, then reads its bitstream, and puts the two side by
side. Use it when a stream will not play and you need to know WHY, per profile,
before changing anything.

    python -m tools.camera --host 192.168.0.250 -u tapo260 -p 'secret'
    python -m tools.camera --camera LIVING_ROOM        # a registered camera
    python -m tools.camera --camera LIVING_ROOM --json

Exit code: 0 when the recommended profile is playable, 1 when it is not.
"""

import argparse
import json
import sys

from common.rtsp import observe_stream
from config.settings import settings
from onvif.client import probe
from onvif.soap import OnvifError


def _rows(result: dict) -> list[dict]:
    """One row per profile: the claim, the evidence, and the verdict."""
    rec = (result.get("recommended") or {}).get("token")
    rows = []
    for p in result.get("profiles", []):
        claimed = (p.get("encoding") or "").upper()
        observed = (p.get("observed_codec") or "").upper()
        # ffprobe says "hevc"; the rest of the system says H265. One name.
        observed = "H265" if observed in ("HEVC", "H265") else observed
        # Profile.codec already normalises hevc -> H265 and prefers the observed
        # value; probe() puts it on the dict as `codec`.
        effective = (p.get("codec") or claimed).upper()
        rows.append({
            "token": p.get("token"),
            "name": p.get("name"),
            "resolution": f"{p.get('width')}x{p.get('height')}",
            "observed_resolution": (f"{p['observed_width']}x{p['observed_height']}"
                                    if p.get("observed_width") else None),
            "claimed": claimed or None,
            "observed": observed or None,
            "agrees": (observed == claimed) if observed else None,
            "playable": effective == "H264",
            "recommended": p.get("token") == rec,
        })
    return rows


def _print(result: dict, rows: list[dict]) -> None:
    dev = result.get("device", {})
    print(f"\n{dev.get('manufacturer', '?')} {dev.get('model', '?')}  "
          f"serial={dev.get('serial', '?')}  fw={dev.get('firmware', '?')}")
    print(f"{'PROFILE':<12}{'RESOLUTION':<14}{'ONVIF SAYS':<12}{'REALLY IS':<12}"
          f"{'PLAYS?':<9}")
    print("  " + "-" * 66)
    for r in rows:
        stills = r["claimed"] == "JPEG"
        verdict = "stills" if stills else ("yes" if r["playable"] else "NO")
        note = ""
        if stills:
            note = "   (snapshot profile, never a live stream)"
        elif r["observed"] and not r["agrees"]:
            note = "   <-- the camera's claim is WRONG"
        elif not r["observed"]:
            note = "   (could not read the stream)"
        star = "*" if r["recommended"] else " "
        print(f"{star} {r['token']:<10}{r['resolution']:<14}"
              f"{r['claimed'] or '-':<12}{r['observed'] or '-':<12}{verdict:<9}{note}")
    print("  " + "-" * 66)
    print(f"  * = what CERAVIS would consume "
          f"(target height {settings.camera_preferred_height})")
    print(f"  {(result.get('recommended') or {}).get('reason', '')}\n")

    bad = [r for r in rows
           if r["observed"] and not r["playable"] and r["claimed"] != "JPEG"]
    if bad:
        print("  Why a stream will not play:")
        print("    No browser decodes H.265 over WebRTC, and recordings are")
        print("    remuxed as-is, so an H.265 profile is a black live view AND")
        print("    unplayable clips. Change the codec in the camera's own app,")
        print("    or use a profile listed as playable above.\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ceravis-camera",
        description="What a camera claims over ONVIF vs what it really streams.")
    ap.add_argument("--camera", help="a REGISTERED camera_id (uses its stored creds)")
    ap.add_argument("--host", help="camera IP, for one not yet registered")
    ap.add_argument("--port", type=int, default=2020, help="ONVIF port (default 2020)")
    ap.add_argument("-u", "--username", default="")
    ap.add_argument("-p", "--password", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.camera:
        from configuration.camera_config import CameraConfig
        cam = CameraConfig().get_by_id(args.camera)
        if cam is None:
            print(f"no such camera: {args.camera}")
            return 2
        xaddr = cam.onvif_xaddr
        user, pw = cam.onvif_username or "", cam.onvif_password or ""
        if not xaddr:
            print(f"{args.camera} has no ONVIF endpoint (added manually).")
            # Still worth reading the stream it is actually configured on.
            seen = observe_stream(cam.rtsp_url)
            print(json.dumps(seen, indent=2) if args.json else f"  wire: {seen}")
            return 0 if (seen or {}).get("codec") == "h264" else 1
    elif args.host:
        xaddr = f"http://{args.host}:{args.port}/onvif/device_service"
        user, pw = args.username, args.password
    else:
        ap.error("give --camera <id> or --host <ip>")

    try:
        result = probe(xaddr, user, pw,
                       preferred_height=settings.camera_preferred_height)
    except OnvifError as exc:
        print(f"could not interrogate the camera: {exc}")
        return 2

    rows = _rows(result)
    if args.json:
        print(json.dumps({"device": result.get("device"),
                          "profiles": rows,
                          "recommended": result.get("recommended")}, indent=2))
    else:
        _print(result, rows)
    rec = next((r for r in rows if r["recommended"]), None)
    return 0 if (rec and rec["playable"]) else 1


if __name__ == "__main__":
    sys.exit(main())
