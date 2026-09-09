from __future__ import annotations

"""
The two byte-level pieces the camera speaker needs: G.711 A-law encoding and the
MPEG-TS wrapper TP-Link expects around it.

Neither is negotiable — the Tapo talk endpoint accepts exactly ONE thing: an
MPEG-TS stream carrying a single elementary stream with TP-Link's private
`stream_type 0x90`, whose payload is 8 kHz mono G.711 A-law. (0x91 = PCMU/16000
turns up on some newer firmware, but on the RECEIVE side; the speaker takes
A-law.) Anything else is silently dropped by the camera — no error, no sound.

Pure standard library on purpose. FFmpeg cannot produce this: its MPEG-TS muxer
has no mapping for pcm_alaw, let alone TP-Link's private stream type. Both
functions here are verified byte-for-byte in tests/test_talkback.py.
"""

import struct

TS_PACKET = 188
TS_SYNC = 0x47
PAT_PID = 0x0000
PMT_PID = 0x1000
PES_PID = 0x0100
STREAM_TYPE_PCMA_TAPO = 0x90        # TP-Link private: G.711 A-law @ 8 kHz mono
STREAM_ID_AUDIO = 0xC0

SAMPLE_RATE = 8000                  # G.711 is 8 kHz mono, full stop
FRAME_MS = 20                       # one 160-byte A-law frame
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000
PTS_HZ = 90000                      # MPEG-TS clock


# ---------------------------------------------------------------------------
# G.711 A-law
# ---------------------------------------------------------------------------

_SEG_ENDS = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def linear_to_alaw(sample: int) -> int:
    """One 16-bit signed PCM sample -> one A-law byte (ITU-T G.711).

    Written out rather than taken from `audioop`, which is deprecated and
    removed in Python 3.13 — this module must not carry an expiry date."""
    sample >>= 3                                    # 16-bit -> 13-bit
    if sample >= 0:
        mask = 0xD5                                 # sign bit 1 = positive
    else:
        mask = 0x55
        sample = -sample - 1
    seg = 8
    for i, end in enumerate(_SEG_ENDS):
        if sample <= end:
            seg = i
            break
    if seg >= 8:                                    # clipped
        return 0x7F ^ mask
    val = (sample >> 1) & 0x0F if seg < 2 else (sample >> seg) & 0x0F
    return ((seg << 4) | val) ^ mask


_ALAW_TABLE = bytes(linear_to_alaw(s if s < 32768 else s - 65536) for s in range(65536))


def pcm16_to_alaw(pcm: bytes) -> bytes:
    """Little-endian 16-bit mono PCM -> A-law, through a precomputed table.

    The table costs 64 kB once at import and turns the hot path into a memory
    read, which is what keeps a live microphone free on the Orin's CPU."""
    if len(pcm) & 1:
        pcm = pcm[:-1]
    return bytes(_ALAW_TABLE[pcm[i] | (pcm[i + 1] << 8)] for i in range(0, len(pcm), 2))


# ---------------------------------------------------------------------------
# MPEG-TS
# ---------------------------------------------------------------------------

def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 \
                else (crc << 1) & 0xFFFFFFFF
    return crc


def _psi_packet(pid: int, table: bytes) -> bytes:
    """One 188-byte TS packet carrying a complete PSI section."""
    head = bytes([TS_SYNC, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    section = table + struct.pack(">I", _crc32_mpeg(table))
    return (head + b"\x00" + section).ljust(TS_PACKET, b"\x00")


def _psi_header(table_id: int, content_len: int) -> bytes:
    section_len = 5 + content_len + 4              # 5 below + content + CRC32
    return bytes([
        table_id,
        0xB0 | ((section_len >> 8) & 0x0F), section_len & 0xFF,
        0x00, 0x01,                                # table id extension
        0xC1,                                      # version 0, current
        0x00, 0x00,                                # section 0 of 0
    ])


def _pat() -> bytes:
    content = struct.pack(">HH", 1, 0xE000 | PMT_PID)
    return _psi_packet(PAT_PID, _psi_header(0x00, len(content)) + content)


def _pmt() -> bytes:
    content = struct.pack(">HH", 0xE000 | 0x1FFF, 0xF000)      # no PCR, no program info
    content += bytes([STREAM_TYPE_PCMA_TAPO]) + struct.pack(">HH", 0xE000 | PES_PID, 0xF000)
    return _psi_packet(PMT_PID, _psi_header(0x02, len(content)) + content)


def _pts_bytes(pts: int) -> bytes:
    return bytes([
        0x20 | ((pts >> 29) & 0x0E) | 1,
        (pts >> 22) & 0xFF,
        ((pts >> 14) & 0xFE) | 1,
        (pts >> 7) & 0xFF,
        ((pts << 1) & 0xFE) | 1,
    ])


class AudioMuxer:
    """Wraps A-law frames into a transport stream. One instance per talk session
    — it carries the continuity counter and the PTS clock, both of which must
    advance monotonically for the whole session or the camera drops audio."""

    def __init__(self) -> None:
        self._counter = 0
        self._pts = 0

    @staticmethod
    def header() -> bytes:
        """PAT + PMT. Sent once, before any audio."""
        return _pat() + _pmt()

    def frame(self, alaw: bytes) -> bytes:
        """One A-law frame -> the TS packets that carry it."""
        size = 3 + 5 + len(alaw)
        pes = (b"\x00\x00\x01" + bytes([STREAM_ID_AUDIO])
               + struct.pack(">H", size if size <= 0xFFFF else 0)
               + bytes([0x80, 0x80, 5]) + _pts_bytes(self._pts) + alaw)
        self._pts = (self._pts + PTS_HZ * len(alaw) // SAMPLE_RATE) & 0xFFFFFFFF

        out = bytearray()
        first = True
        while pes:
            pusi = 0x40 if first else 0x00
            first = False
            head = bytes([TS_SYNC, pusi | (PES_PID >> 8), PES_PID & 0xFF])
            if len(pes) < TS_PACKET - 4:
                stuff = TS_PACKET - 5 - len(pes)
                out += (head + bytes([0x30 | (self._counter & 0x0F), stuff])
                        + bytes(stuff) + pes)
                pes = b""
            else:
                out += head + bytes([0x10 | (self._counter & 0x0F)]) + pes[:TS_PACKET - 4]
                pes = pes[TS_PACKET - 4:]
            self._counter += 1
        return bytes(out)
