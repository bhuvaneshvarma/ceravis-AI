from __future__ import annotations

"""
Where event snapshot JPEGs live — the ONE resolver shared by the writer
(EventEnricher), the cloud publisher (reads them back for saveSnapshot), and
the API route that serves them. Layout is S3-mirrored:
<events_dir>/<device_id>/<date>/<event_id>.jpg, so a cloud cutover stays a
path swap.
"""

from pathlib import Path

from config.settings import resolve_path, settings


def events_root() -> Path:
    return resolve_path(settings.events_dir)


def snapshot_file(rel_path: str) -> Path | None:
    """Resolve a stored (relative) snapshot path to an existing file, or None.
    Rejects absolute / parent-escaping paths — this also guards the API route
    that serves snapshots by path."""
    safe = Path(rel_path)
    if safe.is_absolute() or ".." in safe.parts:
        return None
    f = events_root() / safe
    return f if f.exists() else None
