from __future__ import annotations

import logging
import threading
import time

from events.event_bus import EventBus
from rules.fall_rule import FallRule
from rules.inactivity_rule import InactivityRule
from rules.posture_rule import PostureRule
from rules.rule_context import RuleContext
from rules.visitor_rule import VisitorRule


logger = logging.getLogger("rules")


class RuleEngine:
    """
    Periodic rule evaluation loop.

    Runs at 1 Hz — rules are O(persons) and cheap. The expensive
    inference upstream (detection/pose/reid) sets its own cadence.
    """

    TICK_HZ = 1.0

    def __init__(self, context: RuleContext, bus: EventBus) -> None:
        self._ctx = context
        self._bus = bus
        self._rules = [FallRule(), PostureRule(), InactivityRule(), VisitorRule()]
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
                        self._bus.publish(event)
                except Exception:
                    logger.exception("rule failed: %s", rule.__class__.__name__)
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)
