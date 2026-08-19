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
  * Order is clinical information. A fall, its still and its clip must reach
    the server in the order they happened. The queue is FIFO by `seq` WITHIN a
    priority tier, and a fall outranks every tier — so an incident is never
    scrambled, and a fall raised behind an hour of posture snapshots still
    leaves the device first.
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

RECLAIMING THE DISK
  The bytes are the media, and a job's media is deleted THE MOMENT ITS CALL
  SUCCEEDS (mark_sent) — and equally when it is given up on, so nothing is held
  by a job that will never be sent. What survives is the row, which is a receipt
  for the sync console, capped by both age (`outbox_history_secs`) and count.
  Steady state on a delivering device is therefore an empty spool directory and
  a table that does not grow. A slow orphan sweep backstops the one case the
  row-driven deletes cannot see: a crash between writing the file and
  committing the row that owns it.

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

# ONE priority scale, read from both ends, so urgency means the same thing
# everywhere:
#   delivery  takes the HIGHEST first — a fall leaves the device before anything
#             else, however long the ambient backlog in front of it is;
#   eviction  takes the LOWEST first — a full queue surrenders wallpaper, never
#             the emergency.
# Within one tier the order is strictly oldest-first, so a fall never overtakes
# an earlier fall and an incident's alert, still and clip stay in sequence.
PRIORITY_FALL = 3       # a fall alert and the still + clip that prove it
PRIORITY_ALERT = 2      # every other alert (no-motion) and its media
PRIORITY_AMBIENT = 1    # posture, room and dwell snapshots (nice to have)

STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_DEAD = "dead"

# Finished jobs are receipts for the sync console, not data. They are capped by
# age (outbox_history_secs) AND by count, so a busy day cannot grow the table
# without bound between age sweeps.
_HISTORY_MAX_ROWS = 500
# The orphan-media sweep walks a directory, so it runs on a slow beat rather
# than on every enqueue, and ignores anything written in the last few minutes
# (that is a job mid-enqueue, not an orphan).
_SPOOL_SWEEP_SECS = 600.0
_SPOOL_ORPHAN_GRACE_SECS = 300.0


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
CREATE INDEX IF NOT EXISTS idx_outbox_next ON outbox(state, priority DESC, seq);
CREATE INDEX IF NOT EXISTS idx_outbox_dep ON outbox(depends_on);
DROP INDEX IF EXISTS idx_outbox_state;
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
        self._swept_at = 0.0
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
        """Startup: report the backlog we inherited, and reclaim any spooled
        media the rows no longer account for."""
        pending = self.stats()["pending"]
        if pending:
            logger.info("outbox: %d upload(s) waiting from a previous run",
                        pending)
        self._sweep_spool(force=True)

    def _sweep_spool(self, force: bool = False) -> None:
        """Delete spooled media no row points at any more.

        A job's media is released the moment it is delivered (or given up on),
        so in normal running this finds nothing — it exists for the case nothing
        else covers: a crash between writing the file and committing its row,
        which would otherwise leave bytes on disk that no code will ever look at
        again. Rate-limited because it walks the directory.

        Only files older than the grace period are touched. A job being queued
        right now has written its bytes and not yet committed its row, so to
        this sweep it is indistinguishable from an orphan — and deleting a fall
        clip a millisecond before its row lands would be far worse than leaving
        a few stale bytes for one more pass."""
        now = time.monotonic()
        if not force and now - self._swept_at < _SPOOL_SWEEP_SECS:
            return
        self._swept_at = now
        known = {row[0] for row in self._store.fetchall(
            "SELECT blob_path FROM outbox WHERE blob_path IS NOT NULL")}
        settled = time.time() - _SPOOL_ORPHAN_GRACE_SECS
        freed = files = 0
        for f in self._spool.glob("*"):
            if not f.is_file() or f.name in known:
                continue
            try:
                stat = f.stat()
                if stat.st_mtime > settled:
                    continue                 # still being queued — leave it
                f.unlink()
                freed += stat.st_size
                files += 1
            except OSError:
                pass
        if files:
            logger.info("outbox: reclaimed %d orphaned media file(s), %.1f MB",
                        files, freed / 1e6)

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
        priority = self._capped_priority(priority, depends_on)
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

    def _capped_priority(self, priority: int, depends_on: str | None) -> int:
        """A job never outranks the job it depends on.

        Delivery is priority-first, so a snapshot that outranked its own alert
        would be sent before the server had issued the alertId to link it to.
        Clamping here makes that impossible by construction instead of relying
        on every caller passing matching priorities."""
        if not depends_on:
            return int(priority)
        rows = self._store.fetchall(
            "SELECT priority FROM outbox WHERE job_id=?", (depends_on,))
        return min(int(priority), int(rows[0][0])) if rows else int(priority)

    # ---- domain wrappers ---------------------------------------------
    def enqueue_alert(self, patient_id, alert_type: str, message: str,
                      *, label: str = "",
                      priority: int = PRIORITY_ALERT) -> str | None:
        """Queue one saveAlert. Its job_id is the local stand-in for the
        alertId the server has not issued yet: snapshots that belong to this
        alert pass it as `depends_on`, and the sender substitutes the real
        alertId once the alert lands."""
        return self.enqueue(
            "saveAlert",
            {"patient_id": patient_id, "alert_type": alert_type,
             "message": message},
            label=label or f"{alert_type} · {message}",
            priority=priority)

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
        """The next job to send: highest priority, then oldest.

        The sender looks at this one and never reaches past it. So within a
        tier delivery order is exactly creation order — an incident's alert,
        still and clip stay in sequence, and an earlier fall is never overtaken
        by a later one — while a fall raised during a backlog of ambient
        snapshots goes out FIRST, not after them.

        A job that is backing off holds the line on purpose: during an outage
        nothing behind it would have succeeded either. But a fall queued while
        an ambient job is mid-backoff becomes the head immediately and is sent
        at once, because it is due and it outranks what is waiting.

        Starvation is bounded by the age window rather than by a fairness rule:
        an ambient snapshot that never gets its turn expires at
        `outbox_window_secs`, and by then it was worthless anyway. Continuous
        falls for a whole day is not a queueing problem."""
        rows = self._store.fetchall(
            "SELECT " + ", ".join(_COLS) +
            " FROM outbox WHERE state=? ORDER BY priority DESC, seq ASC LIMIT 1",
            (STATE_PENDING,))
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
               "pending_alerts": 0, "next_priority": None,
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
            # Two different questions, deliberately answered separately now that
            # delivery is priority-ordered: `oldest_*` is how far the BACKLOG
            # stretches (what says "we are offline"), while the head is simply
            # what goes next — under priority ordering a fresh fall, not the
            # oldest row.
            rows = self._store.fetchall(
                "SELECT COUNT(*) FROM outbox WHERE state=? AND priority>=?",
                (STATE_PENDING, PRIORITY_ALERT))
            out["pending_alerts"] = rows[0][0] if rows else 0
            rows = self._store.fetchall(
                "SELECT created_at, created_epoch FROM outbox WHERE state=? "
                "ORDER BY created_epoch ASC LIMIT 1", (STATE_PENDING,))
            if rows:
                out["oldest_pending_at"] = rows[0][0]
                out["oldest_pending_age_secs"] = round(
                    max(0.0, time.time() - rows[0][1]), 1)
            head = self.head()
            if head:
                out["next_priority"] = head["priority"]
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
        """Hold the queue inside its window and give the disk back.

        Runs on every enqueue (cheap: a few indexed statements on a table that
        is normally empty) and on a slow beat from the sender, so the caps are
        enforced continuously rather than at some later sweep. Nothing here
        touches a pending job's media — that is released the instant its call
        succeeds, in mark_sent."""
        try:
            self._expire_old()
            self._enforce_caps()
            self._prune_history()
            self._sweep_spool()
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

    # A finished job is deletable unless a job still waiting to be sent depends
    # on it — that row is carrying the alertId its snapshots have not used yet.
    _DELETABLE = ("state IN (?,?) AND job_id NOT IN "
                  "(SELECT depends_on FROM outbox "
                  " WHERE state=? AND depends_on IS NOT NULL)")

    def _prune_history(self) -> None:
        """Forget finished jobs — by age first, then by count.

        Their media is already gone (released on delivery); this is the row
        itself, kept only so the sync console can show what happened. The count
        cap is the backstop: a device that raises thousands of events inside one
        history window would otherwise carry every receipt until the window
        rolled."""
        cutoff = time.time() - max(60.0, settings.outbox_history_secs)
        base = (STATE_DONE, STATE_DEAD, STATE_PENDING)
        self._store.execute(
            f"DELETE FROM outbox WHERE {self._DELETABLE} AND created_epoch<?",
            base + (cutoff,))
        self._store.execute(
            f"DELETE FROM outbox WHERE {self._DELETABLE} AND seq NOT IN "
            f"(SELECT seq FROM outbox WHERE {self._DELETABLE} "
            f" ORDER BY seq DESC LIMIT ?)",
            base + base + (_HISTORY_MAX_ROWS,))
