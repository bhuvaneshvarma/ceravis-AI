from __future__ import annotations

"""
Who may speak into which camera, right now — the FLOOR.

Three things used to be one: a carer's connection, the camera's talk session,
and the right to speak. Holding one held all three, for 90 s after the last
word, so a carer who had gone quiet still locked the room. They are separate now:

  * a carer's page keeps ONE standby connection to the edge (talkback_routes'
    /session). It claims nothing and blocks nobody;
  * the camera's talk session is the edge's LINE (talkback.lines), kept open;
  * the FLOOR — this module — is who may feed a camera's line. It is taken on a
    press, and kept `talkback_floor_hold_secs` after release so the carer can
    answer the resident without another carer cutting in. Then it is free.

There is no limit on how long a carer speaks while holding the button. What ends
a floor is the carer letting go, their page going away, or their audio stopping
(`STALL_SECS`: a button held in a page that has frozen is not a person speaking).

One voice per camera at a time, deliberately: two instructions at once are
unintelligible to the person in the room, and "who said that" must always have
one answer. Listening has no such limit.

PRIORITY is future-proofing for a doctor role: a claim from a higher-priority
client takes the floor from a lower one. Every client is priority 0 today, so
nothing is ever taken over — it must only be raised from something the edge can
verify (a signed ticket from the app server), never from what a browser says.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from config.settings import settings
from configuration.camera_config import CameraConfig

from . import credentials
from .lines import camera_host, code_for, lines

logger = logging.getLogger("talkback.sessions")

# A floor whose speaker has sent no audio for this long is released. The page
# sends a frame every 20 ms for as long as the button is held, so three seconds
# of nothing is a frozen page or a dead network, not a person talking.
STALL_SECS = 3.0
_TICK = 0.25


@dataclass(eq=False)
class Client:
    """One carer's page, connected by its standby WebSocket."""

    client_id: str                 # the page's own id: survives its reconnects
    name: str                      # shown to other carers ("Nurse Priya")
    holder: str                    # the address, for /health and the logs only
    push: Callable[[dict], Awaitable[None]]
    close: Callable[[int, str], Awaitable[None]]
    priority: int = 0              # see PRIORITY in the module docstring
    connected_at: float = field(default_factory=time.monotonic)


@dataclass(eq=False)
class Floor:
    camera_id: str
    client: Client
    speaking: bool = True          # False = released, inside the floor hold
    pending: bool = True           # waiting for the camera line to open
    since: float = field(default_factory=time.monotonic)
    last_frame: float = field(default_factory=time.monotonic)
    hold_until: float = 0.0
    frames: int = 0


class TalkbackHub:
    """Process-wide. One instance (`hub`); the API routes use it."""

    def __init__(self) -> None:
        self._clients: list[Client] = []
        self._floors: dict[str, Floor] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        lines.on_change = self._camera_changed

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
                "by": f.client.name or "another carer",
                "client_id": f.client.client_id}

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "clients": len(self._clients),
            "floors": {cid: {**self.floor(cid), "holder": f.client.holder,
                             "seconds": round(now - f.since, 1), "frames": f.frames}
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

    # -- clients ----------------------------------------------------------- #

    @staticmethod
    def new_client_id() -> str:
        return "srv-" + uuid.uuid4().hex[:12]

    async def connect(self, client: Client) -> None:
        """A page connected. If the SAME page (same client_id) was already
        connected — its network dropped and it came back before the edge noticed
        — the new connection replaces the old one and inherits its floor, so a
        carer mid-sentence keeps the room."""
        async with self._lock:
            old = [c for c in self._clients if c.client_id == client.client_id]
            for c in old:
                self._clients.remove(c)
                for f in self._floors.values():
                    if f.client is c:
                        f.client = client
            self._clients.append(client)
        for c in old:
            await c.close(4000, "replaced by a newer connection from this page")
        lines.set_clients(len(self._clients))

    async def disconnect(self, client: Client) -> None:
        """A page went away. Its floor is released, not dropped: it stays the
        page's for the floor hold, so a page that reconnects in time carries on."""
        async with self._lock:
            if client not in self._clients:
                return                          # already replaced
            self._clients.remove(client)
            mine = [f for f in self._floors.values() if f.client is client]
        for f in mine:
            await self._release(f)
        lines.set_clients(len(self._clients))

    # -- the floor --------------------------------------------------------- #

    async def claim(self, client: Client, camera_id: str) -> dict:
        """A press. Returns {"type": "granted"} or {"type": "refused", ...}."""
        cid = self.canonical(camera_id)
        if CameraConfig().get_by_id(cid) is None:
            return _refused(cid, "no_camera", f"No camera called '{camera_id}'.")
        name = self._name_of(cid)
        taken = None
        async with self._lock:
            # One microphone per page: taking a floor lets go of any other.
            for f in list(self._floors.values()):
                if f.client is client and f.camera_id != cid:
                    self._floors.pop(f.camera_id, None)
                    asyncio.ensure_future(self._announce(f.camera_id))
            f = self._floors.get(cid)
            now = time.monotonic()
            if f is not None and not f.speaking and f.hold_until <= now:
                self._floors.pop(cid, None)
                f = None
            if f is not None and f.client.client_id != client.client_id:
                if client.priority <= f.client.priority:
                    who = f.client.name or "Another carer"
                    if f.speaking:
                        return _refused(cid, "busy", f"{who} is speaking to {name}.")
                    wait = max(1, round(f.hold_until - now))
                    return _refused(cid, "busy", f"{who} was just speaking to {name}; "
                                                 f"it is free in {wait} s.")
                taken = f.client
            # Reserved BEFORE waiting for the line, so a second press arriving
            # meanwhile is refused rather than raced.
            f = Floor(cid, client)
            self._floors[cid] = f

        ok, state = await lines.ensure_open(cid, settings.talkback_timeout_secs)
        if not ok:
            async with self._lock:
                if self._floors.get(cid) is f:
                    self._floors.pop(cid, None)
            readiness = lines.readiness(cid)
            return _refused(cid, code_for(state),
                            readiness.get("detail") or readiness.get("message")
                            or "The camera's speaker is not available.")
        f.pending = False
        f.last_frame = time.monotonic()
        if taken is not None:
            logger.info("talk floor %s taken from %s by %s (priority)",
                        cid, taken.name or taken.holder, client.name or client.holder)
            await _push(taken, {"type": "taken", "camera": cid,
                                "by": client.name or "another carer"})
        await self._announce(cid)
        return {"type": "granted", "camera": cid}

    async def release(self, client: Client) -> None:
        """The button came up."""
        for f in [f for f in self._floors.values() if f.client is client and f.speaking]:
            await self._release(f)

    async def audio(self, client: Client, frame: bytes) -> str | None:
        """One frame of speech. Returns the camera it went to, or None when this
        page does not hold a floor it is speaking on (dropped)."""
        for f in self._floors.values():
            if f.client is client and f.speaking and not f.pending:
                f.last_frame = time.monotonic()
                f.frames += 1
                await lines.send(f.camera_id, frame)
                return f.camera_id
        return None

    async def _release(self, f: Floor) -> None:
        f.speaking = False
        f.hold_until = time.monotonic() + settings.talkback_floor_hold_secs
        await self._announce(f.camera_id)

    async def _tick(self) -> None:
        """Floor holds expire, and a speaker whose audio stopped is released."""
        try:
            while True:
                await asyncio.sleep(_TICK)
                now = time.monotonic()
                for f in list(self._floors.values()):
                    if f.speaking and not f.pending and now - f.last_frame > STALL_SECS:
                        logger.info("talk floor %s: no audio for %.0fs, released",
                                    f.camera_id, now - f.last_frame)
                        await self._release(f)
                        await _push(f.client, {"type": "released", "camera": f.camera_id,
                                               "reason": "no audio"})
                    elif not f.speaking and f.hold_until <= now:
                        if self._floors.get(f.camera_id) is f:
                            self._floors.pop(f.camera_id, None)
                            await self._announce(f.camera_id)
        except asyncio.CancelledError:
            return

    # -- telling every page ------------------------------------------------ #

    async def _announce(self, camera_id: str) -> None:
        await self.broadcast({"type": "floor", "camera": camera_id,
                              **self.floor(camera_id)})

    async def broadcast(self, message: dict) -> None:
        for c in list(self._clients):
            await _push(c, message)

    def _camera_changed(self, camera_id: str, readiness: dict) -> None:
        if self._clients:
            asyncio.ensure_future(self.broadcast(
                {"type": "camera", "camera": camera_id, "readiness": readiness}))


def _refused(camera_id: str, code: str, message: str) -> dict:
    return {"type": "refused", "camera": camera_id, "code": code, "message": message}


async def _push(client: Client, message: dict) -> None:
    try:
        await client.push(message)
    except Exception:
        pass                       # a page that went away is cleaned up by disconnect()


hub = TalkbackHub()

__all__ = ["hub", "TalkbackHub", "Client", "Floor", "STALL_SECS", "camera_host"]
