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

from . import credentials, guard
from .protocol import DEFAULT_PORT, TalkbackError, TapoTalkSession

logger = logging.getLogger("talkback.sessions")

# How long past the hold window a session may sit with no activity before it is
# reclaimed. The WebSocket handler ends a held session at `talkback_hold_secs`
# by itself; this LEASE only ever catches a session nothing is ending — an
# orphan. It is the guarantee that no leak, present or future, can lock a room.
LEASE_GRACE_SECS = 30.0


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
    # The BROWSER's own id for this microphone, not an address. A carer whose
    # phone changes network keeps the same client_id and a different address,
    # which is exactly the case where they must be allowed back in — see
    # `open(takeover=...)`.
    client_id: str = ""
    # What OTHER carers are told when they are refused ("already speaking to
    # LOUNGE (Nurse Priya)"). A display name the client supplies, never the
    # address: an IP means nothing to a carer and is not theirs to see. The
    # address stays in `holder`, for /health and the logs.
    label: str = ""
    # The device's own silent self-check, not a person. A carer who presses
    # during one takes the camera from it instead of being told it is busy —
    # a background check must never cost anyone a sentence.
    preemptible: bool = False
    started: float = field(default_factory=time.monotonic)
    frames: int = 0
    # Refreshed by every frame of speech. A holder silent for longer than the
    # hold window plus LEASE_GRACE_SECS is not a person, it is a leak.
    last_activity: float = field(default_factory=time.monotonic)

    def expired(self, now: float) -> bool:
        return now - self.last_activity > settings.talkback_hold_secs + LEASE_GRACE_SECS


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
        cams = CameraConfig().get_all()
        stored = credentials.resolve([c.camera_id for c in cams])
        out = []
        for cam in cams:
            entry = stored.get(cam.camera_id) or {}
            out.append({
                "camera_id": cam.camera_id,
                "camera_name": cam.camera_name,
                "room_name": cam.room_name,
                "host": camera_host(cam),
                "configured": entry.get("configured", False),
                # "home" = the one TP-Link password for this home; "camera" = an
                # override for a camera on a different account.
                "credential_scope": entry.get("scope") or None,
                "credential_updated_at": entry.get("updated_at"),
                "enabled": cam.is_enabled,
            })
        return out

    def busy(self, camera_id: str) -> bool:
        return self.canonical(camera_id) in self._active

    @staticmethod
    def canonical(camera_id: str) -> str:
        """The id a camera is KEPT under. Callers may say `LIVING_ROOM`,
        `living room` or the raw id; every table here is keyed by one of them,
        and a lookup under a different spelling silently misses — which is how
        a session could be opened under one name and never released under
        another, leaving that camera "busy" until a restart."""
        cam = (CameraConfig().get_by_id(camera_id)
               or CameraConfig().get_by_label(camera_id))
        return cam.camera_id if cam is not None else camera_id

    def status(self) -> dict:
        out = {}
        for cid, h in self._active.items():
            now = time.monotonic()
            row = {"holder": h.holder,
                   "label": h.label,
                   "client_id": h.client_id,
                   "seconds": round(now - h.started, 1),
                   # The number that tells a live conversation from a leak.
                   "idle_seconds": round(now - h.last_activity, 1),
                   "frames": h.frames}
            # What the channel is actually costing right now. A carer who says
            # "it sounds delayed" and a fleet dashboard asking the same question
            # deserve the same number, from the same place.
            try:
                row.update(h.session.health())
            except Exception:
                pass
            out[cid] = row
        return out

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

    async def open(self, camera_id: str, holder: str,
                   client_id: str = "", preemptible: bool = False,
                   label: str = "", force: bool = False) -> TapoTalkSession:
        """Take the camera's speaker. Raises TalkbackError('busy') rather than
        queueing: the caller is a person holding a button, and a queue would put
        their voice in the room at a moment they have stopped expecting it.

        THE RECONNECT TRAP: a dropped WebSocket does not always tell this side
        it dropped. A carer whose phone changed cell would then be refused from
        their OWN session, by a socket that no longer exists, until the hold
        window expired — the exact moment reconnecting matters most. So a
        request carrying the SAME client_id as the current holder reclaims it
        instead of bouncing off it. The id is minted by the browser per
        microphone, so this can only ever hand a session back to the tab that
        already had it."""
        cam, host, cred = self._resolve(camera_id)
        # Before ANY connection: a camera that has refused this credential again
        # and again is paused, and asking it once more is how cameras lock
        # accounts out (talkback.guard). `force` is for a technician who knows.
        if not force:
            guard.check(cam.camera_id, cred, cam.camera_name)
        stale = None
        async with self._lock:
            live = self._active.get(cam.camera_id)
            if live is not None and live.expired(time.monotonic()):
                # An orphan (see LEASE_GRACE_SECS): give the room back to the
                # person asking for it now, instead of calling it busy.
                logger.warning("talk: reclaimed orphaned session on %s (idle %.0fs, "
                               "%d frames)", cam.camera_id,
                               time.monotonic() - live.last_activity, live.frames)
                stale = live
                self._active.pop(cam.camera_id, None)
                live = None
            if live is not None:
                if live.preemptible and not preemptible:
                    # A person outranks the self-check, always.
                    logger.info("talk takeover on %s by a carer (over the "
                                "self-check)", cam.camera_id)
                    stale = live
                    self._active.pop(cam.camera_id, None)
                elif preemptible:
                    # And a self-check never takes a camera off anyone.
                    raise TalkbackError("busy", f"{cam.camera_name} is in use.")
                elif client_id and live.client_id == client_id:
                    # The same microphone coming back. Take the old session out
                    # from under the lock and close it below — the camera has
                    # ONE speaker and will not grant a second while this holds.
                    logger.info("talk takeover on %s by returning client %s",
                                cam.camera_id, client_id)
                    stale = live
                    self._active.pop(cam.camera_id, None)
                else:
                    raise TalkbackError(
                        "busy",
                        f"Someone is already speaking to {cam.camera_name}"
                        + (f" ({live.label})." if live.label else "."))
            session = TapoTalkSession(
                host, cred, port=settings.talkback_port,
                timeout=settings.talkback_timeout_secs, mode=settings.talkback_mode)
            # Placed under the lock BEFORE the network work, so a second request
            # arriving during the ~200 ms handshake is refused, not raced.
            self._active[cam.camera_id] = _Holder(session, holder, client_id,
                                                  label=label,
                                                  preemptible=preemptible)
        if stale is not None:
            try:
                await stale.session.close()
            except Exception:
                logger.debug("stale talk session would not close", exc_info=True)
        try:
            await session.open()
            await session.start_audio()
        except BaseException as exc:
            async with self._lock:
                # Only OUR entry: a returning client may already have replaced it.
                live = self._active.get(cam.camera_id)
                if live is not None and live.session is session:
                    self._active.pop(cam.camera_id, None)
            await session.close()
            # Only a REFUSAL counts against the credential. A camera that was
            # offline or slow said nothing about the password.
            if isinstance(exc, TalkbackError) and exc.code == "unauthorized":
                guard.refused(cam.camera_id, cred)
            raise
        guard.accepted(cam.camera_id)
        return session

    async def release(self, camera_id: str, session: TapoTalkSession) -> None:
        """Give the camera back. Found by SESSION, not by the id the caller
        used: the id may be spelled differently from the one it was opened
        under, and a missed release keeps a household's speaker locked."""
        async with self._lock:
            for cid, live in list(self._active.items()):
                if live.session is session:
                    self._active.pop(cid, None)
                    break
        await session.close()

    def note_frame(self, camera_id: str) -> None:
        live = self._active.get(self.canonical(camera_id))
        if live is not None:
            live.frames += 1
            live.last_activity = time.monotonic()

    async def reap_stale(self) -> list[str]:
        """Reclaim every orphaned session (see LEASE_GRACE_SECS) and close its
        socket to the camera, so the speaker is free again for carers AND for
        the Tapo app. Called on the readiness tick; cheap when there is none."""
        now = time.monotonic()
        async with self._lock:
            dead = [(cid, h) for cid, h in self._active.items() if h.expired(now)]
            for cid, _ in dead:
                self._active.pop(cid, None)
        for cid, h in dead:
            logger.warning("talk: reclaimed orphaned session on %s (idle %.0fs, "
                           "%d frames)", cid, now - h.last_activity, h.frames)
            try:
                await h.session.close()
            except Exception:
                logger.debug("orphaned talk session would not close", exc_info=True)
        return [cid for cid, _ in dead]

    async def probe(self, camera_id: str, preemptible: bool = False,
                    force: bool = False) -> dict:
        """Prove the whole chain — reachable, credential accepted, speaker
        session granted — WITHOUT making a sound. This is the check a technician
        runs at handover, so it must never surprise a resident with a noise.

        `preemptible` marks the device's own background self-check: a carer who
        presses during it takes the camera, and it never takes one off a carer."""
        t0 = time.monotonic()
        session = await self.open(camera_id, holder="self-check" if preemptible
                                  else "probe", preemptible=preemptible, force=force)
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
