from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(prefix="/api/v1/events", tags=["Events"])


@router.get("")
def recent_events(request: Request, limit: int = 50,
                  camera_id: str | None = None):
    """Most recent rule-engine events (falls, postures, visitors), newest first."""
    event_store = getattr(request.app.state, "event_store", None)
    if event_store is None:
        raise HTTPException(503, "event store not running")
    return event_store.recent(limit=min(int(limit), 500), camera_id=camera_id)
