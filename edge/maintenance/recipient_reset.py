"""
Removing the care recipient from this device — the ONE place that knows what on
the edge belongs to the person it watches over, and erases it.

The device stays exactly as it was set up: its fleet enrollment and edge_id,
the cameras (cameras.json) with what belongs to them — their room zones and the
talk-back camera password — the hotspot, the reboot password, every service.
What is about the care recipient goes:

  account      the care account signed in on this device (account.json)
  enrollment   photos, videos and the body / face recognition vectors
  events       alerts, their snapshots, and uploads still waiting to be sent
  recordings   the rolling video archive and its proof stills
  logs         the app-server call logs and the talk-back log (they hold the
               recipient's name and email, and who spoke into which room)
  sign-ins     every browser signed in for them

It is done in two steps, so nothing is erased under a running writer (the
event store, the recorder and the outbox all hold their files open):

  request()           at the API: leaves a marker. The caller takes the account
                      away at once (AccountConfig.clear) and restarts the
                      service (restart_soon).
  complete_pending()  at service start, BEFORE any component opens a file:
                      erases the rest, keeps only the sign-ins of the account
                      now on the device (none after a removal; the new
                      recipient's own after a switch), then drops the marker.
                      A start interrupted half-way finishes on the next one.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from common.atomic import write_atomic
from config.settings import resolve_path, settings

logger = logging.getLogger("recipient")

MARKER = "recipient_reset.json"

# What stays, in the words the confirmation shows.
KEPT = ("Cameras, their room zones and the talk-back camera password",
        "The fleet enrollment and this device's edge_id",
        "The camera hotspot, the reboot password and every service")


@dataclass(frozen=True)
class Part:
    """One kind of the recipient's data and where it lives."""
    key: str
    label: str
    paths: tuple[Path, ...]

    def size(self) -> tuple[int, int]:
        """(files, bytes) held now."""
        files = size = 0
        for p in self.paths:
            for f in ([p] if p.is_file() else p.rglob("*") if p.is_dir() else []):
                if f.is_file():
                    files += 1
                    size += f.stat().st_size
        return files, size

    def erase(self) -> int:
        """Delete every path; the number of files removed."""
        files, _ = self.size()
        for p in self.paths:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
        return files


def _data() -> Path:
    return resolve_path(settings.data_dir)


def parts() -> list[Part]:
    """Everything of the recipient's on this device, by kind. The names are the
    owners' own (tests/test_recipient_reset.py holds each to its owner)."""
    data = _data()
    db = resolve_path(settings.sqlite_path)
    logs = [f for pattern in ("cloud_calls.jsonl*", "ceravis_api_wire.jsonl*",
                              "talkback_log.jsonl*") for f in sorted(data.glob(pattern))]
    return [
        Part("enrollment", "Enrollment photos, videos and recognition data",
             (data / "recipients",)),
        Part("events", "Alerts, event snapshots and uploads waiting to be sent",
             (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"),
              resolve_path(settings.events_dir), data / "outbox")),
        Part("recordings", "Video recordings",
             (resolve_path(settings.record_dir), data / "recordings_proof")),
        Part("logs", "Server call logs and the talk-back log", tuple(logs)),
    ]


ACCOUNT_LABEL = "The care account, and every browser signed in for it"


def erased_labels() -> list[str]:
    """What a removal erases, in words (no counts — for a page not signed in)."""
    return [ACCOUNT_LABEL] + [p.label for p in parts()]


def inventory() -> list[dict]:
    """What would be erased now: [{key, label, files, bytes}]."""
    out = []
    for part in parts():
        files, size = part.size()
        out.append({"key": part.key, "label": part.label, "files": files, "bytes": size})
    return out


def pending() -> dict | None:
    f = _data() / MARKER
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, ValueError):
        return {"reason": "unreadable marker"}       # still pending: finish it


def request(reason: str, actor: str) -> None:
    """Mark the recipient's data for erasure at the next service start. The
    caller removes the account itself and restarts the service."""
    write_atomic(_data() / MARKER, json.dumps(
        {"requested_at": time.time(), "reason": reason, "actor": actor}).encode())
    logger.warning("care recipient removal requested (%s, by %s) — erased when the "
                   "service starts again", reason, actor)


def restart_soon() -> None:
    """Restart the service cleanly (the nightly refresh's own SIGUSR1), after the
    answer has gone out. Not under systemd (a dev PC): logged — the erasure then
    runs on the next start."""
    from maintenance.refresh import restart_service
    time.sleep(1.0)
    if restart_service():
        logger.warning("restarting the service to finish the care recipient removal")
    else:
        logger.warning("service restart NOT signalled (not under systemd) — the "
                       "removal finishes on the next start")


def complete_pending() -> dict | None:
    """At service start, before anything opens a file: finish a requested
    removal. Returns {key: files removed} when one ran, else None."""
    marker = pending()
    if marker is None:
        return None
    removed = {}
    for part in parts():
        try:
            removed[part.key] = part.erase()
        except OSError:
            logger.exception("care recipient removal: %s not fully erased — retried "
                             "on the next start", part.key)
            return removed
    from api.auth import sessions
    from config.account_config import AccountConfig
    keep = (AccountConfig().get().get("email") or "").strip()
    signed_out = sessions.revoke_all(keep_email=keep or None)
    (_data() / MARKER).unlink(missing_ok=True)
    logger.warning("care recipient data erased (%s): %s; %d browser sign-in(s) ended",
                   marker.get("reason", "?"),
                   ", ".join(f"{k} {n} file(s)" for k, n in removed.items()), signed_out)
    from integration import call_log
    call_log.record("event", True, label=f"INFO · Care recipient data removed from this "
                                         f"device ({marker.get('reason', '?')})")
    return removed
