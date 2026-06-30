from __future__ import annotations

import logging
import queue

from schemas.event import Event


logger = logging.getLogger("events")


class EventBus:
    """
    Thread-safe in-process event bus.

    Producers (rule_engine) call publish().
    Consumers (event_writer, alert_manager) register subscribers
    and pop from their own queues.
    """

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue[Event]] = []

    def subscribe(self) -> "queue.Queue[Event]":
        q: queue.Queue[Event] = queue.Queue(maxsize=1000)
        self._subscribers.append(q)
        return q

    def publish(self, event: Event) -> None:
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                logger.warning("EventBus subscriber queue full — dropping")
