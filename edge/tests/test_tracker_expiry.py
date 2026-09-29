"""
BoT-SORT lost-track expiry is WALL TIME, and an empty frame is still a step.

  1. Missed for less than tracker_lost_secs (turned, hidden): the same id
     comes back when they are seen again at the same spot.
  2. Gone longer (the room stayed empty): the next person at that spot gets
     a NEW id — the old one (and its appearance memory) is never inherited.
  3. The same holds at any detection rate: a slow (idle) rate does not keep a
     lost track alive longer.

Run:  PYTHONPATH=edge python edge/tests/test_tracker_expiry.py
"""
import sys

import numpy as np

import tracking.botsort as bs
from tracking.botsort import BoTSORT

failures: list[str] = []
NOW = [1000.0]
bs.time.monotonic = lambda: NOW[0]          # a clock the test drives


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def tracker():
    return BoTSORT(track_high_thresh=0.5, track_low_thresh=0.1, new_track_thresh=0.6,
                   match_thresh=0.8, proximity_thresh=0.5, appearance_thresh=0.25,
                   with_reid=False, lost_secs=2.0)


BOX = np.array([[300.0, 400.0, 120.0, 360.0]], np.float32)
SCORE = np.array([0.9], np.float32)
EMPTY = (np.zeros((0, 4), np.float32), np.zeros(0, np.float32), None)


def seen(bt, dt=0.1):
    NOW[0] += dt
    out = bt.update(BOX, SCORE, None)
    return out[0].track_id if out else None


def empty(bt, secs, dt=0.1):
    for _ in range(int(round(secs / dt))):
        NOW[0] += dt
        bt.update(*EMPTY)


print("\n1. a short miss keeps the id")
bt = tracker()
for _ in range(5):
    tid = seen(bt)
empty(bt, 1.0)
check("missed 1 s, seen again at the same spot: same id", seen(bt) == tid)

print("\n2. an emptied room does not hand the id to the next person")
bt = tracker()
for _ in range(5):
    tid = seen(bt)
empty(bt, 3.0)
seen(bt)                                     # a new track confirms on its 2nd sighting
new = seen(bt)
check("empty 3 s (> lost_secs), someone at the same spot: a NEW id",
      new is not None and new != tid, f"{tid} -> {new}")

print("\n3. the same at a slow (idle) detection rate")
bt = tracker()
for _ in range(5):
    tid = seen(bt)
empty(bt, 3.0, dt=0.5)                       # 2 fps idle rate: only 6 steps
seen(bt, dt=0.5)
new = seen(bt, dt=0.5)
check("3 s at 2 fps also expires the lost track", new is not None and new != tid,
      f"{tid} -> {new}")

if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll tracker-expiry checks passed.")
