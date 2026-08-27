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
  - Pose is already FP16 TRT, runs at settings.pose_fps — almost free downstream.
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
from datetime import datetime
from enum import Enum

from config.settings import settings
from pose.pose_schema import PoseEstimation
from common import clock


# ---- COCO indices ----------------------------------------------------
NOSE = 0
LEFT_EYE, RIGHT_EYE = 1, 2
LEFT_EAR, RIGHT_EAR = 3, 4
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
    # Per-frame body scale in pixels (shoulder->hip torso length, or the
    # keypoint vertical extent as a fallback). Used to make walking motion
    # scale-invariant — a chair-roll near the camera and a stride across a
    # far room then compare on the same footing.
    body_ref_px: float = 1.0
    # Head reference point's image-y (nose/eyes/ears, fallback shoulder). The
    # tracker watches this rise/fall to corroborate sit<->stand transitions —
    # the first thing that moves when standing up is the head going up.
    head_y: float = 0.0
    head_x: float = 0.0              # head image-x — for the floor point-in-polygon test
    legs_visible: bool = True        # a full hip-knee-ankle leg was confidently seen
    truncated_bottom: bool = False   # the body is CUT OFF at the frame's bottom edge
    # A POSTURE-INVARIANT scale (shoulder width, fallback head width): unlike the
    # torso, it does not shorten when the person sits, so its shrinking/growing
    # cleanly means the person is walking away from / toward the camera. This is
    # what separates a real sit-down from a recede when the legs are hidden.
    span_px: float = 1.0


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

def classify_frame(pose: PoseEstimation, frame_h: int = 0) -> PostureResult:
    kps = [(k.x, k.y, k.confidence) for k in pose.keypoints]

    shoulder = _avg_point([kps[LEFT_SHOULDER], kps[RIGHT_SHOULDER]])
    hip = _avg_point([kps[LEFT_HIP], kps[RIGHT_HIP]])
    knee = _avg_point([kps[LEFT_KNEE], kps[RIGHT_KNEE]])
    ankle = _avg_point([kps[LEFT_ANKLE], kps[RIGHT_ANKLE]])

    if shoulder is None or hip is None:
        return PostureResult(Posture.UNKNOWN, 0.0, 0.0, 0.0, (0.0, 0.0), 1.0, 0.0)

    sx, sy, _ = shoulder
    hx, hy, _ = hip
    torso_ang = _angle_from_vertical((sx, sy), (hx, hy))

    # Head reference (top of the body): nose/eyes/ears, fallback to shoulders.
    head = _avg_point([kps[NOSE], kps[LEFT_EYE], kps[RIGHT_EYE],
                       kps[LEFT_EAR], kps[RIGHT_EAR]])
    head_y = head[1] if head is not None else sy
    head_x = head[0] if head is not None else sx

    # Centroid for motion tracking
    xs = [k.x for k in pose.keypoints if k.confidence >= _MIN_KP_CONF]
    ys = [k.y for k in pose.keypoints if k.confidence >= _MIN_KP_CONF]
    centroid = (sum(xs) / len(xs), sum(ys) / len(ys)) if xs else (0.0, 0.0)

    # Body scale: torso length, falling back to the keypoint vertical extent
    # if shoulders/hips are nearly coincident (heavy foreshortening).
    body_ref = math.hypot(sx - hx, sy - hy)
    if body_ref < 1.0:
        body_ref = (max(ys) - min(ys)) if ys else 1.0
    body_ref = max(body_ref, 1.0)

    # Posture-invariant scale for the recede/approach guard (see PostureResult):
    # shoulder width, then head width, and only then the torso as a last resort.
    def _span(a, b) -> float:
        return (math.hypot(a[0] - b[0], a[1] - b[1])
                if a[2] >= _MIN_KP_CONF and b[2] >= _MIN_KP_CONF else 0.0)
    span = _span(kps[LEFT_SHOULDER], kps[RIGHT_SHOULDER])
    if span < 1.0:
        span = _span(kps[LEFT_EAR], kps[RIGHT_EAR])
    if span < 1.0:
        span = _span(kps[LEFT_EYE], kps[RIGHT_EYE])
    if span < 1.0:
        span = body_ref
    span = max(span, 1.0)

    # Knee angle (hip-knee-ankle) — a JOINT angle, so it is view-invariant: it
    # reads the same whether the camera is level or ceiling-mounted at a tilt.
    # This is the primary sit/stand cue. present[] also tells us whether the
    # legs are actually visible.
    knee_ang = 180.0
    present: list[float] = []
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

    # ---- decision tree (view-invariant) ---------------------------
    fall_thr = settings.fall_torso_angle_deg

    # Strong fall: torso is closer to horizontal than to vertical.
    if torso_ang >= fall_thr:
        return PostureResult(Posture.FALLEN, 0.85, torso_ang, knee_ang,
                             centroid, body_ref, head_y, head_x,
                             legs_visible=bool(present), span_px=span)

    # Decide sit vs stand ONLY when the legs are actually visible (knee joint
    # angle available). Bent knees => sitting, straight => standing.
    if present:
        if knee_ang < 140.0:
            return PostureResult(Posture.SITTING, 0.80, torso_ang, knee_ang,
                                 centroid, body_ref, head_y, head_x,
                                 legs_visible=True, span_px=span)
        return PostureResult(Posture.STANDING, 0.75, torso_ang, knee_ang,
                             centroid, body_ref, head_y, head_x,
                             legs_visible=True, span_px=span)

    # Legs NOT visible: two very different reasons, and the tracker must tell
    # them apart (see PostureTracker.update):
    #   CUT OFF at the frame's bottom edge (walking out) -> HOLD; never a sit.
    #   HIDDEN by furniture, body well inside frame       -> a head drop is a sit.
    # The bottom of the visible body reaching the frame edge is what separates
    # them; with no frame height known, truncation is simply unclaimed (False)
    # and the tracker falls back to holding.
    lowest_y = max(ys) if ys else 0.0
    truncated = (frame_h > 0
                 and lowest_y >= frame_h * (1.0 - settings.posture_truncation_margin_frac))
    return PostureResult(Posture.UNKNOWN, 0.40, torso_ang, knee_ang,
                         centroid, body_ref, head_y, head_x,
                         legs_visible=False, truncated_bottom=truncated,
                         span_px=span)


# =====================================================================
# Per-track stateful tracker — adds walking + N-frame fall confirmation
# =====================================================================

@dataclass(slots=True)
class _TrackState:
    last_postures: deque
    last_centroids: deque        # (ts, x, y, body_ref_px)
    last_heads: deque            # (ts, head_y, body_ref_px)
    fall_streak: int = 0
    walk_streak: int = 0
    stable: Posture = Posture.UNKNOWN   # last CONFIRMED sit/stand/fall posture
    sit_stand_streak: int = 0           # frames of corroborated sit<->stand transition
    commit_base: Posture = Posture.UNKNOWN   # candidate for the FIRST sit/stand commit
    commit_streak: int = 0              # agreeing frames toward that first commit
    occluded_streak: int = 0            # head-only sit/stand (legs hidden) toward a switch
    # ---- the ONE fall machine (label depth = alert depth, no wait) ----
    fall_down: bool = False              # DOWN: streak-confirmed horizontal (label)
    fall_alerted: bool = False           # latch: one alert per fall episode
    fall_confirmed_pending: bool = False # a fall to emit (drained by confirm_fall)


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
                last_heads=deque(maxlen=self.HISTORY),
            )
        )
        self._last_fall: dict[tuple[str, int], datetime] = {}

    def update(
        self,
        camera_id: str,
        track_id: int,
        pose: PoseEstimation,
        floor_query=None,          # callable(head_x, head_y) -> bool|None, or None
        frame_h: int = 0,          # AI frame height — lets the classifier see truncation
    ) -> PostureResult:
        st = self._state[(camera_id, track_id)]
        raw = classify_frame(pose, frame_h)

        st.last_postures.append(raw.posture)
        st.last_centroids.append((pose.timestamp, *raw.centroid_xy, raw.body_ref_px))
        # Only remember a REAL head reading — a degenerate frame (no torso/head)
        # reports head_y=0, which would look like the head leaping to the top of
        # the image and poison the head-shift transition cues.
        if raw.head_y > 0.0:
            st.last_heads.append((pose.timestamp, raw.head_y,
                                  raw.body_ref_px, raw.span_px))

        # The ONE fall machine: the DOWN state is the FALLEN label the UI and
        # rules read AND the alert trigger — the moment a fall is detected it is
        # raised (confirm_fall), with no post-fall wait. Same signal, one depth.
        self._update_fall_fsm(st, raw, pose.timestamp, floor_query)

        def out(posture: Posture) -> PostureResult:
            return PostureResult(posture, raw.confidence, raw.torso_angle_deg,
                                 raw.avg_knee_angle_deg, raw.centroid_xy,
                                 raw.body_ref_px, raw.head_y, raw.head_x)

        # ---- fall label from the FSM (highest priority) -------------
        if st.fall_down:
            st.stable = Posture.FALLEN
            return out(Posture.FALLEN)

        # ---- sit/stand with transition evidence --------------------
        # A confident SITTING/STANDING only OVERRIDES the confirmed state when
        # corroborated: the head must rise (to stand) or fall (to sit) by a
        # fraction of body length, sustained for N frames. UNKNOWN frames (e.g.
        # legs left the frame) HOLD the confirmed state — so a small shift while
        # seated can no longer flip the label to standing.
        base = raw.posture if raw.posture in (Posture.STANDING, Posture.SITTING) else None
        if base is not None:
            if st.stable in (Posture.UNKNOWN, Posture.FALLEN):
                # First commit needs a few AGREEING frames, so one foreshortened
                # (ceiling-mounted) knee read can't stamp a wrong starting posture
                # that then has to be corroborated back off.
                st.commit_streak = (st.commit_streak + 1
                                    if base == st.commit_base else 1)
                st.commit_base = base
                if st.commit_streak >= settings.posture_commit_frames:
                    st.stable = base
                    st.commit_streak = 0
                st.sit_stand_streak = 0
            elif base == st.stable:
                st.sit_stand_streak = 0
            elif self._head_supports(st, st.stable, base) and not self._receding(st):
                st.sit_stand_streak += 1
                if st.sit_stand_streak >= settings.posture_transition_confirm_frames:
                    st.stable = base
                    st.sit_stand_streak = 0
            else:
                st.sit_stand_streak = 0
            st.occluded_streak = 0
        elif (raw.legs_visible is False and not raw.truncated_bottom
                and st.stable in (Posture.STANDING, Posture.SITTING)):
            # Legs HIDDEN by furniture (a table/desk), body well inside the frame:
            # the knee cue is gone, so read sit<->stand from the HEAD's vertical
            # shift alone — the case that was stuck reporting STANDING forever.
            want = self._occluded_transition(st, raw)
            if want is not None and want != st.stable:
                st.occluded_streak += 1
                if st.occluded_streak >= settings.posture_transition_confirm_frames:
                    st.stable = want
                    st.occluded_streak = 0
            else:
                st.occluded_streak = 0
        else:
            # UNKNOWN because the legs are CUT OFF at the frame edge (walking out)
            # or no anchor yet: HOLD the confirmed posture, never guess a sit.
            st.sit_stand_streak = 0
            st.occluded_streak = 0                # no head motion -> don't switch

        stable = st.stable if st.stable != Posture.UNKNOWN else raw.posture

        # ---- walking: confirmed standing + sustained, scale-normalized motion
        if stable == Posture.STANDING:
            st.walk_streak = st.walk_streak + 1 if self._is_moving(st, raw) else 0
            if st.walk_streak >= settings.walking_confirm_frames:
                return out(Posture.WALKING)
        else:
            st.walk_streak = 0

        return out(stable)

    # ---- transition helpers -----------------------------------------
    def _is_moving(self, st: _TrackState, raw: PostureResult) -> bool:
        """Scale-normalized centroid speed over the window, with a pixel floor."""
        if len(st.last_centroids) < 2:
            return False
        t_old, x_old, y_old, _ = st.last_centroids[0]
        t_new, x_new, y_new, _ = st.last_centroids[-1]
        dt = max((t_new - t_old).total_seconds(), 1e-3)
        if dt > settings.walking_motion_window_secs:
            return False
        disp = math.hypot(x_new - x_old, y_new - y_old)
        speed_frac = (disp / dt) / max(raw.body_ref_px, 1.0)     # body-lengths / sec
        return (speed_frac >= settings.walking_motion_body_fraction
                and disp >= settings.walking_min_pixels)

    def _head_supports(self, st: _TrackState,
                       from_posture: Posture, to_posture: Posture) -> bool:
        """True if head vertical motion corroborates the sit<->stand change:
        head rising (image-y decreasing) supports sit->stand; falling supports
        stand->sit. Normalized by body length so it is distance-independent."""
        if len(st.last_heads) < 2:
            return False
        t_old, y_old, _, _ = st.last_heads[0]
        t_new, y_new, ref_new, _ = st.last_heads[-1]
        dt = (t_new - t_old).total_seconds()
        if dt <= 0 or dt > settings.walking_motion_window_secs:
            return False
        dy_frac = (y_new - y_old) / max(ref_new, 1.0)    # +ve = head moved DOWN
        thr = settings.posture_transition_head_frac
        if from_posture == Posture.SITTING and to_posture == Posture.STANDING:
            return dy_frac <= -thr                        # head moved UP
        if from_posture == Posture.STANDING and to_posture == Posture.SITTING:
            return dy_frac >= thr                         # head moved DOWN
        return False

    def _scale_ratio(self, st: _TrackState) -> float:
        """Body-scale new/old over the head window: >1 approaching the camera,
        <1 receding. 1.0 when there isn't enough history to tell."""
        if len(st.last_heads) < 2:
            return 1.0
        span_old = st.last_heads[0][3]
        span_new = st.last_heads[-1][3]
        return (span_new / span_old) if span_old > 1.0 else 1.0

    def _receding(self, st: _TrackState) -> bool:
        """The person is walking AWAY — their whole body shrinks and their head
        slides down the image, which must NOT be read as sitting down."""
        return self._scale_ratio(st) < (1.0 - settings.posture_recede_shrink_frac)

    def _occluded_transition(self, st: _TrackState,
                             raw: PostureResult) -> Posture | None:
        """Sit<->stand inferred from the HEAD alone, for when a table/desk hides
        the legs so no knee angle exists. Guarded against approach/recede — both
        move the head vertically without any change of posture."""
        if len(st.last_heads) < 2:
            return None
        t_old, y_old, _, _ = st.last_heads[0]
        t_new, y_new, ref_new, _ = st.last_heads[-1]
        dt = (t_new - t_old).total_seconds()
        if dt <= 0 or dt > settings.walking_motion_window_secs:
            return None
        if raw.torso_angle_deg >= settings.fall_torso_angle_deg:
            return None                                   # not upright — fall owns this
        dy_frac = (y_new - y_old) / max(ref_new, 1.0)     # +ve = head moved DOWN
        thr = settings.posture_occluded_sit_head_frac
        scale = self._scale_ratio(st)
        recede = settings.posture_recede_shrink_frac
        if dy_frac >= thr and scale >= (1.0 - recede):    # head DOWN, not receding
            return Posture.SITTING
        if dy_frac <= -thr and scale <= (1.0 + recede):   # head UP, not approaching
            return Posture.STANDING
        return None

    # ---- the ONE fall FSM -------------------------------------------
    def _update_fall_fsm(self, st: _TrackState, raw: PostureResult,
                         ts: datetime, floor_query) -> None:
        """Single owner of the fall signal — detection IS the alert, no wait:

          DOWN   — N consecutive ~horizontal frames (fall_confirmation_frames):
                   the FALLEN posture label update() returns (monitor dot, rules).
          ALERT  — the SAME DOWN, at/near the ground, raised the INSTANT it is
                   seen (confirm_fall drains it). Falls are prioritised: a person
                   who fell and is already moving or getting back up has still
                   fallen, so we alert on detection and never wait for a post-fall
                   immobility hold. Latched so one fall episode = one alert
                   (confirm_fall's cooldown is the backstop).

        The near-floor check is the ONE discriminator kept, to separate a fall
        (head drops to the ground) from bending over (torso horizontal but head
        still high) — draw a floor zone so that check has teeth. With no floor
        zone drawn a confirmed horizontal is taken as a fall (fail loud)."""
        # ---- DOWN: the label-level state ----------------------------
        if raw.posture == Posture.FALLEN:      # torso ~horizontal this frame
            st.fall_streak += 1
        else:
            st.fall_streak = 0
        st.fall_down = st.fall_streak >= settings.fall_confirmation_frames

        near = floor_query(raw.head_x, raw.head_y) if floor_query is not None else None
        near_ok = (near is True) or (near is None and not settings.fall_require_near_floor)

        # ---- immediate, prioritised trigger -------------------------
        if st.fall_down and near_ok:
            if not st.fall_alerted:            # rising edge of a fall episode…
                st.fall_alerted = True
                st.fall_confirmed_pending = True   # …alert NOW
        else:
            st.fall_alerted = False            # got up / only bending -> re-arm

    # Helpers for FallRule
    def confirm_fall(self, camera_id: str, track_id: int) -> bool:
        """True once when the FSM confirms a fall and we're past cooldown."""
        key = (camera_id, track_id)
        st = self._state.get(key)
        if st is None or not st.fall_confirmed_pending:
            return False
        st.fall_confirmed_pending = False        # consume the one-shot
        now = clock.now()
        last = self._last_fall.get(key)
        if last is not None and (now - last).total_seconds() < settings.fall_cooldown_secs:
            return False
        self._last_fall[key] = now
        return True
