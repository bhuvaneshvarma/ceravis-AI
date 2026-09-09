#!/usr/bin/env python3
"""
Tapo camera SPEAKER (two-way audio) — standalone throwaway test tool.

THIS IS AN EXPERIMENT. It is deliberately outside `edge/`, imports nothing from
it, is imported by nothing, and is removed with `rm -rf experiments/`.

What it does
------------
Pushes audio out of a Tapo camera's built-in speaker over TP-Link's own local
protocol (the one the Tapo phone app's "talk" button uses). No cloud round-trip,
no middleman server, no extra Python packages — standard library only.

The protocol, in full
---------------------
  1. Plain TCP to <camera>:8800 (NOT the ONVIF port, NOT TLS).
  2. POST /stream, Content-Type: multipart/mixed; boundary=--client-stream-boundary--
     The camera answers 401 with a Digest challenge. The digest password is NOT
     the RTSP/camera-account password — it is a hash of the TP-LINK CLOUD ACCOUNT
     password:
         encrypt_type="3"  ->  SHA256(cloud_pw).hexdigest().upper()
         otherwise         ->  MD5(cloud_pw).hexdigest().upper()
     with username "admin". (Some old firmwares advertise username="none", the
     CVE-2022-37255 fixed-credential case; handled below.)
  3. Re-send the request with the Authorization header -> 200 OK. The socket then
     stays open and both sides stream multipart parts over it.
  4. Ask for a talk session:
         {"params":{"talk":{"mode":"aec"},"method":"get"},"seq":3,"type":"request"}
     The camera replies with params.session_id.
  5. Push audio as MPEG-TS parts on that session:
         Content-Type: audio/mp2t, X-If-Encrypt: 0, X-Session-Id: <session>
     The TS carries ONE elementary stream, stream_type 0x90 — TP-Link's private
     value for G.711 A-law @ 8 kHz mono. (0x91 = PCMU/16000 shows up on some
     newer firmware in the RECEIVE direction; A-law is what the speaker takes.)

Only the camera->client media is AES-encrypted. Our outbound audio goes with
X-If-Encrypt: 0, so there is no crypto on the send path at all.

Reference: AlexxIT/go2rtc pkg/tapo (MIT). This file is an independent Python
reimplementation of that wire format; no go2rtc code is vendored or required.

Usage
-----
    python3 tapo_talk.py probe --host 192.168.0.250 --cloud-password 'PW'
    python3 tapo_talk.py tone  --host 192.168.0.250 --cloud-password 'PW' --seconds 2
    python3 tapo_talk.py play  --host 192.168.0.250 --cloud-password 'PW' --file alert.wav
    python3 tapo_talk.py say   --host 192.168.0.250 --cloud-password 'PW' --text "Please sit down"

`probe` sends NO audio — it only proves auth + session open. Run it first.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

DEFAULT_PORT = 8800
CLIENT_BOUNDARY = b"--client-stream-boundary--"
DEVICE_BOUNDARY = b"--device-stream-boundary--"

TS_PACKET = 188
TS_SYNC = 0x47
PAT_PID = 0x0000
PMT_PID = 0x1000
PES_PID = 0x0100
STREAM_TYPE_PCMA_TAPO = 0x90
STREAM_ID_AUDIO = 0xC0

SAMPLE_RATE = 8000          # G.711 is 8 kHz mono, full stop
CHUNK_MS = 20               # one 160-byte A-law frame
PART_MS = 100               # how much audio goes in one HTTP part


# --------------------------------------------------------------------------- #
# MPEG-TS muxer (PAT + PMT + one PES stream). No dependencies.
# --------------------------------------------------------------------------- #

def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def _psi_packet(pid: int, table: bytes) -> bytes:
    """One 188-byte TS packet carrying a complete PSI section."""
    head = bytes([TS_SYNC, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    section = table + struct.pack(">I", _crc32_mpeg(table))
    body = b"\x00" + section                      # pointer_field + section
    return (head + body).ljust(TS_PACKET, b"\x00")


def _psi_header(table_id: int, content_len: int) -> bytes:
    section_len = 5 + content_len + 4             # 5 below + content + CRC32
    return bytes([
        table_id,
        0xB0 | ((section_len >> 8) & 0x0F), section_len & 0xFF,
        0x00, 0x01,                               # table id extension
        0xC1,                                     # version 0, current
        0x00, 0x00,                               # section 0 of 0
    ])


def pat_packet() -> bytes:
    content = struct.pack(">HH", 1, 0xE000 | PMT_PID)
    return _psi_packet(PAT_PID, _psi_header(0x00, len(content)) + content)


def pmt_packet() -> bytes:
    content = struct.pack(">HH", 0xE000 | 0x1FFF, 0xF000)     # no PCR, no program info
    content += bytes([STREAM_TYPE_PCMA_TAPO]) + struct.pack(">HH", 0xE000 | PES_PID, 0xF000)
    return _psi_packet(PMT_PID, _psi_header(0x02, len(content)) + content)


def _write_pts(pts: int) -> bytes:
    return bytes([
        0x20 | ((pts >> 29) & 0x0E) | 1,
        (pts >> 22) & 0xFF,
        ((pts >> 14) & 0xFE) | 1,
        (pts >> 7) & 0xFF,
        ((pts << 1) & 0xFE) | 1,
    ])


class TsMuxer:
    """Wraps A-law frames into TS packets on PES_PID. One instance per session."""

    def __init__(self) -> None:
        self.counter = 0
        self.pts = 0

    def header(self) -> bytes:
        return pat_packet() + pmt_packet()

    def payload(self, audio: bytes, pts_step: int) -> bytes:
        pes = b"\x00\x00\x01" + bytes([STREAM_ID_AUDIO])
        size = 3 + 5 + len(audio)
        pes += struct.pack(">H", size if size <= 0xFFFF else 0)
        pes += bytes([0x80, 0x80, 5]) + _write_pts(self.pts) + audio
        self.pts = (self.pts + pts_step) & 0xFFFFFFFF

        out = bytearray()
        first = True
        while pes:
            pusi = 0x40 if first else 0x00
            first = False
            if len(pes) < TS_PACKET - 4:
                stuff = TS_PACKET - 4 - 1 - len(pes)
                out += bytes([TS_SYNC, pusi | (PES_PID >> 8), PES_PID & 0xFF,
                              0x30 | (self.counter & 0x0F), stuff]) + bytes(stuff) + pes
                pes = b""
            else:
                out += bytes([TS_SYNC, pusi | (PES_PID >> 8), PES_PID & 0xFF,
                              0x10 | (self.counter & 0x0F)]) + pes[:TS_PACKET - 4]
                pes = pes[TS_PACKET - 4:]
            self.counter += 1
        return bytes(out)


# --------------------------------------------------------------------------- #
# The camera connection
# --------------------------------------------------------------------------- #

class TapoError(RuntimeError):
    pass


def _between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i)
    return text[i:j] if j >= 0 else ""


class TapoTalk:
    def __init__(self, host: str, cloud_password: str, port: int = DEFAULT_PORT,
                 timeout: float = 10.0, verbose: bool = True) -> None:
        self.host = host
        self.port = port
        self.cloud_password = cloud_password
        self.timeout = timeout
        self.verbose = verbose
        self.sock = None
        self.buf = b""
        self.session = ""
        self.challenge = ""

    # ---- plumbing --------------------------------------------------------- #

    def _log(self, msg: str) -> None:
        if self.verbose:
            print("[tapo] " + msg, flush=True)

    def _request_bytes(self, authorization: str = "") -> bytes:
        lines = [
            "POST /stream HTTP/1.1",
            "Host: {}:{}".format(self.host, self.port),
            "User-Agent: ceravis-tapo-talk/0.1",
            "Content-Type: multipart/mixed; boundary=" + CLIENT_BOUNDARY.decode(),
        ]
        if authorization:
            lines.append("Authorization: " + authorization)
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise TapoError("camera closed the connection")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(marker)
        return head

    def _read_exactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise TapoError("camera closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_http_response(self):
        head = self._read_until(b"\r\n\r\n").decode("utf-8", "replace")
        status_line, _, rest = head.partition("\r\n")
        status = int(status_line.split(" ")[1])
        headers = {}
        for line in rest.split("\r\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        body = self._read_exactly(length) if length else b""
        return status, headers, body

    def _digest(self, auth: str) -> str:
        realm = _between(auth, 'realm="', '"')
        nonce = _between(auth, 'nonce="', '"')
        qop = _between(auth, 'qop="', '"') or "auth"
        opaque = _between(auth, 'opaque="', '"')

        if 'username="none"' in auth:
            # CVE-2022-37255 firmware: fixed built-in credentials.
            username, password = "none", "TPL075526460603"
        else:
            username = "admin"
            raw = self.cloud_password.encode()
            if 'encrypt_type="3"' in auth:
                password = hashlib.sha256(raw).hexdigest().upper()
            else:
                password = hashlib.md5(raw).hexdigest().upper()

        def h(*parts):
            return hashlib.md5(":".join(parts).encode()).hexdigest()

        nc, cnonce = "00000001", os.urandom(16).hex()
        ha1 = h(username, realm, password)
        ha2 = h("POST", "/stream")
        response = h(ha1, nonce, nc, cnonce, qop, ha2)
        header = ('Digest username="{}", realm="{}", nonce="{}", uri="/stream", '
                  'qop={}, nc={}, cnonce="{}", response="{}"').format(
                      username, realm, nonce, qop, nc, cnonce, response)
        if opaque:
            header += ', opaque="{}", algorithm=MD5'.format(opaque)
        return header

    # ---- session ---------------------------------------------------------- #

    def connect(self) -> None:
        self._log("connecting to {}:{}".format(self.host, self.port))
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)

        self.sock.sendall(self._request_bytes())
        status, headers, _ = self._read_http_response()
        auth = headers.get("www-authenticate", "")
        if status != 401 or not auth.startswith("Digest"):
            raise TapoError("expected a 401 Digest challenge, got HTTP {} ({})".format(
                status, auth or "no challenge"))
        self.challenge = auth
        self._log("challenge: " + auth)

        self.sock.sendall(self._request_bytes(self._digest(auth)))
        status, headers, _ = self._read_http_response()
        if status != 200:
            raise TapoError(
                "authentication failed: HTTP {}. The password here is the TP-LINK CLOUD "
                "ACCOUNT password, not the camera/RTSP account password.".format(status))
        self._log("authenticated (HTTP 200), stream socket open")

    def _send_part(self, content_type: str, body: bytes, session: str = "",
                   encrypt: bool = False) -> None:
        head = [b"--" + CLIENT_BOUNDARY, ("Content-Type: " + content_type).encode()]
        if content_type != "application/json":
            head.append(b"X-If-Encrypt: " + (b"1" if encrypt else b"0"))
            if session:
                head.append(("X-Session-Id: " + session).encode())
        head.append(("Content-Length: " + str(len(body))).encode())
        self.sock.sendall(b"\r\n".join(head) + b"\r\n\r\n" + body + b"\r\n")

    def _read_device_part(self):
        self._read_until(DEVICE_BOUNDARY)
        head = self._read_until(b"\r\n\r\n").decode("utf-8", "replace")
        headers = {}
        for line in head.split("\r\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        return headers, self._read_exactly(length)

    def open_talk(self, mode: str = "aec") -> str:
        req = ('{"params":{"talk":{"mode":"%s"},"method":"get"},"seq":3,"type":"request"}'
               % mode).encode()
        self._send_part("application/json", req)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            headers, body = self._read_device_part()
            if "json" not in headers.get("content-type", ""):
                continue
            text = body.decode("utf-8", "replace")
            self._log("talk response: " + text.strip())
            sid = _between(text, '"session_id":"', '"') or _between(text, '"session_id": "', '"')
            if not sid:                       # some firmwares answer with a number
                sid = _between(text, '"session_id":', ",").strip().strip('}"')
            if sid:
                self.session = sid
                return sid
            if '"error_code"' in text and '"error_code":0' not in text.replace(" ", ""):
                raise TapoError("camera refused the talk session: " + text.strip())
        raise TapoError("timed out waiting for a talk session id")

    def send_alaw(self, alaw: bytes, realtime: bool = True) -> None:
        """Push A-law audio (8 kHz mono) to the speaker, paced like a live mic."""
        muxer = TsMuxer()
        self._send_part("audio/mp2t", muxer.header(), self.session)

        frame = SAMPLE_RATE * CHUNK_MS // 1000                 # 160 bytes / 20 ms
        per_part = frame * (PART_MS // CHUNK_MS)
        pts_step = 90000 * CHUNK_MS // 1000                    # 90 kHz clock
        start = time.monotonic()
        sent_ms = 0

        for off in range(0, len(alaw), per_part):
            block = alaw[off:off + per_part]
            ts = b""
            for i in range(0, len(block), frame):
                ts += muxer.payload(block[i:i + frame], pts_step)
            self._send_part("audio/mp2t", ts, self.session)
            sent_ms += len(block) * 1000 // SAMPLE_RATE
            if realtime:
                behind = (start + sent_ms / 1000.0) - time.monotonic()
                if behind > 0:
                    time.sleep(behind)
        self._log("sent {:.2f}s of audio ({} bytes A-law)".format(sent_ms / 1000.0, len(alaw)))
        # let the camera drain its jitter buffer before we drop the socket
        time.sleep(0.6)

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None


# --------------------------------------------------------------------------- #
# Audio sources
# --------------------------------------------------------------------------- #

_ALAW_SEG_ENDS = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def linear_to_alaw(sample: int) -> int:
    """16-bit signed PCM -> one A-law byte (ITU-T G.711, the classic g711.c
    algorithm). Pure Python because `audioop` is deprecated and gone in 3.13,
    and this is a dozen lines. Verified byte-for-byte in selftest.py."""
    sample >>= 3                                   # 16-bit -> 13-bit
    if sample >= 0:
        mask = 0xD5                                # sign bit 1 = positive
    else:
        mask = 0x55
        sample = -sample - 1
    seg = 8
    for i, end in enumerate(_ALAW_SEG_ENDS):
        if sample <= end:
            seg = i
            break
    if seg >= 8:                                   # out of range
        return 0x7F ^ mask
    if seg < 2:
        val = (sample >> 1) & 0x0F
    else:
        val = (sample >> seg) & 0x0F
    return ((seg << 4) | val) ^ mask


def tone_alaw(seconds: float, freq: float = 880.0, volume: float = 0.5) -> bytes:
    n = int(SAMPLE_RATE * seconds)
    out = bytearray(n)
    for i in range(n):
        # short fade in/out so the speaker does not click
        env = min(1.0, i / 400.0, (n - i) / 400.0)
        s = int(volume * env * 32000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        out[i] = linear_to_alaw(s)
    return bytes(out)


def ffmpeg_to_alaw(path_or_url: str, ffmpeg: str = "ffmpeg", volume: float = 1.0) -> bytes:
    """Any file the device's ffmpeg can read -> 8 kHz mono A-law. Runs ONCE, off
    the audio path, and exits; nothing stays resident."""
    if shutil.which(ffmpeg) is None:
        raise TapoError(ffmpeg + " not found on this machine")
    cmd = [ffmpeg, "-v", "error", "-i", path_or_url, "-vn",
           "-af", "volume={}".format(volume), "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-c:a", "pcm_alaw", "-f", "alaw", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise TapoError("ffmpeg failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def espeak_to_alaw(text: str, voice: str = "en") -> bytes:
    """Optional offline TTS via espeak-ng, if it happens to be installed. Not a
    dependency — `play` with a pre-rendered wav is the supported path."""
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe is None:
        raise TapoError("espeak-ng is not installed; render a .wav on your laptop and use "
                        "`play --file` instead (sudo apt install espeak-ng to enable this)")
    proc = subprocess.run([exe, "-v", voice, "-s", "150", "--stdout", text], capture_output=True)
    if proc.returncode != 0:
        raise TapoError("espeak failed: " + proc.stderr.decode("utf-8", "replace").strip())
    tmp = "/tmp/ceravis_tapo_tts.wav"
    with open(tmp, "wb") as fh:
        fh.write(proc.stdout)
    try:
        return ffmpeg_to_alaw(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Play audio out of a Tapo camera speaker.")
    ap.add_argument("command", choices=["probe", "tone", "play", "say"])
    ap.add_argument("--host", required=True, help="camera IP, e.g. 192.168.0.250")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--cloud-password", default=os.environ.get("TAPO_CLOUD_PASSWORD", ""),
                    help="TP-Link CLOUD ACCOUNT password (or set TAPO_CLOUD_PASSWORD)")
    ap.add_argument("--mode", default="aec", help="talk mode: aec (default) or half")
    ap.add_argument("--seconds", type=float, default=2.0, help="tone length")
    ap.add_argument("--freq", type=float, default=880.0)
    ap.add_argument("--file", help="audio file for `play` (wav/mp3/anything ffmpeg reads)")
    ap.add_argument("--text", help="text for `say` (needs espeak-ng)")
    ap.add_argument("--volume", type=float, default=1.0, help="ffmpeg volume multiplier for `play`")
    ap.add_argument("--repeat", type=int, default=1, help="play the clip N times")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not args.cloud_password:
        print("error: --cloud-password (or TAPO_CLOUD_PASSWORD) is required", file=sys.stderr)
        return 2

    try:
        if args.command == "tone":
            audio = tone_alaw(args.seconds, args.freq)
        elif args.command == "play":
            if not args.file:
                print("error: play needs --file", file=sys.stderr)
                return 2
            audio = ffmpeg_to_alaw(args.file, volume=args.volume)
        elif args.command == "say":
            if not args.text:
                print("error: say needs --text", file=sys.stderr)
                return 2
            audio = espeak_to_alaw(args.text)
        else:
            audio = b""
    except TapoError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1

    client = TapoTalk(args.host, args.cloud_password, args.port, verbose=not args.quiet)
    t0 = time.monotonic()
    try:
        client.connect()
        session = client.open_talk(args.mode)
        print("talk session {} opened in {:.2f}s".format(session, time.monotonic() - t0))
        if args.command == "probe":
            print("PROBE OK — auth works and the camera granted a speaker session. "
                  "No audio was sent.")
            return 0
        for i in range(max(1, args.repeat)):
            if i:
                time.sleep(0.3)
            client.send_alaw(audio)
        print("done")
        return 0
    except TapoError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: network: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
