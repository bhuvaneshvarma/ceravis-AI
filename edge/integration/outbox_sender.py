from __future__ import annotations

"""
The one thread that talks to the app server on the event path.

It walks the outbox from the head and delivers one job at a time. Single
consumer, so ordering is decided entirely by the queue: highest priority first,
oldest first within a tier.

That means a FALL leaves the device before anything else in the queue, however
much ambient traffic was raised ahead of it — and within the incident, the
alert, the still that shows it and the clip that proves it still arrive in the
order they happened, because they share a tier.

WHAT HAPPENS WHEN THE LINK IS DOWN
    A send fails with a transport error -> the job stays at the head, its
    attempt count goes up and it is retried on a backoff capped at
    `outbox_backoff_max_secs`. Nothing behind it is skipped (nothing behind it
    would succeed either). Meanwhile the rest of the device is untouched:
    cameras, AI, recording and LAN live view never knew there was an internet.
    When the link returns, the very next attempt succeeds and the queue drains
    back-to-back, oldest first, until it is empty.

WHAT IS *NOT* RETRIED
    A 4xx that is not 408/425/429 is the server saying "this request is wrong",
    and it will be just as wrong in an hour. Those are marked dead immediately
    and the queue moves on — retrying them forever would block every good
    upload behind them, which is the classic way a durable queue turns into a
    stalled one.

ALERT LINKAGE ACROSS AN OUTAGE
    A snapshot belongs to an alert by `alertId`, which only exists after the
    server has accepted the alert. Offline, there is no alertId — so the
    publisher links the snapshot to the alert's local job_id (`depends_on`) and
    this sender substitutes the real alertId at delivery time, once the alert
    ahead of it in the queue has landed. The link therefore survives a week
    offline. If that alert was itself given up on, the snapshot still goes out,
    unlinked — a fall photo with no alert row beats no fall photo.

Delivery is AT LEAST ONCE: a reply lost after the server committed will be
retried and can duplicate. Making it exactly-once needs an idempotency key the
backend honours; until then the duplicate is the safe failure direction.
"""

import logging
import random
import threading
import time

from config.settings import settings
from integration import call_log
from integration.ceravis_api import (
    CeravisApiError, alert_id_of, is_configured, save_alert, save_snapshot,
)
from storage.outbox_store import PRIORITY_ALERT, PRIORITY_AMBIENT, OutboxStore


logger = logging.getLogger("outbox")

# HTTP statuses worth another attempt: the request was fine, the server or the
# path to it was not.
_RETRY_STATUSES = {408, 425, 429}


def _retriable(exc: CeravisApiError) -> bool:
    status = getattr(exc, "status", None)
    if status is None:
        return True                     # transport: DNS, refused, timeout, TLS
    return status >= 500 or status in _RETRY_STATUSES


class OutboxSender:
    """Drains OutboxStore -> the CERAVIS app server, urgent-first, forever."""

    def __init__(self, outbox: OutboxStore) -> None:
        self._outbox = outbox
        self._outbox.set_drop_listener(self._log_drop)
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        # Was the last attempt a failure? Reported once, not once per retry.
        self._offline = False
        self._trimmed_at = 0.0

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="cloud-outbox")
        self._thread.start()
        depth = self._outbox.stats()["pending"]
        logger.info("Cloud outbox on — %d upload(s) waiting, %.0fh window, "
                    "cap %d", depth, settings.outbox_window_secs / 3600.0,
                    settings.outbox_max_items)

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- what producers call -----------------------------------------
    # Queue, then wake the loop, so an upload on a healthy link goes out in the
    # same breath it was raised — the queue adds durability, not latency.
    def queue_alert(self, patient_id, alert_type: str, message: str, *,
                    priority: int = PRIORITY_ALERT) -> str | None:
        """Queue one saveAlert; the returned job_id is what its snapshots link
        to until the server issues the real alertId."""
        job_id = self._outbox.enqueue_alert(patient_id, alert_type, message,
                                            priority=priority)
        self._queued("saveAlert", job_id, f"{alert_type} · {message}")
        return job_id

    def queue_snapshot(self, patient_id, text: str, camera_number: str, *,
                       image: bytes | None = None, video: bytes | None = None,
                       category: str | None = None,
                       depends_on: str | None = None,
                       priority: int = PRIORITY_AMBIENT) -> str | None:
        """Queue one saveSnapshot — a still or an incident clip."""
        job_id = self._outbox.enqueue_snapshot(
            patient_id, text, camera_number, image=image, video=video,
            category=category, depends_on=depends_on, priority=priority)
        self._queued("saveSnapshot", job_id, text)
        return job_id

    def _queued(self, kind: str, job_id: str | None, label: str) -> None:
        if job_id is None:
            return
        # The console shows the upload the instant the event happens, before the
        # server has seen anything — during an outage that QUEUED line is the
        # proof the detection was captured and is waiting, not lost.
        call_log.record(kind, True, label=label, direction="out", state="queued")
        self._wake.set()

    # ---- loop --------------------------------------------------------
    def _run(self) -> None:
        while self._running:
            wait = settings.outbox_poll_secs
            try:
                wait = self._tick()
            except Exception:
                logger.exception("outbox: sender tick failed")
            if wait <= 0:
                continue                 # a backlog drains back-to-back
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def _tick(self) -> float:
        """Deliver the head job if it is due. Returns how long to wait before
        looking again; 0 means there is more to send right now."""
        self._trim_periodically()
        if not is_configured():
            return 5.0
        job = self._outbox.head()
        if job is None:
            return settings.outbox_poll_secs
        due_in = job["next_attempt"] - time.time()
        if due_in > 0:
            return min(due_in, settings.outbox_poll_secs)
        self._deliver(job)
        return 0.0                       # keep draining while there is work

    def _trim_periodically(self) -> None:
        """The window is normally enforced as jobs are queued. During a long
        outage nothing new may be queued for hours, so the sender re-applies it
        on a slow beat — otherwise a backlog could outlive its own window and
        then flood the server with stale uploads on reconnect."""
        now = time.monotonic()
        if now - self._trimmed_at < 60.0:
            return
        self._trimmed_at = now
        self._outbox.trim()

    def _deliver(self, job: dict) -> None:
        # While the link is known to be down, the API client's own per-call
        # console record is silenced: the first failure was already reported and
        # the queue's depth carries the rest.
        call_log.quiet_retries(self._offline)
        try:
            result_id = self._send(job)
        except CeravisApiError as exc:
            self._failed(job, exc)
            return
        except Exception as exc:
            # A bug in our own code is not the server's fault and will repeat —
            # drop the job so the queue keeps moving, loudly.
            logger.exception("outbox: %s job raised", job["kind"])
            self._outbox.mark_dead(job["job_id"], f"internal error: {exc}")
            return
        self._outbox.mark_sent(job["job_id"], result_id)
        if self._offline:
            logger.info("outbox: link restored — draining %d queued upload(s)",
                        self._outbox.stats()["pending"])
            self._offline = False

    def _send(self, job: dict) -> int | None:
        payload = job["payload"]
        if job["kind"] == "saveAlert":
            resp = save_alert(payload["patient_id"], payload["alert_type"],
                              payload["message"])
            return alert_id_of(resp)
        if job["kind"] == "saveSnapshot":
            media = self._outbox.blob(job)
            part = job["blob_part"]
            if not media:
                # The spooled file is gone (disk wipe, manual cleanup). Retrying
                # cannot bring it back, so this is a permanent failure — 410, so
                # the classifier drops it instead of looping on it.
                raise CeravisApiError("media body missing from the spool",
                                      status=410)
            save_snapshot(
                payload["patient_id"], payload.get("text") or "",
                payload.get("camera_number") or "",
                image=media if part == "image" else None,
                video=media if part == "video" else None,
                alert_id=self._alert_id_for(job),
                category=payload.get("category"))
            return None
        raise CeravisApiError(f"unknown outbox job kind {job['kind']!r}")

    def _alert_id_for(self, job: dict):
        """The server-issued alertId this snapshot belongs to, resolved from the
        alert job it was queued against. None when there is no parent, the
        parent was dropped, or the server returned no id — the snapshot is sent
        unlinked rather than held back."""
        explicit = job["payload"].get("alert_id")
        if explicit is not None:
            return explicit
        parent_id = job.get("depends_on")
        if not parent_id:
            return None
        parent = self._outbox.job(parent_id)
        return parent["result_id"] if parent else None

    def _failed(self, job: dict, exc: CeravisApiError) -> None:
        attempts = job["attempts"] + 1
        status = getattr(exc, "status", None)
        if not _retriable(exc):
            self._outbox.mark_dead(job["job_id"], f"rejected: {exc}")
            return
        if attempts >= settings.outbox_max_attempts:
            self._outbox.mark_dead(
                job["job_id"],
                f"gave up after {attempts} attempts: {exc}")
            return
        delay = min(settings.outbox_backoff_base_secs * (2 ** (attempts - 1)),
                    settings.outbox_backoff_max_secs)
        delay *= random.uniform(0.8, 1.2)   # jitter: a fleet must not sync up
        self._outbox.mark_retry(job["job_id"], str(exc), time.time() + delay)
        if not self._offline:
            logger.warning("outbox: cloud unreachable (%s) — %s queued, "
                           "retrying every <=%.0fs until it is back",
                           status or "no response", job["kind"],
                           settings.outbox_backoff_max_secs)
            self._offline = True

    # ---- console -----------------------------------------------------
    def _log_drop(self, job: dict, reason: str) -> None:
        """A discarded upload is news: it is the only case where an event the
        device detected never reaches the cloud, so it lands on the same sync
        console as every call, with the reason."""
        call_log.record(job["kind"], False, label=job.get("label"),
                        direction="out", state="dropped", error=reason)
        logger.warning("outbox: dropped %s (%s) — %s", job["kind"],
                       job.get("label", "")[:80], reason)
