from __future__ import annotations

"""
REMOVABLE ADD-ON — idle auto-revert for PTZ.

What it does: after a viewer pans/tilts a camera, a timer returns it to the
framing it held before the override once `ptz_revert_secs` pass with no further
PTZ request. Every PTZ action re-arms the window.

Why it is built to be thrown away: the cloud PTZ call now has
`action:"revert"`, which does the same thing on demand and under the app's
control. A timer that guesses when the viewer is finished is strictly worse than
the app saying so. This add-on stays only until the app team has switched over —
the monitor carries its ON/OFF switch and the 🗑 button that removes it for good.

    GET    /api/v1/ptz/autorevert          -> {present, enabled, idle_secs, pending}
    POST   /api/v1/ptz/autorevert/toggle   -> flip it (persisted)
    DELETE /api/v1/ptz/autorevert          -> DELETE THE FEATURE (?dry_run=1 to plan)

The delete is total and it is safe, because the feature was built for it:

  * ALL of its behaviour lives in THIS file. It attaches to the core through the
    generic `ptz_control.add_listener()` hook, so **the purge never rewrites the
    code that drives the motors** (api/ptz_control.py is not a target).
  * Everything it added elsewhere is three sentinel-marked blocks — the router
    include in main.py, `ptz_revert_secs` in config/settings.py, and the monitor
    panel in static/monitor.html. The purge strips exactly those, then deletes
    this file and its state.
  * It is validated before it is applied: every edit is computed and the result
    compiled in memory first, and if ANY file would come out broken nothing is
    written at all.

After it runs, `grep -ri autorevert edge/` is empty, a hard refresh has no
buttons, and a restart brings nothing back. Git is the undo.
"""

import json
import logging
import os
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api import ptz_control
from config.settings import settings
from integration import call_log
from schemas.cameras import Camera


router = APIRouter(prefix="/api/v1/ptz/autorevert", tags=["PTZ auto-revert"])
logger = logging.getLogger("cameras")

_EDGE_ROOT = Path(__file__).resolve().parents[1]

_lock = threading.RLock()
_timers: dict[str, threading.Timer] = {}     # camera_id -> pending idle revert
_purged = False                              # set by the delete; disables everything


def _state_file() -> Path:
    base = settings.data_path
    base = base if base.is_absolute() else (_EDGE_ROOT / base)
    return base / "ptz_autorevert.json"


def idle_secs() -> float:
    """Seconds of no PTZ traffic before the camera goes home. 0 = never."""
    return float(getattr(settings, "ptz_revert_secs", 0) or 0)


def _load_enabled() -> bool:
    try:
        return bool(json.loads(_state_file().read_text())["enabled"])
    except Exception:
        return idle_secs() > 0               # env default, like recording's switch


_enabled = _load_enabled()


def is_enabled() -> bool:
    with _lock:
        return _enabled and not _purged


def set_enabled(on: bool) -> bool:
    """Flip the feature (persisted, so a deliberate choice survives a restart).
    Turning it OFF drops every pending revert immediately — no camera moves on
    its own after the switch goes off."""
    global _enabled
    with _lock:
        _enabled = bool(on)
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabled": bool(on)}))
    except Exception:
        logger.exception("PTZ auto-revert: persisting the switch failed")
    if not on:
        _cancel_all()
    logger.info("PTZ auto-revert %s (monitor toggle)", "ENABLED" if on else "DISABLED")
    return is_enabled()


# ---- the timer itself -------------------------------------------------

def _cancel(camera_id: str) -> None:
    with _lock:
        timer = _timers.pop(camera_id, None)
    if timer is not None:
        timer.cancel()


def _cancel_all() -> None:
    with _lock:
        timers = list(_timers.values())
        _timers.clear()
    for timer in timers:
        timer.cancel()


def _fire(camera: Camera) -> None:
    with _lock:
        _timers.pop(camera.camera_id, None)
    if not is_enabled():
        return
    from onvif.soap import OnvifError
    try:
        if ptz_control.revert(camera):
            ptz_control.log(True, camera.camera_id,
                            "auto-revert: back to the recipient's framing (idle)", 200)
    except OnvifError as exc:
        ptz_control.log(False, camera.camera_id, f"auto-revert failed: {exc}", 502)


def on_ptz_activity(camera: Camera) -> None:
    """The hook the core calls after EVERY PTZ action: restart the idle window,
    so it can only elapse once the viewer has truly stopped touching the pad.
    Cancels instead when the camera is already home (nothing left to revert)."""
    if not is_enabled() or idle_secs() <= 0 or not ptz_control.has_home(camera.camera_id):
        _cancel(camera.camera_id)
        return
    _cancel(camera.camera_id)
    timer = threading.Timer(idle_secs(), _fire, args=(camera,))
    timer.daemon = True
    with _lock:
        _timers[camera.camera_id] = timer
    timer.start()


ptz_control.add_listener(on_ptz_activity)    # attach on import; main.py owns that


# =====================================================================
# SELF-DELETE — the 🗑 button on the monitor
# =====================================================================

_OPEN = ">>> ceravis:ptz-autorevert"
_CLOSE = "<<< ceravis:ptz-autorevert"

# Every file this feature touched outside its own module. Note what is NOT here:
# api/ptz_control.py — the core PTZ path is never rewritten by this.
_TARGETS = (
    _EDGE_ROOT / "main.py",                  # the router include
    _EDGE_ROOT / "config" / "settings.py",   # ptz_revert_secs
    _EDGE_ROOT / "static" / "monitor.html",  # the panel + its script
)
_SELF = Path(__file__).resolve()


def _strip_blocks(text: str) -> tuple[str, int]:
    """Drop every sentinel-delimited block, inclusive of its marker lines.
    Handles several blocks per file and a block opened and closed on one line.
    Raises ValueError on an unbalanced pair rather than guessing."""
    out: list[str] = []
    removed, inside = 0, False
    for line in text.splitlines(keepends=True):
        if not inside and _OPEN in line:
            removed += 1
            inside = _CLOSE not in line
            continue
        if inside:
            inside = _CLOSE not in line
            continue
        out.append(line)
    if inside:
        raise ValueError("unbalanced marker — an opening block is never closed")
    return "".join(out), removed


def _plan_file(path: Path) -> tuple[str, str | None]:
    """Compute this file's stripped content. Returns (report, new_text); new_text
    is None when there is nothing to write. A file that would end up broken is
    reported as an ERROR and never written — the caller aborts the whole purge."""
    name = path.name
    if not path.exists():
        return f"{name}: missing (skipped)", None
    text = path.read_text(encoding="utf-8")
    try:
        new, count = _strip_blocks(text)
    except ValueError as exc:
        return f"ERROR {name}: {exc}", None
    if not count:
        return f"{name}: no marked block (skipped)", None
    if path.suffix == ".py":
        try:
            compile(new, str(path), "exec")
        except SyntaxError as exc:
            return f"ERROR {name}: the result would not compile ({exc})", None
    if not os.access(path, os.W_OK):         # read-only checkout / wrong owner
        return f"ERROR {name}: not writable by this process", None
    return f"{name}: {count} block(s) removed", new


def _write(path: Path, text: str) -> None:
    """Atomic replace, LF preserved — a half-written source file is not a state
    this device is ever allowed to boot from."""
    tmp = path.with_name(path.name + ".purge-tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _delete_own_files() -> list[str]:
    done = []
    for path in (_state_file(), _SELF):
        try:
            if path.exists():
                path.unlink()
                done.append(f"{path.name}: deleted")
        except OSError as exc:
            done.append(f"WARNING {path.name}: {exc}")
    cache = _SELF.parent / "__pycache__"
    stale_pyc = sorted(cache.glob(f"{_SELF.stem}.*.pyc")) if cache.is_dir() else []
    for stale in stale_pyc:                  # a sourceless .pyc can't be imported,
        try:                                 # but leaving one behind is just litter
            stale.unlink()
            done.append(f"{stale.name}: deleted")
        except OSError:
            pass
    return done


@router.get("")
def autorevert_state():
    if _purged:
        raise HTTPException(404, "auto-revert has been removed from this device")
    with _lock:
        pending = sorted(_timers)
    return {"present": True, "enabled": is_enabled(), "idle_secs": idle_secs(),
            "pending": pending}


@router.post("/toggle")
def autorevert_toggle():
    if _purged:
        raise HTTPException(404, "auto-revert has been removed from this device")
    return {"enabled": set_enabled(not is_enabled())}


@router.delete("")
def autorevert_delete(dry_run: bool = False):
    """Remove the feature — code, switch, buttons and state. `?dry_run=1` reports
    exactly what would go, and changes nothing.

    Two phases on purpose: PLAN every edit and compile-check it first, and only
    if all of them are clean, apply. A file that cannot be edited safely aborts
    the whole thing with nothing touched."""
    global _purged
    if _purged:
        return {"status": "already-removed", "removed": [], "warnings": []}

    plan = [(path, *_plan_file(path)) for path in _TARGETS]
    problems = [report for _p, report, _t in plan if report.startswith("ERROR")]
    if problems:
        raise HTTPException(409, "auto-revert NOT removed — nothing was written: "
                                 + "; ".join(problems))
    if dry_run:
        return {"status": "planned", "removed": [r for _p, r, _t in plan]
                + [f"{p.name}: would be deleted" for p in (_state_file(), _SELF)],
                "warnings": []}

    # Runtime first: stop being a live feature before the sources change, so no
    # timer can fire against half-removed code.
    _purged = True
    ptz_control.remove_listener(on_ptz_activity)
    _cancel_all()

    removed, warnings = [], []
    for path, report, new_text in plan:
        if new_text is None:
            removed.append(report)
            continue
        try:
            _write(path, new_text)
            removed.append(report)
        except OSError as exc:
            warnings.append(f"WARNING {path.name}: {exc}")
    removed += _delete_own_files()

    logger.warning("PTZ auto-revert REMOVED — %s", "; ".join(removed))
    call_log.record("ptz", True, status=200,
                    label="auto-revert feature deleted from this device "
                          f"({len(removed)} items)")
    return {"status": "removed", "removed": removed, "warnings": warnings,
            "note": "the switch and this button are gone on refresh; the "
                    "on-demand action:'revert' is untouched. git restores "
                    "everything if this was a mistake."}
