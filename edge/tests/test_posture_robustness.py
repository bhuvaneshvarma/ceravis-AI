"""
Posture robustness: the three failure cases, all body-relative (no camera
calibration). Each is tested in BOTH directions so a gate that suppresses
everything is not mistaken for one that works.

  CASE 2  walking OUT of frame — the lower body is clipped by the edge. The
          legs vanishing must NOT be read as a sit; the posture holds.
  CASE 3  sitting behind a TABLE — the legs are hidden but the body is well
          inside the frame. The knee cue is gone, so a clear head drop (with
          the body staying put) is the sit. This was stuck at STANDING forever.
  RECEDE  a person walking AWAY drops their head in the image AND shrinks. That
          is not a sit — the shrink (measured on the posture-invariant shoulder
          width) must veto the head-only call.

Run:  PYTHONPATH=edge python edge/tests/test_posture_robustness.py
"""
import sys
from datetime import timedelta

from common import clock
from config.settings import settings
from pose.pose_schema import Keypoint, PoseEstimation
from pose.posture_classifier import Posture, PostureTracker


FRAME_H = 720
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def _kp(coords: dict) -> list:
    out = []
    for i in range(17):
        if i in coords:
            x, y = coords[i]
            out.append(Keypoint(x=float(x), y=float(y), confidence=0.9))
        else:
            out.append(Keypoint(x=0.0, y=0.0, confidence=0.0))
    return out


def body(head_y, sh_y, hip_y, knee_y=None, ank_y=None, cx=200, sh_w=44):
    """A skeleton. Legs are included only when knee_y/ank_y are given (else they
    read as hidden). sh_w is the shoulder width — the recede/approach scale."""
    d = {0: (cx, head_y), 1: (cx - 9, head_y - 2), 2: (cx + 9, head_y - 2),
         3: (cx - 15, head_y), 4: (cx + 15, head_y),
         5: (cx - sh_w // 2, sh_y), 6: (cx + sh_w // 2, sh_y),
         11: (cx - 16, hip_y), 12: (cx + 16, hip_y)}
    if knee_y is not None:
        d[13] = (cx - 20, knee_y); d[14] = (cx + 20, knee_y)
    if ank_y is not None:
        d[15] = (cx - 16, ank_y); d[16] = (cx + 16, ank_y)
    return _kp(d)


def feed(tr, tid, frames, t0, dt=0.1):
    """frames: list of keypoint lists. Returns (last posture, end time)."""
    t = t0
    last = None
    for kps in frames:
        pose = PoseEstimation(track_id=tid, camera_id="c", frame_id=0,
                              timestamp=t, keypoints=kps)
        last = tr.update("c", tid, pose, frame_h=FRAME_H).posture
        t = t + timedelta(seconds=dt)
    return last, t


# A clean, fully-visible standing person (feet well inside a 720-tall frame).
STAND = body(head_y=100, sh_y=165, hip_y=330, knee_y=450, ank_y=560)


print("\n1. legs CUT OFF at the frame edge (walking out) is never a sit")
tr = PostureTracker()
t0 = clock.now()
_, t1 = feed(tr, 1, [STAND] * 5, t0)                 # establish STANDING
# now the person walks out the bottom: knees/ankles gone, the lowest visible
# joint (hips) sits right on the frame's bottom edge, and the head slides DOWN
# the image the way perspective makes it — the classic false-sit trigger.
walkout = [body(head_y=h, sh_y=h + 70, hip_y=700)     # hips at 700 of 720 -> truncated
           for h in (300, 350, 400, 440, 470, 470)]
last, _ = feed(tr, 1, walkout, t1)
check("posture holds (not flipped to sitting) as the legs clip off",
      last in (Posture.STANDING, Posture.WALKING), str(last))


print("\n2. sitting behind a TABLE (legs hidden, body inside frame) IS a sit")
tr2 = PostureTracker()
t0 = clock.now()
_, t1 = feed(tr2, 2, [STAND] * 5, t0)                # establish STANDING
# sit down: head + shoulders drop, hips settle mid-frame (well above the edge),
# legs hidden by the desk. Shoulder width unchanged -> not receding.
tablesit = [body(head_y=h, sh_y=h + 60, hip_y=360, sh_w=44)
            for h in (170, 210, 245, 265, 265, 265, 265)]
last, _ = feed(tr2, 2, tablesit, t1)
check("the head-drop with the legs hidden is read as SITTING",
      last == Posture.SITTING, str(last))


print("\n3. a person walking AWAY (head drops but body SHRINKS) is NOT a sit")
tr3 = PostureTracker()
t0 = clock.now()
_, t1 = feed(tr3, 3, [STAND] * 5, t0)                # establish STANDING
# same head drop as the table-sit, but the whole body shrinks (shoulder width
# collapses) — the signature of receding, which must veto the sit.
recede = [body(head_y=h, sh_y=h + 40, hip_y=360, sh_w=w)
          for h, w in [(170, 40), (210, 33), (245, 27), (265, 22),
                       (265, 18), (265, 16), (265, 15)]]
last, _ = feed(tr3, 3, recede, t1)
check("the shrink vetoes the head-only sit -> posture holds",
      last in (Posture.STANDING, Posture.WALKING), str(last))


print("\n4. standing UP behind a table (head rises, body steady) IS a stand")
tr4 = PostureTracker()
t0 = clock.now()
# establish SITTING with visible bent knees first…
sit_full = body(head_y=250, sh_y=310, hip_y=380, knee_y=380, ank_y=430)
_, t1 = feed(tr4, 4, [sit_full] * 5, t0)
check("seeded as sitting", tr4._state[("c", 4)].stable == Posture.SITTING,
      str(tr4._state[("c", 4)].stable))
# stand up: legs hidden by the desk again, head rises, shoulder width steady.
standup = [body(head_y=h, sh_y=h + 60, hip_y=360, sh_w=44)
           for h in (230, 190, 150, 120, 120, 120, 120)]
last, _ = feed(tr4, 4, standup, t1)
# The confirmed posture must leave SITTING; the live label may be WALKING while
# the stand-up motion is still in progress, which is itself an upright state.
check("the head-rise with legs hidden is read as standing up (not stuck sitting)",
      tr4._state[("c", 4)].stable == Posture.STANDING
      and last in (Posture.STANDING, Posture.WALKING), str(last))


print("\n5. a single noisy first frame does not stamp a posture (commit needs 2)")
tr5 = PostureTracker()
t0 = clock.now()
one = PoseEstimation(track_id=5, camera_id="c", frame_id=0, timestamp=t0,
                     keypoints=STAND)
tr5.update("c", 5, one, frame_h=FRAME_H)
check("one frame is not yet a committed stable posture",
      tr5._state[("c", 5)].stable == Posture.UNKNOWN,
      f"commit_frames={settings.posture_commit_frames}")
tr5.update("c", 5, PoseEstimation(track_id=5, camera_id="c", frame_id=1,
           timestamp=t0 + timedelta(seconds=0.1), keypoints=STAND),
           frame_h=FRAME_H)
check("a second agreeing frame commits it",
      tr5._state[("c", 5)].stable == Posture.STANDING)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll posture-robustness checks passed.")
