"""
Local test for TargetLockManager (no TRT/faiss needed).

Run:  PYTHONPATH=edge python edge/tests/test_target_lock.py
"""
import numpy as np

from reid.target_lock import TargetLockManager


class _M:
    def __init__(self, rid, score, view, margin, is_match):
        self.recipient_id, self.score, self.view_label = rid, score, view
        self.margin, self.is_match = margin, is_match


class FakeGallery:
    """feat[0] >= 0.5 => enrolled recipient 'R'; else an unenrolled intruder."""
    def match(self, feat):
        if feat[0] >= 0.5:
            return _M("R", 0.9, "front", 0.3, True)
        return _M(None, 0.2, None, 0.0, False)


R = np.array([1.0, 0, 0], dtype=np.float32)      # target appearance
X = np.array([0.0, 1.0, 0], dtype=np.float32)    # intruder appearance


def feat_map(d):
    return lambda tid: d.get(tid)


def test_acquire_verify_freeze_release_reacquire():
    m = TargetLockManager(FakeGallery())
    cam = "cam1"

    # 1) acquire: target track 1 alone
    o = m.update(cam, {1: (100, 100, 180, 400)}, feat_map({1: R}))
    assert o.target_track_id == 1 and o.recipient_id == "R", o
    assert o.adaptive is not None, "should learn when alone + confident"
    print("[acquire] locked track 1 as R, adaptive on  OK")

    # 2) verify
    o = m.update(cam, {1: (104, 100, 184, 400)}, feat_map({1: R}))
    assert o.target_track_id == 1 and o.adaptive is not None
    print("[verify] still locked, adaptive on  OK")

    # 3) occlusion: intruder track 2 overlaps target heavily -> FREEZE
    o = m.update(cam, {1: (104, 100, 184, 400), 2: (110, 100, 190, 400)},
                 feat_map({1: R, 2: X}))
    assert o.target_track_id == 1, "must hold the target through occlusion"
    assert o.adaptive is None, "must NOT learn during occlusion (poisoning)"
    print("[freeze] held lock, adaptive paused during occlusion  OK")

    # 4) release: target track now reads as the intruder while clearly visible
    o = m.update(cam, {1: (300, 100, 380, 400)}, feat_map({1: X}))   # 1st mismatch
    assert o.released is False
    o = m.update(cam, {1: (300, 100, 380, 400)}, feat_map({1: X}))   # 2nd -> release
    assert o.released is True and o.target_track_id is None
    print("[release] dropped a bad lock after 2 mismatches  OK")

    # 5) reacquire on a NEW track id near the last position
    m.update(cam, {7: (100, 100, 180, 400)}, feat_map({7: R}))       # re-acquire as 7
    # 7 vanishes, reappears as 9 near the same spot
    m.update(cam, {}, feat_map({}))
    o = m.update(cam, {9: (108, 100, 188, 400)}, feat_map({9: R}))
    assert o.target_track_id == 9 and o.recipient_id == "R"
    print("[reacquire] restored target on new id 9  OK")

    print("ALL TARGET-LOCK TESTS PASSED")


if __name__ == "__main__":
    test_acquire_verify_freeze_release_reacquire()
