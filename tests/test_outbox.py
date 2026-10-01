#!/usr/bin/env python3
"""
Prove event uploads leave at once on a healthy link, and survive a dead one.

A fake app server that can hang a call, go offline, reject or overload, driven
through the REAL OutboxSender (live workers + the drainer, real threads) and the
real OutboxStore on a scratch SQLite file.

Covered:
  Live       — on a healthy link an alert, its photo and its clip go out at once
               with NOTHING written to disk; the photo carries the alert's id.
  Side by side — a fall is not stuck behind a hanging photo or a slow alert, and
               a photo waits only for its OWN alert.
  Kept       — a failed send is kept on the SSD under its own id (the same
               Idempotency-Key on retry); a still is referenced, a clip spooled
               once; its photos are kept behind it; it survives a restart.
  Path clear — ANY answered app-server call (a recordingEvent) sends what was
               kept at once, falls first, in order, with the alert links intact;
               a down server is probed one upload per pause, not flooded.
  Load       — an overload answer pauses: ambient photos raised meanwhile are
               kept without being fired at the server, an alarm is still tried.
  Never lost — a rejection retries and is stepped around; 401 raises attention;
               a vanished file retries; shutdown keeps what was not sent.
  Window     — the store's caps shed ambient first, never an alert something
               depends on; the age window; spool/orphan/history reclaim.

Pure python + sqlite; no network. Runs on the dev box:

    python tests/test_outbox.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

# A scratch data dir BEFORE settings is read (the spool lives under it).
_TMP = Path(tempfile.mkdtemp(prefix="ceravis-outbox-"))
os.environ["DATA_DIR"] = str(_TMP)

from config.settings import settings                             # noqa: E402
from integration import ceravis_api, outbox_sender               # noqa: E402
from integration.ceravis_api import CeravisApiError              # noqa: E402
from integration.outbox_sender import OutboxSender               # noqa: E402
from storage import outbox_store                                 # noqa: E402
from storage.outbox_store import (PRIORITY_ALERT, PRIORITY_AMBIENT,  # noqa: E402
                                  PRIORITY_FALL, OutboxStore)
from storage.sqlite_store import SqliteStore                     # noqa: E402

# Real threads, fast clocks: backoffs and pauses in tens of milliseconds.
settings.outbox_backoff_base_secs = 0.05
settings.outbox_backoff_max_secs = 0.2
settings.outbox_overload_pause_max_urgent_secs = 0.2
settings.outbox_overload_pause_max_secs = 0.5
settings.outbox_bulk_min_interval_secs = 0.0
settings.outbox_poll_secs = 0.5

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


class FakeServer:
    """A stand-in app server, called from several threads at once."""

    def __init__(self) -> None:
        self.online = True
        self.reject: dict[str, int] = {}           # text -> HTTP status
        self.overload: dict[str, tuple] = {}       # text -> (status, retry_after)
        self.hold: set[str] = set()                # texts that hang until released
        self.gate = threading.Event()
        self.received: list[tuple] = []            # (kind, text, alert_id)
        self.attempts: list[tuple] = []            # (text, key, monotonic)
        self.alert_ids: dict[str, int] = {}
        self._lock = threading.Lock()
        self._next_id = 100

    def _call(self, text: str, key) -> None:
        with self._lock:
            self.attempts.append((text, key, time.monotonic()))
        if text in self.hold:
            self.gate.wait(10.0)
        if not self.online:
            raise CeravisApiError("cannot reach app server: connection refused")
        if text in self.overload:
            code, after = self.overload[text]
            raise CeravisApiError(f"HTTP {code}", status=code, retry_after=after)
        if text in self.reject:
            raise CeravisApiError(f"HTTP {self.reject[text]}", status=self.reject[text])

    def save_alert(self, pid, alert_type, message, idempotency_key=None):
        self._call(message, idempotency_key)
        with self._lock:
            self._next_id += 1
            self.alert_ids[message] = self._next_id
            self.received.append(("saveAlert", message, None))
            return {"alertId": self._next_id}

    def save_snapshot(self, pid, text, camera_number, *, image=None, video=None,
                      alert_id=None, category=None, idempotency_key=None):
        self._call(text, idempotency_key)
        with self._lock:
            self.received.append(("saveSnapshot", text, alert_id))
        return True

    def texts(self) -> list[str]:
        with self._lock:
            return [r[1] for r in self.received]

    def tries(self, text: str) -> int:
        with self._lock:
            return sum(1 for a in self.attempts if a[0] == text)

    def clear(self) -> None:
        with self._lock:
            self.received.clear()
            self.attempts.clear()


def build(server: FakeServer, db: Path):
    """The real store + sender, the API calls patched at the sender's module."""
    outbox_sender.save_alert = server.save_alert
    outbox_sender.save_snapshot = server.save_snapshot
    outbox_sender.is_configured = lambda: True
    store = SqliteStore(str(db))
    outbox = OutboxStore(store)
    return store, outbox, OutboxSender(outbox)


def rows(outbox: OutboxStore) -> int:
    return outbox._store.fetchall("SELECT COUNT(*) FROM outbox")[0][0]


def answered() -> None:
    """What any app-server call answered with a 2xx does (e.g. recordingEvent)."""
    ceravis_api._wire("recordingEvent", "POST", "http://test", {}, status=200)


SPOOL = _TMP / "outbox"
EVENTS = _TMP / "events"
EVENTS.mkdir()
STILL = EVENTS / "fall.jpg"
STILL.write_bytes(b"jpeg-of-the-fall")
CLIP = b"mp4-incident-clip"

server = FakeServer()
DB = _TMP / "ceravis.db"
store, outbox, sender = build(server, DB)
sender.start()

# --------------------------------------------------------------------------
print("\n1. a healthy link: sent at once, nothing written to disk")
a = sender.queue_alert(7, "FALL", "fell · Kitchen", priority=PRIORITY_FALL)
sender.queue_snapshot(7, "fell · Kitchen", "KITCHEN", image_path=STILL,
                      depends_on=a, category="FALL", priority=PRIORITY_FALL)
sender.queue_snapshot(7, "fell · Kitchen", "KITCHEN", video=CLIP,
                      depends_on=a, category="FALL", priority=PRIORITY_FALL)
check("alert, photo and clip all delivered", until(lambda: len(server.received) == 3))
check("the alert first, its photo and clip after it",
      [r[0] for r in server.received] == ["saveAlert", "saveSnapshot", "saveSnapshot"])
check("both carry the alertId the server gave the alert",
      [r[2] for r in server.received[1:]] == [server.alert_ids["fell · Kitchen"]] * 2)
check("not one row was written on the way", rows(outbox) == 0)
check("nothing spooled", list(SPOOL.glob("*")) == [])
check("nothing left in flight", until(lambda: not sender._live, 1.0))

# --------------------------------------------------------------------------
print("\n2. a fall is not stuck behind hanging uploads")
server.clear()
server.gate.clear()
server.hold = {"slow photo 1", "slow photo 2", "slow no-motion"}
sender.queue_snapshot(7, "slow photo 1", "LOUNGE", image=b"j")
sender.queue_snapshot(7, "slow photo 2", "LOUNGE", image=b"j")
sender.queue_alert(7, "NO_MOTION", "slow no-motion")
until(lambda: server.tries("slow no-motion") and server.tries("slow photo 2"), 2.0)
t0 = time.time()
sender.queue_alert(7, "FALL", "urgent fall", priority=PRIORITY_FALL)
went = until(lambda: "urgent fall" in server.texts(), 2.0)
dt = time.time() - t0
check(f"the fall went out in {dt:.2f}s while two photos and an alert hang",
      went and dt < 1.0 and server.texts() == ["urgent fall"])
server.gate.set()
check("the hanging ones complete once the server answers",
      until(lambda: len(server.received) == 4))
server.hold = set()

# --------------------------------------------------------------------------
print("\n3. a photo waits only for its OWN alert")
server.clear()
server.gate.clear()
server.hold = {"alert A"}
aa = sender.queue_alert(7, "FALL", "alert A", priority=PRIORITY_FALL)
pa = sender.queue_snapshot(7, "photo A", "LOUNGE", image=b"j", depends_on=aa,
                           priority=PRIORITY_FALL)
sender.queue_snapshot(7, "posture photo", "LOUNGE", image=b"j")
check("a photo of something else is not held by that alert",
      until(lambda: "posture photo" in server.texts(), 2.0)
      and "photo A" not in server.texts())
check("photo A waits beside its alert, not on a worker",
      pa in sender._live and sender._work["alarm"].qsize() == 0)
server.gate.set()
check("then goes the moment its alert lands, with its id",
      until(lambda: "photo A" in server.texts())
      and [r[2] for r in server.received if r[1] == "photo A"]
      == [server.alert_ids["alert A"]])
server.hold = set()

# --------------------------------------------------------------------------
print("\n4. the link is down: a failed send is kept on the SSD")
server.clear()
server.online = False
still2 = EVENTS / "fall2.jpg"
still2.write_bytes(b"jpeg-2")
fa = sender.queue_alert(7, "FALL", "outage fall", priority=PRIORITY_FALL)
fp = sender.queue_snapshot(7, "outage fall", "KITCHEN", image_path=still2,
                           depends_on=fa, category="FALL", priority=PRIORITY_FALL)
fc = sender.queue_snapshot(7, "outage fall", "KITCHEN", video=CLIP,
                           depends_on=fa, category="FALL", priority=PRIORITY_FALL)
nm = sender.queue_alert(7, "NO_MOTION", "outage lull")
check("all four kept", until(lambda: outbox.stats()["pending"] == 4))
check("nothing reached the server", server.received == [])
check("the fall alert is first in line", outbox.head()["job_id"] == fa)
check("kept under the SAME id the live attempt used (one Idempotency-Key)",
      outbox.job(fa) is not None
      and {k for t, k, _ in server.attempts if t == "outage fall"} >= {fa})
check("its photo and clip are kept BEHIND it",
      outbox.job(fp)["depends_on"] == fa and outbox.job(fc)["depends_on"] == fa)
check("the still is referenced in place, not copied",
      outbox.job(fp)["blob_owned"] == 0 and len(list(SPOOL.glob("*"))) == 1)
check("the clip is spooled once", outbox.blob(outbox.job(fc)) == CLIP)
with server._lock:
    seen = len(server.attempts)            # the live tries are behind us
time.sleep(0.6)
with server._lock:
    probes = sorted(m for _t, _k, m in server.attempts[seen:])
gaps = [y - x for x, y in zip(probes, probes[1:])]
check(f"a down server is probed one upload per pause, not flooded "
      f"({len(probes)} probes in 0.6 s, at least {min(gaps or [0]):.2f} s apart)",
      len(probes) >= 2 and min(gaps) >= 0.04)
lines = [json.loads(ln) for ln in
         (_TMP / "cloud_calls.jsonl").read_text(encoding="utf-8").splitlines()]
check("the console shows each kept upload as QUEUED",
      sum(c.get("state") == "queued" for c in lines) >= 4)

# --------------------------------------------------------------------------
print("\n5. the device restarts mid-outage — what was kept is still there")
sender.stop()
sender.join(timeout=2)
store.close()
store, outbox, sender = build(server, DB)
check("all four survived the restart", outbox.stats()["pending"] == 4)
check("the fall alert is still first in line", outbox.head()["job_id"] == fa)
check("the spooled clip survived too", outbox.blob(outbox.job(fc)) == CLIP)

# --------------------------------------------------------------------------
print("\n6. any answered call clears the path: what was kept goes at once")
settings.outbox_backoff_base_secs = settings.outbox_backoff_max_secs = 60.0
settings.outbox_overload_pause_max_urgent_secs = 60.0
sender.start()
until(lambda: outbox.job(fa)["attempts"] >= 1, 3.0)   # it probed, failed, paused
server.online = True
time.sleep(0.5)
check("while paused nothing is fired at the server", server.received == [])
answered()                               # a recordingEvent got its 2xx
check("an answered recordingEvent sends everything kept",
      until(lambda: outbox.stats()["pending"] == 0, 3.0))
check("in the order it happened, the fall before the lesser alert",
      server.texts() == ["outage fall"] * 3 + ["outage lull"])
check("the photo and clip carry the fall's new alertId",
      [r[2] for r in server.received if r[0] == "saveSnapshot"]
      == [server.alert_ids["outage fall"]] * 2)
check("the clip gave its spool back", list(SPOOL.glob("*")) == [])
check("the still was not deleted (it belongs to the event)", still2.exists())
settings.outbox_backoff_base_secs, settings.outbox_backoff_max_secs = 0.05, 0.2
settings.outbox_overload_pause_max_urgent_secs = 0.2

# --------------------------------------------------------------------------
print("\n7. an overloaded server: pause, keep ambient, still try alarms")
server.clear()
server.overload = {"busy photo": (503, None)}
sender.queue_snapshot(7, "busy photo", "LOUNGE", image=b"j")
check("the 503 photo is kept", until(lambda: outbox.stats()["pending"] == 1))
nxt = sender.queue_snapshot(7, "next photo", "LOUNGE", image=b"j")
check("a photo raised during the pause is kept WITHOUT being fired at it",
      outbox.job(nxt) is not None and server.tries("next photo") == 0)
sender.queue_alert(7, "FALL", "fall in the pause", priority=PRIORITY_FALL)
check("an alarm is still tried live, and lands",
      until(lambda: "fall in the pause" in server.texts(), 2.0))
server.overload = {}
check("once answered, the kept photos follow",
      until(lambda: outbox.stats()["pending"] == 0, 3.0)
      and {"busy photo", "next photo"} <= set(server.texts()))
retries = [k for _t, k, _m in server.attempts if _t == "busy photo"]
check("the retry reused the live attempt's Idempotency-Key",
      len(retries) >= 2 and len(set(retries)) == 1)

# --------------------------------------------------------------------------
print("\n8. a rejection is never dropped; attention is raised and cleared")
server.clear()
server.reject = {"still rejecting": 400, "bad key": 401}
bad = sender.queue_alert(7, "FALL", "still rejecting", priority=PRIORITY_FALL)
key = sender.queue_alert(7, "FALL", "bad key", priority=PRIORITY_FALL)
sender.queue_snapshot(7, "a good photo", "LOUNGE", image=b"j")
check("the good one is delivered around them",
      until(lambda: "a good photo" in server.texts(), 2.0))
check("the rejected ones are kept and retried",
      until(lambda: outbox.job(bad) and outbox.job(bad)["attempts"] >= 2, 3.0)
      and outbox.job(bad)["state"] == "pending")
check("the 401 raised the needs-attention note",
      (outbox.stats().get("attention") or {}).get("code") == 401)
server.reject = {}
answered()
check("once accepted, both land and the note clears",
      until(lambda: outbox.stats()["pending"] == 0, 3.0)
      and outbox.stats().get("attention") is None)

# --------------------------------------------------------------------------
print("\n9. a kept still whose file vanished retries, never drops")
server.online = False
gone = EVENTS / "gone.jpg"
gone.write_bytes(b"x")
g = sender.queue_snapshot(7, "gone photo", "LOUNGE", image_path=gone)
until(lambda: outbox.job(g) is not None, 2.0)
server.online = True
gone.unlink()
answered()
check("still pending, with the reason",
      until(lambda: "missing" in (outbox.job(g)["last_error"] or ""), 3.0)
      and outbox.job(g)["state"] == "pending")
gone.write_bytes(b"restored")
answered()
check("delivered once the file is readable again",
      until(lambda: outbox.job(g)["state"] == "done", 3.0))

# --------------------------------------------------------------------------
print("\n10. too many photos waiting: the overflow goes to the SSD, not RAM")
server.clear()
server.gate.clear()
server.hold = {f"queue photo {i}" for i in range(12)}
for i in range(12):
    sender.queue_snapshot(7, f"queue photo {i}", "LOUNGE", image=b"j")
check(f"RAM holds at most {outbox_sender._AMBIENT_MAX_WAITING} waiting; the rest are kept",
      until(lambda: outbox.stats()["pending"] == 12 - 2
            - outbox_sender._AMBIENT_MAX_WAITING, 2.0))
server.gate.set()
check("and all twelve are delivered",
      until(lambda: len([t for t in server.texts() if t.startswith("queue")]) == 12))
server.hold = set()

# --------------------------------------------------------------------------
print("\n11. shutdown keeps what was not sent")
server.clear()
server.gate.clear()
server.hold = {"in flight"}
fl = sender.queue_alert(7, "FALL", "in flight", priority=PRIORITY_FALL)
ch = sender.queue_snapshot(7, "its photo", "LOUNGE", image=b"j", depends_on=fl,
                           priority=PRIORITY_FALL)
until(lambda: server.tries("in flight") == 1, 2.0)
sender.stop()
sender.join(timeout=0.3)
check("the alert in flight is kept, under its own id",
      outbox.job(fl) is not None and outbox.job(fl)["state"] == "pending")
check("its photo is kept behind it", outbox.job(ch)["depends_on"] == fl)
server.gate.set()                        # the hung request finishes in the end
server.hold = set()
time.sleep(0.2)
store.close()

# --------------------------------------------------------------------------
print("\n12. the store's window: ambient goes first, never a needed alert")
store, outbox, sender = build(server, _TMP / "window.db")    # not started
settings.outbox_max_items = 5
keep = [outbox.enqueue_alert(7, "FALL", f"fall {i}") for i in range(3)]
toss = [outbox.enqueue_snapshot(7, f"posture {i}", "LOUNGE", image=b"j",
                                priority=PRIORITY_AMBIENT) for i in range(6)]
states = {j: outbox.job(j)["state"] for j in keep + toss}
check("held at the cap", outbox.stats()["pending"] == 5)
check("every fall alert kept", all(states[j] == "pending" for j in keep))
check("the oldest ambient dropped, the newest kept",
      [states[j] for j in toss] == ["dead"] * 4 + ["pending"] * 2)
settings.outbox_max_items = 2000
for j in keep + toss:
    outbox.mark_sent(j)
settings.outbox_max_items = 2
parent = outbox.enqueue_alert(7, "FALL", "the alert")
child = outbox.enqueue_snapshot(7, "its photo", "KITCHEN", image=b"j",
                                depends_on=parent, priority=PRIORITY_ALERT)
spare = outbox.enqueue_alert(7, "FALL", "an unrelated alert")
check("an alert its photo depends on is never the one evicted",
      outbox.job(parent)["state"] == "pending" and outbox.job(child)["state"] == "dead"
      and outbox.job(spare)["state"] == "pending")
settings.outbox_max_items = 2000
settings.outbox_window_secs = 60.0
old = outbox.head()["job_id"]
outbox._store.execute(
    "UPDATE outbox SET created_epoch=created_epoch-300 WHERE job_id=?", (old,))
outbox.trim()
check("past the age window it is given up on, the reason named",
      outbox.job(old)["state"] == "dead"
      and "window" in (outbox.job(old)["last_error"] or ""))
settings.outbox_window_secs = 172800.0
check("a dropped upload is reported on the console",
      any(json.loads(ln).get("state") == "dropped" for ln in
          (_TMP / "cloud_calls.jsonl").read_text(encoding="utf-8").splitlines()))

# --------------------------------------------------------------------------
print("\n13. reclaim: orphaned spool files and old receipts")
orphan = SPOOL / "orphan-from-a-crash.jpg"
orphan.write_bytes(b"x" * 4096)
outbox._swept_at = 0.0
outbox.trim()
check("a file still being written is NOT taken for an orphan", orphan.exists())
os.utime(orphan, (0, 0))
outbox._swept_at = 0.0
outbox.trim()
check("but a settled crash-orphan is reclaimed", not orphan.exists())
settings.outbox_history_secs = 86400.0
outbox_store._HISTORY_MAX_ROWS = 10
for i in range(30):
    outbox.mark_sent(outbox.enqueue_alert(7, "FALL", f"receipt {i}"))
outbox.trim()
check("finished rows are capped by count", rows(outbox) <= 10 + outbox.stats()["pending"])
outbox_store._HISTORY_MAX_ROWS = 500
store.close()

# --------------------------------------------------------------------------
print("\n14. a table from before zero-copy migrates and still delivers")
legacy = SqliteStore(str(_TMP / "legacy.db"))
legacy.execute("""CREATE TABLE outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, label TEXT,
    priority INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL, created_epoch REAL NOT NULL, payload TEXT NOT NULL,
    blob_path TEXT, blob_part TEXT, blob_bytes INTEGER NOT NULL DEFAULT 0,
    depends_on TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT,
    result_id INTEGER)""")
(SPOOL / "legacyjob.jpg").write_bytes(b"old-spooled")
legacy.execute("""INSERT INTO outbox (job_id, kind, label, priority, created_at,
    created_epoch, payload, blob_path, blob_part, blob_bytes) VALUES
    ('legacyjob','saveSnapshot','old row',1,'2026-09-24T00:00:00',?,
     '{"patient_id": 7, "text": "old row", "camera_number": "LOUNGE"}',
     'legacyjob.jpg','image',11)""", (time.time(),))
legacy.close()
server.clear()
store, outbox, sender = build(server, _TMP / "legacy.db")
check("the old row reads back as spooled media",
      outbox.job("legacyjob")["blob_owned"] == 1)
sender.start()
check("it is delivered on start, its spool file released",
      until(lambda: "old row" in server.texts())
      and until(lambda: not (SPOOL / "legacyjob.jpg").exists(), 1.0))

# --------------------------------------------------------------------------
print("\n15. idle: nothing kept means an answered call costs nothing")
check("the drainer notices nothing is kept", until(lambda: not sender._kept, 2.0))
writes = []
real_wake_all = outbox.wake_all
outbox.wake_all = lambda: writes.append(1) or real_wake_all()
answered()
check("so a 2xx does not even touch the database", writes == [])
sender.stop()
sender.join(timeout=2)
store.close()

# --------------------------------------------------------------------------
shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All cloud-outbox checks passed.")
