from __future__ import annotations

"""
The device's own logs, newest first, for the monitor console (ui-testing/
monitor.html) — read-only, and only these files (edge/LOGS.md explains each):

    health     every minute: every part of the device (the newest is enough)
    events     only what changed: states, warnings, hotspot devices, check-ins
    fleet      every check-in with the fleet server and every order, whole
    incoming   every request into the device
    mediamtx   the video server's own log (text lines)

Cheap to poll: a log is read from its END only, and `since` (the `version` a
previous answer gave) answers `unchanged` without reading the file at all.
The app-server and talk-back logs have their own endpoints
(/api/v1/cloud/activity, /api/v1/talkback/log).
"""

import json

from fastapi import APIRouter, HTTPException

from common.logbook import Logbook, log_dir
from config.settings import settings

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])

MAX_LINES = 300
# name -> (where it is, inside the data folder; rotated copies kept)
_FILES = {
    "health": ("logs/health.jsonl", 10),
    "events": ("logs/events.jsonl", 4),
    "fleet": ("logs/fleet.jsonl", 4),
    "incoming": ("logs/incoming.jsonl", 5),
    "mediamtx": ("mediamtx.log", 0),
}
NAMES = "|".join(_FILES)          # api/auth opens exactly these on a bench


def _book(name: str) -> Logbook:
    rel, backups = _FILES[name]
    folder, _, file = rel.rpartition("/")
    base = log_dir(settings.data_path) if folder == "logs" else settings.data_path
    return Logbook(base / file, max_bytes=0, backups=backups)


@router.get("/{name}")
def read_log(name: str, limit: int = 50, since: str | None = None):
    if name not in _FILES:
        raise HTTPException(404, f"no log named {name!r}; one of: {', '.join(_FILES)}")
    book = _book(name)
    version = book.version()
    if version is None:
        return {"name": name, "version": None, "entries": []}
    if since and since == version:
        return {"name": name, "version": version, "unchanged": True}
    rows = book.tail(max(1, min(int(limit), MAX_LINES)))
    entries = []
    for row in reversed(rows):                 # newest first
        if name == "mediamtx":
            entries.append({"line": row})
            continue
        try:
            entries.append(json.loads(row))
        except ValueError:
            continue                           # a line cut by a crash mid-write
    return {"name": name, "version": version, "entries": entries}
