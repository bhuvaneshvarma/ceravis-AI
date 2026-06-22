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
from datetime import datetime

from config.settings import settings
from configuration.account_config import AccountConfig
from events.event_bus import EventBus
from integration.ceravis_api import CeravisApiError, is_configured, save_alert


logger = logging.getLogger("alerts")


class CloudAlertPublisher:
    def __init__(self, bus: EventBus) -> None:
        self._queue = bus.subscribe()
        self._account = AccountConfig()
        self._severities = {s.strip().lower()
                            for s in settings.cloud_alert_severities.split(",")
                            if s.strip()}
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
        logger.info("Cloud alerts on — forwarding %s to saveAlert",
                    sorted(self._severities))

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
            if sev not in self._severities:
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

    def _format(self, event) -> str:
        """Professional, fixed-format alert line, e.g.
        'CRITICAL · Fall detected · Kitchen / fridge · Ravi · 11:45 AM, 23 Jun 2026'."""
        who = self._account.get().get("firstName") or "recipient"
        sev = (event.severity or "info").upper()
        title = event.title or (event.event_type or "").replace("_", " ").title()
        loc = " / ".join(p for p in (event.room_name, event.zone_name) if p)
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
