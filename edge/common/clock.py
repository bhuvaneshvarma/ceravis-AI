from __future__ import annotations

"""
One clock for the whole edge — the device's LOCAL time, timezone-aware.

Single point of truth for "now" so every user-facing timestamp (alerts,
snapshots) and the recording timeline share ONE wall clock: the Jetson's local
time. The device runs 24/7 and is NTP-disciplined by systemd-timesyncd, so its
local clock is the authority; nothing in the system invents its own time.

Everything returned is timezone-AWARE (carries the local UTC offset), so:
  * .isoformat() yields e.g. 2026-07-16T20:00:00+05:30 — unambiguous AND local;
  * subtracting two aware datetimes is always correct even when one came from a
    buffer stamped in UTC (aware arithmetic normalizes the offset);
  * the same instant stamped on an alert and on a recording segment lines up
    exactly, which is what lets footage playback resolve an event to the frame.
"""

from datetime import datetime, timezone, tzinfo


def local_tz() -> tzinfo:
    """The device's configured local timezone (falls back to UTC)."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def now() -> datetime:
    """Current edge-local time, timezone-aware."""
    return datetime.now(local_tz())


def now_iso() -> str:
    """Current edge-local time as an ISO-8601 string with offset."""
    return now().isoformat()
