"""Every part of this device, measured each minute — at two detail levels.

    Health.observe()    the DETAIL: everything measured, kept on the device in
                        logs/health.jsonl (edge/LOGS.md)
    Health.summary()    the HEARTBEAT: each part's state, the few numbers worth a
                        graph, and its warnings (docs/fleet-heartbeat-api.md)
    Health.delivered()  the FMS accepted a heartbeat: warnings that are said once
                        ("restarted since the last heartbeat", "the internet was
                        lost from … to …") are done

One vocabulary for the state of every part:
    operational  working normally            degraded  working, with a warning
    outage       not working                 disabled  off on purpose / not installed
    unknown      cannot be checked right now (e.g. the app is not answering)

Two sources, each reporting only what it can really see:

  • the agent itself (this file): the HARDWARE, read straight from the kernel
    (/proc, /sys, statvfs), and the SYSTEMD SERVICES (systemctl show). Both
    still report when the edge app is down — when they matter most.
  • the edge app's GET /api/v1/system/health: the app, its AI, the video
    server, the cloud link, the tunnel, the networks (internet and hotspot),
    every camera and the board sensors it reads (tegrastats).

Where both could judge the same thing (disk, temperature, clock), the app's
rule is used while it answers; the agent's own check is the fallback when it
does not, so the device is never reported blind. Standard library only.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import time
from pathlib import Path

from . import __version__, collect

collect.use_edge_common()
from common import clock                    # noqa: E402  the edge's one clock

OPERATIONAL, DEGRADED, OUTAGE, DISABLED, UNKNOWN = (
    "operational", "degraded", "outage", "disabled", "unknown")
_SEVERITY = {DISABLED: 0, OPERATIONAL: 1, UNKNOWN: 2, DEGRADED: 3, OUTAGE: 4}

# The services the heartbeat reports, in its order, by the name it reports them
# under. `unit` = the systemd unit checked (None: the app reports it alone).
# kind: "service" must run · "timer" must be scheduled · "optional" may be off.
SERVICES = (
    # name               unit                           kind
    ("edge_application", "ceravis.service",             "service"),
    ("mediamtx",         None,                          ""),
    ("ai_detection",     None,                          ""),
    ("cloud_connection", None,                          ""),
    ("frpc",             "frpc.service",                "service"),
    ("fleet_agent",      "ceravis-fleet-agent.service", "service"),
    ("nightly_reboot",   "ceravis-reboot.timer",        "timer"),
    ("edge_hotspot",     "NetworkManager.service",      "service"),
    ("ssh_access",       "ssh.service",                 "service"),
    ("time_sync",        "systemd-timesyncd.service",   "service"),
    ("dns_resolver",     "systemd-resolved.service",    "service"),
    ("local_discovery",  "avahi-daemon.service",        "optional"),
    ("fan_control",      "nvfancontrol.service",        "service"),
    ("system_logging",   "systemd-journald.service",    "service"),
    ("security_updates", "apt-daily-upgrade.timer",     "timer"),
    ("os_hardening",     None,                          ""),
)
# Recorded in the detail only (logs/health.jsonl), never in the heartbeat.
DETAIL_UNITS = ("ceravis-edge-id.path", "ceravis-reboot.service", "ceravis-firstboot.service",
                "wpa_supplicant.service", "nv-l4t-usb-device-mode.service", "nvpmodel.service",
                "nvzramconfig.service", "unattended-upgrades.service")
# Switched off for good by setup/tune_os.sh; one coming back on is drift.
SWITCHED_OFF = ("bluetooth.service", "docker.service", "containerd.service", "snapd.service",
                "cups.service", "ModemManager.service", "whoopsie.service", "apport.service",
                "kerneloops.service", "packagekit.service", "fwupd.service",
                "nvargus-daemon.service")
# The parts that make the whole device an outage: it cannot watch over the person.
CRITICAL = ("edge_application", "mediamtx", "ai_detection", "edge_hotspot")

MEMORY_WARN_PCT = 90.0
DISK_WARN_PCT = 90.0
RECENT_BOOT_SECS = 900          # an unexpected restart is reported for 15 min after it
GB = 1024 ** 3


def _thermal_warn_c() -> float:
    try:                                   # the edge app's own setting (jetson.env)
        return float(os.environ.get("THERMAL_WARN_C") or 85.0)
    except ValueError:
        return 85.0


def worst(*states: str) -> str:
    return max(states, key=lambda s: _SEVERITY.get(s, 2))


class Health:
    """Keeps what it needs between minutes: the CPU counters (load is the
    average since the last look, not one instant), each unit's restart count at
    the last delivered heartbeat, the out-of-memory counter, and when the
    internet went — so each is said once, at the right time."""

    def __init__(self) -> None:
        self._cpu_prev: tuple[int, int] | None = None
        self._baseline: dict[str, int] = {}          # unit -> restarts at the last delivery
        self._notices: list[tuple[str, str]] = []    # (part, sentence) said until delivered
        self._sent: list[tuple[str, str]] = []
        self._oom_prev: int | None = None
        self._offline_since: tuple[float, str] | None = None   # (monotonic, "HH:MM")
        self._cameras_known: list[dict] = []

    # ---- the detail ---------------------------------------------------------
    def observe(self, app: dict | None, app_error: str | None, agent: dict) -> dict:
        answered = isinstance(app, dict) and "backend" in app
        names = [u for _, u, _ in SERVICES if u] + list(DETAIL_UNITS) + list(SWITCHED_OFF)
        detail = {
            "kind": "snapshot",
            "hardware": self._hardware(),
            "units": {u: _unit_detail(p) for u, p in _systemd(names).items()},
            "updates": _updates(),
            "versions": _versions(),
            "agent": agent,
            "app": app if answered else None,
            "app_error": None if answered else (app_error or "the CERAVIS app is not answering"),
        }
        self._track(detail)
        return detail

    def _hardware(self) -> dict:
        l4t = collect._read("/etc/nv_tegra_release")
        return {
            "model": collect.hardware()["model"],
            "hostname": socket.gethostname(),
            "boot_id": collect._read("/proc/sys/kernel/random/boot_id"),
            "uptime_secs": round(collect._uptime()),
            "last_boot": _last_boot(),
            "os": {"name": collect._os_name(), "kernel": platform.release(),
                   "l4t": l4t.splitlines()[0].lstrip("# ").strip() if l4t else "",
                   "python": platform.python_version()},
            "cpu": {"cores": os.cpu_count(), "load_pct": self._cpu_load_pct(),
                    "load_avg": _load_avg()},
            "memory": _memory(),
            "temperature_c": _temperatures(),
            "fan": _fan(),
            "power_mode": _power_mode(),
            "disk": collect.disk(),
            "read_only": _read_only(["/", str(collect.data_dir())]),
            "oom_kills": _oom_kills(),
            "journal_mb": _journal_mb(),
            "clock": {"unix_time": round(time.time(), 3), "ntp_synchronized": _ntp_synchronized(),
                      "timezone": clock.now().tzname()},
        }

    def _track(self, detail: dict) -> None:
        """What happened BETWEEN two looks becomes a notice, said until delivered."""
        at = f"{clock.now():%H:%M}"
        oom = detail["hardware"]["oom_kills"]
        if oom is not None and self._oom_prev is not None and oom > self._oom_prev:
            n = oom - self._oom_prev
            self._notices.append(("hardware", f"{n} process{'es were' if n > 1 else ' was'} "
                                              f"stopped for lack of memory at {at}"))
        if oom is not None:
            self._oom_prev = oom
        internet = ((detail["app"] or {}).get("network") or {}).get("internet") or {}
        connected = internet.get("connected")
        if connected is False and self._offline_since is None:
            self._offline_since = (time.monotonic(), at)
        elif connected and self._offline_since is not None:
            (began, since), self._offline_since = self._offline_since, None
            self._notices.append(("network", f"the internet connection was lost from {since} "
                                             f"to {at} ({_duration(time.monotonic() - began)})"))

    def _cpu_load_pct(self) -> float | None:
        now = _cpu_counters()
        if now is None:
            return None
        if self._cpu_prev is None:                         # first look: a short sample
            time.sleep(0.25)
            self._cpu_prev, now = now, _cpu_counters() or now
        (busy0, total0), (busy1, total1) = self._cpu_prev, now
        self._cpu_prev = now
        span = total1 - total0
        return round(100.0 * (busy1 - busy0) / span, 1) if span > 0 else None

    # ---- the heartbeat ------------------------------------------------------
    def summary(self, d: dict) -> dict:
        app = d["app"] or {}
        answered = d["app"] is not None
        hardware = self._hardware_part(d, app, answered)
        network = self._network_part(app, answered)
        services = self._services_part(d, app, answered)
        cameras = self._cameras_part(app, answered)
        parts = [hardware, network, *services.values(), *cameras]
        count = sum(len(p["warnings"]) for p in parts)
        watched = [c for c in cameras if c["state"] != DISABLED]
        outage = (any(services[k]["state"] == OUTAGE for k in CRITICAL)
                  or (watched and all(c["state"] == OUTAGE for c in watched)))
        self._sent = list(self._notices)
        return {"state": OUTAGE if outage else DEGRADED if count else OPERATIONAL,
                "warning_count": count, "hardware": hardware, "network": network,
                "services": services, "cameras": cameras}

    def delivered(self, detail: dict) -> None:
        """The FMS accepted the heartbeat built from `detail`."""
        for unit, u in detail["units"].items():
            if u["restarts"] is not None:
                self._baseline[unit] = u["restarts"]
        self._notices = [n for n in self._notices if n not in self._sent]

    def _notes(self, part: str) -> list[str]:
        return [m for p, m in self._notices if p == part]

    def _hardware_part(self, d: dict, app: dict, answered: bool) -> dict:
        hw = d["hardware"]
        sensors = app.get("hardware") or {}
        mem, disk = hw["memory"], hw["disk"]
        data = disk.get("data") or disk.get("root") or {}
        temps = list(hw["temperature_c"].values()) + list((sensors.get("temperature_c") or {}).values())
        hottest = max(temps, default=None)
        warnings = list(sensors.get("warnings") or []) if answered else []
        if (mem.get("used_pct") or 0) >= MEMORY_WARN_PCT:
            warnings.append(f"memory {mem['used_pct']}% used — the device may slow down "
                            f"or stop processes")
        root = disk.get("root")
        if root and root["used_pct"] >= DISK_WARN_PCT and not _same_fs("/", collect.data_dir()):
            warnings.append(f"system disk (/) {root['used_pct']}% full")
        warnings += [f"the disk at {m} switched to read-only after errors — recordings and "
                     f"logs cannot be written; the device needs service" for m in hw["read_only"]]
        if not answered:                                   # the app's own checks, as a fallback
            if data and data["used_pct"] >= DISK_WARN_PCT:
                warnings.append(f"data disk {data['used_pct']}% full")
            if hottest is not None and hottest >= _thermal_warn_c():
                warnings.append(f"running hot: {hottest:.0f} °C (warning at "
                                f"{_thermal_warn_c():.0f} °C) — check the fan and airflow")
        boot = hw["last_boot"]
        if boot["reason"] == "unexpected_restart" and hw["uptime_secs"] < RECENT_BOOT_SECS:
            warnings.append(f"the device restarted unexpectedly {hw['uptime_secs'] // 60} min ago "
                            f"(a power cut or a system crash)")
        warnings += self._notes("hardware")
        total, avail = mem.get("total_bytes"), mem.get("available_bytes")
        return {
            "state": DEGRADED if warnings else OPERATIONAL,
            "uptime_secs": hw["uptime_secs"],
            "last_boot": boot["reason"],
            "cpu_pct": _whole(hw["cpu"]["load_pct"]),
            "gpu_pct": _whole(sensors.get("gpu_load_pct")),
            "ram_used_gb": _gb(total - avail if total and avail is not None else None),
            "ram_total_gb": _gb(total),
            "disk_used_gb": _gb(data["total_bytes"] - data["free_bytes"] if data else None),
            "disk_total_gb": _gb(data.get("total_bytes")),
            "temperature_c": round(hottest, 1) if hottest is not None else None,
            "fan_pct": hw["fan"].get("pct"),
            "power_w": sensors.get("board_power_w"),
            "power_mode": hw["power_mode"],
            "warnings": warnings,
        }

    def _network_part(self, app: dict, answered: bool) -> dict:
        notes = self._notes("network")
        net = app.get("network") if answered else None
        if not isinstance(net, dict):
            return {"state": UNKNOWN, "internet": UNKNOWN, "latency_ms": None,
                    "hotspot": UNKNOWN, "hotspot_devices": None, "warnings": notes}
        internet = net.get("internet") or {}
        spot = net.get("hotspot") or {}
        warnings = list(net.get("warnings") or []) + notes
        connected = internet.get("connected")
        return {
            "state": worst(net.get("state") or UNKNOWN, DEGRADED if notes else OPERATIONAL),
            "internet": UNKNOWN if connected is None else "connected" if connected else "disconnected",
            "latency_ms": internet.get("latency_ms"),
            "hotspot": UNKNOWN if not spot.get("supported") else "on" if spot.get("on") else "off",
            "hotspot_devices": len(spot.get("devices") or []) if spot.get("supported") else None,
            "warnings": warnings,
        }

    def _services_part(self, d: dict, app: dict, answered: bool) -> dict:
        units = d["units"]
        app_services = app.get("services") or {}
        backend = app.get("backend") if answered else None
        out: dict[str, dict] = {}
        for name, unit, kind in SERVICES:
            part = self._unit_part(units, unit, kind) if unit else None
            if name == "edge_application":
                if answered:
                    part = _combine(part, backend)
                elif part["state"] != OUTAGE:
                    part = {"state": OUTAGE, "warnings": part["warnings"] + [
                        f"the CERAVIS app on this device is not answering ({d['app_error']})"]}
                part["version"] = ((backend or {}).get("version")
                                   or collect.software()["edge_version"])
            elif name == "mediamtx":
                part = _app_part(app_services.get("mediamtx"), answered)
            elif name == "ai_detection":
                part = _app_part(app.get("ai"), answered)
            elif name == "cloud_connection":
                link = app_services.get("app_server") if answered else None
                part = _app_part(link, answered)
                part["pending_uploads"] = (link or {}).get("uploads_pending")
            elif name == "frpc":
                part = _combine(part, app_services.get("tunnel") if answered else None)
            elif name == "fleet_agent":
                part["version"] = __version__
            elif name == "nightly_reboot":
                last = units.get("ceravis-reboot.service") or {}
                if last.get("result") not in (None, "", "success"):
                    part["warnings"].append(f"the last nightly reboot check failed ({last['result']}) "
                                            f"— see journalctl -u ceravis-reboot")
                    part["state"] = worst(part["state"], DEGRADED)
            elif name == "edge_hotspot":
                spot = (app.get("network") or {}).get("hotspot") if answered else None
                part = _combine(part, spot if isinstance(spot, dict) and spot.get("supported") else None)
            elif name == "time_sync":
                part = _combine(part, app_services.get("time_sync") if answered else None)
                if not answered and d["hardware"]["clock"]["ntp_synchronized"] is False:
                    part = {"state": worst(part["state"], DEGRADED),
                            "warnings": part["warnings"] + ["system clock is not NTP-synced"]}
            elif name == "security_updates":
                part["pending_security"] = d["updates"].get("security")
            elif name == "os_hardening":
                part = _hardening(units)
            out[name] = {"state": part["state"], **{k: v for k, v in part.items()
                                                    if k not in ("state", "warnings")},
                         "warnings": part["warnings"]}
        return out

    def _unit_part(self, units: dict, unit: str, kind: str) -> dict:
        u = units.get(unit)
        if not u:
            return {"state": UNKNOWN, "warnings": []}          # no systemd here (a dev box)
        warnings: list[str] = []
        load, active = u["load"], u["active"]
        if load in ("not-found", "masked"):
            state = DISABLED
        elif active == "failed":
            state = OUTAGE
            warnings.append(f"{unit} has FAILED ({u['result'] or 'see journalctl -u ' + unit})")
        elif active in ("active", "reloading"):
            state = OPERATIONAL
        elif active in ("activating", "deactivating"):
            state = DEGRADED
            warnings.append(f"{unit} is {'starting' if active == 'activating' else 'stopping'}")
        elif kind == "service":
            state = OUTAGE
            warnings.append(f"{unit} is not running")
        elif kind == "timer":
            state = DEGRADED
            warnings.append(f"{unit} is not scheduled")
        else:
            state = DISABLED
        restarts = u["restarts"]
        if restarts is not None:
            before = self._baseline.setdefault(unit, restarts)
            if restarts > before:
                n = restarts - before
                warnings.append(f"{unit} restarted {n} time{'s' if n > 1 else ''} "
                                f"since the last heartbeat")
                state = worst(state, DEGRADED)
        return {"state": state, "warnings": warnings}

    def _cameras_part(self, app: dict, answered: bool) -> list[dict]:
        if not answered:                    # the last known cameras, each unknown right now
            return [{**c, "state": UNKNOWN, "fps": None, "reconnects": None, "recording": None,
                     "night_vision": None, "ptz": None, "wifi_signal_dbm": None, "warnings": []}
                    for c in self._cameras_known]
        cameras = []
        for c in app.get("cameras") or []:
            cameras.append({
                "camera_id": c.get("camera_id"), "room": c.get("room"),
                "state": c.get("state") or UNKNOWN, "fps": c.get("fps"),
                "reconnects": c.get("reconnects"),
                "recording": (c.get("recording") or {}).get("on"),
                "night_vision": c.get("night_vision"), "ptz": c.get("ptz"),
                "wifi_signal_dbm": (c.get("wifi") or {}).get("signal_dbm"),
                "warnings": list(c.get("warnings") or []),
            })
        self._cameras_known = [{"camera_id": c["camera_id"], "room": c["room"]} for c in cameras]
        return cameras


def agent_facts(rtt_ms: float | None, interval: float, enroll_key: str) -> dict:
    """What only the agent knows about its own link to the FMS."""
    return {"version": __version__, "fms_rtt_ms": rtt_ms, "interval_secs": interval,
            "can_enroll": bool(enroll_key)}


def _app_part(part, answered: bool) -> dict:
    if not answered or not isinstance(part, dict):
        return {"state": UNKNOWN, "warnings": []}
    return {"state": part.get("state") or UNKNOWN, "warnings": list(part.get("warnings") or [])}


def _combine(unit_part: dict, app_part) -> dict:
    """A systemd unit and what the app sees of the same thing: a stopped or
    missing unit says it all; otherwise the worse of the two, both warnings."""
    if not isinstance(app_part, dict) or unit_part["state"] in (OUTAGE, DISABLED):
        return unit_part
    return {"state": worst(unit_part["state"], app_part.get("state") or UNKNOWN),
            "warnings": unit_part["warnings"] + list(app_part.get("warnings") or [])}


def _hardening(units: dict) -> dict:
    if not units:
        return {"state": UNKNOWN, "warnings": []}
    on = [u.removesuffix(".service") for u in SWITCHED_OFF
          if (units.get(u) or {}).get("active") in ("active", "activating")]
    if not on:
        return {"state": OPERATIONAL, "warnings": []}
    return {"state": DEGRADED, "warnings": [
        f"{', '.join(on)} should be switched off but {'is' if len(on) == 1 else 'are'} running "
        f"(bash setup/tune_os.sh switches it off again; Bluetooth shares the hotspot's radio)"]}


# ---- readers (never raise: a missing file is "unknown", not a crash) ----------

def _systemd(units: list[str]) -> dict[str, dict]:
    """`systemctl show` for every unit at once: {unit: {Property: value}}."""
    props = ("Id", "LoadState", "ActiveState", "SubState", "Result",
             "ActiveEnterTimestampMonotonic", "NRestarts", "MemoryCurrent")
    try:
        out = subprocess.run(["systemctl", "show", "--no-pager", *[f"-p{p}" for p in props],
                              *units], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    blocks = [b for b in out.stdout.strip().split("\n\n") if b.strip()]
    result: dict[str, dict] = {}
    for unit, block in zip(units, blocks):
        result[unit] = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
    return result


def _unit_detail(props: dict) -> dict:
    active = props.get("ActiveState", "")
    entered = _int(props.get("ActiveEnterTimestampMonotonic"))
    memory = _int(props.get("MemoryCurrent"))
    return {"load": props.get("LoadState", ""), "active": active, "sub": props.get("SubState", ""),
            "result": props.get("Result", ""),
            "uptime_secs": (round(time.monotonic() - entered / 1e6)
                            if active == "active" and entered else None),
            "restarts": _int(props.get("NRestarts")),
            "memory_mb": (round(memory / 2 ** 20, 1)          # "not available" is UINT64_MAX
                          if memory is not None and memory < 2 ** 63 else None)}


def _cpu_counters() -> tuple[int, int] | None:
    first = collect._read("/proc/stat").splitlines()[:1]
    try:
        fields = [int(v) for v in first[0].split()[1:]]
    except (IndexError, ValueError):
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)        # idle + iowait
    total = sum(fields[:8])                                          # guest is inside user
    return total - idle, total


def _load_avg() -> list[float] | None:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _memory() -> dict:
    info: dict[str, int] = {}
    for line in collect._read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        try:
            info[key] = int(rest.split()[0]) * 1024                  # the kernel counts kB
        except (IndexError, ValueError):
            continue
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    swap, swap_free = info.get("SwapTotal"), info.get("SwapFree")
    return {
        "total_bytes": total, "available_bytes": avail,
        "used_pct": round(100.0 * (total - avail) / total, 1) if total and avail is not None else None,
        "swap_total_bytes": swap,
        "swap_used_pct": round(100.0 * (swap - swap_free) / swap, 1) if swap and swap_free is not None else None,
    }


def _temperatures() -> dict[str, float]:
    """Every thermal zone the kernel exposes, by name (cpu, gpu, tj, soc0 …), °C."""
    out: dict[str, float] = {}
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        name = collect._read(str(zone / "type")).lower().removesuffix("-thermal")
        try:
            c = int(collect._read(str(zone / "temp"))) / 1000.0
        except ValueError:
            continue
        if name and -40.0 < c < 150.0:                               # unused zones read -256 °C
            out.setdefault(name, round(c, 1))
    return out


def _fan() -> dict:
    """The fan's drive (pwm1, 0-255 -> %) and its measured speed (rpm)."""
    out: dict = {"pct": None, "rpm": None}
    for mon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        pwm, rpm = collect._read(str(mon / "pwm1")), collect._read(str(mon / "rpm"))
        if pwm.isdigit() and out["pct"] is None:
            out["pct"] = round(int(pwm) * 100 / 255)
        if rpm.isdigit() and out["rpm"] is None:
            out["rpm"] = int(rpm)
    return out


def _power_mode() -> str | None:
    """The NVIDIA power mode in force (e.g. 15W, 25W, MAXN_SUPER)."""
    m = re.search(r"pmode:(\d+)", collect._read("/var/lib/nvpmodel/status"))
    if not m:
        return None
    names = dict(re.findall(r"<\s*POWER_MODEL\s+ID=(\d+)\s+NAME=([^\s>]+)",
                            collect._read("/etc/nvpmodel.conf")))
    return names.get(str(int(m.group(1))), f"mode {int(m.group(1))}")


def _read_only(paths: list[str]) -> list[str]:
    """The mount points among `paths` that the kernel has made read-only."""
    mounts = []
    for line in collect._read("/proc/mounts").splitlines():
        f = line.split()
        if len(f) >= 4:
            mounts.append((f[1], "ro" in f[3].split(",")))
    out = []
    for path in paths:
        best = max((m for m in mounts if path == m[0] or path.startswith(m[0].rstrip("/") + "/")),
                   key=lambda m: len(m[0]), default=None)
        if best and best[1] and best[0] not in out:
            out.append(best[0])
    return out


def _oom_kills() -> int | None:
    m = re.search(r"^oom_kill (\d+)$", collect._read("/proc/vmstat"), re.M)
    return int(m.group(1)) if m else None


def _journal_mb() -> float | None:
    total = 0
    for root, _, files in os.walk("/var/log/journal"):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return round(total / 2 ** 20, 1) if total else None


def _updates() -> dict:
    """Ubuntu's own count of waiting updates, and whether one waits for a reboot."""
    text = collect._read("/var/lib/update-notifier/updates-available")
    pending = re.search(r"(\d+) updates? can be applied", text)
    security = re.search(r"(\d+) of these updates (?:is|are) (?:a )?(?:standard )?security", text)
    return {"pending": int(pending.group(1)) if pending else None,
            "security": int(security.group(1)) if security else (0 if pending else None),
            "reboot_required": Path("/var/run/reboot-required").exists()}


def _versions() -> dict:
    """What is installed (/etc/ceravis/versions.json, written by every installer)."""
    try:
        data = json.loads(Path("/etc/ceravis/versions.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _last_boot() -> dict:
    """Why the device last started, as the edge app decided it at its start
    (maintenance/reboot.py → data/boot_state.json). Unknown until the app has
    started once in this boot."""
    try:
        state = json.loads((collect.data_dir() / "boot_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    current = collect._read("/proc/sys/kernel/random/boot_id")
    boot = state.get("last_boot") if current and state.get("boot_id") == current else None
    return boot if isinstance(boot, dict) and boot.get("reason") else {"reason": UNKNOWN, "at": None}


def _ntp_synchronized() -> bool | None:
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return {"yes": True, "no": False}.get(value) if out.returncode == 0 else None


def _same_fs(a, b) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _gb(value) -> float | None:
    return round(value / GB, 1) if isinstance(value, (int, float)) else None


def _whole(value) -> int | None:
    return round(value) if isinstance(value, (int, float)) else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
