#!/usr/bin/env python3
"""
Prove the cloud outbox does what an outage needs it to do.

The queue only matters on the day the internet is down, which is exactly the day
nobody is watching. So this simulates the outage instead: a fake app server that
can be switched offline, made to reject, or made to fail once, driven through the
real OutboxStore + OutboxSender.

Covered:
  FIFO       — a backlog raised offline is delivered in creation order, and a
               later alert never overtakes an earlier one.
  Durability — the backlog survives a process restart (rows + spooled media).
  Linkage    — a snapshot queued before any alertId existed still carries the
               real alertId once the alert lands.
  No stall   — a 4xx-rejected job is dropped, not retried forever, and the jobs
               behind it still go out.
  Window     — over the cap, ambient snapshots are evicted and alerts are not;
               past the age window a job is given up on; and an alert another
               queued job depends on is never the one evicted.
  Media      — spooled bytes are released once a job leaves the queue.
  Console    — an upload shows as QUEUED at once, and a discarded one is always
               reported rather than silently lost.

Pure python + sqlite; no TensorRT, no camera, no network. Runs on the dev box:

    python tests/test_outbox.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

# A scratch data dir BEFORE settings is read, so the test never touches the real
# data/ tree (the spool path is derived from settings.data_dir).
_TMP = Path(tempfile.mkdtemp(prefix="ceravis-outbox-"))
import os                                                        # noqa: E402
os.environ["DATA_DIR"] = str(_TMP)

from config.settings import settings                             # noqa: E402
from integration import outbox_sender                            # noqa: E402
from integration.ceravis_api import CeravisApiError              # noqa: E402
from integration.outbox_sender import OutboxSender               # noqa: E402
from storage.outbox_store import (PRIORITY_ALERT, PRIORITY_AMBIENT,  # noqa: E402
                                  OutboxStore)
from storage.sqlite_store import SqliteStore                     # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# A stand-in app server. `online` is the network switch.
# ---------------------------------------------------------------------------
class FakeServer:
    def __init__(self) -> None:
        self.online = True
        self.reject_texts: set[str] = set()   # texts answered with a hard 400
        self.received: list[tuple] = []       # ("saveAlert"|"saveSnapshot", …)
        self._next_alert_id = 100

    def save_alert(self, pid, alert_type, message):
        if not self.online:
            raise CeravisApiError("cannot reach app server: connection refused")
        if message in self.reject_texts:
            raise CeravisApiError("app server returned HTTP 400: bad request",
                                  status=400)
        self._next_alert_id += 1
        self.received.append(("saveAlert", message, None))
        return {"alertId": self._next_alert_id}

    def save_snapshot(self, pid, text, camera_number, *, image=None, video=None,
                      alert_id=None, category=None):
        if not self.online:
            raise CeravisApiError("cannot reach app server: connection refused")
        if text in self.reject_texts:
            raise CeravisApiError("app server returned HTTP 422: unprocessable",
                                  status=422)
        self.received.append(("saveSnapshot", text, alert_id))
        return True


def build(server: FakeServer, db: Path):
    """A real store + sender wired to the fake server (patched at the sender's
    module, so the queue, ordering, retry and window logic under test are the
    production ones)."""
    outbox_sender.save_alert = server.save_alert
    outbox_sender.save_snapshot = server.save_snapshot
    outbox_sender.is_configured = lambda: True
    store = SqliteStore(str(db))
    outbox = OutboxStore(store)
    return store, outbox, OutboxSender(outbox)


def pump(sender: OutboxSender, outbox: OutboxStore, rounds: int = 500) -> None:
    """Run the sender's own tick until the queue empties or `rounds` is spent —
    the production loop with the sleeping fast-forwarded, so the test is
    deterministic and instant. Offline sections pass a small `rounds` to model
    "a few retries have gone by and the link is still down"."""
    import time as _t
    for _ in range(rounds):
        job = outbox.head()
        if job is None:
            return
        if job["next_attempt"] > _t.time():
            outbox.mark_retry(job["job_id"], job["last_error"] or "", 0)
        sender._tick()


DB = _TMP / "ceravis.db"
server = FakeServer()
store, outbox, sender = build(server, DB)

# --------------------------------------------------------------------------
print("\n1. the link is down — nothing is sent, nothing is lost")
server.online = False
alert_job = sender.queue_alert(7, "FALL", "CRITICAL · Fall detected · Kitchen")
snap_job = sender.queue_snapshot(7, "CRITICAL · Fall detected · Kitchen",
                                 "KITCHEN", image=b"\xff\xd8jpeg-still",
                                 depends_on=alert_job, category="FALL",
                                 priority=PRIORITY_ALERT)
clip_job = sender.queue_snapshot(7, "CRITICAL · Fall detected · Kitchen",
                                 "KITCHEN", video=b"mp4-incident-clip",
                                 depends_on=alert_job, category="FALL",
                                 priority=PRIORITY_ALERT)
sender.queue_alert(7, "NO_MOTION", "CRITICAL · No movement · Lounge")
pump(sender, outbox, rounds=3)      # a few retries, still no link
check("nothing reached the server", server.received == [])
check("all four uploads are queued", outbox.stats()["pending"] == 4)
check("the head is the fall alert, not the newest event",
      outbox.head()["job_id"] == alert_job)
check("the queue reports the outage", bool(outbox.stats()["last_error"]))

# --------------------------------------------------------------------------
print("\n2. the device restarts mid-outage — the backlog is still there")
store.close()
store, outbox, sender = build(server, DB)
check("all four survived the restart", outbox.stats()["pending"] == 4)
check("still oldest-first", outbox.head()["job_id"] == alert_job)
check("the spooled media survived too",
      outbox.blob(outbox.job(snap_job)) == b"\xff\xd8jpeg-still")

# --------------------------------------------------------------------------
print("\n3. the link comes back — the queue drains itself, in order")
server.online = True
pump(sender, outbox)
kinds = [r[0] for r in server.received]
texts = [r[1] for r in server.received]
check("everything went out", len(server.received) == 4)
check("in exactly the order it happened",
      kinds == ["saveAlert", "saveSnapshot", "saveSnapshot", "saveAlert"])
check("the later NO_MOTION did not overtake the FALL",
      texts[-1].endswith("Lounge"))
check("the queue is empty", outbox.stats()["pending"] == 0)
check("and counted as sent", outbox.stats()["sent"] == 4)

# --------------------------------------------------------------------------
print("\n4. alertId linkage survived the outage")
alert_id = outbox.job(alert_job)["result_id"]
snap_alert_ids = [r[2] for r in server.received if r[0] == "saveSnapshot"]
check("the alert got a server id", alert_id is not None)
check("the still is linked to it", snap_alert_ids[0] == alert_id)
check("so is the clip", snap_alert_ids[1] == alert_id)

# --------------------------------------------------------------------------
print("\n5. spooled media is released once delivered")
spool = _TMP / "outbox"
check("no media files left on disk",
      [f.name for f in spool.glob("*")] == [])
check("the rows no longer point at any", outbox.job(clip_job)["blob_path"] is None)

# --------------------------------------------------------------------------
print("\n6. a rejected upload is dropped, not retried forever")
server.received.clear()
server.reject_texts = {"malformed"}
bad = sender.queue_alert(7, "FALL", "malformed")
good = sender.queue_alert(7, "FALL", "the one behind it")
pump(sender, outbox)
check("the rejected job is dead", outbox.job(bad)["state"] == "dead")
check("its reason is recorded",
      "400" in (outbox.job(bad)["last_error"] or ""))
check("the job behind it still went out",
      [r[1] for r in server.received] == ["the one behind it"])
check("the queue is empty again", outbox.stats()["pending"] == 0)

# --------------------------------------------------------------------------
print("\n7. the sliding window sheds ambient snapshots, never alerts")
server.online = False
settings.outbox_max_items = 5
keep = [sender.queue_alert(7, "FALL", f"fall {i}") for i in range(3)]
toss = [sender.queue_snapshot(7, f"posture {i}", "LOUNGE", image=b"jpg",
                              priority=PRIORITY_AMBIENT) for i in range(6)]
# No pump: the window is enforced on every enqueue, not by the sender.
states = {j: outbox.job(j)["state"] for j in keep + toss}
check("the queue is held at the cap",
      outbox.stats()["pending"] == settings.outbox_max_items)
check("every fall alert is still queued",
      all(states[j] == "pending" for j in keep))
check("the oldest ambient snapshots are the ones dropped",
      [states[j] for j in toss[:4]] == ["dead"] * 4)
check("the newest ambient snapshots were kept",
      [states[j] for j in toss[4:]] == ["pending"] * 2)

# --------------------------------------------------------------------------
print("\n8. an upload older than the window is given up on")
settings.outbox_window_secs = 60.0
old = outbox.head()["job_id"]
# Age it past the upload window but well inside the history window, so the row
# is still there to be inspected after it is given up on.
outbox._store.execute(
    "UPDATE outbox SET created_epoch=created_epoch-300 WHERE job_id=?", (old,))
outbox.trim()
check("the stale job is dead", outbox.job(old)["state"] == "dead")
check("with the window named in the reason",
      "window" in (outbox.job(old)["last_error"] or ""))
check("and it is no longer at the head", outbox.head()["job_id"] != old)

# --------------------------------------------------------------------------
print("\n9. a full queue never evicts an alert something still depends on")
settings.outbox_window_secs = 86400.0
server.online = True
pump(sender, outbox)                     # start from an empty queue
server.online = False
settings.outbox_max_items = 2
parent = sender.queue_alert(7, "FALL", "the alert")
child = sender.queue_snapshot(7, "its photo", "KITCHEN", image=b"jpg",
                              depends_on=parent, priority=PRIORITY_ALERT)
spare = sender.queue_alert(7, "FALL", "an unrelated alert")
# Oldest-first still decides WHICH job goes, but never one that another queued
# job is waiting on: losing the photo of a fall is survivable, delivering a
# photo whose alert was thrown away is not.
check("the alert its snapshot depends on survived",
      outbox.job(parent)["state"] == "pending")
check("the photo went instead, oldest evictable first",
      outbox.job(child)["state"] == "dead")
check("the later unrelated alert is untouched",
      outbox.job(spare)["state"] == "pending")

# --------------------------------------------------------------------------
print("\n10. the sync console sees both states")
import json                                                      # noqa: E402
lines = [json.loads(ln) for ln in
         (_TMP / "cloud_calls.jsonl").read_text(encoding="utf-8").splitlines()]
check("an upload appears as QUEUED the moment the event fires",
      any(c.get("state") == "queued" for c in lines))
check("and a discarded one is reported, never silently lost",
      any(c.get("state") == "dropped" for c in lines))

# --------------------------------------------------------------------------
store.close()
shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All cloud-outbox checks passed.")
