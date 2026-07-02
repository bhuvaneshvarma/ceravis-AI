from __future__ import annotations

"""
Runtime ON/OFF switch for forwarding to the CERAVIS app server.

ON  = alerts + snapshots hit the API exactly as in production.
OFF = nothing hits the API; the would-be payloads are written locally to
      data/cloud_outbox/ (see local_outbox) so you can test the full
      detection -> snapshot flow without flooding the DB / S3.

Persisted to data/cloud_mode.json so a test session stays OFF across restarts
until you flip it back on. Thread-safe (the publisher thread reads it; the API
thread writes it).
"""

import json
import threading
from pathlib import Path

from config.settings import settings


_EDGE_ROOT = Path(__file__).resolve().parents[1]


class CloudSwitch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        base = settings.data_path
        base = base if base.is_absolute() else (_EDGE_ROOT / base)
        self._file = base / "cloud_mode.json"
        self._on = self._load()

    def _load(self) -> bool:
        try:
            return bool(json.loads(self._file.read_text()).get("on", True))
        except Exception:
            return True                              # default ON (production)

    def is_on(self) -> bool:
        with self._lock:
            return self._on

    def set(self, on: bool) -> bool:
        with self._lock:
            self._on = bool(on)
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                self._file.write_text(json.dumps({"on": self._on}))
            except Exception:
                pass
            return self._on

    def toggle(self) -> bool:
        return self.set(not self.is_on())


# process-wide singleton
switch = CloudSwitch()
