"""
One clock for the whole device.

Timestamps drifted apart across the tree: the monitor console stamped UTC while
alerts and recordings stamped edge-local, enrollment status wrote a NAIVE string
with no offset at all, and three wait loops measured durations with a wall clock
that an NTP step can move underneath them.

This is a STATIC source check — no engine, no camera, no clock skew needed, so
it runs anywhere and cannot be skipped by a missing dependency.

The rules it enforces:
  1. Wall-clock "now" comes from common.clock, never datetime.now()/utcnow().
  2. Exactly ONE documented exception exists (ONVIF WS-Security needs UTC-Z).
  3. Durations use a MONOTONIC clock, never wall time.
  4. clock.now() is timezone-AWARE, so arithmetic against UTC-stamped buffers
     stays correct.

Run:  PYTHONPATH=edge python tests/test_time_unified.py
"""
import io
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "edge"))

from common import clock                      # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "static"}

# The clock module itself must call the stdlib — it IS the implementation.
CLOCK_IMPL = "edge/common/clock.py"
SELF = "tests/test_time_unified.py"

# ONVIF WS-Security UsernameToken folds this exact UTC string into the password
# digest the camera verifies. An edge-local offset is rejected as a bad digest.
UTC_EXEMPT = {"edge/onvif/soap.py"}

# outbox_sender schedules the next retry into a PERSISTED SQLite column
# (outbox.next_attempt REAL). That has to be wall-clock epoch: a monotonic value
# is meaningless after the restart the queue exists to survive.
DEADLINE_EXEMPT = {"edge/integration/outbox_sender.py"}

RX_UTCNOW = re.compile(r"\bdatetime\.utcnow\s*\(")
RX_NOW_UTC = re.compile(r"datetime\.now\(\s*timezone\.utc\s*\)")
RX_NOW_NAIVE = re.compile(r"datetime\.now\(\s*\)")
RX_WALL_DEADLINE = re.compile(r"\btime\.time\s*\(\s*\)\s*[+<>]")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def sources():
    for p in sorted(ROOT.rglob("*.py")):
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        rel = p.relative_to(ROOT).as_posix()
        # Skip the clock implementation (it IS the stdlib call) and this file
        # (it carries every banned pattern as a literal, by necessity).
        if rel in (CLOCK_IMPL, SELF):
            continue
        for n, line in enumerate(
                io.open(p, encoding="utf-8", errors="replace").read().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            yield rel, n, line


print("\n1. nobody calls datetime.utcnow() (naive, and deprecated in 3.12)")
hits = [f"{r}:{n}" for r, n, l in sources() if RX_UTCNOW.search(l)]
check("no datetime.utcnow() anywhere", not hits, ", ".join(hits[:5]))


print("\n2. UTC 'now' only where a wire protocol demands it")
hits = [f"{r}:{n}" for r, n, l in sources()
        if RX_NOW_UTC.search(l) and r not in UTC_EXEMPT]
check("datetime.now(timezone.utc) only in the exempt file",
      not hits, ", ".join(hits[:5]))
soap = ROOT / "edge/onvif/soap.py"
check("the exemption is DOCUMENTED where it lives",
      "WS-Security" in io.open(soap, encoding="utf-8").read())


print("\n3. no naive local 'now' (an offset-less timestamp is unorderable)")
hits = [f"{r}:{n}" for r, n, l in sources() if RX_NOW_NAIVE.search(l)]
check("no bare datetime.now()", not hits, ", ".join(hits[:5]))


print("\n4. durations use a monotonic clock, not wall time")
# `time.time() + x` / `time.time() < x` is a DEADLINE — an NTP step moves it.
# Bare time.time() stored as an epoch column or compared to st_mtime is fine.
hits = [f"{r}:{n}" for r, n, l in sources()
        if RX_WALL_DEADLINE.search(l) and r not in DEADLINE_EXEMPT]
check("no wall-clock deadline arithmetic", not hits, ", ".join(hits[:6]))


print("\n5. the clock itself behaves")
now = clock.now()
check("clock.now() is timezone-AWARE", now.tzinfo is not None, str(now.tzinfo))
check("clock.now_iso() carries an offset",
      bool(re.search(r"[+-]\d{2}:\d{2}$|Z$", clock.now_iso())), clock.now_iso())
check("clock.local_tz() resolves", clock.local_tz() is not None)
# Aware arithmetic against a UTC-stamped buffer must still be correct — this is
# what lets a UTC timestamp from an older buffer compare cleanly to local now.
from datetime import datetime, timezone      # noqa: E402
drift = abs((now - datetime.now(timezone.utc)).total_seconds())
check("aware arithmetic vs a UTC stamp is offset-free", drift < 2.0,
      f"{drift:.3f}s apart")


print("\n6. the modules that stamp user-visible time are on the clock")
for rel, marker in [
        ("edge/integration/call_log.py", "clock.now_iso()"),
        ("edge/events/event_enricher.py", "clock.now()"),
        ("edge/enrollment/enrollment_manager.py", "clock.now_iso()"),
        ("edge/alerts/cloud_alert_publisher.py", "clock.now()"),
]:
    src = io.open(ROOT / rel, encoding="utf-8").read()
    check(f"{rel.split('/')[-1]} stamps via clock", marker in src)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll time-unification checks passed.")
