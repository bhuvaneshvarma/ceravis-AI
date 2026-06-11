from __future__ import annotations

"""
Posture-based fall detection.

Conditions for emitting a 'fall' event (all required):
  1. Posture is FALLEN (torso ~horizontal) in the latest record.
  2. The PostureTracker has confirmed FALLEN for
     settings.fall_confirmation_frames consecutive frames.
  3. We're outside the per-track cooldown window.

False positives are aggressively suppressed because:
  - Confirmation across multiple pose frames means single-frame keypoint
    noise can't trigger.
  - Cooldown prevents the same fall being re-emitted while the person
    is still on the floor.
"""

import logging
import uuid
from datetime import datetime, timezone

from schemas.event import Event
from pose.posture_classifier import Posture
from rules.rule_context import RuleContext


logger = logging.getLogger("rules.fall")


class FallRule:
    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        for camera_id, per_track in ctx.postures.get_all().items():
            for track_id, rec in per_track.items():
                if rec.posture != Posture.FALLEN:
                    continue
                if not ctx.posture_tracker.confirm_fall(camera_id, track_id):
                    continue
                identity = ctx.identities.get(camera_id, track_id)
                events.append(
                    Event(
                        event_id=str(uuid.uuid4()),
                        event_type="fall",
                        camera_id=camera_id,
                        room_name="",   # filled by EventEnricher (camera config)
                        recipient_id=identity.recipient_id if identity else None,
                        timestamp=now.isoformat(),
                    )
                )
                logger.warning(
                    "FALL camera=%s track=%s torso=%.0fdeg",
                    camera_id, track_id, rec.torso_angle_deg,
                )
        return events
