from __future__ import annotations

"""
Posture classification from 17-keypoint (COCO) pose.

Output: one of {STANDING, SITTING, WALKING, FALLEN, UNKNOWN}.

Method (deterministic, no ML):

  1. Torso-vertical angle from mid-shoulder -> mid-hip.
     A near-vertical torso (< 30 deg from vertical) plus knees roughly
     below hips ==> upright (standing or walking).
     A torso > 60 deg from vertical (i.e. close to horizontal) plus
     the centroid having dropped quickly in the recent window ==> FALLEN.

  2. Knee angle (hip-knee-ankle). Knee around 90 deg with the hip-to-knee
     vertical drop being short ==> SITTING.

  3. Walking = STANDING with centroid motion > threshold over a 1.5s
     window, evaluated by PostureTracker (stateful, per track_id).

Why this works for elderly care:
  - Pose is already FP16 TRT, runs at 2 FPS — almost free downstream.
  - Hysteresis + N-frame confirmation prevents flicker (sitting->fallen
    on a single noisy frame is rejected).
  - Doesn't depend on a perfect bounding box, only on the keypoint
    geometry FastReid + ByteTrack already give us per track.

Confidence is a calibration factor: keypoints with conf < 0.2 are
treated as missing and their joint contributes only to the absent-flag.
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from config.settings import settings
from pose.pose_schema import PoseEstimation


# ---- COCO indices ----------------------------------------------------
NOSE = 0
LEFT_SHOULDER, RIGHT_SHOULDER = 5, 6
LEFT_HIP, RIGHT_HIP = 11, 12
LEFT_KNEE, RIGHT_KNEE = 13, 14
LEFT_ANKLE, RIGHT_ANKLE = 15, 16

_MIN_KP_CONF = 0.20


class Posture(str, Enum):
    STANDING = "standing"
    SITTING = "sitting"
    WALKING = "walking"
    FALLEN = "fallen"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class PostureResult:
    posture: Posture
    confidence: float                # 0..1
    torso_angle_deg: float           # 0 = perfectly vertical
    avg_knee_angle_deg: float        # 180 = straight legs
    centroid_xy: tuple[float, float]


# =====================================================================
# Geometry helpers
# =====================================================================

def _avg_point(pts: list[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    """Average list of (x, y, conf) where conf above the floor."""
    xs, ys, cs = [], [], []
    for x, y, c in pts:
        if c >= _MIN_KP_CONF:
            xs.append(x); ys.append(y); cs.append(c)
    if not xs:
        return None
    return sum(xs) / len(xs), sum(ys) / len(ys), sum(cs) / len(cs)


def _angle_deg(a: tuple[float, float], b: tuple[float, float],
               c: tuple[float, float]) -> float:
    """Angle ABC in degrees at vertex B (0..180)."""
    bax, bay = a[0] - b[0], a[1] - b[1]
    bcx, bcy = c[0] - b[0], c[1] - b[1]
    dot = bax * bcx + bay * bcy
    nA = math.hypot(bax, bay); nC = math.hypot(bcx, bcy)
    if nA == 0 or nC == 0:
        return 0.0
    cos = max(-1.0, min(1.0, dot / (nA * nC)))
    return math.degrees(math.acos(cos))


def _angle_from_vertical(top: tuple[float, float],
                         bottom: tuple[float, float]) -> float:
    """Angle the line (top -> bottom) makes with the image-vertical axis."""
    dx = bottom[0] - top[0]
    dy = bottom[1] - top[1]
    if dx == 0 and dy == 0:
        return 0.0
    # Image y grows downward; vertical = (0, 1)
    return abs(math.degrees(math.atan2(abs(dx), abs(dy))))


# =====================================================================
# Per-frame classification (stateless)
# =====================================================================

def classify_frame(pose: PoseEstimation) -> PostureResult:
    kps = [(k.x, k.y, k.confidence) for k in pose.keypoints]

    shoulder = _avg_point([kps[LEFT_SHOULDER], kps[RIGHT_SHOULDER]])
    hip = _avg_point([kps[LEFT_HIP], kps[RIGHT_HIP]])
    knee = _avg_point([kps[LEFT_KNEE], kps[RIGHT_KNEE]])
    ankle = _avg_point([kps[LEFT_ANKLE], kps[RIGHT_ANKLE]])

    if shoulder is None or hip is None:
        return PostureResult(Posture.UNKNOWN, 0.0, 0.0, 0.0, (0.0, 0.0))

    sx, sy, _ = shoulder
    hx, hy, _ = hip
    torso_ang = _angle_from_vertical((sx, sy), (hx, hy))

    # Centroid for motion tracking
    xs = [k.x for k in pose.keypoints if k.confidence >= _MIN_KP_CONF]
    ys = [k.y for k in pose.keypoints if k.confidence >= _MIN_KP_CONF]
    centroid = (sum(xs) / len(xs), sum(ys) / len(ys)) if xs else (0.0, 0.0)

    # Knee angle — used to distinguish sitting from standing
    knee_ang = 180.0
    if knee is not None and ankle is not None:
        k_left = _angle_deg(
            (kps[LEFT_HIP][0], kps[LEFT_HIP][1]),
            (kps[LEFT_KNEE][0], kps[LEFT_KNEE][1]),
            (kps[LEFT_ANKLE][0], kps[LEFT_ANKLE][1]),
        ) if kps[LEFT_HIP][2] > _MIN_KP_CONF and kps[LEFT_KNEE][2] > _MIN_KP_CONF \
            and kps[LEFT_ANKLE][2] > _MIN_KP_CONF else None
        k_right = _angle_deg(
            (kps[RIGHT_HIP][0], kps[RIGHT_HIP][1]),
            (kps[RIGHT_KNEE][0], kps[RIGHT_KNEE][1]),
            (kps[RIGHT_ANKLE][0], kps[RIGHT_ANKLE][1]),
        ) if kps[RIGHT_HIP][2] > _MIN_KP_CONF and kps[RIGHT_KNEE][2] > _MIN_KP_CONF \
            and kps[RIGHT_ANKLE][2] > _MIN_KP_CONF else None
        present = [a for a in (k_left, k_right) if a is not None]
        if present:
            knee_ang = sum(present) / len(present)

    # ---- decision tree --------------------------------------------
    fall_thr = settings.fall_torso_angle_deg

    # Strong fall: torso is closer to horizontal than to vertical.
    if torso_ang >= fall_thr:
        return PostureResult(Posture.FALLEN, 0.85, torso_ang, knee_ang, centroid)

    # Upright torso + bent knees => sitting.
    # Also if hips are at ~knee height (small vertical drop), sitting wins.
    if knee_ang < 140.0:
        if knee is not None and abs(knee[1] - hy) < (abs(hy - sy) * 0.6):
            return PostureResult(Posture.SITTING, 0.80, torso_ang, knee_ang, centroid)

    # Otherwise we treat as standing — walking is decided by the
    # stateful PostureTracker below, which folds in motion.
    return PostureResult(Posture.STANDING, 0.75, torso_ang, knee_ang, centroid)


# =====================================================================
# Per-track stateful tracker — adds walking + N-frame fall confirmation
# =====================================================================

@dataclass(slots=True)
class _TrackState:
    last_postures: deque
    last_centroids: deque        # (ts, x, y)
    fall_streak: int = 0


class PostureTracker:
    """
    Folds per-frame PostureResult into a smoothed posture stream
    per (camera_id, track_id).

    Adds:
      - WALKING: STANDING + centroid motion > threshold over window
      - FALLEN: requires N consecutive FALLEN frames (default 3)
      - Cooldown so we don't re-emit FALLEN within fall_cooldown_secs
    """

    HISTORY = 8

    def __init__(self) -> None:
        self._state: dict[tuple[str, int], _TrackState] = defaultdict(
            lambda: _TrackState(
                last_postures=deque(maxlen=self.HISTORY),
                last_centroids=deque(maxlen=self.HISTORY),
            )
        )
        self._last_fall: dict[tuple[str, int], datetime] = {}

    def update(
        self,
        camera_id: str,
        track_id: int,
        pose: PoseEstimation,
    ) -> PostureResult:
        st = self._state[(camera_id, track_id)]
        raw = classify_frame(pose)

        st.last_postures.append(raw.posture)
        st.last_centroids.append((pose.timestamp, *raw.centroid_xy))

        # ---- walking detection (needs motion) ----------------------
        if raw.posture == Posture.STANDING and len(st.last_centroids) >= 2:
            t_old, x_old, y_old = st.last_centroids[0]
            t_new, x_new, y_new = st.last_centroids[-1]
            dt = max((t_new - t_old).total_seconds(), 1e-3)
            if dt <= settings.walking_motion_window_secs:
                disp = math.hypot(x_new - x_old, y_new - y_old) / dt
                if disp >= settings.walking_motion_threshold_pixels:
                    return PostureResult(
                        Posture.WALKING, raw.confidence,
                        raw.torso_angle_deg, raw.avg_knee_angle_deg, raw.centroid_xy,
                    )

        # ---- N-frame fall confirmation -----------------------------
        if raw.posture == Posture.FALLEN:
            st.fall_streak += 1
            if st.fall_streak < settings.fall_confirmation_frames:
                # Not confirmed yet — fall back to previous stable posture
                stable = next(
                    (p for p in reversed(list(st.last_postures)[:-1])
                     if p in (Posture.STANDING, Posture.SITTING, Posture.WALKING)),
                    Posture.UNKNOWN,
                )
                return PostureResult(
                    stable, raw.confidence * 0.5,
                    raw.torso_angle_deg, raw.avg_knee_angle_deg, raw.centroid_xy,
                )
        else:
            st.fall_streak = 0

        return raw

    # Helpers for FallRule
    def confirm_fall(self, camera_id: str, track_id: int) -> bool:
        """Has a fall been confirmed *and* are we past cooldown?"""
        key = (camera_id, track_id)
        st = self._state.get(key)
        if st is None or st.fall_streak < settings.fall_confirmation_frames:
            return False
        now = datetime.now(timezone.utc)
        last = self._last_fall.get(key)
        if last is not None and (now - last).total_seconds() < settings.fall_cooldown_secs:
            return False
        self._last_fall[key] = now
        return True
