"""Running an order from the server.

Every order is re-validated here with the SAME function the server used to
accept it (fms_protocol.normalize_command). Even a message that somehow got
past the response signature could only ever do what that allow-list permits:
ping, restart ceravis/frpc, read their logs or the device's own log files, or
say what is installed.
"""
from __future__ import annotations

import json
import logging
import platform
import socket
import subprocess
import time

import fms_protocol as protocol

from . import __version__, collect
from . import health as device_health
from .logs import Logbook, log_dir

log = logging.getLogger("fms_agent")


def run(order: dict) -> dict:
    started = time.monotonic()
    try:
        args = protocol.normalize_command(order.get("kind", ""), order.get("args"))
        ok, output = _HANDLERS[order["kind"]](args)
    except ValueError as exc:
        ok, output = False, f"refused: {exc}"
    except subprocess.TimeoutExpired as exc:
        ok, output = False, f"timed out after {exc.timeout:.0f}s"
    except Exception as exc:                       # an order must never kill the agent
        log.exception("order %s crashed", order.get("id"))
        ok, output = False, f"{type(exc).__name__}: {exc}"
    log.info("order %s (%s) %s", order.get("id"), order.get("kind"), "ok" if ok else "FAILED")
    return {"id": str(order.get("id", ""))[:40], "ok": ok,
            "output": _clip(output), "duration_ms": int((time.monotonic() - started) * 1000)}


def _ping(args: dict) -> tuple[bool, str]:
    return True, f"pong from {socket.gethostname()}"


def _restart(args: dict) -> tuple[bool, str]:
    # Exactly the command line the sudoers rule grants — nothing composed.
    return _sh(["sudo", "-n", "/usr/bin/systemctl", "restart", f"{args['unit']}.service"], 90,
               done=f"{args['unit']}.service restarted")


def _logs(args: dict) -> tuple[bool, str]:
    if "file" in args:
        path = _LOG_FILES[args["file"]](collect.data_dir())
        rows = Logbook(path, max_bytes=0, backups=10).tail(args["lines"])
        return (True, "\n".join(rows)) if rows else (False, f"{path.name}: no lines yet")
    return _sh(["journalctl", "-u", f"{args['unit']}.service", "-n", str(args["lines"]),
                "--no-pager", "-o", "short-iso"], 30)


def _info(args: dict) -> tuple[bool, str]:
    l4t = collect._read("/etc/nv_tegra_release")
    return True, json.dumps({
        "device_id": collect.device_id(), "hostname": socket.gethostname(),
        **collect.hardware(), **collect.software(), "agent_version": __version__,
        "os": collect._os_name(), "kernel": platform.release(),
        "l4t": l4t.splitlines()[0].lstrip("# ").strip() if l4t else "",
        "python": platform.python_version(), "power_mode": device_health._power_mode(),
        "installed": device_health._versions(),
    }, indent=1, default=str)


# `logs` file name -> where it is, inside the edge app's data folder.
_LOG_FILES = {
    "health": lambda d: log_dir(d) / "health.jsonl",
    "events": lambda d: log_dir(d) / "events.jsonl",
    "fleet": lambda d: log_dir(d) / "fleet.jsonl",
    "incoming": lambda d: log_dir(d) / "incoming.jsonl",
    "cloud_calls": lambda d: d / "cloud_calls.jsonl",
    "mediamtx": lambda d: d / "mediamtx.log",
}


def _sh(argv: list[str], timeout: int, done: str = "") -> tuple[bool, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    text = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, text or f"exit code {proc.returncode}"
    return True, text or done


def _clip(text: str) -> str:
    """Keep the END of long output — the newest log lines matter most."""
    limit = protocol.MAX_OUTPUT_CHARS
    return text if len(text) <= limit else "…(earlier output trimmed)\n" + text[-(limit - 40):]


_HANDLERS = {"ping": _ping, "restart_service": _restart, "logs": _logs, "info": _info}
