from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from detection.detection_buffer import DetectionBuffer
from ingestion.frame_buffer import FrameBuffer
from pose.pose_buffer import PoseBuffer
from pose.posture_buffer import PostureBuffer
from pose.posture_classifier import PostureTracker
from reid.identity_buffer import IdentityBuffer
from tracking.track_buffer import TrackBuffer


def is_fresh(ts: datetime, now: datetime, max_age_secs: float) -> bool:
    """True when a buffer timestamp is recent enough to act on.

    With active-camera-only focus, the idle cameras' buffers go STALE rather
    than empty (the tracker dedupes on frame_id and never rewrites them) — a
    rule reading raw get_all() would keep acting on a frozen snapshot of a
    camera the recipient already left: phantom stillness/inactivity, and for
    StillnessRule a false CRITICAL no_motion an hour after a room change."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() <= max_age_secs


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

    def fresh_tracks(self, now: datetime, max_age_secs: float = 5.0):
        """Per-camera TrackResults younger than max_age_secs (see is_fresh)."""
        return {cam: res for cam, res in self.tracks.get_all().items()
                if is_fresh(res.timestamp, now, max_age_secs)}
