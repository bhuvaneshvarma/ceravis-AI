#!/usr/bin/env python3
"""
Sanity check for talk-back — the bytes and the rules nothing else can catch.

The camera gives NO feedback on a malformed stream. A wrong A-law byte, a bad
PSI checksum, a stream type it does not recognise or a PTS that stops moving all
produce exactly the same result as a working session: silence. So everything
that has to be right by construction is asserted here, offline, before anything
is ever pointed at a real room.

Checked:
  * A-law encoding matches an independently written reference over ALL 65536
    samples, and the table-driven bulk path matches the per-sample one
  * the muxed transport stream is structurally valid — 188-byte alignment, sync
    bytes, correct MPEG CRC32 on both PSI sections — and the PMT declares
    TP-Link's private stream_type 0x90, which is the whole reason this muxer
    exists (ffmpeg cannot emit it)
  * the continuity counter and the PTS advance frame to frame
  * the Digest response matches a hand-computed RFC 7616 vector, and the
    firmware-dependent hash choice (MD5 / SHA256 / the fixed-account case)
    picks the right one
  * a stored credential contains no trace of the plaintext password, and an
    empty password can never replace a working one
  * a camera hanging up ends its session without freezing the process
  * the edge keeps its own talk LINE to every camera open, re-opens it when the
    camera ends it, records how long each connection lasted, never retries a
    refused password in a loop, and opens lines on demand when told to
  * the FLOOR: one voice per camera (two cameras at once is fine), no time
    limit while the button is held, a short hold after release, released when
    speech stops, silence never claims a room, kept across a reconnect, idle
    sockets closed, priority only upward — and every talk and refusal logged

Run:  python tests/test_talkback.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

_TMP = Path(tempfile.mkdtemp(prefix="ceravis-talkback-"))
os.environ.setdefault("DATA_DIR", str(_TMP))

from talkback import credentials as store                          # noqa: E402
from talkback.credentials import TalkCredential                    # noqa: E402
from talkback.mpegts import (FRAME_BYTES, STREAM_TYPE_PCMA_TAPO,   # noqa: E402
                             TS_PACKET, TS_SYNC, AudioMuxer, _crc32_mpeg,
                             linear_to_alaw, pcm16_to_alaw)
from talkback import protocol as proto                            # noqa: E402
from talkback.protocol import TalkbackError, digest_header         # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# G.711 A-law
# ---------------------------------------------------------------------------
print("\nG.711 A-law")


def reference_alaw(sample: int) -> int:
    """The classic g711.c linear2alaw, written out independently of the code
    under test so a shared mistake cannot pass."""
    seg_ends = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]
    pcm = sample >> 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = next((i for i, e in enumerate(seg_ends) if pcm <= e), 8)
    if seg >= 8:
        return 0x7F ^ mask
    aval = (seg << 4) | ((pcm >> 1) & 0x0F if seg < 2 else (pcm >> seg) & 0x0F)
    return aval ^ mask


_bad = [s for s in range(-32768, 32768) if linear_to_alaw(s) != reference_alaw(s)]
check("every one of the 65536 samples encodes exactly as the reference", not _bad)

_samples = list(range(-32768, 32768, 37))
_pcm = b"".join(struct.pack("<h", s) for s in _samples)
check("the table-driven bulk path matches the per-sample path",
      pcm16_to_alaw(_pcm) == bytes(linear_to_alaw(s) for s in _samples))
check("a chunk split mid-sample drops the half sample, never reads past it",
      len(pcm16_to_alaw(b"\x00\x10\x00")) == 1)


# ---------------------------------------------------------------------------
# MPEG-TS
# ---------------------------------------------------------------------------
print("\nMPEG-TS framing")


def packets(blob: bytes) -> list[bytes]:
    if len(blob) % TS_PACKET:
        return []
    return [blob[i:i + TS_PACKET] for i in range(0, len(blob), TS_PACKET)]


def pid_of(pkt: bytes) -> int:
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


_head = packets(AudioMuxer.header())
check("the header is two 188-byte packets, PAT then PMT",
      len(_head) == 2 and [pid_of(p) for p in _head] == [0x0000, 0x1000])
check("both start with the sync byte", all(p[0] == TS_SYNC for p in _head))

_crc_ok = True
_section = b""
for _pkt in _head:
    _len = ((_pkt[6] & 0x0F) << 8) | _pkt[7]
    _section = _pkt[5:5 + 3 + _len]
    if _crc32_mpeg(_section[:-4]) != int.from_bytes(_section[-4:], "big"):
        _crc_ok = False
check("both PSI sections carry a correct MPEG CRC32", _crc_ok)
check("the PMT declares TP-Link's private stream_type 0x90",
      _section[12] == STREAM_TYPE_PCMA_TAPO)

_muxer = AudioMuxer()
_counters, _pts, _shape = [], [], True
for _ in range(4):
    _pk = packets(_muxer.frame(b"\x55" * FRAME_BYTES))
    if len(_pk) != 1:
        _shape = False
        break
    _p = _pk[0]
    if _p[0] != TS_SYNC or pid_of(_p) != 0x0100 or not _p[1] & 0x40:
        _shape = False
    _counters.append(_p[3] & 0x0F)
    _body = _p[5 + _p[4]:]                       # skip the adaptation stuffing
    if _body[:4] != b"\x00\x00\x01\xc0":
        _shape = False
    _pts.append(_body[9:14])
check("one 20 ms frame is one TS packet, PES-framed, PUSI set", _shape)
check("the continuity counter advances 0,1,2,3", _counters == [0, 1, 2, 3])
check("the PTS moves every frame (a stalled clock is dropped by the camera)",
      len(set(_pts)) == 4)
check("a chunk larger than one packet spans packets instead of truncating",
      len(packets(AudioMuxer().frame(b"\x55" * 800))) > 1)


# ---------------------------------------------------------------------------
# Digest auth
# ---------------------------------------------------------------------------
print("\nDigest authentication")

CHALLENGE = 'Digest realm="AXIS", nonce="abc123", qop="auth"'
_header = digest_header(CHALLENGE, "admin", "SECRETHASH")
_cnonce = _header.split('cnonce="')[1].split('"')[0]


def _h(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode()).hexdigest()


_expect = _h(_h("admin", "AXIS", "SECRETHASH"), "abc123", "00000001",
             _cnonce, "auth", _h("POST", "/stream"))
check("the response matches a hand-computed RFC 7616 vector",
      f'response="{_expect}"' in _header)
check("a fresh cnonce each time (a replayable one is not authentication)",
      digest_header(CHALLENGE, "admin", "SECRETHASH") != _header)

_cred = TalkCredential(md5="MD5VALUE", sha256="SHAVALUE")
check("a plain challenge uses the MD5 hash",
      _cred.password_for(CHALLENGE) == ("admin", "MD5VALUE"))
check('encrypt_type="3" firmware uses the SHA256 hash',
      _cred.password_for(CHALLENGE + ' encrypt_type="3"') == ("admin", "SHAVALUE"))
_user, _pw = _cred.password_for(CHALLENGE + ' username="none"')
check("the fixed-account firmware uses neither of our hashes",
      _user == "none" and _pw not in ("MD5VALUE", "SHAVALUE"))


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------
print("\nCredential storage")

store._DATA = _TMP
store._FILE = _TMP / "talkback.json"
# The lock-out guard keeps its own small file next to the credentials. Point it
# at the scratch directory too, or these checks would write into edge/data.
from talkback import guard as guard_mod                             # noqa: E402
guard_mod._DATA = _TMP
guard_mod._FILE = _TMP / "talkback_guard.json"

SECRET = "Sup3rSecret!Passw0rd"
store.set_password("cam_1", SECRET)
_disk = (_TMP / "talkback.json").read_text()
check("the plaintext password is NOWHERE on disk", SECRET not in _disk)
check("the MD5 hash is what got stored",
      hashlib.md5(SECRET.encode()).hexdigest().upper() in _disk)
check("nothing but hashes, a timestamp and where it came from is kept",
      set(json.loads(_disk)["cam_1"]) == {"md5", "sha256", "updated_at", "source"})
check("the stored credential answers a challenge correctly",
      store.get("cam_1").password_for(CHALLENGE)[1]
      == hashlib.md5(SECRET.encode()).hexdigest().upper())
check("the camera reports as configured", store.configured() == {"cam_1"})

# summary() backs the inventory endpoint, so it must carry the FACT of a
# credential and none of the material — a hash in an API response is a hash
# someone can take away and grind offline.
_summary = store.summary()
check("summary names the commissioned camera", set(_summary) == {"cam_1"})
check("summary carries when it was set", bool(_summary["cam_1"]["updated_at"]))
check("summary leaks NO hash material",
      "md5" not in _summary["cam_1"] and "sha256" not in _summary["cam_1"])

if os.name == "posix":
    check("the file is not world-readable",
          oct((_TMP / "talkback.json").stat().st_mode)[-3:] == "600")

_refused = True
for _blank in ("", "   ", None):
    try:
        store.set_password("cam_1", _blank)
        _refused = False
    except ValueError:
        pass
check("a blank password cannot silently replace a working credential", _refused)
check("the working credential survived those attempts", store.get("cam_1") is not None)
check("forgetting removes it", store.forget("cam_1") and store.get("cam_1") is None)
check("forgetting again is a no-op, not an error", store.forget("cam_1") is False)


# ---------------------------------------------------------------------------
# Backpressure: late speech is dropped, a dead camera is reported
# ---------------------------------------------------------------------------
print("\nBackpressure")


class _FakeTransport:
    """A socket whose write buffer we control."""

    def __init__(self, queued=0):
        self.queued = queued
        self.written = 0

    def get_write_buffer_size(self):
        return self.queued


class _FakeWriter:
    def __init__(self, queued=0):
        self.transport = _FakeTransport(queued)
        self.chunks = []

    def write(self, data):
        self.chunks.append(data)


async def _backpressure():
    sess = proto.TapoTalkSession("10.0.0.9", object())
    sess.session_id = "s1"
    frame = bytes(160)                               # 20 ms of A-law

    # An empty socket: the frame goes straight out, no waiting on drain().
    sess._writer = _FakeWriter(0)
    await sess.send(frame)
    check("an idle socket takes the frame immediately",
          len(sess._writer.chunks) == 1 and sess.frames_sent == 1)
    per_frame = len(sess._writer.chunks[0])

    # Past the stale budget: the frame is dropped rather than deepening a
    # backlog of speech the room has already fallen behind.
    sess._writer = _FakeWriter(int(per_frame * proto._QUEUE_STALE_MS / 20) + per_frame)
    await sess.send(frame)
    check("speech queued past the stale budget is DROPPED, not queued deeper",
          sess._writer.chunks == [] and sess.frames_dropped == 1)
    check("a dropped frame is not counted as sent", sess.frames_sent == 1)

    # A camera that stopped reading altogether is an error, not a deeper queue.
    sess._writer = _FakeWriter(int(per_frame * proto._QUEUE_DEAD_MS / 20) + per_frame)
    stalled = None
    try:
        await sess.send(frame)
    except TalkbackError as exc:
        stalled = exc
    check("a camera that stopped reading is reported as stalled",
          stalled is not None and stalled.code == "stalled")

    h = sess.health()
    check("health reports what the channel is costing",
          h["frames_sent"] == 1 and h["frames_dropped"] == 1
          and "queued_ms" in h and "peak_queued_ms" in h)


asyncio.run(_backpressure())


async def _camera_hangs_up():
    """At end of stream read() returns b"" at once without yielding. The drain
    loop used to spin on it and starve the whole process — the edge API froze.
    A camera hanging up must end the session and leave everything else running."""
    async def serve(r, w):
        w.close()                                  # the camera hangs up
    srv = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    sess = proto.TapoTalkSession("127.0.0.1", object())
    sess.session_id = "6"
    sess._reader, sess._writer = await asyncio.open_connection("127.0.0.1", port)
    ticks = 0

    async def rest_of_the_edge():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    other = asyncio.create_task(rest_of_the_edge())
    asyncio.create_task(sess._drain())
    try:
        await asyncio.wait_for(sess.gone.wait(), 2)
        noticed = True
    except asyncio.TimeoutError:
        noticed = False
    await asyncio.sleep(0.1)
    other.cancel()
    srv.close()
    check("a camera hanging up ends the session (it is noticed, not spun on)", noticed)
    check("and the rest of the edge keeps running meanwhile", ticks > 3)
    check("a session the camera hung up is no longer alive", not sess.alive)
    refused = None
    try:
        await sess.send(bytes(160))
    except TalkbackError as exc:
        refused = exc
    check("sending on it says so instead of writing into a dead socket",
          refused is not None and refused.code == "closed")


asyncio.run(_camera_hangs_up())


# ---------------------------------------------------------------------------
# One password for the whole home
# ---------------------------------------------------------------------------
print("\nOne TP-Link password for the whole home")

store._FILE.unlink(missing_ok=True)
check("no credential anywhere to begin with",
      store.get("LOUNGE") is None and not store.home_configured())

store.set_home_password("home-secret")
check("a camera with no entry of its own uses the home password",
      store.get("LOUNGE") is not None
      and store.get("LOUNGE").md5 == hashlib.md5(b"home-secret").hexdigest().upper())
check("so does a camera added later, with no extra step",
      store.get("ADDED_NEXT_YEAR") is not None)
check("the home password is reported as the home's",
      store.resolve(["LOUNGE"])["LOUNGE"]["scope"] == "home")
check("the home password is NOWHERE on disk in plaintext",
      "home-secret" not in store._FILE.read_text())

store.set_password("GARAGE", "other-account")
check("a per-camera override wins over the home password",
      store.get("GARAGE").md5 == hashlib.md5(b"other-account").hexdigest().upper()
      and store.resolve(["GARAGE"])["GARAGE"]["scope"] == "camera")

store.set_password("PORCH", "found-by-device", source="stream")
cleared = store.set_home_password("new-home-secret")
check("a new home password replaces the stale TYPED overrides",
      "GARAGE" in cleared and store.resolve(["GARAGE"])["GARAGE"]["scope"] == "home")
check("but keeps a credential the device PROVED by itself",
      "PORCH" not in cleared
      and store.resolve(["PORCH"])["PORCH"]["source"] == "stream")
check("the inventory summary carries no hash material",
      all(set(v) <= {"updated_at", "source"} for v in store.summary().values()))
check("configured() never lists the home entry as a camera",
      store.HOME not in store.configured())
store._FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The lock-out guard: our own retries must never lock a camera out
# ---------------------------------------------------------------------------
print("\nLock-out guard")

import time as _time                                                # noqa: E402

guard_mod._FILE.unlink(missing_ok=True)
_credA = TalkCredential(md5="A" * 32, sha256="A" * 64)
_credB = TalkCredential(md5="B" * 32, sha256="B" * 64)

guard_mod.refused("CAM", _credA)
guard_mod.refused("CAM", _credA)
check("two refusals are forgiven (typos happen)",
      guard_mod.paused_until("CAM", _credA) == 0)
guard_mod.refused("CAM", _credA)
_u3 = guard_mod.paused_until("CAM", _credA)
check("the third refusal in a row pauses the camera for ~15 min",
      840 < _u3 - _time.time() <= 900)
guard_mod.refused("CAM", _credA, count=3)
check("repeated refusals lengthen the pause, capped at 3 h",
      10700 < guard_mod.paused_until("CAM", _credA) - _time.time() <= 10800)
check("the pause belongs to THAT credential — a different one is not paused",
      guard_mod.paused_until("CAM", _credB) == 0)
_blocked = None
try:
    guard_mod.check("CAM", _credA, "LOUNGE")
except TalkbackError as exc:
    _blocked = exc
check("a paused camera is refused locally, with a sentence saying until when",
      _blocked is not None and _blocked.code == "cooldown"
      and "paused until" in str(_blocked) and "LOUNGE" in str(_blocked))
check("the pause is on disk, so the command line honours it too",
      "CAM" in json.loads(guard_mod._FILE.read_text()))
check("no password material is written to the guard file",
      "A" * 16 not in guard_mod._FILE.read_text())
guard_mod.accepted("CAM")
check("the camera accepting the password clears everything",
      guard_mod.paused_until("CAM", _credA) == 0)

for _ in range(4):
    guard_mod.refused("LOUNGE", _credA)
store.set_home_password("typed-again")
check("setting a password (even the same one) lifts the pause at once",
      guard_mod.paused_until("LOUNGE", _credA) == 0)
store._FILE.unlink(missing_ok=True)
guard_mod._FILE.unlink(missing_ok=True)




# ---------------------------------------------------------------------------
# Camera lines: the edge keeps its own talk line to every camera open
# ---------------------------------------------------------------------------
print("\nCamera lines")

from talkback import lines as lines_mod                            # noqa: E402
from talkback import sessions as floor_mod                         # noqa: E402
from talkback import audit as audit_mod                            # noqa: E402

_settings = lines_mod.settings


async def _until(cond, timeout=2.0):
    """Wait for a condition the supervisors reach on their own schedule."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return bool(cond())


class _Cam:
    def __init__(self, cid, name=None, pw=""):
        self.camera_id = cid
        self.camera_name = name or cid
        self.room_name = self.camera_name
        self.is_enabled = True
        self.rtsp_url = (f"rtsp://isw:{pw}@10.0.0.5:554/stream1" if pw
                         else "rtsp://10.0.0.5:554/stream1")
        self.onvif_xaddr = ""
        self.onvif_password = None


class _Cams:
    rows: list = []

    def get_all(self):
        return list(_Cams.rows)

    def get_by_id(self, cid):
        return next((c for c in _Cams.rows if c.camera_id == cid), None)

    def get_by_label(self, label):
        key = label.replace(" ", "_").upper()
        return next((c for c in _Cams.rows if c.camera_name.replace(" ", "_").upper() == key), None)


class _LineSession:
    """A camera's talk port, scripted. OUTCOME decides what the next open does."""
    OUTCOME = "ok"
    opened: list = []

    def __init__(self, host, cred, **kw):
        self.host = host
        self.session_id = ""
        self.challenge = 'encrypt_type="3"'
        self.gone = asyncio.Event()
        self.frames: list = []
        self._closed = False

    async def open(self):
        _LineSession.opened.append(self)
        if _LineSession.OUTCOME != "ok":
            raise TalkbackError(_LineSession.OUTCOME, f"scripted {_LineSession.OUTCOME}")
        self.session_id = "6"

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
        return {"frames_sent": len(self.frames), "queued_ms": 0}

    async def close(self):
        self._closed = True
        self.gone.set()


def _hangs_up(L, cid, after=60.0):
    """The camera ends a line that has been open `after` seconds."""
    line = L._lines[cid]
    line.opened_at -= after
    line.session.gone.set()


async def _no_stream_login(host, port, username="", password="", timeout=8.0):
    return "HTTP/1.1 401", 'Digest realm="x",encrypt_type="3"'


lines_mod.TapoTalkSession = _LineSession
lines_mod.CameraConfig = _Cams
lines_mod.attempt = _no_stream_login
lines_mod._BOOT_DELAY = 0.0
lines_mod._RECONCILE = 0.05
_settings.talkback_enabled = True
_settings.talkback_line_always = True
_settings.talkback_line_keepalive_secs = 0.0


async def _lines():
    store._FILE.unlink(missing_ok=True)
    guard_mod._FILE.unlink(missing_ok=True)
    store.set_home_password("the-right-one")
    _Cams.rows = [_Cam("LOUNGE")]
    _LineSession.OUTCOME, _LineSession.opened = "ok", []
    L = lines_mod.Lines()
    L.start()

    check("from boot, the edge opens a talk line to the camera by itself",
          await _until(lambda: L.readiness("LOUNGE")["line"] == "open"))
    row = L.health()["LOUNGE"]
    check("it is recorded: opened once, with how long the camera took",
          row["opens"] == 1 and row["history"][-1]["event"] == "open"
          and row["elapsed_ms"] is not None)

    first = L._lines["LOUNGE"].session
    check("speech fed to an open line reaches the camera",
          await L.send("LOUNGE", b"\x01" * 160) and first.frames == [b"\x01" * 160])

    _hangs_up(L, "LOUNGE")                            # after a minute open
    check("when the camera ends the line, the edge re-opens it by itself",
          await _until(lambda: L._lines["LOUNGE"].opens == 2 and L.readiness("LOUNGE")["line"] == "open"))
    rec = L.health()["LOUNGE"]
    check("and the record says how long the camera kept it and that it came back",
          rec["drops"] == 1 and any(e["event"] == "dropped" and "after_secs" in e
                                    for e in rec["history"]))

    # A camera ending a line straight after granting it is refusing something:
    # that one is retried with a growing wait, not immediately.
    before = L._lines["LOUNGE"].retry
    _hangs_up(L, "LOUNGE", after=0.0)
    await _until(lambda: L.readiness("LOUNGE")["state"] == "connecting")
    check("a line ended straight after it opened is retried with a growing wait",
          L._lines["LOUNGE"].retry > before)
    L.kick("LOUNGE")
    await _until(lambda: L.readiness("LOUNGE")["line"] == "open")
    ok, state = await L.ensure_open("LOUNGE", 1.0)
    opened = len(_LineSession.opened)
    info = await L.test("LOUNGE")
    check("a test of an open line answers at once, with no second session",
          ok and info["line"] == "open" and len(_LineSession.opened) == opened)

    # The camera starts refusing the password.
    _LineSession.OUTCOME = "unauthorized"
    _hangs_up(L, "LOUNGE")
    check("a refused password is reported as such",
          await _until(lambda: L.readiness("LOUNGE")["state"] == "rejected"))
    tries = len(_LineSession.opened)
    await asyncio.sleep(0.4)
    check("and is NOT retried in a loop (the lock-out risk)",
          len(_LineSession.opened) == tries)
    refused = None
    try:
        await L.test("LOUNGE")
    except TalkbackError as exc:
        refused = exc
    check("a test of a refused camera says so, with the camera's reason",
          refused is not None and refused.code == "unauthorized")

    _LineSession.OUTCOME = "ok"
    store.set_home_password("the-new-one")            # someone fixed it
    L.kick()
    check("a new password is tried straight away, and the line comes up",
          await _until(lambda: L.readiness("LOUNGE")["line"] == "open"))

    # No credential at all: one try with the camera's own stream password.
    store._FILE.unlink(missing_ok=True)
    _Cams.rows = [_Cam("LOUNGE"), _Cam("PORCH", pw="streampw")]
    check("a camera with no password is reported as needing one",
          await _until(lambda: L.readiness("PORCH")["state"] == "needs_password"))
    check("after exactly one try with its own stream password",
          L._lines["PORCH"].stream_tried)

    # Unreachable: retried, with a growing wait.
    store.set_home_password("the-new-one")
    _LineSession.OUTCOME = "unreachable"
    _hangs_up(L, "LOUNGE")
    check("an unreachable camera is reported, not hidden",
          await _until(lambda: L.readiness("LOUNGE")["state"] == "unreachable"))
    check("and retried with a growing wait, not hammered",
          L._lines["LOUNGE"].retry > lines_mod._RETRY_MIN)
    _LineSession.OUTCOME = "ok"

    # A silent keep-alive, only when switched on.
    L.kick()
    await _until(lambda: L.readiness("LOUNGE")["line"] == "open")
    s = L._lines["LOUNGE"].session
    _settings.talkback_line_keepalive_secs = 0.15
    L.kick("LOUNGE")
    check("with keep-alive on, an idle line gets a silent frame (A-law 0xD5)",
          await _until(lambda: b"\xd5" * 160 in s.frames, 2.0))
    _settings.talkback_line_keepalive_secs = 0.0

    # Lines opened only while carers are connected.
    _settings.talkback_line_always = False
    L.kick()
    check("with lines on demand and no carer connected, lines are closed",
          await _until(lambda: L.readiness("LOUNGE")["line"] == "closed"))
    L.set_clients(1)
    check("a carer connecting to a camera opens them",
          await _until(lambda: L.readiness("LOUNGE")["line"] == "open"))
    L.set_clients(0)
    check("and the last one leaving closes them again",
          await _until(lambda: L.readiness("LOUNGE")["line"] == "closed"))
    _settings.talkback_line_always = True

    _Cams.rows = [_Cam("LOUNGE")]
    check("a camera removed from setup loses its line",
          await _until(lambda: "PORCH" not in L.all()))
    await L.stop()
    check("stopping closes every line", all(not ln.open for ln in L._lines.values()))
    store._FILE.unlink(missing_ok=True)
    guard_mod._FILE.unlink(missing_ok=True)


asyncio.run(_lines())


# ---------------------------------------------------------------------------
# The floor: who may speak into which camera, right now
# ---------------------------------------------------------------------------
print("\nThe floor")


class _Lines:
    """The line manager, scripted: which cameras are open, and what they got."""

    def __init__(self):
        self.open = {"LOUNGE": True, "LIVING_ROOM": True}
        self.sent: list = []
        self.clients = 0

    async def ensure_open(self, cid, timeout):
        return self.open.get(cid, False), ("ready" if self.open.get(cid) else "rejected")

    async def send(self, cid, frame):
        self.sent.append(cid)
        return True

    def readiness(self, cid):
        return {"state": "ready" if self.open.get(cid) else "rejected",
                "message": "m", "detail": "the camera refused the password"}

    def set_clients(self, n):
        self.clients = n


def _talker(cid, client_id, name, priority=0, user_id=""):
    """One carer's socket on one camera, recording what the edge did to it."""
    ended, closed = [], []

    async def end(code, message):
        ended.append((code, message))

    async def close(code, reason):
        closed.append(code)

    t = floor_mod.Talker(camera_id=cid, client_id=client_id, name=name, user_id=user_id,
                         holder="203.0.113.1", end=end, close=close, priority=priority)
    t.ended, t.closed = ended, closed
    return t


SPEECH = b"\x10" * 160
SILENCE = b"\xd5" * 160              # what a muted microphone sends


async def _floor():
    fl = _Lines()
    floor_mod.lines = fl
    floor_mod.CameraConfig = _Cams
    floor_mod.GAP_SECS = 0.2
    floor_mod.IDLE_CLOSE_SECS = 1.0
    audit_mod.PATH = _TMP / "talkback_log.jsonl"
    _settings.talkback_floor_hold_secs = 0.4
    _Cams.rows = [_Cam("LOUNGE"), _Cam("LIVING_ROOM", "LIVING ROOM")]
    hub = floor_mod.TalkbackHub()
    hub.start()

    priya = _talker("LOUNGE", "p1", "Nurse Priya", user_id="u-17")
    check("connecting to a free camera grants its floor at once",
          await hub.open(priya) is None and hub.floor("LOUNGE")["by"] == "Nurse Priya")
    check("each connected carer is counted (lines on demand follow it)", fl.clients == 1)
    r = await hub.open(_talker("LOUNGE", "a1", "Nurse Arun"))
    check("a second carer on the same camera is refused, and told who",
          r is not None and r["code"] == "busy" and "Nurse Priya" in r["message"])
    check("and is not counted as connected", fl.clients == 1)
    arun = _talker("LIVING_ROOM", "a1", "Nurse Arun")
    check("while two carers talk into two cameras at the same time",
          await hub.open(arun) is None and hub.floor("LIVING_ROOM")["by"] == "Nurse Arun"
          and hub.floor("LOUNGE")["by"] == "Nurse Priya")

    check("the speaker's audio goes to that camera's line",
          await hub.audio(priya, SPEECH) is None and fl.sent[-1] == "LOUNGE")
    for _ in range(12):
        await hub.audio(priya, SPEECH)
        await asyncio.sleep(0.05)
    check("there is no time limit while the button is held",
          hub.floor("LOUNGE")["state"] == "speaking")

    hub.release(priya)
    check("letting go keeps the floor briefly (the floor hold)",
          hub.floor("LOUNGE")["state"] == "holding")
    sent = len(fl.sent)
    await hub.audio(priya, SILENCE)
    check("digital silence on a held floor goes nowhere and claims nothing",
          len(fl.sent) == sent and hub.floor("LOUNGE")["state"] == "holding")
    r = await hub.open(_talker("LOUNGE", "a2", "Nurse Arun"))
    check("inside the hold another carer waits, told for how long",
          r is not None and "free in" in r["message"])
    await hub.audio(priya, SPEECH)
    check("but the same carer carries on at once (a conversation)",
          hub.floor("LOUNGE")["state"] == "speaking")
    check("when speech stops the floor is held, then free — the socket may stay open",
          await _until(lambda: hub.floor("LOUNGE")["state"] == "free", 2.0)
          and priya in hub._talkers)

    talk = audit_mod.recent("LOUNGE", 1)[0]
    check("the talk is logged: who, which room, when, and how long they spoke",
          talk["event"] == "talk" and talk["name"] == "Nurse Priya"
          and talk["user_id"] == "u-17" and talk["camera_name"] == "LOUNGE"
          and talk["talk_secs"] >= 0.2 and talk["started_at"] and talk["ended_at"])
    check("and so are refusals, with why",
          any(e["event"] == "refused" and e["code"] == "busy"
              for e in audit_mod.recent("LOUNGE", 20)))

    arun2 = _talker("LOUNGE", "a3", "Nurse Arun")
    check("so the next carer gets it", await hub.open(arun2) is None)
    r = await hub.audio(priya, SPEECH)
    check("speaking again on an old open socket while someone else has the room is refused",
          r is not None and r["code"] == "busy")
    hub.release(arun2)
    await hub.leave(arun2)
    await _until(lambda: hub.floor("LOUNGE")["state"] == "free", 2.0)
    r = await hub.audio(priya, SPEECH)
    check("speech on an open socket takes a free floor again, instantly",
          r is None and hub.floor("LOUNGE")["by"] == "Nurse Priya")

    # A carer whose network drops mid-sentence.
    await hub.leave(priya)
    check("a socket that drops mid-sentence keeps its floor for the hold",
          hub.floor("LOUNGE")["state"] == "holding")
    back = _talker("LOUNGE", "p1", "Nurse Priya")
    check("and reconnecting with the same client_id carries on",
          await hub.open(back) is None and hub.floor("LOUNGE")["client_id"] == "p1")
    dup = _talker("LOUNGE", "p1", "Nurse Priya")
    await hub.open(dup)
    await asyncio.sleep(0.01)
    check("a second socket from the same control replaces the first",
          back.closed == [1000] and hub._floors["LOUNGE"].talker is dup)
    hub.release(dup)
    await _until(lambda: hub.floor("LOUNGE")["state"] == "free", 2.0)
    check("an idle socket holding nothing is closed after hold_secs",
          await _until(lambda: dup.closed == [1000], 2.0))

    # Priority (future doctor role): only ever raised from a verified source.
    carer = _talker("LOUNGE", "c1", "Nurse Arun")
    await hub.open(carer)
    doctor = _talker("LOUNGE", "d1", "Dr Rao", priority=10)
    check("a higher-priority carer takes the floor (the future doctor role)",
          await hub.open(doctor) is None and hub.floor("LOUNGE")["by"] == "Dr Rao")
    await asyncio.sleep(0.01)
    check("and the carer it was taken from is told who took it",
          carer.ended and carer.ended[0][0] == "taken" and "Dr Rao" in carer.ended[0][1])
    check("everyone is priority 0 today, so nobody can take over anybody",
          priya.priority == arun.priority == 0)

    # A camera whose line is not open.
    fl.open["LIVING_ROOM"] = False
    hub.release(arun)
    await hub.leave(arun)
    await _until(lambda: hub.floor("LIVING_ROOM")["state"] == "free", 2.0)
    r = await hub.open(_talker("LIVING_ROOM", "x1", "Nurse X"))
    check("a camera that cannot be reached is refused with the camera's reason",
          r is not None and r["code"] == "unauthorized" and "refused" in r["message"])
    check("and no floor is left reserved behind the refusal",
          hub.floor("LIVING_ROOM")["state"] == "free")
    r = await hub.open(_talker("GARAGE", "x2", "Nurse X"))
    check("an unknown camera is refused as such", r is not None and r["code"] == "no_camera")

    await hub.stop()
    check("stopping the service logs the talk still in progress",
          audit_mod.recent("LOUNGE", 1)[0]["ended"] == "shutdown")
    for i in range(3):
        audit_mod.record({"event": "talk", "camera_id": "PORCH", "n": i})
    check("the log reads newest first, filtered by camera",
          [e["n"] for e in audit_mod.recent("PORCH", 2)] == [2, 1])


asyncio.run(_floor())


# ---------------------------------------------------------------------------
# The duplex probe: is the room audible WHILE the camera's speaker plays?
# ---------------------------------------------------------------------------
print("\nDuplex probe")

import contextlib                                                  # noqa: E402
import io as _io                                                   # noqa: E402
import types                                                       # noqa: E402

from tools import talkback as cli                                  # noqa: E402

_PLAYING = [False]


class _FakeStream:
    """ffmpeg reading the camera's audio: a steady room, silenced or not while
    the speaker plays."""

    def __init__(self, gated):
        self.stdout = asyncio.StreamReader()
        self._task = asyncio.ensure_future(self._feed(gated))

    async def _feed(self, gated):
        while True:
            amp = 0 if gated and _PLAYING[0] else 3000
            self.stdout.feed_data(struct.pack("<800h", *([amp, -amp] * 400)))
            await asyncio.sleep(0.1)

    def kill(self):
        self._task.cancel()

    async def wait(self):
        return 0


async def _fake_speak(camera_id, audio, marks=None):
    loop = asyncio.get_running_loop()
    marks["start"], _PLAYING[0] = loop.time(), True
    await asyncio.sleep(len(audio) / 8000)
    marks["end"], _PLAYING[0] = loop.time(), False
    return 0


async def _probe(gated):
    async def spawn(*args, **kw):
        return _FakeStream(gated)
    real_spawn, real_which = asyncio.create_subprocess_exec, shutil.which
    asyncio.create_subprocess_exec, shutil.which = spawn, lambda name: "/usr/bin/" + name
    out = _io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = await cli._duplex("LOUNGE", 1.5)
    finally:
        asyncio.create_subprocess_exec, shutil.which = real_spawn, real_which
    return code, out.getvalue()


sys.modules.setdefault("livestream", types.ModuleType("livestream"))
sys.modules["livestream.mediamtx_client"] = types.SimpleNamespace(
    local_rtsp_url=lambda cid: "rtsp://127.0.0.1:8554/" + cid)
cli._speak = _fake_speak
code, text = asyncio.run(_probe(gated=False))
check("a camera that keeps its microphone live while talking is reported FULL DUPLEX",
      code == 0 and "FULL DUPLEX" in text)
code, text = asyncio.run(_probe(gated=True))
check("a camera that silences its microphone while its speaker plays is caught",
      code == 1 and "HALF DUPLEX IN THE CAMERA" in text)


# ---------------------------------------------------------------------------
# One source for the "password refused" advice
# ---------------------------------------------------------------------------
print("\nRefusal advice")

from talkback import advice                                        # noqa: E402

check("the protocol error carries the shared checklist",
      advice.refused_detail().startswith("The camera refused")
      and "Third-Party Compatibility" in advice.refused_detail())
check("re-pairing is the LAST step, not the first",
      "remove the camera" in advice.REFUSED_STEPS[-1].lower()
      and all("remove the camera" not in s.lower() for s in advice.REFUSED_STEPS[:-1]))
check("a refused camera tells the carer the short version",
      lines_mod._SAY["rejected"] == advice.REFUSED_SHORT)

# ---------------------------------------------------------------------------
shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All talk-back checks passed.")
