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
  * a timestamp handed back to us later (event -> footage playback) resolves to
    the exact instant regardless of the reader's timezone.
"""

from datetime import datetime, timedelta, timezone, tzinfo


def local_tz() -> tzinfo:
    """The device's configured local timezone (falls back to UTC)."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def now() -> datetime:
    """Current edge-local time, timezone-aware."""
    return datetime.now(local_tz())


def now_iso() -> str:
    """Current edge-local time as an ISO-8601 string with offset."""
    return now().isoformat()


def to_aware(ts) -> datetime:
    """Parse any incoming timestamp into an absolute, timezone-aware instant.

    Accepts a datetime or an ISO-8601 string. A NAIVE value (no offset) is read
    as edge-local time — the system's single clock — so a timestamp the frontend
    echoes back from an alert always maps to the right moment. Raises ValueError
    on an unparseable string.
    """
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip().replace("Z", "+00:00")
        # Frontends send the timestamp RAW in the query string, where standard
        # URL decoding turns the offset's '+' into a space ("…T20:00:05 05:30").
        # A real ISO string with a 'T' separator never contains a space, so a
        # space here can only be that eaten '+' — repair it instead of making
        # every caller URL-encode.
        if "T" in s and " " in s:
            s = s.replace(" ", "+")
        dt = datetime.fromisoformat(s)          # raises ValueError if malformed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt


def to_rfc3339(ts) -> str:
    """An incoming timestamp normalized to RFC-3339 (what MediaMTX playback
    wants). Naive input is treated as edge-local; aware input is preserved."""
    return to_aware(ts).isoformat()


def shift_iso(ts, seconds: float) -> str:
    """`ts` moved by `seconds` (may be negative), returned as RFC-3339 — used to
    step the playback cursor back 15s from an event, then forward for continuation."""
    return (to_aware(ts) + timedelta(seconds=seconds)).isoformat()
