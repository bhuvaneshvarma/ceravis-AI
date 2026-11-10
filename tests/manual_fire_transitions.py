#!/usr/bin/env python3
"""
Manual end-to-end test — fire the posture TRANSITION events NOW, to the SAME
app server, with the SAME key, account and camera the running ceravis.service
uses:

    sitting_down        "Sat down in Lounge"
    standing_up         "Stood up in Lounge"
    walking_started     "Started walking in Lounge"
    walking_stopped     "Stopped walking in Lounge"
    no_transition_snapshot
                        "No transition for 1 h in Lounge · still sitting"

⚠ NOT an automated test: each one is a REAL saveSnapshot that shows up in the
app. That is why its name does not start with test_ — no test runner picks it
up. Run it only on purpose, on a bench device with a test account.

What one event is, exactly as production sends it (CloudAlertPublisher ->
OutboxSender -> integration.ceravis_api.save_snapshot): ONE saveSnapshot,
multipart, with
    patientId    = account.json ceravisUserId
    text         = alerts.alert_format.describe(event)     (the one wording)
    cameraNumber = room_to_enum(camera room)                (LOUNGE, KITCHEN …)
    category     = the event type in capitals               (SITTING_DOWN …)
    image        = a live still from that camera
    Idempotency-Key header = a fresh id per event (the outbox's job id)
No saveAlert: these are snapshot-only types (settings.cloud_snapshot_event_types).

WHY it reads the service's environment: a shell sees only
edge/infra/env/jetson.env, but the service also loads the files its unit names
(e.g. /etc/ceravis/server.env, which points the bench at app.ceravishealth.in
while jetson.env says pilot). An earlier version of this script sent from the
shell's view and hit the wrong server: every snapshot came back HTTP 400. This
one loads the unit's Environment= and EnvironmentFile= values first, and
refuses to send if its server differs from the one the service last called.

The key is what the service uses, never printed (only a short sha256 tag):
    1. $CERAVIS_API_KEY if you exported one;
    2. else the root-only credential /etc/ceravis/credstore/ceravis_api_key,
       read with sudo (it may ask for your password once);
    3. else — when that file does not exist, or the service's own last start
       logged that it runs on the built-in key — the built-in key, as the
       service does.

Run from the repo root, on the device:
    python3 tests/manual_fire_transitions.py --dry-run         # check, send nothing
    python3 tests/manual_fire_transitions.py                   # all five, first camera
    python3 tests/manual_fire_transitions.py LOUNGE            # a specific camera
    python3 tests/manual_fire_transitions.py --only sitting_down,walking_started
    python3 tests/manual_fire_transitions.py --posture standing --held-min 75

Exit status: 0 all sent, 1 some failed, 2 aborted before sending.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"
SERVICE = "ceravis.service"
CRED_KEY = "/etc/ceravis/credstore/ceravis_api_key"
FALLBACK_ENV_FILES = ("/etc/ceravis/server.env",)
TAG = "[fire-transitions]"

# The order a real day produces them in; the last one is the long-dwell burst.
EVENTS = ("sitting_down", "standing_up", "walking_started", "walking_stopped",
          "no_transition_snapshot")


def say(msg: str) -> None:
    print(f"{TAG} {msg}", flush=True)


def die(msg: str) -> None:
    say(f"ABORT: {msg}")
    raise SystemExit(2)


# ---- 1. the service's environment ---------------------------------------
def _parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines the way systemd's EnvironmentFile= reads them (comments,
    blank lines, optional quotes, an optional leading `export`)."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def _systemctl_show() -> tuple[list[str], list[tuple[Path, bool]]]:
    """(Environment= assignments, [(EnvironmentFile path, optional)]) of the
    unit, in systemd's order; empty when systemctl or the unit is missing."""
    try:
        out = subprocess.run(
            ["systemctl", "show", SERVICE, "-p", "Environment", "-p", "EnvironmentFiles"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return [], []
    assigns: list[str] = []
    files: list[tuple[Path, bool]] = []
    for line in out.splitlines():
        name, _, value = line.partition("=")
        if name == "Environment" and value:
            assigns += shlex.split(value)
        elif name == "EnvironmentFiles" and value:
            path, _, flags = value.partition(" ")
            files.append((Path(path), "ignore_errors=yes" in flags))
    return assigns, files


def load_service_env() -> str | None:
    """Put the unit's environment into this process before settings is
    imported, so pydantic sees what the service sees. systemd applies
    Environment= first and the files over it, in order; so does this.
    The API key is not put in the environment here: it is returned (a value
    the unit's files set wins over the credential, as in the service) and
    pick_key decides."""
    assigns, files = _systemctl_show()
    applied: list[str] = []
    if not files:
        files = [(Path(p), True) for p in FALLBACK_ENV_FILES]
        say(f"{SERVICE} not found via systemctl — using {', '.join(FALLBACK_ENV_FILES)}")
    env: dict[str, str] = {}
    for a in assigns:
        k, sep, v = a.partition("=")
        if sep:
            env[k] = v
    for path, optional in files:
        try:
            env.update(_parse_env_file(path))
            applied.append(str(path))
        except OSError as exc:
            if optional:
                continue
            die(f"the service reads {path} but this user cannot ({exc.strerror}); "
                "run as the service's user")
    unit_key = None
    for k, v in env.items():
        if k.upper() == "CERAVIS_API_KEY":
            unit_key = v.strip() or None
            continue
        if k.upper() == "CREDENTIALS_DIRECTORY":
            continue
        os.environ[k] = v
    say("service environment: " + (", ".join(applied) or "none"))
    return unit_key


# ---- 2. the service's API key -------------------------------------------
def _service_runs_on() -> str | None:
    """What the service's LAST start logged about its key: 'builtin', 'none',
    or None (no complaint logged = it has its own key, or the log is gone)."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", SERVICE, "-o", "cat", "--no-pager", "-n", "20000"],
            capture_output=True, text=True, timeout=30).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    starts = [i for i, l in enumerate(out) if "Cloud on — alert+snap" in l]
    if not starts:
        return None
    window = out[max(0, starts[-1] - 40):starts[-1]]
    if any("API key is the old built-in one" in l for l in window):
        return "builtin"
    if any("API key not installed" in l for l in window):
        return "none"
    return None


def pick_key(unit_key: str | None) -> tuple[str | None, str]:
    """(key, where from). None = leave settings' built-in key, as the service
    does without its credential file. The key itself is never printed."""
    key = os.environ.get("CERAVIS_API_KEY", "").strip()
    if key:
        return key, "$CERAVIS_API_KEY from your shell"
    os.environ.pop("CERAVIS_API_KEY", None)        # an empty one would blank it
    if unit_key:
        return unit_key, "the service's environment files"
    attempts = [["sudo", "-n", "cat", CRED_KEY]]
    if sys.stdin.isatty():
        attempts.append(["sudo", "cat", CRED_KEY])  # may ask for the password
    reason = "sudo not available"
    for cmd in attempts:
        if cmd == attempts[-1] and len(attempts) > 1:
            say(f"reading {CRED_KEY} with sudo (root-only) …")
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            reason = str(exc)
            break
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip(), CRED_KEY
        if "No such file" in p.stderr:
            return None, f"built-in key ({CRED_KEY} does not exist — the service uses the built-in one too)"
        reason = (p.stderr.strip().splitlines() or ["no output"])[-1]
    runs_on = _service_runs_on()
    if runs_on == "builtin":
        return None, "built-in key (the service's last start logged that it runs on it)"
    if runs_on == "none":
        return "", "no key (the service's last start logged that it has none)"
    die(f"cannot read {CRED_KEY} ({reason}) and cannot tell which key the service "
        "uses. Run it in a terminal (sudo will ask), or first:\n"
        f"    export CERAVIS_API_KEY=$(sudo cat {CRED_KEY})")
    raise AssertionError  # unreachable


# ---- 3. what the service talks to ---------------------------------------
def last_service_call(wire: Path) -> dict | None:
    """The newest wire-log record the SERVICE made (anything but the
    saveSnapshot calls a manual run like this one writes)."""
    try:
        lines = wire.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("endpoint") != "saveSnapshot" and rec.get("url"):
            return rec
    return None


def last_wire(wire: Path, endpoint: str = "saveSnapshot") -> dict | None:
    try:
        lines = wire.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("endpoint") == endpoint:
            return rec
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fire the posture transition events (sit, stand, walk, no transition) now.")
    ap.add_argument("label", nargs="?", help="camera room / label (default: first enabled)")
    ap.add_argument("--only", help="comma list of: " + ",".join(EVENTS))
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between events (default 3)")
    ap.add_argument("--posture", choices=("sitting", "standing"), default="sitting",
                    help="the posture held, for no_transition_snapshot (default sitting)")
    ap.add_argument("--held-min", type=float, default=None,
                    help="minutes held, for no_transition_snapshot (default: the "
                         "stillness window, when the first one really fires)")
    ap.add_argument("--dry-run", action="store_true", help="check everything, send nothing")
    ap.add_argument("--force", action="store_true",
                    help="send even if the server differs from the one the service last called")
    args = ap.parse_args()

    chosen = list(EVENTS)
    if args.only:
        chosen = [e.strip() for e in args.only.split(",") if e.strip()]
        bad = [e for e in chosen if e not in EVENTS]
        if bad:
            die(f"unknown event(s) {bad}; choose from {list(EVENTS)}")

    key, key_source = pick_key(load_service_env())
    if key is not None:
        os.environ["CERAVIS_API_KEY"] = key

    sys.path.insert(0, str(EDGE))
    from alerts.alert_format import describe
    from common import clock
    from common.rtsp import grab_one_frame
    from config.account_config import AccountConfig
    from config.camera_config import CameraConfig
    from config.settings import resolve_path, settings
    from events.event_enricher import _ALERT_MAP
    from integration.ceravis_api import (
        CeravisApiError, api_key_problem, is_configured, room_to_enum, save_snapshot)
    from schemas.event import Event

    wire = resolve_path(settings.data_dir) / "ceravis_api_wire.jsonl"

    # -- preflight: server, key, account, camera, live still --------------
    if not is_configured():
        die("CERAVIS_API_BASE_URL is empty — the cloud is not configured")
    base = settings.ceravis_api_base_url.rstrip("/")
    host = urlsplit(base).netloc
    say(f"server : {base}")
    ref = last_service_call(wire)
    if ref:
        ref_host = urlsplit(ref["url"]).netloc
        say(f"service: last called {ref_host} ({ref.get('endpoint')} at "
            f"{str(ref.get('ts'))[:19]}, HTTP {ref.get('http_status')})")
        if ref_host != host:
            msg = (f"this script would call {host} but the service calls {ref_host} — "
                   "the snapshots would not reach the account the device is on")
            if not args.force:
                die(msg + " (--force sends anyway)")
            say("WARNING: " + msg)
    else:
        say("service: no earlier app-server call in the wire log to compare with")

    fingerprint = hashlib.sha256(settings.ceravis_api_key.encode()).hexdigest()[:10]
    say(f"api key: {key_source}; sha256 {fingerprint}…")
    problem = api_key_problem()
    if problem:
        say(f"  note: {problem}")

    pid = AccountConfig().get().get("ceravisUserId")
    if not pid:
        die("no verified account (account.json has no ceravisUserId) — run setup step 1")

    cams = [c for c in CameraConfig().get_all() if c.is_enabled]
    if not cams:
        die("no enabled camera (cameras.json)")
    cam = cams[0]
    if args.label:
        cam = CameraConfig().get_by_label(args.label)
        if not cam:
            rooms = ", ".join(sorted({c.room_name for c in cams}))
            die(f"no camera matches {args.label!r} (cameras: {rooms})")
    camera_number = room_to_enum(cam.room_name)
    say(f"account: patientId={pid}   camera: {cam.room_name!r} -> cameraNumber={camera_number}")

    frame = grab_one_frame(cam.rtsp_url)
    if frame is None:
        die("could not grab a live frame from the camera (is it streaming?)")
    import cv2
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        die("could not encode the live frame as JPEG")
    jpeg = buf.tobytes()
    say(f"still  : {frame.shape[1]}x{frame.shape[0]}, {len(jpeg)} bytes")

    held = (args.held_min * 60.0 if args.held_min is not None
            else float(settings.stillness_window_secs))
    plan = []
    for etype in chosen:
        dwell = etype == "no_transition_snapshot"
        event = Event(event_id=str(uuid.uuid4()), event_type=etype,
                      title=_ALERT_MAP[etype][1], camera_id=cam.camera_id,
                      room_name=cam.room_name, timestamp=clock.now().isoformat(),
                      detail=args.posture if dwell else None,
                      duration_secs=held if dwell else None)
        plan.append((etype.upper(), describe(event)))

    if args.dry_run:
        for category, text in plan:
            say(f"would send {category:<24} {text.splitlines()[0]}")
        say("dry run: nothing sent")
        raise SystemExit(0)

    # -- send, one by one, the way the outbox would -----------------------
    sent = 0
    for i, (category, text) in enumerate(plan):
        if i:
            time.sleep(args.gap)
        try:
            result = save_snapshot(pid, text, camera_number, image=jpeg,
                                   category=category,
                                   idempotency_key=str(uuid.uuid4()))
            sent += 1
            brief = json.dumps(result, default=str)[:100] if result is not True else ""
            say(f"{category:<24} OK    {text.splitlines()[0]}  {brief}")
        except CeravisApiError as exc:
            say(f"{category:<24} FAIL  {exc}")
            rec = last_wire(wire)
            if rec:
                say(f"    reply: HTTP {rec.get('http_status')} from {rec.get('url')} "
                    f"body={str(rec.get('response'))[:300]!r} error={rec.get('error')}")
    say(f"done: {sent}/{len(plan)} sent to {host}"
        + ("" if sent == len(plan) else f" — see {wire}"))
    raise SystemExit(0 if sent == len(plan) else 1)


if __name__ == "__main__":
    main()
