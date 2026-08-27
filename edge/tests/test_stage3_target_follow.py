"""
Stage 3: follow ONE recipient across cameras, and never mislabel them.

The bugs this pins down, in order of how badly they hurt:

  * the recipient leaves a camera and is treated as a NEW person (a visitor) on
    the next one, instead of being re-identified as the same recipient;
  * on a lost track, whoever scores highest is grabbed — a look-alike with the
    same score can inherit the lock;
  * a neighbour standing NEAR (not yet overlapping) the recipient contributes
    pixels to what gets learned as "the recipient".

Every mechanism here is arithmetic over signals the pipeline already produces —
no new model. Each is tested in BOTH directions: it must hold the recipient AND
refuse an impostor, because a follower that locks nobody looks identical to one
that works until the day it locks the wrong person.

Run:  PYTHONPATH=edge python edge/tests/test_stage3_target_follow.py
"""
import sys
import time

import numpy as np

from config.settings import settings
from reid.identity_buffer import IdentityBuffer
from reid.identity_schema import Identity
from reid.target_lock import TargetLockManager
from reid.target_registry import TargetRegistry
from reid.track_memory import TrackMemory


D = 8
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def vec(*parts) -> np.ndarray:
    v = np.zeros(D, dtype=np.float32)
    for i, c in parts:
        v[i] = c
    return unit(v)


TARGET = vec((0, 1.0))
OTHER = vec((1, 1.0))                       # nothing like the target


class _Gallery:
    """One enrolled recipient, "ravi", at TARGET. Score = cosine to TARGET."""
    def match(self, feat):
        class M:
            pass
        m = M()
        m.score = float(np.dot(unit(feat), TARGET))
        m.recipient_id = "ravi"
        m.view_label = None
        m.margin = 1.0
        m.is_match = m.score >= settings.reid_match_threshold
        return m


def _box(x, w=90):
    return (float(x), 100.0, float(x + w), 400.0)


print("\n1. the recipient is followed onto the NEXT camera, not seen as new")
mgr = TargetLockManager(_Gallery())
a = mgr.update("camA", {1: _box(200)}, lambda t: TARGET)
check("locked on camera A", a.target_track_id == 1 and a.recipient_id == "ravi")
# same body walks into camB under a brand-new track id
b = mgr.update("camB", {5: _box(300)}, lambda t: TARGET)
check("re-acquired on camera B as the SAME recipient",
      b.target_track_id == 5 and b.recipient_id == "ravi")
check("camB carries an is_target identity, not a blank",
      b.identities.get(5, (None, False))[1] is True)


print("\n2. a lost track WIDENS the search instead of silently holding camA")
mgr2 = TargetLockManager(_Gallery())
mgr2.update("camA", {1: _box(200)}, lambda t: TARGET)
# the recipient's track is gone; only a stranger remains on camA
lost = mgr2.update("camA", {2: _box(210)}, lambda t: OTHER)
check("no target on camA now", lost.target_track_id is None)
check("but the recipient is NOT dropped — it reports 'lost' to widen the search",
      lost.lost is True and lost.released is False)
check("the stranger did NOT inherit the lock",
      lost.identities.get(2, (None, False))[1] is False
      or 2 not in lost.identities)


print("\n3. two look-alikes -> lock NOBODY (precision over a coin-flip)")
mgr3 = TargetLockManager(_Gallery())
twin_a = unit(np.array([0.98, 0.20, 0, 0, 0, 0, 0, 0]))     # ~0.98 to target
twin_b = unit(np.array([0.97, 0.24, 0, 0, 0, 0, 0, 0]))     # ~0.97 to target
feats = {1: twin_a, 2: twin_b}
out = mgr3.update("camX", {1: _box(100), 2: _box(400)}, lambda t: feats[t])
check("both clear the gallery bar", _Gallery().match(twin_a).is_match
      and _Gallery().match(twin_b).is_match)
check("yet the manager refuses to guess between them",
      out.target_track_id is None,
      "a tie within reid_target_pick_margin must lock nobody")
# once one of them is alone, the ambiguity is gone and it locks
solo = mgr3.update("camX", {1: _box(100)}, lambda t: twin_a)
check("and locks cleanly once the tie is gone", solo.target_track_id == 1)


print("\n4. a candidate that looks like a KNOWN bystander is vetoed")
mem = TrackMemory()
confuser = unit(np.array([0.72, 0.72, 0, 0, 0, 0, 0, 0]))   # ~0.71 to target, clears bar
mem.add_negative(confuser)                                   # ...but is a known non-target
mgr4 = TargetLockManager(_Gallery(), memory=mem)
out = mgr4.update("camY", {1: _box(100)}, lambda t: confuser)
check("the look-alike is a gallery match", _Gallery().match(confuser).is_match)
check("but the negative pool vetoes it -> no lock", out.target_track_id is None)
check("the true recipient is NOT vetoed",
      mgr4.update("camZ", {1: _box(100)}, lambda t: TARGET).target_track_id == 1)


print("\n5. freeze the moment a neighbour is NEAR — learn no foreign pixels")
mgr5 = TargetLockManager(_Gallery())
lock = mgr5.update("camA", {1: _box(200)}, lambda t: TARGET)  # alone -> learns
check("locks and learns when alone", lock.target_track_id == 1
      and lock.adaptive is not None)
near = {1: _box(200), 2: _box(240)}          # neighbour 40px away, boxes do NOT overlap
frozen = mgr5.update("camA", near, lambda t: TARGET if t == 1 else OTHER)
check("still held on the recipient", frozen.target_track_id == 1)
check("but FROZEN: nothing is learned from a frame with a neighbour near",
      frozen.adaptive is None)
apart = mgr5.update("camA", {1: _box(200)}, lambda t: TARGET)
check("learning resumes once they separate", apart.adaptive is not None)


print("\n6. per-camera memory is forgotten after too long lost")
original = settings.target_reacquire_ttl_secs
try:
    settings.target_reacquire_ttl_secs = 0.05
    mgr6 = TargetLockManager(_Gallery())
    mgr6.update("camA", {1: _box(200)}, lambda t: TARGET)
    mgr6.update("camA", {2: _box(210)}, lambda t: OTHER)     # lost, memory kept
    check("memory kept right after loss", "camA" in mgr6._state)
    time.sleep(0.08)
    mgr6.update("camA", {2: _box(210)}, lambda t: OTHER)     # past ttl -> forget
    check("memory forgotten past the reacquire ttl", "camA" not in mgr6._state,
          "a later look-alike near the old spot must not inherit the lock")
finally:
    settings.target_reacquire_ttl_secs = original


print("\n7. the identity buffer is bounded (the leak shape)")
ib = IdentityBuffer()
from common import clock
for tid in range(50):
    ib.update(Identity(track_id=tid, camera_id="camA", frame_id=tid,
                       timestamp=clock.now(), recipient_id=None, is_target=False,
                       confidence=0.5))
check("all recorded", len(ib.get_all()["camA"]) == 50)
ib.prune("camA", {49})
check("prune drops dead tracks", len(ib.get_all()["camA"]) == 1)
check("and keeps the live one", ib.get("camA", 49) is not None)


print("\n8. a visitor snapshot is held for a beat while the recipient is re-found")
from detection.detection_schema import BoundingBox        # noqa: E402
from rules.rule_context import RuleContext                # noqa: E402
from rules.visitor_rule import VisitorRule                # noqa: E402
from tracking.track_buffer import TrackBuffer             # noqa: E402
from tracking.track_schema import Track, TrackResult      # noqa: E402

settings.visitor_snapshot_cooldown_secs = 0.05
settings.visitor_identity_grace_secs = 0.20


def _ctx(reg):
    return RuleContext(frames=None, detections=None, tracks=TrackBuffer(),
                       poses=None, postures=None, posture_tracker=None,
                       identities=IdentityBuffer(), target_registry=reg)


def _feed(ctx, tid, x, fid):
    now = clock.now()
    ctx.tracks.update(TrackResult(camera_id="camA", frame_id=fid, timestamp=now,
        tracks=[Track(track_id=tid, camera_id="camA", frame_id=fid, timestamp=now,
                      bbox=BoundingBox(x1=float(x), y1=100.0, x2=float(x + 90),
                                       y2=400.0), confidence=0.9)]))


# A registry that says "a target is known but not located" -> searching.
reg = TargetRegistry()
reg.lock("camB", 1, "ravi")
reg.unlock("camB")                # known recipient, none fresh -> searching() True
check("registry reports an active search", reg.searching() is True)

rule = VisitorRule()
ctxs = _ctx(reg)
held = []
for k in range(4):                # ~first 0.1s of this track's life, during a search
    _feed(ctxs, 7, 100 + k * 30, k + 1)
    held += rule.evaluate(ctxs)
    time.sleep(0.02)
check("a fresh mover is HELD during the search (could be the recipient)",
      not held, str([e.event_type for e in held]))
time.sleep(0.22)                  # let the grace lapse
after = []
for k in range(4, 9):
    _feed(ctxs, 7, 100 + k * 30, k + 1)
    after += rule.evaluate(ctxs)
    time.sleep(0.02)
check("...then fires once ReID has had its moment", bool(after),
      "held forever would drop genuine visitors during every transition")

# With no search in progress, an ordinary visitor is never held.
reg2 = TargetRegistry()           # never had a target -> searching() False
check("no target ever -> not searching", reg2.searching() is False)
rule2 = VisitorRule()
ctx2 = _ctx(reg2)
normal = []
for k in range(6):
    _feed(ctx2, 8, 100 + k * 30, k + 1)
    normal += rule2.evaluate(ctx2)
    time.sleep(0.02)
check("an ordinary visitor fires with no delay", bool(normal),
      str([e.event_type for e in normal]))


print("\n9. the recipient's OWN recent exit re-finds them in the NEXT room")
# The recipient left camA looking like E (their clothing seconds ago). On camB
# two people BOTH match the enrolled gallery equally — a genuine tie the pick
# margin would refuse to guess. The recipient's exit breaks it: only the real
# recipient also matches E.
E = unit(np.array([0.75, 0, 0.66, 0, 0, 0, 0, 0]))          # recipient's exit look
lookalike = unit(np.array([0.75, 0, 0, 0.66, 0, 0, 0, 0]))  # same gallery score, diff person
check("both clear the gallery equally (a tie)",
      abs(_Gallery().match(E).score - _Gallery().match(lookalike).score) < 1e-6
      and _Gallery().match(E).is_match)

mem9 = TrackMemory()
mem9.observe("camA", 1, E)
mem9.retire("camA", 1, recipient_id="ravi")     # tagged exit from camA
check("target_continuation matches the recipient, not the look-alike",
      (mem9.target_continuation("camB", E, "ravi") or 0) > 0.99
      and (mem9.target_continuation("camB", lookalike, "ravi") or 0)
          < settings.track_memory_min_score)
check("and it is scoped to the recipient id",
      mem9.target_continuation("camB", E, "someone_else") is None)

mgr9 = TargetLockManager(_Gallery(), memory=mem9)
feats9 = {1: E, 2: lookalike}
out9 = mgr9.update("camB", {1: _box(100), 2: _box(400)}, lambda t: feats9[t])
check("the exit boost locks the REAL recipient across the camera",
      out9.target_track_id == 1 and out9.recipient_id == "ravi")

# Without the exit evidence it is an honest tie -> lock nobody (no wrong pick).
mgr9b = TargetLockManager(_Gallery(), memory=TrackMemory())
out9b = mgr9b.update("camB", {1: _box(100), 2: _box(400)}, lambda t: feats9[t])
check("with no exit evidence the tie locks nobody (never the wrong one)",
      out9b.target_track_id is None)


print("\n10. a RELEASED lock stops flagging the old track (the green dot follows)")
# The monitor's dot and the recipient rules both read is_target off the identity
# buffer. The flag is sticky (only the target is ever published), so a released
# lock must have the old track's flag actively cleared or the dot sticks to the
# wrong person.
from common import clock as _clk                                   # noqa: E402
ib10 = IdentityBuffer()
ib10.update(Identity(track_id=3, camera_id="camA", frame_id=1, timestamp=_clk.now(),
                     recipient_id="ravi", is_target=True, confidence=0.8))
ib10.update(Identity(track_id=5, camera_id="camA", frame_id=1, timestamp=_clk.now(),
                     recipient_id=None, is_target=False, confidence=0.4))
check("the target is flagged", ib10.get("camA", 3).is_target is True)
# lock released this tick -> no track is the target
ib10.demote_stale_targets("camA", set())
check("the released track's target flag is cleared",
      ib10.get("camA", 3) is None)
check("a non-target record is left alone", ib10.get("camA", 5) is not None)

# target moved to a NEW track id -> the old flag must not linger alongside it
ib11 = IdentityBuffer()
ib11.update(Identity(track_id=3, camera_id="camA", frame_id=1, timestamp=_clk.now(),
                     recipient_id="ravi", is_target=True, confidence=0.8))
ib11.update(Identity(track_id=9, camera_id="camA", frame_id=2, timestamp=_clk.now(),
                     recipient_id="ravi", is_target=True, confidence=0.9))
ib11.demote_stale_targets("camA", {9})
check("exactly ONE track is the target after a hand-off",
      ib11.get("camA", 3) is None and ib11.get("camA", 9).is_target is True)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll Stage 3 target-follow checks passed.")
