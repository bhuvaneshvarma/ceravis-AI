"""Every request INTO the device, in <data dir>/logs/incoming.jsonl.

Two kinds of line (what every field means: edge/LOGS.md):

    request   one call that changed something or failed: every write
              (POST/PUT/PATCH/DELETE) and every answer of 400 or more —
              method, path, query parameter NAMES, status, duration, origin
    polling   successful reads (GET/HEAD) counted per path over about a minute:
              the UI pages poll several times a second, and one line each would
              bury everything else (the journal measured ~65k such lines a day)

Never logged: request or response bodies, query VALUES, headers, cookies — they
can carry passwords, tokens or personal data. The origin says how the call came:
`fleet` (through the tunnel, by this device's edge_id), `local` (this device
itself) or `lan` (the home network).
"""
from __future__ import annotations

import threading
import time
from collections import Counter

from common.logbook import Logbook, log_dir
from config.settings import settings

_SUMMARY_SECS = 60.0


class RequestLog:
    def __init__(self, book: Logbook) -> None:
        self._book = book
        self._lock = threading.Lock()
        self._polling: Counter = Counter()
        self._since = time.monotonic()

    def record(self, method: str, path: str, status: int, duration_ms: float,
               origin: str, client: str | None, query_names: list[str]) -> None:
        if method in ("GET", "HEAD") and status < 400:
            with self._lock:
                self._polling[f"{method} {path}"] += 1
        else:
            self._book.write({"kind": "request", "method": method, "path": path,
                              "query": query_names, "status": status,
                              "duration_ms": round(duration_ms, 1),
                              "origin": origin, "client": client})
        self.flush()

    def flush(self, force: bool = False) -> None:
        """Write the polling counts once their window is over (or now, on stop)."""
        with self._lock:
            span = time.monotonic() - self._since
            if not self._polling or (span < _SUMMARY_SECS and not force):
                return
            counts, self._polling = dict(self._polling), Counter()
            self._since = time.monotonic()
        self._book.write({"kind": "polling", "window_secs": round(span),
                          "requests": dict(sorted(counts.items()))})


requests = RequestLog(Logbook(log_dir(settings.data_path) / "incoming.jsonl",
                              max_bytes=10 * 1024 * 1024, backups=5))
