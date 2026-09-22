from __future__ import annotations

"""
Who may speak into which camera, right now — the FLOOR.

A carer talks to a camera over that camera's own socket
(`/api/v1/talkback/{camera_id}/stream`, one TALKER each). The camera side is the
edge's LINE (talkback.lines), kept open. The FLOOR — this module — is which
talker may feed a camera's line:

  * connecting to a camera claims its floor; so does speech arriving on an open
    socket whose floor had lapsed;
  * the floor is SPEAKING while speech arrives, and becomes HOLDING when it
    stops (`release`, the socket closing, or GAP_SECS without speech). It stays
    the carer's for `talkback_floor_hold_secs` so they can answer the resident,
    then it is free — however long the carer's socket stays open;
  * there is no limit on how long a carer speaks while holding the button.

One voice per CAMERA, deliberately: two instructions at once are unintelligible
to the person in the room, and "who said that" must always have one answer. Two
carers on two different cameras talk at the same time; so can one carer on two.
Listening has no such limit.

Every floor ends in the talk log (talkback.audit): who, which room, when, and how
many seconds of speech.

PRIORITY is future-proofing for a doctor role: a higher-priority talker takes the
floor from a lower one. Every talker is priority 0 today, so nothing is ever
taken over — it must only be raised from something the edge can verify (a signed
ticket from the app server), never from what a browser says.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from common.clock import now_iso
from config.settings import settings
from configuration.camera_config import CameraConfig

from . import audit, credentials
from .lines import camera_host, code_for, lines
from .mpegts import FRAME_BYTES, SAMPLE_RATE

logger = logging.getLogger("talkback.sessions")

# No speech for this long = the button is up. Speech arrives every 20 ms while it
# is held; a muted microphone sends digital silence or nothing at all.
GAP_SECS = 1.0
# A socket with no speech for this long is closed (1000). It holds no floor by
# then; this only stops forgotten tabs collecting sockets. Sent to clients as
# `hold_secs`, so a client closes its own side first.
IDLE_CLOSE_SECS = 60.0
_TICK = 0.25
_FRAME_SECS = FRAME_BYTES / SAMPLE_RATE
# G.711 A-law encodings of zero. A frame of nothing else is a muted microphone.
_SILENCE = b"\xd5\x55"


def _is_speech(frame: bytes) -> bool:
    return bool(frame.strip(_SILENCE))


@dataclass(eq=False)
class Talker:
    """One carer's microphone on one camera: one socket."""

    camera_id: str
    client_id: str                 # the carer's control: survives its reconnects
    name: str                      # the carer, as their app names them
    user_id: str                   # the app's own id for the carer, for the log
    holder: str                    # the address, for /health and the log
    end: Callable[[str, str], Awaitable[None]]    # refuse(code, message) + close
    close: Callable[[int, str], Awaitable[None]]
    priority: int = 0              # see PRIORITY in the module docstring
    last_speech: float = field(default_factory=time.monotonic)
    closing: bool = False


@dataclass(eq=False)
class Floor:
    camera_id: str
    talker: Talker
    speaking: bool = True          # False = holding, until hold_until
    started_at: str = field(default_factory=now_iso)
    since: float = field(default_factory=time.monotonic)
    last_speech: float = field(default_factory=time.monotonic)
    hold_until: float = 0.0
    speech_frames: int = 0


class TalkbackHub:
    """Process-wide. One instance (`hub`); the API routes use it."""

    def __init__(self) -> None:
        self._talkers: list[Talker] = []
        self._floors: dict[str, Floor] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # -- inventory --------------------------------------------------------- #

    @staticmethod
    def cameras() -> list[dict]:
        """Every camera and whether it is commissioned. Never touches the
        network — this backs pages that render on load."""
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
                "credential_scope": entry.get("scope") or None,
                "credential_updated_at": entry.get("updated_at"),
                "enabled": cam.is_enabled,
            })
        return out

    @staticmethod
    def canonical(camera_id: str) -> str:
        """The id a camera is KEPT under. Callers may say `LIVING_ROOM`,
        `living room` or the raw id; everything here is keyed by one of them."""
        cam = (CameraConfig().get_by_id(camera_id)
               or CameraConfig().get_by_label(camera_id))
        return cam.camera_id if cam is not None else camera_id

    @staticmethod
    def _name_of(camera_id: str) -> str:
        cam = CameraConfig().get_by_id(camera_id)
        return cam.camera_name if cam is not None else camera_id

    # -- what the pages see ------------------------------------------------ #

    def busy(self, camera_id: str) -> bool:
        return self.canonical(camera_id) in self._floors

    def floor(self, camera_id: str) -> dict:
        f = self._floors.get(camera_id)
        if f is None:
            return {"state": "free"}
        return {"state": "speaking" if f.speaking else "holding",
                "by": f.talker.name or "another carer",
                "client_id": f.talker.client_id}

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "talkers": len(self._talkers),
            "floors": {cid: {**self.floor(cid), "user_id": f.talker.user_id,
                             "holder": f.talker.holder, "since": f.started_at,
                             "seconds": round(now - f.since, 1),
                             "talk_secs": round(f.speech_frames * _FRAME_SECS, 1)}
                       for cid, f in self._floors.items()},
        }

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._task is None and settings.talkback_enabled:
            self._task = asyncio.create_task(self._tick(), name="talkback-floors")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for f in list(self._floors.values()):
            self._end(f, "shutdown")

    # -- a carer's socket -------------------------------------------------- #

    async def open(self, t: Talker) -> dict | None:
        """A carer connected to a camera: claim its floor and make sure the
        camera's line is up. None = granted; else a refusal {code, message}."""
        if CameraConfig().get_by_id(t.camera_id) is None:
            return self._refused(t, "no_camera", f"No camera called '{t.camera_id}'.")
        async with self._lock:
            refusal = self._take(t)
            if refusal is None:
                self._talkers.append(t)
        if refusal is not None:
            return refusal
        lines.set_clients(len(self._talkers))
        ok, state = await lines.ensure_open(t.camera_id, settings.talkback_timeout_secs)
        if ok:
            return None
        async with self._lock:
            f = self._floors.get(t.camera_id)
            if f is not None and f.talker is t:
                self._floors.pop(t.camera_id)     # nothing was said: not a talk
        await self.leave(t)
        r = lines.readiness(t.camera_id)
        return self._refused(t, code_for(state), r.get("detail") or r.get("message")
                             or "The camera's speaker is not available.")

    async def audio(self, t: Talker, frame: bytes) -> dict | None:
        """One frame from a carer. Forwarded while their floor is speaking.
        Speech on a socket whose floor had lapsed claims it again. Returns a
        refusal when the floor is someone else's or the camera is gone."""
        speech = _is_speech(frame)
        f = self._floors.get(t.camera_id)
        if f is None or f.talker is not t:
            if not speech:
                return None                        # silence never claims a room
            async with self._lock:
                refusal = self._take(t)
            if refusal is not None:
                return refusal
            f = self._floors[t.camera_id]
        now = time.monotonic()
        if speech:
            f.speaking = True
            f.last_speech = t.last_speech = now
            f.speech_frames += 1
        if not f.speaking:
            return None
        if await lines.send(t.camera_id, frame):
            return None
        state = lines.readiness(t.camera_id)["state"]
        if state in ("ready", "connecting"):
            return None                            # the line is coming straight back
        self._end(f, "line_lost")
        return self._refused(t, code_for(state), lines.readiness(t.camera_id)["message"])

    def release(self, t: Talker) -> None:
        """The button came up: the floor is held, then free."""
        f = self._floors.get(t.camera_id)
        if f is not None and f.talker is t and f.speaking:
            f.speaking = False
            f.hold_until = time.monotonic() + settings.talkback_floor_hold_secs

    async def leave(self, t: Talker) -> None:
        """The socket closed. Its floor is held, not dropped: a carer whose
        network dropped mid-sentence comes back to their own floor."""
        async with self._lock:
            if t in self._talkers:
                self._talkers.remove(t)
        self.release(t)
        lines.set_clients(len(self._talkers))

    # -- the floor --------------------------------------------------------- #

    def _take(self, t: Talker) -> dict | None:
        """Give `t` its camera's floor if it may have it. Call under the lock."""
        cid, now = t.camera_id, time.monotonic()
        f = self._floors.get(cid)
        if f is not None and not f.speaking and f.hold_until <= now:
            self._end(f, "released")
            f = None
        if f is not None and f.talker is t:
            return None
        if f is not None and f.talker.client_id == t.client_id:
            # The same carer's control reconnected: the room is still theirs.
            old, f.talker = f.talker, t
            if old in self._talkers and not old.closing:
                old.closing = True
                asyncio.ensure_future(old.close(1000, "replaced by a newer connection"))
            return None
        if f is not None:
            if t.priority <= f.talker.priority:
                who, room = f.talker.name or "Another carer", self._name_of(cid)
                if f.speaking:
                    return self._refused(t, "busy", f"{who} is speaking to {room}.")
                wait = max(1, round(f.hold_until - now))
                return self._refused(t, "busy", f"{who} was just speaking to {room}; "
                                                f"it is free in {wait} s.")
            taken = f.talker
            self._end(f, "taken")
            asyncio.ensure_future(taken.end(
                "taken", f"{t.name or 'Another carer'} took over {self._name_of(cid)}."))
        self._floors[cid] = Floor(cid, t)
        return None

    def _end(self, f: Floor, how: str) -> None:
        """A floor is over: free it and write the talk into the log."""
        if self._floors.get(f.camera_id) is f:
            self._floors.pop(f.camera_id)
        talk_secs = round(f.speech_frames * _FRAME_SECS, 1)
        t = f.talker
        logger.info("talk %s: %s spoke %.1fs (%s)", f.camera_id,
                    t.name or t.holder, talk_secs, how)
        audit.record({
            "event": "talk", "camera_id": f.camera_id,
            "camera_name": self._name_of(f.camera_id),
            "name": t.name, "user_id": t.user_id, "client_id": t.client_id,
            "holder": t.holder, "started_at": f.started_at, "ended_at": now_iso(),
            "talk_secs": talk_secs,
            "held_secs": round(f.last_speech - f.since, 1), "ended": how,
        })

    def _refused(self, t: Talker, code: str, message: str) -> dict:
        audit.record({
            "event": "refused", "camera_id": t.camera_id,
            "camera_name": self._name_of(t.camera_id),
            "name": t.name, "user_id": t.user_id, "client_id": t.client_id,
            "holder": t.holder, "at": now_iso(), "code": code, "message": message,
        })
        return {"code": code, "message": message}

    async def _tick(self) -> None:
        """Speech that stopped becomes a held floor; held floors expire; idle
        sockets close."""
        try:
            while True:
                await asyncio.sleep(_TICK)
                now = time.monotonic()
                for f in list(self._floors.values()):
                    if f.speaking and now - f.last_speech > GAP_SECS:
                        f.speaking = False
                        f.hold_until = f.last_speech + settings.talkback_floor_hold_secs
                    elif not f.speaking and f.hold_until <= now:
                        self._end(f, "released")
                held = {f.talker for f in self._floors.values()}
                for t in list(self._talkers):
                    if (not t.closing and t not in held
                            and now - t.last_speech > IDLE_CLOSE_SECS):
                        t.closing = True
                        asyncio.ensure_future(t.close(1000, "idle"))
        except asyncio.CancelledError:
            return


hub = TalkbackHub()

__all__ = ["hub", "TalkbackHub", "Talker", "Floor", "GAP_SECS", "IDLE_CLOSE_SECS",
           "camera_host"]
