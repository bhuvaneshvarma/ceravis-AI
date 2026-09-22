from __future__ import annotations

"""
The talk log — who spoke into which room, when, and for how long.

One JSON line per event in `data/talkback_log.jsonl`:

    talk     a carer held a camera's floor: started_at, ended_at, talk_secs
             (seconds of actual speech), held_secs, and how it ended
    refused  a carer tried and was refused, and why (busy, password, offline)

The carer's `name` and `user_id` are what their app sent on the socket. The edge
cannot verify them (the edge_id is the only credential it checks), so the log is
as trustworthy as the app that connects — the same as every other field a client
supplies. The size is capped: past _MAX_BYTES the file rotates to `.1`, so the
last two files (~50k talks) are always there and the disk never fills.

Writing never raises: a full disk must not cost a carer their conversation.
"""

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("talkback.audit")

_DATA = Path(__file__).resolve().parents[1] / "data"
PATH = _DATA / "talkback_log.jsonl"
_MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


def record(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        with _lock:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            if PATH.exists() and PATH.stat().st_size >= _MAX_BYTES:
                os.replace(PATH, PATH.with_suffix(".jsonl.1"))
            with open(PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        logger.warning("talk log not written", exc_info=True)


def recent(camera_id: str | None = None, limit: int = 100) -> list[dict]:
    """The newest `limit` entries, newest first, optionally for one camera."""
    out: list[dict] = []
    for path in (PATH, PATH.with_suffix(".jsonl.1")):
        try:
            rows = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in reversed(rows):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if camera_id is None or row.get("camera_id") == camera_id:
                out.append(row)
                if len(out) >= limit:
                    return out
    return out
