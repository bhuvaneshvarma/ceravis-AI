from __future__ import annotations

"""
One freshness definition for the whole pipeline.

With active-camera-only focus, idle cameras' buffers go STALE rather than
empty (writers dedupe on frame_id and never rewrite them). Anything reading
per-camera buffers must ignore entries older than these horizons, or it acts
on a frozen snapshot of a camera the person already left — phantom stillness,
false CRITICAL no-motion alerts, ghost tracks on the monitor.
"""

from datetime import datetime, timezone

TRACK_FRESH_SECS = 5.0      # track results update at detection fps on live cams
POSTURE_FRESH_SECS = 10.0   # posture records update at pose fps (slower)


def is_fresh(ts: datetime, now: datetime, max_age_secs: float) -> bool:
    """True when a buffer timestamp is recent enough to act on."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() <= max_age_secs
