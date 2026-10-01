from __future__ import annotations

"""
Event uploads to the app server: SENT THE MOMENT THEY ARE RAISED, kept on the
SSD only when a send fails.

SEND NOW, SIDE BY SIDE (2026-10-01)
    An alert, its photo and its clip go straight to the server on a worker
    thread: nothing is written to disk first, and nothing waits behind another
    upload. Before this every upload went through the queue, drained ONE call at
    a time, while the server takes ~9.5 s to accept an alert and ~2 s per photo
    — so each upload also waited for everything ahead of it (23-24 Sep: the
    worst 10% left 10 s+ late, a fall photo 23 s). Now alarms (fall, no-motion)
    have their own workers and ambient photos theirs, so a fall never waits for
    a posture photo, and a photo waits only for its OWN alert, whose server
    alertId it carries.

ONLY A FAILURE IS KEPT
    A send that fails — no answer, 5xx, 4xx — is written to the outbox table
    (storage/outbox_store.py) with its reason and the SAME job id, so the retry
    carries the same Idempotency-Key. In RAM there are only the few uploads in
    flight, and a still there is just the path of its file, read at send time.

THE PATH IS CLEAR -> SEND WHAT WAS KEPT
    Every app-server call that gets a 2xx — a recordingEvent (camera started /
    finalized), a direct upload, the status heartbeat — reports it through
    ceravis_api.on_reachable, which calls kick(): the kept uploads are sent at
    once, falls first, oldest first. While the server stays down the drainer
    tries ONE kept upload per pause (2 s doubling, capped), so a dead server is
    probed, not flooded, and ambient photos raised meanwhile go straight to the
    SSD instead of each timing out against it. An alarm is always tried live.

NEVER DROPPED FOR AN ERROR
    A kept upload leaves the table only by a successful send or by the 48h age
    window (outbox_window_secs). A 401/403/404/413 also raises a needs-attention
    note (bad key, wrong patient, clip too large) — still retried, nothing lost.
    A kept job that keeps failing sits on its own backoff and is stepped around.

ALERT LINKAGE
    A photo needs its alert's server alertId. While the alert is in flight the
    photo waits beside it (not on a worker) and goes the moment it lands; if
    the alert is kept, so is the photo, linked by job id, and the drainer stamps
    the real alertId once the alert is delivered.

Delivery is AT LEAST ONCE: an upload still in flight at shutdown is also kept,
and may arrive twice — the Idempotency-Key lets the backend drop the duplicate.
An upload in flight at the instant of a power cut is not kept (it was never
written down) — the price of sending without a write in front of it.
"""

import logging
import queue
import random
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from config.settings import settings
from integration import call_log
from integration.ceravis_api import (
    CeravisApiError, alert_id_of, is_configured, on_reachable, save_alert,
    save_snapshot,
)
from storage.outbox_store import PRIORITY_ALERT, PRIORITY_AMBIENT, OutboxStore


logger = logging.getLogger("outbox")

# Answers that usually mean a human must act (bad/expired API key, wrong patient
# id, payload too large): still retried, but they raise the needs-attention note.
_ATTENTION_STATUSES = {401, 403, 404, 413}

# The server (or the proxy in front of it) is OVERLOADED or unreachable — the
# path is the problem, not this upload — so sending pauses instead of firing the
# next upload into it. On 2026-09-23 a burst of 4K snapshots drew 200 of these.
_OVERLOAD_STATUSES = {429, 502, 503, 504}

# Kinds this build no longer sends. A device upgrading from an older build can
# still have rows for them; they are cleared on sight instead of retried.
_RETIRED_KINDS = {"recordingEvent"}

# Worker threads per class. Alarms never share a worker with an ambient photo.
_WORKERS = {"alarm": 2, "ambient": 2}
# Ambient photos waiting for a worker past this go to the SSD (drained in turn)
# rather than piling up in RAM behind a slow server.
_AMBIENT_MAX_WAITING = 8
# alertIds of alerts delivered live, for photos and clips raised after them.
_ALERT_IDS_KEPT = 256


class _Upload:
    """One upload in flight. `media` is a Path (a still on disk) or bytes (a
    clip); `children` are the photos waiting for this alert's alertId."""
    __slots__ = ("job_id", "kind", "payload", "label", "priority", "media",
                 "part", "depends_on", "children")

    def __init__(self, kind: str, payload: dict, label: str, priority: int, *,
                 media=None, part: str | None = None,
                 depends_on: str | None = None) -> None:
        self.job_id = uuid.uuid4().hex
        self.kind, self.payload, self.label = kind, payload, label
        self.priority, self.media, self.part = priority, media, part
        self.depends_on = depends_on
        self.children: list[_Upload] = []


class OutboxSender:
    """Sends event uploads live; keeps the failed ones on the SSD and sends
    them when the path is clear."""

    def __init__(self, outbox: OutboxStore) -> None:
        self._outbox = outbox
        self._outbox.set_drop_listener(self._log_drop)
        self._running = False
        self._threads: list[threading.Thread] = []
        self._work = {cls: queue.Queue() for cls in _WORKERS}
        self._lock = threading.Lock()
        self._live: dict[str, _Upload] = {}            # raised, not finished
        self._alert_ids: OrderedDict = OrderedDict()   # job_id -> alertId (live)
        self._wake = threading.Event()                 # the drainer's
        self._kept = True        # anything on the SSD? (checked on the first pass)
        self._paused_until = 0.0                       # monotonic
        self._failures = 0                             # consecutive path failures
        self._last_drained = 0.0
        self._trimmed_at = 0.0
        self._problem: str | None = None               # "offline" | "rejecting"

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        on_reachable(self.kick)
        for cls, n in _WORKERS.items():
            for i in range(n):
                self._spawn(self._worker, self._work[cls], f"cloud-send-{cls}-{i}")
        self._spawn(self._drain, None, "cloud-outbox-drain")
        logger.info("Cloud uploads on — sent live; %d kept on the SSD from "
                    "before", self._outbox.stats()["pending"])

    def _spawn(self, target, arg, name: str) -> None:
        thread = threading.Thread(target=target,
                                  args=() if arg is None else (arg,),
                                  daemon=True, name=name)
        self._threads.append(thread)
        thread.start()

    def stop(self) -> None:
        """Stop sending. Uploads not started yet are kept on the SSD."""
        self._running = False
        self._wake.set()
        for work in self._work.values():
            while True:
                try:
                    self._keep(work.get_nowait(), "not sent before shutdown")
                except queue.Empty:
                    break

    def join(self, timeout: float | None = None) -> None:
        """Wait for the workers, then keep whatever is still in flight."""
        end = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            thread.join(None if end is None else max(0.0, end - time.monotonic()))
        with self._lock:
            left = list(self._live.values())
        for up in left:
            if up.job_id in self._live:   # a parent's keep may have taken it
                self._keep(up, "in flight at shutdown")

    def kick(self) -> None:
        """The path to the server is clear (an app-server call just got a 2xx):
        send what was kept, now. Called on every good answer, so it costs
        nothing while nothing is kept (or once this sender is stopped)."""
        if not (self._running and self._kept):
            return
        self._paused_until, self._failures = 0.0, 0
        self._outbox.wake_all()
        self._wake.set()

    # ---- what producers call -----------------------------------------
    def queue_alert(self, patient_id, alert_type: str, message: str, *,
                    priority: int = PRIORITY_ALERT) -> str:
        """Send one saveAlert. The returned id is what its photos link to."""
        return self._raise(_Upload(
            "saveAlert", {"patient_id": patient_id, "alert_type": alert_type,
                          "message": message},
            f"{alert_type} · {message}", priority))

    def queue_snapshot(self, patient_id, text: str, camera_number: str, *,
                       image: bytes | None = None, video: bytes | None = None,
                       image_path=None, category: str | None = None,
                       depends_on: str | None = None,
                       priority: int = PRIORITY_AMBIENT) -> str | None:
        """Send one saveSnapshot — a still (bytes, or a file on disk via
        `image_path`, read at send time) or a clip."""
        media = Path(image_path) if image_path is not None else (image or video)
        if not media:
            return None
        return self._raise(_Upload(
            "saveSnapshot", {"patient_id": patient_id, "text": text,
                             "camera_number": camera_number, "category": category},
            text, priority, media=media, part="video" if video else "image",
            depends_on=depends_on))

    def _raise(self, up: _Upload) -> str:
        with self._lock:
            parent = self._live.get(up.depends_on) if up.depends_on else None
            self._live[up.job_id] = up
            if parent is not None:          # its alert is on its way: go with it
                parent.children.append(up)
                return up.job_id
        if not self._running:
            self._keep(up, "sending is stopped")
        elif up.depends_on and self._kept_job(up.depends_on):
            self._keep(up, "its alert is kept on the SSD")
        else:
            if up.depends_on:
                up.payload["alert_id"] = self._alert_id(up.depends_on)
            self._dispatch(up)
        return up.job_id

    def _dispatch(self, up: _Upload) -> None:
        if up.priority >= PRIORITY_ALERT:
            self._work["alarm"].put(up)            # an alarm is always tried live
        elif time.monotonic() < self._paused_until:
            self._keep(up, "app server unavailable — kept until it answers")
        elif self._work["ambient"].qsize() >= _AMBIENT_MAX_WAITING:
            self._keep(up, "photos are waiting for the server — kept")
        else:
            self._work["ambient"].put(up)

    # ---- live sending ------------------------------------------------
    def _worker(self, work: queue.Queue) -> None:
        while self._running:
            try:
                up = work.get(timeout=1.0)
            except queue.Empty:
                continue
            if not self._running:
                self._keep(up, "not sent before shutdown")
                continue
            try:
                media = up.media.read_bytes() if isinstance(up.media, Path) else up.media
                result = self._call(up.kind, up.payload, media, up.part, up.job_id)
            except Exception as exc:               # noqa: BLE001 — kept, never lost
                self._keep(up, exc)
                continue
            self._recovered()
            self._finish(up, result)

    def _finish(self, up: _Upload, alert_id=None, kept: bool = False) -> None:
        """An upload is done (delivered or kept): release the photos waiting
        for it — live with its alertId, or onto the SSD behind it."""
        with self._lock:
            self._live.pop(up.job_id, None)
            if up.kind == "saveAlert" and not kept:
                self._alert_ids[up.job_id] = alert_id
                while len(self._alert_ids) > _ALERT_IDS_KEPT:
                    self._alert_ids.popitem(last=False)
            children, up.children = up.children, []
        for child in children:
            if kept or not self._running:
                self._keep(child, "its alert is kept on the SSD")
            else:
                child.payload["alert_id"] = alert_id
                self._dispatch(child)

    def _keep(self, up: _Upload, why) -> None:
        """Onto the SSD, with the reason, under the upload's own job id. A real
        failed attempt (`why` an exception) also counts against the path."""
        media = up.media
        job_id = self._outbox.enqueue(
            up.kind, up.payload, label=up.label, priority=up.priority,
            blob=media if isinstance(media, bytes) else None,
            blob_file=media if isinstance(media, Path) else None,
            blob_part=up.part, blob_ext="mp4" if up.part == "video" else "jpg",
            depends_on=up.depends_on, job_id=up.job_id)
        if job_id is not None:
            self._kept = True
            if isinstance(why, Exception):
                self._failed(job_id, 0, why, alarm=up.priority >= PRIORITY_ALERT)
            # The console's line for it: the detection is captured and waiting.
            call_log.record(up.kind, True, label=up.label, direction="out",
                            state="queued")
            self._wake.set()
        self._finish(up, kept=True)

    # ---- the drainer: what was kept ----------------------------------
    def _drain(self) -> None:
        while self._running:
            wait = settings.outbox_poll_secs
            try:
                wait = self._drain_tick()
            except Exception:
                logger.exception("outbox: drain tick failed")
            if wait <= 0:
                continue                   # a backlog drains back-to-back
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def _drain_tick(self) -> float:
        """Send the next kept upload that is ready. Returns how long to wait
        before looking again; 0 means there is more to send right now."""
        self._trim_periodically()
        if not is_configured():
            return settings.outbox_poll_secs
        mono = time.monotonic()
        if self._paused_until > mono:                # probing a down server
            return self._paused_until - mono
        now = time.time()
        job = self._outbox.next_ready(now)
        if job is None:
            due_at = self._outbox.next_due_at()
            if due_at is None:
                self._kept = self._outbox.stats()["pending"] > 0
                return settings.outbox_poll_secs
            return max(0.05, min(due_at - now, settings.outbox_poll_secs))
        if job["priority"] < PRIORITY_ALERT and job["kind"] not in _RETIRED_KINDS:
            # an ambient backlog is paced (a retired row never reaches the server)
            gap = self._last_drained + settings.outbox_bulk_min_interval_secs - mono
            if gap > 0:
                return gap
        self._deliver(job)
        return 0.0

    def _trim_periodically(self) -> None:
        """Re-apply the 48h window on a slow beat: during a long outage nothing
        new may be kept for hours, and a backlog must not outlive its window."""
        now = time.monotonic()
        if now - self._trimmed_at >= 60.0:
            self._trimmed_at = now
            self._outbox.trim()

    def _deliver(self, job: dict) -> None:
        if job["kind"] in _RETIRED_KINDS:
            self._outbox.mark_dead(
                job["job_id"],
                f"{job['kind']} is no longer sent from the outbox — moved to the "
                "best-effort reporter (integration/recording_events.py)")
            return
        self._last_drained = time.monotonic()
        # While the path is failing, the API client's per-call console line is
        # silenced: the first failure was reported, the kept count says the rest.
        call_log.quiet_retries(self._problem is not None)
        payload = dict(job["payload"])
        try:
            media = self._outbox.blob(job) if job["kind"] == "saveSnapshot" else None
            if payload.get("alert_id") is None:
                payload["alert_id"] = self._alert_id(job.get("depends_on"))
            result = self._call(job["kind"], payload, media, job["blob_part"],
                                job["job_id"])
        except Exception as exc:                   # noqa: BLE001 — retried, never lost
            self._failed(job["job_id"], job["attempts"], exc,
                         alarm=job["priority"] >= PRIORITY_ALERT)
            return
        self._outbox.mark_sent(job["job_id"], result)
        self._recovered()

    # ---- shared ------------------------------------------------------
    @staticmethod
    def _call(kind: str, payload: dict, media, part, key: str) -> int | None:
        """One request. The job id is the Idempotency-Key, identical on every
        attempt of the same upload."""
        if kind == "saveAlert":
            return alert_id_of(save_alert(payload["patient_id"], payload["alert_type"],
                                          payload["message"], idempotency_key=key))
        if kind == "saveSnapshot":
            if not media:
                raise CeravisApiError("media body missing")
            save_snapshot(payload["patient_id"], payload.get("text") or "",
                          payload.get("camera_number") or "",
                          image=media if part == "image" else None,
                          video=media if part == "video" else None,
                          alert_id=payload.get("alert_id"),
                          category=payload.get("category"), idempotency_key=key)
            return None
        raise CeravisApiError(f"unknown upload kind {kind!r}")

    def _alert_id(self, parent: str | None):
        """The server alertId of the alert `parent` (a job id): from a live
        delivery, or from its kept row once the drainer delivered it. None when
        there is no parent or no id — the photo then goes unlinked rather than
        held back."""
        if not parent:
            return None
        with self._lock:
            if parent in self._alert_ids:
                return self._alert_ids[parent]
        row = self._outbox.job(parent)
        return row["result_id"] if row else None

    def _kept_job(self, job_id: str) -> bool:
        """Is this alert waiting on the SSD (not delivered yet)?"""
        row = self._outbox.job(job_id)
        return bool(row and row["state"] == "pending")

    def _failed(self, job_id: str, attempts: int, exc: Exception, *,
                alarm: bool) -> None:
        """An attempt failed. The upload stays kept, retried on a capped
        backoff (only the 48h window ever gives up on it). When the PATH is the
        problem — no answer, or an overload answer — sending pauses too, so the
        server gets one probe per pause rather than every upload at once."""
        if not isinstance(exc, CeravisApiError):
            logger.error("outbox: upload raised %r — kept, will retry", exc)
            exc = CeravisApiError(f"internal error: {exc}")
        status = exc.status
        delay = min(settings.outbox_backoff_base_secs * (2 ** attempts),
                    settings.outbox_backoff_max_secs) * random.uniform(0.8, 1.2)
        self._outbox.mark_retry(job_id, str(exc), time.time() + delay)
        if status in _ATTENTION_STATUSES:
            self._outbox.flag_attention(status, str(exc))
        if status is None or status in _OVERLOAD_STATUSES:
            self._failures += 1
            pause = exc.retry_after
            if pause is None:
                pause = settings.outbox_backoff_base_secs * (2 ** (self._failures - 1))
            cap = (settings.outbox_overload_pause_max_urgent_secs if alarm
                   else settings.outbox_overload_pause_max_secs)
            # An alarm shortens a longer pause an ambient failure set.
            mono = time.monotonic()
            until = mono + min(pause, cap)
            active = self._paused_until > mono
            self._paused_until = (min(self._paused_until, until) if alarm and active
                                  else until)
        problem = "offline" if status is None else "rejecting"
        if self._problem != problem:
            self._problem = problem
            kept = self._outbox.stats()["pending"]
            if problem == "offline":
                logger.warning("outbox: app server unreachable — %d upload(s) kept "
                               "on the SSD, sent when it answers again", kept)
            else:
                logger.warning("outbox: app server rejecting uploads (HTTP %s) — "
                               "%d kept and retried; nothing is dropped", status, kept)

    def _recovered(self) -> None:
        """A delivery succeeded: the path works and the config is accepted."""
        self._failures = 0
        self._outbox.clear_attention()
        if self._problem is not None:
            self._problem = None
            logger.info("outbox: app server answering again — sending %d kept "
                        "upload(s)", self._outbox.stats()["pending"])

    def _log_drop(self, job: dict, reason: str) -> None:
        """A discarded upload is news: the only case where an event the device
        detected never reaches the cloud."""
        call_log.record(job["kind"], False, label=job.get("label"),
                        direction="out", state="dropped", error=reason)
        logger.warning("outbox: dropped %s (%s) — %s", job["kind"],
                       job.get("label", "")[:80], reason)
