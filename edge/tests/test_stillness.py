"""
Local test for the StillnessRule (no TRT needed) — pose-keypoint no-motion vs
posture-label no-transition, plus the stale-camera freshness guard.

Run:  PYTHONPATH=edge python edge/tests/test_stillness.py

Uses the REAL buffer classes and shrinks the stillness window to sub-second so
the whole suite runs in a few seconds of wall clock (the rule reads
datetime.now() internally, so time is real, just compressed).
"""
import time
from datetime import datetime, timezone
from itertools import count

from config.settings import settings

# Compress the 75-min slot to sub-second scale BEFORE any ticking.
settings.stillness_window_secs = 0.8
settings.stillness_burst_interval_secs = 0.1
settings.stillness_burst_count = 3

from detection.detection_schema import BoundingBox            # noqa: E402
from pose.pose_buffer import PoseBuffer                       # noqa: E402
from pose.pose_schema import Keypoint, PoseEstimation, PoseResult  # noqa: E402
from pose.posture_buffer import PostureBuffer, PostureRecord  # noqa: E402
from pose.posture_classifier import Posture, PostureTracker   # noqa: E402
from reid.identity_buffer import IdentityBuffer               # noqa: E402
from reid.identity_schema import Identity                     # noqa: E402
from rules.rule_context import RuleContext                    # noqa: E402
from rules.stillness_rule import StillnessRule                # noqa: E402
from tracking.track_buffer import TrackBuffer                 # noqa: E402
from tracking.track_schema import Track, TrackResult          # noqa: E402


_FID = count(1)

# Seated skeleton: torso length 150px (shoulders y=150 -> hips y=300).
# LEFT_WRIST = 9 — the "stitching hand" the tests wiggle.
BASE = {0: (200, 100), 5: (190, 150), 6: (210, 150), 7: (185, 220), 8: (215, 220),
        9: (180, 260), 10: (220, 260), 11: (195, 300), 12: (205, 300),
        13: (190, 380), 14: (210, 380), 15: (190, 460), 16: (210, 460)}


def _kp17(coords: dict) -> list[Keypoint]:
    return [Keypoint(x=float(coords[i][0]), y=float(coords[i][1]), confidence=0.9)
            if i in coords else Keypoint(x=0.0, y=0.0, confidence=0.0)
            for i in range(17)]


def _world():
    """Fresh buffers + context; frames/detections unused by StillnessRule."""
    tracks, idents = TrackBuffer(), IdentityBuffer()
    poses, postures = PoseBuffer(), PostureBuffer()
    ctx = RuleContext(frames=None, detections=None, tracks=tracks, poses=poses,
                      postures=postures, posture_tracker=PostureTracker(),
                      identities=idents)
    return ctx, tracks, idents, poses, postures


def _tick(bufs, cam: str, tid: int, coords: dict,
          posture: Posture = Posture.SITTING, conf: float = 0.9) -> None:
    """One fresh sighting of the target on `cam` (all buffers stamped now)."""
    _, tracks, idents, poses, postures = bufs
    now = datetime.now(timezone.utc)
    fid = next(_FID)
    tracks.update(TrackResult(
        camera_id=cam, frame_id=fid, timestamp=now,
        tracks=[Track(track_id=tid, camera_id=cam, frame_id=fid, timestamp=now,
                      bbox=BoundingBox(x1=150, y1=80, x2=260, y2=480),
                      confidence=0.9)]))
    idents.update(Identity(track_id=tid, camera_id=cam, frame_id=fid,
                           timestamp=now, recipient_id="ceravis-8",
                           is_target=True, confidence=conf))
    postures.update(PostureRecord(camera_id=cam, track_id=tid, posture=posture,
                                  confidence=0.9, timestamp=now,
                                  torso_angle_deg=30.0, knee_angle_deg=120.0))
    poses.update(PoseResult(camera_id=cam, frame_id=fid, timestamp=now,
                            poses=[PoseEstimation(track_id=None, camera_id=cam,
                                                  frame_id=fid, timestamp=now,
                                                  keypoints=_kp17(coords))]))


def _run(rule, bufs, secs: float, frame, dt: float = 0.05) -> list:
    """Tick the world + rule for `secs`; frame(k) -> (coords, posture)."""
    events = []
    for k in range(int(secs / dt)):
        coords, posture = frame(k)
        _tick(bufs, "camA", 1, coords, posture)
        events += rule.evaluate(bufs[0])
        time.sleep(dt)
    return events


def _wiggled(k: int) -> dict:
    """BASE with the left wrist swung 40px (0.27 torso lengths) every tick."""
    c = dict(BASE)
    c[9] = (180 + (40 if k % 2 else 0), 260)
    return c


def test_frozen_fires_no_motion():
    rule = StillnessRule()
    bufs = _world()
    events = _run(rule, bufs, 1.5, lambda k: (BASE, Posture.SITTING))
    kinds = [e.event_type for e in events]
    print(f"[frozen] events={kinds}")
    assert "no_motion" in kinds, "a frozen skeleton must raise the CRITICAL no_motion"
    assert kinds[0] == "no_motion", "the slot must OPEN with the alert"
    assert "no_transition_snapshot" not in kinds, \
        "no_motion must suppress the routine no_transition"
    print("[frozen] PASS")


def test_stitching_fires_no_transition_only():
    # Active hands, unchanged posture: the stitching-elderly case. The moving
    # wrist must keep resetting the no-motion clock; the held SITTING label
    # must reach the no-transition burst instead.
    rule = StillnessRule()
    bufs = _world()
    events = _run(rule, bufs, 1.5, lambda k: (_wiggled(k), Posture.SITTING))
    kinds = [e.event_type for e in events]
    print(f"[stitching] events={kinds}")
    assert "no_motion" not in kinds and "no_motion_snapshot" not in kinds, \
        "a moving hand must keep no_motion from ever firing"
    assert "no_transition_snapshot" in kinds, \
        "an hour of unchanged posture while active must raise no_transition"
    print("[stitching] PASS")


def test_posture_changes_fire_nothing():
    rule = StillnessRule()
    bufs = _world()
    flip = lambda k: (_wiggled(k),
                      Posture.SITTING if (k // 6) % 2 else Posture.STANDING)
    events = _run(rule, bufs, 1.5, flip)
    print(f"[posture flip] events={[e.event_type for e in events]}")
    assert not events, "moving AND changing posture must stay silent"
    print("[posture flip] PASS")


def test_stale_camera_never_false_alarms():
    # The recipient leaves camera A (its buffers freeze — active-camera-only
    # never rewrites them) and stays ACTIVE on camera B. The rule must follow
    # the fresh camera; acting on A's frozen snapshot would accumulate into a
    # false CRITICAL no_motion.
    rule = StillnessRule()
    rule.FRESH_SECS = 0.3                       # stale horizon on test scale
    bufs = _world()
    ctx = bufs[0]
    events = []
    for k in range(8):                          # 0.4s frozen on camera A
        _tick(bufs, "camA", 1, BASE)
        events += rule.evaluate(ctx)
        time.sleep(0.05)
    for k in range(24):                         # 1.2s ACTIVE on camera B only
        _tick(bufs, "camB", 7, _wiggled(k), conf=0.95)
        events += rule.evaluate(ctx)
        time.sleep(0.05)
    kinds = [e.event_type for e in events]
    print(f"[stale camera] events={kinds}")
    assert "no_motion" not in kinds and "no_motion_snapshot" not in kinds, \
        "a stale camera snapshot must never raise no_motion for an active person"
    print("[stale camera] PASS")


if __name__ == "__main__":
    test_frozen_fires_no_motion()
    test_stitching_fires_no_transition_only()
    test_posture_changes_fire_nothing()
    test_stale_camera_never_false_alarms()
    print("ALL STILLNESS TESTS PASSED")
