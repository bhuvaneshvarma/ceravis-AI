#!/usr/bin/env python3
"""
The wording of every event the edge sends: saveAlert messageText, the
saveSnapshot annotation, the fall clip, the local event store.

The line it replaces, as a carer saw it in the app:
    No motion 2/15 min · LOUNGE · Care · 8:33 AM, 09 Sep 2026
— a snapshot counter where a duration belongs, the room in capitals, the care
recipient's first name ("Care"), and the time crammed onto the same line.

Now, for every trigger:
  1. exactly two lines — what happened + where, then when
  2. never a name; the recipient is only ever "the care recipient"
  3. nothing repeated: no severity prefix, no camera number, a move names its
     rooms once
  4. stillness states the real elapsed time, never "n/15"
  5. places read as names; a mixed-case name is kept as typed
  6. ONE builder: the cloud publisher and the enricher both use it

Pure string checks — no camera, no network, no engine.

    python tests/test_event_wording.py
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from alerts.alert_format import describe, duration, place, stamp  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  [{detail!r}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


AT = "2026-09-09T08:33:00+05:30"


def ev(event_type: str, title: str, *, room="LOUNGE", zone=None, detail=None,
       co=None, held=None, at=AT):
    return SimpleNamespace(event_type=event_type, title=title, room_name=room,
                           zone_name=zone, detail=detail, co_present=co,
                           duration_secs=held, timestamp=at)


# --------------------------------------------------------------------------
print("\n1. every trigger, exactly as the app will show it")
CASES = [
    (ev("fall", "Fall detected"),
     "Fall detected in Lounge"),
    (ev("fall", "Fall detected", zone="floor"),
     "Fall detected in Lounge (Floor)"),
    (ev("no_motion", "No motion", held=3600),
     "No motion for 1 h in Lounge"),
    (ev("no_motion_snapshot", "No motion", held=3660),
     "No motion for 1 h 1 min in Lounge"),
    (ev("no_motion_snapshot", "No motion", zone="sofa", held=4440),
     "No motion for 1 h 14 min in Lounge (Sofa)"),
    (ev("no_transition_snapshot", "No transition", detail="sitting", held=3780),
     "No transition for 1 h 3 min in Lounge · still sitting"),
    (ev("standing_up", "Stood up"), "Stood up in Lounge"),
    (ev("sitting_down", "Sat down"), "Sat down in Lounge"),
    (ev("walking_started", "Started walking"), "Started walking in Lounge"),
    (ev("walking_stopped", "Stopped walking"), "Stopped walking in Lounge"),
    (ev("lying_down", "Lying down", zone="bed"), "Lying down in Lounge (Bed)"),
    (ev("room_transition", "Changed room", room="LOUNGE", detail="KITCHEN → LOUNGE"),
     "Moved from Kitchen to Lounge"),
    (ev("area_transition", "Moved area", zone="table", detail="sofa → table"),
     "Moved from Sofa to Table in Lounge"),
    (ev("visitor_motion_snapshot", "Visitor moving"), "Visitor moving in Lounge"),
    (ev("visitor_motion_snapshot", "Visitor moving", co="with the care recipient"),
     "Visitor moving in Lounge · with the care recipient"),
    (ev("standing_up", "Stood up", co="with 2 visitors"),
     "Stood up in Lounge · with 2 visitors"),
]
for e, want in CASES:
    got = describe(e)
    check(f"{e.event_type:<24} -> {want}", got == f"{want}\n8:33 AM, 9 Sep 2026", got)


# --------------------------------------------------------------------------
print("\n2. rules that hold for EVERY trigger")
texts = [describe(e) for e, _ in CASES]
check("exactly two lines, the time alone on the second",
      all(len(t.split("\n")) == 2 and t.split("\n")[1] == "8:33 AM, 9 Sep 2026"
          for t in texts))
check("no severity prefix (the AlertType already carries it)",
      not any("CRITICAL" in t or "INFO" in t for t in texts))
check("no camera number (saveSnapshot carries cameraNumber)",
      not any("Camera " in t for t in texts))
check("no snapshot counter", not any("/15" in t for t in texts))
check("no care-recipient name — the builder is never given one",
      not any(w in t for t in texts for w in ("Care ·", "Ravi", "recipient ·")))
check("a move names its rooms once",
      describe(CASES[11][0]).count("Lounge") == 1)


# --------------------------------------------------------------------------
print("\n3. the pieces")
check("durations: minutes", duration(45 * 60) == "45 min")
check("durations: whole hours", duration(7200) == "2 h")
check("durations: hours and minutes", duration(3720) == "1 h 2 min")
check("durations: rounded to the minute", duration(3629) == "1 h")
check("place: CAPS -> Title", place("LIVING_ROOM") == "Living Room")
check("place: lower -> Title", place("kitchen counter") == "Kitchen Counter")
check("place: mixed case kept as typed", place("TV room") == "TV room")
check("place: apostrophes survive", place("KID'S ROOM") == "Kid's Room")
check("place: empty stays empty", place(None) == "" and place("  ") == "")
check("time: no leading zeros, 12-hour", stamp("2026-09-09T08:03:00+05:30")
      == "8:03 AM, 9 Sep 2026")
check("time: afternoon", stamp("2026-12-25T15:45:00+05:30") == "3:45 PM, 25 Dec 2026")
check("time: an unparseable value is shown, not dropped", stamp("sometime") == "sometime")
multi = describe(ev("fall", "Fall detected"), "frame 2 of 3")
check("a multi-frame suffix joins line 1, the time stays alone on line 2",
      multi == "Fall detected in Lounge · frame 2 of 3\n8:33 AM, 9 Sep 2026", multi)
check("no timestamp -> one line, no dangling break",
      describe(ev("fall", "Fall detected", at="")) == "Fall detected in Lounge")
check("an un-enriched event still reads (title from the type)",
      describe(ev("fall", None)).startswith("Fall in Lounge"))


# --------------------------------------------------------------------------
print("\n4. ONE builder, every reader")
pub = io.open(EDGE / "alerts/cloud_alert_publisher.py", encoding="utf-8").read()
enr = io.open(EDGE / "events/event_enricher.py", encoding="utf-8").read()
check("the cloud publisher sends describe(event)", "message = describe(event)" in pub)
check("snapshots and dropped-event labels use it too",
      pub.count("describe(event") >= 3, str(pub.count("describe(event")))
check("the local event store gets the same text", "event.message = describe(event)" in enr)
check("no second wording left in the publisher",
      "_ARROWS" not in pub and "def _format" not in pub and "def _head" not in pub)
check("no name is read anywhere on the way out",
      "firstName" not in pub and "_recipient_name" not in enr)


if FAILURES:
    print(f"\n{len(FAILURES)} FAILED: " + "; ".join(FAILURES))
    sys.exit(1)
print("\nall event-wording checks passed")
