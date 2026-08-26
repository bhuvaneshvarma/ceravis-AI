from __future__ import annotations

"""
Device status heartbeat — the ONE thing that tells the app server this edge is
alive, and which of its cameras are ingesting right now.

It POSTs a tiny JSON body to settings.status_heartbeat_url every
status_heartbeat_interval_secs:

    { "ceravisUserId": <id>,          # the account this device serves
      "edgeId":        "<edge_id>",   # this device's fleet token — the identity
      "serverStatus":  "ON",          # see PRESENCE below
      "cameras": [ { "label": "LIVING_ROOM", "status": "ON" }, ... ],
      "timestamp":     "<edge-local ISO-8601>" }

The device is still keyed by edgeId (the app server can map edgeId -> account on
its own); ceravisUserId is sent alongside as a convenience the backend asked
for, and can be dropped later without touching the identity of the beat.

PRESENCE IS A DEAD-MAN'S SWITCH
    serverStatus is always "ON": the edge must be running to send at all, so it
    can never truthfully report itself OFF. A powered-off device, or one whose
    internet is cut, simply stops sending — the app server infers OFFLINE from
    the ABSENCE of beats (mark a device down after it misses ~3). This half of
    the contract lives on the server; the edge's only job is to keep saying "ON"
    while it can.

CAMERA on/off IS MEASURED AT THE INGESTION POINT
    A camera is "ON" when MediaMTX has its live path READY — i.e. its video is
    actually flowing into the system (the AI, the recorder, every live view).
    That is deliberately NOT an ONVIF ping: a camera can answer ONVIF while its
    RTSP stream is failing to ingest (wrong profile, HEVC, Wi-Fi collapse), and
    reporting it "ON" then would be a lie. path_ready is the state that matters.
    The label is the app server's CameraName room enum (KITCHEN, LIVING_ROOM,
    ...) — the SAME identifier saveCamera already registers cameras under, so
    the two calls line up on the cloud.

Purely additive and best-effort: every failure is swallowed, so cameras, AI and
recording never know this exists.

WHERE TO SEE IT
    Every beat is recorded in the forensic wire log by ceravis_api.send_status,
    so the live per-beat view is:  tail -f data/ceravis_api_wire.jsonl
    The service log (journalctl) stays quiet on purpose — a line a minute would
    bury it — so it carries only the STATE CHANGES at INFO/WARNING: the device
    coming online, going offline, or sitting idle waiting for an edge_id. The
    Cloud Sync Console (call_log) likewise shows only those transitions.
"""

import logging
import threading
from typing import Callable

from common import clock
from config.settings import settings
from configuration.account_config import effective_edge_id, patient_user_id
from configuration.camera_config import CameraConfig
from integration import call_log
from integration.ceravis_api import room_to_enum, send_status
from livestream.mediamtx_client import path_info


logger = logging.getLogger("integration")

# A short pause before the first beat so MediaMTX paths are up — otherwise the
# very first heartbeat after a boot would report every camera OFF and misdescribe
# a perfectly healthy device for one interval.
_WARMUP_SECS = 15.0


def build_payload() -> dict | None:
    """The heartbeat body, or None when there is nothing to key on yet — the
    device has no edge_id, so it is not provisioned and the app server could not
    attribute the beat anyway. Reads live state; never raises for a missing/odd
    camera field."""
    edge_id = effective_edge_id()
    if not edge_id:
        return None                       # not provisioned yet — skip this beat
    cameras = []
    for cam in CameraConfig().get_all():
        ready = bool((path_info(cam.camera_id) or {}).get("ready"))
        cameras.append({
            "label": room_to_enum(getattr(cam, "room_name", "")),
            "status": "ON" if ready else "OFF",
        })
    return {
        "ceravisUserId": patient_user_id(),
        "edgeId": edge_id,
        "serverStatus": "ON",
        "cameras": cameras,
        "timestamp": clock.now_iso(),
    }


class StatusReporter:
    """Beats device status -> the CERAVIS app server, once per interval, forever.

    Same lifecycle shape as the cloud outbox sender: a daemon thread woken for a
    clean shutdown via an Event, so stop()/join() return promptly."""

    def __init__(self, on_online: Callable[[], None] | None = None) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._interval = max(5.0, float(settings.status_heartbeat_interval_secs))
        # Called on each beat that confirms the app server is reachable. The
        # cloud outbox wires its kick() here, so the moment the heartbeat proves
        # the server is back the queued uploads drain — the heartbeat is the
        # reachability probe, the outbox is what acts on it. Best-effort: a
        # failure here never affects the beat.
        self._on_online = on_online
        # State the transition-only logging tracks, so the service log carries
        # each CHANGE exactly once instead of a line a minute: last beat failed?
        # ever succeeded? currently idle (no edge_id)?
        self._offline = False
        self._sent_once = False
        self._idle = False

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        if not settings.status_heartbeat_url.strip():
            logger.info("status heartbeat disabled (no status_heartbeat_url)")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="status-heartbeat")
        self._thread.start()
        logger.info("status heartbeat on -> %s every %.0fs",
                    settings.status_heartbeat_url, self._interval)

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- loop --------------------------------------------------------
    def _run(self) -> None:
        if self._wake.wait(min(_WARMUP_SECS, self._interval)):
            return                        # stopped during warmup
        while self._running:
            try:
                self._beat()
            except Exception:
                logger.exception("status heartbeat tick failed")
            if self._wake.wait(self._interval):
                return                    # stop() woke us — exit cleanly

    def _beat(self) -> None:
        payload = build_payload()
        if payload is None:
            self._mark_idle()             # no edge_id yet — say so, once
            return
        ok, _status, error = send_status(payload)
        if ok:
            self._mark_online(payload)
            self._notify_online()
        else:
            self._mark_offline(error or "no response")

    def _notify_online(self) -> None:
        """Tell whoever is listening (the cloud outbox) that the server just
        answered, so it can drain. Every good beat, not just the first — so a
        backlog that built up while the server was rejecting uploads gets a push
        each minute until it clears. Best-effort; never breaks the beat."""
        if self._on_online is None:
            return
        try:
            self._on_online()
        except Exception:
            logger.exception("status heartbeat: on_online hook failed")

    # ---- transition-only logging (keeps the service log readable) ----
    def _mark_online(self, payload: dict) -> None:
        self._idle = False
        cams = payload["cameras"]
        on = sum(1 for c in cams if c["status"] == "ON")
        summary = f"device ON · {on}/{len(cams)} cameras ingesting"
        if self._offline or not self._sent_once:
            self._offline = False
            self._sent_once = True
            logger.info("status heartbeat: online — %s", summary)
            call_log.record("status", True, direction="out", label=summary)
        else:
            logger.debug("status heartbeat ok — %s", summary)

    def _mark_idle(self) -> None:
        # The device has no edge_id (account not verified), so there is nothing
        # to attribute a beat to. Logged ONCE on entering this state so the
        # service log explains the silence instead of just being silent.
        if not self._idle:
            self._idle = True
            logger.info("status heartbeat: idle — no edge_id yet (account not "
                        "verified); beats will start once the device is verified")

    def _mark_offline(self, reason: str) -> None:
        # One piece of news, not one per beat: only the transition into offline
        # is recorded, mirroring how the outbox reports an outage exactly once.
        self._idle = False
        if not self._offline:
            self._offline = True
            logger.warning("status heartbeat: app server unreachable — %s", reason)
            call_log.record("status", False, direction="out",
                            label="device status", error=reason)
        else:
            logger.debug("status heartbeat still offline — %s", reason)
