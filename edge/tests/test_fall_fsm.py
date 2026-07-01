"""
Local test for the scene-aware fall FSM (no TRT needed).

Run:  PYTHONPATH=edge python edge/tests/test_fall_fsm.py
"""
from datetime import datetime, timedelta, timezone

from pose.pose_schema import Keypoint, PoseEstimation
from pose.posture_classifier import PostureTracker


def _kp(coords: dict) -> list:
    out = []
    for i in range(17):
        if i in coords:
            x, y = coords[i]
            out.append(Keypoint(x=float(x), y=float(y), confidence=0.9))
        else:
            out.append(Keypoint(x=0.0, y=0.0, confidence=0.0))
    return out


STANDING = {0: (200, 100), 1: (195, 98), 2: (205, 98), 3: (190, 100), 4: (210, 100),
            5: (190, 150), 6: (210, 150), 11: (195, 300), 12: (205, 300),
            13: (195, 400), 14: (205, 400), 15: (195, 500), 16: (205, 500)}
# horizontal, head dropped near the floor (big y)
FALLEN = {0: (150, 460), 1: (148, 458), 2: (152, 458), 3: (145, 460), 4: (155, 460),
          5: (200, 450), 6: (210, 452), 11: (300, 450), 12: (310, 452),
          13: (360, 450), 14: (370, 452), 15: (420, 450), 16: (430, 452)}
# bending over: torso horizontal but head still HIGH (not near floor), slow
BENDING = {0: (200, 250), 1: (198, 248), 2: (202, 248), 3: (196, 250), 4: (204, 250),
           5: (200, 250), 6: (210, 252), 11: (300, 250), 12: (310, 252),
           13: (300, 360), 14: (310, 360), 15: (300, 460), 16: (310, 460)}

floor_q = lambda x, y: y >= 400          # floor zone = lower part of frame


def _feed(tracker, cam, tid, coords, t0, n, dt=0.1):
    t = t0
    fired = False
    for k in range(n):
        pose = PoseEstimation(track_id=tid, camera_id=cam, frame_id=k,
                              timestamp=t, keypoints=_kp(coords))
        tracker.update(cam, tid, pose, floor_query=floor_q)
        if tracker.confirm_fall(cam, tid):
            fired = True
        t = t + timedelta(seconds=dt)
    return fired, t


def test_real_fall_fires():
    tr = PostureTracker()
    t0 = datetime.now(timezone.utc)
    # standing a moment
    _, t1 = _feed(tr, "c", 1, STANDING, t0, 10)
    # sudden collapse to the floor, then lie still for ~4 s
    fired, _ = _feed(tr, "c", 1, FALLEN, t1, 45)
    print(f"[real fall] confirmed={fired}")
    assert fired, "a genuine fall onto the floor should confirm"
    print("[real fall] PASS")


def test_bending_does_not_fire():
    tr = PostureTracker()
    t0 = datetime.now(timezone.utc)
    _, t1 = _feed(tr, "c", 2, STANDING, t0, 10)
    # bend over (horizontal torso) but head stays high, hold a while
    fired, _ = _feed(tr, "c", 2, BENDING, t1, 45)
    print(f"[bending] confirmed={fired}")
    assert not fired, "bending over (head not near floor) must NOT confirm a fall"
    print("[bending] PASS")


def test_slow_settle_on_floor_fires():
    # an elderly faint: head ends near the floor even without a big impact spike
    tr = PostureTracker()
    t0 = datetime.now(timezone.utc)
    _, t1 = _feed(tr, "c", 3, STANDING, t0, 10)
    fired, _ = _feed(tr, "c", 3, FALLEN, t1, 45)
    print(f"[slow settle] confirmed={fired}")
    assert fired, "lying motionless on the floor should confirm (near-floor path)"
    print("[slow settle] PASS")


if __name__ == "__main__":
    test_real_fall_fires()
    test_bending_does_not_fire()
    test_slow_settle_on_floor_fires()
    print("ALL FALL-FSM TESTS PASSED")
