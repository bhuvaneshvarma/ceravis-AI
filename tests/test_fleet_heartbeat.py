"""
The device's health — measured each minute, kept on the device, summarised in the heartbeat.

1. the edge app's GET /api/v1/system/health: every part in detail with one state
   vocabulary (operational / degraded / outage / disabled / unknown) — networks
   (internet + hotspot), AI, cameras with their Wi-Fi signal, the tunnel's live
   connection — built from the SAME warning rules as /api/v1/system/status, whose
   output does not change
2. the agent's summary (the heartbeat body): service names anyone understands,
   GB and whole percentages, systemd states, warnings said until delivered, the
   app-down case (unknowns, last known cameras), unexpected restarts, OOM, outages
3. the local logs: health.jsonl every minute, events.jsonl only on change,
   incoming.jsonl (requests into the device), one rotation rule
4. orders: `info`, `logs` of a log file, the allow-list
5. the heartbeat against a stand-in FMS (real HTTP, real signatures)
6. why the device last started (scheduled / manual / orderly / unexpected)

    python tests/test_fleet_heartbeat.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
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
DATA = Path(tempfile.mkdtemp(prefix="ceravis-health-"))
os.environ["DATA_DIR"] = str(DATA)                  # the agent's logs go here, not edge/data

failures: list[str] = []
VOCAB = {"operational", "degraded", "outage", "disabled", "unknown"}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + str(detail)[:300]) if detail else ''}")
    if not ok:
        failures.append(label)


# --------------------------------------------------------------------------
print("\n1. the edge app's /api/v1/system/health")
from common import clock                                         # noqa: E402
from config.settings import settings                             # noqa: E402

settings.data_dir = str(DATA)
from config.camera_config import CameraConfig                    # noqa: E402
from ingestion.camera_status import CameraHealthState, CameraStatus  # noqa: E402
from schemas.cameras import Camera                               # noqa: E402

cfg = CameraConfig()
cfg.add(Camera(room_name="LOUNGE", rtsp_url="rtsp://10.42.0.11/stream1"))
cfg.add(Camera(room_name="KITCHEN", rtsp_url="rtsp://10.42.0.12/stream1"))
cfg.add(Camera(room_name="BEDROOM", rtsp_url="rtsp://10.42.0.13/stream1", is_enabled=False))
lounge, kitchen, bedroom = [c.camera_id for c in cfg.get_all()]

import api.setup_routes as setup_routes                          # noqa: E402
import api.system_routes as sr                                   # noqa: E402
import common.hotspot as hotspot                                 # noqa: E402
import common.net as net                                         # noqa: E402
import livestream.mediamtx_client as mtx                         # noqa: E402

real_gateway, real_stations, real_radio_run = net.default_gateway, hotspot._stations, hotspot._run
setup_routes.internet_problem = lambda: None
setup_routes.internet_latency_ms = lambda: 42.0
net.wired_uplink = lambda: net.WiredUplink(True, "enP8p1s0", "192.168.0.125")
net.default_gateway = lambda: "192.168.0.1"
net.link_speed_mbps = lambda iface: 1000
SPOT = {"supported": True, "interface": "wlP1p1s0", "on": True, "ssid": "ceravis-acg08mbv3",
        "channel": 36, "power_save": "off",
        "devices": [{"mac": "aa:bb:cc:00:00:11", "ip": "10.42.0.11", "hostname": "C220",
                     "signal_dbm": -52, "signal_avg_dbm": -53, "tx_retries": 10, "tx_failed": 0},
                    {"mac": "aa:bb:cc:00:00:12", "ip": "10.42.0.12", "hostname": "C200",
                     "signal_dbm": -81, "signal_avg_dbm": -80}]}
real_hotspot_health, hotspot.health = hotspot.health, lambda: json.loads(json.dumps(SPOT))
mtx.is_up = lambda: True
mtx.path_info = lambda cid: {"ready": cid == lounge, "readers": [{}] if cid == lounge else []}
mtx.path_stream_info = lambda cid: {"codec": "h264" if cid == lounge else "h265", "profile": "High"}
sr.code_version = lambda: {"commit": "abc1234", "on_disk": "abc1234", "restart_needed": False}
sr._storage_status = lambda: {"disk_used_pct": 40.0, "recordings_gb": 12.5, "retention_hours": 72}
sr._time_status = lambda: {"local": "x", "timezone": "Asia/Kolkata", "ntp_synchronized": True}
TUNNEL = {"installed": True, "edge_id": "ce7k2m9xq4w8n3", "readable": True, "keyed": True,
          "connected": True, "reason": None}
sr._tunnel_status = lambda: dict(TUNNEL)
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


detector = types.SimpleNamespace(is_running=True, last_inference_time=clock.now())
metrics = types.SimpleNamespace(snapshot_all=lambda: [{"name": "detection", "fps": 5.0}])
state = types.SimpleNamespace(system_monitor=Monitor(), recording_controller=RC(),
                              camera_manager=Manager(), gallery=types.SimpleNamespace(size=3),
                              outbox=None, mediamtx=types.SimpleNamespace(uptime_secs=86000.2, restarts=0),
                              detection_runner=detector, metrics_registry=metrics,
                              rule_engine=object())
request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
status = sr.system_status(request)
health = sr.system_health(request)

check("seven parts: backend, hardware, network, setup, ai, services, cameras",
      list(health) == ["backend", "hardware", "network", "setup", "ai", "services", "cameras"], list(health))
parts = [health["backend"], health["hardware"], health["network"], health["setup"], health["ai"],
         *health["services"].values(), *health["cameras"]]
check("EVERY part: a state from the one vocabulary, and a warnings list",
      all(isinstance(p.get("warnings"), list) and p.get("state") in VOCAB for p in parts),
      [p.get("state") for p in parts])
by_id = {c["camera_id"]: c for c in health["cameras"]}
lo, ki, be = by_id[lounge], by_id[kitchen], by_id[bedroom]
check("a healthy camera: operational, fps, resolution, uptime, recording, Wi-Fi signal and link",
      lo["state"] == "operational" and lo["stream"] == "running" and lo["fps"] == 14.8
      and lo["resolution"] == "1920x1080" and lo["uptime_secs"] == 3601
      and lo["recording"]["on"] and lo["wifi"]["signal_dbm"] == -53
      and lo["wifi"]["ip"] == "10.42.0.11" and lo["wifi"]["tx_retries"] == 10 and lo["warnings"] == [], lo)
check("a camera without video: outage, with its reasons and its weak Wi-Fi signal",
      ki["state"] == "outage" and ki["stream"] == "reconnecting"
      and any("no video" in w for w in ki["warnings"]) and any("H265" in w for w in ki["warnings"])
      and any("no segments landing" in w for w in ki["warnings"])
      and any("weak Wi-Fi signal (-80 dBm)" in w for w in ki["warnings"]), ki["warnings"])
check("a camera switched off: disabled, no warnings", be["state"] == "disabled" and be["warnings"] == [], be)
nw = health["network"]
check("network: internet connected with its latency, the uplink, the hotspot and its devices",
      nw["state"] == "operational" and nw["internet"] == {"connected": True, "latency_ms": 42.0}
      and nw["uplink"]["gateway"] == "192.168.0.1" and nw["uplink"]["speed_mbps"] == 1000
      and nw["hotspot"]["on"] and nw["hotspot"]["state"] == "operational"
      and len(nw["hotspot"]["devices"]) == 2, nw)
ai = health["ai"]
check("ai: operational — detector running, analysing, fall detection on, stage rates",
      ai["state"] == "operational" and ai["detector_running"] and ai["fall_detection"]
      and ai["last_analysis_age_secs"] < 5 and ai["pipelines"][0]["name"] == "detection", ai)
svc = health["services"]
check("services: video server, app server, tunnel (live connection), time sync",
      list(svc) == ["mediamtx", "app_server", "tunnel", "time_sync"]
      and svc["mediamtx"]["state"] == "operational" and svc["app_server"]["state"] == "operational"
      and svc["tunnel"]["state"] == "operational" and svc["tunnel"]["connected"] is True
      and svc["time_sync"]["state"] == "operational", svc)
check("hardware: why it last started, tegrastats GPU, temperatures, board power",
      health["hardware"]["last_boot"]["reason"] in ("unknown", "unexpected_restart", "scheduled_reboot",
                                                    "manual_reboot", "system_restart")
      and health["hardware"]["gpu_load_pct"] == 64 and health["hardware"]["board_power_w"] == 7.8,
      health["hardware"])
flat = [w for p in parts for w in p["warnings"]]
check("every /system/status problem is in /system/health, under its part",
      all(p in flat for p in status["problems"]) and len(status["problems"]) == 2, status["problems"])
check("setup: no care recipient assigned yet — said in a line; the cameras added are counted",
      health["setup"] == {"state": "degraded", "care_recipient": "not_assigned", "cameras_added": 3,
                          "warnings": ["no care recipient assigned yet — sign in on this device and assign one"]},
      health["setup"])
real_recipient = sr.account_recipient
sr.account_recipient = lambda: {"recipient_id": "5012"}
assigned = sr._setup_part(state, health["cameras"])
state.gallery.size = 0
not_enrolled = sr._setup_part(state, health["cameras"])
state.gallery.size = 3
bare = sr._setup_part(state, [])
sr.account_recipient = real_recipient
check("setup: assigned / assigned but not enrolled (the AI part warns) / no cameras added yet",
      assigned == {"state": "operational", "care_recipient": "assigned", "cameras_added": 3, "warnings": []}
      and not_enrolled["care_recipient"] == "not_enrolled" and not_enrolled["warnings"] == []
      and bare["cameras_added"] == 0 and bare["warnings"][-1].startswith("no cameras added yet"),
      (assigned, not_enrolled, bare))
check("/system/status is unchanged: the same 2 problems, the same keys (weak Wi-Fi is health-only)",
      status["status"] == "degraded" and set(status) >= {"problems", "cameras", "recording", "storage",
                                                         "cloud", "tunnel", "media_backbone"}
      and not any("Wi-Fi" in p for p in status["problems"]))

detector.is_running = False
check("AI detection stopped: an outage with the reason",
      sr.system_health(request)["ai"]["state"] == "outage")
detector.is_running, detector.last_inference_time = True, clock.now() - timedelta(minutes=5)
stale = sr.system_health(request)["ai"]
check("AI detection stalled while cameras deliver video: an outage",
      stale["state"] == "outage" and "analysed no frame for 5 min" in stale["warnings"][0], stale["warnings"])
detector.last_inference_time = clock.now()
SPOT["on"], SPOT["devices"] = False, []
off = sr.system_health(request)["network"]["hotspot"]
check("hotspot off: an outage, cameras cannot connect", off["state"] == "outage"
      and "hotspot is off" in off["warnings"][0], off)
SPOT.update(on=True, power_save="on")
check("hotspot power saving on: degraded, with what to do",
      sr.system_health(request)["network"]["hotspot"]["state"] == "degraded")
SPOT.update(power_save="off",
            devices=[{"mac": "aa:bb:cc:00:00:11", "ip": "10.42.0.11", "hostname": "C220", "signal_dbm": -52,
                      "signal_avg_dbm": -53, "tx_retries": 10, "tx_failed": 0},
                     {"mac": "aa:bb:cc:00:00:12", "ip": "10.42.0.12", "hostname": "C200",
                      "signal_dbm": -81, "signal_avg_dbm": -80}])
setup_routes.internet_problem = lambda: "Connected to the router, but the app server cannot be reached"
cut = sr.system_health(request)
setup_routes.internet_problem = lambda: None
check("app server unreachable: network outage, and the cloud link says alerts wait on the device",
      cut["network"]["state"] == "outage" and cut["network"]["internet"]["connected"] is False
      and cut["services"]["app_server"]["state"] == "outage"
      and "alerts wait on this device" in cut["services"]["app_server"]["warnings"][0],
      cut["services"]["app_server"])
cases = {"disabled": {"installed": False},
         "unknown": {"installed": True, "edge_id": "e1", "readable": False},
         "outage": {"installed": True, "edge_id": "e1", "readable": True, "keyed": False,
                    "reason": "frpc.toml does not route /e1"}}
lost = sr._tunnel_health({**TUNNEL, "connected": False})
check("tunnel: not installed / unreadable are states; a wrong route or a lost server connection warns",
      all(sr._tunnel_health(t)["state"] == want for want, t in cases.items())
      and lost["state"] == "outage" and "tunnel server" in lost["warnings"][0], lost)

STATION_DUMP = """Station aa:bb:cc:00:00:11 (on wlP1p1s0)
\tinactive time:\t120 ms
\ttx retries:\t12
\ttx failed:\t1
\tsignal:  \t-52 [-52, -55] dBm
\tsignal avg:\t-53 [-53, -56] dBm
\ttx bitrate:\t72.2 MBit/s MCS 7 short GI
\tconnected time:\t3600 seconds
Station aa:bb:cc:00:00:12 (on wlP1p1s0)
\tsignal:  \t-81 dBm
"""
IW_INFO = ("Interface wlP1p1s0\n\tssid ceravis-acg08mbv3\n\ttype AP\n\tchannel 36 (5180 MHz), "
           "width: 80 MHz, center1: 5210 MHz\n\ttxpower 20.00 dBm")
hotspot._run = lambda args, timeout=15.0: (0, STATION_DUMP if "station" in args else
                                           IW_INFO if "info" in args else "Power save: off")
st = hotspot._stations("wlP1p1s0")
rd = hotspot.radio("wlP1p1s0")
hotspot._run = real_radio_run
check("hotspot: each device's signal, retries, bitrate and connected time from `iw station dump`",
      st["aa:bb:cc:00:00:11"] == {"inactive_ms": 120, "tx_retries": 12, "tx_failed": 1, "signal_dbm": -52,
                                  "signal_avg_dbm": -53, "tx_bitrate_mbps": 72.2, "connected_secs": 3600}
      and st["aa:bb:cc:00:00:12"] == {"signal_dbm": -81}, st)
check("hotspot radio: name, channel, frequency, width, power, power saving",
      rd == {"ssid": "ceravis-acg08mbv3", "channel": 36, "frequency_mhz": 5180, "width_mhz": 80,
             "tx_power_dbm": 20.0, "power_save": "off"}, rd)
box = Path(tempfile.mkdtemp(prefix="ceravis-net-"))
(box / "route").write_text("Iface\tDestination\tGateway\tFlags\nenP8p1s0\t00000000\t0100A8C0\t0003\n")
check("default gateway read from the kernel's routing table (little-endian hex)",
      real_gateway(box / "route") == "192.168.0.1", real_gateway(box / "route"))
APP_HEALTH = json.loads(json.dumps(sr.system_health(request), default=str))   # what the agent receives
from api import auth                                              # noqa: E402
check("/api/v1/system/health is readable over loopback, like /system/status",
      auth._matches(auth._LOCAL_READ_RE, "GET", "/api/v1/system/health")
      and not auth._matches(auth._LOCAL_READ_RE, "POST", "/api/v1/system/health"))

# --------------------------------------------------------------------------
print("\n2. the agent's heartbeat summary")
from fms_agent import collect, health as agent_health, identity, transport  # noqa: E402
import fms_protocol as protocol                                   # noqa: E402


class _NoData(type(Path())):
    """A sysfs file whose driver gives nothing: Python's decoder raises TypeError
    (the bench Jetson's thermal zone, 2026-11-12 — it crashed every heartbeat)."""
    def read_text(self, *a, **k):
        raise TypeError("can't concat NoneType to bytes")


_real_path, collect.Path = collect.Path, _NoData
check("a sysfs file that gives no data reads as '' — never a crashed heartbeat",
      collect._read("/sys/class/thermal/thermal_zone9/temp") == "")
collect.Path = _real_path

UNITS = {
    "ceravis.service": ("loaded", "active", "2"), "frpc.service": ("not-found", "inactive", "0"),
    "ceravis-fleet-agent.service": ("loaded", "active", "0"),
    "ceravis-reboot.timer": ("loaded", "failed", ""), "NetworkManager.service": ("loaded", "active", "0"),
    "ssh.service": ("loaded", "active", "0"), "systemd-timesyncd.service": ("loaded", "active", "0"),
    "systemd-resolved.service": ("loaded", "active", "0"), "avahi-daemon.service": ("loaded", "inactive", "0"),
    "nvfancontrol.service": ("loaded", "active", "0"), "systemd-journald.service": ("loaded", "active", "0"),
    "apt-daily-upgrade.timer": ("loaded", "active", ""), "bluetooth.service": ("loaded", "active", "0"),
    "docker.service": ("masked", "inactive", "0"),
}


def fake_systemd(names):
    out = {}
    for n in names:
        load, active, restarts = UNITS.get(n, ("not-found", "inactive", ""))
        out[n] = {"Id": n, "LoadState": load, "ActiveState": active, "SubState": "",
                  "Result": "resources" if active == "failed" else "success",
                  "ActiveEnterTimestampMonotonic": "1", "NRestarts": restarts,
                  "MemoryCurrent": "812345678" if n == "ceravis.service" else "[not set]"}
    return out


GiB = 1024 ** 3
OOM = [5]
BOOT = {"reason": "scheduled_reboot", "at": "2026-11-12T03:21:07+05:30"}
agent_health._systemd = fake_systemd
agent_health._ntp_synchronized = lambda: True
agent_health._memory = lambda: {"total_bytes": 8 * GiB, "available_bytes": 3 * GiB, "used_pct": 62.5,
                                "swap_total_bytes": 4 * GiB, "swap_used_pct": 4.1}
agent_health._temperatures = lambda: {"cpu": 52.1, "tj": 53.0}
agent_health._fan = lambda: {"pct": 40, "rpm": 2150}
agent_health._power_mode = lambda: "25W"
agent_health._read_only = lambda paths: []
agent_health._oom_kills = lambda: OOM[0]
agent_health._last_boot = lambda: dict(BOOT)
agent_health._updates = lambda: {"pending": 3, "security": 1, "reboot_required": False}
collect.disk = lambda: {"root": {"total_bytes": 116 * GiB, "free_bytes": 64 * GiB, "used_pct": 44.8},
                        "data": {"total_bytes": 116 * GiB, "free_bytes": 64 * GiB, "used_pct": 44.8}}
collect._uptime = lambda: 86412.0
facts = agent_health.agent_facts(41.3, 60.0, "the-key")
h = agent_health.Health()
d1 = h.observe(APP_HEALTH, None, facts)
s1 = h.summary(d1)
check("the summary: state, warning_count, hardware, network, services, cameras — nothing else",
      list(s1) == ["state", "warning_count", "hardware", "network", "setup", "services", "cameras"], list(s1))
check("services under names anyone understands, in a fixed order (no edge_id watcher)",
      list(s1["services"]) == [n for n, _, _ in agent_health.SERVICES] and "edge_id_watch" not in s1["services"]
      and list(s1["services"])[:4] == ["edge_application", "mediamtx", "ai_detection", "cloud_connection"],
      list(s1["services"]))
check("every part in the summary: a state from the vocabulary and a warnings list",
      all(p["state"] in VOCAB and isinstance(p["warnings"], list) for p in
          [s1["hardware"], s1["network"], *s1["services"].values(), *s1["cameras"]]))
hw = s1["hardware"]
check("hardware in plain numbers: GB, whole %, hottest °C, fan %, watts, power mode, last boot",
      hw == {"state": "operational", "uptime_secs": 86412, "last_boot": "scheduled_reboot",
             "cpu_pct": hw["cpu_pct"], "gpu_pct": 64, "ram_used_gb": 5.0, "ram_total_gb": 8.0,
             "disk_used_gb": 52.0, "disk_total_gb": 116.0, "temperature_c": 53.0, "fan_pct": 40,
             "power_w": 7.8, "power_mode": "25W", "warnings": []}, hw)
check("no model, OS, boot_id, hostname, edge_id or device_id in the heartbeat",
      not any(k in json.dumps(s1) for k in ('"model"', '"os"', '"boot_id"', '"hostname"', '"edge_id"',
                                            '"device_id"')))
check("network: internet connected, latency, hotspot on, how many devices on it",
      s1["network"] == {"state": "operational", "internet": "connected", "latency_ms": 42.0,
                        "hotspot": "on", "hotspot_devices": 2, "warnings": []}, s1["network"])
sv = s1["services"]
check("edge_application: running and answering, with the release version",
      sv["edge_application"] == {"state": "operational", "version": settings.app_version, "warnings": []},
      sv["edge_application"])
check("frpc disabled on a LAN-only home (frpc not installed): a state, not a warning",
      sv["frpc"] == {"state": "disabled", "warnings": []}, sv["frpc"])
check("nightly_reboot: a failed timer is an outage with systemd's reason",
      sv["nightly_reboot"]["state"] == "outage" and "resources" in sv["nightly_reboot"]["warnings"][0],
      sv["nightly_reboot"])
check("edge_hotspot = NetworkManager running AND the hotspot on",
      sv["edge_hotspot"]["state"] == "operational", sv["edge_hotspot"])
check("an optional service that is off (local discovery) is disabled, not an outage",
      sv["local_discovery"]["state"] == "disabled" and sv["local_discovery"]["warnings"] == [])
check("os_hardening: a switched-off service running again is drift (bluetooth)",
      sv["os_hardening"]["state"] == "degraded" and "bluetooth" in sv["os_hardening"]["warnings"][0],
      sv["os_hardening"])
check("cloud_connection carries the uploads waiting; security_updates the security updates waiting",
      sv["cloud_connection"]["pending_uploads"] == 0 and sv["security_updates"]["pending_security"] == 1)
cams = {c["camera_id"]: c for c in s1["cameras"]}
check("setup in the heartbeat: which devices run without a care recipient (or cameras)",
      s1["setup"] == {"state": "degraded", "care_recipient": "not_assigned", "cameras_added": 3,
                      "warnings": ["no care recipient assigned yet — sign in on this device and assign one"]},
      s1["setup"])
failed = h.summary(h.observe(APP_HEALTH, None, agent_health.agent_facts(41.3, 60.0, "k", "HTTP 401: bad_signature")))
stopped = h.summary(h.observe(APP_HEALTH, None, agent_health.agent_facts(41.3, 60.0, "k", "reply signature invalid",
                                                                          stopped=True)))
check("the last check-in's problem rides in the next heartbeat (services.fleet_agent)",
      failed["services"]["fleet_agent"]["state"] == "degraded"
      and failed["services"]["fleet_agent"]["warnings"] == ["the last check-in failed: HTTP 401: bad_signature"]
      and stopped["services"]["fleet_agent"]["state"] == "outage"
      and stopped["services"]["fleet_agent"]["warnings"] == ["stopped checking in: reply signature invalid"],
      (failed["services"]["fleet_agent"], stopped["services"]["fleet_agent"]))
check("cameras: id, room, state, fps, reconnects, recording, night vision, PTZ, Wi-Fi signal — nothing else",
      list(cams[lounge]) == ["camera_id", "room", "state", "fps", "reconnects", "recording", "night_vision",
                             "ptz", "wifi_signal_dbm", "warnings"]
      and cams[lounge]["wifi_signal_dbm"] == -53 and cams[kitchen]["state"] == "outage"
      and cams[bedroom]["state"] == "disabled", cams[lounge])
check("device state: degraded (warnings, the watch still works); the count adds every part's warnings",
      s1["state"] == "degraded" and s1["warning_count"] == sum(
          len(p["warnings"]) for p in [s1["hardware"], s1["network"], s1["setup"], *s1["services"].values(),
                                       *s1["cameras"]]), (s1["state"], s1["warning_count"]))
UNITS["ceravis.service"] = ("loaded", "active", "3")
s2 = h.summary(h.observe(APP_HEALTH, None, facts))
d3 = h.observe(APP_HEALTH, None, facts)
s3 = h.summary(d3)
h.delivered(d3)
s4 = h.summary(h.observe(APP_HEALTH, None, facts))
check("a restart is said until a heartbeat carrying it is delivered, then not again",
      s2["services"]["edge_application"]["warnings"] == ["ceravis.service restarted 1 time since the last heartbeat"]
      and s3["services"]["edge_application"]["warnings"] == s2["services"]["edge_application"]["warnings"]
      and s4["services"]["edge_application"]["warnings"] == [],
      (s2["services"]["edge_application"], s4["services"]["edge_application"]))
OOM[0] = 7
s5 = h.summary(h.observe(APP_HEALTH, None, facts))
check("processes stopped for lack of memory between two looks: said until delivered",
      any("2 processes were stopped for lack of memory" in w for w in s5["hardware"]["warnings"]),
      s5["hardware"]["warnings"])
offline = json.loads(json.dumps(APP_HEALTH))
offline["network"].update(state="outage", internet={"connected": False, "latency_ms": None},
                          warnings=["No internet: connect an Ethernet cable"])
d6 = h.observe(offline, None, facts)
s6 = h.summary(d6)
s7 = h.summary(h.observe(APP_HEALTH, None, facts))
check("no internet: network outage, disconnected — and once back, when it was lost and for how long",
      s6["network"]["state"] == "outage" and s6["network"]["internet"] == "disconnected"
      and s7["network"]["state"] == "degraded"
      and any("the internet connection was lost from" in w for w in s7["network"]["warnings"]),
      s7["network"])
BOOT.update(reason="unexpected_restart")
collect._uptime = lambda: 300.0
s8 = h.summary(h.observe(APP_HEALTH, None, facts))
check("an unexpected restart (power cut / crash) is reported for 15 minutes after it",
      s8["hardware"]["last_boot"] == "unexpected_restart"
      and any("restarted unexpectedly 5 min ago" in w for w in s8["hardware"]["warnings"]))
BOOT.update(reason="scheduled_reboot")
collect._uptime = lambda: 86412.0
down = h.summary(h.observe(None, "cannot reach http://127.0.0.1:8000/api/v1/system/health "
                                 "(Connection refused)", facts))
dsv = down["services"]
check("app not answering: OUTAGE — the reason under edge_application",
      down["state"] == "outage" and dsv["edge_application"]["state"] == "outage"
      and "not answering" in dsv["edge_application"]["warnings"][-1], dsv["edge_application"])
check("...what only the app can see is unknown: video server, AI, cloud, network",
      all(dsv[k]["state"] == "unknown" for k in ("mediamtx", "ai_detection", "cloud_connection"))
      and down["network"]["state"] == "unknown" and down["network"]["hotspot"] == "unknown")
check("...setup is unknown too (only the app knows the care recipient and cameras)",
      down["setup"] == {"state": "unknown", "care_recipient": "unknown", "cameras_added": None, "warnings": []},
      down["setup"])
check("...the cameras stay listed (last known), each unknown — tiles grey, not gone",
      [c["camera_id"] for c in down["cameras"]] == [lounge, kitchen, bedroom]
      and all(c["state"] == "unknown" for c in down["cameras"]), down["cameras"])
check("...the systemd services are still measured (NetworkManager, SSH, …)",
      dsv["edge_hotspot"]["state"] == "operational" and dsv["ssh_access"]["state"] == "operational")
asked: list[int] = []
agent_health._ntp_synchronized = lambda: asked.append(1) or False
up_again = h.observe(APP_HEALTH, None, facts)
down2 = h.summary(h.observe(None, "not answering", facts))
agent_health._ntp_synchronized = lambda: True
check("timedatectl runs only while the app is not answering (the app reports the clock otherwise)",
      len(asked) == 1 and up_again["hardware"]["clock"]["ntp_synchronized"] is None
      and "system clock is not NTP-synced" in down2["services"]["time_sync"]["warnings"],
      (asked, down2["services"]["time_sync"]))
size = len(transport.encode({"status": s1, "results": []}))
check("the heartbeat is small: under 3 KB for three cameras and 16 services", size < 3072, size)

# --------------------------------------------------------------------------
print("\n3. the local logs")
from common.logbook import Logbook, log_dir                       # noqa: E402
from fms_agent.logs import DeviceLogs                             # noqa: E402

logs = DeviceLogs(DATA)
logs.observation(d1, s1)
joined = json.loads(json.dumps(d6))
joined["app"]["network"]["hotspot"]["devices"].append({"mac": "aa:bb:cc:00:00:99", "ip": "10.42.0.99",
                                                       "hostname": "phone", "signal_dbm": -60})
logs.observation(joined, s6)
health_rows = [json.loads(r) for r in logs.health.tail(10)]
event_rows = [json.loads(r) for r in logs.events.tail(100)]
kinds = [e["event"] for e in event_rows]
check("health.jsonl: one full record per look — the detail AND the summary sent",
      len(health_rows) == 2 and health_rows[0]["kind"] == "snapshot" and "units" in health_rows[0]
      and health_rows[0]["summary"]["state"] == s1["state"] and health_rows[0]["ts"], list(health_rows[0]))
check("events.jsonl: the agent's start, then only what changed",
      kinds[0] == "agent_started" and "state_changed" in kinds and "warning_raised" in kinds, kinds)
check("...a part's state change says from what to what (network operational -> outage)",
      any(e["event"] == "state_changed" and e["part"] == "network" and e["from"] == "operational"
          and e["to"] == "outage" for e in event_rows))
check("...a device joining the hotspot is an event (mac, ip, signal)",
      any(e["event"] == "hotspot_device_joined" and e["mac"] == "aa:bb:cc:00:00:99" for e in event_rows))
check("all logs live in ONE folder: <data>/logs", logs.health.path.parent == log_dir(DATA)
      and logs.events.path.parent == logs.fleet.path.parent == log_dir(DATA))
book = Logbook(DATA / "rot" / "x.jsonl", max_bytes=200, backups=2)
for i in range(40):
    book.write({"n": i, "pad": "x" * 30})
names = sorted(p.name for p in (DATA / "rot").iterdir())
newest = [json.loads(r)["n"] for r in book.tail(5)]
check("one rotation rule: past the size the file moves to .1, .2 — never more than kept",
      names == ["x.jsonl", "x.jsonl.1", "x.jsonl.2"] and newest == [35, 36, 37, 38, 39], (names, newest))
from monitoring.request_log import RequestLog                     # noqa: E402

inbound = Logbook(DATA / "in" / "incoming.jsonl", max_bytes=10 ** 6, backups=1)
rl = RequestLog(inbound)
for _ in range(5):
    rl.record("GET", "/api/v1/system/status", 200, 3.0, "lan", "192.168.0.9", [])
rl.record("POST", "/api/v1/system/reboot", 401, 12.0, "fleet", "127.0.0.1", ["edge_id"])
rl.record("GET", "/api/v1/cameras", 500, 8.0, "lan", "192.168.0.9", [])
rl.flush(force=True)
rows = [json.loads(r) for r in inbound.tail(10)]
check("incoming.jsonl: writes and failures one line each (names of query parameters only)",
      [r["kind"] for r in rows] == ["request", "request", "polling"]
      and rows[0]["status"] == 401 and rows[0]["origin"] == "fleet" and rows[0]["query"] == ["edge_id"]
      and "body" not in rows[0], rows[:2])
check("...successful reads counted per path in one polling line",
      rows[2]["requests"] == {"GET /api/v1/system/status": 5}, rows[2])

# --------------------------------------------------------------------------
print("\n4. orders")
check("`info` and `logs` of a log file are allowed",
      protocol.normalize_command("info") == {}
      and protocol.normalize_command("logs", {"file": "events", "lines": 50}) == {"file": "events", "lines": 50}
      and protocol.normalize_command("logs", {"unit": "ceravis"}) == {"unit": "ceravis", "lines": 200})
refused = []
for bad in ({"file": "cloud_wire"}, {"file": "talkback"}, {"file": "../../etc/shadow"}, {"unit": "sshd"}):
    try:
        protocol.normalize_command("logs", bad)
    except ValueError:
        refused.append(bad)
check("...the wire and talk-back logs (personal data), any path and any other unit are refused",
      len(refused) == 4, refused)

# --------------------------------------------------------------------------
print("\n5. the heartbeat against a stand-in FMS")
SECRET = "nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE"           # the SAMPLE enroll-reply secret
ENROLL_SAMPLE = "SAMPLE-enroll-key-NOT-REAL-0123456789abcdef"     # docs/fleet-enrollment-api.md's SAMPLE key
HB_KEY = protocol.heartbeat_key(ENROLL_SAMPLE, SECRET)
check("heartbeat key = hex(HMAC-SHA256(fleet enrollment key, secret)) — the FMS's own formula",
      HB_KEY == hmac.new(ENROLL_SAMPLE.encode(), SECRET.encode(), hashlib.sha256).hexdigest()
      and len(HB_KEY) == 64, HB_KEY)
EDGE_ID = "ce7k2m9xq4w8n3"


class Server:
    def __init__(self, handler_body) -> None:
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


fms_seen: list[dict] = []
orders = [[{"id": "cmd_81", "kind": "ping"}, {"id": "cmd_82", "kind": "info"},
           {"id": "cmd_83", "kind": "logs", "args": {"file": "events", "lines": 5}}], []]


def fms_post(req):
    body = req.rfile.read(int(req.headers.get("Content-Length") or 0))
    h_ = req.headers
    message = protocol.request_message("POST", req.path, h_.get(protocol.HDR_TIMESTAMP, ""),
                                       h_.get(protocol.HDR_NONCE, ""), body)
    # The FMS's side, written from the spec alone (as the Java team will): the
    # enrollment call's lines plus the nonce, one Base64 of the HMAC.
    by_hand = "\n".join(("POST", req.path, h_.get(protocol.HDR_TIMESTAMP, ""), h_.get(protocol.HDR_NONCE, ""),
                         hashlib.sha256(body).hexdigest())).encode()
    independent = base64.b64encode(hmac.new(HB_KEY.encode(), by_hand, hashlib.sha256).digest()).decode()
    fms_seen.append({"signed": protocol.verify(HB_KEY, message, h_.get(protocol.HDR_SIGNATURE))
                     and independent == h_.get(protocol.HDR_SIGNATURE),
                     "path": req.path, "device": h_.get(protocol.HDR_DEVICE),
                     "lines": message.decode().split("\n"), "body": json.loads(body)})
    raw = json.dumps({"server_time": 1793795400.73, "next_poll_after_secs": 60,
                      "commands": orders.pop(0) if orders else []}).encode()
    req.send_response(200)
    req.send_header("Content-Type", "application/json")
    req.send_header(protocol.HDR_SIGNATURE, protocol.sign(
        HB_KEY, protocol.response_message(h_.get(protocol.HDR_TIMESTAMP, ""), h_.get(protocol.HDR_NONCE, ""),
                                          raw)))
    req.end_headers()
    req.wfile.write(raw)


fms = Server(fms_post)
edge_app = Server(lambda req: None)
from fms_agent.agent import Agent, Halted                         # noqa: E402
from fms_agent.config import Config                               # noqa: E402

state_dir = DATA / "state"
identity.save(state_dir, {"edge_id": EDGE_ID, "heartbeat_key": HB_KEY, "device_id": "ceravis-acg08mbv3"})
agent = Agent(Config(url=fms.url, enroll_key="k", status_url=edge_app.url + "/api/v1/system/status",
                     state_dir=state_dir, fingerprint="1424622063459",
                     health_url=edge_app.url + "/api/v1/system/health"))
first = agent.beat()
second = agent.beat()
b1, b2 = fms_seen[0], fms_seen[1]
check("the body is exactly {status, results}", list(b1["body"]) == ["status", "results"], list(b1["body"]))
check("status: state, warning_count, hardware, network, setup, services, cameras",
      list(b1["body"]["status"]) == ["state", "warning_count", "hardware", "network", "setup", "services", "cameras"],
      list(b1["body"]["status"]))
check("signed with the DEVICE key over the enrollment call's lines plus the nonce: "
      "POST, path, timestamp, nonce, sha256 of the raw body",
      b1["signed"] and b1["path"] == protocol.HEARTBEAT_PATH and b1["device"] == EDGE_ID
      and b1["lines"][:2] == ["POST", protocol.HEARTBEAT_PATH] and len(b1["lines"]) == 5
      and b1["lines"][4] == hashlib.sha256(transport.encode(b1["body"])).hexdigest(), b1["lines"])
check("the reply is verified over RESPONSE, the request's timestamp and nonce, sha256 of the raw reply",
      protocol.response_message("1.0", "ab", b"{}").decode().split("\n")[:3] == ["RESPONSE", "1.0", "ab"])
check("the cameras arrive with fps and Wi-Fi signal, as the app measured them",
      [c["fps"] for c in b1["body"]["status"]["cameras"]] == [14.8, 0.0, None]
      and b1["body"]["status"]["cameras"][0]["wifi_signal_dbm"] == -53, b1["body"]["status"]["cameras"])
res = {r["id"]: r for r in b2["body"]["results"]}
check("orders in the reply run, and their answers go out 1 second later in the next heartbeat",
      first == 1.0 and res["cmd_81"]["ok"] and "pong" in res["cmd_81"]["output"], b2["body"]["results"])
info = json.loads(res["cmd_82"]["output"]) if res.get("cmd_82", {}).get("ok") else {}
check("`info`: release, agent version, board, OS — what is installed",
      info.get("agent_version") == agent_health.__version__ and "edge_version" in info and "model" in info,
      res.get("cmd_82"))
check("`logs` of a log file: the device's own events, newest last",
      res["cmd_83"]["ok"] and "agent_started" in res["cmd_83"]["output"], res["cmd_83"]["output"][-200:])
check("...and once delivered they are not sent again", second == 60.0 and agent.results == [])
agent.problem = "HTTP 502: Bad Gateway"                    # as the loop records a failed check-in
agent.beat()                                               # sent on the record of a moment ago
carried = fms_seen[-1]["body"]["status"]["services"]["fleet_agent"]
check("a check-in sent on a reused record still carries the agent's problem as it is now",
      carried["warnings"] == ["the last check-in failed: HTTP 502: Bad Gateway"]
      and carried["state"] == "degraded", carried)
check("identity.json keeps the heartbeat key, never the enrollment's secret",
      (identity.load(state_dir) or {}).get("heartbeat_key") == HB_KEY
      and "secret" not in (identity.load(state_dir) or {}))
fleet_rows = [json.loads(r) for r in agent.logs.fleet.tail(20)]
check("fleet.jsonl: every check-in and every order, with its outcome",
      [r["kind"] for r in fleet_rows].count("heartbeat") == 3
      and {r.get("order") for r in fleet_rows if r["kind"] == "order"} == {"ping", "info", "logs"},
      fleet_rows[:2])
from fms_agent.agent import _logged                               # noqa: E402

beats = [r for r in fleet_rows if r["kind"] == "heartbeat"]
check("...each check-in in full: the request body exactly as the FMS received it, and its whole reply",
      beats[0]["request"] == b1["body"] and beats[1]["request"] == _logged(b2["body"])
      and [c["id"] for c in beats[0]["reply"]["commands"]] == ["cmd_81", "cmd_82", "cmd_83"]
      and beats[1]["reply"] == {"server_time": 1793795400.73, "next_poll_after_secs": 60, "commands": []},
      (beats[0].get("reply"), beats[1].get("reply")))
long_run = {"status": {}, "results": [{"id": "cmd_9", "ok": True, "output": "x" * 4000 + "END"}]}
kept = _logged(long_run)["results"][0]["output"]
check("...an order's long output is cut to its end in that copy only (the FMS got it whole)",
      kept.endswith("END") and len(kept) < 2100 and "4003 characters sent" in kept
      and long_run["results"][0]["output"] == "x" * 4000 + "END", kept[:60])
fms.server.shutdown()
edge_app.server.shutdown()

# --------------------------------------------------------------------------
print("\n5b. never a loop: refused check-ins wait, untrusted enrollments stop (the bench, 2026-11-13)")
# The FMS that flooded: every heartbeat 401 bad_signature, every enrollment
# accepted with a brand-new edge_id — and, when asked, an enroll reply signed wrongly.
ENROLL_KEY = "k"
calls = {"enroll": 0, "heartbeat": 0}
flood_mode = {"bad_enroll_signature": False}


def flood_fms(req):
    req.rfile.read(int(req.headers.get("Content-Length") or 0))          # consume the request
    if req.path == protocol.ENROLL_PATH:
        calls["enroll"] += 1
        raw = json.dumps({"result": "enrolled", "edge_id": f"loop{calls['enroll']:04d}edge",
                          "secret": f"secret-{calls['enroll']}", "warnings": [], "config": None}).encode()
        key = "not-the-fleet-key" if flood_mode["bad_enroll_signature"] else ENROLL_KEY
        req.send_response(200)
        req.send_header(protocol.HDR_SIGNATURE, protocol.sign(
            key, protocol.enroll_response_message(req.headers.get(protocol.HDR_TIMESTAMP, ""), raw)))
    else:
        calls["heartbeat"] += 1
        raw = b'{"success":false,"error":"bad_signature"}'
        req.send_response(401)
    req.send_header("Content-Type", "application/json")
    req.end_headers()
    req.wfile.write(raw)


flood = Server(flood_fms)
health_app = Server(lambda req: None)


def flood_agent(ident: dict | None, enroll_key: str = ENROLL_KEY) -> Agent:
    sd = Path(tempfile.mkdtemp(prefix="ceravis-flood-", dir=DATA))
    if ident is not None:
        identity.save(sd, {"edge_id": EDGE_ID, "device_id": "ceravis-acg08mbv3", **ident})
    a = Agent(Config(url=flood.url, enroll_key=enroll_key, status_url=health_app.url + "/s", state_dir=sd,
                     fingerprint="1424622063459", health_url=health_app.url + "/api/v1/system/health"))
    a.fingerprint = "0"                                   # no start-up stagger in run_forever
    return a


def run_for(a: Agent, secs: float) -> list[dict]:
    """The agent's own loop for `secs`; the events it wrote meanwhile."""
    before = len(a.logs.events.tail(10_000))
    runner = threading.Thread(target=a.run_forever, daemon=True)
    runner.start()
    a.stop.wait(secs)
    a.stop.set()
    runner.join(5)
    return [json.loads(r) for r in a.logs.events.tail(10_000)[before:]]


refused = flood_agent({"heartbeat_key": "k" * 64})
waited = None
try:
    refused.beat()
except transport.Rejected as exc:
    waited = exc
check("a refused heartbeat (401) is never answered with an enrollment",
      waited is not None and waited.status == 401 and calls == {"enroll": 0, "heartbeat": 1}, calls)
row = json.loads(refused.logs.fleet.tail(1)[0])
check("...and fleet.jsonl keeps that check-in whole: the request as sent, the server's answer",
      row["kind"] == "heartbeat" and row["ok"] is False and row["status"] == 401
      and list(row["request"]) == ["status", "results"]
      and row["reply"] == {"success": False, "error": "bad_signature"}, row.get("reply"))

calls.update(enroll=0, heartbeat=0)
looping = flood_agent({"heartbeat_key": "k" * 64})
events = run_for(looping, 3.0)                             # the 0.7.0 agent enrolled ~3 times here
looping.observe()                                          # what the next heartbeat carries
fa = looping.status["services"]["fleet_agent"]
check("the agent's own loop against that FMS: no enrollment at all — one refused heartbeat, then back-off",
      calls == {"enroll": 0, "heartbeat": 1}, calls)
check("...the refusal is logged (events.jsonl) and rides in the next heartbeat (services.fleet_agent)",
      any(e["event"] == "fleet_check_in_failed" and "bad_signature" in e.get("problem", "") for e in events)
      and fa["state"] == "degraded" and fa["warnings"] == ["the last check-in failed: HTTP 401: bad_signature"],
      (events, fa))

older = flood_agent({"secret": SECRET})
saved = json.loads((older.cfg.state_dir / "identity.json").read_text(encoding="utf-8"))
check("an identity from an older agent: its heartbeat key is derived once, the secret dropped (file too)",
      older.identity["heartbeat_key"] == protocol.heartbeat_key(ENROLL_KEY, SECRET)
      and "secret" not in older.identity and "secret" not in saved
      and saved["heartbeat_key"] == older.identity["heartbeat_key"], sorted(saved))
calls.update(enroll=0, heartbeat=0)
keyless = flood_agent({"secret": SECRET}, enroll_key="")
halted = None
try:
    keyless.beat()
except Halted as exc:
    halted = exc
check("...without the fleet enrollment key to derive it: check-ins stop with the reason, nothing sent",
      halted is not None and "no heartbeat key" in str(halted) and calls == {"enroll": 0, "heartbeat": 0},
      (str(halted), calls))

calls.update(enroll=0, heartbeat=0)
flood_mode["bad_enroll_signature"] = True
fresh = flood_agent(None)                                    # not enrolled yet
events = run_for(fresh, 4.0)
flood_mode["bad_enroll_signature"] = False
check("an enrollment reply that cannot be trusted STOPS check-ins: one enrollment call, never a second",
      calls == {"enroll": 1, "heartbeat": 0} and fresh.identity is None
      and "signature invalid" in (fresh.halted or ""), (calls, fresh.halted))
check("...said once and loudly (events.jsonl), carried as an outage of services.fleet_agent, health kept",
      [e["event"] for e in events].count("fleet_check_in_stopped") == 1
      and fresh.status["services"]["fleet_agent"]["state"] == "outage", events)
logged = [json.loads(r) for r in fresh.logs.fleet.tail(50)]
check("...and an enrollment's answer is never kept in the log (it carries the secret)",
      not any("secret-" in json.dumps(r) for r in logged)
      and all("reply" not in r for r in logged if r["kind"] == "enroll"), logged[-1:])
flood.server.shutdown()
health_app.server.shutdown()

# --------------------------------------------------------------------------
print("\n6. why the device last started")
from maintenance import reboot                                    # noqa: E402

boot_id = ["boot-A"]
reboot._read = lambda path: boot_id[0] if path.endswith("boot_id") else "86400.0 1.0"
reboot._BOOT_FILE.unlink(missing_ok=True)
reboot._MARKER_FILE.unlink(missing_ok=True)
reasons = []
for step in ("first", "same boot", "no clean stop", "clean stop", "nightly", "manual"):
    if step == "no clean stop":
        boot_id[0] = "boot-B"
    elif step == "clean stop":
        reboot.mark_clean_stop()
        boot_id[0] = "boot-C"
    elif step == "nightly":
        reboot._write_marker("scheduled nightly", "systemd timer")
        boot_id[0] = "boot-D"
    elif step == "manual":
        reboot._write_marker("manual", "carer")
        boot_id[0] = "boot-E"
    reboot.boot_report()
    reasons.append(reboot.last_boot()["reason"])
check("first start: unknown; an app restart keeps the boot's answer",
      reasons[:2] == ["unknown", "unknown"], reasons)
check("no orderly stop and no marker: unexpected_restart (power cut / crash)",
      reasons[2] == "unexpected_restart", reasons)
check("an orderly stop outside CERAVIS: system_restart", reasons[3] == "system_restart", reasons)
check("the nightly reboot / a person's reboot: scheduled_reboot / manual_reboot",
      reasons[4:] == ["scheduled_reboot", "manual_reboot"], reasons)

shutil.rmtree(DATA, ignore_errors=True)
shutil.rmtree(box, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All heartbeat checks passed.")
