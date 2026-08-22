from __future__ import annotations

"""
THE answer to "is the recipient moving right now" — one owner, two channels.

This replaces the single max-over-keypoints statistic that made no_motion
unreachable. That estimator took the MAX displacement over every keypoint above
a 0.20 confidence floor, so ONE noisy joint out of seventeen re-anchored the
whole 60-minute clock. Measured on a genuinely motionless person with realistic
per-keypoint jitter: 72.9% of ticks were called "motion", 59 resets in 60 ticks,
longest still-run 0.00 s. The hour could never mature, so only no_transition
ever fired — exactly the reported symptom.

Two channels, because neither is sufficient alone:

  PIXELS (primary, sensitive)
      A rectangle anchored in FRAME coordinates, downscaled, blurred and
      standardised, compared by mean absolute difference against the reference
      taken when stillness began. This is "no pixel movement" measured directly
      rather than inferred.

      The ROI is fixed in frame space and deliberately does NOT follow the
      tracker box. Measured: 3 px of box jitter produces MORE apparent change
      (0.047) than a real 25 px hand movement (0.040), so a box-following crop
      would drown the signal it exists to detect. The cameras are static wall
      units, so a fixed rectangle is sound: if the person is still the pixels
      are identical, and if they leave the rectangle that IS motion.

      Standardising each signature (subtract mean, divide by std) makes the
      channel immune to global illumination change — an 8% light flicker
      measures 0.0032, indistinguishable from sensor noise.

  POSE (secondary, conservative)
      Counts high-confidence keypoints that have moved, against a robust anchor.
      Deliberately tuned for GROSS body movement only. Pose cannot resolve a
      seated person's hand movement — the wrist sits near 0.44 confidence with
      ~13 px of jitter while the hand moves 18-34 px, and no threshold separates
      those. So pose is not asked to; it catches whole-body motion and carries
      the signal when no frame is available.

Both sides of the pose comparison are per-keypoint MEDIANS over a short window
(anchor side and current side alike), so neither a single bad reference frame
nor a single bad current frame can move the verdict.

Fusion is OR — moving if EITHER channel says so — behind an M-of-N window rather
than M CONSECUTIVE ticks. Real movement is intermittent (the hand moves, pauses,
moves); consecutive-tick hysteresis is blind to exactly that, while a sliding
window still cancels independent noise.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from collections import deque

import numpy as np

from config.settings import settings

try:
    import cv2
    _HAVE_CV2 = True
except ImportError:                                  # pragma: no cover
    cv2 = None                                       # type: ignore
    _HAVE_CV2 = False


logger = logging.getLogger("rules.motion")

# COCO torso indices, for the scale reference.
_L_SH, _R_SH, _L_HIP, _R_HIP = 5, 6, 11, 12


@dataclass(frozen=True, slots=True)
class MotionVerdict:
    """What the detector concluded this tick, and why — the `reason` string is
    the observability this signal never had."""
    moving: bool
    reason: str
    still_secs: float
    moved_joints: int | None = None       # pose channel: joints over threshold
    pixel_diff: float | None = None       # pixel channel: normalised MAD
    pose_ready: bool = False
    pixel_ready: bool = False


class TargetMotionDetector:
    """Per-recipient (single-target system); re-anchors on a camera change."""

    def __init__(self) -> None:
        self._cam: str | None = None
        # Accumulated stillness, NOT a since-timestamp. A tick we cannot judge
        # (pose dropout, no frame) must HOLD the total rather than silently
        # accrue time as if the person had been verified still.
        self._still_accum: float = 0.0
        self._last_tick: datetime | None = None
        self._warned_no_pixels = False

        # pose channel
        self._recent: deque = deque(maxlen=max(1, settings.pose_still_window))
        self._anchor: np.ndarray | None = None        # (17, 2) median anchor pose

        # pixel channel
        self._roi: tuple[int, int, int, int] | None = None
        self._ref_sig: np.ndarray | None = None

        # fusion hysteresis
        self._window: deque = deque(maxlen=max(1, settings.motion_confirm_n))

    # ---- public ------------------------------------------------------
    def reset(self) -> None:
        self._cam = None
        self._still_accum = 0.0
        self._last_tick = None
        self._recent.clear()
        self._anchor = None
        self._roi = None
        self._ref_sig = None
        self._window.clear()

    def update(self, camera_id: str, keypoints, bbox, frame, now: datetime
               ) -> MotionVerdict:
        """One tick. `keypoints` may be None (pose dropout), `frame` may be None
        (no FrameBuffer, e.g. under test) — the detector degrades to whichever
        channel is available and says so in the verdict."""
        if camera_id != self._cam:                   # first sight / camera hop
            self.reset()
            self._cam = camera_id
        dt = 0.0 if self._last_tick is None else max(
            0.0, (now - self._last_tick).total_seconds())
        self._last_tick = now

        moved_joints, pose_moved, pose_ready = self._pose_channel(keypoints, bbox)
        pixel_diff, pixel_moved, pixel_ready = self._pixel_channel(frame, bbox)

        raw_moving = bool(pose_moved or pixel_moved)
        self._window.append(1 if raw_moving else 0)
        confirmed = sum(self._window) >= settings.motion_confirm_m

        if confirmed:
            reason = ("pixels" if pixel_moved else "") + \
                     ("+pose" if pixel_moved and pose_moved else
                      ("pose" if pose_moved else ""))
            self._reanchor(now)
            return MotionVerdict(
                moving=True, reason=f"moved ({reason or 'fused'})", still_secs=0.0,
                moved_joints=moved_joints, pixel_diff=pixel_diff,
                pose_ready=pose_ready, pixel_ready=pixel_ready)

        # no_motion is a CRITICAL alert, so stillness is only ever claimed on a
        # channel that can actually refute it. The pose channel alone CANNOT: a
        # seated person's hand movement is indistinguishable from wrist jitter at
        # 0.44 confidence, whatever the threshold. Without pixels we therefore
        # HOLD and never accrue — failing toward silence, not toward a false
        # critical alarm on a live care system.
        if settings.pixel_still_enabled and not pixel_ready:
            if not self._warned_no_pixels:
                logger.warning(
                    "stillness: pixel channel unavailable (no frame, or ROI "
                    "under %dpx) — no_motion HELD. Pose alone cannot rule out "
                    "hand movement, so no critical alert will be raised.",
                    settings.pixel_still_min_roi_px)
                self._warned_no_pixels = True
            return MotionVerdict(
                moving=False, reason="pixel channel unavailable (holding)",
                still_secs=self._still_accum, moved_joints=moved_joints,
                pixel_diff=pixel_diff, pose_ready=pose_ready, pixel_ready=False)
        self._warned_no_pixels = False

        if not (pose_ready or pixel_ready):
            # Nothing to judge with. HOLD rather than guess in either direction
            # — a dropout is not evidence of stillness.
            return MotionVerdict(
                moving=False, reason="no usable signal (holding)",
                still_secs=self._still_accum, moved_joints=moved_joints,
                pixel_diff=pixel_diff, pose_ready=False, pixel_ready=False)

        self._still_accum += dt
        return MotionVerdict(
            moving=False, reason="still", still_secs=self._still_accum,
            moved_joints=moved_joints, pixel_diff=pixel_diff,
            pose_ready=pose_ready, pixel_ready=pixel_ready)

    # ---- pose channel ------------------------------------------------
    def _pose_channel(self, keypoints, bbox):
        """(moved_joints, moved, ready). Gross body movement only — see module
        docstring for why pose is not asked to resolve hand movement."""
        arr = self._to_array(keypoints)
        if arr is None:
            return None, False, False
        self._recent.append(arr)
        need = max(1, settings.pose_still_window)
        if len(self._recent) < need:
            return None, False, False

        current = self._median_pose()
        if self._anchor is None:
            self._anchor = current                   # robust anchor, not one frame
            return 0, False, True

        scale = self._scale(current, bbox)
        base = scale * settings.pose_move_frac
        k = settings.pose_still_conf_weight
        moved = 0
        for i in range(current.shape[0]):
            c_conf, a_conf = current[i, 2], self._anchor[i, 2]
            if c_conf < settings.pose_still_min_conf or \
                    a_conf < settings.pose_still_min_conf:
                continue
            d = float(np.hypot(current[i, 0] - self._anchor[i, 0],
                               current[i, 1] - self._anchor[i, 1]))
            # A low-confidence joint stays in play but must move FURTHER to
            # count — the threshold mirrors the noise model itself.
            if d > base * (1.0 + k * (1.0 - float(c_conf))):
                moved += 1
        return moved, moved >= settings.pose_still_min_joints, True

    def _median_pose(self) -> np.ndarray:
        stack = np.stack(list(self._recent), axis=0)      # (W, 17, 3)
        return np.median(stack, axis=0)

    @staticmethod
    def _to_array(keypoints) -> np.ndarray | None:
        if not keypoints or len(keypoints) < 17:
            return None
        return np.asarray([[k.x, k.y, k.confidence] for k in keypoints],
                          dtype=np.float32)

    @staticmethod
    def _scale(pose: np.ndarray, bbox) -> float:
        """Torso length, floored by a fraction of bbox height.

        Torso alone foreshortens badly for a SEATED person on a high-mounted
        camera — precisely when no_motion matters most — which shrinks the pixel
        threshold and manufactures motion. The bbox floor keeps the scale sane."""
        floor = 1.0
        if bbox is not None:
            h = float(getattr(bbox, "height", 0.0) or 0.0)
            floor = max(floor, h * settings.pose_still_scale_bbox_frac)
        conf_ok = (pose[[_L_SH, _R_SH, _L_HIP, _R_HIP], 2] >=
                   settings.pose_still_min_conf).all()
        if conf_ok:
            sh = (pose[_L_SH, :2] + pose[_R_SH, :2]) / 2.0
            hp = (pose[_L_HIP, :2] + pose[_R_HIP, :2]) / 2.0
            torso = float(np.hypot(*(sh - hp)))
            return max(torso, floor)
        return floor

    # ---- pixel channel -----------------------------------------------
    def _pixel_channel(self, frame, bbox):
        """(mad, moved, ready). ROI is anchored in FRAME coordinates and never
        follows the tracker box — see the module docstring."""
        if not settings.pixel_still_enabled or not _HAVE_CV2 or frame is None:
            return None, False, False
        if self._roi is None:
            roi = self._roi_from_bbox(bbox, frame)
            if roi is None:
                return None, False, False
            sig = self._signature(frame, roi)
            if sig is None:
                return None, False, False
            self._roi, self._ref_sig = roi, sig
            return 0.0, False, True

        sig = self._signature(frame, self._roi)
        if sig is None or self._ref_sig is None or sig.shape != self._ref_sig.shape:
            return None, False, False
        mad = float(np.mean(np.abs(sig - self._ref_sig)))
        return mad, mad > settings.pixel_move_thresh, True

    @staticmethod
    def _roi_from_bbox(bbox, frame) -> tuple[int, int, int, int] | None:
        if bbox is None:
            return None
        h, w = frame.shape[:2]
        pad = settings.pixel_still_pad_frac
        bw = float(bbox.x2 - bbox.x1)
        bh = float(bbox.y2 - bbox.y1)
        x1 = int(max(0, bbox.x1 - bw * pad))
        y1 = int(max(0, bbox.y1 - bh * pad))
        x2 = int(min(w, bbox.x2 + bw * pad))
        y2 = int(min(h, bbox.y2 + bh * pad))
        m = settings.pixel_still_min_roi_px
        if x2 - x1 < m or y2 - y1 < m:
            return None
        return x1, y1, x2, y2

    @staticmethod
    def _signature(frame, roi) -> np.ndarray | None:
        """Downscale -> blur -> standardise. Downscale+blur absorb sub-pixel
        sensor wobble; standardising removes global illumination change."""
        x1, y1, x2, y2 = roi
        h, w = frame.shape[:2]
        if x2 > w or y2 > h or x2 <= x1 or y2 <= y1:
            return None
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        if crop.ndim == 3:
            crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        n = max(8, int(settings.pixel_still_size))
        s = cv2.resize(crop.astype(np.float32), (n, n),
                       interpolation=cv2.INTER_AREA)
        s = cv2.GaussianBlur(s, (3, 3), 0)
        return (s - s.mean()) / (s.std() + 1e-6)

    # ---- internals ---------------------------------------------------
    def _reanchor(self, now: datetime) -> None:
        """Confirmed motion: everything restarts from here."""
        self._still_accum = 0.0
        self._anchor = None
        self._recent.clear()
        self._roi = None
        self._ref_sig = None
        self._window.clear()
