"""
PoseRunner: whose skeleton is read, and how often.

  1. Target crop: the skeleton that FITS the target's box is read — not the
     most confident one, which may be a neighbour's inside the padded crop.
  2. A skeleton that fits a neighbour's box as well is ambiguous: not read.
  3. Once locked, the target's pose is sampled at pose_locked_fps.
  4. Full frame: one skeleton per person and one person per skeleton.

Run:  PYTHONPATH=edge python edge/tests/test_pose_runner.py
"""
import sys

import numpy as np

from common import clock
from common.crops import crop_person
from config.settings import settings
from detection.detection_schema import BoundingBox
from ingestion.frame_buffer import FrameBuffer
from pose.pose_buffer import PoseBuffer
from pose.pose_runner import PoseRunner
from pose.pose_schema import Keypoint, PoseEstimation, PoseResult
from pose.posture_buffer import PostureBuffer
from reid.target_registry import TargetRegistry
from tracking.track_buffer import TrackBuffer
from tracking.track_schema import Track, TrackResult

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def skeleton(x1, y1, x2, y2, conf=0.9):
    """17 keypoints spread over a box (frame or crop coordinates)."""
    xs = np.linspace(x1, x2, 17)
    ys = np.linspace(y1, y2, 17)
    return PoseEstimation(track_id=None, camera_id="c", frame_id=0, timestamp=clock.now(),
                          keypoints=[Keypoint(x=float(x), y=float(y), confidence=conf)
                                     for x, y in zip(xs, ys)])


class Estimator:
    """Returns the poses it is given, in the coordinates of the image passed."""
    def __init__(self):
        self.calls = 0
        self.poses = []

    def estimate(self, frame, camera_id, frame_id, timestamp):
        self.calls += 1
        return PoseResult(camera_id=camera_id, frame_id=frame_id,
                          timestamp=timestamp, poses=list(self.poses))


def world(boxes: dict, target=None):
    fb, tb, reg = FrameBuffer(), TrackBuffer(), TargetRegistry()
    runner = PoseRunner(fb, PoseBuffer(), tb, PostureBuffer(), target_registry=reg)
    runner._estimator = est = Estimator()
    read = []
    runner._classify = lambda cam, tid, pose, frame_h=0: read.append((tid, pose))
    tracks = [Track(track_id=t, camera_id="c", frame_id=1, timestamp=clock.now(),
                    bbox=BoundingBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]), confidence=0.9)
              for t, b in boxes.items()]
    tb.update(TrackResult(camera_id="c", frame_id=1, timestamp=clock.now(), tracks=tracks))
    if target is not None:
        reg.lock("c", target, "ravi")
    frame = np.zeros((1080, 1920, 3), np.uint8)
    state = {"fid": 0}

    def new_frame():
        state["fid"] += 1
        fb.update(camera_id="c", frame=frame, frame_id=state["fid"],
                  timestamp=clock.now(), fps=10.0)
    return runner, est, read, new_frame, frame


T = (400, 200, 560, 700)          # the target's box
N = (700, 200, 860, 700)          # a neighbour, apart
settings.pose_locked_fps = 1000.0  # sections 1-2: never rate-limited

print("\n1. the target's skeleton is the one that fits the target's box")
runner, est, read, new_frame, frame = world({1: T, 2: N}, target=1)
_, ox, oy = crop_person(frame, *T, settings.pose_crop_padding_frac)
est.poses = [skeleton(N[0] - ox, N[1] - oy, N[2] - ox, N[3] - oy, conf=0.99),  # neighbour, surer
             skeleton(T[0] - ox, T[1] - oy, T[2] - ox, T[3] - oy, conf=0.60)]  # target
new_frame(); runner._tick()
check("one skeleton read, filed under the target", len(read) == 1 and read[0][0] == 1)
kx = [k.x for k in read[0][1].keypoints] if read else [0]
check("...and it is the TARGET's (not the more confident neighbour's)",
      abs(min(kx) - T[0]) < 1 and abs(max(kx) - T[2]) < 1, f"x {min(kx):.0f}-{max(kx):.0f}")

print("\n2. a skeleton that fits a neighbour as well is not read")
M = (430, 200, 590, 700)          # a neighbour almost on top of the target
runner, est, read, new_frame, frame = world({1: T, 2: M}, target=1)
_, ox, oy = crop_person(frame, *T, settings.pose_crop_padding_frac)
est.poses = [skeleton(415 - ox, 200 - oy, 575 - ox, 700 - oy)]  # halfway between them
new_frame(); runner._tick()
check("ambiguous skeleton: posture held, not read", read == [] and est.calls == 1)
est.poses = [skeleton(T[0] - ox + 300, T[1] - oy, T[2] - ox + 300, T[3] - oy)]
new_frame(); runner._tick()
check("a skeleton that fits nobody on the target is not read either", read == [])

print("\n3. once locked, the target's pose is sampled at pose_locked_fps")
settings.pose_locked_fps = 0.5    # at most once per 2 s
runner, est, read, new_frame, frame = world({1: T}, target=1)
_, ox, oy = crop_person(frame, *T, settings.pose_crop_padding_frac)
est.poses = [skeleton(T[0] - ox, T[1] - oy, T[2] - ox, T[3] - oy)]
for _ in range(4):
    new_frame(); runner._tick()
check("4 fresh frames within the interval -> 1 pose inference", est.calls == 1, str(est.calls))
settings.pose_locked_fps = 1000.0

print("\n4. full frame: one skeleton per person, one person per skeleton")
A, B = (100, 100, 260, 600), (300, 100, 460, 600)
runner, est, read, new_frame, frame = world({1: A, 2: B})
est.poses = [skeleton(*A), skeleton(110, 100, 270, 600), skeleton(*B)]  # two fit A
new_frame(); runner._tick()
check("each person gets exactly one skeleton", sorted(t for t, _ in read) == [1, 2],
      str([t for t, _ in read]))

if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll pose-runner checks passed.")
