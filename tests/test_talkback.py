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
  * one camera cannot be given two speakers at once, INCLUDING when the second
    request arrives during the first one's handshake

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
from talkback import sessions as hub_mod                           # noqa: E402
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
# One speaker per camera
# ---------------------------------------------------------------------------
print("\nOne speaker per camera")


class _FakeSession:
    """A session whose handshake takes long enough to race against."""

    def __init__(self, *a, **k):
        self.session_id = "s1"
        self.host = "10.0.0.9"
        self.challenge = ""
        self.closed = False

    async def open(self):
        await asyncio.sleep(0.05)

    async def start_audio(self):
        pass

    async def close(self):
        self.closed = True


hub_mod.TapoTalkSession = _FakeSession
hub_mod.TalkbackHub._resolve = lambda self, cid: (
    type("C", (), {"camera_id": cid, "camera_name": cid})(), "10.0.0.9", object())

_hub = hub_mod.TalkbackHub()


async def _second(hub, cid):
    await asyncio.sleep(0.01)                    # arrive DURING the handshake
    return await hub.open(cid, "second carer")


async def _race():
    first, second = await asyncio.gather(
        _hub.open("cam_1", "203.0.113.7", label="first carer"), _second(_hub, "cam_1"),
        return_exceptions=True)
    check("the first speaker gets the camera", not isinstance(first, Exception))
    check("a second speaker arriving mid-handshake is REFUSED, not queued",
          isinstance(second, TalkbackError) and second.code == "busy")
    check("the busy message names who is holding it — by name, not address",
          isinstance(second, TalkbackError) and "first carer" in str(second)
          and "203.0.113.7" not in str(second))
    await _hub.release("cam_1", first)
    check("releasing frees the camera", not _hub.busy("cam_1"))
    third = await _hub.open("cam_1", "next carer")
    check("and the next carer can then talk", _hub.busy("cam_1"))
    await _hub.release("cam_1", third)


asyncio.run(_race())


async def _failure_releases():
    """A camera that refuses mid-handshake must not stay locked — this is the
    difference between one bad attempt and a household losing its intercom."""
    class _Failing(_FakeSession):
        async def open(self):
            raise TalkbackError("unreachable", "no answer")

    hub_mod.TapoTalkSession = _Failing
    hub = hub_mod.TalkbackHub()
    try:
        await hub.open("cam_2", "carer")
    except TalkbackError:
        pass
    check("a failed handshake leaves the camera free", not hub.busy("cam_2"))


asyncio.run(_failure_releases())


# ---------------------------------------------------------------------------
# Getting back in after a drop
# ---------------------------------------------------------------------------
print("\nReclaiming a session after a drop")


async def _takeover():
    """A carer whose network dropped must be able to reclaim their OWN session.

    The device cannot always tell that a WebSocket died. Without this, the
    holder of a camera would be a socket that no longer exists, and the person
    it belonged to would be refused from their own microphone until the hold
    window expired — precisely when getting back matters most."""
    hub_mod.TapoTalkSession = _FakeSession
    hub = hub_mod.TalkbackHub()

    first = await hub.open("cam_3", "carer A", client_id="phone-1")
    check("the first carer holds the camera", hub.busy("cam_3"))

    other = None
    try:
        await hub.open("cam_3", "carer B", client_id="phone-2")
    except TalkbackError as exc:
        other = exc
    check("a DIFFERENT client is still refused",
          other is not None and other.code == "busy")

    back = await hub.open("cam_3", "carer A", client_id="phone-1")
    check("the SAME client reclaims it instead of bouncing off itself",
          back is not first and hub.busy("cam_3"))
    check("the stale session is closed, so the camera is not held twice",
          first.closed)

    nameless = None
    try:
        await hub.open("cam_3", "carer C", client_id="")
    except TalkbackError as exc:
        nameless = exc
    check("a client with NO id can never take a session over",
          nameless is not None and nameless.code == "busy")
    await hub.release("cam_3", back)


asyncio.run(_takeover())


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
# A background self-check never costs a carer a sentence
# ---------------------------------------------------------------------------
print("\nThe self-check yields to people")


async def _preempt():
    hub_mod.TapoTalkSession = _FakeSession
    hub = hub_mod.TalkbackHub()

    check_session = await hub.open("cam_9", "self-check", preemptible=True)
    carer = await hub.open("cam_9", "carer")
    check("a carer who presses during a self-check TAKES the camera",
          hub.busy("cam_9") and hub._active["cam_9"].holder == "carer")
    check("and the self-check's session is closed, not left open",
          check_session.closed)

    refused = None
    try:
        await hub.open("cam_9", "self-check", preemptible=True)
    except TalkbackError as exc:
        refused = exc
    check("a self-check never takes a camera off a carer",
          refused is not None and refused.code == "busy"
          and hub._active["cam_9"].holder == "carer")
    await hub.release("cam_9", carer)


asyncio.run(_preempt())


# ---------------------------------------------------------------------------
# Readiness: known before anyone presses
# ---------------------------------------------------------------------------
print("\nReadiness, known before the press")

from talkback import readiness as rd_mod                           # noqa: E402


class _Cam:
    def __init__(self, cid, pw=""):
        self.camera_id = cid
        self.camera_name = cid
        self.is_enabled = True
        self.rtsp_url = f"rtsp://isw:{pw}@10.0.0.5:554/stream1" if pw else "rtsp://10.0.0.5/s"
        self.onvif_username = None
        self.onvif_password = None
        self.onvif_xaddr = ""


class _Cams:
    rows: list = []

    def get_all(self):
        return list(_Cams.rows)


class _FakeHub:
    """Answers probes from a script: camera id -> result or error code."""

    def __init__(self):
        self.script = {}
        self.calls = []
        self.held = set()

    def busy(self, cid):
        return cid in self.held

    async def probe(self, cid, preemptible=False):
        self.calls.append((cid, preemptible))
        outcome = self.script.get(cid, "ok")
        if outcome != "ok":
            raise TalkbackError(outcome, f"{cid}: {outcome}")
        return {"elapsed_ms": 42}


async def _readiness():
    store._FILE.unlink(missing_ok=True)
    fake = _FakeHub()
    rd_mod.hub = fake
    rd_mod.CameraConfig = _Cams
    attempts = []

    async def no_stream_login(host, port, username="", password="", timeout=8.0):
        attempts.append(username)
        if not username:
            return "HTTP/1.1 401", 'Digest realm="x",encrypt_type="3"'
        return "HTTP/1.1 401 Unauthorized", ""

    rd_mod.attempt = no_stream_login
    _Cams.rows = [_Cam("LOUNGE", pw="streampw"), _Cam("LIVING_ROOM")]
    r = rd_mod.Readiness()

    await r.check_now()
    check("with no password, a camera is reported as needing one",
          r.get("LOUNGE")["state"] == "needs_password")
    check("and nothing was probed — there was nothing to probe with",
          fake.calls == [])
    check("the camera's OWN stream password was tried, once",
          attempts.count("admin") == 1)
    await r.check_now()
    check("and not tried again on the next check",
          attempts.count("admin") == 1)

    store.set_home_password("the-right-one")
    fake.script = {"LOUNGE": "ok", "LIVING_ROOM": "unauthorized"}
    await r.check_now()
    check("after the home password: a camera that accepts it is READY",
          r.get("LOUNGE")["state"] == "ready")
    check("one that refuses it is REJECTED, with advice to re-pair",
          r.get("LIVING_ROOM")["state"] == "rejected"
          and "Tapo app" in r.get("LIVING_ROOM")["message"])
    check("every probe was the pre-emptible kind",
          all(pre for _, pre in fake.calls))

    import time as _t
    check("a refused camera is NOT retried quickly (lock-out risk)",
          r._due["LIVING_ROOM"] - _t.monotonic() > 3000)
    check("a ready camera is re-confirmed within the quarter hour",
          r._due["LOUNGE"] - _t.monotonic() <= 600)

    fake.held.add("LOUNGE")
    fake.calls.clear()
    await r.check_now()
    check("a camera a carer is talking through is left alone",
          ("LOUNGE", True) not in fake.calls and r.get("LOUNGE")["state"] == "ready")
    fake.held.clear()

    r.observe("LOUNGE", "unauthorized", "refused on a real press")
    check("a REAL refused session updates readiness at once",
          r.get("LOUNGE")["state"] == "rejected")
    r.observe("LOUNGE")
    check("and a real success restores it",
          r.get("LOUNGE")["state"] == "ready")

    fake.script["LIVING_ROOM"] = "ok"            # re-paired in the Tapo app
    await r.check_now()
    check("a re-paired camera comes back to ready with no one touching the device",
          r.get("LIVING_ROOM")["state"] == "ready")

    _Cams.rows = [_Cam("LOUNGE")]
    await r.check_now()
    check("a camera removed from setup drops out of readiness",
          "LIVING_ROOM" not in r.all())

    # A firmware that DOES accept its stream password: zero-input commissioning.
    store._FILE.unlink(missing_ok=True)

    async def stream_login_ok(host, port, username="", password="", timeout=8.0):
        if not username:
            return "HTTP/1.1 401", 'Digest realm="x",encrypt_type="3"'
        ok = password == hashlib.sha256(b"streampw").hexdigest().upper()
        return ("HTTP/1.1 200 OK" if ok else "HTTP/1.1 401"), ""

    rd_mod.attempt = stream_login_ok
    _Cams.rows = [_Cam("PORCH", pw="streampw")]
    fake.script = {}
    r2 = rd_mod.Readiness()
    await r2.check_now()
    check("a camera that accepts its own stream password needs NO setup at all",
          r2.get("PORCH")["state"] == "ready"
          and store.resolve(["PORCH"])["PORCH"]["source"] == "stream")
    store._FILE.unlink(missing_ok=True)


asyncio.run(_readiness())


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


async def _guard_in_the_hub():
    """The hub must consult the guard BEFORE dialling, count only refusals,
    and give `force` to a technician."""
    dialled = []

    class _Refusing(_FakeSession):
        async def open(self):
            dialled.append(1)
            raise TalkbackError("unauthorized", "refused")

    class _Offline(_FakeSession):
        async def open(self):
            dialled.append(1)
            raise TalkbackError("unreachable", "no answer")

    hub = hub_mod.TalkbackHub()
    cred = TalkCredential(md5="C" * 32, sha256="C" * 64)
    hub_mod.TalkbackHub._resolve = lambda self, cid: (
        type("C", (), {"camera_id": cid, "camera_name": cid})(), "10.0.0.9", cred)

    hub_mod.TapoTalkSession = _Offline
    for _ in range(5):
        try:
            await hub.open("G1", "carer")
        except TalkbackError:
            pass
    check("an OFFLINE camera never earns a pause (it said nothing about the password)",
          guard_mod.paused_until("G1", cred) == 0)

    hub_mod.TapoTalkSession = _Refusing
    for _ in range(3):
        try:
            await hub.open("G2", "carer")
        except TalkbackError:
            pass
    before = len(dialled)
    fourth = None
    try:
        await hub.open("G2", "carer")
    except TalkbackError as exc:
        fourth = exc
    check("after three refusals the fourth attempt is refused WITHOUT dialling",
          fourth is not None and fourth.code == "cooldown" and len(dialled) == before)
    check("and it does not leave the camera marked busy", not hub.busy("G2"))

    hub_mod.TapoTalkSession = _FakeSession
    session = await hub.open("G2", "technician", force=True)
    check("`force` lets a technician through the pause", hub.busy("G2"))
    check("a success clears the pause", guard_mod.paused_until("G2", cred) == 0)
    await hub.release("G2", session)
    guard_mod._FILE.unlink(missing_ok=True)


asyncio.run(_guard_in_the_hub())


# ---------------------------------------------------------------------------
# One camera, many spellings: a session is always given back
# ---------------------------------------------------------------------------
print("\nCamera ids are canonical")


async def _spellings():
    hub = hub_mod.TalkbackHub()
    hub_mod.TapoTalkSession = _FakeSession
    # The camera is KEPT as LIVING_ROOM; callers may spell it otherwise.
    hub_mod.TalkbackHub._resolve = lambda self, cid: (
        type("C", (), {"camera_id": "LIVING_ROOM", "camera_name": "LIVING ROOM"})(),
        "10.0.0.9", TalkCredential(md5="D" * 32, sha256="D" * 64))
    session = await hub.open("living room", "carer", label="Nurse Priya")
    check("a session opened under another spelling is kept under the real id",
          "LIVING_ROOM" in hub._active)
    refused = None
    try:
        await hub.open("LIVING_ROOM", "someone else")
    except TalkbackError as exc:
        refused = exc
    check("the busy message names the CARER, never an address",
          refused is not None and "Nurse Priya" in str(refused)
          and "carer" not in str(refused))
    await hub.release("living room", session)
    check("released under a different spelling, the camera is free again",
          not hub._active)


asyncio.run(_spellings())


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
check("readiness tells the carer the short version",
      rd_mod._SAY["rejected"] == advice.REFUSED_SHORT)

# ---------------------------------------------------------------------------
shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All talk-back checks passed.")
