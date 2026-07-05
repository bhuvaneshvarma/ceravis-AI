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

import base64
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path

from config.settings import settings
from configuration.account_config import AccountConfig
from configuration.camera_config import CameraConfig
from events.event_bus import EventBus
from integration import call_log
from integration.ceravis_api import (
    CeravisApiError, alert_id_of, is_configured, room_to_enum, save_alert,
    save_snapshot,
)


logger = logging.getLogger("alerts")

# edge/ project root — to resolve event.snapshot_path (relative) to a file.
_EDGE_ROOT = Path(__file__).resolve().parents[1]


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
                    resp = save_alert(pid, etype.upper(), message)
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

    def _send_snapshots(self, pid, event, text: str, alert_id=None) -> None:
        """Base64-encode and POST each snapshot tied to this alert, linking it
        to alert_id when the event has one. One today; the first/middle/last
        nest once Phase B fills snapshot_paths."""
        paths = list(event.snapshot_paths or [])
        if not paths and event.snapshot_path:
            paths = [event.snapshot_path]
        if not paths:
            return
        camera_number = room_to_enum(event.room_name)
        n = len(paths)
        for i, rel in enumerate(paths):
            b64 = self._b64(rel)
            if not b64:
                continue
            label = text if n == 1 else f"{text} · frame {i + 1}/{n}"
            try:
                save_snapshot(pid, b64, label, camera_number, alert_id=alert_id)
            except CeravisApiError as exc:
                logger.warning("saveSnapshot failed: %s", exc)
            except Exception:
                logger.exception("saveSnapshot unexpected error")

    @staticmethod
    def _abs(rel_path: str):
        """Resolve a stored snapshot path to an absolute file, or None."""
        edir = Path(settings.events_dir)
        root = edir if edir.is_absolute() else (_EDGE_ROOT / edir)
        f = root / rel_path
        return f if f.exists() else None

    def _b64(self, rel_path: str) -> str | None:
        f = self._abs(rel_path)
        if f is None:
            return None
        try:
            return base64.b64encode(f.read_bytes()).decode("ascii")
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
        """Fixed-format line, e.g.
        'CRITICAL · Fall detected · Camera 1* Kitchen / fridge · Ravi · 11:45 AM, 23 Jun 2026'
        or 'Sitting → Standing · Camera 1 Kitchen · Ravi · 11:45 AM, 23 Jun 2026'."""
        who = self._account.get().get("firstName") or "recipient"
        # Camera label with its * (bathroom) / & (egress) designation, per spec.
        cam_label = ""
        try:
            cam = self._cameras.get_by_id(event.camera_id)
        except Exception:
            cam = None
        if cam is not None and cam.camera_number:
            sym = ("*" if cam.has_bathroom_entrance else "") \
                + ("&" if cam.is_main_egress else "")
            cam_label = f"Camera {cam.camera_number}{sym} "
        loc = cam_label + " / ".join(p for p in (event.room_name, event.zone_name) if p)
        try:
            ts = datetime.fromisoformat(event.timestamp).strftime("%I:%M %p, %d %b %Y")
            ts = ts.lstrip("0")
        except Exception:
            ts = event.timestamp or ""
        parts = [self._head(event)]
        if loc:
            parts.append(loc)
        parts += [who, ts]
        return " · ".join(parts)
