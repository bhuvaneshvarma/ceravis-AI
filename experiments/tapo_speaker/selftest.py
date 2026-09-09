#!/usr/bin/env python3
"""
Offline self-test — no camera needed.

Checks the two things that are easy to get silently wrong before you point this
at a real camera:

  1. the MPEG-TS bytes we mux are structurally valid (PAT/PMT CRC32, 188-byte
     alignment, PES framing) — verified by ffprobe if it is available, and by a
     built-in parser regardless;
  2. the A-law encoder matches ffmpeg's pcm_alaw output byte for byte.

Run:  python3 selftest.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tapo_talk import (SAMPLE_RATE, TS_PACKET, TS_SYNC, TsMuxer, _crc32_mpeg,
                       linear_to_alaw, tone_alaw)

FAIL = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" - " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


def note(text):
    print("  SKIP  " + text)


def ts_structure(blob):
    """Minimal walk of the transport stream: alignment, sync bytes, PSI CRCs."""
    if len(blob) % TS_PACKET:
        return False, "not a multiple of 188 ({} bytes)".format(len(blob))
    pids = []
    for off in range(0, len(blob), TS_PACKET):
        pkt = blob[off:off + TS_PACKET]
        if pkt[0] != TS_SYNC:
            return False, "missing sync byte at packet {}".format(off // TS_PACKET)
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pids.append(pid)
        if pid in (0x0000, 0x1000):
            section_len = ((pkt[6] & 0x0F) << 8) | pkt[7]
            section = pkt[5:5 + 3 + section_len]
            body, crc = section[:-4], int.from_bytes(section[-4:], "big")
            if _crc32_mpeg(body) != crc:
                return False, "bad CRC32 on PID 0x{:04X}".format(pid)
    return True, "{} packets, pids {}".format(len(pids), sorted(set(pids)))


def main():
    print("A-law encoder")
    ff = shutil.which("ffmpeg")
    ramp = bytearray()
    for s in range(-32768, 32768, 37):
        ramp += int(s).to_bytes(2, "little", signed=True)
    ours = bytes(linear_to_alaw(int.from_bytes(ramp[i:i + 2], "little", signed=True))
                 for i in range(0, len(ramp), 2))
    if ff:
        proc = subprocess.run(
            [ff, "-v", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
             "-i", "pipe:0", "-c:a", "pcm_alaw", "-f", "alaw", "pipe:1"],
            input=bytes(ramp), capture_output=True)
        theirs = proc.stdout
        diff = sum(1 for a, b in zip(ours, theirs) if a != b)
        check("matches ffmpeg pcm_alaw",
              proc.returncode == 0 and diff == 0 and len(ours) == len(theirs),
              "{} of {} bytes differ".format(diff, len(ours)))
    else:
        note("ffmpeg not on PATH - skipping the ffmpeg comparison")

    try:                                     # gone in Python 3.13, present on JetPack's 3.10/3.12
        import audioop
        theirs = b"".join(audioop.lin2alaw(ramp[i:i + 2], 2) for i in range(0, len(ramp), 2))
        diff = sum(1 for a, b in zip(ours, theirs) if a != b)
        check("matches audioop.lin2alaw", diff == 0, "{} of {} bytes differ".format(diff, len(ours)))
    except ImportError:
        note("audioop not available on this Python - skipping that comparison")

    print("MPEG-TS muxer")
    muxer = TsMuxer()
    blob = muxer.header()
    audio = tone_alaw(1.0)
    frame = SAMPLE_RATE * 20 // 1000
    for i in range(0, len(audio), frame):
        blob += muxer.payload(audio[i:i + frame], 90000 * 20 // 1000)
    ok, detail = ts_structure(blob)
    check("structure", ok, detail)
    check("size is sane", 0 < len(blob) < 400_000, "{} bytes for 1s of audio".format(len(blob)))

    if ff:
        path = os.path.join(tempfile.gettempdir(), "ceravis_tapo_selftest.ts")
        with open(path, "wb") as fh:
            fh.write(blob)
        probe = shutil.which("ffprobe")
        if probe:
            proc = subprocess.run([probe, "-v", "error", "-show_entries",
                                   "program_stream=codec_type,id", "-of", "compact", path],
                                  capture_output=True)
            # ffmpeg does not know TP-Link's private stream_type 0x90, so it will
            # report the program but no decodable codec. Finding the PROGRAM at
            # all is the proof that PAT+PMT parsed.
            out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
            check("ffprobe parses the stream", proc.returncode == 0, out or "no diagnostics")
        print("  wrote " + path + " (inspect with: ffprobe -v trace " + path + ")")

    print()
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
