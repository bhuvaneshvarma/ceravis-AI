#!/usr/bin/env python3
"""
Sanity check for the two camera health alarms — the checks that catch a camera
which is running perfectly and still useless.

Both exist because of HOW the real faults hid. A 4K camera pointed at its 720p
sub-stream, and a camera streaming HEVC, each report healthy on every field the
system had: running, steady fps, zero reconnects, MediaMTX path ready. Nothing
measured the resolution, and the codec was only whispered into a log — so the
AI ran on a fraction of the pixels and live view was a black screen, for weeks,
without the status surface saying a word.

Asserted here:
  * a sub-stream resolution raises a warning naming the camera and the size
  * a real main stream (1080p and up) does not
  * exactly 720p is caught — that is the common sub-stream size, not a
    borderline case to be generous about
  * H.265 is flagged as unplayable, saying so about BOTH live view and the
    recordings, and naming the fix
  * a camera with no frames or no codec yet (starting up, offline) is silent,
    never a false alarm

    python tests/test_camera_health_alarms.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from ingestion.camera_status import (                       # noqa: E402
    SUBSTREAM_MAX_HEIGHT, codec_warning, substream_warning,
)

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# --------------------------------------------------------------------------
print("\n1. the real bug: a C260 pointed at /stream2")
w = substream_warning("LIVING_ROOM", 1280, 720)
check("720p is flagged", w is not None)
check("the message names the camera", "LIVING_ROOM" in (w or ""))
check("the message states the actual size", "1280x720" in (w or ""))
check("the message says what to fix", "rtsp_url" in (w or ""))

# --------------------------------------------------------------------------
print("\n2. correctly configured cameras stay silent")
check("1440p main (the C220 on /stream1) is fine",
      substream_warning("LOUNGE", 2560, 1440) is None)
check("4K main (the C260 on /stream1) is fine",
      substream_warning("LIVING_ROOM", 3840, 2160) is None)
check("1080p main is fine", substream_warning("HALL", 1920, 1080) is None)

# --------------------------------------------------------------------------
print("\n3. boundary and degenerate cases")
check(f"exactly {SUBSTREAM_MAX_HEIGHT}p is caught, not excused",
      substream_warning("X", 1280, SUBSTREAM_MAX_HEIGHT) is not None)
check("one pixel above the line is accepted",
      substream_warning("X", 1280, SUBSTREAM_MAX_HEIGHT + 1) is None)
check("360p (the C220 sub-stream) is caught",
      substream_warning("X", 640, 360) is not None)
check("no frames yet -> silent, never a false alarm",
      substream_warning("X", 0, 0) is None)
check("a half-reported size -> silent",
      substream_warning("X", 1280, 0) is None)

# --------------------------------------------------------------------------
print("\n4. the codec alarm: H.265 is unplayable everywhere that matters")
w = codec_warning("LIVING_ROOM", "h265")
check("h265 is flagged", w is not None)
check("it says live view is black", "black" in (w or "").lower())
check("it says the recordings are affected too",
      "recording" in (w or "").lower())
check("it names the fix", "H.264" in (w or ""))
check("h264 is silent", codec_warning("LOUNGE", "h264") is None)
check("an unknown codec is flagged rather than assumed fine",
      codec_warning("X", "vp9") is not None)
check("no codec reported yet -> silent, never a false alarm",
      codec_warning("X", None) is None)

# --------------------------------------------------------------------------
print("\n5. the recorder REFUSES a codec nobody can play back")
# Writing HEVC segments is worse than writing nothing: hundreds of MB an hour of
# footage that looks present in the timeline and shows a black player the moment
# somebody actually needs it.
import recording.controller as rc                            # noqa: E402
from recording.controller import RecordingController         # noqa: E402

ctrl = RecordingController.__new__(RecordingController)
ctrl._codec_seen = {}
live_codec = {"LOUNGE": "h264", "LIVING_ROOM": "h265", "STARTING": None}
rc.path_codec = lambda cid: live_codec[cid]

check("an h264 camera records", ctrl._recordable("LOUNGE") is True)
check("an h265 camera is refused", ctrl._recordable("LIVING_ROOM") is False)
check("a camera whose path is not ready yet is NOT withheld",
      ctrl._recordable("STARTING") is True)

calls = []
rc.path_codec = lambda cid: (calls.append(cid), live_codec[cid])[1]
ctrl._recordable("LOUNGE"); ctrl._recordable("LOUNGE")
check("the answer is cached, not re-asked every tick", calls == [])

ctrl._codec_seen["LIVING_ROOM"] = (0.0, False)      # stale -> forces a recheck
live_codec["LIVING_ROOM"] = "h264"
check("a camera fixed at the camera end recovers with no restart",
      ctrl._recordable("LIVING_ROOM") is True)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All camera health alarm checks passed.")
