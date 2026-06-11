from __future__ import annotations

import logging
import threading
import time

from config.settings import settings
from ingestion.frame_buffer import FrameBuffer
from pose.pose_buffer import PoseBuffer
from pose.posture_buffer import PostureBuffer, PostureRecord
from pose.posture_classifier import PostureTracker
from pose.yolo_pose import YOLOPose
from tracking.track_buffer import TrackBuffer


logger = logging.getLogger("pose")


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
    Pose estimation @ settings.pose_fps, then posture classification
    per matched track (IoU between pose-derived bbox and ByteTrack bbox).
    """

    def __init__(
        self,
        frame_buffer: FrameBuffer,
        pose_buffer: PoseBuffer,
        track_buffer: TrackBuffer,
        posture_buffer: PostureBuffer,
        metrics_registry=None,
    ) -> None:
        self._frames = frame_buffer
        self._poses = pose_buffer
        self._tracks = track_buffer
        self._postures = posture_buffer
        self._tracker = PostureTracker()
        self._metrics = (
            metrics_registry.get_or_create("pose") if metrics_registry else None
        )

        self._estimator: YOLOPose | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_seen: dict[str, int] = {}

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
            self._last_seen[camera_id] = fd.frame_id

            t = time.perf_counter()
            result = self._estimator.estimate(
                frame=fd.frame,
                camera_id=fd.camera_id,
                frame_id=fd.frame_id,
                timestamp=fd.timestamp,
            )
            if self._metrics:
                self._metrics.record(time.perf_counter() - t)
            self._poses.update(result)
            self._associate_and_classify(camera_id, result)

    def _associate_and_classify(self, camera_id: str, result) -> None:
        track_result = self._tracks.get(camera_id)
        if track_result is None or not result.poses:
            return

        for pose in result.poses:
            xs = [k.x for k in pose.keypoints if k.confidence > 0.1]
            ys = [k.y for k in pose.keypoints if k.confidence > 0.1]
            if not xs or not ys:
                continue
            pose_bbox = (min(xs), min(ys), max(xs), max(ys))

            best_iou, best_track = 0.0, None
            for tr in track_result.tracks:
                iou = _iou(
                    pose_bbox,
                    (tr.bbox.x1, tr.bbox.y1, tr.bbox.x2, tr.bbox.y2),
                )
                if iou > best_iou:
                    best_iou, best_track = iou, tr

            if best_track is None or best_iou < 0.2:
                continue

            posture_result = self._tracker.update(
                camera_id, best_track.track_id, pose,
            )
            self._postures.update(
                PostureRecord(
                    camera_id=camera_id,
                    track_id=best_track.track_id,
                    posture=posture_result.posture,
                    confidence=posture_result.confidence,
                    timestamp=pose.timestamp,
                    torso_angle_deg=posture_result.torso_angle_deg,
                    knee_angle_deg=posture_result.avg_knee_angle_deg,
                )
            )
