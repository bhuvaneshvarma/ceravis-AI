from __future__ import annotations

"""
The camera LINE — the edge's own talk session to each camera, kept open.

A press used to open the camera's talk session (~100-300 ms of TCP + Digest +
session request) and then hold it, together with the carer's connection, for
90 s — which made every press after the first instant, and locked the room to
everyone else for those 90 s. The line takes the camera out of that: the edge
opens it, keeps it, re-opens it when the camera hangs up, and carers only ever
FEED it (talkback.sessions decides who). So a press never waits for the camera,
and a carer going quiet never holds a room.

An open line is also the only proof of readiness anyone needs: the old periodic
silent self-check is gone, because a line that is up has just been proven, and a
line that fails says why (wrong password, offline, not a talk port).

`talkback_line_always` (default on) keeps every line open from boot. Off, lines
open only while at least one carer is connected to a camera's talk socket, so the
Tapo app can use the speaker when nobody is talking.

HOW LONG DOES A CAMERA HOLD A LINE? Unknown per firmware, so every line keeps its
own record — when it opened, how long each connection lasted, why it ended, how
fast it came back — reported in /talkback/health and `tools.talkback lines`. The
running system is the measurement. If a firmware drops SILENT lines,
`talkback_line_keepalive_secs` sends one silent frame after that many idle
seconds; it is off until the record shows it is needed.
"""

import asyncio
import collections
import hashlib
import logging
import time
from urllib.parse import urlparse

from common.clock import now_iso
from config.settings import settings
from configuration.camera_config import CameraConfig

from . import credentials, guard
from .advice import REFUSED_SHORT
from .protocol import TalkbackError, TapoTalkSession, attempt

logger = logging.getLogger("talkback.lines")

_BOOT_DELAY = 10.0        # let the media backbone come up before adding traffic
_RECONCILE = 15.0         # how often the camera list is re-read
_RETRY_MIN, _RETRY_MAX = 2.0, 120.0
# A line the camera ends within this long of granting it was refused, not timed
# out, and is retried with a growing wait. Anything held longer is re-opened at
# once — including a firmware that closes idle sessions after a few seconds,
# which is exactly what the line record exists to reveal.
_REFUSED_WITHIN = 5.0
_SILENCE = b"\xd5" * 160  # 20 ms of A-law silence (linear 0 encodes to 0xD5)

# Words for the carer, keyed by state. The camera's own sentence goes in `detail`.
_SAY = {
    "ready": "Ready.",
    "connecting": "Connecting to the camera's speaker…",
    "needs_password": "Talk-back needs this home's TP-Link account password.",
    "rejected": REFUSED_SHORT,
    "paused": "Paused after repeated refused logins, so the camera cannot lock the account.",
    "unreachable": "The camera's speaker is not answering right now.",
    "in_use": ("The camera's speaker is in use from the Tapo app, or switched off "
               "in it."),
    "unsupported": "This camera does not offer talk-back.",
}

# What a failed connect says about the camera, and how long to wait before the
# next try. A refused password waits longest ON PURPOSE: repeated failed logins
# are what cameras lock accounts out for (talkback.guard counts them too).
_ON_FAILURE = {
    "unauthorized": ("rejected", 3600.0),
    "refused": ("in_use", 30.0),
    "protocol": ("unsupported", 3600.0),
    "no_credential": ("needs_password", None),
}
# A state -> the error code a caller acting on it gets back.
_CODE = {"rejected": "unauthorized", "needs_password": "no_credential",
         "unreachable": "unreachable", "in_use": "refused",
         "unsupported": "protocol", "paused": "cooldown", "connecting": "timeout"}


def code_for(state: str) -> str:
    """The error code for a line state, as the API and the floor report it."""
    return _CODE.get(state, "unreachable")


def camera_host(camera) -> str:
    """Where this camera lives on the network, from what we already store.

    rtsp_url first (every camera has one and it is what the media backbone
    actually dials), then the ONVIF XAddr. Deliberately NOT a new field: a
    separate `talk_host` would be a second, quietly divergent idea of where a
    camera lives."""
    for url in (getattr(camera, "rtsp_url", ""), getattr(camera, "onvif_xaddr", "") or ""):
        host = urlparse(url).hostname if "://" in (url or "") else ""
        if host:
            return host
    return ""


class Line:
    """One camera's talk line, and its record."""

    def __init__(self, camera_id: str, name: str) -> None:
        self.camera_id = camera_id
        self.name = name
        self.session: TapoTalkSession | None = None
        self.state = "connecting"
        self.detail = ""
        self.checked_at: str | None = None
        self.connect_ms: int | None = None
        self.opened_at = 0.0           # monotonic, while open
        self.last_frame = 0.0          # monotonic: speech or keep-alive
        self.opens = 0
        self.drops = 0
        self.history: collections.deque = collections.deque(maxlen=20)
        self.retry = _RETRY_MIN
        self.stream_tried = False
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    @property
    def open(self) -> bool:
        return self.session is not None and self.session.alive

    def note(self, event: str, **fields) -> None:
        self.history.append({"at": now_iso(), "event": event, **fields})


class Lines:
    """Process-wide. One instance (`lines`), started from the app lifespan."""

    def __init__(self) -> None:
        self._lines: dict[str, Line] = {}
        self._clients = 0
        self._task: asyncio.Task | None = None

    # -- what callers read ------------------------------------------------- #

    def readiness(self, camera_id: str) -> dict:
        line = self._lines.get(camera_id)
        if line is None:
            return {"state": "connecting", "message": _SAY["connecting"],
                    "detail": "", "checked_at": None, "elapsed_ms": None,
                    "line": "closed"}
        return {
            "state": line.state,
            "message": _SAY.get(line.state, ""),
            "detail": line.detail,
            "checked_at": line.checked_at,
            "elapsed_ms": line.connect_ms,
            "line": "open" if line.open else "closed",
        }

    def all(self) -> dict[str, dict]:
        return {cid: self.readiness(cid) for cid in self._lines}

    def health(self) -> dict[str, dict]:
        """The line record: the answer to "how long does a camera hold a line"."""
        now = time.monotonic()
        out = {}
        for cid, line in self._lines.items():
            row = self.readiness(cid)
            row.update({
                "open_secs": round(now - line.opened_at, 1) if line.open else None,
                "opens": line.opens,
                "drops": line.drops,
                "idle_secs": (round(now - line.last_frame, 1)
                              if line.open and line.last_frame else None),
                "history": list(line.history),
            })
            if line.open:
                row.update(line.session.health())
            out[cid] = row
        return out

    def session_health(self, camera_id: str) -> dict:
        line = self._lines.get(camera_id)
        return line.session.health() if line is not None and line.open else {}

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._task is not None or not settings.talkback_enabled:
            return
        self._task = asyncio.create_task(self._manage(), name="talkback-lines")
        logger.info("talk-back lines: %s", "kept open from boot"
                    if settings.talkback_line_always else "opened while carers are connected")

    async def stop(self) -> None:
        tasks = [t for t in [self._task] + [l.task for l in self._lines.values()] if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for line in self._lines.values():
            await self._close(line)
        self._task = None

    def set_clients(self, count: int) -> None:
        """How many carers' talk sockets are connected. Without
        `talkback_line_always`, this is what decides whether lines are open."""
        before, self._clients = self._clients, count
        if not settings.talkback_line_always and (before == 0) != (count == 0):
            self.kick()

    def kick(self, camera_id: str | None = None) -> None:
        """Something changed (a password, a camera, a carer arrived): look again
        NOW instead of at the next scheduled retry."""
        for cid, line in self._lines.items():
            if camera_id is None or cid == camera_id:
                line.retry = _RETRY_MIN
                line.wake.set()

    # -- what the floor and the API call ----------------------------------- #

    async def send(self, camera_id: str, frame: bytes) -> bool:
        """Feed one frame of speech into a camera's line. False if the line is
        not open — the frame is dropped, and the line is already coming back.

        Never waits on the camera: the session measures its own backpressure
        and raises only once the camera has stopped reading altogether
        (protocol._QUEUE_DEAD_MS), which ends the line so it can re-open clean."""
        line = self._lines.get(camera_id)
        if line is None or not line.open:
            return False
        try:
            await line.session.send(frame)
        except TalkbackError as exc:
            logger.warning("talk line %s: %s", camera_id, exc)
            await line.session.close()          # the supervisor notes it and re-opens
            return False
        line.last_frame = time.monotonic()
        return True

    async def ensure_open(self, camera_id: str, timeout: float) -> tuple[bool, str]:
        """Is this camera's line open — and if not, open it now and wait up to
        `timeout`. Returns (open, state)."""
        line = self._lines.get(camera_id)
        if line is None:
            return False, "connecting"
        if line.open:
            return True, line.state
        line.wake.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if line.open:
                return True, line.state
            if line.state not in ("connecting", "ready"):
                return False, line.state
            await asyncio.sleep(0.05)
        return line.open, line.state

    async def check_now(self, timeout: float = 25.0) -> dict[str, dict]:
        """Look at every camera NOW and return the verdicts — for an operator
        who has just typed a password, or re-paired a camera."""
        for line in self._lines.values():
            if not line.open:
                line.state = "connecting"
        self.kick()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(l.state != "connecting" for l in self._lines.values()):
                break
            await asyncio.sleep(0.1)
        return self.all()

    async def test(self, camera_id: str, force: bool = False) -> dict:
        """The handover check: is this camera's line open, or can it be opened
        now? Silent — a line carries no sound until a carer speaks."""
        line = self._lines.get(camera_id)
        if line is None:
            raise TalkbackError("no_camera", f"No camera called '{camera_id}'.")
        if force:
            guard.reset([camera_id])
        if not line.open:
            line.state = "connecting"
        ok, state = await self.ensure_open(camera_id, settings.talkback_timeout_secs * 2)
        if not ok:
            raise TalkbackError(code_for(state),
                                line.detail or _SAY.get(state, state))
        return {"ok": True, "camera_id": camera_id, "host": line.session.host,
                "line": "open", "connect_ms": line.connect_ms,
                "open_secs": round(time.monotonic() - line.opened_at, 1),
                "auth": "sha256" if 'encrypt_type="3"' in line.session.challenge else "md5"}

    # -- the supervisors --------------------------------------------------- #

    async def _manage(self) -> None:
        """Keep one supervised line per enabled camera, following camera setup."""
        try:
            await asyncio.sleep(_BOOT_DELAY)
            seen = credentials.stamp()
            while True:
                # A password set from ANYWHERE — the API, or `tools.talkback set`
                # in another process — changes the credential file, and a line
                # refused on the old password deserves a retry now, not in an hour.
                stamp = credentials.stamp()
                if stamp != seen:
                    seen = stamp
                    self.kick()
                try:
                    cams = {c.camera_id: c for c in CameraConfig().get_all() if c.is_enabled}
                    for cid, cam in cams.items():
                        if cid not in self._lines:
                            line = Line(cid, cam.camera_name)
                            self._lines[cid] = line
                            line.task = asyncio.create_task(self._run(line),
                                                            name=f"talkback-line-{cid}")
                    for cid in [c for c in self._lines if c not in cams]:
                        line = self._lines.pop(cid)
                        if line.task:
                            line.task.cancel()
                        await self._close(line)
                except Exception:
                    logger.warning("talk-back line reconcile failed", exc_info=True)
                await asyncio.sleep(_RECONCILE)
        except asyncio.CancelledError:
            return

    def _wanted(self) -> bool:
        return settings.talkback_line_always or self._clients > 0

    async def _run(self, line: Line) -> None:
        try:
            while True:
                if line.open:
                    await self._hold(line)
                elif self._wanted() or line.state == "connecting":
                    ok, delay = await self._connect(line)
                    if not ok:
                        await self._sleep(line, delay)
                    elif not self._wanted():
                        # Opened only to prove the camera (a check, a new
                        # password): not needed now, so give the speaker back.
                        await self._close(line)
                else:
                    await self._sleep(line, None)
        except asyncio.CancelledError:
            return

    async def _hold(self, line: Line) -> None:
        """Keep an open line until the camera hangs up, it is no longer wanted,
        or a keep-alive is due."""
        ka = settings.talkback_line_keepalive_secs
        timeout = None
        if ka > 0:
            timeout = max(0.5, ka - (time.monotonic() - (line.last_frame or line.opened_at)))
        gone = asyncio.ensure_future(line.session.gone.wait())
        woke = asyncio.ensure_future(line.wake.wait())
        done, pending = await asyncio.wait({gone, woke}, timeout=timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        line.wake.clear()
        if not line.session.alive:
            held = time.monotonic() - line.opened_at
            line.drops += 1
            line.note("dropped", after_secs=round(held, 1))
            logger.warning("talk line %s: the camera ended it after %.0fs", line.camera_id, held)
            await self._close(line)
            quick = held <= _REFUSED_WITHIN
            line.retry = min(line.retry * 2, _RETRY_MAX) if quick else _RETRY_MIN
            self._set(line, "connecting", "")
            await self._sleep(line, line.retry if quick else 0.5)
        elif not self._wanted():
            await self._close(line)
            self._set(line, line.state, line.detail)
        elif ka > 0 and not done:
            await self.send(line.camera_id, _SILENCE)

    async def _connect(self, line: Line) -> tuple[bool, float | None]:
        """One attempt. (True, 0) when open, else (False, seconds until the next
        attempt — None means wait until something changes)."""
        cam = CameraConfig().get_by_id(line.camera_id)
        host = camera_host(cam) if cam is not None else ""
        if not host:
            self._set(line, "unreachable", f"{line.name} has no usable address on file.")
            return False, _RETRY_MAX
        cred = credentials.get(line.camera_id)
        if cred is None and not line.stream_tried:
            line.stream_tried = True
            if await self._try_stream_password(line, cam, host):
                cred = credentials.get(line.camera_id)
        if cred is None:
            self._set(line, "needs_password", "")
            return False, None
        until = guard.paused_until(line.camera_id, cred)
        if until:
            self._set(line, "paused", _paused_detail(line, cred))
            return False, max(60.0, until - time.time())

        session = TapoTalkSession(host, cred, port=settings.talkback_port,
                                  timeout=settings.talkback_timeout_secs,
                                  mode=settings.talkback_mode)
        t0 = time.monotonic()
        try:
            await session.open()
            await session.start_audio()
        except TalkbackError as exc:
            await session.close()
            if exc.code == "unauthorized":
                guard.refused(line.camera_id, cred)
            state, delay = _ON_FAILURE.get(exc.code, ("unreachable", None))
            if delay is None and state == "unreachable":
                delay = line.retry
                line.retry = min(line.retry * 2, _RETRY_MAX)
            line.note("failed", code=exc.code)
            self._set(line, state, str(exc))
            return False, delay
        except Exception as exc:                      # never let one camera kill its line
            await session.close()
            logger.warning("talk line %s: connect crashed", line.camera_id, exc_info=True)
            self._set(line, "unreachable", str(exc))
            return False, _RETRY_MAX
        guard.accepted(line.camera_id)
        line.session = session
        line.opened_at = line.last_frame = time.monotonic()
        line.connect_ms = round((line.opened_at - t0) * 1000)
        line.opens += 1
        line.retry = _RETRY_MIN
        line.note("open", connect_ms=line.connect_ms)
        self._set(line, "ready", "")
        return True, 0

    async def _try_stream_password(self, line: Line, cam, host: str) -> bool:
        """One attempt, once per boot, with the camera's OWN stream password (the
        one camera setup already holds). If the firmware takes it, the camera is
        commissioned with nothing typed. Costs at most one failed login."""
        pw = (getattr(cam, "onvif_password", "") or "").strip()
        if not pw:
            pw = urlparse(getattr(cam, "rtsp_url", "") or "").password or ""
        if not pw:
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
        credentials.set_password(line.camera_id, pw, source="stream")
        logger.info("talk-back: %s accepted its own stream password — "
                    "commissioned with no input", line.camera_id)
        return True

    async def _sleep(self, line: Line, seconds: float | None) -> None:
        try:
            await asyncio.wait_for(line.wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        line.wake.clear()

    async def _close(self, line: Line) -> None:
        session, line.session = line.session, None
        if session is not None:
            await session.close()

    def _set(self, line: Line, state: str, detail: str) -> None:
        before = (line.state, line.detail, line.open)
        line.state, line.detail, line.checked_at = state, detail, now_iso()
        if before == (state, detail, line.open):
            return
        # Log CHANGES only: a healthy line is not news, and a log full of it
        # hides the one line that is.
        if state != before[0]:
            (logger.info if state in ("ready", "connecting") else logger.warning)(
                "talk line %s: %s -> %s%s", line.camera_id, before[0], state,
                f" ({detail})" if detail else "")


def _paused_detail(line: Line, cred) -> str:
    try:
        guard.check(line.camera_id, cred, line.name)
    except TalkbackError as exc:
        return str(exc)
    return ""


lines = Lines()

__all__ = ["lines", "Lines", "Line", "camera_host", "code_for"]
