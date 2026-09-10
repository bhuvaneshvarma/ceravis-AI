from __future__ import annotations

"""
Per-camera talk-back credentials.

The camera's speaker endpoint authenticates with HTTP Digest, where the password
is a hash of the TP-LINK CLOUD ACCOUNT password — NOT the RTSP/ONVIF account we
already keep in cameras.json, and not the edge_id. Which hash depends on what
the camera's challenge asks for (`encrypt_type="3"` -> SHA256, otherwise MD5),
and that can change under a firmware update.

So we store the two hashes and NEVER the password itself. The plaintext lives
only inside the request that sets it: it is hashed in `set_password`, is not
logged, is not echoed back by any endpoint, and never touches disk. A copy of
data/talkback.json is enough to talk to the camera — it is not enough to log in
to anyone's TP-Link account, which is the part that actually matters.

One file, 0600, alongside the other device secrets (see maintenance.reboot for
the same treatment of the reboot password).
"""

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from common.clock import now_iso

logger = logging.getLogger("talkback.credentials")

_DATA = Path(__file__).resolve().parents[1] / "data"
_FILE = _DATA / "talkback.json"
_lock = threading.Lock()


@dataclass(frozen=True)
class TalkCredential:
    """What the camera's Digest challenge can ask for. `username` is what TP-Link
    expects for a cloud-password login on every firmware we have seen."""

    md5: str
    sha256: str
    username: str = "admin"

    def password_for(self, challenge: str) -> tuple[str, str]:
        """(username, digest-password) for this challenge.

        Old firmware with a fixed built-in account (CVE-2022-37255) advertises
        `username="none"` and takes the published constant instead — handled
        here so the caller never branches on firmware."""
        if 'username="none"' in challenge:
            return "none", "TPL075526460603"
        return self.username, self.sha256 if 'encrypt_type="3"' in challenge else self.md5


def _read() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("talkback.json unreadable — treating as empty", exc_info=True)
        return {}


def _write(data: dict) -> None:
    _DATA.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, _FILE)                     # atomic: never a half-written secret
    try:
        os.chmod(_FILE, 0o600)
    except OSError:
        pass                                    # best-effort (non-POSIX dev box)


def set_password(camera_id: str, password: str) -> None:
    """Store the hashes for one camera. The plaintext ends here.

    Raises ValueError on an empty password so a stray blank cannot silently
    replace a working credential."""
    if not (password or "").strip():
        raise ValueError("password is required")
    raw = password.encode("utf-8")
    with _lock:
        data = _read()
        data[camera_id] = {
            "md5": hashlib.md5(raw).hexdigest().upper(),
            "sha256": hashlib.sha256(raw).hexdigest().upper(),
            "updated_at": now_iso(),
        }
        _write(data)
    logger.info("talkback credential stored for %s", camera_id)


def get(camera_id: str) -> TalkCredential | None:
    entry = _read().get(camera_id) or {}
    if not (entry.get("md5") and entry.get("sha256")):
        return None
    return TalkCredential(md5=entry["md5"], sha256=entry["sha256"])


def updated_at(camera_id: str) -> str | None:
    return (_read().get(camera_id) or {}).get("updated_at")


def forget(camera_id: str) -> bool:
    with _lock:
        data = _read()
        if camera_id not in data:
            return False
        del data[camera_id]
        _write(data)
    logger.info("talkback credential removed for %s", camera_id)
    return True


def configured() -> set[str]:
    return set(summary())


def summary() -> dict[str, dict]:
    """Every commissioned camera and when it was set, from ONE read of the file.

    Deliberately returns no hash material: this is what the inventory endpoint
    renders, and the only safe thing to put in an API response is the fact that
    a credential exists."""
    return {cid: {"updated_at": e.get("updated_at")}
            for cid, e in _read().items() if e.get("md5")}
