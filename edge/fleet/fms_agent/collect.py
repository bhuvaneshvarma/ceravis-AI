"""What the agent reports.

Two sources, and only two:

  1. The edge app's own health surface, GET /api/v1/system/status on this
     machine — forwarded VERBATIM. The edge already decides ok/degraded and
     explains why; the agent re-derives nothing (one mechanism per signal).
  2. Facts only the agent can know: its version, boot id, uptime, OS — and,
     crucially, whether the edge app answered at all.
"""
from __future__ import annotations

import json
import platform
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__


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


def facts(rtt_ms: float | None, interval: float) -> dict:
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
    }


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
