"""Who this device is to the fleet: its hardware fingerprint, and the edge_id +
signing key it received when it enrolled."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import uuid
from pathlib import Path

# The Jetson module's serial survives a re-flash (the OS machine-id does not),
# so the fleet recognises the same box after its disk is rebuilt.
_SERIAL_SOURCES = ("/sys/firmware/devicetree/base/serial-number",
                   "/proc/device-tree/serial-number",
                   "/etc/machine-id")


def fingerprint(override: str = "") -> str:
    source = override
    for path in () if override else _SERIAL_SOURCES:
        try:
            source = Path(path).read_text(encoding="utf-8", errors="replace").strip("\x00 \n")
        except OSError:
            continue
        if source:
            break
    source = source or f"{socket.gethostname()}:{uuid.getnode():012x}"   # non-Linux dev box
    return hashlib.sha256(f"ceravis-fleet:{source}".encode("utf-8")).hexdigest()


def load(state_dir: Path) -> dict | None:
    try:
        data = json.loads((state_dir / "identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("edge_id") and data.get("secret") else None


def save(state_dir: Path, identity: dict) -> None:
    """Atomic and private: a crash mid-write can't leave half a key, and only
    the agent's own user can read it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "identity.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(identity, fh, indent=2)
    os.replace(tmp, state_dir / "identity.json")
