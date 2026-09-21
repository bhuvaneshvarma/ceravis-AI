from __future__ import annotations

"""
Stop our own retries from locking a camera out.

On 2026-09-21 the bench cameras collected ~25 refused logins in an afternoon, and
almost none of them were a person deciding to try again: a refused socket looked
like a network blip, so the browser retried it; a vague failure triggered a
diagnostic test; the self-check re-tried on schedule; diagnose tried twelve
shapes, twice. Cameras lock accounts out for exactly that pattern, and a locked
camera is a second fault stacked on the first.

The rule: a camera that has refused the SAME credential several times in a row is
not asked again until a cool-down passes. A person who changes the password —
or re-enters it, which is a deliberate act — resets it at once. Anything else
that would dial the camera is refused locally with a sentence saying why and
until when, and costs the camera nothing.

    refusals in a row   paused for
    1 - 2               not paused (typos happen)
    3                   15 min
    4                   30 min
    5                   60 min
    6+                  3 h

The state is shared across PROCESSES, because the command line runs outside the
service and its attempts count just the same. So it lives in a small file next to
the credentials, and the pause is a wall-clock epoch: a monotonic deadline means
nothing to another process, or after the restart it has to survive. That is the
same reason the outbox stores `next_attempt` as an epoch (tests/test_time_unified
DEADLINE_EXEMPT). No secret is stored: the credential is represented by a short
fingerprint of its hash, enough to notice that it CHANGED.
"""

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from common.clock import local_tz
from datetime import datetime

from .protocol import TalkbackError

logger = logging.getLogger("talkback.guard")

_DATA = Path(__file__).resolve().parents[1] / "data"
_FILE = _DATA / "talkback_guard.json"
_lock = threading.Lock()

FREE_REFUSALS = 2
# Pause after the 3rd, 4th, 5th and 6th-or-later refusal in a row.
_PAUSE_SECS = (15 * 60, 30 * 60, 60 * 60, 3 * 60 * 60)


def fingerprint(credential) -> str:
    """Enough to tell that a credential changed; useless for anything else."""
    material = getattr(credential, "sha256", "") or ""
    return hashlib.sha256(("guard:" + material).encode()).hexdigest()[:12]


def _read() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("talkback_guard.json unreadable — starting clean", exc_info=True)
        return {}


def _write(data: dict) -> None:
    _DATA.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, _FILE)
    try:
        os.chmod(_FILE, 0o600)
    except OSError:
        pass


def _pause_for(refusals: int) -> float:
    if refusals <= FREE_REFUSALS:
        return 0.0
    return float(_PAUSE_SECS[min(refusals - FREE_REFUSALS, len(_PAUSE_SECS)) - 1])


def _at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, local_tz()).strftime("%H:%M")


def paused_until(camera_id: str, credential) -> float:
    """Epoch until which this camera must not be dialled with this credential,
    or 0 when it may be."""
    row = _read().get(camera_id) or {}
    if row.get("fp") != fingerprint(credential):
        return 0.0                      # a different credential starts clean
    until = float(row.get("until") or 0)
    return until if until > time.time() else 0.0


def check(camera_id: str, credential, name: str = "") -> None:
    """Raise TalkbackError('cooldown') if this camera is paused. Called before
    ANY connection to the camera, so a paused camera costs nothing."""
    until = paused_until(camera_id, credential)
    if not until:
        return
    row = _read().get(camera_id) or {}
    raise TalkbackError(
        "cooldown",
        f"Talk-back on {name or camera_id} is paused until {_at(until)} after "
        f"{row.get('refusals', FREE_REFUSALS + 1)} refused logins in a row, so our "
        f"own retries cannot lock the camera out. Fix the password (entering it "
        f"again resumes at once), or wait.")


def refused(camera_id: str, credential, count: int = 1) -> float:
    """Record `count` refusals of this credential. Returns the pause epoch (0 when
    not paused yet)."""
    fp = fingerprint(credential)
    with _lock:
        data = _read()
        row = data.get(camera_id) or {}
        if row.get("fp") != fp:
            row = {"fp": fp, "refusals": 0}
        row["refusals"] = int(row.get("refusals", 0)) + max(1, count)
        pause = _pause_for(row["refusals"])
        row["until"] = time.time() + pause if pause else 0
        data[camera_id] = row
        _write(data)
    if pause:
        logger.warning("talk-back %s paused %d min after %d refusals in a row",
                       camera_id, pause // 60, row["refusals"])
    return row["until"]


def accepted(camera_id: str) -> None:
    """The camera took the credential: forget every refusal."""
    with _lock:
        data = _read()
        if data.pop(camera_id, None) is not None:
            _write(data)


def reset(camera_ids=None) -> None:
    """A person changed or re-entered a password: clear the pause for those
    cameras (all of them for the home password)."""
    with _lock:
        data = _read()
        if camera_ids is None:
            changed = bool(data)
            data = {}
        else:
            changed = False
            for cid in camera_ids:
                changed |= data.pop(cid, None) is not None
        if changed:
            _write(data)


def snapshot() -> dict:
    """For /health: per camera, refusals in a row and the pause, if any."""
    now = time.time()
    out = {}
    for cid, row in _read().items():
        until = float(row.get("until") or 0)
        out[cid] = {"refusals_in_a_row": int(row.get("refusals", 0)),
                    "paused_until": _at(until) if until > now else None}
    return out
