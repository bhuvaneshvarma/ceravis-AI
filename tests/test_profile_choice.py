#!/usr/bin/env python3
"""
Sanity check for WHICH profile a camera gets consumed on.

This is the decision that broke the bench twice, so it is a pure function with
a test rather than a judgement made in a wizard. A camera exposes several
profiles and we consume exactly ONE — the same stream reaches the AI, the
recordings, the /ui tiles and the public live links. Get it wrong once at
registration and nothing complains afterwards: the camera reports running,
steady fps, zero reconnects, forever.

Both real failures are covered:
  * the C260 stored with its 720p SUB-stream while its main was 4K (weeks of
    720p AI input and 720p recordings, invisible)
  * the C260's 4K main being H.265 — a black screen in every browser, and
    recordings that cannot be played back at all

The ranking under test: H.264 as a hard requirement, then the largest
resolution at or below the preferred height, then (if all are taller) the
smallest of them.

No camera, no network — Profile records only.

    python tests/test_profile_choice.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from onvif.client import Profile, recommend_profile, usable_profiles  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def prof(token, enc, w, h, fps=20, ptz=False, observed="") -> Profile:
    """`enc` is what ONVIF CLAIMS; `observed` is what ffprobe read off the wire."""
    return Profile(token=token, name=token, encoding=enc, width=w, height=h,
                   fps=fps, encoder_token="enc", has_ptz=ptz,
                   observed_codec=observed)


# The bench cameras as they really are.
C260 = [prof("profile_1", "H265", 3840, 2160),      # main: HEVC, unplayable
        prof("profile_2", "H264", 1280, 720),       # sub
        prof("profile_3", "JPEG", 640, 360)]        # stills only
C260_H264 = [prof("profile_1", "H264", 2560, 1440),  # after the Tapo app fix
             prof("profile_2", "H264", 1280, 720),
             prof("profile_3", "JPEG", 640, 360)]
C220 = [prof("profile_1", "H264", 2560, 1440),
        prof("profile_2", "H264", 640, 360),
        prof("profile_3", "JPEG", 640, 360)]

# --------------------------------------------------------------------------
print("\n1. JPEG is never a candidate")
check("dropped from the usable list",
      all(p.encoding != "JPEG" for p in usable_profiles(C260)))
check("the rest are ordered largest first",
      [p.token for p in usable_profiles(C260)] == ["profile_1", "profile_2"])

# --------------------------------------------------------------------------
print("\n2. C220 -> its 1440p main (the target exactly)")
best, why = recommend_profile(C220, preferred_height=1440)
check("picks the 1440p main", best.token == "profile_1")
check("not the 360p sub", best.height == 1440)
check("says why", "1440" in why and "H.264" in why)

# --------------------------------------------------------------------------
print("\n3. C260 as found: 4K H.265 main + 720p H.264 sub")
best, why = recommend_profile(C260, preferred_height=1440)
check("REFUSES the H.265 main even though it is much bigger",
      best.token == "profile_2")
check("takes the playable 720p instead", (best.width, best.height) == (1280, 720))
check("a playable small stream beats an unplayable big one",
      best.encoding == "H264")

# --------------------------------------------------------------------------
print("\n4. C260 once its main is H.264 -> 1440p, not 4K, not 720p")
best, why = recommend_profile(C260_H264, preferred_height=1440)
check("picks the 1440p main", (best.width, best.height) == (2560, 1440))
check("does NOT fall back to the sub-stream (the original bug)",
      best.token == "profile_1")

# --------------------------------------------------------------------------
print("\n5. a lower target still picks the largest that fits")
best, _ = recommend_profile(C260_H264, preferred_height=1080)
check("1080 target -> the 720p profile (nothing between)",
      (best.width, best.height) == (1280, 720))
best, _ = recommend_profile(C260_H264, preferred_height=4000)
check("a target above everything -> the biggest H.264",
      (best.width, best.height) == (2560, 1440))

# --------------------------------------------------------------------------
print("\n6. every H.264 profile is above the target -> the smallest of them")
tall = [prof("a", "H264", 3840, 2160), prof("b", "H264", 2560, 1440)]
best, why = recommend_profile(tall, preferred_height=720)
check("takes 1440p, not 4K", (best.width, best.height) == (2560, 1440))
check("explains that nothing fit", "smallest" in why)

# --------------------------------------------------------------------------
print("\n7. no H.264 anywhere -> pick the best available and say it is broken")
best, why = recommend_profile([prof("a", "H265", 3840, 2160)], preferred_height=1440)
check("returns something rather than nothing", best is not None)
check("but states plainly that it will not work",
      "cannot play in a browser" in why)

# --------------------------------------------------------------------------
print("\n8. a camera with only JPEG -> no choice at all")
best, why = recommend_profile([prof("a", "JPEG", 640, 360)], preferred_height=1440)
check("returns None", best is None)
check("says the camera has no video profile", "no video profile" in why)

# --------------------------------------------------------------------------
print("\n9. the camera LIES about its codec - evidence beats the claim")
# The real C260, exactly as the bench found it: ONVIF reports H264 for both the
# 4K and the 1440p profile, and ffprobe shows both are actually HEVC. Ranking on
# the claim picked the 1440p one and produced a black tile; ranking on the
# observed codec must reject both.
liar = [prof("profile_1", "H264", 3840, 2160, observed="hevc"),
        prof("profile_1b", "H264", 2560, 1440, observed="hevc"),
        prof("profile_2", "H264", 1280, 720, observed="h264")]
check("hevc normalises to H265 regardless of the claim",
      liar[0].codec == "H265" and liar[1].codec == "H265")
best, why = recommend_profile(liar, preferred_height=1440)
check("REFUSES the 1440p profile ONVIF called H264",
      best.token != "profile_1b")
check("picks the only genuinely playable stream",
      best.token == "profile_2" and best.codec == "H264")

# --------------------------------------------------------------------------
print("\n10. with no ffprobe the claim is all we have, and is used")
blind = [prof("a", "H264", 2560, 1440), prof("b", "H264", 1280, 720)]
best, _ = recommend_profile(blind, preferred_height=1440)
check("falls back to the claimed encoding", best.token == "a")
check("codec reads through from the claim", best.codec == "H264")

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All profile-choice checks passed.")
