"""
Face evidence inside the ONE lock decision (reid/target_lock.py) — face first.

Bars from a joint body+face test on the bench (2026-09-24). Evidence is a
track's USABLE face looks of the last few seconds, never one glance:
  confirm  face_confirm_min_looks looks >= face_confirm_score -> locks the
           recipient whatever their clothes (body bar and clothes-based vetoes
           do not apply), holds a lock through body drift, lets the look be learned;
  veto     face_veto_min_looks usable looks, ALL < face_veto_score -> vetoes a
           candidate, releases a lock;
  weak     a readable face that does not vouch -> no NEW lock (an existing
           lock is kept);
  pending  not looked at yet -> a NEW lock waits for the look.
An infrared camera (the night rule set) or no usable face leave the body-only
rules exactly as they were.

Run:  PYTHONPATH=edge python edge/tests/test_face_fusion.py
"""
import numpy as np

from config.settings import settings
from reid.face_identity import FaceGallery, yaw_ratio
from reid.target_lock import TargetLockManager, NightContext
from config import scene_rules
from tracking.track_feature_buffer import TrackFeatureBuffer

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


class Gallery:
    """Body score = the feature's first value (a test stand-in for FAISS)."""
    def match(self, feat, **_kw):
        class M:
            pass
        m = M()
        m.score = float(feat[0])
        m.recipient_id = "ravi"
        m.view_label = None
        m.is_match = m.score >= settings.reid_match_threshold
        return m


settings.face_enabled = True                   # this file tests the face mechanism itself
BOXES = {1: (100, 100, 180, 400)}
V = settings.reid_match_threshold            # verify bar
A = settings.reid_acquire_min_score          # new-lock bar
FC, FV, FA = settings.face_confirm_score, settings.face_veto_score, settings.face_acquire_score
NV, NC = settings.face_veto_min_looks, settings.face_confirm_min_looks


def looks(score: float, n: int):
    """Evidence of n usable looks, all scoring `score`."""
    return (score, n, n if score >= FC else 0)


def lock(body: float, face=None, boxes=BOXES, night=None, mgr=None):
    """face: looks(score, n); None = looked, no usable face; "unseen" = not
    looked at yet."""
    mgr = mgr or TargetLockManager(Gallery())
    faces = {} if face == "unseen" else {1: (None, 0, 0) if face is None else face}
    out = mgr.update("CAM", boxes, lambda tid: np.array([body], np.float32), night,
                     face_for=(lambda tid, rid: faces.get(tid)))
    return mgr, out


print("\n1. a new lock")
_, out = lock(A + 0.02)
check("body over the new-lock bar, no face: locks", out.target_track_id == 1)
_, out = lock((V + A) / 2)
check("body between the bars, no face: no lock", out.target_track_id is None)
_, out = lock(V - 0.10, face=looks(FC + 0.05, NC))
check("FACE FIRST: a confirming face locks even a body in other clothes",
      out.target_track_id == 1 and out.face_confirmed)
_, out = lock(V - 0.10, face=looks(FC + 0.05, NC - 1))
check("...but not on a single passing look", out.target_track_id is None)
_, out = lock(A + 0.05, face=looks(FV - 0.1, NV))
check("strong body but faces that are someone else: VETOED", out.target_track_id is None)
_, out = lock(A + 0.05, face=looks(FV - 0.1, 1))
check("one bad look is no veto — but it does not vouch for a NEW lock either",
      out.target_track_id is None)
NA = scene_rules.NIGHT.reid_acquire_min_score   # the night set's new-lock bar
_, out = lock(NA + 0.05, face=looks(FV - 0.1, NV), night=NightContext(ir=True))
check("infrared: a face never vetoes (no night face data)", out.target_track_id == 1)
_, out = lock(A + 0.15, face=looks((FV + FA) / 2, 1))
check("strong body, readable face that does not vouch: no NEW lock", out.target_track_id is None)
_, out = lock(A + 0.05, face=looks(FA + 0.02, 1))
check("strong body, readable face that vouches: locks", out.target_track_id == 1)
_, out = lock(A + 0.05, face="unseen")
check("strong body, face not looked at yet: waits for the look (no new lock)",
      out.target_track_id is None)
two = {1: (100, 100, 180, 400), 2: (600, 100, 680, 400)}
mgr = TargetLockManager(Gallery())
o2 = mgr.update("CAM", two, lambda tid: np.array([V - 0.1], np.float32),
                face_for=lambda tid, rid: looks(FC + 0.05, NC))
check("two faces that both confirm: pick nobody", o2.target_track_id is None)

print("\n2. an existing lock")
mgr, out = lock(A + 0.02)
_, out = lock(A + 0.02, face=looks(FV - 0.1, NV), mgr=mgr)
check("faces that are clearly someone else release it at once",
      out.released and out.target_track_id is None)
mgr, out = lock(A + 0.02)
_, out = lock(A + 0.02, face=looks(FV - 0.1, NV - 1), mgr=mgr)
check("...but fewer bad looks than the veto needs keep it (a turned face)",
      out.target_track_id == 1 and not out.released)
mgr, out = lock(A + 0.02)
_, out = lock(V - 0.10, face=looks(FC + 0.05, NC), mgr=mgr)
check("body drift (new clothes) + confirming face: held", out.target_track_id == 1)
check("...and that look may be learned", out.face_confirmed and out.adaptive is not None)
mgr, out = lock(A + 0.02)
_, out = lock(A + 0.02, mgr=mgr)
check("no face: verified on body as before", out.target_track_id == 1 and not out.face_confirmed)
mgr, out = lock(A + 0.02)
_, out = lock(A + 0.02, face="unseen", mgr=mgr)
check("an existing lock is held while its face look is pending", out.target_track_id == 1)
mgr, out = lock(A + 0.02)
_, out = lock(A + 0.15, face=looks(FV - 0.1, NV), mgr=mgr)
_, out = lock(A + 0.15, mgr=mgr)
check("after a face veto the same track is not re-locked on body alone", out.target_track_id is None)
_, out = lock(A + 0.15, face=looks(FC + 0.05, NC), mgr=mgr)
check("...but a confirming face may lock it again", out.target_track_id == 1)
mgr, out = lock(A + 0.02)
_, out = lock(settings.reid_adaptive_min_score + 0.05, face=looks((FV + FA) / 2, 1), mgr=mgr)
check("a locked track whose readable face does not vouch is NOT learned from",
      out.target_track_id == 1 and out.adaptive is None)
_, out = lock(settings.reid_adaptive_min_score + 0.05, mgr=mgr)
check("...with no face in view, a strong body look still is", out.adaptive is not None)

print("\n3. no face evidence at all == the body-only decision")
for body in (V - 0.05, (V + A) / 2, A + 0.02):
    m1 = TargetLockManager(Gallery())
    a = m1.update("CAM", BOXES, lambda t: np.array([body], np.float32))
    b = lock(body)[1]
    check(f"body {body:.2f}: same outcome with and without face_for",
          a.target_track_id == b.target_track_id)

print("\n4. the gallery, the per-track face looks, and the turn of a face")
g = FaceGallery()
e = np.eye(512, dtype=np.float32)
g.rebuild({"ravi": e[:3]})
check("best cosine against the recipient's faces", abs(g.score(e[1], "ravi") - 1.0) < 1e-6)
check("unknown recipient -> no score", g.score(e[1], "nobody") is None)
fb = TrackFeatureBuffer()
z = np.zeros(4, np.float32)
fb.update("CAM", 1, z, z, 1, None)
fb.set_face("CAM", 1, e[0], 70.0)
fb.set_face("CAM", 1, None, 0.0)               # a look without a usable face
fb.update("CAM", 1, z, z, 2, None)
rec = fb.get("CAM", 1)
check("usable looks survive the per-tick body update; an empty look is not stored",
      len(rec.face_looks) == 1 and rec.face is not None and rec.face_px == 70.0)
for i in range(10):
    fb.set_face("CAM", 1, e[i], 70.0)
check("only the last few looks are kept", len(fb.get("CAM", 1).face_looks) == 6)
front = np.zeros(15, np.float32); front[4:10] = [40, 50, 60, 50, 50, 60]
side = front.copy(); side[8] = 62
check("turn of a face: frontal ~0, the nose past an eye ~0.6",
      yaw_ratio(front) < 0.05 and yaw_ratio(side) > settings.face_max_yaw_ratio)

print("\n5. a new face look is an identity event (no 20 s wait for the heartbeat)")
from reid.reid_runner import ReIDRunner                        # noqa: E402
rr = ReIDRunner.__new__(ReIDRunner)
rr._seen_tracks, rr._last_match, rr._last_face = {}, {}, {}
ids = frozenset({1, 2})
rr._identity_event("CAM", ids, 0.0)                            # first sighting
check("nothing new -> skipped", rr._identity_event("CAM", ids, 0.0) is None)
check("a new face look -> evaluated now", rr._identity_event("CAM", ids, 5.0) == "face look")
check("...once", rr._identity_event("CAM", ids, 5.0) is None)

print()
if FAILURES:
    raise SystemExit(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
print("All face-fusion checks passed.")
