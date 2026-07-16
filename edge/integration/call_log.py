from __future__ import annotations

"""
Rolling log of every CERAVIS app-server call — what the monitor's Cloud Sync
Console displays. The pipeline also logs each DETECTION here (endpoint
"event": ok=True when it was raised, ok=False with an `error` when the cloud
gate dropped it), so a physical test reads on screen as
    EVENT detected -> saveAlert / saveSnapshot hit (or the drop reason).

File-backed (data/cloud_calls.jsonl) rather than in-memory on purpose, so a
separate diagnostic process can append to the same console the service reads.
Each line is one JSON record:

    {ts, endpoint, ok, status, latency_ms, label, alert_id, link, error}

`link` is any http(s) URL found in the server's response (e.g. the uploaded
snapshot's S3 URL) so the console can render "view snapshot". Best-effort
everywhere — a logging hiccup must never break a cloud call.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings


logger = logging.getLogger("integration")

_EDGE_ROOT = Path(__file__).resolve().parents[1]
_LOCK = threading.Lock()

_MAX_BYTES = 512 * 1024      # rotate past this…
_KEEP_LINES = 300            # …down to the newest N records

# Who is making the calls in THIS process: "live" = the service's real
# pipeline (actual falls/transitions/stillness). A separate diagnostic process
# may override this to "test", and the console renders a TEST chip on those so
# real events stay unmistakable during end-to-end checks.
SOURCE = "live"


def _file() -> Path:
    base = settings.data_path
    base = base if base.is_absolute() else (_EDGE_ROOT / base)
    return base / "cloud_calls.jsonl"


def find_link(payload) -> str | None:
    """First http(s) URL anywhere in a response payload (dict/list/str)."""
    if isinstance(payload, str):
        return payload if payload.startswith(("http://", "https://")) else None
    if isinstance(payload, dict):
        for v in payload.values():
            link = find_link(v)
            if link:
                return link
    if isinstance(payload, list):
        for v in payload:
            link = find_link(v)
            if link:
                return link
    return None


def record(endpoint: str, ok: bool, *, status: int | None = None,
           latency_ms: float | None = None, label: str | None = None,
           alert_id=None, link: str | None = None,
           error: str | None = None) -> None:
    """Append one call record. Never raises."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,   # userDetails|saveCamera|saveAlert|saveSnapshot|event
        "source": SOURCE,                # "live" (real pipeline) | "test" (test_cloud)
        "ok": bool(ok),
        "status": status,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "label": (label or "")[:300],
        "alert_id": alert_id,
        "link": link,
        "error": (error or "")[:300] or None,
    }
    try:
        f = _file()
        with _LOCK:
            f.parent.mkdir(parents=True, exist_ok=True)
            with f.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            if f.stat().st_size > _MAX_BYTES:
                lines = f.read_text(encoding="utf-8").splitlines()[-_KEEP_LINES:]
                f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("cloud call-log write failed")


def recent(limit: int = 100) -> list[dict]:
    """Newest-first records for the console."""
    try:
        f = _file()
        if not f.exists():
            return []
        with _LOCK:
            lines = f.read_text(encoding="utf-8").splitlines()
        out = []
        for line in reversed(lines[-max(int(limit), 1):]):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
    except Exception:
        logger.exception("cloud call-log read failed")
        return []
