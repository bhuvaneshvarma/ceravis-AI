#!/usr/bin/env python3
"""
Prove event retention expires the right things and nothing else.

Event snapshots were the last unbounded writer on the data volume: one JPEG per
event, forever. The sweep that now bounds them is only safe if it is exact —
deleting a still that an alert still needs, or a row a snapshot still points at,
would be worse than the disk filling slowly.

Covered:
  Window     — rows and snapshot day-folders past EVENT_RETENTION_DAYS go; the
               ones inside it stay, untouched.
  Both halves— the row and its file expire together, so the record is never half
               deleted in either direction.
  Strays     — a day folder left by a PREVIOUS device_id ages out too (that is
               what sweeping by folder buys), and a folder whose name is not a
               date is never touched.
  Outbox     — a queued upload survives its source snapshot being deleted,
               because the outbox spooled its own copy of the bytes. This is the
               reason retention needs no coupling to the queue at all.
  Disabled   — 0 days keeps everything, forever, as documented.
  Wired      — the sweep actually RUNS: EventWriter, the thread that already
               owns writing events, applies it without anything else starting.

Pure python + sqlite; no cv2, no camera, no network. Runs on the dev box:

    python tests/test_event_retention.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

# Scratch dirs BEFORE settings is read, so the test never touches the real data/
# tree (events root, the spool and the db are all derived from settings).
_TMP = Path(tempfile.mkdtemp(prefix="ceravis-events-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["EVENTS_DIR"] = str(_TMP / "events")
os.environ["SQLITE_PATH"] = str(_TMP / "ceravis.db")
os.environ["EVENT_RETENTION_DAYS"] = "7"

from common import clock, event_snapshots                       # noqa: E402
from config.settings import settings                            # noqa: E402
from events.event_bus import EventBus                           # noqa: E402
from events.event_writer import EventWriter                     # noqa: E402
from schemas.event import Event                                 # noqa: E402
from storage.event_store import EventStore                      # noqa: E402
from storage.outbox_store import OutboxStore                    # noqa: E402
from storage.sqlite_store import SqliteStore                    # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def snapshot(device: str, days_ago: int, event_id: str) -> Path:
    """Write a stand-in JPEG exactly where EventEnricher would have."""
    day = (clock.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    f = event_snapshots.events_root() / device / day / f"{event_id}.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\xff\xd8\xff" + event_id.encode() * 64)
    return f


def event(event_id: str, days_ago: int, rel: str) -> Event:
    return Event(event_id=event_id, event_type="fall", camera_id="cam-1",
                 room_name="Bedroom",
                 timestamp=(clock.now() - timedelta(days=days_ago)).isoformat(),
                 snapshot_path=rel)


store = SqliteStore(settings.sqlite_path)
events = EventStore(store)
dev = settings.device_id

# --------------------------------------------------------------------------
print("\n1. a record older than the window is expired, both halves")
old_file = snapshot(dev, 30, "old-1")
fresh_file = snapshot(dev, 1, "fresh-1")
events.save_event(event("old-1", 30, f"{dev}/old/old-1.jpg"))
events.save_event(event("fresh-1", 1, f"{dev}/fresh/fresh-1.jpg"))

rows, folders = events.sweep_retention()
check("the old row is gone", events.snapshot_path("old-1") is None)
check("the old snapshot file is gone", not old_file.exists())
check("it reported what it dropped", rows == 1 and folders == 1)

print("\n2. anything inside the window is untouched")
check("the recent row is still there",
      events.snapshot_path("fresh-1") is not None)
check("the recent snapshot file is still there", fresh_file.exists())

# --------------------------------------------------------------------------
print("\n3. strays age out, unreadable names never do")
stale_dev = snapshot("edge-PREVIOUS-ID", 20, "orphan-1")   # no row ever existed
junk = event_snapshots.events_root() / dev / "not-a-date"
junk.mkdir(parents=True, exist_ok=True)
(junk / "keep.jpg").write_bytes(b"\xff\xd8\xff")

events.sweep_retention()
check("a day folder from a previous device_id is expired",
      not stale_dev.exists())
check("its emptied device folder is cleaned up too",
      not stale_dev.parent.parent.exists())
check("a folder that is not a date is left alone", (junk / "keep.jpg").exists())

# --------------------------------------------------------------------------
print("\n4. a queued upload outlives the snapshot it came from")
# Exactly what CloudAlertPublisher does: read the JPEG at event time and hand
# the BYTES to the outbox, which spools its own copy.
about_to_expire = snapshot(dev, 9, "queued-1")
body = about_to_expire.read_bytes()
outbox = OutboxStore(store)
job_id = outbox.enqueue_snapshot(42, "Fall detected", "CAMERA_1", image=body)
events.save_event(event("queued-1", 9, f"{dev}/x/queued-1.jpg"))

events.sweep_retention()
job = outbox.job(job_id)
check("the source snapshot was expired", not about_to_expire.exists())
check("the upload is still pending", job["state"] == "pending")
check("and still carries its bytes — the spool is a separate copy",
      outbox.blob(job) == body)

# --------------------------------------------------------------------------
print("\n5. 0 days = keep forever")
settings.event_retention_days = 0
ancient = snapshot(dev, 400, "ancient-1")
events.save_event(event("ancient-1", 400, f"{dev}/y/ancient-1.jpg"))
rows, folders = events.sweep_retention()
check("nothing is swept when retention is disabled",
      (rows, folders) == (0, 0) and ancient.exists()
      and events.snapshot_path("ancient-1") is not None)

# --------------------------------------------------------------------------
print("\n6. the sweep is actually wired to a running thread")
settings.event_retention_days = 7
stale = snapshot(dev, 40, "wired-1")
events.save_event(event("wired-1", 40, f"{dev}/z/wired-1.jpg"))
writer = EventWriter(EventBus(), events)
writer.start()                       # nothing else is started: no rules, no API
deadline = time.monotonic() + 5.0
while stale.exists() and time.monotonic() < deadline:
    time.sleep(0.1)
writer.stop()
writer.join(2.0)
check("the event writer expires the old record on its own",
      not stale.exists() and events.snapshot_path("wired-1") is None)

# --------------------------------------------------------------------------
store.close()
shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All event-retention checks passed.")
