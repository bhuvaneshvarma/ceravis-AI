from __future__ import annotations

"""
ceravis-talkback — commission and prove a camera's speaker from the device shell.

The browser path (live wall -> hold Talk) is the product. This is the engineer's
path, over SSH, with no browser, no HTTPS and no microphone.

    python -m tools.talkback list                          # what is commissioned
    python -m tools.talkback set                           # THE home password
    python -m tools.talkback set    --camera KITCHEN       # an override for one camera
    python -m tools.talkback test   [--camera KITCHEN]     # silent proof
    python -m tools.talkback lines                         # the line record
    python -m tools.talkback diagnose --camera KITCHEN --try-password
                                                       # why auth failed
    python -m tools.talkback tone   --camera KITCHEN       # a beep in the room
    python -m tools.talkback play   --camera KITCHEN --file hello.wav
    python -m tools.talkback forget --camera KITCHEN

The running service holds every camera's talk line (talkback.lines), and a camera
has ONE speaker — so `test`, `lines`, `tone` and `play` go THROUGH the service
(its API and its /session socket, exactly like a carer's page) instead of opening
a second talk session beside it. `set`, `forget`, `list` and `diagnose` work on
the device directly.

Run from edge/. `set` prompts for the password without echoing it; pass
--password only in a script, where it lands in your shell history.

Exit code: 0 success, 1 a named failure (unreachable / wrong password / busy),
2 a usage problem — so it drops into a commissioning script unchanged.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import math
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urlparse

from config.settings import settings
from configuration.account_config import effective_edge_id
from configuration.camera_config import CameraConfig
from talkback import credentials, guard
from talkback.advice import refused_lines
from talkback.lines import camera_host
from talkback.mpegts import FRAME_BYTES, SAMPLE_RATE, linear_to_alaw
from talkback.protocol import TalkbackError, attempt
from talkback.sessions import hub

SERVICE = "http://127.0.0.1:8000"         # the ceravis service (systemd: port 8000)


def _api(method: str, path: str, body: dict | None = None, timeout: float = 45.0) -> dict:
    """One call to the running service's talk-back API, authenticated like any
    other caller. Raises SystemExit with a readable sentence on failure."""
    edge = effective_edge_id()
    url = f"{SERVICE}/api/v1/talkback{path}"
    data = None
    if method == "GET":
        url += ("&" if "?" in url else "?") + "edge_id=" + quote(edge)
    else:
        data = json.dumps({**(body or {}), "edgeId": edge}).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail") or {}
        except ValueError:
            detail = {}
        if isinstance(detail, dict):
            raise TalkbackError(detail.get("code") or str(exc.code),
                                detail.get("message") or str(exc))
        raise TalkbackError(str(exc.code), str(detail))
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"the ceravis service is not answering at {SERVICE} ({exc}). "
                         f"Is it running?  sudo systemctl status ceravis")


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
    """Play `audio` through the service exactly as a carer's page does: connect
    to /session, claim the floor, stream 20 ms frames at real time (a camera fed
    faster than real time drops most of it), release."""
    try:
        import websockets
    except ImportError:
        raise SystemExit("the `websockets` package is missing (edge/requirements.txt)")
    ws_url = (SERVICE.replace("http", "ws", 1) + "/api/v1/talkback/session?edge_id="
              + quote(effective_edge_id()) + "&client_id=cli-" + uuid.uuid4().hex[:8]
              + "&name=" + quote("Technician (CLI)"))
    async with websockets.connect(ws_url) as ws:
        welcome = json.loads(await ws.recv())
        if welcome.get("type") != "welcome":
            raise TalkbackError(welcome.get("code", "refused"), welcome.get("message", ""))
        await ws.send(json.dumps({"type": "claim", "camera": camera_id}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if reply.get("type") == "granted":
                break
            if reply.get("type") == "refused":
                raise TalkbackError(reply.get("code", "busy"), reply.get("message", ""))
        loop = asyncio.get_running_loop()
        start = loop.time()
        for i in range(0, len(audio), FRAME_BYTES):
            await ws.send(audio[i:i + FRAME_BYTES])
            ahead = start + (i + FRAME_BYTES) / SAMPLE_RATE - loop.time()
            if ahead > 0:
                await asyncio.sleep(ahead)
        await asyncio.sleep(0.6)          # let the camera drain its buffer
        await ws.send(json.dumps({"type": "release"}))
        await ws.send(json.dumps({"type": "stop"}))
    print(f"sent {len(audio) / SAMPLE_RATE:.2f}s of audio")
    return 0


def _check_all(force: bool = False) -> int:
    """Ask the service to look at every camera now, and print one line each.
    Exit 0 only when every camera is ready, so a commissioning script can gate
    on it."""
    if force:
        guard.reset()
    res = _api("POST", "/check")
    cams = res.get("cameras") or {}
    names = {r["camera_id"]: r["camera_name"] for r in hub.cameras()}
    print()
    for cid, v in cams.items():
        state = v.get("state", "?")
        extra = (f"{v.get('elapsed_ms')} ms to open" if state == "ready"
                 else v.get("detail") or v.get("message") or "")
        print(f"  {state:<15} {names.get(cid, cid):<20} {extra}")
    print(f"\n{res.get('ready', 0)} of {res.get('total', 0)} cameras ready.")
    if any(v.get("state") == "rejected" for v in cams.values()):
        print("\n  A camera refused the password. Check, in this order:")
        print(refused_lines())
    return 0 if res.get("all_ready") else 1


def _print_lines() -> int:
    """The line record: how long each camera kept its talk line, and why each
    connection ended — the measured answer to "how long does a camera hold it"."""
    h = _api("GET", "/health")
    lines = h.get("lines") or {}
    if not lines:
        print("no lines yet (the service opens them ~10 s after it starts)")
        return 1
    st = h.get("settings") or {}
    print(f"lines kept open: {st.get('line_always')}   keep-alive: "
          f"{st.get('line_keepalive_secs') or 'off'}   floor hold: {st.get('floor_hold_secs')} s\n")
    for cid, ln in lines.items():
        now = (f"open {ln.get('open_secs')} s" if ln.get("line") == "open"
               else "closed")
        print(f"{cid:<16} {ln.get('state'):<14} {now:<18} opened {ln.get('opens')}x, "
              f"dropped by camera {ln.get('drops')}x")
        for ev in ln.get("history") or []:
            rest = ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("at", "event"))
            print(f"    {ev.get('at', '')[11:19]}  {ev.get('event'):<8} {rest}")
    floors = h.get("floors") or {}
    held = ", ".join(k + "=" + str(v.get("state")) for k, v in floors.items())
    print(f"\npages connected: {h.get('clients', 0)}" + (f"   floors: {held}" if held else ""))
    return 0


def _candidates(cam, cred, cloud_password: str, email: str) -> list:
    """Every credential the camera could plausibly mean, from what this device
    already knows. Each is (label, username, secret).

    This exists because the handshake is PROVEN correct — it is byte-for-byte
    what pytapo and go2rtc send — so a 401 can only be the secret value, and
    which secret a Tapo firmware wants has changed more than once. Enumerating
    is cheap: one local TCP round-trip each, a few milliseconds on the LAN.

    Nothing here is a secret we did not already hold: the stored hashes, the
    camera's own stream credentials out of cameras.json, and whatever the
    operator typed at the prompt."""
    def hashes(value: str) -> tuple[str, str]:
        raw = value.encode()
        return (hashlib.md5(raw).hexdigest().upper(),
                hashlib.sha256(raw).hexdigest().upper())

    out = [
        ("account password, SHA256   (admin)", "admin", cred.sha256),
        ("account password, MD5      (admin)", "admin", cred.md5),
    ]

    if cloud_password:
        md5_up, sha_up = hashes(cloud_password)
        out += [
            ("account password, SHA256 lower-case", "admin", sha_up.lower()),
            ("account password, MD5 lower-case", "admin", md5_up.lower()),
            ("account password, sent as typed", "admin", cloud_password),
        ]
        if email:
            out += [
                (f"account password SHA256, username {email}", email, sha_up),
                (f"account password as typed, username {email}", email, cloud_password),
            ]

    # The camera's OWN stream account (the Tapo app's "Camera Account", what we
    # already use for RTSP/ONVIF). Newer firmware moved some local surfaces onto
    # it, and if it works here the whole cloud-password step disappears from
    # commissioning — worth knowing either way.
    user = (cam.onvif_username or "").strip()
    pw = (cam.onvif_password or "").strip()
    if not (user and pw):
        parsed = urlparse(cam.rtsp_url or "")
        user, pw = (parsed.username or ""), (parsed.password or "")
    if user and pw:
        md5_up, sha_up = hashes(pw)
        out += [
            ("camera stream password SHA256 (admin)", "admin", sha_up),
            ("camera stream password MD5    (admin)", "admin", md5_up),
            (f"camera stream password SHA256 ({user})", user, sha_up),
            (f"camera stream password as typed ({user})", user, pw),
        ]

    out.append(("fixed account (CVE-2022-37255 firmware)", "none", "TPL075526460603"))
    return out


async def _diagnose(camera_id: str, cam, host: str, cloud_password: str,
                    email: str, force: bool = False) -> int:
    """Answer the ONE question `test` cannot: the camera rejected us — why?

    Reads the challenge the camera actually sent, then tries every credential
    shape it could have meant. Prints labels only: no secret is ever echoed."""
    port = settings.talkback_port
    timeout = settings.talkback_timeout_secs
    status, challenge = await attempt(host, port, timeout=timeout)
    print(f"  camera        {camera_id} at {host}:{port}")
    print(f"  first answer  {status}")
    print(f"  challenge     {challenge or '(none - this is not a Tapo talk port)'}")
    if not challenge.startswith("Digest"):
        print("\n  Port 8800 answered but not with a Tapo Digest challenge. This "
              "model/firmware\n  does not expose the talk endpoint.")
        return 1

    wants = "SHA256" if 'encrypt_type="3"' in challenge else "MD5"
    print(f"  asks for      {wants} of the password"
          + (" (fixed-account firmware)" if 'username="none"' in challenge else ""))

    cred = credentials.get(camera_id)
    if cred is None:
        print("\n  No credential stored yet - run `set` first.")
        return 1

    # Every shape below is a real login attempt on the camera. On 2026-09-21 two
    # diagnose runs were ~24 of the ~25 refused logins a camera took in one
    # afternoon — the pattern cameras lock accounts out for. So a paused camera
    # is not diagnosed without --force, and every refusal here is counted.
    if not force:
        guard.check(camera_id, cred, getattr(cam, "camera_name", camera_id))
    trials = _candidates(cam, cred, cloud_password, email)
    print(f"\n  Trying {len(trials)} credential shapes, one connection each "
          f"(each is a real login attempt - run this once, not repeatedly):")
    accepted = []
    for label, user, secret in trials:
        try:
            result, _ = await attempt(host, port, user, secret, timeout=timeout)
        except TalkbackError as exc:
            result = f"failed ({exc.code})"
        ok = " 200" in result
        if ok:
            accepted.append(label)
        print(f"    {'ACCEPTED' if ok else 'rejected'}  {label:<44} {result}")

    print()
    if accepted:
        guard.accepted(camera_id)
        print(f"  ACCEPTED: {accepted[0]}")
        print("  Store exactly that password with `set` and `test` will pass.")
        return 0
    guard.refused(camera_id, cred, count=len(trials))

    print("  The camera accepted NOTHING derived from the password entered. The")
    print("  handshake is proven correct (byte-for-byte what pytapo and go2rtc")
    print("  send, and unchanged since it worked), so the camera is holding a")
    print("  secret other than this password. Check, in this order:")
    print(refused_lines())
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.talkback",
                                 description="Camera speaker (talk-back) commissioning.")
    ap.add_argument("command", choices=["list", "set", "forget", "test", "lines",
                                       "diagnose", "tone", "play"])
    ap.add_argument("--camera", help="camera label or id (KITCHEN, cam_1, …)")
    ap.add_argument("--password", help="TP-Link ACCOUNT password (prompted if omitted)")
    ap.add_argument("--file", help="audio file for `play`")
    ap.add_argument("--seconds", type=float, default=2.0, help="tone length")
    ap.add_argument("--freq", type=float, default=880.0)
    ap.add_argument("--volume", type=float, default=1.0)
    ap.add_argument("--try-password", action="store_true",
                    help="diagnose: prompt for a password to try "
                         "live (never stored)")
    ap.add_argument("--email", help="diagnose: also try the TP-Link "
                                    "account email as the username")
    ap.add_argument("--force", action="store_true",
                    help="test/diagnose: ignore the lock-out pause (a camera "
                         "that refused the same password again and again)")
    ap.add_argument("--service", default=SERVICE,
                    help=f"the ceravis service to go through (default {SERVICE})")
    args = ap.parse_args(argv)
    globals()["SERVICE"] = args.service.rstrip("/")

    if args.command == "lines":
        return _print_lines()

    if args.command == "list":
        rows = hub.cameras()
        if not rows:
            print("no cameras registered")
            return 0
        print(f"{'CAMERA':<22}{'HOST':<18}{'CREDENTIAL':<14}SET AT")
        for r in rows:
            scope = r.get("credential_scope") or ""
            print(f"{r['camera_name'][:21]:<22}{r['host'] or '-':<18}"
                  f"{(scope + ' password') if r['configured'] else 'not set':<14}"
                  f"{r['credential_updated_at'] or '-'}")
        print(f"\nhome TP-Link password: "
              f"{'set' if credentials.home_configured() else 'NOT SET'}"
              f"  (set it once with: python -m tools.talkback set)")
        print(f"\ntalk-back is {'ENABLED' if settings.talkback_enabled else 'DISABLED'} "
              f"on this device (TALKBACK_ENABLED), port {settings.talkback_port}")
        return 0

    # No --camera: the HOME. One TP-Link account password for every camera,
    # then a silent check of each so the technician leaves knowing the answer.
    if args.command in ("set", "test") and not args.camera:
        if args.command == "set":
            password = args.password or getpass.getpass(
                "This home's TP-Link ACCOUNT password (the Tapo app login): ")
            try:
                cleared = credentials.set_home_password(password)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print("stored (hashes only) for the whole home"
                  + (f"; replaced per-camera entries for {', '.join(cleared)}"
                     if cleared else ""))
        try:
            return _check_all(force=args.force)
        except TalkbackError as exc:
            print(f"error [{exc.code}]: {exc}", file=sys.stderr)
            return 1

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
        if args.command == "diagnose":
            typed = ""
            if args.try_password:
                typed = getpass.getpass(
                    "Account password to try (not stored): ")
            return asyncio.run(_diagnose(camera_id, cam, camera_host(cam),
                                         typed, args.email or "", force=args.force))
        if args.command == "test":
            result = _api("POST", f"/{quote(camera_id)}/test", {"force": args.force})
            print(f"OK — {camera_id}'s talk line is open ({result.get('host')}, opened in "
                  f"{result.get('connect_ms')} ms, open {result.get('open_secs')} s, "
                  f"auth {result.get('auth')}). No audio sent.")
            return 0
        if args.command == "play" and not args.file:
            print("error: play needs --file", file=sys.stderr)
            return 2
        audio = _tone(args.seconds, args.freq) if args.command == "tone" \
            else _file_audio(args.file, args.volume)
        return asyncio.run(_speak(camera_id, audio))
    except TalkbackError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        if exc.code == "unauthorized":
            print("\n  Check, in this order:\n" + refused_lines(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
