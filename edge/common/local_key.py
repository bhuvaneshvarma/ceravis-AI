"""
The device's local key: how a program ON this device proves to the API that it
is one of ours — not merely something that can reach 127.0.0.1 (any process on
the box, a browser opened on the device, a forwarded port).

One random key in <data>/local.key, mode 0600, owned by the service account:
readable by the service, by root — the tools and update.sh run with sudo — and
by nobody else. Made when the service starts, kept across restarts. A caller
sends it as the X-Ceravis-Local-Key header (headers()); api/auth checks it in
constant time (matches()).
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
from pathlib import Path

from common.atomic import write_atomic
from config.settings import settings

logger = logging.getLogger("auth")

HEADER = "X-Ceravis-Local-Key"


class LocalKey:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._key: str | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or settings.data_path / "local.key"

    def ensure(self) -> str:
        """The key, made now if this device has none yet (the service calls it
        at start)."""
        with self._lock:
            key = self._read()
            if not key:
                key = secrets.token_urlsafe(32)
                write_atomic(self.path, key, mode=0o600)
                logger.info("local key created: %s", self.path)
            self._key = key
            return key

    def _read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def matches(self, offered: str | bytes | None) -> bool:
        if not offered:
            return False
        if isinstance(offered, bytes):
            offered = offered.decode("latin-1")
        key = self._key or self._read()
        return bool(key) and hmac.compare_digest(offered.strip(), key)

    def headers(self) -> dict[str, str]:
        """The header a local caller sends; empty when it may not read the key
        (then it is treated like any other client)."""
        key = self._key or self._read()
        return {HEADER: key} if key else {}


local_key = LocalKey()
