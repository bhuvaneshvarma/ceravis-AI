from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from config.settings import settings


logger = logging.getLogger("monitoring")


_TEGRA_RE = {
    "ram": re.compile(r"RAM (\d+)/(\d+)MB"),
    "cpu": re.compile(r"CPU \[([^\]]+)\]"),
    "gr3d": re.compile(r"GR3D_FREQ (\d+)%"),
    # JetPack 6 prints these in lower case (cpu@53.1C), older releases upper.
    "temp_cpu": re.compile(r"\bCPU@([\d.]+)C", re.I),
    "temp_gpu": re.compile(r"\bGPU@([\d.]+)C", re.I),
    "temp_tj": re.compile(r"\btj@([\d.]+)C", re.I),
    "power_in": re.compile(r"VDD_IN (\d+)mW"),
}

# The board's own over-current event counters (INA3221 power monitor). A rising
# count means the board drew more than its supply limit and slowed itself down —
# the "over-current" warning the desktop shows. Read straight from sysfs, so it
# works without the desktop running.
_OC_GLOB = "/sys/class/hwmon/hwmon*/oc*_event_cnt"
_OC_WINDOW_SECS = 600.0       # over-current within the last 10 min counts as "now"


def _oc_events() -> int | None:
    total, seen = 0, False
    for p in Path("/").glob(_OC_GLOB.lstrip("/")):
        try:
            total += int(p.read_text().strip() or 0)
            seen = True
        except (OSError, ValueError):
            continue
    return total if seen else None


class SystemMonitor:
    """
    Polls Jetson tegrastats for CPU / GPU / RAM / temperatures / board power
    every 2s, and the over-current counters. `problems()` turns them into plain
    warnings (too hot, over-current) for the status page and the logs.

    On non-Jetson hosts it returns empty stats — safe in dev.
    """

    POLL_SECS = 2.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats: dict = {}
        self._proc: subprocess.Popen | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._tegrastats_path = shutil.which("tegrastats")
        self._oc_base: int | None = None      # count when we started
        self._oc_last: int | None = None
        self._oc_changed_at = 0.0
        self._warned: set[str] = set()

    @property
    def is_jetson(self) -> bool:
        return self._tegrastats_path is not None or Path("/etc/nv_tegra_release").exists()

    def start(self) -> None:
        if self._running or not self._tegrastats_path:
            if not self._tegrastats_path:
                logger.info("tegrastats not available — system monitor disabled")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="system-monitor",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._proc is not None:
            self._proc.terminate()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def _run(self) -> None:
        interval_ms = int(self.POLL_SECS * 1000)
        try:
            self._proc = subprocess.Popen(
                [self._tegrastats_path, "--interval", str(interval_ms)],  # type: ignore[arg-type]
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception:
            logger.exception("failed to start tegrastats")
            return

        assert self._proc.stdout is not None
        while self._running:
            line = self._proc.stdout.readline()
            if not line:
                time.sleep(0.5)
                continue
            self._parse(line)

    def _parse(self, line: str) -> None:
        stats: dict = {"raw": line.strip()}

        m = _TEGRA_RE["ram"].search(line)
        if m:
            used, total = int(m.group(1)), int(m.group(2))
            stats["ram_used_mb"] = used
            stats["ram_total_mb"] = total
            stats["ram_pct"] = round(100 * used / max(total, 1), 1)

        m = _TEGRA_RE["cpu"].search(line)
        if m:
            parts = m.group(1).split(",")
            loads: list[float] = []
            for p in parts:
                pct = p.split("%")[0]
                try:
                    loads.append(float(pct))
                except ValueError:
                    pass
            if loads:
                stats["cpu_pct_per_core"] = loads
                stats["cpu_pct_avg"] = round(sum(loads) / len(loads), 1)

        m = _TEGRA_RE["gr3d"].search(line)
        if m:
            stats["gpu_pct"] = int(m.group(1))

        m = _TEGRA_RE["temp_cpu"].search(line)
        if m:
            stats["cpu_temp_c"] = float(m.group(1))
        m = _TEGRA_RE["temp_gpu"].search(line)
        if m:
            stats["gpu_temp_c"] = float(m.group(1))
        m = _TEGRA_RE["temp_tj"].search(line)
        if m:
            stats["junction_temp_c"] = float(m.group(1))
        m = _TEGRA_RE["power_in"].search(line)
        if m:
            stats["power_in_w"] = round(int(m.group(1)) / 1000.0, 2)

        oc = _oc_events()
        if oc is not None:
            if self._oc_base is None:
                self._oc_base = oc
            if self._oc_last is not None and oc > self._oc_last:
                self._oc_changed_at = time.time()
            self._oc_last = oc
            stats["overcurrent_events"] = oc - self._oc_base

        stats["ts"] = time.time()
        with self._lock:
            self._stats = stats
        self._log_changes()

    def problems(self) -> list[str]:
        """Plain warnings for what is wrong right now (empty = fine)."""
        s = self.snapshot()
        out = []
        hottest = max((s.get(k) or 0.0) for k in
                      ("junction_temp_c", "cpu_temp_c", "gpu_temp_c"))
        if hottest >= settings.thermal_warn_c:
            out.append(f"running hot: {hottest:.0f} °C (warning at "
                       f"{settings.thermal_warn_c:.0f} °C) — check the fan and airflow")
        if self._oc_changed_at and time.time() - self._oc_changed_at < _OC_WINDOW_SECS:
            out.append(f"over-current: the board slowed itself "
                       f"({s.get('overcurrent_events', 0)} events since start) — "
                       "check the power adapter or lower the power mode")
        return out

    def _log_changes(self) -> None:
        now = set(self.problems())
        for p in now - self._warned:
            logger.warning("system: %s", p)
        for p in self._warned - now:
            logger.info("system: back to normal (%s)", p.split(":")[0])
        self._warned = now
