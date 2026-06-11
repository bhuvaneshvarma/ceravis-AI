from __future__ import annotations

from dataclasses import dataclass

from detection.detection_buffer import DetectionBuffer
from ingestion.frame_buffer import FrameBuffer
from pose.pose_buffer import PoseBuffer
from pose.posture_buffer import PostureBuffer
from pose.posture_classifier import PostureTracker
from reid.identity_buffer import IdentityBuffer
from tracking.track_buffer import TrackBuffer


@dataclass(slots=True, frozen=True)
class RuleContext:
    """All buffers a rule may inspect — passed once per tick."""
    frames: FrameBuffer
    detections: DetectionBuffer
    tracks: TrackBuffer
    poses: PoseBuffer
    postures: PostureBuffer
    posture_tracker: PostureTracker
    identities: IdentityBuffer
