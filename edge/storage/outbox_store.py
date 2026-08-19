from __future__ import annotations

"""
The cloud outbox — a durable, ordered, bounded queue of app-server uploads.

Everything the device wants to push to app.ceravishealth.in as a RESULT OF AN
EVENT (saveAlert, and the saveSnapshot stills + fall clips that belong to it)
is written here FIRST and sent from here SECOND. There is no second path: the
publisher never calls the API directly, so "the alert fired" and "the alert was
delivered" are one mechanism with two states instead of two mechanisms that can
disagree. On a healthy network the queue is drained within milliseconds and is
invisible; during an outage it is the thing that keeps the incident.

Why a queue and not a retry loop:
  * The whole system keeps working offline (streams, AI, recording, LAN live
    view). Only the cloud hop is down, and it comes back — so an alert must
    survive the gap, not be logged-and-lost.
  * Order is clinical information. A fall, its still, and its clip must reach
    the server in the order they happened, and a later NO_MOTION must not
    overtake an earlier FALL. The queue is strict FIFO by `seq`.
  * A restart (or a power cut) in the middle of an outage must not lose the
    backlog, so the queue is SQLite rows + spooled media files on disk, in the
    same data/ceravis.db everything else uses.

SLIDING WINDOW
  The queue is deliberately NOT unbounded — a device offline for a week must not
  fill its disk or, when it reconnects, flood the server with a week of stale
  ambient snapshots. It holds a moving window over the recent past:

    age    : a pending job older than `outbox_window_secs` (default 24h) is
             dropped — nobody is helped by yesterday's snapshot arriving now.
    count  : at most `outbox_max_items` pending jobs.
    bytes  : at most `outbox_max_blob_mb` of spooled media.

  When the count/byte caps bite, eviction is LOWEST PRIORITY FIRST, oldest
  first — the ambient posture/room snapshots go, the FALL and NO_MOTION alerts
  and their media stay. A window that drops the emergency to keep the wallpaper
  would be worse than no window at all.

LAYOUT
  outbox rows   -> data/ceravis.db, table `outbox`
  media bodies  -> data/outbox/<job_id>.<ext>, deleted the moment the job
                   leaves the queue (sent, dead or evicted)

This module is storage only: it decides what is kept and in what order, never
when to send. The sending policy lives in integration/outbox_sender.py.
"""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Callable

from common import clock
from config.settings import settings
from storage.sqlite_store import SqliteStore


logger = logging.getLogger("outbox")

_EDGE_ROOT = Path(__file__).resolve().parents[1]

# Job priority. Eviction takes the lowest number first, so an emergency is the
# last thing a full queue gives up.
PRIORITY_ALERT = 2      # a fall / no-motion alert and the media that proves it
PRIORITY_AMBIENT = 1    # posture, room and dwell snapshots (nice to have)

STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_DEAD = "dead"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    label         TEXT,
    priority      INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    payload       TEXT NOT NULL,
    blob_path     TEXT,
    blob_part     TEXT,
    blob_bytes    INTEGER NOT NULL DEFAULT 0,
    depends_on    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_attempt  REAL NOT NULL DEFAULT 0,
    last_error    TEXT,
    sent_at       TEXT,
    result_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, seq);
CREATE INDEX IF NOT EXISTS idx_outbox_dep ON outbox(depends_on);
"""

_COLS = ("seq", "job_id", "kind", "label", "priority", "state", "created_at",
         "created_epoch", "payload", "blob_path", "blob_part", "blob_bytes",
         "depends_on", "attempts", "next_attempt", "last_error", "sent_at",
         "result_id")


class OutboxStore:
    """The durable FIFO itself. Thread-safe by way of SqliteStore's lock: the
    event thread enqueues while the sender thread drains, and each statement is
    atomic. Exactly one consumer (OutboxSender) claims jobs, so no row-level
    locking or lease is needed."""

    def __init__(self, store: SqliteStore,
                 on_drop: Callable[[dict, str], None] | None = None) -> None:
        self._store = store
        # Called for every job the window discards, so the operator-facing
        # console can say WHICH upload was given up on and why.
        self._on_drop = on_drop
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._store.execute(stmt)
        self._spool = self._spool_dir()
        self._recover()

    def set_drop_listener(self,
                          on_drop: Callable[[dict, str], None] | None) -> None:
        """Register who hears about discarded uploads (the sender wires the sync
        console). One listener: a dropped upload is announced once."""
        self._on_drop = on_drop

    # ---- paths -------------------------------------------------------
    @staticmethod
    def _spool_dir() -> Path:
        base = settings.data_path
        base = base if base.is_absolute() else (_EDGE_ROOT / base)
        spool = base / "outbox"
        spool.mkdir(parents=True, exist_ok=True)
        return spool

    def _recover(self) -> None:
        """Startup: report the backlog we inherited and delete spooled media
        with no row left pointing at it (a crash between file and INSERT)."""
        pending = self.stats()["pending"]
        if pending:
            logger.info("outbox: %d upload(s) waiting from a previous run",
                        pending)
        known = {row[0] for row in self._store.fetchall(
            "SELECT blob_path FROM outbox WHERE blob_path IS NOT NULL")}
        orphans = 0
        for f in self._spool.glob("*"):
            if not f.is_file():
                continue
            if str(f.relative_to(self._spool)) not in known:
                try:
                    f.unlink()
                    orphans += 1
                except OSError:
                    pass
        if orphans:
            logger.info("outbox: cleared %d orphaned media file(s)", orphans)

    # ---- enqueue -----------------------------------------------------
    def enqueue(self, kind: str, payload: dict, *, label: str = "",
                priority: int = PRIORITY_AMBIENT, blob: bytes | None = None,
                blob_part: str | None = None, blob_ext: str = "bin",
                depends_on: str | None = None) -> str | None:
        """Append one upload to the tail of the queue. Returns its job_id — the
        handle a dependent job uses as `depends_on` — or None if it could not be
        persisted (in which case nothing was queued and the caller has already
        lost nothing it had).

        `blob` is the media body; it is spooled to its own file rather than
        stored in the row so the database stays small and a 10 MB fall clip
        never has to be read to answer "how deep is the queue".
        """
        job_id = uuid.uuid4().hex
        blob_rel = None
        blob_len = 0
        try:
            if blob:
                f = self._spool / f"{job_id}.{blob_ext}"
                f.write_bytes(blob)
                blob_rel = f.name
                blob_len = len(blob)
            self._store.execute(
                """INSERT INTO outbox
                   (job_id, kind, label, priority, state, created_at,
                    created_epoch, payload, blob_path, blob_part, blob_bytes,
                    depends_on, attempts, next_attempt)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                (job_id, kind, (label or "")[:300], int(priority), STATE_PENDING,
                 clock.now_iso(), time.time(),
                 json.dumps(payload, default=str), blob_rel, blob_part,
                 blob_len, depends_on),
            )
        except Exception:
            logger.exception("outbox: enqueue failed (%s)", kind)
            if blob_rel:
                self._unlink(blob_rel)
            return None
        self.trim()
        return job_id

    # ---- domain wrappers ---------------------------------------------
    def enqueue_alert(self, patient_id, alert_type: str, message: str,
                      *, label: str = "") -> str | None:
        """Queue one saveAlert. Its job_id is the local stand-in for the
        alertId the server has not issued yet: snapshots that belong to this
        alert pass it as `depends_on`, and the sender substitutes the real
        alertId once the alert lands."""
        return self.enqueue(
            "saveAlert",
            {"patient_id": patient_id, "alert_type": alert_type,
             "message": message},
            label=label or f"{alert_type} · {message}",
            priority=PRIORITY_ALERT)

    def enqueue_snapshot(self, patient_id, text: str, camera_number: str, *,
                         image: bytes | None = None,
                         video: bytes | None = None,
                         category: str | None = None,
                         depends_on: str | None = None,
                         priority: int = PRIORITY_AMBIENT) -> str | None:
        """Queue one saveSnapshot — a still (`image`) or an incident clip
        (`video`), exactly the two shapes the endpoint takes. One media part per
        job, which is how the API is already called and what lets the window
        account for bytes precisely."""
        if image and video:
            raise ValueError("outbox: queue the still and the clip separately")
        part = "image" if image else "video" if video else None
        if part is None:
            return None
        return self.enqueue(
            "saveSnapshot",
            {"patient_id": patient_id, "text": text,
             "camera_number": camera_number, "category": category},
            label=text, priority=priority, blob=image or video,
            blob_part=part, blob_ext="jpg" if part == "image" else "mp4",
            depends_on=depends_on)

    # ---- read --------------------------------------------------------
    @staticmethod
    def _row(row: tuple | None) -> dict | None:
        if row is None:
            return None
        job = dict(zip(_COLS, row))
        try:
            job["payload"] = json.loads(job["payload"])
        except (TypeError, ValueError):
            job["payload"] = {}
        return job

    def head(self) -> dict | None:
        """The oldest pending job. STRICT FIFO: the sender always looks at this
        one and never reaches past it, so delivery order is exactly creation
        order. A job that is backing off holds the line on purpose — during an
        outage nothing behind it would have succeeded either."""
        rows = self._store.fetchall(
            "SELECT " + ", ".join(_COLS) +
            " FROM outbox WHERE state=? ORDER BY seq LIMIT 1", (STATE_PENDING,))
        return self._row(rows[0] if rows else None)

    def job(self, job_id: str) -> dict | None:
        rows = self._store.fetchall(
            "SELECT " + ", ".join(_COLS) + " FROM outbox WHERE job_id=?",
            (job_id,))
        return self._row(rows[0] if rows else None)

    def blob(self, job: dict) -> bytes | None:
        """The spooled media body for a job, or None when it has none (or the
        file vanished — the job is then sent without it rather than stalling)."""
        if not job.get("blob_path"):
            return None
        f = self._spool / job["blob_path"]
        try:
            return f.read_bytes()
        except OSError:
            logger.warning("outbox: spooled media missing for %s", job["job_id"])
            return None

    def stats(self) -> dict:
        """The queue at a glance — what the status surface and the sync console
        render, and what makes an outage visible while it is happening."""
        out = {"pending": 0, "sent": 0, "dropped": 0, "pending_bytes": 0,
               "oldest_pending_at": None, "oldest_pending_age_secs": None,
               "attempts_on_head": 0, "last_error": None,
               "window_hours": round(settings.outbox_window_secs / 3600.0, 1),
               "max_items": settings.outbox_max_items}
        try:
            for state, n, nbytes in self._store.fetchall(
                    "SELECT state, COUNT(*), COALESCE(SUM(blob_bytes),0) "
                    "FROM outbox GROUP BY state"):
                if state == STATE_PENDING:
                    out["pending"] = n
                    out["pending_bytes"] = int(nbytes)
                elif state == STATE_DONE:
                    out["sent"] = n
                elif state == STATE_DEAD:
                    out["dropped"] = n
            head = self.head()
            if head:
                out["oldest_pending_at"] = head["created_at"]
                out["oldest_pending_age_secs"] = round(
                    max(0.0, time.time() - head["created_epoch"]), 1)
                out["attempts_on_head"] = head["attempts"]
                out["last_error"] = head["last_error"]
        except Exception:
            logger.exception("outbox: stats failed")
        return out

    def recent(self, limit: int = 50) -> list[dict]:
        """Newest-first job list for the console: what is waiting, what went out
        and what the window discarded."""
        cols = ("seq", "job_id", "kind", "label", "priority", "state",
                "created_at", "blob_part", "blob_bytes", "depends_on",
                "attempts", "last_error", "sent_at", "result_id")
        rows = self._store.fetchall(
            "SELECT " + ", ".join(cols) + " FROM outbox ORDER BY seq DESC "
            "LIMIT ?", (max(int(limit), 1),))
        return [dict(zip(cols, r)) for r in rows]

    # ---- state transitions -------------------------------------------
    def mark_sent(self, job_id: str, result_id: int | None = None) -> None:
        """Delivered. The media body is released immediately — the row stays a
        while as the receipt, the bytes do not."""
        self._release_blob(job_id)
        self._store.execute(
            "UPDATE outbox SET state=?, sent_at=?, last_error=NULL, result_id=? "
            "WHERE job_id=?",
            (STATE_DONE, clock.now_iso(), result_id, job_id))

    def mark_retry(self, job_id: str, error: str, next_attempt: float) -> None:
        """Still pending, try again at `next_attempt` (epoch seconds)."""
        self._store.execute(
            "UPDATE outbox SET attempts=attempts+1, next_attempt=?, last_error=? "
            "WHERE job_id=?",
            (float(next_attempt), (error or "")[:300], job_id))

    def mark_dead(self, job_id: str, error: str) -> None:
        """Given up on — the server rejected it outright, or it exhausted its
        attempts. Dropping it is what stops one poisoned upload from blocking
        every good one behind it."""
        job = self.job(job_id)
        self._release_blob(job_id)
        self._store.execute(
            "UPDATE outbox SET state=?, last_error=? WHERE job_id=?",
            (STATE_DEAD, (error or "")[:300], job_id))
        if job and self._on_drop:
            try:
                self._on_drop(job, error)
            except Exception:
                logger.exception("outbox: drop callback failed")

    def _release_blob(self, job_id: str) -> None:
        rows = self._store.fetchall(
            "SELECT blob_path FROM outbox WHERE job_id=?", (job_id,))
        if rows and rows[0][0]:
            self._unlink(rows[0][0])
            self._store.execute(
                "UPDATE outbox SET blob_path=NULL WHERE job_id=?", (job_id,))

    def _unlink(self, rel: str) -> None:
        try:
            (self._spool / rel).unlink()
        except OSError:
            pass

    # ---- the sliding window ------------------------------------------
    def trim(self) -> None:
        """Hold the queue inside its window. Runs on every enqueue (cheap: three
        indexed statements on a table that is normally empty) so the caps are
        enforced continuously rather than at some later sweep."""
        try:
            self._expire_old()
            self._enforce_caps()
            self._prune_history()
        except Exception:
            logger.exception("outbox: trim failed")

    def _expire_old(self) -> None:
        window = max(60.0, settings.outbox_window_secs)
        span = (f"{window / 3600:.0f}h" if window >= 3600
                else f"{window / 60:.0f} min")
        rows = self._store.fetchall(
            "SELECT job_id FROM outbox WHERE state=? AND created_epoch<?",
            (STATE_PENDING, time.time() - window))
        for (job_id,) in rows:
            self.mark_dead(job_id,
                           f"dropped — older than the {span} upload window")

    def _enforce_caps(self) -> None:
        """Count and byte caps. Both evict in the same order — lowest priority
        first, oldest first — so ambient snapshots are surrendered long before
        an alert is."""
        max_bytes = settings.outbox_max_blob_mb * 1024 * 1024
        while True:
            rows = self._store.fetchall(
                "SELECT COUNT(*), COALESCE(SUM(blob_bytes),0) FROM outbox "
                "WHERE state=?", (STATE_PENDING,))
            count, nbytes = (rows[0] if rows else (0, 0))
            over_count = count - settings.outbox_max_items
            over_bytes = nbytes - max_bytes
            if over_count <= 0 and over_bytes <= 0:
                return
            victim = self._pick_victim()
            if not victim:
                return
            reason = (f"dropped — upload queue full ({count} waiting, cap "
                      f"{settings.outbox_max_items})" if over_count > 0 else
                      f"dropped — upload spool full "
                      f"({nbytes / 1e6:.0f} MB, cap {settings.outbox_max_blob_mb:.0f} MB)")
            self.mark_dead(victim, reason)

    def _pick_victim(self) -> str | None:
        """What a full queue gives up: the lowest-priority, oldest job that
        nothing else is waiting on. The dependency clause matters — evicting an
        alert while keeping its snapshot would deliver a photo of a fall with no
        fall attached. Only if every candidate is depended upon does it fall
        back to plain priority/age order."""
        order = "ORDER BY priority ASC, seq ASC LIMIT 1"
        for clause in ("AND job_id NOT IN (SELECT depends_on FROM outbox "
                       "WHERE state=? AND depends_on IS NOT NULL) ", ""):
            params = (STATE_PENDING, STATE_PENDING) if clause else (STATE_PENDING,)
            rows = self._store.fetchall(
                f"SELECT job_id FROM outbox WHERE state=? {clause}{order}",
                params)
            if rows:
                return rows[0][0]
        return None

    def _prune_history(self) -> None:
        """Forget finished jobs once they are older than the console needs them.
        A `done` row a pending job still depends on is kept regardless — it is
        carrying that job's alertId."""
        cutoff = time.time() - max(60.0, settings.outbox_history_secs)
        self._store.execute(
            "DELETE FROM outbox WHERE state IN (?,?) AND created_epoch<? "
            "AND job_id NOT IN (SELECT depends_on FROM outbox "
            "                   WHERE state=? AND depends_on IS NOT NULL)",
            (STATE_DONE, STATE_DEAD, cutoff, STATE_PENDING))
