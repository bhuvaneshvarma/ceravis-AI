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

KEEP EVERYTHING UNTIL IT IS SENT
    A failed upload is NEVER dropped for the failure itself — not an outage, not
    a 5xx, not a 4xx. Every error just schedules another attempt. The ONLY two
    ways a job leaves the queue are a clean successful send, or reaching the
    outer age window (`outbox_window_secs`, 48h) — the single safety bound so a
    server that never comes back cannot fill the disk forever. Everything the
    device generated for an event — the alert, its still, its annotation, its
    clip, the alertId linkage — is preserved on disk until it has actually
    landed. (Under genuine disk pressure the capacity caps still shed the
    lowest-priority AMBIENT wallpaper first; a fall is never the victim.)

    This is deliberate for a safety product: "we gave up early" is the worst
    outcome. A 400 that looks permanent may be a server mid-maintenance (a DB
    swap), so the request that failed this minute can succeed the next.

STEP AROUND A STUCK JOB, DON'T STALL BEHIND IT
    Because nothing is dropped, a job that keeps failing must not block the ones
    behind it. A failing job sits in the future on its backoff, so it is not
    "due"; the sender delivers the next job that IS due (see next_ready), and the
    broken one is retried when its timer comes up. A snapshot the server keeps
    refusing can never hold up the fall alert queued behind it. Order is still
    kept where it matters: a snapshot waits for the alert it depends on, and a
    fall still outranks everything.

NEEDS ATTENTION, STILL NOT DROPPED
    A 401/403/404/413 usually means a real config problem (bad key, wrong
    patient, clip too big). Those keep retrying like everything else — but they
    also raise a loud needs-attention note on the console and /system/status, so
    a human fixes the cause while no data is lost in the meantime.

DRAINED BY THE HEARTBEAT
    The status heartbeat already probes the server every 60s. When it gets a
    clean response — the server is reachable again — it kicks this sender, which
    clears the backoff and drains the queue at once rather than waiting out the
    retry timer. The sender's own capped backoff is the fallback if the
    heartbeat is disabled.

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

# Server responses that usually mean a human must act (bad/expired API key,
# wrong patient id, payload too large). These are RETRIED like any other error —
# nothing is dropped — but they also raise the needs-attention note so the cause
# gets fixed instead of silently retried forever.
_ATTENTION_STATUSES = {401, 403, 404, 413}


class OutboxSender:
    """Drains OutboxStore -> the CERAVIS app server, urgent-first, forever."""

    def __init__(self, outbox: OutboxStore) -> None:
        self._outbox = outbox
        self._outbox.set_drop_listener(self._log_drop)
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        # Currently failing to deliver? Drives the once-per-transition logging
        # and the console-quiet-while-retrying behaviour. "offline" (no response)
        # and "rejecting" (server answered with an error) are tracked separately
        # so the log names which one it is.
        self._degraded = False
        self._problem: str | None = None    # None | "offline" | "rejecting"
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

    def kick(self) -> None:
        """The server is reachable — drain now. Wired to the status heartbeat:
        a clean 60s beat means the app server is answering, so clear every
        pending job's backoff and wake the loop, and the queue empties at once
        instead of each job waiting out its own retry timer. Safe to call when
        the queue is empty (a no-op) and safe to call often."""
        self._outbox.wake_all()
        self._wake.set()

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
        """Deliver the next READY job. Returns how long to wait before looking
        again; 0 means there is more to send right now.

        Ready = due (backoff elapsed) and dependency satisfied — so a job that
        is mid-backoff is stepped over rather than blocking the queue. When
        nothing is ready, sleep exactly until the earliest one is due."""
        self._trim_periodically()
        if not is_configured():
            return 5.0
        now = time.time()
        job = self._outbox.next_ready(now)
        if job is not None:
            self._deliver(job)
            return 0.0                   # keep draining while there is work
        due_at = self._outbox.next_due_at()
        if due_at is None:
            return settings.outbox_poll_secs           # queue empty
        return max(0.05, min(due_at - now, settings.outbox_poll_secs))

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
        # While deliveries are failing, the API client's own per-call console
        # record is silenced: the first failure was reported and the queue's
        # depth carries the rest, so a long outage cannot flush the log.
        call_log.quiet_retries(self._degraded)
        try:
            result_id = self._send(job)
        except CeravisApiError as exc:
            self._failed(job, exc)
            return
        except Exception as exc:
            # A bug in our own send code is not a reason to lose the event — it
            # is retried like any other failure (bounded by the 48h window),
            # loudly, so nothing generated is ever thrown away.
            logger.exception("outbox: %s job raised — will retry", job["kind"])
            self._failed(job, CeravisApiError(f"internal error: {exc}"))
            return
        self._outbox.mark_sent(job["job_id"], result_id)
        self._recovered()

    def _recovered(self) -> None:
        """A delivery just succeeded — clear any degraded/attention state and say
        so once."""
        self._outbox.clear_attention()
        if self._degraded:
            logger.info("outbox: deliveries recovered — draining %d queued "
                        "upload(s)", self._outbox.stats()["pending"])
            self._degraded = False
            self._problem = None

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
                # The spooled file is gone (external disk corruption — the queue
                # itself never releases it before delivery). Nothing to send, so
                # this attempt fails and retries; it rides to the 48h window
                # rather than being dropped, honouring "never discard an event".
                raise CeravisApiError("media body missing from the spool")
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
        """A delivery attempt failed. The job is NEVER dropped here — it is
        rescheduled on a capped exponential backoff, and only the 48h age window
        (enforced by the store's trim) ever gives up on it. A code that usually
        means a human must act also raises the needs-attention note."""
        attempts = job["attempts"] + 1
        status = getattr(exc, "status", None)
        delay = min(settings.outbox_backoff_base_secs * (2 ** (attempts - 1)),
                    settings.outbox_backoff_max_secs)
        delay *= random.uniform(0.8, 1.2)   # jitter: a fleet must not sync up
        self._outbox.mark_retry(job["job_id"], str(exc), time.time() + delay)

        if status in _ATTENTION_STATUSES:
            self._outbox.flag_attention(status, str(exc), job.get("label", ""))

        # One transition line, not one per retry: "offline" (no response at all)
        # and "rejecting" (the server answered with an error) are different news.
        problem = "offline" if status is None else "rejecting"
        self._degraded = True
        if self._problem != problem:
            self._problem = problem
            if problem == "offline":
                logger.warning("outbox: app server unreachable — %d upload(s) "
                               "queued, retrying until the %.0fh window",
                               self._outbox.stats()["pending"],
                               settings.outbox_window_secs / 3600.0)
            else:
                logger.warning("outbox: app server rejecting uploads (HTTP %s) — "
                               "%d queued and retrying; nothing is dropped",
                               status, self._outbox.stats()["pending"])

    # ---- console -----------------------------------------------------
    def _log_drop(self, job: dict, reason: str) -> None:
        """A discarded upload is news: it is the only case where an event the
        device detected never reaches the cloud, so it lands on the same sync
        console as every call, with the reason."""
        call_log.record(job["kind"], False, label=job.get("label"),
                        direction="out", state="dropped", error=reason)
        logger.warning("outbox: dropped %s (%s) — %s", job["kind"],
                       job.get("label", "")[:80], reason)
