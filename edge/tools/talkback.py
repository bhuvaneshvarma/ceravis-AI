from __future__ import annotations

"""
ceravis-talkback — commission and prove a camera's speaker from the device shell.

The browser path (live wall -> hold Talk) is the product. This is the engineer's
path: it runs the SAME talkback package, so anything it proves is true of the
real thing, and it works over SSH with no browser, no HTTPS and no microphone.

    python -m tools.talkback list                          # what is commissioned
    python -m tools.talkback set    --camera KITCHEN       # store the password
    python -m tools.talkback test   --camera KITCHEN       # silent proof
    python -m tools.talkback tone   --camera KITCHEN       # a beep in the room
    python -m tools.talkback play   --camera KITCHEN --file hello.wav
    python -m tools.talkback forget --camera KITCHEN

Run from edge/. `set` prompts for the password without echoing it; pass
--password only in a script, where it lands in your shell history.

Exit code: 0 success, 1 a named failure (unreachable / wrong password / busy),
2 a usage problem — so it drops into a commissioning script unchanged.
"""

import argparse
import asyncio
import getpass
import math
import shutil
import subprocess
import sys

from config.settings import settings
from configuration.camera_config import CameraConfig
from talkback import credentials
from talkback.sessions import camera_host, hub
from talkback.mpegts import FRAME_BYTES, SAMPLE_RATE, linear_to_alaw
from talkback.protocol import TalkbackError


def _resolve_id(label: str) -> str:
    """Accept KITCHEN, 'Kitchen Camera' or cam_1 — the same addressing the cloud
    control endpoints use (CameraConfig.get_by_label)."""
    cam = CameraConfig().get_by_id(label) or CameraConfig().get_by_label(label)
    if cam is None:
        raise SystemExit(f"no camera called '{label}' — try: python -m tools.talkback list")
    return cam.camera_id


def _tone(seconds: float, freq: float = 880.0, volume: float = 0.5) -> bytes:
    n = int(SAMPLE_RATE * seconds)
    out = bytearray(n)
    for i in range(n):
        env = min(1.0, i / 400.0, (n - i) / 400.0)      # fade so it does not click
        out[i] = linear_to_alaw(
            int(volume * env * 32000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)))
    return bytes(out)


def _file_audio(path: str, volume: float) -> bytes:
    """Any file the device's ffmpeg reads -> 8 kHz mono A-law. One short run,
    off the audio path; the same ffmpeg the recordings already use."""
    exe = shutil.which(settings.ffmpeg_binary)
    if exe is None:
        raise SystemExit(f"{settings.ffmpeg_binary} not found on this device")
    proc = subprocess.run(
        [exe, "-v", "error", "-i", path, "-vn", "-af", f"volume={volume}",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_alaw", "-f", "alaw", "-"],
        capture_output=True)
    if proc.returncode != 0:
        raise SystemExit("ffmpeg: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


async def _speak(camera_id: str, audio: bytes) -> int:
    """Open a session and play `audio` at real time — a camera fed faster than
    real time drops most of it."""
    session = await hub.open(camera_id, holder="cli")
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        for i in range(0, len(audio), FRAME_BYTES):
            await session.send(audio[i:i + FRAME_BYTES])
            ahead = start + (i + FRAME_BYTES) / SAMPLE_RATE - loop.time()
            if ahead > 0:
                await asyncio.sleep(ahead)
        await asyncio.sleep(0.6)          # let the camera drain its buffer
        print(f"sent {len(audio) / SAMPLE_RATE:.2f}s of audio")
        return 0
    finally:
        await hub.release(camera_id, session)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.talkback",
                                 description="Camera speaker (talk-back) commissioning.")
    ap.add_argument("command", choices=["list", "set", "forget", "test", "tone", "play"])
    ap.add_argument("--camera", help="camera label or id (KITCHEN, cam_1, …)")
    ap.add_argument("--password", help="TP-Link ACCOUNT password (prompted if omitted)")
    ap.add_argument("--file", help="audio file for `play`")
    ap.add_argument("--seconds", type=float, default=2.0, help="tone length")
    ap.add_argument("--freq", type=float, default=880.0)
    ap.add_argument("--volume", type=float, default=1.0)
    args = ap.parse_args(argv)

    if args.command == "list":
        rows = hub.cameras()
        if not rows:
            print("no cameras registered")
            return 0
        print(f"{'CAMERA':<22}{'HOST':<18}{'TALK-BACK':<12}CONFIGURED AT")
        for r in rows:
            print(f"{r['camera_name'][:21]:<22}{r['host'] or '-':<18}"
                  f"{'ready' if r['configured'] else 'not set':<12}"
                  f"{r['credential_updated_at'] or '-'}")
        print(f"\ntalk-back is {'ENABLED' if settings.talkback_enabled else 'DISABLED'} "
              f"on this device (TALKBACK_ENABLED), port {settings.talkback_port}")
        return 0

    if not args.camera:
        print("error: --camera is required", file=sys.stderr)
        return 2
    camera_id = _resolve_id(args.camera)

    if args.command == "set":
        password = args.password or getpass.getpass(
            "TP-Link ACCOUNT password (not the camera's stream password): ")
        try:
            credentials.set_password(camera_id, password)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"stored (hashes only) for {camera_id} — now run: "
              f"python -m tools.talkback test --camera {args.camera}")
        return 0

    if args.command == "forget":
        print("removed" if credentials.forget(camera_id) else "nothing stored")
        return 0

    cam = CameraConfig().get_by_id(camera_id)
    if cam is not None and not camera_host(cam):
        print(f"error: {camera_id} has no usable address on file", file=sys.stderr)
        return 1

    try:
        if args.command == "test":
            result = asyncio.run(hub.probe(camera_id))
            print(f"OK — {result['host']} granted speaker session {result['session_id']} "
                  f"in {result['elapsed_ms']} ms (auth: {result['auth']}). No audio sent.")
            return 0
        if args.command == "play" and not args.file:
            print("error: play needs --file", file=sys.stderr)
            return 2
        audio = _tone(args.seconds, args.freq) if args.command == "tone" \
            else _file_audio(args.file, args.volume)
        return asyncio.run(_speak(camera_id, audio))
    except TalkbackError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
