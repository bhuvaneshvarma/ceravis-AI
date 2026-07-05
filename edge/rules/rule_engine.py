from __future__ import annotations

import logging
import threading
import time

from events.event_bus import EventBus
from integration import call_log
from rules.fall_rule import FallRule
from rules.posture_rule import PostureRule
from rules.room_rule import RoomTransitionRule
from rules.rule_context import RuleContext
from rules.spatial_rule import SpatialRule
from rules.stillness_rule import StillnessRule


logger = logging.getLogger("rules")


class RuleEngine:
    """
    Periodic rule evaluation loop.

    Runs at 1 Hz — rules are O(persons) and cheap. The expensive
    inference upstream (detection/pose/reid) sets its own cadence.
    """

    TICK_HZ = 1.0

    def __init__(self, context: RuleContext, bus: EventBus,
                 enricher=None) -> None:
        self._ctx = context
        self._bus = bus
        self._enricher = enricher          # EventEnricher | None
        self._rules = [FallRule(), PostureRule(), StillnessRule(),
                       SpatialRule(), RoomTransitionRule()]
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="rule-engine",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        interval = 1.0 / self.TICK_HZ
        while self._running:
            t0 = time.perf_counter()
            for rule in self._rules:
                try:
                    for event in rule.evaluate(self._ctx):
                        if self._enricher is not None:
                            try:
                                event = self._enricher.enrich(event, self._ctx)
                            except Exception:
                                logger.exception("event enrich failed")
                        # Every detection lands on the monitor's sync console
                        # (endpoint "event") — including the ones the cloud
                        # gate drops later — so a physical test is verifiable
                        # on screen whether or not it reached the server.
                        call_log.record(
                            "event", True,
                            label=f"{(event.severity or 'info').upper()} · "
                                  f"{event.message or event.event_type}")
                        self._bus.publish(event)
                except Exception:
                    logger.exception("rule failed: %s", rule.__class__.__name__)
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)
