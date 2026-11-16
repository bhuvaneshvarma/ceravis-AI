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
        self._started = False                # first write of this process: end a cut line

    def write(self, record: dict) -> None:
        """Append one record, stamped with `ts` first when it has none."""
        line = json.dumps({"ts": now_iso(), **record} if "ts" not in record else record,
                          ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    self._rotate()
                if not self._started:
                    line = _after_cut_line(self.path) + line
                    self._started = True
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            self._failed = False
        except OSError as exc:
            if not self._failed:
                logger.warning("log %s not written: %s", self.path, exc)
            self._failed = True

    def tail(self, lines: int) -> list[str]:
        """The newest `lines` lines, oldest first, across the rotated files. Reads
        only the end of each file, so a full 25 MB log costs a few kilobytes."""
        out: list[str] = []
        for path in [self.path] + [self._numbered(n) for n in range(1, self.backups + 1)]:
            if len(out) >= lines:
                break
            out = _last_lines(path, lines - len(out)) + out
        return out

    def version(self) -> str | None:
        """Changes whenever the log is written: its size and modification time."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return f"{st.st_size}-{st.st_mtime_ns}"

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


_BLOCK = 64 * 1024


def _after_cut_line(path: Path) -> str:
    """A newline when the file ends mid-line (a crash during a write), so the
    next record starts on its own line instead of being glued to the cut one."""
    try:
        with open(path, "rb") as fh:
            if fh.seek(0, os.SEEK_END) == 0:
                return ""
            fh.seek(-1, os.SEEK_END)
            return "" if fh.read(1) == b"\n" else "\n"
    except OSError:
        return ""


def _last_lines(path: Path, n: int) -> list[str]:
    """The last `n` lines of a file, read backwards in blocks from its end."""
    if n <= 0:
        return []
    try:
        with open(path, "rb") as fh:
            pos = fh.seek(0, os.SEEK_END)
            data = b""
            while pos > 0 and data.count(b"\n") <= n:
                step = min(_BLOCK, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
    except OSError:
        return []
    rows = data.decode("utf-8", errors="replace").splitlines()
    return (rows[1:] if pos > 0 else rows)[-n:]      # a block's first line may be cut
