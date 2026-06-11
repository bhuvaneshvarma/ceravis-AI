from __future__ import annotations

import json
import logging
import queue
import threading

from config.settings import settings
from events.event_bus import EventBus

try:
    import paho.mqtt.client as mqtt  # type: ignore
    _MQTT_AVAILABLE = True
except ImportError:  # pragma: no cover
    mqtt = None  # type: ignore
    _MQTT_AVAILABLE = False


logger = logging.getLogger("alerts")


class MqttPublisher:
    """
    Publishes events to AWS IoT Core via MQTT TLS.

    QoS by severity:
      - fall      -> QoS 1
      - visitor   -> QoS 1
      - others    -> QoS 0

    Stays silent if endpoint not configured.
    """

    QOS1_EVENTS = {"fall", "visitor"}

    def __init__(self, bus: EventBus) -> None:
        self._queue = bus.subscribe()
        self._client: "mqtt.Client | None" = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not _MQTT_AVAILABLE or not settings.mqtt_endpoint:
            logger.info("MQTT disabled (no endpoint / paho missing)")
            return
        self._client = mqtt.Client(client_id=settings.device_id)
        self._client.tls_set()  # certs come from env / mounted volume
        self._client.connect(settings.mqtt_endpoint, settings.mqtt_port, 60)
        self._client.loop_start()

        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mqtt-publisher",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        topic_root = f"{settings.mqtt_topic_prefix}/{settings.device_id}"
        while self._running:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            qos = 1 if event.event_type in self.QOS1_EVENTS else 0
            topic = f"{topic_root}/{event.event_type}"
            try:
                assert self._client is not None
                self._client.publish(
                    topic,
                    payload=event.model_dump_json(),
                    qos=qos,
                )
            except Exception:
                logger.exception("mqtt publish failed")
