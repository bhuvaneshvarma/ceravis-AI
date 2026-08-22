"""
Local test for the StillnessRule (no TRT needed) — fused pixel+pose no-motion vs
posture-label no-transition, plus the stale-camera freshness guard.

The harness renders a REAL frame consistent with the keypoints and adds sensor
NOISE to both. Both of those matter: the previous version fed pixel-identical
coordinates at confidence 0.9 with no frames at all, which is why it passed
while no_motion was unreachable in the field for every real camera.

Run:  PYTHONPATH=edge python edge/tests/test_stillness.py

Uses the REAL buffer classes and shrinks the stillness window to sub-second so
the whole suite runs in a few seconds of wall clock (the rule reads
datetime.now() internally, so time is real, just compressed).
"""
import time
from datetime import datetime, timezone
from itertools import count

import cv2
import numpy as np

from config.settings import settings

# Compress the 75-min slot to sub-second scale BEFORE any ticking.
settings.stillness_window_secs = 0.8
settings.stillness_burst_interval_secs = 0.1
settings.stillness_burst_count = 3
# The pixel channel self-calibrates against the scene's own noise; shrink its
# warm-up to test scale too, or every case would spend the whole run holding.
settings.pixel_noise_min_samples = 5
settings.pixel_noise_window = 40

from detection.detection_schema import BoundingBox            # noqa: E402
from ingestion.frame_buffer import FrameBuffer                 # noqa: E402
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


# Real pose confidence is NOT uniform: on a seated person the wrists and ankles
# sit far lower than the torso. Feeding 0.9 everywhere is what hid the bug.
_CONF = {0: .92, 5: .90, 6: .90, 7: .72, 8: .71, 9: .44, 10: .42,
         11: .88, 12: .87, 13: .55, 14: .54, 15: .28, 16: .27}
_RNG = np.random.default_rng(4)
_FRAME_W, _FRAME_H = 320, 520


def _kp17(coords: dict, jitter: bool = True) -> list[Keypoint]:
    """Keypoints with per-joint noise scaled by (1 - confidence) — the real
    behaviour of a pose net. A 0.90 shoulder barely moves; a 0.27 ankle wanders."""
    out = []
    for i in range(17):
        if i not in coords:
            out.append(Keypoint(x=0.0, y=0.0, confidence=0.0))
            continue
        x, y = coords[i]
        c = _CONF[i]
        if jitter:
            sd = 2 + 26 * (1 - c)
            x += _RNG.normal(0, sd)
            y += _RNG.normal(0, sd)
        out.append(Keypoint(x=float(x), y=float(y), confidence=c))
    return out


def _render(coords: dict) -> np.ndarray:
    """A frame consistent with those keypoints, plus sensor noise. Without a real
    frame the pixel channel cannot run, and no_motion is (correctly) HELD."""
    img = np.full((_FRAME_H, _FRAME_W), 40, np.float32)
    if 5 in coords and 11 in coords:
        cx = int((coords[5][0] + coords[6][0]) / 2)
        cy = int((coords[5][1] + coords[11][1]) / 2)
        cv2.ellipse(img, (cx, cy), (46, 82), 0, 0, 360, 150, -1)
    if 0 in coords:
        cv2.circle(img, (int(coords[0][0]), int(coords[0][1])), 28, 175, -1)
    for i in (9, 10, 13, 14, 15, 16):                 # limbs incl. the hands
        if i in coords:
            cv2.circle(img, (int(coords[i][0]), int(coords[i][1])), 13, 200, -1)
    img += _RNG.normal(0, 3.0, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _world(with_frames: bool = True):
    """Fresh buffers + context. `with_frames=False` simulates a device where the
    pixel channel cannot run."""
    tracks, idents = TrackBuffer(), IdentityBuffer()
    poses, postures = PoseBuffer(), PostureBuffer()
    frames = FrameBuffer() if with_frames else None
    ctx = RuleContext(frames=frames, detections=None, tracks=tracks, poses=poses,
                      postures=postures, posture_tracker=PostureTracker(),
                      identities=idents)
    return ctx, tracks, idents, poses, postures, frames


def _tick(bufs, cam: str, tid: int, coords: dict,
          posture: Posture = Posture.SITTING, conf: float = 0.9) -> None:
    """One fresh sighting of the target on `cam` (all buffers stamped now)."""
    _, tracks, idents, poses, postures, frames = bufs
    now = datetime.now(timezone.utc)
    fid = next(_FID)
    if frames is not None:
        frames.update(cam, _render(coords), fid, now, 15.0)
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
    # The no_motion SLOT must open with the alert, not a follow-up snapshot.
    # Leading no_transition events are legitimate: the pixel channel HOLDS the
    # motion clock while it calibrates against the scene, and on this test's
    # compressed window that warm-up is a large fraction of the run. In
    # production it is ~20 s against a 3600 s window.
    nm = [k for k in kinds if k.startswith("no_motion")]
    assert nm[0] == "no_motion", "the no_motion slot must OPEN with the alert"
    tail = kinds[kinds.index("no_motion"):]
    assert "no_transition_snapshot" not in tail, \
        "once no_motion is active it must suppress the routine no_transition"
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


def test_pose_jitter_alone_never_blocks_no_motion():
    """THE regression guard for the field bug.

    A genuinely motionless person still produces per-keypoint jitter. The old
    max-over-keypoints statistic called 72.9% of such ticks "motion" and reset
    the 60-min clock, so no_motion could never mature and only no_transition
    ever fired. Jitter is ON in this harness by default, so this failing again
    means the estimator has regressed."""
    rule = StillnessRule()
    bufs = _world()
    events = _run(rule, bufs, 1.5, lambda k: (BASE, Posture.SITTING))
    kinds = [e.event_type for e in events]
    assert "no_motion" in kinds, (
        "keypoint jitter on a motionless person must NOT reset the clock — "
        "this is the max-over-keypoints regression")
    tail = kinds[kinds.index("no_motion"):]
    assert "no_transition_snapshot" not in tail
    print(f"[jitter guard] no_motion survived realistic jitter  PASS")


def test_no_pixel_channel_holds_instead_of_false_alarming():
    """Without frames, pose alone CANNOT rule out hand movement (a seated wrist
    sits near 0.44 confidence). The detector must HOLD rather than raise a
    critical alert it cannot justify — fail toward silence, not toward alarm."""
    rule = StillnessRule()
    bufs = _world(with_frames=False)
    events = _run(rule, bufs, 1.5, lambda k: (BASE, Posture.SITTING))
    kinds = [e.event_type for e in events]
    assert "no_motion" not in kinds and "no_motion_snapshot" not in kinds,         "no_motion must never fire on the pose channel alone"
    v = rule.last_verdict
    assert v is not None and not v.pixel_ready, "expected the pixel channel down"
    print(f"[no pixels] held, reason={v.reason!r}  PASS")


if __name__ == "__main__":
    test_frozen_fires_no_motion()
    test_stitching_fires_no_transition_only()
    test_posture_changes_fire_nothing()
    test_stale_camera_never_false_alarms()
    test_pose_jitter_alone_never_blocks_no_motion()
    test_no_pixel_channel_holds_instead_of_false_alarming()
    print("ALL STILLNESS TESTS PASSED")
