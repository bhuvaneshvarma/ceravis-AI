"""
Face evidence inside the ONE lock decision (reid/target_lock.py).

The bars come from a joint body+face test on the bench (2026-09-23): a new
lock needs body >= acquire, OR body >= verify AND a confirming face; a visible
face that is clearly someone else vetoes a candidate and releases a lock; a
face too small to trust, an infrared camera, or no face at all leave the
body-only rules exactly as they were.

Run:  PYTHONPATH=edge python edge/tests/test_face_fusion.py
"""
import numpy as np

from config.settings import settings
from reid.face_identity import FaceGallery
from reid.target_lock import TargetLockManager, NightContext
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


BOXES = {1: (100, 100, 180, 400)}
V = settings.reid_match_threshold            # verify bar
A = settings.reid_acquire_min_score          # new-lock bar
FC, FV, PX = settings.face_confirm_score, settings.face_veto_score, settings.face_min_px


def lock(body: float, face=None, boxes=BOXES, night=None, mgr=None):
    mgr = mgr or TargetLockManager(Gallery())
    faces = {} if face is None else {1: face}
    out = mgr.update("CAM", boxes, lambda tid: np.array([body], np.float32), night,
                     face_for=(lambda tid, rid: faces.get(tid)))
    return mgr, out


print("\n1. a new lock")
_, out = lock(A + 0.02)
check("body over the new-lock bar, no face: locks", out.target_track_id == 1)
_, out = lock((V + A) / 2)
check("body between the bars, no face: no lock", out.target_track_id is None)
_, out = lock((V + A) / 2, face=(FC + 0.05, PX + 20))
check("body between the bars + confirming face: locks", out.target_track_id == 1)
_, out = lock(A + 0.05, face=(FV - 0.1, PX + 20))
check("strong body but a face that is someone else: VETOED", out.target_track_id is None)
_, out = lock(A + 0.05, face=(FV - 0.1, PX - 20))
check("a face too small to trust is not evidence: locks on body", out.target_track_id == 1)
_, out = lock(A + 0.05, face=(FV - 0.1, PX + 20), night=NightContext(ir=True))
check("infrared: a face never vetoes (no night face data)", out.target_track_id == 1)
_, out = lock(V - 0.05, face=(FC + 0.2, PX + 20))
check("a face cannot lock a body below the verify bar", out.target_track_id is None)

print("\n2. an existing lock")
mgr, out = lock(A + 0.02)
_, out = lock(A + 0.02, face=(FV - 0.1, PX + 20), mgr=mgr)
check("a clear face mismatch releases it at once", out.released and out.target_track_id is None)
mgr, out = lock(A + 0.02)
_, out = lock(V - 0.10, face=(FC + 0.05, PX + 20), mgr=mgr)
check("body drift (new clothes) + confirming face: held", out.target_track_id == 1)
check("...and that look may be learned", out.face_confirmed and out.adaptive is not None)
mgr, out = lock(A + 0.02)
_, out = lock(A + 0.02, mgr=mgr)
check("no face: verified on body as before", out.target_track_id == 1 and not out.face_confirmed)

print("\n3. no face evidence at all == the body-only decision")
for body in (V - 0.05, (V + A) / 2, A + 0.02):
    m1 = TargetLockManager(Gallery())
    a = m1.update("CAM", BOXES, lambda t: np.array([body], np.float32))
    b = lock(body)[1]
    check(f"body {body:.2f}: same outcome with and without face_for",
          a.target_track_id == b.target_track_id)

print("\n4. the gallery and the per-track face look")
g = FaceGallery()
e = np.eye(128, dtype=np.float32)
g.rebuild({"ravi": e[:3]})
check("best cosine against the recipient's faces", abs(g.score(e[1], "ravi") - 1.0) < 1e-6)
check("unknown recipient -> no score", g.score(e[1], "nobody") is None)
fb = TrackFeatureBuffer()
z = np.zeros(4, np.float32)
fb.update("CAM", 1, z, z, 1, None)
fb.set_face("CAM", 1, e[0], 55.0)
fb.update("CAM", 1, z, z, 2, None)
rec = fb.get("CAM", 1)
check("the face look survives the per-tick body update",
      rec.face is not None and rec.face_px == 55.0)

print()
if FAILURES:
    raise SystemExit(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
print("All face-fusion checks passed.")
