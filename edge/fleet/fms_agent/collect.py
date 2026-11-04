"""What the agent reports.

Two sources, and only two:

  1. The edge app's own health surface, GET /api/v1/system/status on this
     machine — forwarded VERBATIM. The edge already decides ok/degraded and
     explains why; the agent re-derives nothing (one mechanism per signal).
  2. Facts only the agent can know: its version, boot id, uptime, OS, free
     disk — and, crucially, whether the edge app answered at all.

Enrollment adds who this device is: its name (from the edge's own identity
module), its board and memory, the release installed, and the edge_id the app
server gave it before the FMS issued them (so an installed home keeps it).
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import fms_protocol as protocol

from . import __version__

# The edge app this agent ships inside: edge/fleet/fms_agent/collect.py -> edge/.
EDGE_DIR = Path(__file__).resolve().parents[2]
LAYOUT_FILE = "/etc/ceravis/layout.env"
# An edge_id this device can route on: the rule the tunnel helper
# (cloud/apply_edge_id.sh) and setup/sync_edge_id.sh hold it to, within the
# protocol's own (fms_protocol.EDGE_ID_RE).
_ROUTABLE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def routable_edge_id(value) -> bool:
    return isinstance(value, str) and bool(_ROUTABLE.match(value)) and protocol.valid_edge_id(value)


def edge_status(url: str, timeout: float = 8.0) -> tuple[dict | None, str | None]:
    """(status, None) when the edge answered; (None, reason) when it did not —
    which is itself the most important thing this device can report."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if not isinstance(data, dict):
            return None, "status endpoint returned something that is not a JSON object"
        return data, None
    except urllib.error.HTTPError as exc:
        return None, f"status endpoint answered HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, f"cannot reach {url} ({reason})"
    except ValueError:
        return None, "status endpoint returned invalid JSON"


def facts(rtt_ms: float | None, interval: float, enroll_key: str) -> dict:
    l4t = _read("/etc/nv_tegra_release")
    return {
        "version": __version__,
        "unix_time": time.time(),          # this device's own clock — the server works out the offset
        "boot_id": _read("/proc/sys/kernel/random/boot_id"),
        "uptime_secs": _uptime(),
        "hostname": socket.gethostname(),
        "os": _os_name(),
        "kernel": platform.release(),
        "l4t": l4t.splitlines()[0].lstrip("# ").strip() if l4t else "",
        "python": platform.python_version(),
        "rtt_ms": rtt_ms,
        "interval_secs": interval,
        # WHICH fleet enrollment key it holds — a short id, never the key. The
        # server checks it against its own: only that key lets the device enroll
        # again by itself after an admin's Reset key. can_enroll is what 0.2.1
        # agents reported instead (holds some key); kept for servers that read it.
        "enroll_key_id": protocol.enroll_key_id(enroll_key),
        "can_enroll": bool(enroll_key),
        "disk": disk(),
    }


# ---- who this device is (enrollment) ---------------------------------------

def device_id() -> str:
    """This device's name, ceravis-<code>, from the edge's own identity module —
    the ONE mechanism the hostname, the hotspot and the tunnel prefix use too."""
    if str(EDGE_DIR) not in sys.path:
        sys.path.append(str(EDGE_DIR))     # edge/common, after the agent's own modules
    from common.device_identity import identity
    return identity().name


def legacy_edge_id() -> str | None:
    """The edge_id the app server gave this device before the FMS issued them
    (account.json's edgeId), sent at enrollment so an installed home keeps its
    links. None when there is none, or it is not a valid edge_id."""
    try:
        account = json.loads((data_dir() / "account.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = str(account.get("edgeId") or "").strip() if isinstance(account, dict) else ""
    return value if routable_edge_id(value) else None


def hardware() -> dict:
    model = _read("/proc/device-tree/model").replace("\x00", "").strip()
    return {"model": model or platform.machine(), "ram_total_bytes": _ram_bytes()}


def software() -> dict:
    """The edge release installed: edge/VERSION, and its commit when known."""
    out = {"edge_version": _read(str(EDGE_DIR / "VERSION")) or "unknown"}
    commit = _commit()
    if commit:
        out["edge_commit"] = commit
    return out


# ---- free disk -----------------------------------------------------------------

def disk() -> dict:
    """Free space where it matters: the system (/) and the edge's data folder
    (recordings, database). Measured here, so a full disk is still reported when
    the edge app itself is down — a full disk is a common reason it is."""
    return {"root": _usage("/"), "data": _usage(data_dir())}


def data_dir() -> Path:
    """The edge app's data folder, by the app's own rule: DATA_DIR, else the
    layout file's CERAVIS_DATA_DIR, else data/ — a relative one under edge/."""
    raw = (os.environ.get("DATA_DIR") or _layout("CERAVIS_DATA_DIR") or "data").strip()
    path = Path(raw)
    return path if path.is_absolute() else EDGE_DIR / path


def _usage(path) -> dict | None:
    try:
        st = os.statvfs(path)
    except OSError:                                  # not there yet (fresh install)
        return None
    total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    return {"total_bytes": total, "free_bytes": free,
            "used_pct": round(100 * (total - free) / total, 1) if total else 0.0}


def _layout(key: str) -> str:
    for line in _read(os.environ.get("CERAVIS_LAYOUT_FILE") or LAYOUT_FILE).splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"')
    return ""


def _ram_bytes() -> int:
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) * 1024          # the kernel counts kB
            except (IndexError, ValueError):
                break
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        return 0


def _commit() -> str:
    """A release names its commit in manifest.json; a development clone asks git."""
    try:
        manifest = json.loads((EDGE_DIR.parent / "manifest.json").read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("commit"):
            return str(manifest["commit"])[:40]
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["git", "-C", str(EDGE_DIR.parent), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _uptime() -> float:
    first = _read("/proc/uptime").split(" ")[0]
    try:
        return float(first)
    except ValueError:
        return 0.0


def _os_name() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return f"{platform.system()} {platform.release()}".strip()
