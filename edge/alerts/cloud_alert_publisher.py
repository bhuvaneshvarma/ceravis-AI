from __future__ import annotations

"""
Forwards actionable alerts to the CERAVIS app server.

Subscribes to the same EventBus the MQTT publisher uses. For every enriched
event whose severity is in CLOUD_ALERT_SEVERITIES (default critical + warning),
it calls saveAlert with:
    patientUserId = the verified account's ceravisUserId (account.json)
    alertType     = the event type, upper-cased (e.g. "FALL")
    messageText   = the operator-facing message the enricher built

Fire-and-forget per event (errors are logged, never block the pipeline). Stays
silent if the app server isn't configured or no account has been verified yet.
"""

import logging
import queue
import threading
import time
from datetime import datetime

from alerts.alert_format import format_line
from common.event_snapshots import snapshot_file
from config.settings import settings
from configuration.account_config import AccountConfig
from configuration.camera_config import CameraConfig
from events.event_bus import EventBus
from integration import call_log
from integration.ceravis_api import (
    CeravisApiError, alert_id_of, is_configured, room_to_enum, save_alert,
    save_snapshot,
)
from livestream.mediamtx_client import record_path_name
from recording.incident_clip import build_incident_clip


logger = logging.getLogger("alerts")

# event_type -> the backend AlertType enum. Explicit so the wire value is a
# deliberate contract, not an artifact of upper-casing the internal event name
# (fall -> FALL, no_motion -> NO_MOTION). Anything unmapped falls back to upper().
_ALERT_TYPE = {"fall": "FALL", "no_motion": "NO_MOTION"}


class CloudAlertPublisher:
    def __init__(self, bus: EventBus) -> None:
        self._queue = bus.subscribe()
        self._account = AccountConfig()
        self._cameras = CameraConfig()
        self._severities = {s.strip().lower()
                            for s in settings.cloud_alert_severities.split(",")
                            if s.strip()}
        self._event_types = {s.strip().lower()           # alert + snapshot
                             for s in settings.cloud_alert_event_types.split(",")
                             if s.strip()}
        self._snapshot_types = {s.strip().lower()         # snapshot ONLY (no alert)
                                for s in settings.cloud_snapshot_event_types.split(",")
                                if s.strip()}
        self._recipient_only = bool(settings.cloud_alert_recipient_only)
        self._running = False
        self._thread: threading.Thread | None = None
        self._warned_no_account = False
        # recipient_id -> alertId of that recipient's current no_motion slot, so
        # the follow-up no_motion_snapshot burst (fired minute-by-minute over the
        # rest of the slot) links back to the alert that opened it.
        self._slot_alert_id: dict = {}
        # camera_id -> monotonic time of its last fall clip, so a burst of fall
        # events for one incident yields a single clip (fall_clip_cooldown_secs).
        self._fall_clip_at: dict = {}

    def start(self) -> None:
        if not is_configured():
            logger.info("Cloud alerts disabled (CERAVIS_API_BASE_URL not set)")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="cloud-alert-publisher")
        self._thread.start()
        logger.info("Cloud on — alert+snap=%s snap-only=%s recipient_only=%s",
                    sorted(self._event_types), sorted(self._snapshot_types),
                    self._recipient_only)

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while self._running:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            etype = (event.event_type or "").lower()
            is_alert = etype in self._event_types          # saveAlert + saveSnapshot
            is_snap = etype in self._snapshot_types         # saveSnapshot only
            if not (is_alert or is_snap):
                continue
            if self._recipient_only and not event.recipient_id:
                # Visible on the sync console: the detection happened but the
                # track wasn't identified as the recipient, so nothing is sent.
                call_log.record(
                    "event", False, label=self._format(event),
                    error="not sent — track not identified as the recipient "
                          "(no ReID lock at event time)")
                continue
            pid = self._account.get().get("ceravisUserId")
            if not pid:
                if not self._warned_no_account:
                    logger.warning("cloud send skipped — no verified account yet "
                                   "(run setup account verification)")
                    self._warned_no_account = True
                call_log.record(
                    "event", False, label=self._format(event),
                    error="not sent — no verified account (run setup step 1)")
                continue
            message = self._format(event)
            alert_id = None
            if is_alert:
                try:
                    resp = save_alert(pid, _ALERT_TYPE.get(etype, etype.upper()),
                                      message)
                    alert_id = alert_id_of(resp)
                    if etype == "no_motion" and event.recipient_id:
                        self._slot_alert_id[event.recipient_id] = alert_id
                except CeravisApiError as exc:
                    logger.warning("saveAlert failed (%s): %s", etype, exc)
                except Exception:
                    logger.exception("saveAlert unexpected error")
            elif etype == "no_motion_snapshot" and event.recipient_id:
                # a follow-up snap in the no_motion slot -> reuse the slot's alertId
                alert_id = self._slot_alert_id.get(event.recipient_id)
            # Snapshot goes with both alert and snapshot-only events (Phase B will
            # populate snapshot_paths with the first/middle/last 3-frame nest).
            self._send_snapshots(pid, event, message, alert_id)
            # A FALL also gets the moving footage: merge the recorded segments
            # around the instant and send that clip through the SAME saveSnapshot,
            # linked by the same alertId + annotation. Deferred (the post-roll
            # must finish writing) and best-effort — it never blocks this event.
            if is_alert and etype == "fall":
                self._schedule_fall_clip(pid, event, message, alert_id)

    def _send_snapshots(self, pid, event, text: str, alert_id=None) -> None:
        """POST each still snapshot tied to this alert as the multipart `image`
        file, linking it to alert_id when the event has one. One today; the
        first/middle/last nest once Phase B fills snapshot_paths."""
        paths = list(event.snapshot_paths or [])
        if not paths and event.snapshot_path:
            paths = [event.snapshot_path]
        if not paths:
            return
        camera_number = room_to_enum(event.room_name)
        n = len(paths)
        for i, rel in enumerate(paths):
            img = self._image_bytes(rel)
            if not img:
                continue
            label = text if n == 1 else f"{text} · frame {i + 1}/{n}"
            try:
                save_snapshot(pid, label, camera_number, image=img,
                              alert_id=alert_id)
            except CeravisApiError as exc:
                logger.warning("saveSnapshot failed: %s", exc)
            except Exception:
                logger.exception("saveSnapshot unexpected error")

    # ---- fall incident clip ------------------------------------------
    def _schedule_fall_clip(self, pid, event, text: str, alert_id) -> None:
        """Kick off the deferred fall-clip build for this event (once per camera
        per incident). Returns immediately — the merge + upload run on a daemon
        thread so the event loop is never blocked."""
        if not settings.fall_clip_enabled:
            return
        try:
            cam = self._cameras.get_by_id(event.camera_id)
        except Exception:
            cam = None
        if cam is None:
            return
        now = time.monotonic()
        last = self._fall_clip_at.get(event.camera_id, 0.0)
        if now - last < settings.fall_clip_cooldown_secs:
            return                                  # same incident — one clip only
        self._fall_clip_at[event.camera_id] = now
        try:
            at = datetime.fromisoformat(event.timestamp)
            if at.tzinfo is None:
                at = at.astimezone()                # naive -> device-local
        except Exception:
            at = datetime.now().astimezone()
        threading.Thread(
            target=self._build_and_send_fall_clip,
            args=(pid, record_path_name(cam), at, text,
                  room_to_enum(event.room_name), alert_id),
            daemon=True, name="fall-clip").start()

    def _build_and_send_fall_clip(self, pid, rec_path, at, text, camera_number,
                                  alert_id) -> None:
        """Wait for the post-roll footage to flush, merge the clip, and POST it
        via saveSnapshot with the same alertId + annotation as the alert."""
        # The segment covering at+post is still being written when the alert
        # fires; wait it out (post window + one segment + a small margin).
        time.sleep(settings.fall_clip_post_secs + settings.record_segment_secs + 2.0)
        try:
            clip = build_incident_clip(rec_path, at, settings.fall_clip_pre_secs,
                                       settings.fall_clip_post_secs)
        except Exception:
            logger.exception("fall clip build error (%s)", rec_path)
            clip = None
        if not clip:
            call_log.record(
                "saveSnapshot", False, label=f"{text} · clip", alert_id=alert_id,
                error="fall clip not sent — no footage (recording off or nobody "
                      "in frame at the incident)")
            return
        try:
            save_snapshot(pid, text, camera_number, video=clip, alert_id=alert_id)
            logger.info("fall clip sent: %d bytes, alert=%s", len(clip), alert_id)
        except CeravisApiError as exc:
            logger.warning("fall clip saveSnapshot failed: %s", exc)
        except Exception:
            logger.exception("fall clip saveSnapshot unexpected error")

    def _image_bytes(self, rel_path: str) -> bytes | None:
        # Shared resolver (common.event_snapshots) — same one the enricher
        # writes through and the events API serves from. Raw JPEG bytes: the
        # saveSnapshot `image` file part is the file itself, not base64.
        f = snapshot_file(rel_path)
        if f is None:
            return None
        try:
            return f.read_bytes()
        except Exception:
            logger.exception("snapshot read failed: %s", f)
            return None

    # event_type -> "from → to" arrow head (same line shape as the fall alert)
    _ARROWS = {
        "standing_up": "Sitting → Standing",
        "sitting_down": "Standing → Sitting",
        "walking_started": "Standing → Walking",
        "walking_stopped": "Walking → Standing",
    }

    def _head(self, event) -> str:
        """The leading segment: 'CRITICAL · Fall detected' for an alert, the
        posture arrow for a transition, 'No movement N/15 min' for inactivity."""
        et = (event.event_type or "").lower()
        if et in self._ARROWS:
            return self._ARROWS[et]
        det = (event.detail or "").strip()
        if et in ("area_transition", "room_transition"):
            # detail is the move itself, e.g. "kitchen → living room"
            if "→" in det:
                a, _, b = det.partition("→")
                return f"{a.strip().title()} → {b.strip().title()}"
            return det or ("Changed room" if et == "room_transition"
                           else "Moved area")
        if et == "no_motion_snapshot":
            return f"No movement {det} min" if det else "No movement"
        if et == "no_transition_snapshot":
            return f"No transition {det} min" if det else "No transition"
        # fall, the critical no_motion alert, and anything else -> SEV · Title
        sev = (event.severity or "info").upper()
        title = event.title or et.replace("_", " ").title()
        return f"{sev} · {title}"

    def _format(self, event) -> str:
        """Fixed-format line via the shared Format-A builder, e.g.
        'CRITICAL · Fall detected · Camera 1* Kitchen / fridge · Ravi · 11:45 AM, 23 Jun 2026'
        or 'Sitting → Standing · Camera 1 Kitchen · Ravi · 11:45 AM, 23 Jun 2026'."""
        who = self._account.get().get("firstName") or "recipient"
        try:
            cam = self._cameras.get_by_id(event.camera_id)
        except Exception:
            cam = None
        try:
            when: datetime | str = datetime.fromisoformat(event.timestamp)
        except Exception:
            when = event.timestamp or ""
        return format_line(self._head(event), cam, event.room_name, who,
                           when, zone_name=event.zone_name)
