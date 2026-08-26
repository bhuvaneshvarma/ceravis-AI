from __future__ import annotations

from threading import RLock

from reid.identity_schema import Identity


class IdentityBuffer:
    """Per-camera latest identity-resolution result (one per track)."""

    __slots__ = ("_lock", "_identities")

    def __init__(self) -> None:
        self._lock = RLock()
        # camera_id -> { track_id -> Identity }
        self._identities: dict[str, dict[int, Identity]] = {}

    def update(self, identity: Identity) -> None:
        with self._lock:
            self._identities.setdefault(identity.camera_id, {})[
                identity.track_id
            ] = identity

    def get(self, camera_id: str, track_id: int) -> Identity | None:
        with self._lock:
            return self._identities.get(camera_id, {}).get(track_id)

    def get_all(self) -> dict[str, dict[int, Identity]]:
        with self._lock:
            return {k: dict(v) for k, v in self._identities.items()}

    def prune(self, camera_id: str, alive_ids: set[int]) -> None:
        """Drop identities of tracks that no longer exist. Without this the map
        grows forever, keyed on a monotonically rising track_id — the exact leak
        shape that bit PostureBuffer before. Mirrors TrackFeatureBuffer.prune."""
        with self._lock:
            per = self._identities.get(camera_id)
            if not per:
                return
            for tid in [t for t in per if t not in alive_ids]:
                per.pop(tid, None)
