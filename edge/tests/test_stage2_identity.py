"""
Stage 2: the adaptive-contamination guard, track memory, and auto-negatives.

The contamination test is the important one. Adaptive learning used to resume
the moment two boxes stopped OVERLAPPING, but crop_person pads the box — so a
neighbour merely standing NEAR is already inside the crop being filed under the
recipient's name. The resulting drift is self-reinforcing and every score
involved looks healthy the whole way down, which is exactly why it needs a test
rather than a review.

Run:  PYTHONPATH=edge python edge/tests/test_stage2_identity.py
"""
import sys
import time

import numpy as np

from config.settings import settings
from reid.target_lock import TargetLockManager
from reid.track_memory import TrackMemory


D = 8
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def vec(*parts) -> np.ndarray:
    v = np.zeros(D, dtype=np.float32)
    for i, c in parts:
        v[i] = c
    return v / np.linalg.norm(v)


TARGET = vec((0, 1.0))
OTHER = vec((1, 1.0))


class _Gallery:
    def match(self, feat):
        class M:
            pass
        m = M()
        m.score = float(np.dot(np.asarray(feat, np.float32), TARGET))
        m.recipient_id = "ravi"
        m.view_label = None
        m.is_match = m.score >= settings.reid_match_threshold
        return m


def _boxes(gap_px: int):
    """Target at x=200 (90 wide) and a neighbour `gap_px` to its right.
    Boxes never OVERLAP in any of these cases — that is the whole point."""
    return {1: (200.0, 100.0, 290.0, 400.0),
            2: (290.0 + gap_px, 100.0, 380.0 + gap_px, 400.0)}


def _feat(tid):
    return TARGET if tid == 1 else OTHER


print("\n1. adaptive capture requires SOLITUDE, not merely non-overlap")
check("alone -> learning allowed",
      TargetLockManager._alone({1: (200.0, 100.0, 290.0, 400.0)}, 1))
check("neighbour far away -> allowed",
      TargetLockManager._alone(_boxes(600), 1))
check("neighbour NEAR but NOT overlapping -> BLOCKED",
      not TargetLockManager._alone(_boxes(40), 1),
      "this is the contamination case the old IoU gate missed")
check("neighbour touching -> blocked", not TargetLockManager._alone(_boxes(1), 1))
check("a missing track is never 'alone'",
      not TargetLockManager._alone(_boxes(600), 99))


print("\n2. ...and the lock manager honours it end to end")
mgr = TargetLockManager(_Gallery())
out = mgr.update("camA", {1: (200.0, 100.0, 290.0, 400.0)}, _feat)
check("locks the target when alone", out.target_track_id == 1)
check("and captures adaptive", out.adaptive is not None)

mgr2 = TargetLockManager(_Gallery())
out2 = mgr2.update("camB", _boxes(40), _feat)
check("still locks the target with a neighbour near", out2.target_track_id == 1)
check("but REFUSES to learn from that frame", out2.adaptive is None,
      "a padded crop here contains the neighbour")

out3 = mgr2.update("camB", {1: (200.0, 100.0, 290.0, 400.0)}, _feat)
check("learning resumes once they separate", out3.adaptive is not None)


print("\n3. track memory remembers everyone, not just the target")
mem = TrackMemory()
mem.observe("camA", 1, TARGET)
mem.observe("camA", 2, OTHER)
check("live tracks held", mem.stats()["live_tracks"] == 2, str(mem.stats()))

mem.prune("camA", {1}, boxes={2: (600.0, 100.0, 690.0, 400.0)},
          frame_w=640, frame_h=480)
st = mem.stats()
check("a vanished track becomes an exit record", st["exit_records"] == 1, str(st))
check("the surviving track stays live", st["live_tracks"] == 1, str(st))


print("\n4. a new track is matched against RECENT EXITS, not the whole gallery")
hit = mem.match_candidate("camB", OTHER)
check("the person who just left camA is found on camB", hit is not None)
if hit:
    rec, score = hit
    check("it is the right record", rec.camera_id == "camA" and rec.track_id == 2)
    check("with a strong score", score > 0.9, f"{score:.3f}")
check("a stranger matches nobody", mem.match_candidate("camB", vec((5, 1.0))) is None)
check("the SAME camera is excluded (the tracker owns that case)",
      mem.match_candidate("camA", OTHER) is None)

original = settings.track_memory_transit_secs
try:
    settings.track_memory_transit_secs = 0.05
    time.sleep(0.1)
    check("an exit older than the transit window stops being a candidate",
          mem.match_candidate("camB", OTHER) is None)
finally:
    settings.track_memory_transit_secs = original


print("\n5. negatives harvest themselves")
mem2 = TrackMemory()
check("an empty pool vetoes nothing", mem2.negative_score(OTHER) == 0.0)
mem2.add_negative(OTHER)
check("a known non-target scores high", mem2.negative_score(OTHER) > 0.99,
      f"{mem2.negative_score(OTHER):.3f}")
check("the recipient does NOT", mem2.negative_score(TARGET) < 0.1,
      f"{mem2.negative_score(TARGET):.3f}")

for _ in range(settings.reid_negative_pool_max + 50):
    mem2.add_negative(OTHER)
check("the pool is bounded",
      mem2.stats()["negatives"] <= settings.reid_negative_pool_max,
      str(mem2.stats()["negatives"]))


print("\n6. bounded everywhere (the leak shape that already bit two buffers)")
mem3 = TrackMemory()
for i in range(settings.track_memory_per_track * 5):
    mem3.observe("camA", 1, TARGET)
mem3.prune("camA", set())
rec = mem3.stats()
check("per-track embeddings capped",
      rec["exit_records"] == 1 and rec["live_tracks"] == 0, str(rec))
mem4 = TrackMemory()
for i in range(settings.track_memory_max_exits + 20):
    mem4.observe("camA", i, TARGET)
    mem4.prune("camA", set())
check("exit records capped",
      mem4.stats()["exit_records"] <= settings.track_memory_max_exits,
      str(mem4.stats()["exit_records"]))


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll Stage 2 identity checks passed.")
