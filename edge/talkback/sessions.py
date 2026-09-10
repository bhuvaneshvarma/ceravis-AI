from __future__ import annotations

"""
Who is allowed to talk, to which camera, right now.

A room has one speaker. Two people talking into it at once is not a degraded
experience, it is an unusable one — so this module is the single gate every
talk request passes through, and it holds exactly one rule: ONE live speaker
session per camera, first come, first served, with a hard idle timeout so a
browser tab that dies on a train does not leave a camera locked forever.

It also resolves a camera_id to a host, which is deliberately NOT a new field.
The camera's address is already recorded (rtsp_url, and the ONVIF XAddr behind
it); inventing a `talk_host` would create a second, quietly divergent idea of
where a camera lives.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from config.settings import settings
from configuration.camera_config import CameraConfig

from . import credentials
from .protocol import DEFAULT_PORT, TalkbackError, TapoTalkSession

logger = logging.getLogger("talkback.sessions")


def camera_host(camera) -> str:
    """Where this camera lives on the network, from what we already store.

    rtsp_url first (every camera has one and it is what the media backbone
    actually dials), then the ONVIF XAddr."""
    for url in (getattr(camera, "rtsp_url", ""), getattr(camera, "onvif_xaddr", "") or ""):
        host = urlparse(url).hostname if "://" in (url or "") else ""
        if host:
            return host
    return ""


@dataclass
class _Holder:
    """The single live session on one camera, plus who is holding it."""

    session: TapoTalkSession
    holder: str
    started: float = field(default_factory=time.monotonic)
    frames: int = 0


class TalkbackHub:
    """Process-wide. One instance, created at import; the API routes use it."""

    def __init__(self) -> None:
        self._active: dict[str, _Holder] = {}
        self._lock = asyncio.Lock()

    # -- inventory --------------------------------------------------------- #

    @staticmethod
    def cameras() -> list[dict]:
        """Every camera, with whether it is ready to be talked to. Never probes
        the network — this backs a page that renders on load."""
        # ONE read of the credential file for the whole list. Per-camera lookups
        # re-read it once per camera, and this backs a page that re-syncs.
        stored = credentials.summary()
        out = []
        for cam in CameraConfig().get_all():
            entry = stored.get(cam.camera_id) or {}
            out.append({
                "camera_id": cam.camera_id,
                "camera_name": cam.camera_name,
                "room_name": cam.room_name,
                "host": camera_host(cam),
                "configured": bool(entry),
                "credential_updated_at": entry.get("updated_at"),
                "enabled": cam.is_enabled,
            })
        return out

    def busy(self, camera_id: str) -> bool:
        return camera_id in self._active

    def status(self) -> dict:
        return {
            cid: {"holder": h.holder,
                  "seconds": round(time.monotonic() - h.started, 1),
                  "frames": h.frames}
            for cid, h in self._active.items()
        }

    # -- sessions ---------------------------------------------------------- #

    def _resolve(self, camera_id: str):
        """(host, credential) or a TalkbackError explaining exactly what is
        missing. Called before anything touches the network."""
        cam = CameraConfig().get_by_id(camera_id) or CameraConfig().get_by_label(camera_id)
        if cam is None:
            raise TalkbackError("no_camera", f"No camera called '{camera_id}'.")
        host = camera_host(cam)
        if not host:
            raise TalkbackError("no_host",
                                f"{cam.camera_name} has no usable address on file.")
        cred = credentials.get(cam.camera_id)
        if cred is None:
            raise TalkbackError(
                "no_credential",
                f"{cam.camera_name} has no talk-back credential yet. Add the "
                f"TP-Link account password for this camera first.")
        return cam, host, cred

    async def open(self, camera_id: str, holder: str) -> TapoTalkSession:
        """Take the camera's speaker. Raises TalkbackError('busy') rather than
        queueing: the caller is a person holding a button, and a queue would put
        their voice in the room at a moment they have stopped expecting it."""
        cam, host, cred = self._resolve(camera_id)
        async with self._lock:
            live = self._active.get(cam.camera_id)
            if live is not None:
                raise TalkbackError(
                    "busy",
                    f"Someone is already speaking to {cam.camera_name} "
                    f"({live.holder}).")
            session = TapoTalkSession(
                host, cred, port=settings.talkback_port,
                timeout=settings.talkback_timeout_secs, mode=settings.talkback_mode)
            # Placed under the lock BEFORE the network work, so a second request
            # arriving during the ~200 ms handshake is refused, not raced.
            self._active[cam.camera_id] = _Holder(session, holder)
        try:
            await session.open()
            await session.start_audio()
        except BaseException:
            async with self._lock:
                self._active.pop(cam.camera_id, None)
            await session.close()
            raise
        return session

    async def release(self, camera_id: str, session: TapoTalkSession) -> None:
        async with self._lock:
            live = self._active.get(camera_id)
            if live is not None and live.session is session:
                self._active.pop(camera_id, None)
        await session.close()

    def note_frame(self, camera_id: str) -> None:
        live = self._active.get(camera_id)
        if live is not None:
            live.frames += 1

    async def probe(self, camera_id: str) -> dict:
        """Prove the whole chain — reachable, credential accepted, speaker
        session granted — WITHOUT making a sound. This is the check a technician
        runs at handover, so it must never surprise a resident with a noise."""
        t0 = time.monotonic()
        session = await self.open(camera_id, holder="probe")
        try:
            return {
                "ok": True,
                "camera_id": camera_id,
                "host": session.host,
                "session_id": session.session_id,
                "elapsed_ms": round((time.monotonic() - t0) * 1000),
                "auth": ("sha256" if 'encrypt_type="3"' in session.challenge else "md5"),
            }
        finally:
            await self.release(camera_id, session)


hub = TalkbackHub()

__all__ = ["hub", "TalkbackHub", "TalkbackError", "camera_host", "DEFAULT_PORT"]
