from __future__ import annotations

"""
The Tapo talk-back protocol — the camera's own local speaker endpoint.

This is the WHOLE protocol; there is no SDK behind it and no cloud round-trip:

  1. Plain TCP to <camera>:8800. Not TLS, despite what the write-ups say, and
     not the ONVIF port.
  2. POST /stream with `Content-Type: multipart/mixed; boundary=--client-stream-
     boundary--`. The camera answers 401 with a Digest challenge; we answer it
     with a hash of the TP-Link cloud password (talkback.credentials) and the
     socket becomes a two-way stream of multipart parts that stays open.
  3. `{"params":{"talk":{"mode":"aec"},...},"seq":3,"type":"request"}` opens a
     TALK session and returns its id. We deliberately never open the PREVIEW
     session (seq 1): asking for video here would make the camera open a second
     encoder connection for us, and the one-stream policy (MediaMTX pulls each
     camera exactly once) is worth more than anything this socket could show us.
  4. Audio goes back as MPEG-TS parts on that session id. Only the camera's
     OUTBOUND media is AES-encrypted; ours is sent with `X-If-Encrypt: 0`, so
     there is no crypto on the speaker path at all.

Asyncio because its one caller is a WebSocket handler in the API process: a
microphone is a slow trickle of 20 ms frames, and a thread parked on a blocking
socket per talking user is the wrong shape for that.

Everything that can fail here fails as TalkbackError with a sentence a support
engineer can act on — the failure modes (wrong password, firmware without the
talk API, someone already talking through the phone app) are indistinguishable
at the socket level and very distinguishable to a human.
"""

import asyncio
import hashlib
import logging
import os
import socket
import time

from .advice import refused_detail
from .mpegts import AudioMuxer, FRAME_MS

logger = logging.getLogger("talkback.protocol")

DEFAULT_PORT = 8800
CLIENT_BOUNDARY = b"--client-stream-boundary--"
DEVICE_BOUNDARY = b"--device-stream-boundary--"

_MAX_HEADER_BYTES = 16 * 1024        # a header block this big is a broken peer
_MAX_PART_BYTES = 4 * 1024 * 1024    # ditto for a part body

# Backpressure is measured in MILLISECONDS OF SPEECH, never in bytes, because
# milliseconds are what it costs: every byte sitting in this socket's write
# buffer is a word the room has not heard yet.
#
# TCP gives us no way to withdraw something already queued, so the only lever on
# a backlog is to stop adding to it. Past _QUEUE_STALE_MS the newest frame is
# dropped — the room hears a short gap instead of falling permanently further
# behind the carer's voice. Past _QUEUE_DEAD_MS the camera has stopped reading
# altogether, and the session is torn down so the client's reconnect can open a
# clean socket. That reconnect IS our drop-the-oldest: a new socket has no
# backlog, which is the only way to discard one.
_QUEUE_STALE_MS = 400
_QUEUE_DEAD_MS = 4000


class TalkbackError(RuntimeError):
    """A talk-back attempt failed. `code` is a stable, machine-readable reason;
    str() is the sentence shown to a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i)
    return text[i:j] if j >= 0 else ""


def digest_header(challenge: str, username: str, password: str,
                  method: str = "POST", uri: str = "/stream") -> str:
    """RFC 7616 Digest response for the camera's challenge. Separated out so the
    hard-to-eyeball part is directly testable."""
    realm = _between(challenge, 'realm="', '"')
    nonce = _between(challenge, 'nonce="', '"')
    qop = _between(challenge, 'qop="', '"') or "auth"
    opaque = _between(challenge, 'opaque="', '"')

    def h(*parts: str) -> str:
        return hashlib.md5(":".join(parts).encode()).hexdigest()

    nc, cnonce = "00000001", os.urandom(16).hex()
    response = h(h(username, realm, password), nonce, nc, cnonce, qop, h(method, uri))
    header = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
              f'uri="{uri}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}"')
    if opaque:
        header += f', opaque="{opaque}", algorithm=MD5'
    return header


def parse_headers(block: str) -> tuple[str, dict[str, str]]:
    """An HTTP head -> (status line, lower-cased headers)."""
    first, _, rest = block.partition("\r\n")
    headers = {}
    for line in rest.split("\r\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return first, headers


def request_bytes(host: str, port: int, authorization: str = "") -> bytes:
    """The one POST /stream every path here sends, with or without auth."""
    lines = [
        "POST /stream HTTP/1.1",
        f"Host: {host}:{port}",
        "User-Agent: ceravis-edge",
        f"Content-Type: multipart/mixed; boundary={CLIENT_BOUNDARY.decode()}",
    ]
    if authorization:
        lines.append(f"Authorization: {authorization}")
    return ("\r\n".join(lines) + "\r\n" + "\r\n").encode()


async def _head(reader, timeout: float) -> tuple[str, dict[str, str]]:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except asyncio.IncompleteReadError:
        raise TalkbackError("closed", "The camera closed the connection.")
    except asyncio.TimeoutError:
        raise TalkbackError("timeout", "The camera did not answer in time.")
    return parse_headers(raw.decode("utf-8", "replace"))


async def attempt(host: str, port: int, username: str = "", password: str = "",
                  timeout: float = 8.0) -> tuple[str, str]:
    """ONE authentication attempt on a FRESH socket -> (status line, challenge).

    With no credential it just reads the challenge; with one it reports what the
    camera made of it. This exists for `tools.talkback diagnose`, because
    `unauthorized` is the one failure whose cause is invisible from outside: the
    difference between a wrong password, a firmware that wants the OTHER hash,
    and a firmware using the published fixed account is only ever visible by
    reading the challenge and trying each."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=_MAX_PART_BYTES), timeout)
    except asyncio.TimeoutError:
        raise TalkbackError("unreachable", f"No answer from {host}:{port}.")
    except OSError as exc:
        raise TalkbackError(
            "unreachable", f"Cannot reach {host}:{port} ({exc.strerror or exc}).")
    try:
        writer.write(request_bytes(host, port))
        await writer.drain()
        status, headers = await _head(reader, timeout)
        challenge = headers.get("www-authenticate", "")
        length = int(headers.get("content-length", "0") or 0)
        if length:
            await asyncio.wait_for(reader.readexactly(length), timeout)
        if not username or not challenge.startswith("Digest"):
            return status.strip(), challenge

        writer.write(request_bytes(
            host, port, digest_header(challenge, username, password)))
        await writer.drain()
        status, _ = await _head(reader, timeout)
        return status.strip(), challenge
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2.0)
        except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
            pass


class TapoTalkSession:
    """One open speaker session on one camera.

    Not reusable: `close()` is final. The caller (talkback.sessions) guarantees there
    is at most one of these per camera at a time — the camera itself will accept
    a second one and then mix two people into the room."""

    def __init__(self, host: str, credential, port: int = DEFAULT_PORT,
                 timeout: float = 8.0, mode: str = "aec") -> None:
        self.host = host
        self.port = port
        self.credential = credential
        self.timeout = timeout
        self.mode = mode

        self.session_id = ""
        self.challenge = ""
        self.bytes_sent = 0
        self.opened_at = 0.0
        # Health, reported all the way up to the carer's screen.
        self.frames_sent = 0
        self.frames_dropped = 0
        self.queued_ms = 0.0
        self.peak_queued_ms = 0.0

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._muxer = AudioMuxer()
        self._drain_task: asyncio.Task | None = None
        self._closed = False
        # Set the moment the session is over for ANY reason — we closed it, or
        # the camera hung up (idle timeout, reboot, a second talker). Whoever
        # keeps this session open waits on it to know when to reconnect.
        self.gone = asyncio.Event()

    # -- wire helpers ------------------------------------------------------ #

    async def _readline_block(self) -> str:
        """Read a CRLFCRLF-terminated header block."""
        try:
            raw = await asyncio.wait_for(
                self._reader.readuntil(b"\r\n\r\n"), self.timeout)
        except asyncio.IncompleteReadError:
            raise TalkbackError("closed", "The camera closed the connection.")
        except asyncio.LimitOverrunError:
            raise TalkbackError("protocol", "The camera sent an oversized header block.")
        except asyncio.TimeoutError:
            raise TalkbackError("timeout", "The camera did not answer in time.")
        if len(raw) > _MAX_HEADER_BYTES:
            raise TalkbackError("protocol", "The camera sent an oversized header block.")
        return raw.decode("utf-8", "replace")

    _parse_headers = staticmethod(parse_headers)

    async def _read_body(self, headers: dict[str, str]) -> bytes:
        length = int(headers.get("content-length", "0") or 0)
        if length <= 0:
            return b""
        if length > _MAX_PART_BYTES:
            raise TalkbackError("protocol", "The camera announced an oversized part.")
        try:
            return await asyncio.wait_for(self._reader.readexactly(length), self.timeout)
        except asyncio.IncompleteReadError:
            raise TalkbackError("closed", "The camera closed the connection mid-part.")
        except asyncio.TimeoutError:
            raise TalkbackError("timeout", "The camera stopped sending mid-part.")

    def _request_bytes(self, authorization: str = "") -> bytes:
        return request_bytes(self.host, self.port, authorization)

    async def _send_part(self, content_type: str, body: bytes) -> None:
        head = [b"--" + CLIENT_BOUNDARY, f"Content-Type: {content_type}".encode()]
        if content_type != "application/json":
            head.append(b"X-If-Encrypt: 0")            # speaker audio is plaintext
            if self.session_id:
                head.append(f"X-Session-Id: {self.session_id}".encode())
        head.append(f"Content-Length: {len(body)}".encode())
        self._writer.write(b"\r\n".join(head) + b"\r\n\r\n" + body + b"\r\n")
        await self._writer.drain()

    async def _read_device_part(self) -> tuple[dict[str, str], bytes]:
        try:
            await asyncio.wait_for(self._reader.readuntil(DEVICE_BOUNDARY), self.timeout)
        except asyncio.IncompleteReadError:
            raise TalkbackError("closed", "The camera closed the connection.")
        except asyncio.LimitOverrunError:
            raise TalkbackError("protocol", "The camera sent an oversized part.")
        except asyncio.TimeoutError:
            raise TalkbackError("timeout", "The camera stopped responding.")
        _, headers = self._parse_headers(await self._readline_block())
        return headers, await self._read_body(headers)

    # -- lifecycle --------------------------------------------------------- #

    async def open(self) -> None:
        """Connect, authenticate, and open the talk session. On return the
        speaker is live and `send()` may be called."""
        t0 = time.monotonic()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                # A generous read limit: once the session is up some firmwares
                # push their own mic stream at us in large parts, and the default
                # 64 kB limit would turn that into a spurious protocol error.
                asyncio.open_connection(self.host, self.port, limit=_MAX_PART_BYTES),
                self.timeout)
            # Nagle's algorithm exists to spare the network from small writes.
            # This IS a stream of small writes — one 20 ms frame at a time — and
            # coalescing them means holding a carer's voice back waiting for an
            # ACK that has nothing to do with the room. Off, always.
            sock = self._writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    # A platform that will not take the option is not a reason
                    # to refuse the call; it is a reason to say so once.
                    logger.debug("TCP_NODELAY refused on %s", self.host)
        except asyncio.TimeoutError:
            raise TalkbackError("unreachable",
                                f"No answer from {self.host}:{self.port}. The camera is "
                                f"offline, or this model/firmware does not expose the "
                                f"talk port.")
        except OSError as exc:
            raise TalkbackError(
                "unreachable",
                f"Cannot reach {self.host}:{self.port} ({exc.strerror or exc}).")

        self._writer.write(self._request_bytes())
        await self._writer.drain()
        status_line, headers = self._parse_headers(await self._readline_block())
        await self._read_body(headers)
        challenge = headers.get("www-authenticate", "")
        if "401" not in status_line or not challenge.startswith("Digest"):
            raise TalkbackError("protocol",
                                f"Port {self.port} did not answer with a Tapo Digest "
                                f"challenge ({status_line.strip()}).")
        self.challenge = challenge

        username, password = self.credential.password_for(challenge)
        self._writer.write(self._request_bytes(
            digest_header(challenge, username, password)))
        await self._writer.drain()
        status_line, headers = self._parse_headers(await self._readline_block())
        await self._read_body(headers)
        if " 200" not in status_line:
            raise TalkbackError("unauthorized", refused_detail())

        await self._open_talk()
        self.opened_at = time.monotonic()
        logger.info("talk session %s open on %s in %.0f ms",
                    self.session_id, self.host, (self.opened_at - t0) * 1000)

    async def _open_talk(self) -> None:
        await self._send_part(
            "application/json",
            ('{"params":{"talk":{"mode":"%s"},"method":"get"},"seq":3,"type":"request"}'
             % self.mode).encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            headers, body = await self._read_device_part()
            if "json" not in headers.get("content-type", ""):
                continue
            text = body.decode("utf-8", "replace")
            sid = (_between(text, '"session_id":"', '"')
                   or _between(text, '"session_id": "', '"')
                   or _between(text, '"session_id":', ",").strip().strip('}"'))
            if sid:
                self.session_id = sid
                # Header-carried session ids are used from here on; the drain
                # task keeps the camera's own chatter from filling the socket.
                self._drain_task = asyncio.create_task(self._drain())
                return
            logger.debug("talk handshake, unexpected part: %s", text.strip())
            if '"error_code"' in text and '"error_code":0' not in text.replace(" ", ""):
                raise TalkbackError(
                    "refused",
                    "The camera refused a speaker session. Either its microphone/"
                    "speaker is switched off in the Tapo app, or someone is already "
                    "talking to it from the app.")
        raise TalkbackError("timeout", "The camera never granted a speaker session.")

    async def _drain(self) -> None:
        """Swallow whatever the camera sends us (notifications, its own mic
        stream if the firmware volunteers it). We want none of it — but an unread
        socket fills its buffer and the camera then stops reading OURS, which
        shows up as audio that plays for a few seconds and dies.

        An EMPTY read is the camera hanging up, and it must end this loop. At end
        of stream `read()` returns b"" at once, forever, without ever yielding to
        the event loop — so a loop that only checked `_closed` spun at ~1.7 M
        reads a second and starved every other task in the process: the whole
        edge API froze, and nothing could run to set `_closed` and stop it
        (reproduced 2026-09-22 before this fix)."""
        try:
            while not self._closed:
                if not await self._reader.read(65536):
                    break
        except (asyncio.CancelledError, OSError, TalkbackError):
            pass
        except Exception:
            logger.debug("talk drain ended", exc_info=True)
        finally:
            self.gone.set()

    @property
    def alive(self) -> bool:
        """Open, and the camera has not hung up."""
        return bool(self.session_id) and not self._closed and not self.gone.is_set()

    async def start_audio(self) -> None:
        """Send the PAT/PMT. Separate from open() so a caller can verify the
        session without making a sound (see hub.probe)."""
        await self._send_part("audio/mp2t", self._muxer.header())

    def _queued_ms(self, per_frame: int) -> float:
        """How much speech is already waiting in the socket, in milliseconds."""
        transport = self._writer.transport if self._writer else None
        if transport is None or per_frame <= 0:
            return 0.0
        try:
            return transport.get_write_buffer_size() * FRAME_MS / per_frame
        except Exception:
            # Not every transport exposes its buffer. Reporting "empty" is the
            # safe answer: it costs us the drop policy, never correctness.
            return 0.0

    async def send(self, alaw: bytes) -> None:
        """Push one chunk of 8 kHz mono A-law at the speaker.

        Deliberately does NOT await drain(). A drain per frame makes the handler
        wait on the camera's flow control every 20 ms, which turns one slow
        camera into a stalled carer — and the 8 s ceiling it needed meant a dead
        camera could park a live microphone for eight seconds before admitting
        it. The write buffer is bounded here instead, in milliseconds of speech,
        so backpressure is measured rather than waited on."""
        if self._closed or self.gone.is_set():
            raise TalkbackError("closed", "The speaker session is closed.")
        payload = self._muxer.frame(alaw)
        part = (
            b"\r\n".join([
                b"--" + CLIENT_BOUNDARY,
                b"Content-Type: audio/mp2t",
                b"X-If-Encrypt: 0",
                f"X-Session-Id: {self.session_id}".encode(),
                f"Content-Length: {len(payload)}".encode(),
            ]) + b"\r\n\r\n" + payload + b"\r\n")

        queued = self._queued_ms(len(part))
        self.queued_ms = queued
        self.peak_queued_ms = max(self.peak_queued_ms, queued)

        if queued >= _QUEUE_DEAD_MS:
            raise TalkbackError("stalled", "The camera stopped accepting audio.")
        if queued >= _QUEUE_STALE_MS:
            # Everything queued ahead of this frame is already late. Adding to it
            # would put the carer further behind with every word they say.
            self.frames_dropped += 1
            return

        try:
            self._writer.write(part)
        except OSError as exc:
            raise TalkbackError("closed", f"The camera dropped the connection ({exc}).")
        self.bytes_sent += len(alaw)
        self.frames_sent += 1

    def health(self) -> dict:
        """What this session is costing, for the carer's screen and the fleet."""
        return {
            "frames_sent": self.frames_sent,
            "frames_dropped": self.frames_dropped,
            "bytes_sent": self.bytes_sent,
            "queued_ms": round(self.queued_ms),
            "peak_queued_ms": round(self.peak_queued_ms),
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.gone.set()
        if self._drain_task:
            self._drain_task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), 2.0)
            except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
                pass
        logger.info("talk session %s on %s closed after %.1fs, %d bytes",
                    self.session_id or "-", self.host,
                    time.monotonic() - self.opened_at if self.opened_at else 0.0,
                    self.bytes_sent)
