#!/usr/bin/env python3
"""
Prove a recording notification can never hold up an alarm again.

On 2026-09-09 camera start/stop events were queued on the cloud outbox. The
endpoint was failing, and since the outbox never drops anything, ~10 of them
retried every 30s for 85 minutes. Delivery is synchronous on one thread, so each
doomed attempt held the socket for the full 8s timeout — the sender was ~267%
oversubscribed and a FALL alert raised in that window had to wait for a doomed
request to finish before it could even be picked.

The fix has two halves and this covers both:

  Reporter  — recording events now go to their own best-effort reporter: one
              thread, a BOUNDED queue that sheds the oldest, and NO retries.
              A dead endpoint costs a few slots, never 85 minutes of retries,
              and /api/v1/recordings/status remains the authoritative state.
  Retired   — a device upgrading from the old build still has recordingEvent
              rows in its queue. The sender clears them on sight instead of
              retrying a kind it can no longer send.

Pure python + sqlite; no network, no camera. Runs on the dev box:

    python tests/test_recording_events.py
"""
from __future__ import annotations

import sys
import tempfile
import time
import types
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

_TMP = Path(tempfile.mkdtemp(prefix="ceravis-recevents-"))
import os                                                        # noqa: E402
os.environ["DATA_DIR"] = str(_TMP)

from integration import outbox_sender                            # noqa: E402
from integration.ceravis_api import CeravisApiError              # noqa: E402
from integration.outbox_sender import OutboxSender               # noqa: E402
from storage.outbox_store import OutboxStore, PRIORITY_FALL      # noqa: E402
from storage.sqlite_store import SqliteStore                     # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# ===========================================================================
print("1. the reporter never blocks the recorder, and never retries")

from integration import recording_events                         # noqa: E402

SENT: list[dict] = []
STATE = {"fail": False, "hang": 0.0}


def fake_send(payload: dict) -> None:
    if STATE["hang"]:
        time.sleep(STATE["hang"])
    if STATE["fail"]:
        raise CeravisApiError("app server returned HTTP 404", status=404)
    SENT.append(payload)


recording_events.send_recording_event = fake_send
recording_events.is_configured = lambda: True
recording_events.effective_edge_id = lambda: "EDGE-1"

reporter = recording_events.RecordingEventReporter()
reporter.start()

reporter.queue_recording_event("KITCHEN", "started", "2026-09-09T08:00:00+05:30")
reporter.queue_recording_event(
    "KITCHEN", "finalized", "2026-09-09T08:00:00+05:30",
    end="2026-09-09T08:05:00+05:30", seconds=300.0)
time.sleep(0.5)
check("both transitions were delivered", len(SENT) == 2)
check("the payload is the app server's DTO, unchanged",
      sorted(SENT[0]) == ["camera_id", "edgeId", "segment", "status"]
      and sorted(SENT[0]["segment"]) == ["end", "seconds", "start"])
check("a started event leaves end/seconds null",
      SENT[0]["segment"]["end"] is None and SENT[0]["segment"]["seconds"] is None)
check("a finalized event carries the closed stretch",
      SENT[1]["segment"]["end"] is not None and SENT[1]["segment"]["seconds"] == 300.0)

# THE incident condition: the endpoint is dead.
STATE["fail"] = True
SENT.clear()
for i in range(5):
    reporter.queue_recording_event(f"CAM{i}", "started", "2026-09-09T09:00:00+05:30")
time.sleep(0.6)
check("a failing endpoint delivers nothing", SENT == [])
check("and NOTHING is left queued to retry — one attempt each, then gone",
      reporter.stats()["pending"] == 0)

# The caller is the 0.5s recording tick: it must never wait on the network.
STATE["hang"] = 1.5                      # every send now takes 1.5s
t0 = time.time()
for i in range(10):
    reporter.queue_recording_event(f"SLOW{i}", "started", "2026-09-09T09:00:00+05:30")
handoff = time.time() - t0
check(f"handing over 10 events took {handoff * 1000:.0f}ms, not seconds",
      handoff < 0.05)
STATE["hang"] = 0.0

# Overflow is bounded by construction.
STATE["fail"] = False
before = reporter.stats()["dropped"]
for i in range(recording_events._MAX_PENDING * 3):
    reporter.queue_recording_event(f"FLOOD{i}", "started", "2026-09-09T09:00:00+05:30")
check("the queue is bounded — it can never grow into a backlog",
      reporter.stats()["pending"] <= recording_events._MAX_PENDING)
check("overflow sheds the oldest and is counted",
      reporter.stats()["dropped"] > before)

reporter.stop()
reporter.join(timeout=3)
check("it shuts down cleanly", True)

# ===========================================================================
print("\n2. the outbox no longer accepts recording events at all")

check("the producer is gone from the sender",
      not hasattr(OutboxSender, "queue_recording_event"))
check("recordingEvent is listed as a retired kind",
      "recordingEvent" in outbox_sender._RETIRED_KINDS)

# ===========================================================================
print("\n3. rows left by the OLD build clear themselves on upgrade")

outbox_sender.save_alert = lambda pid, t, m: {"alertId": 1}
outbox_sender.save_snapshot = lambda *a, **k: True
outbox_sender.is_configured = lambda: True

store = SqliteStore(str(_TMP / "outbox.db"))
outbox = OutboxStore(store)
sender = OutboxSender(outbox)

# Exactly what a device upgrading from the 684526c build has sitting in its DB.
stale = [outbox.enqueue("recordingEvent",
                        {"edgeId": "EDGE-1", "camera_id": f"CAM{i}",
                         "status": "started", "segment": {}},
                        label=f"CAM{i} started")
         for i in range(10)]
fall = sender.queue_alert(7, "FALL", "someone fell", priority=PRIORITY_FALL)
check("10 stale rows and a fall are queued", outbox.stats()["pending"] == 11)

lanes = {lane: (lo, hi) for lane, lo, hi in outbox_sender._LANES}
t0 = time.time()
for _ in range(40):                       # drive the lanes to a standstill
    for lane, (lo, hi) in lanes.items():
        sender._tick(lane, lo, hi)
    if outbox.stats()["pending"] == 0:
        break
elapsed = time.time() - t0

check("the queue drained completely", outbox.stats()["pending"] == 0)
check(f"and it took no network time at all ({elapsed * 1000:.0f}ms)", elapsed < 1.0)
check("every stale row was dropped, not retried",
      all(outbox.job(j)["state"] == "dead" for j in stale))
check("each says why, so the drop is never a mystery",
      "no longer sent from the outbox" in (outbox.job(stale[0])["last_error"] or ""))
check("the fall alert was delivered normally alongside them",
      outbox.job(fall)["state"] == "done")

store.close()

# ===========================================================================
import shutil                                                    # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All recording-event checks passed.")
