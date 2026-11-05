"""
The heartbeat: every part of the device, each with its state, uptime and warnings.

1. the edge app's GET /api/v1/system/health: backend, services (MediaMTX, app
   server, internet, tunnel), every camera (fps, uptime, reconnects, recording)
   and the board sensors — each part with its own `warnings`; built from the SAME
   warning rules as /api/v1/system/status, whose output does not change; and
   readable over loopback like /system/status
2. the agent's systemd services: running / stopped / failed / not installed,
   uptime, restarts — "restarted since the last heartbeat" said once, not forever
3. hardware: the app's checks while it answers, the agent's own as the fallback
   when it does not (data disk, temperature, clock) — never both
4. the heartbeat itself, against a stand-in FMS (real HTTP, real signatures):
   the body is exactly {status, results} — no protocol, no agent, no
   status_error; signed over the 6 lines with the device key; an order in the
   reply is answered in the next heartbeat

    python tests/test_fleet_heartbeat.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import types
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"
sys.path.insert(0, str(EDGE))
sys.path.insert(0, str(EDGE / "fleet"))
os.chdir(EDGE)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + str(detail)[:300]) if detail else ''}")
    if not ok:
        failures.append(label)


# --------------------------------------------------------------------------
print("\n1. the edge app's /api/v1/system/health")
from common import clock                                         # noqa: E402
from config.settings import settings                             # noqa: E402

settings.data_dir = tempfile.mkdtemp(prefix="ceravis-health-")
from config.camera_config import CameraConfig                    # noqa: E402
from ingestion.camera_status import CameraHealthState, CameraStatus  # noqa: E402
from schemas.cameras import Camera                               # noqa: E402

cfg = CameraConfig()
cfg.add(Camera(room_name="LOUNGE", rtsp_url="rtsp://cam1/stream1"))
cfg.add(Camera(room_name="KITCHEN", rtsp_url="rtsp://cam2/stream1"))
lounge, kitchen = [c.camera_id for c in cfg.get_all()]

import api.setup_routes as setup_routes                          # noqa: E402
import api.system_routes as sr                                   # noqa: E402
import livestream.mediamtx_client as mtx                         # noqa: E402

setup_routes.internet_problem = lambda: None
mtx.is_up = lambda: True
mtx.path_info = lambda cid: {"ready": cid == lounge, "readers": [{}] if cid == lounge else []}
mtx.path_stream_info = lambda cid: {"codec": "h264" if cid == lounge else "h265", "profile": "High"}
sr.code_version = lambda: {"commit": "abc1234", "on_disk": "abc1234", "restart_needed": False}
sr._storage_status = lambda: {"disk_used_pct": 40.0, "recordings_gb": 12.5, "retention_hours": 72}
sr._time_status = lambda: {"local": "x", "timezone": "Asia/Kolkata", "ntp_synchronized": True}
sr._tunnel_status = lambda: {"installed": True, "edge_id": "ce7k2m9xq4w8n3", "readable": True,
                             "keyed": True, "reason": None}
sr._cloud_status = lambda outbox=None: {"configured": True, "api_key_problem": None,
                                        "recent_calls": 9, "recent_errors": 0,
                                        "last_call": {"ok": True},
                                        "outbox": {"pending": 0, "oldest_pending_age_secs": None}}


class Manager:
    def get_status(self):
        now = clock.now()
        return {
            lounge: CameraStatus(camera_id=lounge, camera_name="", room_name="LOUNGE",
                                 is_running=True, health_state=CameraHealthState.RUNNING,
                                 frames_captured=91234, reconnect_count=1,
                                 last_frame_time=now - timedelta(seconds=0.1),
                                 current_fps=14.84, frame_width=1920, frame_height=1080,
                                 connected_secs=3601.4),
            kitchen: CameraStatus(camera_id=kitchen, camera_name="", room_name="KITCHEN",
                                  is_running=True, health_state=CameraHealthState.RECONNECTING,
                                  frames_captured=0, reconnect_count=7, last_frame_time=None),
        }


class Monitor:
    def problems(self):
        return []

    def snapshot(self):
        return {"gpu_pct": 64, "cpu_pct_avg": 37.5, "cpu_temp_c": 52.1, "gpu_temp_c": 50.3,
                "junction_temp_c": 53.0, "power_in_w": 7.8, "overcurrent_events": 0}


class RC:
    def is_enabled(self):
        return True

    def recording_now(self):
        return [lounge]

    def health(self):
        return [{"camera_id": lounge, "recording": True, "writing_ok": True,
                 "newest_segment_age_secs": 4.2, "segments_last_hour": 60},
                {"camera_id": kitchen, "recording": True, "writing_ok": False,
                 "newest_segment_age_secs": 900.0, "segments_last_hour": 0}]


mediamtx = types.SimpleNamespace(uptime_secs=86000.2, restarts=0)
state = types.SimpleNamespace(system_monitor=Monitor(), recording_controller=RC(),
                              camera_manager=Manager(), gallery=types.SimpleNamespace(size=3),
                              outbox=None, mediamtx=mediamtx)
request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
status = sr.system_status(request)
health = sr.system_health(request)

check("four parts: backend, services, cameras, hardware",
      sorted(health) == ["backend", "cameras", "hardware", "services"], sorted(health))
parts = [health["backend"], health["hardware"], *health["services"].values(), *health["cameras"]]
check("EVERY part carries its own `state` and a `warnings` list",
      all(isinstance(p.get("warnings"), list) and p.get("state") for p in parts))
by_id = {c["camera_id"]: c for c in health["cameras"]}
lo, ki = by_id[lounge], by_id[kitchen]
check("a running camera: state, fps, resolution, codec, uptime, reconnects, recording",
      lo["state"] == "running" and lo["fps"] == 14.8 and lo["resolution"] == "1920x1080"
      and lo["codec"] == "h264" and lo["uptime_secs"] == 3601 and lo["reconnects"] == 1
      and lo["recording"]["on"] and lo["recording"]["writing_ok"] and lo["viewers"] == 1
      and lo["warnings"] == [], lo)
check("...and its last frame is fresh (seconds since it)", 0 <= (lo["last_frame_age_secs"] or -1) < 5,
      lo["last_frame_age_secs"])
check("a camera that is not delivering: its own warnings (no video, codec, recording)",
      ki["state"] == "reconnecting" and ki["uptime_secs"] is None and ki["reconnects"] == 7
      and any("no video" in w for w in ki["warnings"])
      and any("H265" in w for w in ki["warnings"])
      and any("no segments landing" in w for w in ki["warnings"]), ki["warnings"])
svc = health["services"]
check("services from the app: mediamtx (uptime, restarts), app_server, internet, tunnel",
      svc["mediamtx"]["state"] == "running" and svc["mediamtx"]["uptime_secs"] == 86000
      and svc["app_server"]["state"] == "ok" and svc["internet"]["state"] == "ok"
      and svc["tunnel"]["state"] == "ok" and svc["tunnel"]["routes_edge_id"] is True, svc)
check("backend: version, commit, its own uptime, AI and recording summary",
      health["backend"]["version"] == settings.app_version and health["backend"]["commit"] == "abc1234"
      and isinstance(health["backend"]["uptime_secs"], int)
      and health["backend"]["ai"]["gallery_size"] == 3
      and health["backend"]["recording"]["cameras_recording"] == 1, health["backend"])
check("hardware sensors from tegrastats: GPU load, temperatures, board power",
      health["hardware"]["gpu_load_pct"] == 64 and health["hardware"]["temperature_c"]["tj"] == 53.0
      and health["hardware"]["board_power_w"] == 7.8, health["hardware"])
flat = [w for p in parts for w in p["warnings"]]
check("every /system/status problem is in /system/health, under its part",
      all(p in flat for p in status["problems"]) and len(status["problems"]) == 2,
      status["problems"])
check("/system/status still lists them in one flat `problems` list, as before",
      status["status"] == "degraded" and set(status) >= {"problems", "cameras", "recording", "storage",
                                                         "cloud", "tunnel", "media_backbone"})

# The tunnel says "warning" only when it is KNOWN to be wrong.
cases = {"not_installed": {"installed": False},
         "not_enrolled": {"installed": True, "edge_id": None},
         "unknown": {"installed": True, "edge_id": "e1", "readable": False},
         "warning": {"installed": True, "edge_id": "e1", "readable": True, "keyed": False,
                     "reason": "frpc.toml does not route /e1"}}
check("tunnel: not installed / not enrolled / unreadable are states; only a wrong route warns",
      all(sr._tunnel_health(t)["state"] == want and bool(sr._tunnel_health(t)["warnings"]) == (want == "warning")
          for want, t in cases.items()))

from api import auth                                              # noqa: E402
check("/api/v1/system/health is readable over loopback, like /system/status",
      auth._matches(auth._LOCAL_READ_RE, "GET", "/api/v1/system/health")
      and auth._matches(auth._LOCAL_READ_RE, "GET", "/api/v1/system/status")
      and not auth._matches(auth._LOCAL_READ_RE, "POST", "/api/v1/system/health"))
APP_HEALTH = json.loads(json.dumps(health, default=str))          # what the agent receives

# --------------------------------------------------------------------------
print("\n2. the agent's systemd services")
from fms_agent import collect, health as agent_health, identity, transport  # noqa: E402
import fms_protocol as protocol                                   # noqa: E402

box = Path(tempfile.mkdtemp(prefix="ceravis-hb-"))
(box / "bin").mkdir()
scenario = box / "units.txt"
(box / "bin" / "systemctl").write_text('#!/bin/sh\ncat "$UNITS_FILE"\n')
(box / "bin" / "timedatectl").write_text('#!/bin/sh\necho "${NTP:-yes}"\n')
for f in (box / "bin").iterdir():
    f.chmod(0o755)
os.environ["PATH"] = f"{box / 'bin'}{os.pathsep}{os.environ['PATH']}"
os.environ["UNITS_FILE"] = str(scenario)
mono_us = int((__import__("time").monotonic() - 7200) * 1e6)          # active for 2 hours


def units(ceravis_restarts: int) -> None:
    blocks = [
        f"Id=ceravis.service\nLoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n"
        f"ActiveEnterTimestampMonotonic={mono_us}\nNRestarts={ceravis_restarts}\nMemoryCurrent=812345678",
        "Id=frpc.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n"
        "ActiveEnterTimestampMonotonic=0\nNRestarts=0\nMemoryCurrent=[not set]",
        f"Id=ceravis-fleet-agent.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
        f"Result=success\nActiveEnterTimestampMonotonic={mono_us}\nNRestarts=0\nMemoryCurrent=31000000",
        "Id=ceravis-edge-id.path\nLoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\n"
        "ActiveEnterTimestampMonotonic=0\nNRestarts=\nMemoryCurrent=18446744073709551615",
        "Id=ceravis-reboot.timer\nLoadState=loaded\nActiveState=failed\nSubState=failed\nResult=resources\n"
        "ActiveEnterTimestampMonotonic=0\nNRestarts=\nMemoryCurrent=[not set]",
    ]
    scenario.write_text("\n\n".join(blocks) + "\n")


units(2)
h = agent_health.Health()
facts = agent_health.agent_facts(41.3, 60.0, "the-key")
s1 = h.build(APP_HEALTH, None, facts)
units(3)
s2 = h.build(APP_HEALTH, None, facts)
s3 = h.build(APP_HEALTH, None, facts)
c1, c2, c3 = (s["services"]["ceravis"] for s in (s1, s2, s3))
check("ceravis: running, uptime from systemd (2 h), restarts, memory",
      c1["state"] == "running" and 7190 <= c1["uptime_secs"] <= 7210 and c1["restarts"] == 2
      and c1["memory_bytes"] == 812345678 and c1["warnings"] == [], c1)
check("a restart since the last heartbeat is reported once, then not again",
      c2["warnings"] == ["ceravis.service restarted 1 time since the last heartbeat"]
      and c3["warnings"] == [], (c2["warnings"], c3["warnings"]))
sv = s1["services"]
check("frpc not installed (a LAN-only home): a state, not a warning",
      sv["frpc"]["state"] == "not_installed" and sv["frpc"]["warnings"] == [], sv["frpc"])
check("the edge_id watcher stopped: warned (a new edge_id would not reach the app)",
      sv["edge_id_watch"]["state"] == "stopped" and sv["edge_id_watch"]["warnings"]
      and sv["edge_id_watch"]["memory_bytes"] is None, sv["edge_id_watch"])
check("a failed timer: warned, with systemd's reason",
      sv["reboot_timer"]["state"] == "failed" and "resources" in sv["reboot_timer"]["warnings"][0],
      sv["reboot_timer"])
check("the fleet agent reports its own version, FMS round trip, interval, enrollment key",
      sv["fleet_agent"]["version"] == agent_health.__version__ and sv["fleet_agent"]["fms_rtt_ms"] == 41.3
      and sv["fleet_agent"]["can_enroll"] is True and sv["fleet_agent"]["state"] == "running",
      sv["fleet_agent"])
check("the app's own services ride along: mediamtx, app_server, internet, tunnel",
      all(sv[k]["state"] == APP_HEALTH["services"][k]["state"] for k in agent_health.APP_SERVICES))
check("status: the worst of all parts, and the number of warnings",
      s1["state"] == "warning" and s1["warning_count"] == sum(
          len(p["warnings"]) for p in [s1["hardware"], s1["backend"], *s1["services"].values(),
                                       *s1["cameras"]]), (s1["state"], s1["warning_count"]))

# --------------------------------------------------------------------------
print("\n3. hardware: the app's checks while it answers, the agent's own when it does not")
real = (collect.disk, agent_health._temperatures)
collect.disk = lambda: {"root": {"total_bytes": 100, "free_bytes": 3, "used_pct": 97.0},
                        "data": {"total_bytes": 100, "free_bytes": 3, "used_pct": 97.0}}
agent_health._temperatures = lambda: {"cpu": 90.5, "tj": 91.2}
os.environ["NTP"] = "no"
down = h.build(None, "cannot reach http://127.0.0.1:8000/api/v1/system/health (Connection refused)", facts)
hw = down["hardware"]
check("app not answering: the agent warns itself — data disk, running hot, clock",
      any("data disk 97.0% full" in w for w in hw["warnings"])
      and any("running hot: 91" in w for w in hw["warnings"])
      and "system clock is not NTP-synced" in hw["warnings"] and hw["state"] == "warning", hw["warnings"])
check("...the backend is DOWN with the reason, cameras unknown (null), app services unknown",
      down["state"] == "down" and down["backend"]["state"] == "down"
      and "Connection refused" in down["backend"]["warnings"][0] and down["cameras"] is None
      and all(down["services"][k]["state"] == "unknown" for k in agent_health.APP_SERVICES), down["backend"])
up = h.build(APP_HEALTH, None, facts)
check("app answering: its hardware checks are used, the agent adds none of the same",
      not any("running hot" in w or "NTP" in w or "data disk" in w for w in up["hardware"]["warnings"]),
      up["hardware"]["warnings"])
check("...and the board's sensors from the app fill the hardware part (GPU, power)",
      up["hardware"]["gpu"]["load_pct"] == 64 and up["hardware"]["power"]["board_w"] == 7.8
      and up["hardware"]["temperature_c"]["cpu"] == 90.5, up["hardware"]["gpu"])
collect.disk, agent_health._temperatures = real
os.environ.pop("NTP")
mem = agent_health._memory()
check("memory, CPU and uptime read from the kernel here",
      (mem["total_bytes"] or 0) > 0 and isinstance(up["hardware"]["cpu"]["cores"], int)
      and up["hardware"]["uptime_secs"] > 0, mem)

# --------------------------------------------------------------------------
print("\n4. the heartbeat against a stand-in FMS")
SECRET = "nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE"           # the SAMPLE device key
EDGE_ID = "ce7k2m9xq4w8n3"


class Server:
    def __init__(self, handler_body) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                raw = json.dumps(APP_HEALTH).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                handler_body(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        outer.seen = []


fms_seen: list[dict] = []
orders = [[{"id": "cmd_81", "kind": "ping"}], []]


def fms_post(req):
    body = req.rfile.read(int(req.headers.get("Content-Length") or 0))
    h_ = req.headers
    message = protocol.request_message(h_.get(protocol.HDR_DEVICE, ""), "POST", req.path, body,
                                       h_.get(protocol.HDR_TIMESTAMP, ""), h_.get(protocol.HDR_NONCE, ""))
    signed = protocol.verify(SECRET, message, h_.get(protocol.HDR_SIGNATURE))
    fms_seen.append({"signed": signed, "path": req.path, "device": h_.get(protocol.HDR_DEVICE),
                     "lines": message.decode().split("\n"), "body": json.loads(body)})
    raw = json.dumps({"server_time": 1793795400.73, "next_poll_after_secs": 60,
                      "commands": orders.pop(0) if orders else []}).encode()
    req.send_response(200)
    req.send_header("Content-Type", "application/json")
    req.send_header(protocol.HDR_SIGNATURE, protocol.sign(
        SECRET, protocol.response_message(h_.get(protocol.HDR_DEVICE, ""), h_.get(protocol.HDR_NONCE, ""), raw)))
    req.end_headers()
    req.wfile.write(raw)


fms = Server(fms_post)
edge_app = Server(lambda req: None)
from fms_agent.agent import Agent                                 # noqa: E402
from fms_agent.config import Config                               # noqa: E402

units(3)
state_dir = box / "state"
identity.save(state_dir, {"edge_id": EDGE_ID, "secret": SECRET, "device_id": "ceravis-acg08mbv3"})
agent = Agent(Config(url=fms.url, enroll_key="k", status_url=edge_app.url + "/api/v1/system/status",
                     state_dir=state_dir, fingerprint="1424622063459",
                     health_url=edge_app.url + "/api/v1/system/health"))
first = agent.beat()
second = agent.beat()
b1, b2 = fms_seen[0], fms_seen[1]
check("the body is exactly {status, results} — no protocol, no agent, no status_error",
      list(b1["body"]) == ["status", "results"], list(b1["body"]))
check("status holds every part: state, warning_count, hardware, backend, services, cameras",
      list(b1["body"]["status"]) == ["state", "warning_count", "hardware", "backend", "services", "cameras"],
      list(b1["body"]["status"]))
check("signed with the DEVICE key over the 6 lines (edge_id, POST, path, sha256, timestamp, nonce)",
      b1["signed"] and b1["path"] == protocol.HEARTBEAT_PATH and b1["device"] == EDGE_ID
      and len(b1["lines"]) == 6 and b1["lines"][0] == EDGE_ID, b1["lines"][:3])
check("the cameras arrive with fps and uptime, as the app measured them",
      [c["fps"] for c in b1["body"]["status"]["cameras"]] == [14.8, 0.0]
      and b1["body"]["status"]["cameras"][0]["uptime_secs"] == 3601)
check("an order in the reply runs, and its answer goes out 1 second later in the next heartbeat",
      first == 1.0 and b2["body"]["results"] and b2["body"]["results"][0]["id"] == "cmd_81"
      and b2["body"]["results"][0]["ok"] and "pong" in b2["body"]["results"][0]["output"],
      b2["body"]["results"])
check("...and once delivered it is not sent again", second == 60.0 and agent.results == [])
check("the heartbeat stays far below the body limit",
      len(transport.encode(b1["body"])) < protocol.MAX_BODY_BYTES // 20, len(transport.encode(b1["body"])))

fms.server.shutdown()
edge_app.server.shutdown()
shutil.rmtree(box, ignore_errors=True)
shutil.rmtree(settings.data_dir, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All heartbeat checks passed.")
