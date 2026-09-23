#!/usr/bin/env python3
"""
Prove posture-change events are announced only once the new posture HOLDS.

2026-09-23 on the bench: someone pausing between steps flipped WALKING <->
STANDING every second, and each flip became a walking_started/_stopped event
with a cloud snapshot (77 in a day). A change is now announced only after the
new posture has held for settings.posture_event_dwell_secs.

Covered:
  a flip that comes back inside the hold time is never announced;
  a posture that holds is announced once, with the right event type;
  sit/stand transitions still fire after the hold;
  memory is bounded to the recipient's current track.

Pure python; no TensorRT. Runs on the dev box:

    python tests/test_posture_events.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

from config.settings import settings                      # noqa: E402
from pose.posture_classifier import Posture                # noqa: E402
from rules import posture_rule                             # noqa: E402
from rules.posture_rule import PostureRule                 # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


NOW = [1000.0]
posture_rule.time.monotonic = lambda: NOW[0]      # a clock the test drives


class Ctx:
    def __init__(self) -> None:
        self.sighting = None

    def find_recipient(self, _now):
        return self.sighting


def see(ctx: Ctx, rule: PostureRule, posture: Posture, track: int = 1,
        advance: float = 0.5) -> list[str]:
    NOW[0] += advance
    ctx.sighting = NS(camera_id="LIVING_ROOM", track=NS(track_id=track),
                      posture=posture, identity=NS(recipient_id="76"))
    return [e.event_type for e in rule.evaluate(ctx)]


settings.posture_event_dwell_secs = 3.0
ctx, rule = Ctx(), PostureRule()

print("\n1. walking <-> standing flicker is not narrated")
out = see(ctx, rule, Posture.STANDING)                      # first sight
for _ in range(6):                                          # 1 s cycles
    out += see(ctx, rule, Posture.WALKING)
    out += see(ctx, rule, Posture.STANDING)
check("no event for sub-second flips", out == [])

print("\n2. a posture that holds is announced once")
out = []
for _ in range(10):                                         # 5 s of walking
    out += see(ctx, rule, Posture.WALKING)
check("walking_started exactly once", out == ["walking_started"])
out = []
for _ in range(10):
    out += see(ctx, rule, Posture.STANDING)
check("walking_stopped exactly once", out == ["walking_stopped"])

print("\n3. sit / stand still fire after the hold")
out = []
for _ in range(8):
    out += see(ctx, rule, Posture.SITTING)
check("sitting_down after it held", out == ["sitting_down"])
out = []
for _ in range(8):
    out += see(ctx, rule, Posture.STANDING)
check("standing_up after it held", out == ["standing_up"])

print("\n4. only the current track is remembered")
for tid in range(2, 50):
    see(ctx, rule, Posture.STANDING, track=tid)
check("state holds one track, not 49", len(rule._state) == 1)
check("a new track's first posture is not an event",
      see(ctx, rule, Posture.SITTING, track=99) == [])

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All posture-event checks passed.")
