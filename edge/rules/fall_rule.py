from __future__ import annotations

"""
Fall events — the rule-engine face of the ONE fall machine in
posture_classifier. The machine owns the whole signal: its DOWN state is the
FALLEN posture label AND, at/near the ground, the alert — raised the instant a
fall is detected (falls are prioritised; there is no post-fall wait). This rule
just drains confirmations:
  1. confirm_fall() returns True once per fall (one-shot) and enforces the
     per-track cooldown window.
  2. A confirmation fires even if the person has already gotten back up by
     this 1 Hz tick — a fall the CR recovered from is still a fall.

Scans ALL tracked persons deliberately (not just the recipient): visitor
falls are detected and logged locally; the cloud publisher's recipient
gate decides what is forwarded.
"""

import logging
import uuid
from datetime import datetime, timezone

from common.freshness import POSTURE_FRESH_SECS, is_fresh
from schemas.event import Event
from rules.rule_context import RuleContext


logger = logging.getLogger("rules.fall")


class FallRule:
    FRESH_SECS = POSTURE_FRESH_SECS   # ignore stale posture records (idle cameras)

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        for camera_id, per_track in ctx.postures.get_all().items():
            for track_id, rec in per_track.items():
                if not is_fresh(rec.timestamp, now, self.FRESH_SECS):
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
                        track_id=track_id,   # lets the enricher resolve area + box
                    )
                )
                logger.warning(
                    "FALL camera=%s track=%s torso=%.0fdeg",
                    camera_id, track_id, rec.torso_angle_deg,
                )
        return events
