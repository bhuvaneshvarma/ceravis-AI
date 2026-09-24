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
    # No floor zone drawn -> a confirmed horizontal whose hips dropped is taken
    # as a fall (fail loud), and it fires immediately (no hold).
    tr = PostureTracker()
    t0 = clock.now()
    no_ref = lambda x, y: None
    _, t1 = _feed(tr, "c", 4, STANDING, t0, 10, fq=no_ref)
    fired, _ = _feed(tr, "c", 4, FALLEN, t1, 6, fq=no_ref)
    print(f"[no-zone] confirmed={fired}")
    assert fired, "without a floor zone, a confirmed horizontal must alert"
    print("[no-zone] PASS")


# Seated at a desk, legs hidden, then slumping forward onto it: the torso goes
# horizontal but the hips stay on the seat (the bench's 42 false falls).
SEATED = {0: (200, 180), 1: (198, 178), 2: (202, 178), 3: (196, 180), 4: (204, 180),
          5: (195, 220), 6: (215, 220), 11: (200, 330), 12: (210, 330)}
SLUMPED = {0: (330, 300), 1: (328, 298), 2: (332, 298), 3: (326, 300), 4: (334, 300),
           5: (300, 305), 6: (305, 312), 11: (200, 330), 12: (210, 330)}


def test_slump_at_desk_is_not_a_fall():
    # No floor zone (the bench): only the hip descent can tell this apart.
    from pose.posture_classifier import Posture
    tr = PostureTracker()
    no_ref = lambda x, y: None
    _, t1 = _feed(tr, "c", 6, SEATED, clock.now(), 10, fq=no_ref)
    fired, t2 = _feed(tr, "c", 6, SLUMPED, t1, 60, fq=no_ref)
    pose = PoseEstimation(track_id=6, camera_id="c", frame_id=0, timestamp=t2,
                          keypoints=_kp(SLUMPED))
    label = tr.update("c", 6, pose, floor_query=no_ref).posture
    print(f"[slump] confirmed={fired} label={label.value}")
    assert not fired, "slumping onto a desk (hips on the seat) must NOT be a fall"
    assert label != Posture.FALLEN, "and must not be labelled FALLEN either"
    print("[slump] PASS")


def test_fall_across_an_id_switch_fires():
    # The tracker swaps ids mid-fall: the new track is born lying down, but the
    # old track stood at that spot a moment ago — the hip drop still counts.
    tr = PostureTracker()
    no_ref = lambda x, y: None
    _, t1 = _feed(tr, "c", 7, STANDING, clock.now(), 10, fq=no_ref)
    fired, _ = _feed(tr, "c", 8, FALLEN, t1, 6, fq=no_ref)
    print(f"[id-switch] confirmed={fired}")
    assert fired, "a fall must survive a tracker id switch"
    print("[id-switch] PASS")


def test_neighbour_standing_beside_does_not_lend_height():
    # A person bends over (new track, born bent) right beside someone who keeps
    # standing: the standing neighbour is another person, not a lost id.
    from datetime import timedelta
    tr = PostureTracker()
    no_ref = lambda x, y: None
    t = clock.now()
    beside = {k: (x + 60, y - 80) for k, (x, y) in STANDING.items()}
    fired = False
    for k in range(30):
        for tid, coords in ((20, beside), (21, SLUMPED)):
            pose = PoseEstimation(track_id=tid, camera_id="c", frame_id=k,
                                  timestamp=t, keypoints=_kp(coords))
            tr.update("c", tid, pose, floor_query=no_ref)
            fired |= tr.confirm_fall("c", tid)
        t += timedelta(seconds=0.1)
    print(f"[neighbour] confirmed={fired}")
    assert not fired, "a neighbour still in view must not lend their hip height"
    print("[neighbour] PASS")


def test_found_lying_alone_does_not_fire():
    # Nobody was upright here within the window (already lying when first
    # seen — a bed, a sofa): no descent, no fall. The stillness rule owns a
    # person found lying still.
    tr = PostureTracker()
    no_ref = lambda x, y: None
    fired, _ = _feed(tr, "c", 9, FALLEN, clock.now(), 30, fq=no_ref)
    print(f"[found-lying] confirmed={fired}")
    assert not fired, "no observed descent -> not a fall"
    print("[found-lying] PASS")


def test_rolling_off_a_bed_fires():
    # Lying on a bed (horizontal, hips high) then rolling to the floor: the
    # horizontal streak never breaks, but the hips drop — a fall.
    lying_on_bed = {k: (x, y - 150) for k, (x, y) in FALLEN.items()}
    tr = PostureTracker()
    no_ref = lambda x, y: None
    before, t1 = _feed(tr, "c", 10, lying_on_bed, clock.now(), 20, fq=no_ref)
    fired, _ = _feed(tr, "c", 10, FALLEN, t1, 6, fq=no_ref)
    print(f"[bed-roll] on bed={before} rolled off={fired}")
    assert not before and fired, "lying on a bed is not a fall; rolling off it is"
    print("[bed-roll] PASS")


def test_hip_drop_off_restores_any_horizontal():
    from config.settings import settings
    old = settings.fall_min_hip_drop
    settings.fall_min_hip_drop = 0.0
    try:
        tr = PostureTracker()
        fired, _ = _feed(tr, "c", 11, FALLEN, clock.now(), 6, fq=lambda x, y: None)
    finally:
        settings.fall_min_hip_drop = old
    print(f"[drop-off] confirmed={fired}")
    assert fired, "FALL_MIN_HIP_DROP=0 must keep the old any-horizontal behaviour"
    print("[drop-off] PASS")


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


def test_departed_tracks_are_forgotten():
    # Track ids only grow; a track unseen for a minute must not be kept forever.
    tr = PostureTracker()
    t = clock.now()
    for k in range(600):
        _, t = _feed(tr, "c", 100 + k, STANDING, t, 2, dt=0.5)
    print(f"[forget] states kept={len(tr._state)} of 600 tracks")
    assert len(tr._state) < 200, "departed tracks must be dropped"
    print("[forget] PASS")


if __name__ == "__main__":
    test_fall_fires_immediately()
    test_recovered_fall_still_fires()
    test_bending_does_not_fire()
    test_no_zone_horizontal_fires()
    test_require_near_floor_suppresses_no_zone()
    test_slump_at_desk_is_not_a_fall()
    test_fall_across_an_id_switch_fires()
    test_neighbour_standing_beside_does_not_lend_height()
    test_found_lying_alone_does_not_fire()
    test_rolling_off_a_bed_fires()
    test_hip_drop_off_restores_any_horizontal()
    test_departed_tracks_are_forgotten()
    print("ALL FALL-FSM TESTS PASSED")
