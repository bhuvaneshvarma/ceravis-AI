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
from integration.ceravis_api import (
    CeravisApiError, is_configured, room_to_enum, save_alert, save_snapshot,
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
        self._event_types = {s.strip().lower()
                             for s in settings.cloud_alert_event_types.split(",")
                             if s.strip()}
        self._recipient_only = bool(settings.cloud_alert_recipient_only)
        self._running = False
        self._thread: threading.Thread | None = None
        self._warned_no_account = False

    def start(self) -> None:
        if not is_configured():
            logger.info("Cloud alerts disabled (CERAVIS_API_BASE_URL not set)")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="cloud-alert-publisher")
        self._thread.start()
        logger.info("Cloud alerts on — forwarding types=%s recipient_only=%s",
                    sorted(self._event_types) or sorted(self._severities),
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
            sev = (event.severity or "info").lower()
            etype = (event.event_type or "").lower()
            # Gate: only the configured event types (default: falls), and only
            # the enrolled recipient's events (recipient_id set on the event).
            if self._event_types:
                if etype not in self._event_types:
                    continue
            elif sev not in self._severities:
                continue
            if self._recipient_only and not event.recipient_id:
                continue
            pid = self._account.get().get("ceravisUserId")
            if not pid:
                if not self._warned_no_account:
                    logger.warning("saveAlert skipped — no verified account yet "
                                   "(run setup account verification)")
                    self._warned_no_account = True
                continue
            alert_type = (event.event_type or "").upper()
            message = self._format(event)
            try:
                save_alert(pid, alert_type, message)
            except CeravisApiError as exc:
                logger.warning("saveAlert failed (%s): %s", alert_type, exc)
            except Exception:
                logger.exception("saveAlert unexpected error")
            # Send the associated snapshot(s) with the same alert (Phase B will
            # populate snapshot_paths with the first/middle/last 3-frame nest).
            self._send_snapshots(pid, event, message)

    def _send_snapshots(self, pid, event, text: str) -> None:
        """Base64-encode and POST each snapshot tied to this alert. One today;
        the first/middle/last nest once Phase B fills snapshot_paths."""
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
                save_snapshot(pid, b64, label, camera_number)
            except CeravisApiError as exc:
                logger.warning("saveSnapshot failed: %s", exc)
            except Exception:
                logger.exception("saveSnapshot unexpected error")

    @staticmethod
    def _b64(rel_path: str) -> str | None:
        edir = Path(settings.events_dir)
        root = edir if edir.is_absolute() else (_EDGE_ROOT / edir)
        f = root / rel_path
        try:
            if not f.exists():
                return None
            return base64.b64encode(f.read_bytes()).decode("ascii")
        except Exception:
            logger.exception("snapshot read failed: %s", f)
            return None

    def _format(self, event) -> str:
        """Professional, fixed-format alert line, e.g.
        'CRITICAL · Fall detected · Kitchen / fridge · Ravi · 11:45 AM, 23 Jun 2026'."""
        who = self._account.get().get("firstName") or "recipient"
        sev = (event.severity or "info").upper()
        title = event.title or (event.event_type or "").replace("_", " ").title()
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
        parts = [f"{sev} · {title}"]
        if loc:
            parts.append(loc)
        parts += [who, ts]
        return " · ".join(parts)
