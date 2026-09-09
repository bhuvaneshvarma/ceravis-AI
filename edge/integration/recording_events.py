from __future__ import annotations

"""
Best-effort push of "camera started / finished recording" to the app server.

DELIBERATELY NOT ON THE CLOUD OUTBOX. The outbox exists for evidence — a fall,
the still that shows it, the clip that proves it — which must survive an outage
and is therefore retried until it lands. A recording transition is not evidence,
it is a NOTIFICATION, and the app server can read the authoritative answer at any
moment from `GET /api/v1/recordings/status`. So a lost event costs nothing: the
next status poll re-syncs it.

That distinction is not academic. On 2026-09-09 these events WERE on the outbox,
the endpoint was failing, and ~10 of them retried every 30s for 85 minutes. Since
nothing is ever dropped there and the sender is a single thread, those doomed
requests kept the socket busy ~267% of the time, and a FALL alert raised in that
window had to wait for an in-flight doomed request before it could even be
picked. A notification queue must never be able to do that to an alarm.

The rules that keep it harmless:

  * ONE worker thread, so recording traffic can never fan out.
  * A BOUNDED queue that drops the OLDEST on overflow — backlog is capped by
    construction, so a dead endpoint costs a fixed few slots, never 85 minutes
    of retries.
  * NO retries. One attempt, then the event is gone. `/recordings/status` is the
    recovery path, and it is a better one than a stale replay.
  * A SHORT timeout of its own, so an unresponsive endpoint parks this thread
    briefly and nothing else at all.
"""

import logging
import queue
import threading

from configuration.account_config import effective_edge_id
from integration.ceravis_api import CeravisApiError, is_configured, send_recording_event


logger = logging.getLogger("integration")

# Enough to absorb a burst of transitions across every camera; past this the
# oldest is dropped, because the newest state is the one worth telling.
_MAX_PENDING = 32


class RecordingEventReporter:
    """Fire-and-forget reporter for camera record start/stop transitions."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=_MAX_PENDING)
        self._thread: threading.Thread | None = None
        self._running = False
        self._dropped = 0

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="recording-events")
        self._thread.start()
        logger.info("recording-event reporter on (best-effort, no retry; "
                    "/recordings/status is the source of truth)")

    def stop(self) -> None:
        self._running = False
        try:
            self._q.put_nowait(None)          # unblock a worker sitting in get()
        except queue.Full:
            pass                              # it exits on the 1s timeout instead

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- what the recorder calls -------------------------------------
    def queue_recording_event(self, camera_id: str, status: str, start: str,
                              *, end: str | None = None,
                              seconds: float | None = None) -> None:
        """Hand over one transition. Returns immediately — this is called from
        the recording tick, which must never wait on the network.

        The wire shape lives here so the recorder stays domain-level. `edgeId`
        is resolved NOW: the event is a fact about the device as it was when the
        footage was recorded."""
        edge_id = effective_edge_id()
        if not edge_id:
            logger.debug("recordingEvent skipped: no edge_id yet (%s %s)",
                         camera_id, status)
            return
        payload = {"edgeId": edge_id, "camera_id": camera_id, "status": status,
                   "segment": {"start": start, "end": end, "seconds": seconds}}
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            # Shed the OLDEST, keep the newest: a stale transition is the least
            # useful thing here, and the status endpoint covers what we drop.
            try:
                self._q.get_nowait()
                self._q.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass
            self._dropped += 1
            if self._dropped in (1, 10, 100) or self._dropped % 500 == 0:
                logger.warning("recording-event queue full — %d dropped so far "
                               "(harmless: /recordings/status still answers)",
                               self._dropped)

    # ---- the one worker ----------------------------------------------
    def _run(self) -> None:
        while self._running:
            try:
                payload = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if payload is None:                # stop() sentinel
                break
            if not is_configured():
                continue
            try:
                send_recording_event(payload)
            except CeravisApiError as exc:
                # One attempt only. Logged, never retried, never queued.
                logger.info("recordingEvent %s %s not delivered: %s",
                            payload.get("camera_id"), payload.get("status"), exc)
            except Exception:                  # noqa: BLE001 — never kill the thread
                logger.exception("recordingEvent reporter error")

    # ---- introspection for /system/status ----------------------------
    def stats(self) -> dict:
        return {"pending": self._q.qsize(), "dropped": self._dropped,
                "max_pending": _MAX_PENDING}
