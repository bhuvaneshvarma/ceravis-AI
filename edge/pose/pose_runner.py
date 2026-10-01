from __future__ import annotations

import logging
import threading
import time

from common.crops import crop_person
from common.floor_reference import FloorReference
from config.settings import settings
from ingestion.frame_buffer import FrameBuffer
from pose.pose_buffer import PoseBuffer
from pose.pose_schema import Keypoint, PoseEstimation
from pose.posture_buffer import PostureBuffer, PostureRecord
from pose.posture_classifier import PostureTracker
from pose.yolo_pose import YOLOPose
from reid.target_registry import TargetRegistry
from tracking.track_buffer import TrackBuffer


logger = logging.getLogger("pose")


def _kp_box(pose: PoseEstimation):
    """The box around a skeleton's confident keypoints, or None."""
    xs = [k.x for k in pose.keypoints if k.confidence > 0.1]
    ys = [k.y for k in pose.keypoints if k.confidence > 0.1]
    return (min(xs), min(ys), max(xs), max(ys)) if xs and ys else None


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


class PoseRunner:
    """
    Pose estimation @ settings.pose_fps with two efficiency wins:

      1. Idle gating — pose inference only runs for a camera that currently
         has tracked people. An empty room costs nothing.
      2. Target focus — once ReID locks the target (settings.target_only_pose),
         pose runs on the TARGET'S padded crop only and maps keypoints back
         to frame space. Sharper pose, and other people are skipped.

    Until a target is locked (or when ReID is disabled) it falls back to
    full-frame pose + IoU association, so posture works for everyone.

    Target crop: padded by pose_crop_padding_frac (raised arms, a foot mid-
    stride, a body tipping over all leave the tracker's box) and letterboxed by
    the model, so the skeleton is not squashed. Anyone else inside that padded
    crop is a risk — a neighbour's skeleton can be the most confident one — so
    the target's skeleton is the one that FITS the target's box; a skeleton
    that fits a neighbour as well is skipped for that frame rather than read.
    Once locked, the target's pose runs at pose_locked_fps: posture changes far
    slower than the frame rate, and every decision is made over a time window.
    """

    def __init__(
        self,
        frame_buffer: FrameBuffer,
        pose_buffer: PoseBuffer,
        track_buffer: TrackBuffer,
        posture_buffer: PostureBuffer,
        metrics_registry=None,
        target_registry: TargetRegistry | None = None,
    ) -> None:
        self._frames = frame_buffer
        self._poses = pose_buffer
        self._tracks = track_buffer
        self._postures = posture_buffer
        self._targets = target_registry or TargetRegistry()
        self._tracker = PostureTracker()
        self._floor = FloorReference()         # scene-aware fall: ground reference
        self._metrics = (
            metrics_registry.get_or_create("pose") if metrics_registry else None
        )
        self._estimator: YOLOPose | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_seen: dict[str, int] = {}
        self._last_target_pose: dict[str, float] = {}

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def posture_tracker(self) -> PostureTracker:
        return self._tracker

    def start(self) -> None:
        if self._running:
            return
        try:
            self._estimator = YOLOPose()
        except Exception:
            logger.exception("PoseRunner disabled (engine missing)")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="pose-runner",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        interval = 1.0 / settings.pose_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                logger.exception("pose tick failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _tick(self) -> None:
        if self._estimator is None:
            return

        for camera_id, fd in self._frames.get_all_latest().items():
            if self._last_seen.get(camera_id) == fd.frame_id:
                continue

            track_result = self._tracks.get(camera_id)
            if track_result is None or not track_result.tracks:
                continue   # idle gate — nobody here, skip pose inference
            self._last_seen[camera_id] = fd.frame_id

            target_tid = self._targets.get(camera_id)
            if settings.target_only_pose and target_tid is not None:
                tgt = next((t for t in track_result.tracks
                            if t.track_id == target_tid), None)
                if tgt is not None:
                    now = time.monotonic()
                    if (now - self._last_target_pose.get(camera_id, 0.0)
                            < 1.0 / max(settings.pose_locked_fps, 0.1)):
                        continue   # locked: the target's pose is sampled, not every frame
                    if self._pose_target(camera_id, fd, tgt, track_result.tracks):
                        self._last_target_pose[camera_id] = now
                        continue   # handled target-only; done with this camera

            # Fallback: full-frame pose for everyone (pre-lock / ReID off).
            self._pose_full_frame(camera_id, fd, track_result)

    # ---- target-only crop path --------------------------------------
    def _pose_target(self, camera_id: str, fd, track, tracks=()) -> bool:
        crop, ox, oy = crop_person(
            fd.image, track.bbox.x1, track.bbox.y1,
            track.bbox.x2, track.bbox.y2, settings.pose_crop_padding_frac,
        )
        if crop.size == 0:
            return False

        t = time.perf_counter()
        result = self._estimator.estimate(
            frame=crop, camera_id=camera_id,
            frame_id=fd.frame_id, timestamp=fd.timestamp,
        )
        if self._metrics:
            self._metrics.record(time.perf_counter() - t)
        if not result.poses:
            return True   # ran inference, no pose — still "handled"

        # The TARGET's skeleton: the one that fits the target's box best —
        # not the most confident one, which may belong to a neighbour inside
        # the padded crop. Keypoints shifted back to frame space first.
        tbox = (track.bbox.x1, track.bbox.y1, track.bbox.x2, track.bbox.y2)
        others = [(t.bbox.x1, t.bbox.y1, t.bbox.x2, t.bbox.y2)
                  for t in tracks if t.track_id != track.track_id]
        best, best_fit = None, 0.0
        for p in result.poses:
            q = self._shift(p, ox, oy, camera_id, fd.frame_id, fd.timestamp)
            kb = _kp_box(q)
            fit = _iou(kb, tbox) if kb else 0.0
            if fit > best_fit:
                best, best_fit = (q, kb), fit
        if best is None or best_fit < settings.pose_target_min_iou:
            return True   # no skeleton of theirs this frame — hold the posture
        shifted, kb = best
        if any(_iou(kb, o) >= best_fit - settings.pose_target_margin_iou for o in others):
            return True   # fits a neighbour as well: ambiguous, not read
        self._poses.update(
            result.model_copy(update={"poses": [shifted]})
        )
        self._classify(camera_id, track.track_id, shifted, frame_h=fd.height)
        return True

    @staticmethod
    def _shift(pose: PoseEstimation, ox: int, oy: int,
               camera_id: str, frame_id: int, ts) -> PoseEstimation:
        kps = [Keypoint(x=k.x + ox, y=k.y + oy, confidence=k.confidence)
               for k in pose.keypoints]
        return PoseEstimation(track_id=None, camera_id=camera_id,
                              frame_id=frame_id, timestamp=ts, keypoints=kps)

    # ---- full-frame path --------------------------------------------
    def _pose_full_frame(self, camera_id: str, fd, track_result) -> None:
        t = time.perf_counter()
        result = self._estimator.estimate(
            frame=fd.image, camera_id=camera_id,
            frame_id=fd.frame_id, timestamp=fd.timestamp,
        )
        if self._metrics:
            self._metrics.record(time.perf_counter() - t)
        self._poses.update(result)

        # One skeleton per person and one person per skeleton: the best-
        # fitting pairs first. Two skeletons used to be able to land on the
        # same track, the second overwriting the first's posture.
        pairs = []
        for i, pose in enumerate(result.poses):
            pbox = _kp_box(pose)
            if pbox is None:
                continue
            for tr in track_result.tracks:
                iou = _iou(pbox, (tr.bbox.x1, tr.bbox.y1, tr.bbox.x2, tr.bbox.y2))
                if iou >= 0.2:
                    pairs.append((iou, i, tr.track_id))
        used_p, used_t = set(), set()
        for iou, i, tid in sorted(pairs, reverse=True):
            if i in used_p or tid in used_t:
                continue
            used_p.add(i)
            used_t.add(tid)
            self._classify(camera_id, tid, result.poses[i], frame_h=fd.height)

    # ---- shared posture write ---------------------------------------
    def _classify(self, camera_id: str, track_id: int,
                  pose: PoseEstimation, frame_h: int = 0) -> None:
        # "low" = on/near the floor OR below nearby furniture height (table/
        # chair/bed) — the scene-aware fall cue.
        low_q = lambda x, y: self._floor.is_low(camera_id, x, y)  # noqa: E731
        res = self._tracker.update(camera_id, track_id, pose,
                                   floor_query=low_q, frame_h=frame_h)
        self._postures.update(
            PostureRecord(
                camera_id=camera_id, track_id=track_id,
                posture=res.posture, confidence=res.confidence,
                timestamp=pose.timestamp,
                torso_angle_deg=res.torso_angle_deg,
                knee_angle_deg=res.avg_knee_angle_deg,
            )
        )
