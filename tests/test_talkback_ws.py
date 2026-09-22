#!/usr/bin/env python3
"""
Talk-back end to end: a REAL server, a REAL WebSocket client, a fake camera.

Everything between a carer and the camera's talk port runs for real — the
/{camera_id}/stream route, the floor (talkback.sessions), the camera lines
(talkback.lines) and the talk log (talkback.audit). Only the camera itself is
scripted. Two earlier bugs were only visible this way (refusals sent before
accept() arrived as an anonymous 1006; a carer leaving mid-handshake orphaned a
room), so this is where the protocol is proven: what a client actually receives,
message by message.

Checked: a refused connection says why (4503 / 4401 / 4409 / 4500); "open"
carries the audio contract; speech reaches the camera with live stats; a second
carer on the same camera is refused and told who, while two cameras talk at
once; the floor hold then frees the room, and speech on the still-open socket
takes it back; a camera refusing its password is refused with its reason; a
carer who drops mid-sentence and reconnects keeps the floor; a camera ending its
line mid-sentence is re-opened and the speech carries on; GET /cameras shows who
holds each floor; and GET /log says who spoke, where, and for how long.

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
import urllib.request
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
from talkback import audit, credentials, guard        # noqa: E402
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
audit.PATH = _TMP / "talkback_log.jsonl"

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
    rows = [_Cam("LOUNGE", "LOUNGE", "10.0.0.5"), _Cam("BEDROOM", "BEDROOM", "10.0.0.6"),
            _Cam("PORCH", "PORCH", "10.0.0.7")]

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
floor_mod.GAP_SECS = 0.5
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


def url(camera="LOUNGE", client="p1", name="Nurse Priya", edge=EDGE_ID, user="u-17"):
    return (f"ws://127.0.0.1:{PORT}/api/v1/talkback/{camera}/stream"
            f"?edge_id={edge}&client_id={client}&name={name.replace(' ', '%20')}"
            f"&user_id={user}")


def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/talkback{path}") as r:
        return json.loads(r.read())


class Talk:
    """A carer's socket on one camera, and everything it was told."""

    def __init__(self, ws):
        self.ws, self.got, self.code = ws, [], None
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, str):
                    self.got.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        self.code = self.ws.close_code

    async def say(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def speak(self, frames=10):
        try:
            for _ in range(frames):
                await self.ws.send(bytes(160))      # 0x00: speech, not A-law silence
                await asyncio.sleep(0.02)
        except websockets.ConnectionClosed:
            pass                                    # refused mid-speech: see closed()

    async def wait(self, pred, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.got:
                if pred(m):
                    return m
            await asyncio.sleep(0.02)
        return None

    async def closed(self, timeout=3.0):
        try:
            await asyncio.wait_for(asyncio.shield(self._reader), timeout)
        except asyncio.TimeoutError:
            pass
        return self.code

    async def close(self):
        await self.ws.close()
        self._reader.cancel()


async def talk(**kw):
    return Talk(await websockets.connect(url(**kw)))


async def main():
    print("\nConnecting")
    settings.talkback_enabled = False
    t = await talk()
    check("talk-back switched off -> told why, closed 4503",
          await t.closed() == 4503 and t.got and t.got[0].get("code") == "disabled")
    settings.talkback_enabled = True
    t = await talk(edge="WRONG")
    check("wrong edge_id -> told why, closed 4401",
          await t.closed() == 4401 and t.got and t.got[0].get("code") == "edge_id")

    end = time.monotonic() + 3                     # the lines open by themselves
    while time.monotonic() < end and lines.readiness("LOUNGE")["line"] != "open":
        await asyncio.sleep(0.05)
    t0 = time.monotonic()
    priya = await talk()
    opened = await priya.wait(lambda m: m.get("type") == "open")
    check("connecting is granted with no camera handshake to wait for",
          opened is not None and time.monotonic() - t0 < 0.5)
    check("open carries the audio contract (8 kHz A-law, 160-byte frames)",
          opened and opened.get("codec") == "alaw" and opened.get("frame_bytes") == 160
          and opened.get("sample_rate") == 8000 and opened.get("camera_id") == "LOUNGE")
    check("and the timings: floor hold, socket idle (hold_secs), no turn limit",
          opened and opened.get("floor_hold_secs") == 0.6 and opened.get("hold_secs") > 0
          and opened.get("max_turn_secs") == 0)

    print("\nSpeaking")
    line = next(s for s in SESSIONS if s.camera == "LOUNGE" and s.alive)
    await priya.speak(60)                          # 1.2 s of speech
    await asyncio.sleep(0.1)
    check("the speech reaches the camera's line", len(line.frames) >= 55)
    check("and the speaker gets live stats",
          await priya.wait(lambda m: m.get("type") == "stats" and m.get("frames_sent", 0) > 0)
          is not None)
    floor = next(c for c in get(f"/cameras?edge_id={EDGE_ID}")["cameras"]
                 if c["camera_id"] == "LOUNGE")["floor"]
    check("GET /cameras shows who holds the floor, for every other carer's screen",
          floor.get("state") == "speaking" and floor.get("by") == "Nurse Priya")

    await priya.speak(3)
    arun = await talk(client="a1", name="Nurse Arun", user="u-22")
    check("a second carer on the same camera is refused, told who, closed 4409",
          await arun.closed() == 4409 and arun.got
          and arun.got[0].get("code") == "busy" and "Nurse Priya" in arun.got[0].get("message", ""))
    arun_porch = await talk(camera="PORCH", client="a1", name="Nurse Arun", user="u-22")
    check("while he talks into another camera at the same time",
          await arun_porch.wait(lambda m: m.get("type") == "open") is not None
          and hub.floor("LOUNGE")["by"] == "Nurse Priya" and hub.floor("PORCH")["by"] == "Nurse Arun")
    await arun_porch.say(type="release")

    await priya.speak(3)
    await priya.say(type="release")
    await asyncio.sleep(0.1)
    check("letting go holds the floor briefly", hub.floor("LOUNGE")["state"] == "holding")
    end = time.monotonic() + 3
    while time.monotonic() < end and hub.floor("LOUNGE")["state"] != "free":
        await asyncio.sleep(0.05)
    check("then the room is free — while the socket stays open for the next press",
          hub.floor("LOUNGE")["state"] == "free" and priya.code is None)
    arun = await talk(client="a1", name="Nurse Arun", user="u-22")
    check("so the next carer gets it", await arun.wait(lambda m: m.get("type") == "open") is not None)
    await priya.speak(3)
    check("and speaking into the old socket then is refused with who has it (4409)",
          await priya.closed() == 4409)
    await arun.say(type="release")
    await asyncio.sleep(0.8)                       # Arun's hold runs out
    await arun.speak(3)
    check("speech on an open socket takes a free floor straight back",
          hub.floor("LOUNGE")["by"] == "Nurse Arun" and hub.floor("LOUNGE")["state"] == "speaking")
    await arun.say(type="release")
    await arun.close()

    print("\nWhen things go wrong")
    t = await talk(camera="BEDROOM")
    check("a camera refusing its password -> the camera's reason, closed 4500",
          await t.closed() == 4500 and t.got and t.got[0].get("code") == "unauthorized")
    t = await talk(camera="GARAGE")
    check("an unknown camera -> said so, closed 4500",
          await t.closed() == 4500 and t.got and t.got[0].get("code") == "no_camera")

    await asyncio.sleep(0.8)
    priya = await talk()
    await priya.wait(lambda m: m.get("type") == "open")
    await priya.speak(5)
    await priya.close()                            # the network drops mid-sentence
    await asyncio.sleep(0.1)
    check("a carer who drops mid-sentence keeps the floor for the hold",
          hub.floor("LOUNGE")["state"] == "holding")
    priya = await talk()                           # same client_id: back
    check("and, back in time, carries on",
          await priya.wait(lambda m: m.get("type") == "open") is not None)

    before = next(s for s in SESSIONS if s.camera == "LOUNGE" and s.alive)
    lines._lines["LOUNGE"].opened_at -= 60         # open a minute, then the camera ends it
    before.gone.set()
    end = time.monotonic() + 3
    while time.monotonic() < end and not any(
            s.camera == "LOUNGE" and s.alive and s is not before for s in SESSIONS):
        await asyncio.sleep(0.05)
    await priya.speak(10)
    await asyncio.sleep(0.1)
    after = next(s for s in SESSIONS if s.camera == "LOUNGE" and s.alive)
    check("a camera ending its line mid-sentence: re-opened, and the speech carries on",
          after is not before and after.frames and priya.code is None)
    rec = lines.health()["LOUNGE"]
    check("the line record shows the drop and how long the line had lasted",
          rec["drops"] >= 1 and any(e["event"] == "dropped" for e in rec["history"]))
    await priya.say(type="release")
    await priya.close()
    end = time.monotonic() + 3
    while time.monotonic() < end and hub.floor("LOUNGE")["state"] != "free":
        await asyncio.sleep(0.05)

    print("\nThe talk log")
    log = get(f"/log?edge_id={EDGE_ID}&camera=LOUNGE")["entries"]
    talks = [e for e in log if e["event"] == "talk"]
    check("GET /log lists each talk: who, their app's user id, the room, and how long",
          talks and talks[0]["name"] == "Nurse Priya" and talks[0]["user_id"] == "u-17"
          and talks[0]["camera_name"] == "LOUNGE" and talks[0]["talk_secs"] > 0
          and talks[0]["started_at"] and talks[0]["ended_at"])
    check("and who was refused, and why",
          any(e["event"] == "refused" and e["name"] == "Nurse Arun" and e["code"] == "busy"
              for e in log))


asyncio.run(main())

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All talk-back WebSocket checks passed.")
