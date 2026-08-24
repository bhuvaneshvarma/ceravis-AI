"""
Local test for the scene-aware fall FSM (no TRT needed).

Run:  PYTHONPATH=edge python edge/tests/test_fall_fsm.py
"""
from datetime import timedelta

from pose.pose_schema import Keypoint, PoseEstimation
from pose.posture_classifier import PostureTracker
from common import clock                       # noqa: E402


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


def _feed(tracker, cam, tid, coords, t0, n, dt=0.1, fq=floor_q):
    t = t0
    fired = False
    for k in range(n):
        pose = PoseEstimation(track_id=tid, camera_id=cam, frame_id=k,
                              timestamp=t, keypoints=_kp(coords))
        tracker.update(cam, tid, pose, floor_query=fq)
        if tracker.confirm_fall(cam, tid):
            fired = True
        t = t + timedelta(seconds=dt)
    return fired, t


def test_fall_fires_immediately():
    # A fall must alert the INSTANT it is detected — right after the label
    # confirms (fall_confirmation_frames), with NO immobility wait.
    from config.settings import settings
    tr = PostureTracker()
    t0 = clock.now()
    _, t1 = _feed(tr, "c", 1, STANDING, t0, 10)
    # feed FALLEN frame-by-frame and record WHEN it fires
    t = t1
    fired_at = 0
    for k in range(10):
        pose = PoseEstimation(track_id=1, camera_id="c", frame_id=k,
                              timestamp=t, keypoints=_kp(FALLEN))
        tr.update("c", 1, pose, floor_query=floor_q)
        if tr.confirm_fall("c", 1):
            fired_at = k + 1
            break
        t = t + timedelta(seconds=0.1)
    print(f"[immediate] fired after {fired_at} FALLEN frame(s)")
    assert fired_at, "a fall onto the floor must confirm"
    assert fired_at <= settings.fall_confirmation_frames + 1, \
        "must fire as soon as the label confirms — no post-fall wait"
    print("[immediate] PASS")


def test_recovered_fall_still_fires():
    # Fell and immediately got back up (never lay still): still a fall.
    tr = PostureTracker()
    t0 = clock.now()
    _, t1 = _feed(tr, "c", 2, STANDING, t0, 10)
    fired, t2 = _feed(tr, "c", 2, FALLEN, t1, 5)     # brief — then up
    _feed(tr, "c", 2, STANDING, t2, 10)
    print(f"[recovered] confirmed={fired}")
    assert fired, "a fall the person recovered from must still alert"
    print("[recovered] PASS")


def test_bending_does_not_fire():
    # Floor zone present; bending keeps the head HIGH (outside the floor) — the
    # near-floor discriminator must keep it from firing.
    tr = PostureTracker()
    t0 = clock.now()
    _, t1 = _feed(tr, "c", 3, STANDING, t0, 10)
    fired, _ = _feed(tr, "c", 3, BENDING, t1, 45)
    print(f"[bending] confirmed={fired}")
    assert not fired, "bending over (head not near floor) must NOT confirm a fall"
    print("[bending] PASS")


def test_no_zone_horizontal_fires():
    # No floor zone drawn -> a confirmed horizontal is taken as a fall (fail
    # loud), and it fires immediately (no hold).
    tr = PostureTracker()
    t0 = clock.now()
    no_ref = lambda x, y: None
    fired, _ = _feed(tr, "c", 4, FALLEN, t0, 6, fq=no_ref)
    print(f"[no-zone] confirmed={fired}")
    assert fired, "without a floor zone, a confirmed horizontal must alert"
    print("[no-zone] PASS")


def test_require_near_floor_suppresses_no_zone():
    # With fall_require_near_floor=True and no zone, we can't prove near-floor,
    # so it must NOT fire (the opt-in stricter mode).
    from config.settings import settings
    tr = PostureTracker()
    t0 = clock.now()
    old = settings.fall_require_near_floor
    settings.fall_require_near_floor = True
    try:
        no_ref = lambda x, y: None
        fired, _ = _feed(tr, "c", 5, FALLEN, t0, 20, fq=no_ref)
    finally:
        settings.fall_require_near_floor = old
    print(f"[strict/no-zone] confirmed={fired}")
    assert not fired, "strict mode without a floor zone must not confirm"
    print("[strict/no-zone] PASS")


if __name__ == "__main__":
    test_fall_fires_immediately()
    test_recovered_fall_still_fires()
    test_bending_does_not_fire()
    test_no_zone_horizontal_fires()
    test_require_near_floor_suppresses_no_zone()
    print("ALL FALL-FSM TESTS PASSED")
