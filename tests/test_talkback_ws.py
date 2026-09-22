#!/usr/bin/env python3
"""
Talk-back end to end: a REAL server, a REAL WebSocket client, a fake camera.

Everything between a carer's page and the camera's talk port runs for real —
the /session route, the floor (talkback.sessions) and the camera lines
(talkback.lines). Only the camera itself is scripted. Two earlier bugs were only
visible this way (refusals sent before accept() arrived as an anonymous 1006; a
page leaving mid-handshake orphaned a room), so this is where the protocol is
proven: what a browser actually receives, message by message.

Checked: a refused connection says why (4503 / 4401); the welcome describes
every camera; a press is granted and speech reaches the camera; other pages are
told who is speaking and refused while they are; the floor hold then frees the
room; a camera refusing its password is refused with the camera's reason; a page
that leaves mid-sentence and comes back keeps its floor; a camera that ends its
line mid-sentence is re-opened and the speech carries on; and the line record
shows it.

Needs the edge's web stack (fastapi, uvicorn, websockets — edge/requirements.txt).
Where they are missing it says so and skips.

Run:  python tests/test_talkback_ws.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))
_TMP = Path(tempfile.mkdtemp(prefix="ceravis-talkback-ws-"))
os.environ.setdefault("DATA_DIR", str(_TMP))

try:
    import uvicorn
    import websockets
    from fastapi import FastAPI, HTTPException
except ImportError as exc:                           # pragma: no cover
    print(f"SKIPPED: the edge web stack is not installed here ({exc}).")
    sys.exit(0)

from api import talkback_routes as routes             # noqa: E402
from config.settings import settings                  # noqa: E402
from talkback import credentials, guard               # noqa: E402
from talkback import lines as lines_mod               # noqa: E402
from talkback import sessions as floor_mod            # noqa: E402
from talkback.protocol import TalkbackError           # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# --- isolate every file talk-back touches ----------------------------------- #
credentials._DATA = guard._DATA = _TMP
credentials._FILE = _TMP / "talkback.json"
guard._FILE = _TMP / "talkback_guard.json"
credentials.set_home_password("the-right-one")

EDGE_ID = "E1"


def _edge_check(value):
    if value != EDGE_ID:
        raise HTTPException(401, "edgeId required")


routes.check_edge_id = _edge_check


# --- the cameras, and a scripted camera talk port --------------------------- #
class _Cam:
    def __init__(self, cid, name, host):
        self.camera_id, self.camera_name, self.room_name = cid, name, name
        self.is_enabled = True
        self.rtsp_url = f"rtsp://{host}:554/stream1"
        self.onvif_xaddr, self.onvif_password = "", None


class _Cams:
    rows = [_Cam("LOUNGE", "LOUNGE", "10.0.0.5"), _Cam("BEDROOM", "BEDROOM", "10.0.0.6")]

    def get_all(self):
        return list(_Cams.rows)

    def get_by_id(self, cid):
        return next((c for c in _Cams.rows if c.camera_id == cid), None)

    def get_by_label(self, label):
        key = label.replace(" ", "_").upper()
        return next((c for c in _Cams.rows if c.camera_name.upper() == key), None)


REFUSE = {"BEDROOM"}           # cameras whose talk port refuses the password
SESSIONS: list = []


class FakeCameraLine:
    def __init__(self, host, cred, **kw):
        self.host, self.session_id, self.challenge = host, "", 'encrypt_type="3"'
        self.gone = asyncio.Event()
        self.frames: list = []
        self.camera = None
        self._closed = False

    async def open(self):
        # Which camera this is, by its address — exactly how the edge dials it.
        self.camera = next(c.camera_id for c in _Cams.rows if f"//{self.host}:" in c.rtsp_url)
        if self.camera in REFUSE:
            raise TalkbackError("unauthorized", "The camera refused the TP-Link password.")
        self.session_id = "6"
        SESSIONS.append(self)

    async def start_audio(self):
        pass

    @property
    def alive(self):
        return bool(self.session_id) and not self._closed and not self.gone.is_set()

    async def send(self, frame):
        if not self.alive:
            raise TalkbackError("closed", "closed")
        self.frames.append(frame)

    def health(self):
        return {"frames_sent": len(self.frames), "frames_dropped": 0,
                "bytes_sent": 160 * len(self.frames), "queued_ms": 0, "peak_queued_ms": 0}

    async def close(self):
        self._closed = True
        self.gone.set()


lines_mod.TapoTalkSession = FakeCameraLine
lines_mod.CameraConfig = floor_mod.CameraConfig = _Cams
lines_mod._BOOT_DELAY = 0.0
lines_mod._RECONCILE = 0.05
floor_mod.STALL_SECS = 1.0
settings.talkback_enabled = True
settings.talkback_line_always = True
settings.talkback_floor_hold_secs = 0.6
lines, hub = lines_mod.lines, floor_mod.hub


# --- a real server ---------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app):
    lines.start()
    hub.start()
    yield
    await hub.stop()
    await lines.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(routes.router)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT,
                                            log_level="warning"), daemon=True).start()
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.1)


def url(client="p1", name="Nurse Priya", edge=EDGE_ID):
    return (f"ws://127.0.0.1:{PORT}/api/v1/talkback/session"
            f"?edge_id={edge}&client_id={client}&name={name.replace(' ', '%20')}")


class Page:
    """A carer's page: one standby connection, and everything it was told."""

    def __init__(self, ws):
        self.ws, self.got, self._reader = ws, [], asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                self.got.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    async def say(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def wait(self, pred, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.got:
                if pred(m):
                    return m
            await asyncio.sleep(0.02)
        return None

    def forget(self):
        self.got.clear()

    async def close(self):
        await self.ws.close()
        self._reader.cancel()


async def open_page(**kw):
    return Page(await websockets.connect(url(**kw)))


async def refused_connection(u):
    async with websockets.connect(u) as ws:
        first = json.loads(await ws.recv())
        try:
            await ws.recv()
        except websockets.ConnectionClosed as e:
            return first, e.rcvd.code if e.rcvd else None
    return first, None


async def main():
    print("\nConnecting")
    settings.talkback_enabled = False
    first, code = await refused_connection(url())
    check("talk-back switched off -> told why, closed 4503",
          code == 4503 and first.get("code") == "disabled")
    settings.talkback_enabled = True
    first, code = await refused_connection(url(edge="WRONG"))
    check("wrong edge_id -> told why, closed 4401", code == 4401 and first.get("code") == "edge_id")

    # The lines open by themselves from boot.
    end = time.monotonic() + 3
    while time.monotonic() < end and lines.readiness("LOUNGE")["line"] != "open":
        await asyncio.sleep(0.05)
    priya = await open_page()
    welcome = await priya.wait(lambda m: m.get("type") == "welcome")
    cams = (welcome or {}).get("cameras", {})
    check("the welcome describes every camera: its line and its floor",
          cams.get("LOUNGE", {}).get("readiness", {}).get("line") == "open"
          and cams.get("LOUNGE", {}).get("floor", {}).get("state") == "free"
          and cams.get("BEDROOM", {}).get("readiness", {}).get("state") == "rejected")
    check("and the audio contract (8 kHz A-law, 160-byte frames)",
          welcome and welcome.get("codec") == "alaw" and welcome.get("frame_bytes") == 160)

    print("\nSpeaking")
    arun = await open_page(client="a1", name="Nurse Arun")
    await arun.wait(lambda m: m.get("type") == "welcome")
    t0 = time.monotonic()
    await priya.say(type="claim", camera="LOUNGE")
    g = await priya.wait(lambda m: m.get("type") == "granted")
    check("a press is granted with no camera handshake to wait for",
          g is not None and time.monotonic() - t0 < 0.5)
    check("every other page is told who is speaking",
          await arun.wait(lambda m: m.get("type") == "floor" and m.get("state") == "speaking"
                          and m.get("by") == "Nurse Priya") is not None)
    line = next(s for s in SESSIONS if s.camera == "LOUNGE" and s.alive)
    for _ in range(60):                        # 1.2 s of speech
        await priya.ws.send(bytes(160))
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    check("the speech reaches the camera's line", len(line.frames) >= 55)
    check("and the speaker gets live stats",
          await priya.wait(lambda m: m.get("type") == "stats" and m.get("frames_sent", 0) > 0)
          is not None)

    await arun.say(type="claim", camera="LOUNGE")
    r = await arun.wait(lambda m: m.get("type") == "refused")
    check("a second carer is refused while someone speaks, and told who",
          r is not None and r.get("code") == "busy" and "Nurse Priya" in r.get("message", ""))

    await priya.say(type="release")
    check("letting go is announced as the floor hold",
          await arun.wait(lambda m: m.get("type") == "floor" and m.get("state") == "holding")
          is not None)
    check("then the room is free, for everyone",
          await arun.wait(lambda m: m.get("type") == "floor" and m.get("state") == "free",
                          2.0) is not None)
    arun.forget()
    await arun.say(type="claim", camera="LOUNGE")
    check("and the next carer gets it", await arun.wait(lambda m: m.get("type") == "granted")
          is not None)
    await arun.say(type="release")

    print("\nWhen things go wrong")
    priya.forget()
    await priya.say(type="claim", camera="BEDROOM")
    r = await priya.wait(lambda m: m.get("type") == "refused")
    check("a camera refusing its password -> refused with the camera's reason",
          r is not None and r.get("code") == "unauthorized" and "refused" in r.get("message", ""))

    await asyncio.sleep(0.8)                   # Arun's hold runs out
    await priya.say(type="claim", camera="LOUNGE")
    await priya.wait(lambda m: m.get("type") == "granted")
    await priya.close()                        # the page drops mid-sentence
    await asyncio.sleep(0.1)
    check("a page that drops mid-sentence keeps its floor for the hold",
          hub.floor("LOUNGE")["state"] == "holding")
    priya = await open_page()                  # same client_id: the page is back
    await priya.say(type="claim", camera="LOUNGE")
    check("and, back in time, carries on",
          await priya.wait(lambda m: m.get("type") == "granted") is not None)

    before = next(s for s in SESSIONS if s.camera == "LOUNGE" and s.alive)
    lines._lines["LOUNGE"].opened_at -= 60     # open a minute, then the camera ends it
    before.gone.set()
    check("a camera ending its line mid-sentence: the line is re-opened by itself",
          await priya.wait(lambda m: m.get("type") == "camera" and m.get("camera") == "LOUNGE"
                           and m.get("readiness", {}).get("line") == "open", 3.0) is not None)
    after = next(s for s in SESSIONS if s.camera == "LOUNGE" and s.alive)
    for _ in range(10):
        await priya.ws.send(bytes(160))
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    check("and the speech carries on into the new line", after is not before and after.frames)
    rec = lines.health()["LOUNGE"]
    check("the line record shows the drop and how long the line had lasted",
          rec["drops"] >= 1 and any(e["event"] == "dropped" for e in rec["history"]))
    await priya.say(type="release")
    await priya.close()
    await arun.close()


asyncio.run(main())

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All talk-back WebSocket checks passed.")
