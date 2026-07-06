from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from common.freshness import TRACK_FRESH_SECS, is_fresh
from detection.detection_buffer import DetectionBuffer
from ingestion.frame_buffer import FrameBuffer
from pose.pose_buffer import PoseBuffer
from pose.posture_buffer import PostureBuffer
from pose.posture_classifier import Posture, PostureTracker
from reid.identity_buffer import IdentityBuffer
from reid.identity_schema import Identity
from tracking.track_buffer import TrackBuffer
from tracking.track_schema import Track


@dataclass(slots=True)
class RecipientSighting:
    """Where the recipient is RIGHT NOW — the one answer every recipient-scoped
    rule shares (stillness, posture transitions, location)."""
    camera_id: str
    track: Track
    identity: Identity
    posture: Posture              # label from the posture buffer (UNKNOWN if none)
    keypoints: list | None        # the target's 17-pt pose, when available


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

    def fresh_tracks(self, now: datetime,
                     max_age_secs: float = TRACK_FRESH_SECS):
        """Per-camera TrackResults younger than max_age_secs (see
        common.freshness: idle cameras keep stale buffers forever)."""
        return {cam: res for cam, res in self.tracks.get_all().items()
                if is_fresh(res.timestamp, now, max_age_secs)}

    def find_recipient(self, now: datetime,
                       max_age_secs: float = TRACK_FRESH_SECS
                       ) -> RecipientSighting | None:
        """THE resolver for "where is the recipient right now": freshest
        cameras only, best ReID confidence when several claim the target.
        Every recipient-scoped rule reads this one mechanism. FallRule is the
        deliberate exception — it scans ALL persons, because visitor falls
        are still detected locally (the cloud gate decides what is sent)."""
        best = None
        for camera_id, result in self.fresh_tracks(now, max_age_secs).items():
            for t in result.tracks:
                ident = self.identities.get(camera_id, t.track_id)
                if not (ident and ident.is_target):
                    continue
                if best is None or ident.confidence > best[1].confidence:
                    best = (t, ident)
        if best is None:
            return None
        track, ident = best
        rec = self.postures.get(track.camera_id, track.track_id)
        return RecipientSighting(
            camera_id=track.camera_id, track=track, identity=ident,
            posture=rec.posture if rec is not None else Posture.UNKNOWN,
            keypoints=self._target_keypoints(track.camera_id, track.track_id))

    def _target_keypoints(self, camera_id: str, track_id: int):
        """The target's 17 COCO keypoints on this camera, or None.

        Pose runs TARGET-ONLY once the recipient is locked (see PoseRunner),
        so the buffer holds a single pose — the target's. Poses aren't tagged
        with a track_id in this pipeline, so prefer a track_id match if one
        ever appears, else take the most confident pose."""
        pr = self.poses.get(camera_id)
        if pr is None or not pr.poses:
            return None
        tagged = [p for p in pr.poses if p.track_id == track_id]
        pose = tagged[0] if tagged else max(
            pr.poses, key=lambda p: sum(k.confidence for k in p.keypoints))
        return pose.keypoints if len(pose.keypoints) >= 17 else None
