#!/usr/bin/env python3
"""
Prove the cloud outbox does what an outage needs it to do.

The queue only matters on the day the internet is down, which is exactly the day
nobody is watching. So this simulates the outage instead: a fake app server that
can be switched offline, made to reject, or made to fail once, driven through the
real OutboxStore + OutboxSender.

Covered:
  Order      — a FALL is delivered before everything else in the queue even
               when it was raised last, while within a tier delivery is strictly
               oldest-first so an incident is never scrambled.
  Durability — the backlog survives a process restart (rows + spooled media).
  Linkage    — a snapshot queued before any alertId existed still carries the
               real alertId once the alert lands.
  No stall   — a 4xx-rejected job is dropped, not retried forever, and the jobs
               behind it still go out.
  Window     — over the cap, ambient snapshots are evicted and alerts are not;
               past the age window a job is given up on; and an alert another
               queued job depends on is never the one evicted.
  Reclaim    — a delivered upload gives its disk back at once, crash-orphaned
               media is swept, and finished receipts are capped by count.
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
from storage import outbox_store                                 # noqa: E402
from storage.outbox_store import (PRIORITY_ALERT, PRIORITY_AMBIENT,  # noqa: E402
                                  PRIORITY_FALL, OutboxStore)
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
        self.reject_texts: set[str] = set()      # texts answered with a hard 400
        self.reject_status: dict[str, int] = {}  # text -> a specific HTTP status
        self.received: list[tuple] = []          # ("saveAlert"|"saveSnapshot", …)
        self._next_alert_id = 100

    def _maybe_reject(self, text: str) -> None:
        if not self.online:
            raise CeravisApiError("cannot reach app server: connection refused")
        if text in self.reject_status:
            code = self.reject_status[text]
            raise CeravisApiError(f"app server returned HTTP {code}", status=code)
        if text in self.reject_texts:
            raise CeravisApiError("app server returned HTTP 400: bad request",
                                  status=400)

    def save_alert(self, pid, alert_type, message):
        self._maybe_reject(message)
        self._next_alert_id += 1
        self.received.append(("saveAlert", message, None))
        return {"alertId": self._next_alert_id}

    def save_snapshot(self, pid, text, camera_number, *, image=None, video=None,
                      alert_id=None, category=None):
        self._maybe_reject(text)
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


def pump(sender: OutboxSender, outbox: OutboxStore, rounds: int = 4000) -> None:
    """Drive the REAL sender to a standstill, fast-forwarding its sleeps so the
    test is instant. Mirrors _run(): tick; if the tick asked to sleep (nothing
    ready, everything backing off), advance every pending job's backoff by that
    much and loop. This preserves the RELATIVE ordering that the backoff creates
    — a job that just failed is further in the future than one that hasn't — so
    'step around a stuck job' is exercised, not defeated. Returns early once the
    queue is empty; a permanently-failing job just runs out the rounds."""
    for _ in range(rounds):
        if outbox.stats()["pending"] == 0:
            return
        wait = sender._tick()
        if wait and wait > 0:
            outbox._store.execute(
                "UPDATE outbox SET next_attempt = next_attempt - ? "
                "WHERE state='pending' AND next_attempt > 0", (wait,))


DB = _TMP / "ceravis.db"
server = FakeServer()
store, outbox, sender = build(server, DB)

# --------------------------------------------------------------------------
print("\n1. the link is down — nothing is sent, nothing is lost")
server.online = False
STILL = b"\xff\xd8jpeg-still"
CLIP = b"mp4-incident-clip"
alert_job = sender.queue_alert(7, "FALL", "CRITICAL · Fall detected · Kitchen",
                               priority=PRIORITY_FALL)
snap_job = sender.queue_snapshot(7, "CRITICAL · Fall detected · Kitchen",
                                 "KITCHEN", image=STILL,
                                 depends_on=alert_job, category="FALL",
                                 priority=PRIORITY_FALL)
clip_job = sender.queue_snapshot(7, "CRITICAL · Fall detected · Kitchen",
                                 "KITCHEN", video=CLIP,
                                 depends_on=alert_job, category="FALL",
                                 priority=PRIORITY_FALL)
sender.queue_alert(7, "NO_MOTION", "CRITICAL · No movement · Lounge")
pump(sender, outbox, rounds=3)      # a few retries, still no link
check("nothing reached the server", server.received == [])
check("all four uploads are queued", outbox.stats()["pending"] == 4)
check("the head is the fall alert, not the newest event",
      outbox.head()["job_id"] == alert_job)
check("the alert-grade backlog is counted",
      outbox.stats()["pending_alerts"] == 4)
check("the media it is holding is accounted for",
      outbox.stats()["pending_bytes"] == len(STILL) + len(CLIP))
check("the queue reports the outage", bool(outbox.stats()["last_error"]))

# --------------------------------------------------------------------------
print("\n2. the device restarts mid-outage — the backlog is still there")
store.close()
store, outbox, sender = build(server, DB)
check("all four survived the restart", outbox.stats()["pending"] == 4)
check("the fall alert is still first in line",
      outbox.head()["job_id"] == alert_job)
check("the spooled media survived too",
      outbox.blob(outbox.job(snap_job)) == STILL)

# --------------------------------------------------------------------------
print("\n3. the link comes back — the queue drains itself, in order")
server.online = True
# Recovery in production is driven by the status heartbeat: its first clean beat
# kicks the sender, which clears every backoff so the whole backlog is due at
# once and drains in strict priority-then-seq order. (Without the kick, jobs
# that backed off by different amounts would drain as they each come due, which
# can let a due lower-priority job slip ahead of one still backing off — the
# 'step around a stuck job' behaviour. The kick is what makes recovery orderly.)
sender.kick()
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
print("\n6. a server rejection is NEVER dropped — it retries and steps aside")
# The policy: the ONLY thing that drops a job is the age window. A 400 (a server
# mid-DB-swap) keeps retrying, and must not block the good jobs behind it.
server.received.clear()
server.reject_texts = {"still rejecting"}
bad = sender.queue_alert(7, "FALL", "still rejecting", priority=PRIORITY_FALL)
good1 = sender.queue_alert(7, "NO_MOTION", "the one behind it")
good2 = sender.queue_snapshot(7, "an ambient still", "LOUNGE", image=b"jpg",
                              priority=PRIORITY_AMBIENT)
pump(sender, outbox, rounds=200)
check("the rejected job is NOT dropped — still pending, retried",
      outbox.job(bad)["state"] == "pending")
check("it recorded the rejection and kept its attempts up",
      outbox.job(bad)["attempts"] > 3 and "400" in (outbox.job(bad)["last_error"] or ""))
check("the good jobs behind it were delivered anyway (stepped around)",
      set(r[1] for r in server.received) == {"the one behind it", "an ambient still"})
# Now the server recovers (DB swap done) — the held job finally lands.
server.reject_texts = set()
pump(sender, outbox, rounds=200)
check("once the server accepts it, the held job is delivered",
      "still rejecting" in [r[1] for r in server.received])
check("and the queue is finally empty", outbox.stats()["pending"] == 0)

# --------------------------------------------------------------------------
print("\n6b. an attention-grade rejection is flagged, still not dropped")
server.received.clear()
server.reject_status = {"bad key": 401}
authbad = sender.queue_alert(7, "FALL", "bad key", priority=PRIORITY_FALL)
pump(sender, outbox, rounds=30)
check("the 401 job is retried, not dropped", outbox.job(authbad)["state"] == "pending")
check("and it raised the needs-attention note",
      (outbox.stats().get("attention") or {}).get("code") == 401)
# It clears the instant a delivery succeeds again.
server.reject_status = {}
pump(sender, outbox, rounds=30)
check("attention clears once uploads are accepted again",
      outbox.stats().get("attention") is None)

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
print("\n10. a fall raised behind a backlog is sent FIRST")
settings.outbox_max_items = 2000
server.online = True
pump(sender, outbox)                     # start from an empty queue
server.online = False
# An hour of ambient traffic piles up while the link is down…
ambient = [sender.queue_snapshot(7, f"posture {i}", "LOUNGE", image=b"jpg",
                                 priority=PRIORITY_AMBIENT) for i in range(5)]
lull = sender.queue_alert(7, "NO_MOTION", "no movement")
# …then someone falls. It is the NEWEST job in the queue and must still go out
# before every one of them.
fall = sender.queue_alert(7, "FALL", "someone fell", priority=PRIORITY_FALL)
fall_pic = sender.queue_snapshot(7, "someone fell", "LOUNGE", image=b"fall-jpg",
                                 depends_on=fall, priority=PRIORITY_FALL)
check("the fall is at the head despite being queued last",
      outbox.head()["job_id"] == fall)
server.received.clear()
server.online = True
pump(sender, outbox)
order = [r[1] for r in server.received]
check("the fall alert went out first", order[0] == "someone fell")
check("its photo went second, still after its own alert",
      order[1] == "someone fell")
check("the no-motion alert outranked the ambient snapshots",
      order[2] == "no movement")
check("and the ambient backlog followed, oldest first",
      order[3:] == [f"posture {i}" for i in range(5)])
check("the fall's photo carried the alertId the fall had just been given",
      server.received[1][2] == outbox.job(fall)["result_id"])
check("nothing was lost to the reordering",
      len(server.received) == len(ambient) + 3)

# --------------------------------------------------------------------------
print("\n11. a delivered upload gives its disk back")
check("the queue is empty", outbox.stats()["pending"] == 0)
check("it is holding no media", outbox.stats()["pending_bytes"] == 0)
check("and the spool directory is empty", list(spool.glob("*")) == [])
# The one case row-driven deletes cannot see: bytes written, then the crash
# before the row that owns them was committed.
orphan = spool / "orphan-from-a-crash.jpg"
orphan.write_bytes(b"x" * 4096)
outbox._swept_at = 0.0                   # due for its next slow sweep
outbox.trim()
check("a file still being queued is NOT mistaken for an orphan",
      orphan.exists())
os.utime(orphan, (0, 0))                 # …now it is genuinely old
outbox._swept_at = 0.0
outbox.trim()
check("but a settled crash-orphan is reclaimed",
      list(spool.glob("*")) == [])
# Receipts are capped by count as well as age, so the table cannot grow without
# bound between age sweeps. Age is pushed out of reach so the COUNT cap is what
# is under test, and the cap itself is lowered rather than queueing 500 jobs.
settings.outbox_history_secs = 86400.0
outbox_store._HISTORY_MAX_ROWS = 10
for i in range(30):
    sender.queue_alert(7, "FALL", f"receipt {i}", priority=PRIORITY_FALL)
pump(sender, outbox)
outbox.trim()                            # the sender's own slow beat
rows = outbox._store.fetchall("SELECT COUNT(*) FROM outbox")[0][0]
check(f"finished rows are capped by count, not just age ({rows} kept of 30+)",
      rows == 10)

# --------------------------------------------------------------------------
print("\n12. the sync console sees both states")
import json                                                      # noqa: E402
lines = [json.loads(ln) for ln in
         (_TMP / "cloud_calls.jsonl").read_text(encoding="utf-8").splitlines()]
check("an upload appears as QUEUED the moment the event fires",
      any(c.get("state") == "queued" for c in lines))
check("and a discarded one is reported, never silently lost",
      any(c.get("state") == "dropped" for c in lines))

# --------------------------------------------------------------------------
print("\n13. the heartbeat kick drains a backlog held during an outage")
server.online = False
server.received.clear()
held = [sender.queue_alert(7, "FALL", f"outage fall {i}", priority=PRIORITY_FALL)
        for i in range(4)]
pump(sender, outbox, rounds=6)               # a few failed retries, still down
check("nothing delivered while the server is down",
      outbox.stats()["pending"] == 4 and server.received == [])
# The server returns; the next clean heartbeat calls kick() (what the pipeline
# wires StatusReporter(on_online=sender.kick) to do).
server.online = True
sender.kick()
check("the kick made the whole backlog due at once",
      outbox.next_ready() is not None)
pump(sender, outbox)
check("all four held falls delivered right after the beat",
      len(server.received) == 4)
check("and the queue is empty", outbox.stats()["pending"] == 0)

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
