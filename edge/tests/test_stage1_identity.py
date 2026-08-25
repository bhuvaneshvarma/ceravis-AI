"""
Stage 1 of the identity architecture: quality gating, best-shot selection,
proximity-gated appearance, and event-triggered gallery matching.

No new model — every mechanism here is arithmetic and bookkeeping over signals
the pipeline already produces. Each is tested for BOTH directions: it must
reject what it claims to reject AND pass what it claims to pass, because a gate
that rejects everything looks identical to a gate that works until the day
nobody is ever identified.

Run:  PYTHONPATH=edge python edge/tests/test_stage1_identity.py
"""
import sys

import cv2
import numpy as np

from config.settings import settings
from detection.detection_schema import BoundingBox
from reid import crop_quality
from reid.best_shot import BestShotBuffer
from tracking.tracking_runner import TrackingRunner


FRAME_W, FRAME_H = 640, 480
_RNG = np.random.default_rng(3)
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def _person(w=90, h=220, blur=0):
    """A textured person-ish crop. Texture matters: a flat block has near-zero
    Laplacian variance and would be rejected as 'blurred' for the wrong reason."""
    img = _RNG.integers(60, 200, (h, w, 3), dtype=np.uint8)
    cv2.rectangle(img, (w // 4, h // 8), (3 * w // 4, h // 2), (220, 220, 220), -1)
    cv2.circle(img, (w // 2, h // 10), max(4, w // 8), (240, 240, 240), -1)
    if blur:
        img = cv2.GaussianBlur(img, (blur | 1, blur | 1), 0)
    return img


def _box(x1=200, y1=120, x2=290, y2=340):
    return BoundingBox(x1=float(x1), y1=float(y1), x2=float(x2), y2=float(y2))


class _Det:
    """Minimal stand-in for a Detection — only bbox and confidence are read."""
    def __init__(self, x1, y1, x2, y2, conf=0.9):
        self.bbox = _box(x1, y1, x2, y2)
        self.confidence = conf


print("\n1. quality gate REJECTS what cannot support a decision")
q = crop_quality.assess(np.zeros((0, 0, 3), np.uint8), _box(), FRAME_W, FRAME_H)
check("empty crop", not q.ok, q.reason)

tiny = _person(18, 40)
q = crop_quality.assess(tiny, _box(200, 120, 218, 160), FRAME_W, FRAME_H)
check("too small", not q.ok, q.reason)

wide = _person(200, 120)
q = crop_quality.assess(wide, _box(100, 100, 300, 220), FRAME_W, FRAME_H)
check("implausible aspect (wider than tall)", not q.ok, q.reason)

# hard against the left edge -> a PARTIAL person
q = crop_quality.assess(_person(), _box(0, 120, 90, 340), FRAME_W, FRAME_H)
check("truncated at the frame edge", not q.ok, q.reason)

q = crop_quality.assess(_person(blur=31), _box(), FRAME_W, FRAME_H)
check("motion-blurred", not q.ok, q.reason)


print("\n2. ...and PASSES a good crop (a gate that rejects all is not a gate)")
good = crop_quality.assess(_person(), _box(), FRAME_W, FRAME_H)
check("clean crop accepted", good.ok, good.reason or f"score={good.score}")
check("score is in range", 0.0 < good.score <= 1.0, str(good.score))
sharp = crop_quality.assess(_person(), _box(), FRAME_W, FRAME_H)
soft = crop_quality.assess(_person(blur=7), _box(), FRAME_W, FRAME_H)
if soft.ok:
    check("sharper crop outranks softer", sharp.score > soft.score,
          f"{sharp.score} vs {soft.score}")
else:
    check("softer crop rejected outright", True, soft.reason)


print("\n3. best-shot keeps the BEST looks, not the latest")
buf = BestShotBuffer()
buf.offer("camA", 1, _person(blur=9), crop_quality.assess(
    _person(blur=9), _box(), FRAME_W, FRAME_H), 1)
best_q = crop_quality.assess(_person(), _box(), FRAME_W, FRAME_H)
buf.offer("camA", 1, _person(), best_q, 2)
for i in range(10):                                   # flood with mediocre shots
    c = _person(blur=5)
    buf.offer("camA", 1, c, crop_quality.assess(c, _box(), FRAME_W, FRAME_H), 3 + i)
top = buf.best("camA", 1)
check("a shot is held", top is not None)
check("capacity is bounded", buf.count("camA", 1) <= settings.best_shot_capacity,
      f"{buf.count('camA', 1)} <= {settings.best_shot_capacity}")
if top:
    check("the BEST survives the flood", abs(top.quality.score - best_q.score) < 1e-6,
          f"{top.quality.score} vs best offered {best_q.score}")
buf.offer("camA", 2, _person(), best_q, 20)
buf.prune("camA", {1})
check("prune drops dead tracks", buf.count("camA", 2) == 0)
check("prune keeps live ones", buf.count("camA", 1) > 0)
check("a rejected crop is never stored",
      not buf.offer("camA", 9, _person(18, 40),
                    crop_quality.assess(_person(18, 40), _box(200, 120, 218, 160),
                                        FRAME_W, FRAME_H), 1))


print("\n4. appearance is gated on PROXIMITY, not head-count")
far = [_Det(20, 120, 110, 340), _Det(500, 120, 590, 340)]
near = [_Det(200, 120, 290, 340), _Det(250, 120, 340, 340)]
check("two people far apart need no appearance", not TrackingRunner._crowded(far))
check("two people close DO", TrackingRunner._crowded(near))
check("overlapping pair does", TrackingRunner._crowded(
    [_Det(200, 120, 290, 340), _Det(210, 120, 300, 340)]))
check("a single person never crowds", not TrackingRunner._crowded([far[0]]))
check("nobody never crowds", not TrackingRunner._crowded([]))


print("\n5. gallery matching fires on EVENTS, not every tick")


# _identity_event is pure logic over the track set, so it is exercised on a
# bare instance — no FAISS, no engine, no camera needed.
from reid.reid_runner import ReIDRunner                        # noqa: E402
r = ReIDRunner.__new__(ReIDRunner)
r._seen_tracks, r._last_match = {}, {}

check("first sighting is an event",
      r._identity_event("camA", frozenset({1})) == "first sighting")
check("an unchanged set is NOT an event",
      r._identity_event("camA", frozenset({1})) is None)
check("a new track is an event",
      r._identity_event("camA", frozenset({1, 2})) == "new track")
check("a lost track is an event",
      r._identity_event("camA", frozenset({1})) == "track lost")
check("still nothing when it settles",
      r._identity_event("camA", frozenset({1})) is None)

original = settings.reid_heartbeat_secs
try:
    settings.reid_heartbeat_secs = 0.0            # force the safety net
    check("the heartbeat re-checks a long-held lock",
          r._identity_event("camA", frozenset({1})) == "heartbeat")
finally:
    settings.reid_heartbeat_secs = original

check("cameras are independent",
      r._identity_event("camB", frozenset({1})) == "first sighting")


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll Stage 1 identity checks passed.")
