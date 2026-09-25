from __future__ import annotations

"""
The cloud outbox — a durable, ordered, bounded queue of app-server uploads.

Everything the device wants to push to app.ceravishealth.in as a RESULT OF AN
EVENT (saveAlert, and the saveSnapshot stills + fall clips that belong to it)
is handed here and sent from here. There is no second path: the publisher
never calls the API directly, so "the alert fired" and "the alert was
delivered" are one mechanism with two states instead of two mechanisms that can
disagree. On a healthy network the queue is drained within milliseconds and is
invisible; during an outage it is the thing that keeps the incident.

WRITE-AHEAD, ZERO-COPY (2026-09-25)
  The industry store-and-forward shape (OpenTelemetry's persistent sending
  queue, Fluent Bit's filesystem buffer, Android WorkManager): every upload is
  ONE small SQLite row the moment it is raised, and the sender works from the
  rows. A crash or power cut can therefore lose nothing, not even an upload in
  its first second.
  The media is never copied to get there. An event still already lives on disk
  (the enricher wrote it, EventStore's retention owns it), so its row simply
  REFERENCES that file (`blob_owned = 0`) and the sender reads it once, at send
  time. Only bytes that have no home of their own — a fall clip merged in a temp
  dir — are written to the spool, once, and that file belongs to the queue
  (`blob_owned = 1`) and is deleted the moment its job leaves the queue.
  This replaced a RAM tier (send from memory, spill on failure) that held every
  still in RAM, copied it to the spool on any wait, and needed ~300 lines of
  two-tier bookkeeping to keep ordering and alert linkage exact across it.

Why a queue and not a retry loop:
  * The whole system keeps working offline (streams, AI, recording, LAN live
    view). Only the cloud hop is down, and it comes back — so an alert must
    survive the gap, not be logged-and-lost.
  * Order is clinical information. A fall, its still and its clip must reach
    the server in the order they happened. The queue is FIFO by `seq` WITHIN a
    priority tier, and a fall outranks every tier — so an incident is never
    scrambled, and a fall raised behind an hour of posture snapshots still
    leaves the device first.

SLIDING WINDOW
  The queue is deliberately NOT unbounded — a device offline for a week must not
  fill its disk or, when it reconnects, flood the server with a week of stale
  ambient snapshots. It holds a moving window over the recent past:

    age    : a pending job older than `outbox_window_secs` is dropped.
    count  : at most `outbox_max_items` pending jobs.
    bytes  : at most `outbox_max_blob_mb` of media the queue itself spooled
             (a referenced still costs no extra disk, so it does not count).

  When the count/byte caps bite, eviction is LOWEST PRIORITY FIRST, oldest
  first — the ambient posture/room snapshots go, the FALL and NO_MOTION alerts
  and their media stay. A window that drops the emergency to keep the wallpaper
  would be worse than no window at all.

RECLAIMING THE DISK
  A job's spooled media is deleted THE MOMENT ITS CALL SUCCEEDS (mark_sent) —
  and equally when it is given up on. A referenced still is never deleted here:
  it is the event's own record. What survives is the row, a receipt for the
  sync console, capped by both age (`outbox_history_secs`) and count. A slow
  orphan sweep backstops the one case the row-driven deletes cannot see: a crash
  between spooling a clip and committing the row that owns it.

LAYOUT
  outbox rows   -> data/ceravis.db, table `outbox`
  spooled media -> data/outbox/<job_id>.<ext> (clips handed over as bytes)

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
    result_id     INTEGER,
    blob_owned    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_outbox_next ON outbox(state, priority DESC, seq);
CREATE INDEX IF NOT EXISTS idx_outbox_dep ON outbox(depends_on);
DROP INDEX IF EXISTS idx_outbox_state;
"""

_COLS = ("seq", "job_id", "kind", "label", "priority", "state", "created_at",
         "created_epoch", "payload", "blob_path", "blob_part", "blob_bytes",
         "depends_on", "attempts", "next_attempt", "last_error", "sent_at",
         "result_id", "blob_owned")


class OutboxStore:
    """The durable FIFO itself. Thread-safe by way of SqliteStore's lock: the
    event thread enqueues while the sender thread drains, and each statement is
    atomic. The two sender lanes read disjoint priority windows, so no row-level
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
        # A table from before zero-copy has no blob_owned column: every row it
        # holds spooled its own bytes, which is exactly the default of 1.
        cols = {row[1] for row in self._store.fetchall("PRAGMA table_info(outbox)")}
        if "blob_owned" not in cols:
            self._store.execute(
                "ALTER TABLE outbox ADD COLUMN blob_owned INTEGER NOT NULL DEFAULT 1")
        self._spool = self._spool_dir()
        self._swept_at = 0.0
        # A "needs attention" note the sender raises when the server rejects an
        # upload with a code that usually means a human must act (bad API key,
        # wrong patient, payload too large). In-memory: it re-derives within one
        # retry cycle after a restart, and clears the moment a delivery succeeds.
        # None = nothing to look at. Surfaced through stats() so it reaches the
        # console and /system/status without a second channel.
        self._attention: dict | None = None
        self._recover()

    def set_drop_listener(self,
                          on_drop: Callable[[dict, str], None] | None) -> None:
        """Register who hears about discarded uploads (the sender wires the sync
        console). One listener: a dropped upload is announced once."""
        self._on_drop = on_drop

    # ---- needs-attention signal --------------------------------------
    def flag_attention(self, code, reason: str, label: str = "") -> None:
        """Raise the needs-attention note — the server is rejecting uploads with
        a code a human should look at. Idempotent while the same code persists,
        so it does not churn; the timestamp marks when it first appeared."""
        if self._attention and self._attention.get("code") == code:
            return
        self._attention = {"code": code, "reason": (reason or "")[:200],
                           "label": (label or "")[:120], "since": clock.now_iso()}
        logger.warning("outbox: NEEDS ATTENTION — server rejecting uploads "
                       "(HTTP %s): %s", code, reason)

    def clear_attention(self) -> None:
        """A delivery succeeded, so whatever the server was rejecting it now
        accepts — the config is good again. Clear the note."""
        if self._attention is not None:
            logger.info("outbox: needs-attention cleared — uploads accepted again")
            self._attention = None

    # ---- paths -------------------------------------------------------
    @staticmethod
    def _spool_dir() -> Path:
        base = settings.data_path
        base = base if base.is_absolute() else (_EDGE_ROOT / base)
        spool = base / "outbox"
        spool.mkdir(parents=True, exist_ok=True)
        return spool

    def _media_path(self, job: dict) -> Path | None:
        """Where a job's media body is: its own spool file, or the file it
        references in place."""
        if not job.get("blob_path"):
            return None
        return (self._spool / job["blob_path"] if job.get("blob_owned", 1)
                else Path(job["blob_path"]))

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
            "SELECT blob_path FROM outbox WHERE blob_path IS NOT NULL AND blob_owned=1")}
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
                blob_file: str | Path | None = None,
                blob_part: str | None = None, blob_ext: str = "bin",
                depends_on: str | None = None) -> str | None:
        """Append one upload to the tail of the queue — one row, written now.
        Returns its job_id (the handle a dependent job uses as `depends_on`), or
        None if it could not be persisted (then nothing was queued and the
        caller has lost nothing it had).

        Media is either `blob_file` — an existing file, referenced in place and
        never copied or deleted here — or `blob` bytes, spooled once to a file
        the queue owns."""
        job_id = uuid.uuid4().hex
        priority = self._capped_priority(priority, depends_on)
        media, size, owned = None, 0, 1
        try:
            if blob_file is not None:
                path = Path(blob_file).resolve()
                media, size, owned = str(path), path.stat().st_size, 0
            elif blob:
                f = self._spool / f"{job_id}.{blob_ext}"
                f.write_bytes(blob)
                media, size = f.name, len(blob)
            self._store.execute(
                """INSERT INTO outbox
                   (job_id, kind, label, priority, state, created_at,
                    created_epoch, payload, blob_path, blob_part, blob_bytes,
                    depends_on, blob_owned)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, kind, (label or "")[:300], int(priority), STATE_PENDING,
                 clock.now_iso(), time.time(), json.dumps(payload, default=str),
                 media, blob_part if media else None, size, depends_on, owned))
        except Exception:
            logger.exception("outbox: enqueue failed (%s)", kind)
            if media and owned:
                self._unlink(media)
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
                         image_path: str | Path | None = None,
                         category: str | None = None,
                         depends_on: str | None = None,
                         priority: int = PRIORITY_AMBIENT) -> str | None:
        """Queue one saveSnapshot — a still or an incident clip, exactly the two
        shapes the endpoint takes, one media part per job. A still already on
        disk is passed as `image_path` and referenced, not copied."""
        if sum(x is not None for x in (image, video, image_path)) > 1:
            raise ValueError("outbox: one media part per snapshot job")
        part = "video" if video else "image" if (image or image_path) else None
        if part is None:
            return None
        return self.enqueue(
            "saveSnapshot",
            {"patient_id": patient_id, "text": text,
             "camera_number": camera_number, "category": category},
            label=text, priority=priority, blob=image or video,
            blob_file=image_path, blob_part=part,
            blob_ext="jpg" if part == "image" else "mp4", depends_on=depends_on)

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
        """The highest-priority, oldest pending job — for display/stats only.

        This is "what is first in line" regardless of whether it is due yet, so
        the console can show the head of the backlog. It is NOT what the sender
        delivers (that is next_ready): a job mid-backoff is still the head here,
        but the sender steps around it so it does not block the ones behind."""
        rows = self._store.fetchall(
            "SELECT " + ", ".join(_COLS) +
            " FROM outbox WHERE state=? ORDER BY priority DESC, seq ASC LIMIT 1",
            (STATE_PENDING,))
        return self._row(rows[0] if rows else None)

    # A snapshot must not be delivered before the alert it belongs to: its
    # server-issued alertId only exists once that alert has landed. So a job is
    # eligible only when the job it depends on is no longer PENDING — delivered,
    # given up on, or already pruned. Encoded once, used by both the picker and
    # the "when is the next one due" clock so they never disagree.
    _DEP_READY = ("(depends_on IS NULL OR depends_on NOT IN "
                  "(SELECT job_id FROM outbox WHERE state=?))")

    @staticmethod
    def _priority_clause(min_priority, max_priority) -> tuple[str, list]:
        """Optional priority window, so a caller can ask for only part of the
        queue. The delivery lanes use DISJOINT windows, which is what lets two
        senders share one queue with no claim table and no locking: they can
        never select the same row."""
        sql, params = "", []
        if min_priority is not None:
            sql += " AND priority>=?"
            params.append(int(min_priority))
        if max_priority is not None:
            sql += " AND priority<=?"
            params.append(int(max_priority))
        return sql, params

    def next_ready(self, now: float | None = None, *,
                   min_priority: int | None = None,
                   max_priority: int | None = None) -> dict | None:
        """The next job to actually SEND: highest priority, oldest, that is DUE
        (its backoff has elapsed) and whose alert dependency is satisfied.

        This is what gives the queue its "step around a stuck job" behaviour. A
        job that is failing sits in the future (next_attempt), so it is not due,
        so the sender skips past it to whatever IS ready — a broken snapshot can
        never block the fall alert queued behind it. Priority still decides among
        the due jobs, and seq breaks ties, so an incident stays in order and a
        fall still goes first."""
        now = time.time() if now is None else now
        clause, extra = self._priority_clause(min_priority, max_priority)
        rows = self._store.fetchall(
            "SELECT " + ", ".join(_COLS) + " FROM outbox "
            f"WHERE state=? AND next_attempt<=? AND {self._DEP_READY}{clause} "
            "ORDER BY priority DESC, seq ASC LIMIT 1",
            (STATE_PENDING, now, STATE_PENDING, *extra))
        return self._row(rows[0] if rows else None)

    def next_due_at(self, *, min_priority: int | None = None,
                    max_priority: int | None = None) -> float | None:
        """The earliest time any eligible pending job becomes due, so the sender
        can sleep exactly until then instead of polling. None when the queue has
        no eligible pending job (empty, or everything is dependency-blocked
        behind a job that is itself counted here)."""
        clause, extra = self._priority_clause(min_priority, max_priority)
        rows = self._store.fetchall(
            "SELECT MIN(next_attempt) FROM outbox "
            f"WHERE state=? AND {self._DEP_READY}{clause}",
            (STATE_PENDING, STATE_PENDING, *extra))
        return rows[0][0] if rows and rows[0][0] is not None else None

    def wake_all(self) -> None:
        """Clear every pending job's backoff so they are all due NOW. Called when
        an external signal — the status heartbeat getting a clean response —
        reports the server reachable, so the queue drains at once instead of
        waiting out the retry timer."""
        self._store.execute(
            "UPDATE outbox SET next_attempt=0 WHERE state=? AND next_attempt>0",
            (STATE_PENDING,))

    def job(self, job_id: str) -> dict | None:
        rows = self._store.fetchall(
            "SELECT " + ", ".join(_COLS) + " FROM outbox WHERE job_id=?",
            (job_id,))
        return self._row(rows[0] if rows else None)

    def blob(self, job: dict) -> bytes | None:
        """The media body for a job, read now — or None when it has none, or the
        file vanished (the send then fails and retries; nothing is dropped)."""
        path = self._media_path(job)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            logger.warning("outbox: media missing for %s (%s)", job["job_id"], path)
            return None

    def stats(self) -> dict:
        """The queue at a glance — what the status surface and the sync console
        render, and what makes an outage visible while it is happening."""
        out = {"pending": 0, "sent": 0, "dropped": 0, "pending_bytes": 0,
               "pending_alerts": 0, "next_priority": None,
               "oldest_pending_at": None, "oldest_pending_age_secs": None,
               "attempts_on_head": 0, "last_error": None,
               "attention": self._attention,
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
        """Delivered. Spooled media is released immediately — the row stays a
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
        """Given up on — by the age window, a cap, or a kind this build no
        longer sends. Announced to the drop listener: the only case where an
        event the device detected never reaches the cloud."""
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
        """Let go of a job's media: delete a spooled file, merely forget a
        referenced one (it belongs to the event store)."""
        rows = self._store.fetchall(
            "SELECT blob_path, blob_owned FROM outbox WHERE job_id=?", (job_id,))
        if rows and rows[0][0]:
            if rows[0][1]:
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
        enforced continuously rather than at some later sweep."""
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
        an alert is. Bytes are only what the queue spooled itself."""
        max_bytes = settings.outbox_max_blob_mb * 1024 * 1024
        while True:
            rows = self._store.fetchall(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN blob_owned=1 "
                "THEN blob_bytes ELSE 0 END),0) FROM outbox WHERE state=?",
                (STATE_PENDING,))
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

        Their media is already released (on delivery); this is the row itself,
        kept only so the sync console can show what happened. The count cap is
        the backstop: a device that raises thousands of events inside one
        history window would otherwise carry every receipt until it rolled."""
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
