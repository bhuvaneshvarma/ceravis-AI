from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse


router = APIRouter(prefix="/api/v1/events", tags=["Events"])


@router.get("")
def recent_events(request: Request, limit: int = 50,
                  camera_id: str | None = None):
    """Most recent rule-engine events (falls, postures, welfare), newest first."""
    event_store = getattr(request.app.state, "event_store", None)
    if event_store is None:
        raise HTTPException(503, "event store not running")
    return event_store.recent(limit=min(int(limit), 500), camera_id=camera_id)


@router.get("/snapshot")
def event_snapshot_by_path(path: str, request: Request):
    """Serve a snapshot by its (sanitized) relative path."""
    enricher = getattr(request.app.state, "event_enricher", None)
    if enricher is None:
        raise HTTPException(503, "events not running")
    f = enricher.snapshot_file(path)
    if f is None:
        raise HTTPException(404, "snapshot not found")
    return FileResponse(str(f), media_type="image/jpeg")


@router.get("/{event_id}/snapshot")
def event_snapshot(event_id: str, request: Request):
    """Serve the annotated JPEG captured when the event fired."""
    store = getattr(request.app.state, "event_store", None)
    enricher = getattr(request.app.state, "event_enricher", None)
    if store is None or enricher is None:
        raise HTTPException(503, "events not running")
    rel = store.snapshot_path(event_id)
    if not rel:
        raise HTTPException(404, "no snapshot for this event")
    f = enricher.snapshot_file(rel)
    if f is None:
        raise HTTPException(404, "snapshot file missing")
    return FileResponse(str(f), media_type="image/jpeg")

