#!/usr/bin/env python3
"""
Sanity check for the sub-stream guard — the alarm that would have caught a 4K
camera quietly running on its 720p sub-stream.

This is worth a test because of HOW the real bug hid. A camera pointed at the
wrong RTSP path reports perfectly healthy on every field the system had:
running, steady fps, zero reconnects, MediaMTX path ready, correct codec. The
resolution was the only thing that told the truth, and nothing measured it — so
the AI, the recordings and every live link ran at a fraction of the pixels for
weeks without a single warning.

Asserted here:
  * a sub-stream resolution raises a warning naming the camera and the size
  * a real main stream (1080p and up) does not
  * exactly 720p is caught — that is the common sub-stream size, not a
    borderline case to be generous about
  * a camera with no frames yet (starting up, offline) is silent, never a
    false alarm

    python tests/test_substream_guard.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from ingestion.camera_status import (                       # noqa: E402
    SUBSTREAM_MAX_HEIGHT, substream_warning,
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
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All sub-stream guard checks passed.")
