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

    def demote_stale_targets(self, camera_id: str, keep_ids: set[int]) -> None:
        """Clear the is_target flag from any STILL-LIVE track that is no longer
        the target — a released lock (mismatch) or the target having moved to a
        new track_id. The flag is otherwise sticky: identities are published only
        for the target, so a track the lock let go keeps is_target=True forever,
        and both the monitor's green dot and every recipient-scoped rule
        (find_recipient) keep following the wrong person. `keep_ids` are the
        track_ids that ARE the target this tick; every other flagged track is
        dropped (a non-target track simply carries no identity record)."""
        with self._lock:
            per = self._identities.get(camera_id)
            if not per:
                return
            for tid in [t for t, i in per.items()
                        if i.is_target and t not in keep_ids]:
                per.pop(tid, None)
