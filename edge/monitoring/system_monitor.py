from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path


logger = logging.getLogger("monitoring")


_TEGRA_RE = {
    "ram": re.compile(r"RAM (\d+)/(\d+)MB"),
    "cpu": re.compile(r"CPU \[([^\]]+)\]"),
    "gr3d": re.compile(r"GR3D_FREQ (\d+)%"),
    "temp_cpu": re.compile(r"CPU@([\d.]+)C"),
    "temp_gpu": re.compile(r"GPU@([\d.]+)C"),
}


class SystemMonitor:
    """
    Polls Jetson tegrastats for CPU / GPU / RAM / temps every 2s.

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

        stats["ts"] = time.time()
        with self._lock:
            self._stats = stats
