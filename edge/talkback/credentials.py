from __future__ import annotations

"""
Talk-back credentials — ONE for the home, with a per-camera override.

Every camera in a home is paired to the same TP-Link account, so the account
password is a property of the HOME, not of a camera. It is entered once — in the
same camera setup a technician already does — and every camera uses it,
including one added next year. A per-camera entry still wins where it exists,
for the rare home whose cameras sit on two different TP-Link accounts.

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

# The home-wide entry lives in the same file under a key no camera can have:
# camera ids are labels (LOUNGE, cam_1), never wrapped in double underscores.
HOME = "__home__"


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


def _entry(password: str, source: str) -> dict:
    if not (password or "").strip():
        raise ValueError("password is required")
    raw = password.encode("utf-8")
    return {
        "md5": hashlib.md5(raw).hexdigest().upper(),
        "sha256": hashlib.sha256(raw).hexdigest().upper(),
        "updated_at": now_iso(),
        "source": source,
    }


def set_password(camera_id: str, password: str, source: str = "operator") -> None:
    """Store the hashes for ONE camera — the override. The plaintext ends here.

    Raises ValueError on an empty password so a stray blank cannot silently
    replace a working credential."""
    entry = _entry(password, source)
    with _lock:
        data = _read()
        data[camera_id] = entry
        _write(data)
    logger.info("talkback credential stored for %s (%s)", camera_id, source)


def set_home_password(password: str) -> list[str]:
    """Store THE home's TP-Link account password, for every camera.

    Clears the per-camera entries an operator typed, because they are the stale
    copies this replaces: a home is one TP-Link account, and a password change
    means every camera's old entry is wrong at once — which is exactly how both
    bench cameras started refusing together. Entries the device found for
    itself (source "stream") are kept; they were proven, not typed.
    Returns the camera ids whose override was cleared."""
    entry = _entry(password, "operator")
    with _lock:
        data = _read()
        cleared = [cid for cid, e in data.items()
                   if cid != HOME and (e or {}).get("source", "operator") == "operator"]
        for cid in cleared:
            del data[cid]
        data[HOME] = entry
        _write(data)
    logger.info("talkback home credential stored (cleared %d per-camera)", len(cleared))
    return cleared


def _lookup(data: dict, camera_id: str) -> tuple[dict, str]:
    """(entry, scope) — the camera's own entry if it has one, else the home's."""
    own = data.get(camera_id) or {}
    if own.get("md5") and own.get("sha256"):
        return own, "camera"
    home = data.get(HOME) or {}
    if home.get("md5") and home.get("sha256"):
        return home, "home"
    return {}, ""


def get(camera_id: str) -> TalkCredential | None:
    entry, _ = _lookup(_read(), camera_id)
    if not entry:
        return None
    return TalkCredential(md5=entry["md5"], sha256=entry["sha256"])


def updated_at(camera_id: str) -> str | None:
    entry, _ = _lookup(_read(), camera_id)
    return entry.get("updated_at")


def stamp() -> float:
    """When the credential file last changed (0 when there is none). Lets the
    running service notice a password set from the command line — a different
    process — without polling the file's contents."""
    try:
        return _FILE.stat().st_mtime
    except OSError:
        return 0.0


def home_configured() -> bool:
    return bool((_read().get(HOME) or {}).get("md5"))


def home_updated_at() -> str | None:
    return (_read().get(HOME) or {}).get("updated_at")


def forget_home() -> bool:
    with _lock:
        data = _read()
        if HOME not in data:
            return False
        del data[HOME]
        _write(data)
    logger.info("talkback home credential removed")
    return True


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
    return {cid for cid in summary() if cid != HOME}


def summary() -> dict[str, dict]:
    """Every stored entry and when it was set, from ONE read of the file,
    including the home entry under HOME. Use `resolve()` for "does camera X have
    a credential", which also applies the home fallback.

    Deliberately returns no hash material: this is what the inventory endpoint
    renders, and the only safe thing to put in an API response is the fact that
    a credential exists."""
    return {cid: {"updated_at": e.get("updated_at"),
                  "source": e.get("source", "operator")}
            for cid, e in _read().items() if (e or {}).get("md5")}


def resolve(camera_ids) -> dict[str, dict]:
    """For each camera: {configured, scope, updated_at, source}, applying the
    home fallback — from ONE read of the file."""
    data = _read()
    out = {}
    for cid in camera_ids:
        entry, scope = _lookup(data, cid)
        out[cid] = {"configured": bool(entry), "scope": scope,
                    "updated_at": entry.get("updated_at"),
                    "source": entry.get("source", "operator") if entry else None}
    return out
