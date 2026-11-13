"""
Enrollment: the FMS is the only place a device's edge_id comes from.

Against a stand-in FMS on this machine (real HTTP, real signatures, both ways):
1. the enroll call: signed with the fleet enrollment key over FOUR lines
   (METHOD, path, timestamp, sha256 of the body), with no X-Ceravis-Device and
   no X-Ceravis-Nonce header; the reply is signed over RESPONSE, the request's
   timestamp and sha256 of the reply; its body is exactly
   device_id (this device's own name), hardware, software, agent facts (with
   free disk for / and the data folder) and status — no `protocol`, no
   `legacy_edge_id`; and it is made at once, without waiting for the edge app
2. nothing of the app server's account record (account.json) is sent
3. the reply: the edge_id and key are kept (identity.json, 0600) only when the
   reply is plainly this device's — never another device's, never a bad edge_id
4. setup/sync_edge_id.sh hands ONLY the edge_id to the app, as the root-only
   credential file, makes the service unit load exactly what exists (a sealed
   image with no file must still start), restarts once, changes nothing the
   second time; --migrate keeps a pre-FMS home's edge_id until it enrolls,
   --store-only leaves units and restarts to its caller (sudo/systemctl stubbed)
5. the app — service and every page — reads its edge_id from that credential
6. a test FMS on this network over plain http: only with FMS_ALLOW_HTTP_LAN=1
   and a private IP address — never a name, a public address, or without it;
   install_fleet_agent.sh keeps FMS_URL and the key side by side in enroll.env;
   seal.sh drops the test address so no golden image ships it
7. /etc/ceravis/versions.json: what is installed, written by every installer
   (a rollback records the release it went back to)

    python tests/test_fleet_enroll.py
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
sys.path.insert(0, str(ROOT / "edge" / "fleet"))

import fms_protocol as protocol                                    # noqa: E402
from fms_agent import collect, identity, transport                 # noqa: E402
from fms_agent.agent import Agent, Waiting                         # noqa: E402
from fms_agent.config import Config                                # noqa: E402
from common.device_identity import identity as device_identity     # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


KEY = "test-enroll-key-0123456789abcdef0123456789"
DATA = Path(os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="ceravis-test-data-"))
os.environ["DATA_DIR"] = str(DATA)


class FakeFms:
    """POST /v1/device/enroll as the FMS does it: the signature is checked, the
    body kept, and the reply signed and bound to the request's nonce."""

    def __init__(self) -> None:
        self.reply: tuple[int, dict] = (200, {})
        self.old_reply_signature = False      # sign the reply the pre-v2 way (must be refused)
        self.seen: list[dict] = []
        fms = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stamp = self.headers.get(protocol.HDR_TIMESTAMP, "")
                # The agreed v2 enroll call: exactly these four lines, signed.
                message = protocol.enroll_request_message("POST", self.path, stamp, body)
                signed = protocol.verify(KEY, message, self.headers.get(protocol.HDR_SIGNATURE))
                fms.seen.append({"signed": signed, "message": message,
                                 "headers": {k.lower() for k in self.headers.keys()},
                                 "body": json.loads(body or b"{}")})
                status, payload = fms.reply if signed else (401, {"error": "unauthorized"})
                raw = json.dumps({"server_time": 1793361045.43, **payload}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 200:
                    self.send_header(protocol.HDR_SIGNATURE,
                                     protocol.sign(KEY, protocol.response_message("x", "y", raw)
                                                   if fms.old_reply_signature else
                                                   protocol.enroll_response_message(stamp, raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


fms = FakeFms()
state = Path(tempfile.mkdtemp(prefix="ceravis-agent-state-"))


def agent() -> Agent:
    # The edge app is NOT running (nothing answers on port 9): enrollment must not wait for it.
    return Agent(Config(url=fms.url, enroll_key=KEY, status_url="http://127.0.0.1:9/status",
                        state_dir=state, fingerprint="1424622063459"))


def good_reply(**extra) -> dict:
    """The reply exactly as the Java FMS builds it (final, 2026-11-04): no
    key_version, origin, public_base_url or tunnel; config null."""
    return {"result": "enrolled", "edge_id": "ce7k2m9xq4w8n3",
            "device_id": device_identity().name,
            "secret": "nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE",
            "heartbeat": {"path": protocol.HEARTBEAT_PATH, "interval_secs": 60.0},
            "warnings": [], "config": None, **extra}


def reset() -> None:
    shutil.rmtree(state, ignore_errors=True)
    state.mkdir()
    fms.seen.clear()


# --------------------------------------------------------------------------
print("\n1. the enroll call: signed, the agreed body, at once")
(DATA / "account.json").write_text(json.dumps({"ceravisUserId": 5012, "edgeId": "CAr5old"}))
reset()
fms.reply = (200, good_reply())
a = agent()
nxt = a.beat()
sent = fms.seen[-1] if fms.seen else {"body": {}}
body = sent["body"]
check("it enrolls at once, with the edge app not answering (no waiting for it)",
      len(fms.seen) == 1 and nxt == 1.0, f"{len(fms.seen)} call(s), next in {nxt}")
import time                                                          # noqa: E402
t0, first = time.monotonic(), []
loop = Agent(Config(url=fms.url, enroll_key=KEY, status_url="http://127.0.0.1:9/status",
                    state_dir=Path(tempfile.mkdtemp(prefix="ceravis-agent-fresh-")),
                    fingerprint="1424622063459"))                   # never enrolled
loop.beat = lambda: (first.append(time.monotonic() - t0), loop.stop.set(), 60.0)[-1]
threading.Thread(target=loop.run_forever, daemon=True).start()
loop.stop.wait(10)
check("...and the agent's loop makes that first call at once (no stagger before enrolling)",
      bool(first) and first[0] < 1.0, str(first))
import hashlib                                                       # noqa: E402
lines = sent.get("message", b"").decode().split("\n")
check("signed with the fleet enrollment key over exactly 4 lines: METHOD, path, timestamp, sha256(body)",
      sent.get("signed") is True and len(lines) == 4 and lines[:2] == ["POST", protocol.ENROLL_PATH]
      and lines[3] == hashlib.sha256(transport.encode(body)).hexdigest(), str(lines))
check("no X-Ceravis-Device and no X-Ceravis-Nonce header; no fingerprint anywhere in the call",
      protocol.HDR_DEVICE.lower() not in sent.get("headers", set())
      and protocol.HDR_NONCE.lower() not in sent.get("headers", set())
      and "fingerprint" not in body and a.fingerprint not in json.dumps(body), str(sent.get("headers")))
check("the body is exactly the agreed fields — no `protocol`, no `legacy_edge_id`",
      list(body) == ["device_id", "hardware", "software", "agent", "status"], str(list(body)))
check("device_id = this device's own name (common/device_identity, as the hostname and hotspot use)",
      body.get("device_id") == device_identity().name and body["device_id"].startswith("ceravis-"),
      str(body.get("device_id")))
check("status: null when the edge app is not answering", body.get("status") is None)
hw, sw, facts = body.get("hardware") or {}, body.get("software") or {}, body.get("agent") or {}
check("hardware: the board's model and its RAM in bytes",
      isinstance(hw.get("model"), str) and hw["model"] and isinstance(hw.get("ram_total_bytes"), int)
      and hw["ram_total_bytes"] > 0, str(hw))
check("software: the edge release from edge/VERSION",
      sw.get("edge_version") == (ROOT / "edge" / "VERSION").read_text().strip(), str(sw))
check("agent facts: everything the agent reported before is still there",
      all(k in facts for k in ("version", "unix_time", "boot_id", "uptime_secs", "hostname", "os",
                               "kernel", "l4t", "python", "rtt_ms", "interval_secs", "can_enroll")),
      str(sorted(facts)))
check("no enroll_key_id: the FMS checks the signature, it needs no key name",
      "enroll_key_id" not in facts and not hasattr(protocol, "enroll_key_id"), str(sorted(facts)))

print("\n   free disk (statvfs) for / and the data folder")
root_now = os.statvfs("/")
disk = facts.get("disk") or {}
r = disk.get("root") or {}
check("root: total, free and used % of /",
      r.get("total_bytes") == root_now.f_blocks * root_now.f_frsize
      and abs(r.get("free_bytes", -1) - root_now.f_bavail * root_now.f_frsize) < 512 * 1024 * 1024
      and isinstance(r.get("used_pct"), float) and 0 <= r["used_pct"] <= 100, str(r))
check("used % = 100 × (total − free) ÷ total, one decimal",
      r.get("used_pct") == round(100 * (r["total_bytes"] - r["free_bytes"]) / r["total_bytes"], 1))
check("data: the edge's data folder (DATA_DIR)",
      (disk.get("data") or {}).get("total_bytes") == os.statvfs(DATA).f_blocks * os.statvfs(DATA).f_frsize,
      str(disk.get("data")))
check("a data folder not there yet is null, not an error",
      collect._usage(DATA / "not-there-yet") is None)

# --------------------------------------------------------------------------
print("\n2. nothing of the app server's account record is sent")
check("account.json's edgeId (the app server's token) appears nowhere in the enroll call",
      "CAr5old" not in json.dumps(body) and "legacy_edge_id" not in json.dumps(body)
      and not hasattr(collect, "legacy_edge_id"), str(list(body)))
(DATA / "account.json").unlink()

# --------------------------------------------------------------------------
print("\n3. the reply: kept only when plainly this device's")
kept = identity.load(state) or {}
check("edge_id, heartbeat key and device_id kept in identity.json — never the reply's secret",
      kept.get("edge_id") == "ce7k2m9xq4w8n3"
      and kept.get("heartbeat_key") == protocol.heartbeat_key(KEY, good_reply()["secret"])
      and "secret" not in kept and kept.get("device_id") == device_identity().name,
      str(sorted(kept)))
check("...readable by the agent alone (0600)",
      stat.S_IMODE((state / "identity.json").stat().st_mode) == 0o600)

for label, reply in (
        ("another device's reply", good_reply(device_id="ceravis-zzzzzzzzz")),
        ("an edge_id that is not one", good_reply(edge_id="../etc")),
        ("an edge_id the tunnel cannot route", good_reply(edge_id="a.b")),
        ("no key", good_reply(secret="")),
):
    reset()
    fms.reply = (200, reply)
    why = ""
    try:
        agent().beat()
    except transport.Rejected as exc:
        why = str(exc)
    check(f"{label}: refused, nothing saved",
          "not saved" in why and fms.seen and fms.seen[-1]["signed"] and identity.load(state) is None, why)

reset()
fms.reply, fms.old_reply_signature = (200, good_reply()), True
why = ""
try:
    agent().beat()
except transport.Rejected as exc:
    why = str(exc)
fms.old_reply_signature = False
check("a reply NOT signed over RESPONSE, the request timestamp and sha256(reply): refused, nothing saved",
      "signature invalid" in why and identity.load(state) is None, why)

import logging                                                      # noqa: E402
reset()
fms.reply = (200, good_reply(warnings=["device already enrolled; details updated",
                                       "device clock differs from server by 431704 seconds"]))
with_notes = agent()
logged: list[str] = []
handler = logging.Handler()
handler.emit = lambda record: logged.append(record.getMessage())
agent_log = logging.getLogger("fms_agent")
level = agent_log.level
agent_log.setLevel(logging.INFO)          # setLevel also clears the cached level
agent_log.addHandler(handler)
with_notes.beat()
agent_log.removeHandler(handler)
agent_log.setLevel(level)
check("the FMS's warnings are logged, and the key is still kept",
      any("details updated" in m for m in logged) and any("431704" in m for m in logged)
      and identity.load(state) is not None, str(logged))
check("...and the log does not claim an origin the FMS did not send",
      any(m == "enrolled with the fleet as ce7k2m9xq4w8n3" for m in logged), str(logged))
check("the device's clock is corrected from server_time",
      abs(with_notes.clock_offset - (1793361045.43 - time.time())) < 5, str(with_notes.clock_offset))

reset()
older = good_reply()
del older["device_id"]
fms.reply = (200, older)
agent().beat()
check("a reply without device_id (an older server) is still accepted",
      (identity.load(state) or {}).get("edge_id") == "ce7k2m9xq4w8n3")

reset()
fms.reply = (409, {"error": "already_enrolled"})
try:
    agent().beat()
    waited = False
except Waiting:
    waited = True
check("409 already_enrolled: waits for an admin's Reset key, nothing saved",
      waited and identity.load(state) is None)

# --------------------------------------------------------------------------
print("\n4. setup/sync_edge_id.sh: only the edge_id reaches the app, as a credential")
BASH = shutil.which("bash")
if not BASH or os.name == "nt":
    check("bash available (skipped here)", True)
else:
    box = Path(tempfile.mkdtemp(prefix="ceravis-sync-"))
    fake, out, cred = box / "bin", box / "units", box / "credstore"
    for d in (fake, out):
        d.mkdir()
    calls = box / "systemctl.log"
    stubs = {
        "id": 'if [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi',
        "install": 'args=(); for a in "$@"; do case "$a" in root) ;; -o|-g) ;; *) args+=("$a") ;; esac; done\n'
                   'exec /usr/bin/install "${args[@]}"',
        "sudo": '[ "$1" = -n ] && shift\nif [ "$1" = tee ]; then cat > "$OUT/$(basename "$2")"; exit 0; fi\nexec "$@"',
        "systemctl": 'echo "$*" >> "$CALLS"\n[ "$1" = is-active ] && exit "${ACTIVE:-1}"\nexit 0',
    }
    for name, code in stubs.items():
        (fake / name).write_text("#!/bin/bash\n" + code + "\n")
        (fake / name).chmod(0o755)
    ident = box / "identity.json"
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("CERAVIS_")},
           "PATH": f"{fake}{os.pathsep}{os.environ.get('PATH', '')}", "OUT": str(out), "CALLS": str(calls),
           "FMS_IDENTITY_FILE": str(ident), "CERAVIS_CREDSTORE": str(cred), "CERAVIS_UNIT_DIR": str(out),
           "CERAVIS_LAYOUT_FILE": os.devnull, "CERAVIS_PYTHON": sys.executable,
           "CERAVIS_DATA_DIR": str(box / "data"), "CERAVIS_SYNC_LOCK": str(box / "sync.lock")}
    (box / "data").mkdir()

    def sync(active: bool, *args: str) -> subprocess.CompletedProcess:
        calls.write_text("")
        return subprocess.run([BASH, str(ROOT / "setup" / "sync_edge_id.sh"), *args], cwd=ROOT,
                              env={**env, "ACTIVE": "0" if active else "1"},
                              capture_output=True, text=True)

    unit_path = out / "ceravis.service"
    (box / "data" / "account.json").write_text(json.dumps({"edgeId": "CAr5old"}))
    r0 = sync(True)
    check("not enrolled yet: nothing to do, nothing written", r0.returncode == 0 and not cred.exists(),
          r0.stdout.strip())
    unit_path.write_text("[Service]\nUser=ceravis\n")
    r0m = sync(True, "--migrate", "--store-only")
    check("--migrate: a home installed before keeps the edge_id it routes on (account.json)",
          r0m.returncode == 0 and (cred / "edge_id").read_text() == "CAr5old", r0m.stdout + r0m.stderr)
    check("--store-only: the unit and the service are left to the caller",
          unit_path.read_text() == "[Service]\nUser=ceravis\n" and "restart" not in calls.read_text())
    shutil.rmtree(cred)
    ident.write_text(json.dumps({"edge_id": "ce7k2m9xq4w8n3", "secret": "the-device-key-never-copied"}))
    (out / "ceravis.service").write_text("[Service]\nUser=ceravis\n")      # installed before: no credential
    r1 = sync(True)
    stored = (cred / "edge_id").read_text() if (cred / "edge_id").exists() else ""
    unit = (out / "ceravis.service").read_text()
    check("the edge_id alone is stored, exactly, root-only (0600)",
          r1.returncode == 0 and stored == "ce7k2m9xq4w8n3"
          and stat.S_IMODE((cred / "edge_id").stat().st_mode) == 0o600, r1.stdout + r1.stderr)
    check("...never the device key", "the-device-key-never-copied" not in stored)
    check("the service unit now loads it as the credential edge_id",
          f"LoadCredential=edge_id:{cred}/edge_id" in unit, unit[:200])
    check("a running service is restarted once, without blocking (boot-order safe)",
          "--no-block try-restart ceravis" in calls.read_text(), calls.read_text())
    r2 = sync(True)
    check("run again: nothing changes, no restart",
          r2.returncode == 0 and "restart" not in calls.read_text(), calls.read_text())
    sync(True, "--migrate")
    check("--migrate never replaces an edge_id already stored",
          (cred / "edge_id").read_text() == "ce7k2m9xq4w8n3")
    # The case a home installed before the FMS goes through: it routed on the app
    # server's edge_id (migrated), then the FMS enrolls it with a NEW one.
    (cred / "edge_id").write_text("CAr5old")
    ident.write_text(json.dumps({"edge_id": "2zuvi9kd83txoj", "secret": "k"}))
    rn = sync(True)
    check("a migrated edge_id is REPLACED by the one the FMS issues, and ceravis restarted once",
          rn.returncode == 0 and (cred / "edge_id").read_text() == "2zuvi9kd83txoj"
          and calls.read_text().count("try-restart ceravis") == 1, rn.stdout + calls.read_text())
    check("...and running it again changes nothing", sync(True).returncode == 0
          and "restart" not in calls.read_text(), calls.read_text())
    ident.write_text(json.dumps({"edge_id": "ce7k2m9xq4w8n3", "secret": "k"}))
    sync(True)                                      # back to the id the next checks expect
    ident.write_text(json.dumps({"edge_id": "bad id"}))
    r3 = sync(True)
    check("an unusable edge_id is refused and the stored one kept",
          r3.returncode == 2 and (cred / "edge_id").read_text() == "ce7k2m9xq4w8n3", r3.stderr.strip())
    # A sealed image: the identity and the file are gone, the unit still loads it —
    # systemd would refuse to start ceravis. The boot run puts the unit right.
    ident.unlink()
    (cred / "edge_id").unlink()
    r4 = sync(False)
    check("a unit that loads a credential file no longer there is re-written without it (seal)",
          r4.returncode == 0 and "LoadCredential=edge_id" not in unit_path.read_text()
          and "daemon-reload" in calls.read_text(), r4.stdout + r4.stderr)
    shutil.rmtree(box, ignore_errors=True)

# --------------------------------------------------------------------------
print("\n5. the app reads its edge_id from that credential")
from config.settings import Settings, settings                   # noqa: E402
from config.account_config import effective_edge_id              # noqa: E402

creds = Path(tempfile.mkdtemp(prefix="ceravis-creds-"))
(creds / "edge_id").write_text("ce7k2m9xq4w8n3")
fresh = Settings(_secrets_dir=str(creds))
check("the credential file named edge_id sets it", fresh.edge_id == "ce7k2m9xq4w8n3", repr(fresh.edge_id))
jet = (ROOT / "edge" / "infra" / "env" / "jetson.env").read_text(encoding="utf-8")
check("jetson.env names no EDGE_ID (even an empty one would hide the credential)",
      not any(line.split("=", 1)[0].strip() == "EDGE_ID" for line in jet.splitlines()
              if line.strip() and not line.lstrip().startswith("#")))
(DATA / "account.json").write_text(json.dumps({"ceravisUserId": 5012, "edgeId": "CAr5old"}))
real, settings.edge_id = settings.edge_id, "ce7k2m9xq4w8n3"
check("effective_edge_id() = the FMS one, whatever account.json says",
      effective_edge_id() == "ce7k2m9xq4w8n3")
settings.edge_id = ""
os.environ["CERAVIS_LAYOUT_FILE"] = os.devnull                   # never a real device's credstore
from config import account_config                                # noqa: E402
account_config._edge_id_from_credstore.cache_clear()
check("not enrolled: '' — the app server's token is never used to route", effective_edge_id() == "")
settings.edge_id = real
shutil.rmtree(creds, ignore_errors=True)
pages = {name: (ROOT / "edge" / "static" / name).read_text(encoding="utf-8")
         for name in ("cameras-panel.js", "live-view.js", "talk.js", "setup.html",
                      "ui-testing/monitor.html", "ui-testing/recordings.html")}
stale = [n for n, t in pages.items() if "user.edgeId" in t or "u.edgeId" in t]
check("every page takes the edge_id from /api/v1/account's edge_id, none from the account record",
      not stale and all("edge_id" in t for t in pages.values()), str(stale))

# --------------------------------------------------------------------------
print("\n6. a test FMS on this network: plain http only when asked, only to a private IP")
from fms_agent.config import ConfigError, lan_test_fms           # noqa: E402


def config_with(**env):
    saved = {k: os.environ.get(k) for k in ("FMS_URL", "FMS_ALLOW_HTTP_LAN")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(env)
        return Config.from_env()
    except ConfigError as exc:
        return exc
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


lan = "http://192.168.1.40:8085"
check("http to a private IP with FMS_ALLOW_HTTP_LAN=1: allowed",
      isinstance(config_with(FMS_URL=lan, FMS_ALLOW_HTTP_LAN="1"), Config))
check("...10.x and 172.16-31.x too",
      all(isinstance(config_with(FMS_URL=u, FMS_ALLOW_HTTP_LAN="1"), Config)
          for u in ("http://10.0.0.7:8085", "http://172.20.3.4:9000")))
check("the same address without the flag: refused", isinstance(config_with(FMS_URL=lan), ConfigError))
check("FMS_ALLOW_HTTP_LAN=yes is not 1: refused",
      isinstance(config_with(FMS_URL=lan, FMS_ALLOW_HTTP_LAN="yes"), ConfigError))
check("a NAME over http, even with the flag: refused (a name can resolve anywhere)",
      isinstance(config_with(FMS_URL="http://fms.local:8085", FMS_ALLOW_HTTP_LAN="1"), ConfigError))
check("a PUBLIC address over http, even with the flag: refused",
      isinstance(config_with(FMS_URL="http://8.8.8.8:8085", FMS_ALLOW_HTTP_LAN="1"), ConfigError))
check("https anywhere and http to this machine: as before",
      isinstance(config_with(FMS_URL="https://fms.ceravishealth.in"), Config)
      and isinstance(config_with(FMS_URL=fms.url), Config))
check("lan_test_fms() is only for http", not lan_test_fms("https://192.168.1.40", {"FMS_ALLOW_HTTP_LAN": "1"}))

src = (ROOT / "setup" / "install_fleet_agent.sh").read_text(encoding="utf-8")
helper = src[src.index("set_fleet_value() {"):src.index("has_key() {")]
box = Path(tempfile.mkdtemp(prefix="ceravis-enrollenv-"))
key_file = box / "etc" / "enroll.env"
script = ("set -euo pipefail\nsudo() { \"$@\"; }\n" f"KEY_FILE='{key_file}'\n" + helper +
          "set_fleet_value FMS_ENROLL_KEY k1\nset_fleet_value FMS_URL http://192.168.1.40:8085\n"
          "set_fleet_value FMS_ALLOW_HTTP_LAN 1\nset_fleet_value FMS_ENROLL_KEY k2\n"
          "set_fleet_value FMS_ALLOW_HTTP_LAN ''\n")
r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
lines = key_file.read_text().splitlines() if key_file.exists() else []
check("enroll.env: each value set once, a changed one replaced, an empty one removed",
      r.returncode == 0 and sorted(lines) == ["FMS_ENROLL_KEY=k2", "FMS_URL=http://192.168.1.40:8085"],
      f"{lines} {r.stderr.strip()}")
check("...and it stays root-only (0600)",
      key_file.exists() and stat.S_IMODE(key_file.stat().st_mode) == 0o600,
      oct(stat.S_IMODE(key_file.stat().st_mode)) if key_file.exists() else "missing")
shutil.rmtree(box, ignore_errors=True)
seal = (ROOT / "setup" / "seal.sh").read_text(encoding="utf-8")
check("seal.sh drops a test FMS address from enroll.env (no golden image ships one), keeps the key",
      "/^FMS_URL=/d" in seal and "/^FMS_ALLOW_HTTP_LAN=/d" in seal and "/^FMS_ENROLL_KEY=/d" not in seal)

# --------------------------------------------------------------------------
print("\n7. versions.json: what is installed, written by every installer")
box = Path(tempfile.mkdtemp(prefix="ceravis-versions-"))
(box / "bin").mkdir()
for name, out in (("mediamtx", "v1.9.3"), ("frpc", "0.61.1")):
    (box / "bin" / name).write_text(f"#!/bin/sh\necho {out}\n")
    (box / "bin" / name).chmod(0o755)
(box / "bin" / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
(box / "bin" / "sudo").chmod(0o755)
out_file = box / "etc" / "versions.json"
env = {**os.environ, "PATH": f"{box / 'bin'}:{os.environ['PATH']}", "CERAVIS_LAYOUT_FILE": os.devnull,
       "CERAVIS_ROOT": str(ROOT), "CERAVIS_PYTHON": sys.executable,
       "CERAVIS_VERSIONS_FILE": str(out_file),
       "CERAVIS_MEDIAMTX_BIN": str(box / "bin" / "mediamtx"), "CERAVIS_FRPC_BIN": str(box / "bin" / "frpc")}
r = subprocess.run(["bash", str(ROOT / "setup" / "record_versions.sh")], capture_output=True, text=True, env=env)
try:
    v = json.loads(out_file.read_text())
except (OSError, ValueError):
    v = {}
agent_ver = (ROOT / "edge" / "fleet" / "fms_agent" / "__init__.py").read_text()
check("written, readable by all (no secret in it)",
      r.returncode == 0 and out_file.exists() and stat.S_IMODE(out_file.stat().st_mode) == 0o644,
      r.stderr.strip()[-200:])
check("release = edge/VERSION; fleet agent = its __version__ and protocol",
      (v.get("release") or {}).get("version") == (ROOT / "edge" / "VERSION").read_text().strip()
      and f'"{(v.get("fleet_agent") or {}).get("version")}"' in agent_ver
      and (v.get("fleet_agent") or {}).get("protocol") == protocol.VERSION, str(v))
check("mediamtx and frpc: what the installed binaries say",
      v.get("mediamtx") == "v1.9.3" and v.get("frpc") == "0.61.1", str(v))
check("python and the time it was recorded",
      v.get("python") == ".".join(map(str, sys.version_info[:3])) and v.get("recorded_at"), str(v))
env.update(CERAVIS_MEDIAMTX_BIN=str(box / "none"), CERAVIS_FRPC_BIN=str(box / "none"))
r = subprocess.run(["bash", str(ROOT / "setup" / "record_versions.sh")], capture_output=True, text=True, env=env)
check("a binary not installed yet is '' — the file is still written",
      r.returncode == 0 and json.loads(out_file.read_text()).get("mediamtx") == "", r.stderr.strip()[-200:])
shutil.rmtree(box, ignore_errors=True)
hooks = {name: (ROOT / "setup" / name).read_text(encoding="utf-8")
         for name in ("install.sh", "setup.sh", "update.sh", "upgrade.sh")}
check("every installer records it (install, setup, update, upgrade)",
      all("record_versions.sh" in t for t in hooks.values()),
      str([n for n, t in hooks.items() if "record_versions.sh" not in t]))
sw = hooks["update.sh"][hooks["update.sh"].index("switch_to() {"):]
sw = sw[:sw.index("\n}\n")]
check("update.sh records the release it switched to — after a rollback, the one it went back to",
      'CERAVIS_ROOT="$1"' in sw and "record_versions.sh" in sw)

fms.server.shutdown()
shutil.rmtree(state, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All enrollment checks passed.")
