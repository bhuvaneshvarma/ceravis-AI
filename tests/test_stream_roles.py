#!/usr/bin/env python3
"""
Sanity check for the TWO roles a camera plays, and when they need two pulls.

The bench forced this. The C260's biggest stream is 2560x1440 HEVC — the AI's
best possible input, and a black screen in every browser (no browser decodes
HEVC over WebRTC, and recordings are remuxed into a browser-played container).
Its only H.264 profile is 1280x720. No single profile serves both consumers, so
that camera gets two.

The invariant that keeps this honest: **the second pull only happens when it
earns itself.** A second stream on a WiFi camera is bandwidth taken straight
from the first, which is what destabilised this system before. A camera whose
biggest stream is already H.264 (the C220) must stay on ONE connection.

Also pinned: "nearest to the target", not "largest at or below it". Asked for
1080p, a camera offering 1440p and 360p must give 1440p — the at-or-below rule
hands back 360p and silently destroys the picture. That was a real trap in the
policy this replaces.

No camera, no network — Profile records and path names only.

    python tests/test_stream_roles.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from onvif.client import Profile, recommend_streams                  # noqa: E402
from schemas.cameras import Camera                                   # noqa: E402
import livestream.mediamtx_client as mtx                             # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def prof(token, enc, w, h, observed="") -> Profile:
    """`enc` = what ONVIF claims; `observed` = what ffprobe read off the wire."""
    return Profile(token=token, name=token, encoding=enc, width=w, height=h,
                   fps=20, encoder_token=token, has_ptz=False,
                   observed_codec=observed)


# The two bench cameras, exactly as tools.camera reported them on the device.
C260 = [prof("profile_1", "H264", 2560, 1440, "hevc"),   # claims H264, IS HEVC
        prof("profile_2", "H264", 1280, 720, "h264"),
        prof("profile_3", "JPEG", 640, 360)]
C220 = [prof("profile_1", "H264", 2560, 1440, "h264"),
        prof("profile_2", "H264", 640, 360, "h264"),
        prof("profile_3", "JPEG", 640, 360)]

# --------------------------------------------------------------------------
print("\n1. C220 — its best stream is already playable, so ONE pull")
view, ai, why = recommend_streams(C220, view_height=1080)
check("viewers get the 1440p H.264 main", (view.width, view.height) == (2560, 1440))
check("NOT the 360p sub (the at-or-below trap)", view.height != 360)
check("no second stream is opened", ai is None)
check("and it says so", "one stream" in why)

# --------------------------------------------------------------------------
print("\n2. C260 — its best stream is unplayable, so viewers and AI split")
view, ai, why = recommend_streams(C260, view_height=1080)
check("viewers get the playable 720p H.264", (view.width, view.height) == (1280, 720))
check("viewers never get HEVC", view.codec == "H264")
check("the AI gets the BIGGER stream", (ai.width, ai.height) == (2560, 1440))
check("even though it is HEVC (NVDEC decodes it)", ai.codec == "H265")
check("the reason explains the second stream", "more reach" in why)

# --------------------------------------------------------------------------
print("\n3. a camera with a genuine 1080p H.264 main -> exact match, one pull")
ideal = [prof("profile_1", "H264", 1920, 1080, "h264"),
         prof("profile_2", "H264", 1280, 720, "h264")]
view, ai, why = recommend_streams(ideal, view_height=1080)
check("viewers get 1080p exactly", (view.width, view.height) == (1920, 1080))
check("one pull", ai is None)

# --------------------------------------------------------------------------
print("\n4. an all-HEVC camera -> honest failure, still one pull")
hevc = [prof("profile_1", "H264", 3840, 2160, "hevc"),
        prof("profile_2", "H264", 1280, 720, "hevc")]
view, ai, why = recommend_streams(hevc, view_height=1080)
check("something is returned rather than nothing", view is not None)
check("but it states plainly that it cannot play",
      "cannot play in a browser" in why)
check("no pointless second pull", ai is None)

# --------------------------------------------------------------------------
print("\n5. the MediaMTX paths follow, and only split when they must")
mtx.edge_prefix = lambda: "abc123/"

one = Camera(room_name="LOUNGE", rtsp_url="rtsp://cam/stream1")
two = Camera(room_name="LIVING ROOM", rtsp_url="rtsp://cam/stream2",
             ai_rtsp_url="rtsp://cam/stream1")

check("single-stream camera: the AI reads the LIVE path",
      mtx.ai_path_name(one) == mtx.stream_path(one.camera_id) == "abc123/LOUNGE")
check("split camera: the AI reads its own slash-free path",
      mtx.ai_path_name(two) == "LIVING_ROOM-ai")
check("but viewers still get the prefixed live path (frp routes on it)",
      mtx.stream_path(two.camera_id) == "abc123/LIVING_ROOM")
check("recording follows VIEWERS, never the AI stream",
      mtx.record_source_name(two) == "abc123/LIVING_ROOM")
check("the AI loopback URL points at the AI path",
      mtx.ai_local_rtsp_url(two).endswith("/LIVING_ROOM-ai"))
check("and at the live path when there is no split",
      mtx.ai_local_rtsp_url(one).endswith("/abc123/LOUNGE"))

# --------------------------------------------------------------------------
print("\n6. dropping the second stream returns the camera to one connection")
two.ai_rtsp_url = ""
check("the AI falls back to the live path with no other change",
      mtx.ai_path_name(two) == "abc123/LIVING_ROOM")

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All stream-role checks passed.")
