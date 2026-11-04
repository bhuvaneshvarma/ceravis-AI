"""
Enrollment: the FMS is the only place a device's edge_id comes from.

Against a stand-in FMS on this machine (real HTTP, real signatures, both ways):
1. the enroll call: signed with the fleet enrollment key; its body is exactly
   fingerprint, device_id (this device's own name), legacy_edge_id, hardware,
   software, agent facts (with free disk for / and the data folder) and status
   — no `protocol`; and it is made at once, without waiting for the edge app
2. legacy_edge_id is the app server's token from account.json, only when valid
3. the reply: the edge_id and key are kept (identity.json, 0600) only when the
   reply is plainly this device's — never another device's, never a bad edge_id
4. setup/sync_edge_id.sh hands ONLY the edge_id to the app, as the root-only
   credential file, makes the service unit load it, restarts a running service
   once, and changes nothing the second time (sudo/systemctl stubbed)
5. the app reads its edge_id from that credential (config.account_config)

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
        self.seen: list[dict] = []
        fms = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                principal = self.headers.get(protocol.HDR_DEVICE, "")
                nonce = self.headers.get(protocol.HDR_NONCE, "")
                message = protocol.request_message(
                    principal, "POST", self.path, body,
                    self.headers.get(protocol.HDR_TIMESTAMP, ""), nonce)
                signed = protocol.verify(KEY, message, self.headers.get(protocol.HDR_SIGNATURE))
                fms.seen.append({"signed": signed, "principal": principal,
                                 "body": json.loads(body or b"{}")})
                status, payload = fms.reply if signed else (401, {"error": "unauthorized"})
                raw = json.dumps({"server_time": 1793361045.43, **payload}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 200:
                    self.send_header(protocol.HDR_SIGNATURE,
                                     protocol.sign(KEY, protocol.response_message(principal, nonce, raw)))
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
    return {"result": "enrolled", "edge_id": "ce7k2m9xq4w8n3", "origin": "issued",
            "device_id": device_identity().name, "key_version": 1,
            "secret": "nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE",
            "heartbeat": {"path": protocol.HEARTBEAT_PATH, "interval_secs": 60},
            "public_base_url": "https://edgeai.ceravishealth.in/ce7k2m9xq4w8n3",
            "warnings": [], "tunnel": None, "config": {}, **extra}


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
check("signed with the fleet enrollment key; the principal is the fingerprint",
      sent.get("signed") is True and sent.get("principal") == a.fingerprint == body.get("fingerprint"))
check("the body is exactly the agreed fields — no `protocol`",
      list(body) == ["fingerprint", "device_id", "legacy_edge_id", "hardware", "software",
                     "agent", "status"], str(list(body)))
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
print("\n2. legacy_edge_id: the app server's old token, only when valid")
check("taken from account.json's edgeId", body.get("legacy_edge_id") == "CAr5old", str(body.get("legacy_edge_id")))
(DATA / "account.json").write_text(json.dumps({"edgeId": "bad id/../x"}))
check("an invalid one is not sent", collect.legacy_edge_id() is None)
(DATA / "account.json").unlink()
check("no account: none", collect.legacy_edge_id() is None)

# --------------------------------------------------------------------------
print("\n3. the reply: kept only when plainly this device's")
kept = identity.load(state) or {}
check("edge_id, key, device_id and key version kept in identity.json",
      kept.get("edge_id") == "ce7k2m9xq4w8n3" and kept.get("secret") == good_reply()["secret"]
      and kept.get("device_id") == device_identity().name and kept.get("key_version") == 1, str(kept))
check("...readable by the agent alone (0600)",
      stat.S_IMODE((state / "identity.json").stat().st_mode) == 0o600)

for label, reply in (
        ("another device's reply", good_reply(device_id="ceravis-zzzzzzzzz")),
        ("an edge_id that is not one", good_reply(edge_id="../etc")),
        ("no key", good_reply(secret="")),
):
    reset()
    fms.reply = (200, reply)
    try:
        agent().beat()
        refused = False
    except transport.Rejected:
        refused = True
    check(f"{label}: refused, nothing saved", refused and identity.load(state) is None)

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
           "CERAVIS_LAYOUT_FILE": os.devnull, "CERAVIS_PYTHON": sys.executable}

    def sync(active: bool) -> subprocess.CompletedProcess:
        calls.write_text("")
        return subprocess.run([BASH, str(ROOT / "setup" / "sync_edge_id.sh")], cwd=ROOT,
                              env={**env, "ACTIVE": "0" if active else "1"},
                              capture_output=True, text=True)

    r0 = sync(True)
    check("not enrolled yet: nothing to do, nothing written", r0.returncode == 0 and not cred.exists(),
          r0.stdout.strip())
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
    ident.write_text(json.dumps({"edge_id": "bad id"}))
    r3 = sync(True)
    check("an unusable edge_id is refused and the stored one kept",
          r3.returncode == 2 and (cred / "edge_id").read_text() == "ce7k2m9xq4w8n3", r3.stderr.strip())
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
check("not enrolled: '' — the app server's token is never used to route", effective_edge_id() == "")
settings.edge_id = real
shutil.rmtree(creds, ignore_errors=True)

fms.server.shutdown()
shutil.rmtree(state, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All enrollment checks passed.")
