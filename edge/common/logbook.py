"""The device's own logs: one JSON object per line, in one folder, one rule.

    <data dir>/logs/health.jsonl     every part of the device, measured each minute
    <data dir>/logs/events.jsonl     only what CHANGED (a part's state, a warning,
                                     a restart, a hotspot device joining or leaving)
    <data dir>/logs/fleet.jsonl      every check-in with the fleet server, every order
    <data dir>/logs/incoming.jsonl   every request INTO the device

The first three are written by the fleet agent, the last by the edge app; both
use this class, so every log rotates the same way and never fills the disk:
past `max_bytes` the file becomes `.1`, `.1` becomes `.2` … and the oldest
beyond `backups` is deleted. What every field means: edge/LOGS.md.

Standard library only (the fleet agent imports it too). Writing never raises:
a full or read-only disk must never cost the device its real work.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from common import clock

logger = logging.getLogger("logbook")

LOG_FOLDER = "logs"


def log_dir(data_dir) -> Path:
    """The ONE folder of the device's own logs, inside its data folder."""
    return Path(data_dir) / LOG_FOLDER


def now_iso() -> str:
    """Device-local time with its offset — the edge's one clock (common/clock)."""
    return clock.now().isoformat(timespec="milliseconds")


class Logbook:
    """An append-only JSON-lines file with size-based rotation."""

    def __init__(self, path, max_bytes: int, backups: int) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._failed = False                 # a write failure is logged once, not every line

    def write(self, record: dict) -> None:
        """Append one record, stamped with `ts` first when it has none."""
        line = json.dumps({"ts": now_iso(), **record} if "ts" not in record else record,
                          ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    self._rotate()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            self._failed = False
        except OSError as exc:
            if not self._failed:
                logger.warning("log %s not written: %s", self.path, exc)
            self._failed = True

    def tail(self, lines: int) -> list[str]:
        """The newest `lines` lines, oldest first, across the rotated files."""
        out: list[str] = []
        for path in [self.path] + [self._numbered(n) for n in range(1, self.backups + 1)]:
            if len(out) >= lines:
                break
            try:
                rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            out = rows[-(lines - len(out)):] + out
        return out

    def _numbered(self, n: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{n}")

    def _rotate(self) -> None:
        for n in range(self.backups, 0, -1):
            older = self._numbered(n)
            if n == self.backups:
                older.unlink(missing_ok=True)
            elif older.exists():
                os.replace(older, self._numbered(n + 1))
        os.replace(self.path, self._numbered(1))
