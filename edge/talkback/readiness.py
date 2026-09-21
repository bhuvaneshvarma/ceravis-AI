from __future__ import annotations

"""
Is every camera's speaker actually ready — known BEFORE anyone presses a button.

Talk-back used to find out it was broken at the worst possible moment: a carer
holds the button, and only then does the camera refuse the credential. This
module runs with the rest of the device from boot. It silently checks every
camera, keeps the answer, and re-checks on a schedule — so the live wall can say
"needs the TP-Link password" before the press, and a camera that was re-paired
in the Tapo app comes back to "ready" on its own without anyone touching this
device.

The check is the same silent probe a technician runs at handover (hub.probe): it
opens a speaker session and closes it without sending audio. No sound in the
room. It is PRE-EMPTIBLE — a carer who presses during it takes the camera — and
it never takes a camera off a carer.

States a camera can be in:

    unknown         not checked yet (the first minute after boot)
    ready           the camera granted a speaker session
    needs_password  no credential exists for it at all
    rejected        the camera refused the credential we hold
    unreachable     no answer on the talk port (offline, rebooting)
    unsupported     something answered, but not a Tapo talk endpoint

How often, and why those numbers:

    ready           every 10 min  — catches a password change before a carer does
    rejected        every 60 min  — slow ON PURPOSE: repeated failed logins are
                                    what cameras lock accounts out for; this is
                                    only here to notice a re-pair in the Tapo app
    unreachable     every 2 min   — a camera that is rebooting comes back fast
    unsupported     every 60 min
    needs_password  never on a timer — nothing to try until a password arrives

Any change to a credential re-checks everything immediately (kick()).

FIRST, TRY WHAT WE ALREADY HOLD. A camera with no talk-back credential gets one
attempt, once per boot, with its OWN stream password — the one already in
cameras.json from camera setup. If the firmware accepts that, the camera is
commissioned with no input at all. It costs one local round-trip, and at most
one failed login per camera per boot.
"""

import asyncio
import hashlib
import logging
import time
from urllib.parse import urlparse

from common.clock import now_iso
from config.settings import settings
from configuration.camera_config import CameraConfig

from . import credentials
from .protocol import TalkbackError, attempt
from .sessions import camera_host, hub

logger = logging.getLogger("talkback.readiness")

_EVERY = {
    "ready": 600.0,
    "rejected": 3600.0,
    "unreachable": 120.0,
    "unsupported": 3600.0,
    "unknown": 120.0,
}
_TICK = 15.0            # how often the loop looks for a camera that is due
_BOOT_DELAY = 20.0      # let the media backbone come up before we add traffic

# What a probe failure code means for readiness.
_STATE_FOR = {
    "unauthorized": "rejected",
    "no_credential": "needs_password",
    "unreachable": "unreachable",
    "timeout": "unreachable",
    "stalled": "unreachable",
    "closed": "unreachable",
    "refused": "unsupported",
    "protocol": "unsupported",
}

# Words for the carer, keyed by state. The device's own sentence (`detail`) is
# kept alongside for the technician.
_SAY = {
    "ready": "Ready.",
    "needs_password": "Talk-back needs this home's TP-Link account password.",
    # Deliberately says "the stored" password, not "this home's": a camera can
    # be on its own override, and a message that names the wrong password sends
    # someone to fix the wrong thing.
    "rejected": ("The camera refused the stored TP-Link password. Enter the "
                 "current Tapo app password; if it is still refused, remove the "
                 "camera in the Tapo app and add it again — it is holding an "
                 "old copy."),
    "unreachable": "The camera's speaker is not answering right now.",
    "unsupported": "This camera does not offer talk-back.",
    "unknown": "Not checked yet.",
}


class Readiness:
    """Process-wide. One instance (`readiness`), started from the app lifespan."""

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}
        self._due: dict[str, float] = {}
        self._stream_tried: set[str] = set()
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None

    # -- what the API reads ------------------------------------------------ #

    def get(self, camera_id: str) -> dict:
        row = self._state.get(camera_id)
        if row is None:
            return {"state": "unknown", "message": _SAY["unknown"],
                    "detail": "", "checked_at": None}
        return dict(row)

    def all(self) -> dict[str, dict]:
        return {cid: dict(row) for cid, row in self._state.items()}

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._task is not None or not settings.talkback_enabled:
            return
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task = asyncio.create_task(self._run(), name="talkback-readiness")
        logger.info("talk-back readiness: checking every camera from boot")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    def kick(self) -> None:
        """A credential changed: every camera is due NOW."""
        self._due.clear()
        if self._wake is not None:
            self._wake.set()

    async def check_now(self) -> dict[str, dict]:
        """Check every camera immediately and return the verdicts — what an
        operator who just typed a password is waiting to see."""
        self._due.clear()
        await self._sweep(force=True)
        return self.all()

    # -- the loop ---------------------------------------------------------- #

    async def _run(self) -> None:
        try:
            await asyncio.sleep(_BOOT_DELAY)
            seen = credentials.stamp()
            while True:
                # A password set from ANYWHERE — this API, or `tools.talkback
                # set` in another process — changes the file. Any change means
                # every camera deserves a fresh look now, not in an hour.
                stamp = credentials.stamp()
                if stamp != seen:
                    seen = stamp
                    self._due.clear()
                try:
                    await self._sweep()
                except Exception:
                    # A self-check must never take talk-back down with it.
                    logger.warning("talk-back readiness sweep failed", exc_info=True)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), _TICK)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def _sweep(self, force: bool = False) -> None:
        # One camera at a time, never in parallel: the load stays a single short
        # TCP session, and the lock stops a forced check racing the timer.
        # Created lazily so an on-demand check works even before start().
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            cams = [c for c in CameraConfig().get_all() if c.is_enabled]
            known = {c.camera_id for c in cams}
            for gone in [cid for cid in self._state if cid not in known]:
                self._state.pop(gone, None)
                self._due.pop(gone, None)
            now = time.monotonic()
            for cam in cams:
                if not force and self._due.get(cam.camera_id, 0.0) > now:
                    continue
                await self._check(cam)

    async def _check(self, cam) -> None:
        cid = cam.camera_id
        if credentials.get(cid) is None:
            if cid not in self._stream_tried:
                self._stream_tried.add(cid)
                if await self._try_stream_password(cam):
                    return await self._check(cam)
            self._record(cid, "needs_password", "")
            self._due[cid] = float("inf")      # nothing to try until kick()
            return

        if hub.busy(cid):
            # Somebody is talking, which is the best possible proof. Leave the
            # state alone and look again soon.
            self._due[cid] = time.monotonic() + _EVERY["unreachable"]
            return

        try:
            result = await hub.probe(cid, preemptible=True)
        except TalkbackError as exc:
            if exc.code == "busy":
                self._due[cid] = time.monotonic() + _EVERY["unreachable"]
                return
            state = _STATE_FOR.get(exc.code, "unreachable")
            self._record(cid, state, str(exc))
        except Exception as exc:
            logger.debug("talk-back check of %s crashed", cid, exc_info=True)
            self._record(cid, "unreachable", str(exc))
        else:
            self._record(cid, "ready", "", elapsed_ms=result.get("elapsed_ms"))
        self._due[cid] = time.monotonic() + _EVERY.get(
            self._state[cid]["state"], _EVERY["unknown"])

    async def _try_stream_password(self, cam) -> bool:
        """One attempt with the camera's own stream password. True = it worked
        and is now stored as this camera's credential."""
        user = (getattr(cam, "onvif_username", "") or "").strip()
        pw = (getattr(cam, "onvif_password", "") or "").strip()
        if not pw:
            parsed = urlparse(getattr(cam, "rtsp_url", "") or "")
            user, pw = (parsed.username or ""), (parsed.password or "")
        host = camera_host(cam)
        if not (pw and host):
            return False
        try:
            _, challenge = await attempt(host, settings.talkback_port,
                                         timeout=settings.talkback_timeout_secs)
            if not challenge.startswith("Digest"):
                return False
            raw = pw.encode("utf-8")
            secret = (hashlib.sha256(raw) if 'encrypt_type="3"' in challenge
                      else hashlib.md5(raw)).hexdigest().upper()
            status, _ = await attempt(host, settings.talkback_port, "admin", secret,
                                      timeout=settings.talkback_timeout_secs)
        except TalkbackError:
            return False
        if " 200" not in status:
            return False
        credentials.set_password(cam.camera_id, pw, source="stream")
        logger.info("talk-back: %s accepted its own stream password — "
                    "commissioned with no input", cam.camera_id)
        return True

    def observe(self, cid: str, code: str = "", detail: str = "") -> None:
        """A REAL session just succeeded (code "") or failed. That is fresher
        evidence than any scheduled check, so the live wall reflects it at once:
        a carer who is refused flips the button to "needs password" for the next
        person, instead of the next person finding out the same way."""
        if not code:
            self._record(cid, "ready", "")
        elif code in _STATE_FOR:
            self._record(cid, _STATE_FOR[code], detail)
        else:
            return
        self._due[cid] = time.monotonic() + _EVERY.get(
            self._state[cid]["state"], _EVERY["unknown"])

    def _record(self, cid: str, state: str, detail: str,
                elapsed_ms: int | None = None) -> None:
        before = (self._state.get(cid) or {}).get("state")
        self._state[cid] = {
            "state": state,
            "message": _SAY.get(state, ""),
            "detail": detail,
            "checked_at": now_iso(),
            "elapsed_ms": elapsed_ms,
        }
        # Log CHANGES, not checks: a healthy camera re-checked every ten minutes
        # is not news, and a log full of it hides the one line that is.
        if state != before:
            log = logger.info if state == "ready" else logger.warning
            log("talk-back %s: %s -> %s%s", cid, before or "unknown", state,
                f" ({detail})" if detail else "")


readiness = Readiness()

__all__ = ["readiness", "Readiness"]
