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

SECRET = "Sup3rSecret!Passw0rd"
store.set_password("cam_1", SECRET)
_disk = (_TMP / "talkback.json").read_text()
check("the plaintext password is NOWHERE on disk", SECRET not in _disk)
check("the MD5 hash is what got stored",
      hashlib.md5(SECRET.encode()).hexdigest().upper() in _disk)
check("nothing but hashes and a timestamp is kept",
      set(json.loads(_disk)["cam_1"]) == {"md5", "sha256", "updated_at"})
check("the stored credential answers a challenge correctly",
      store.get("cam_1").password_for(CHALLENGE)[1]
      == hashlib.md5(SECRET.encode()).hexdigest().upper())
check("the camera reports as configured", store.configured() == {"cam_1"})

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
        _hub.open("cam_1", "first carer"), _second(_hub, "cam_1"),
        return_exceptions=True)
    check("the first speaker gets the camera", not isinstance(first, Exception))
    check("a second speaker arriving mid-handshake is REFUSED, not queued",
          isinstance(second, TalkbackError) and second.code == "busy")
    check("the busy message names who is holding it",
          isinstance(second, TalkbackError) and "first carer" in str(second))
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
shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All talk-back checks passed.")
