#!/usr/bin/env python3
"""
The talk-back WebSocket, end to end: a REAL server, a REAL client, a fake camera.

This exists because of a bug no unit test could see. Until 2026-09-22 every
refusal on this socket was sent BEFORE accept(), which the ASGI spec turns into
an HTTP 403 on the handshake — so the browser saw code 1006 with an EMPTY reason
for a wrong password, a busy camera and a device with talk-back switched off
alike. The client treated a refused password as a network blip and retried it,
each retry another refused login on the camera. Only a real server speaking real
WebSocket frames to a real client shows what a browser actually receives, so
that is what this runs.

Checked: every refusal arrives as an error frame with the full sentence AND a
close code + reason the client can act on (disabled 4503, edge_id 4401, busy
4409, paused 4429, camera refusal 4500 'unauthorized:'); a close reason never
exceeds the protocol's 123 bytes; a healthy session opens, streams, reports stats
and hangs up cleanly; a camera that fails MID-session closes 4500 (reconnect),
not 1000 (give up); and a camera addressed by another spelling is still released.

Needs the edge's own web stack (fastapi, uvicorn, websockets — all in
edge/requirements.txt). Where they are missing it says so and skips.

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
from talkback import credentials, guard, sessions     # noqa: E402
from talkback.credentials import TalkCredential       # noqa: E402
from talkback.protocol import TalkbackError           # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# --- isolate every file the talk-back code touches -------------------------- #
credentials._DATA = guard._DATA = _TMP
credentials._FILE = _TMP / "talkback.json"
guard._FILE = _TMP / "talkback_guard.json"

EDGE_ID = "E1"


def _edge_check(value):
    if value != EDGE_ID:
        raise HTTPException(401, "edgeId required")


routes.check_edge_id = _edge_check

# --- a fake camera: the script says what the next session does -------------- #
SCRIPT = {"open": "ok", "send": "ok"}
CRED = TalkCredential(md5="E" * 32, sha256="E" * 64)


class FakeCameraSession:
    def __init__(self, *a, **k):
        self.session_id = "7"
        self.host = "10.0.0.9"
        self.challenge = 'encrypt_type="3"'
        self.bytes_sent = 0
        self.closed = False

    async def open(self):
        if SCRIPT["open"] == "slow":
            await asyncio.sleep(0.8)            # a camera handshake in flight
            return
        if SCRIPT["open"] != "ok":
            raise TalkbackError(SCRIPT["open"], LONG_SENTENCE)

    async def start_audio(self):
        pass

    async def send(self, alaw):
        if SCRIPT["send"] != "ok":
            raise TalkbackError(SCRIPT["send"], "The camera stopped accepting audio.")
        self.bytes_sent += len(alaw)

    def health(self):
        return {"frames_sent": 1, "frames_dropped": 0, "bytes_sent": self.bytes_sent,
                "queued_ms": 0, "peak_queued_ms": 0}

    async def close(self):
        self.closed = True


# Longer than 123 bytes AND full of multi-byte characters: the case that used to
# be cut by character count and could fail the close itself.
LONG_SENTENCE = ("The camera refused the TP-Link password — check Third-Party "
                 "Compatibility — check the owner account — check the sign-in "
                 "method — then re-enter it — and only then re-pair the camera.")

sessions.TapoTalkSession = FakeCameraSession
sessions.TalkbackHub._resolve = lambda self, cid: (
    type("C", (), {"camera_id": "LIVING_ROOM", "camera_name": "LIVING ROOM"})(),
    "10.0.0.9", CRED)
sessions.TalkbackHub.canonical = staticmethod(
    lambda cid: "LIVING_ROOM" if cid.replace(" ", "_").upper() == "LIVING_ROOM" else cid)
hub = sessions.hub

# --- a real server ---------------------------------------------------------- #
app = FastAPI()
app.include_router(routes.router)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT,
                                            log_level="warning"),
                 daemon=True).start()
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.1)


def url(cam="LIVING_ROOM", edge=EDGE_ID, extra=""):
    return (f"ws://127.0.0.1:{PORT}/api/v1/talkback/{cam}/stream"
            f"?client_id=c1&edge_id={edge}{extra}")


async def session(u, frames=0, stop=True):
    """Connect, optionally stream, and report (messages, close code, reason)."""
    msgs = []
    try:
        async with websockets.connect(u) as ws:
            try:
                first = json.loads(await asyncio.wait_for(ws.recv(), 5))
                msgs.append(first)
                if first.get("type") == "open":
                    # A send can fail because the SERVER already closed (that
                    # is the mid-session case under test). What it said before
                    # closing is still queued, so fall through and read it.
                    try:
                        for _ in range(frames):
                            await ws.send(bytes(160))
                        await asyncio.sleep(1.2)        # one stats interval
                        await ws.send(bytes(160))
                        if stop:
                            await ws.send(json.dumps({"type": "stop"}))
                    except websockets.ConnectionClosed:
                        pass
                while True:
                    msgs.append(json.loads(await asyncio.wait_for(ws.recv(), 5)))
            except websockets.ConnectionClosed as e:
                rc = e.rcvd
                return msgs, (rc.code if rc else None), (rc.reason if rc else "")
    except websockets.InvalidStatus as e:
        return msgs, f"HTTP {e.response.status_code}", ""
    return msgs, None, ""


async def main():
    print("\nRefusals reach the client")

    settings.talkback_enabled = False
    msgs, code, reason = await session(url())
    check("talk-back switched off -> error frame + close 4503 (not an anonymous 1006)",
          code == 4503 and msgs and msgs[0].get("code") == "disabled")
    settings.talkback_enabled = True

    msgs, code, reason = await session(url(edge="WRONG"))
    check("wrong edge_id -> close 4401, and the camera is never dialled",
          code == 4401 and msgs and msgs[0].get("code") == "edge_id")

    SCRIPT["open"] = "unauthorized"
    msgs, code, reason = await session(url())
    check("camera refuses the password -> close 4500 with an 'unauthorized:' reason",
          code == 4500 and reason.startswith("unauthorized:"))
    check("the error frame carries the FULL sentence",
          msgs and msgs[0].get("message") == LONG_SENTENCE)
    check("the close reason stays within the protocol's 123 bytes",
          len(reason.encode("utf-8")) <= 123)
    check("a refused session does not leave the camera busy", not hub.busy("LIVING_ROOM"))

    # Two more refusals make three in a row -> the next attempt is paused locally.
    await session(url())
    await session(url())
    msgs, code, reason = await session(url())
    check("after repeated refusals the socket closes 4429 (paused), camera not dialled",
          code == 4429 and msgs and msgs[0].get("code") == "cooldown")
    guard.reset()
    SCRIPT["open"] = "ok"

    print("\nA healthy session")
    holder_task = asyncio.create_task(session(url(extra="&name=Nurse%20Priya"),
                                              frames=3, stop=False))
    await asyncio.sleep(0.4)
    msgs, code, reason = await session(url(extra="&client_id=someone-else"))
    check("a second carer gets 4409, told WHO is speaking by name",
          code == 4409 and msgs and "Nurse Priya" in msgs[0].get("message", ""))
    holder_task.cancel()
    try:
        await holder_task
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0.3)

    msgs, code, reason = await session(url(cam="living%20room"), frames=5)
    check("a session opens (open frame first)", msgs and msgs[0].get("type") == "open")
    check("stats arrive while speech flows",
          any(m.get("type") == "stats" and "queued_ms" in m for m in msgs))
    check("'stop' hangs up cleanly with 1000", code == 1000)
    await asyncio.sleep(0.2)
    check("opened under another spelling, the camera is still released",
          not hub.busy("LIVING_ROOM") and not hub._active)

    print("\nFailure mid-sentence")
    SCRIPT["send"] = "stalled"
    msgs, code, reason = await session(url(), frames=2)
    check("a camera that stops reading closes 4500 'stalled:' — reconnect, not 1000",
          code == 4500 and reason.startswith("stalled:")
          and any(m.get("type") == "error" and m.get("code") == "stalled" for m in msgs))
    SCRIPT["send"] = "ok"
    await asyncio.sleep(0.2)
    check("and the speaker is released", not hub._active)

    print("\nThe carer leaves DURING the camera handshake")
    # The 2026-09-22 bench fault: LOUNGE held for 592 s with 0 frames, so every
    # carer after was told "someone is already speaking". A page reloaded while
    # the ~200 ms camera handshake was in flight left the session orphaned.
    SCRIPT["open"] = "slow"
    ws = await websockets.connect(url())
    await asyncio.sleep(0.2)                     # handshake has started
    await ws.close()                             # the page goes away
    await asyncio.sleep(1.5)                     # the handshake completes
    check("a session whose carer left mid-handshake is given back, not orphaned",
          not hub._active)
    SCRIPT["open"] = "ok"
    msgs, code, reason = await session(url(extra="&client_id=next-carer"), frames=1)
    check("and the next carer can talk at once",
          msgs and msgs[0].get("type") == "open")


asyncio.run(main())

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All talk-back WebSocket checks passed.")
