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

WHICH CLOCK TO REACH FOR — the whole policy, enforced by
tests/test_time_unified.py:

  "what time is it"     -> clock.now() / clock.now_iso()
      Anything stamped, stored, logged, compared, or shown to a person.
      Never datetime.now() (naive: an offset-less timestamp cannot be ordered
      against anything) and never datetime.utcnow() (naive AND deprecated).

  "how long has passed" -> time.monotonic() / time.perf_counter()
      Timeouts, deadlines, retry gaps, rate limits, latency. These must NEVER
      use wall time: systemd-timesyncd steps the clock, and a stepped clock
      makes a wall-based deadline fire instantly or never.

  "an epoch number"     -> time.time()
      Only where the value IS a POSIX epoch: comparing to st_mtime, or a REAL
      column that has to survive a restart (outbox.next_attempt). Epoch is
      timezone-independent, so it is not a unification problem — but it is not
      a duration clock either.

ONE documented exception exists in the whole tree: ONVIF WS-Security folds a
UTC-'Z' string into the digest the camera verifies (edge/onvif/soap.py).
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
