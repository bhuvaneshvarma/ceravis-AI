"""The heartbeat's `status`: every part of the device, each with its own state,
uptime and `warnings` (an empty list = fine).

    status = {state, warning_count, hardware, backend, services, cameras}

Two sources, each reporting only what it can really see:

  • the agent itself (this file): the HARDWARE, read straight from the kernel
    (/proc, /sys, statvfs, timedatectl), and the SYSTEMD SERVICES (systemctl
    show). Both still report when the edge app is down — when they matter most.
  • the edge app's GET /api/v1/system/health: the app itself (backend), what it
    runs or talks to (MediaMTX, the app server, the internet, the tunnel), every
    camera, and the board sensors it reads (tegrastats: GPU load, board power).

Where both could judge the same thing (disk, clock, temperature), the app's
rule is used while it answers; the agent's own check is the fallback when it
does not, so the device is never reported blind. Specified in
docs/fleet-heartbeat-api.md. Standard library only.
"""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
from pathlib import Path

from . import __version__, collect

# systemd units, by the name the heartbeat reports them under. `always_on` ones
# must be running; a timer/path unit only warns when it failed or stopped.
UNITS = (
    # name           unit                          always_on
    ("ceravis",       "ceravis.service",            True),
    ("frpc",          "frpc.service",               True),
    ("fleet_agent",   "ceravis-fleet-agent.service", True),
    ("edge_id_watch", "ceravis-edge-id.path",       True),
    ("reboot_timer",  "ceravis-reboot.timer",       False),
)
# What the edge app reports about itself and what it talks to.
APP_SERVICES = ("mediamtx", "app_server", "internet", "tunnel")

MEMORY_WARN_PCT = 90.0
DISK_WARN_PCT = 90.0


def _thermal_warn_c() -> float:
    try:                                   # the edge app's own setting (jetson.env)
        return float(os.environ.get("THERMAL_WARN_C") or 85.0)
    except ValueError:
        return 85.0


class Health:
    """Builds `status` for each heartbeat. Keeps what it needs between beats:
    the CPU counters (so CPU load is the average since the last beat, not one
    instant) and each unit's restart count (to say "restarted since the last
    heartbeat" once, not forever)."""

    def __init__(self) -> None:
        self._cpu_prev: tuple[int, int] | None = None
        self._restarts_prev: dict[str, int] = {}

    def build(self, app: dict | None, app_error: str | None, agent: dict) -> dict:
        answered = isinstance(app, dict) and "backend" in app
        app = app if answered else {}
        hardware = self.hardware(app.get("hardware") or {}, answered)
        services = self.services(app.get("services") or {}, answered, agent)
        backend = dict(app["backend"]) if answered else {
            "state": "down",
            "warnings": [f"the CERAVIS app on this device is not answering ({app_error})"
                         if app_error else "the CERAVIS app on this device is not answering"],
        }
        cameras = app.get("cameras") if answered else None      # None = unknown right now
        parts = [hardware, backend, *services.values(), *(cameras or [])]
        count = sum(len(p.get("warnings") or []) for p in parts)
        down = (backend.get("state") == "down"
                or services["ceravis"]["state"] in ("stopped", "stopping", "failed"))
        return {
            "state": "down" if down else "warning" if count else "ok",
            "warning_count": count,
            "hardware": hardware,
            "backend": backend,
            "services": services,
            "cameras": cameras,
        }

    # ---- the board itself -------------------------------------------------
    def hardware(self, sensors: dict, app_answered: bool) -> dict:
        disk = collect.disk()
        mem = _memory()
        temps = _temperatures()
        for name, value in (sensors.get("temperature_c") or {}).items():
            temps.setdefault(name, value)                  # tegrastats fills any gap
        clock = {"unix_time": round(time.time(), 3), "ntp_synchronized": _ntp_synchronized()}
        if isinstance(sensors.get("clock"), dict):
            clock["timezone"] = sensors["clock"].get("timezone")
            if sensors["clock"].get("ntp_synchronized") is not None:
                clock["ntp_synchronized"] = sensors["clock"]["ntp_synchronized"]
        l4t = collect._read("/etc/nv_tegra_release")

        warnings = list(sensors.get("warnings") or []) if app_answered else []
        if mem.get("used_pct") is not None and mem["used_pct"] >= MEMORY_WARN_PCT:
            warnings.append(f"memory {mem['used_pct']}% used — the device may slow down "
                            f"or stop processes")
        root, data = disk.get("root"), disk.get("data")
        if root and root["used_pct"] >= DISK_WARN_PCT and not _same_fs("/", collect.data_dir()):
            warnings.append(f"system disk (/) {root['used_pct']}% full")
        if not app_answered:                               # the app's own checks, as a fallback
            if data and data["used_pct"] >= DISK_WARN_PCT:
                warnings.append(f"data disk {data['used_pct']}% full")
            hottest = max(temps.values(), default=None)
            if hottest is not None and hottest >= _thermal_warn_c():
                warnings.append(f"running hot: {hottest:.0f} °C (warning at "
                                f"{_thermal_warn_c():.0f} °C) — check the fan and airflow")
            if clock["ntp_synchronized"] is False:
                warnings.append("system clock is not NTP-synced")
        return {
            "state": "warning" if warnings else "ok",
            "model": collect.hardware()["model"],
            "uptime_secs": round(collect._uptime()),
            "boot_id": collect._read("/proc/sys/kernel/random/boot_id"),
            "os": {"name": collect._os_name(), "kernel": platform.release(),
                   "l4t": l4t.splitlines()[0].lstrip("# ").strip() if l4t else "",
                   "python": platform.python_version()},
            "cpu": {"cores": os.cpu_count(), "load_pct": self._cpu_load_pct(),
                    "load_avg": _load_avg()},
            "gpu": {"load_pct": sensors.get("gpu_load_pct")},
            "memory": mem,
            "temperature_c": temps,
            "power": {"board_w": sensors.get("board_power_w"),
                      "over_current_events": sensors.get("over_current_events")},
            "disk": disk,
            "clock": clock,
            "network": {"hostname": socket.gethostname()},
            "warnings": warnings,
        }

    def _cpu_load_pct(self) -> float | None:
        now = _cpu_counters()
        if now is None:
            return None
        if self._cpu_prev is None:                         # first beat: a short sample
            time.sleep(0.25)
            self._cpu_prev, now = now, _cpu_counters() or now
        (busy0, total0), (busy1, total1) = self._cpu_prev, now
        self._cpu_prev = now
        span = total1 - total0
        return round(100.0 * (busy1 - busy0) / span, 1) if span > 0 else None

    # ---- services -----------------------------------------------------------
    def services(self, app_services: dict, app_answered: bool, agent: dict) -> dict:
        units = _systemd([unit for _, unit, _ in UNITS])
        out: dict[str, dict] = {}
        for name, unit, always_on in UNITS:
            out[name] = self._unit_part(name, unit, units.get(unit, {}), always_on)
        out["fleet_agent"].update(agent)
        for name in APP_SERVICES:
            part = app_services.get(name)
            out[name] = dict(part) if app_answered and isinstance(part, dict) else {
                "state": "unknown", "warnings": []}       # the backend's warning says why
        return out

    def _unit_part(self, name: str, unit: str, props: dict, always_on: bool) -> dict:
        load = props.get("LoadState", "")
        active = props.get("ActiveState", "")
        if not props:
            state = "unknown"                              # no systemd here (a dev box)
        elif load == "not-found":
            state = "not_installed"
        else:
            state = {"active": "running", "inactive": "stopped", "failed": "failed",
                     "activating": "starting", "deactivating": "stopping",
                     "reloading": "running"}.get(active, active or "unknown")
        restarts = _int(props.get("NRestarts"))
        warnings: list[str] = []
        if state == "failed":
            warnings.append(f"{unit} has FAILED ({props.get('Result') or 'see journalctl -u ' + unit})")
        elif always_on and state in ("stopped", "stopping"):
            warnings.append(f"{unit} is not running")
        before = self._restarts_prev.get(unit)
        if restarts is not None:
            if before is not None and restarts > before:
                n = restarts - before
                warnings.append(f"{unit} restarted {n} time{'s' if n > 1 else ''} "
                                f"since the last heartbeat")
            self._restarts_prev[unit] = restarts
        entered = _int(props.get("ActiveEnterTimestampMonotonic"))
        uptime = (round(time.monotonic() - entered / 1e6)
                  if state == "running" and entered else None)
        part = {"state": state, "unit": unit, "uptime_secs": uptime, "restarts": restarts,
                "memory_bytes": _memory_bytes(props.get("MemoryCurrent")), "warnings": warnings}
        if unit.endswith(".timer"):
            part.pop("memory_bytes")
        return part


def agent_facts(rtt_ms: float | None, interval: float, enroll_key: str) -> dict:
    """What only the agent knows about its own link to the FMS."""
    return {"version": __version__, "fms_rtt_ms": rtt_ms, "interval_secs": interval,
            "can_enroll": bool(enroll_key)}


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
        values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        result[values.get("Id") or unit] = values
        result.setdefault(unit, values)
    return result


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
    except OSError:
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


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _memory_bytes(value) -> int | None:
    n = _int(value)
    return None if n is None or n >= 2 ** 63 else n                  # "not available" is UINT64_MAX
