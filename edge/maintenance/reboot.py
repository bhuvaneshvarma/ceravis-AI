from __future__ import annotations

"""
The ONE owner of device reboot — scheduled, manual, and everything that guards
either. Nothing else in the tree shells out to `reboot`.

Three things live here because they are one decision, not three:

  AUTHORISATION  A manual reboot needs the reboot password. The edge_id match
                 that authenticates every other control endpoint is NOT enough
                 here: the edge_id travels in the URL path of every fleet
                 request (`/<edge_id>/api/v1/...`), so anyone who has seen one
                 request can replay it. Reboot is the single most destructive
                 operation a care device exposes, so it gets a real secret.
                 Stored only as a PBKDF2-SHA256 hash, compared in constant time,
                 and rate-limited so the hash cannot be brute-forced online.

  SAFETY         A care device must not reboot while a fall alert is still
                 undelivered. The outbox is durable so nothing is LOST across a
                 reboot, but delivery is delayed by the boot time, and that
                 delay is exactly what an alert cannot afford. The scheduled
                 reboot therefore DEFERS to the next night; a manual reboot can
                 override deliberately, because a human is asking.

  ACCOUNTABILITY Every reboot writes a marker before it goes down and reports
                 it on the way back up, so "the device restarted" is a fact in
                 the logs and on the monitor console rather than an inference
                 from a gap in the timeline.

The SCHEDULED reboot deliberately does not take a password: it is the device
restarting itself on a timer nobody is present for. Its authorisation is that
systemd owns the timer, and root owns systemd.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import clock
from config.settings import settings
from integration import call_log


logger = logging.getLogger("maintenance.reboot")

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 260_000
_SALT_BYTES = 16
_MIN_PASSWORD_LEN = 8

_DATA = Path(__file__).resolve().parents[1] / "data"
_AUTH_FILE = _DATA / "reboot_auth.json"
_MARKER_FILE = _DATA / "last_reboot.json"

_lock = threading.Lock()


# =====================================================================
# Password
# =====================================================================

def _read_auth() -> dict:
    try:
        return json.loads(_AUTH_FILE.read_text())
    except Exception:
        return {}


def _write_auth(data: dict) -> None:
    _DATA.mkdir(parents=True, exist_ok=True)
    tmp = _AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, _AUTH_FILE)
    try:
        os.chmod(_AUTH_FILE, 0o600)          # hash file is not world-readable
    except OSError:
        pass                                  # best-effort (e.g. non-POSIX dev box)


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def has_password() -> bool:
    a = _read_auth()
    return bool(a.get("hash") and a.get("salt"))


def set_password(password: str) -> None:
    """Set or replace the reboot password. Raises ValueError if too weak.

    The plaintext is never stored, never logged, and never leaves this call."""
    if len(password or "") < _MIN_PASSWORD_LEN:
        raise ValueError(
            f"reboot password must be at least {_MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _ITERATIONS)
    _write_auth({
        "algo": _ALGO,
        "iterations": _ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
        "set_at": clock.now_iso(),
        "failures": 0,
        "locked_until": None,
    })
    logger.info("reboot password set")


def lockout_remaining() -> float:
    """Seconds until the lockout lifts; 0.0 when not locked out."""
    a = _read_auth()
    until = a.get("locked_until")
    if not until:
        return 0.0
    try:
        left = (datetime.fromisoformat(until) - datetime.now(timezone.utc)
                ).total_seconds()
    except Exception:
        return 0.0
    return max(0.0, left)


def verify_password(password: str) -> tuple[bool, str | None]:
    """(ok, error). Constant-time, and rate-limited after repeated failures so
    an attacker on the LAN cannot grind the hash."""
    a = _read_auth()
    if not (a.get("hash") and a.get("salt")):
        return False, ("no reboot password is set on this device — "
                       "run setup/set_reboot_password.py")

    left = lockout_remaining()
    if left > 0:
        return False, f"too many failed attempts — locked for {int(left)}s"

    try:
        salt = base64.b64decode(a["salt"])
        expected = base64.b64decode(a["hash"])
        iterations = int(a.get("iterations") or _ITERATIONS)
    except Exception:
        logger.exception("reboot auth file unreadable")
        return False, "reboot auth file is corrupt — re-run set_reboot_password.py"

    ok = hmac.compare_digest(_derive(password or "", salt, iterations), expected)

    with _lock:
        a = _read_auth()                      # re-read: another worker may have written
        if ok:
            a["failures"] = 0
            a["locked_until"] = None
        else:
            a["failures"] = int(a.get("failures") or 0) + 1
            if a["failures"] >= settings.reboot_max_attempts:
                a["locked_until"] = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=settings.reboot_lockout_secs)).isoformat()
                a["failures"] = 0
                logger.warning("reboot: locked out after %d failed attempts",
                               settings.reboot_max_attempts)
        _write_auth(a)

    return (True, None) if ok else (False, "incorrect reboot password")


# =====================================================================
# Safety
# =====================================================================

def safety_block(outbox=None) -> str | None:
    """None when it is safe to reboot, else the human reason it is not.

    Takes the outbox directly rather than app.state, so the API (which has the
    live in-process one) and the 03:00 timer script (which opens the same SQLite
    file from its own process) ask the identical question of the identical data.

    The only blocker is an undelivered critical alert. A reboot does not LOSE
    one — the outbox is durable — but it delays delivery by the boot time, and
    that is precisely the delay an alert cannot afford."""
    if not settings.reboot_defer_on_pending_alerts:
        return None
    if outbox is None:
        return None                           # nothing to consult
    try:
        pending = int(outbox.stats().get("pending_alerts") or 0)
    except Exception:
        logger.exception("reboot: could not read outbox stats")
        return None                           # never block on a broken probe
    if pending > 0:
        return (f"{pending} undelivered alert(s) still queued for the cloud — "
                f"rebooting now would delay them by the boot time")
    return None


# =====================================================================
# Execution + accountability
# =====================================================================

def _write_marker(reason: str, actor: str) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        tmp = _MARKER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "at": clock.now_iso(), "reason": reason, "actor": actor,
        }, indent=2))
        os.replace(tmp, _MARKER_FILE)
    except Exception:
        logger.exception("reboot: could not write the marker")


def boot_report() -> dict | None:
    """Called once at startup. Returns the marker left by the reboot that
    brought us down (and clears it), so the restart is a logged FACT rather
    than an inference from a gap in the timeline."""
    try:
        if not _MARKER_FILE.exists():
            return None
        data = json.loads(_MARKER_FILE.read_text())
        _MARKER_FILE.unlink()
    except Exception:
        logger.exception("reboot: could not read the boot marker")
        return None
    logger.info("device is back up after a %s reboot requested at %s by %s",
                data.get("reason", "?"), data.get("at", "?"),
                data.get("actor", "?"))
    call_log.record(
        "event", True,
        label=f"INFO · Device restarted · {data.get('reason', 'reboot')}")
    return data


def perform(reason: str, actor: str, *, delay_secs: float | None = None) -> None:
    """Log it, mark it, then reboot on a short delay.

    The delay exists so the HTTP response reaches the caller and the log line
    reaches disk before the kernel goes down — a reboot nobody was told about
    is indistinguishable from a crash."""
    delay = settings.reboot_delay_secs if delay_secs is None else delay_secs
    logger.warning("REBOOT requested (%s by %s) — going down in %.0fs",
                   reason, actor, delay)
    call_log.record("event", True,
                    label=f"INFO · Device rebooting · {reason} ({actor})")
    _write_marker(reason, actor)

    def _go() -> None:
        try:
            subprocess.run(settings.reboot_command.split(), timeout=30,
                           check=False)
        except Exception:
            logger.exception("reboot command failed — device stayed up")

    t = threading.Timer(delay, _go)
    t.daemon = True
    t.start()


# =====================================================================
# Status
# =====================================================================

def _systemd_timer_status() -> dict:
    """What systemd ACTUALLY has scheduled — not what we intended. A status
    surface that reports intent instead of reality hides a timer that never
    got installed."""
    out: dict = {"installed": False, "next_run": None, "last_run": None}
    try:
        r = subprocess.run(
            ["systemctl", "show", "ceravis-reboot.timer",
             "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSecRealtime",
             "-p", "LoadState"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return out                            # not a systemd box (dev machine)
    if r.returncode != 0:
        return out
    props = dict(
        line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    out["installed"] = props.get("LoadState") == "loaded"
    for key, name in (("NextElapseUSecRealtime", "next_run"),
                      ("LastTriggerUSecRealtime", "last_run")):
        raw = (props.get(key) or "").strip()
        if raw and raw not in ("0", "n/a", "infinity"):
            out[name] = raw
    return out


def status(outbox=None) -> dict:
    """Everything an operator needs to answer 'will this device reboot tonight,
    and can I reboot it now'."""
    marker = None
    try:
        if _MARKER_FILE.exists():
            marker = json.loads(_MARKER_FILE.read_text())
    except Exception:
        marker = None
    block = safety_block(outbox)
    lock_left = lockout_remaining()
    return {
        "scheduled": {
            "enabled": settings.reboot_scheduled_enabled,
            "window": (f"{settings.reboot_window_start_hour:02d}:00–"
                       f"{settings.reboot_window_start_hour + 1:02d}:00 "
                       f"device-local, randomised"),
            **_systemd_timer_status(),
        },
        "password": {
            "set": has_password(),
            "locked_out": lock_left > 0,
            "lockout_secs_remaining": round(lock_left, 1),
        },
        "safe_to_reboot": block is None,
        "blocked_reason": block,
        "pending_marker": marker,
    }
