from __future__ import annotations

"""
Runs MediaMTX as a supervised CHILD of this process — one systemd service
(ceravis.service) owns everything, the same pattern Frigate uses for go2rtc.

On start it writes data/mediamtx.yml from settings + the registered cameras,
spawns the binary, and a monitor thread respawns it (with backoff) if it ever
dies. stop() terminates it cleanly with the app.

TLS: if the installer-generated cert pair exists (data/certs/server.crt/.key),
HLS and WebRTC are served over HTTPS — that's the link shared to the cloud.
Without certs (dev box) they fall back to plain HTTP and everything still runs.

If the binary itself is missing, `available` stays False and the rest of the
app degrades gracefully: ingestion reads cameras directly, recording and the
shared live links are disabled with one clear log line.
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from config.settings import settings
from configuration.camera_config import CameraConfig
from integration import call_log
from media.mediamtx_client import is_up, path_name, tls_enabled


logger = logging.getLogger("media")

_EDGE_ROOT = Path(__file__).resolve().parents[1]


def _abs(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (_EDGE_ROOT / p)


class MediaMTXSupervisor:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._config_file = _abs(settings.data_path) / "mediamtx.yml"
        self._log_file = _abs(settings.data_path) / "mediamtx.log"
        self.available = False

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        binary = settings.mediamtx_binary
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            logger.warning(
                "MediaMTX binary not found at %s — media backbone disabled "
                "(direct camera reads; no recording, no shared live links). "
                "Run setup/install_mediamtx.sh on the device.", binary)
            # Loud on the monitor's sync console too — a dead backbone means
            # the live links pushed to the cloud are dead, silently.
            call_log.record(
                "event", False,
                label="Media backbone DISABLED — live links & recording are dead",
                error="mediamtx binary missing — run: bash setup/"
                      "install_mediamtx.sh, then sudo systemctl restart ceravis")
            return
        self.available = True
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mediamtx-supervisor")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        logger.info("MediaMTX stopped")

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def wait_ready(self, timeout: float = 15.0) -> bool:
        """Block until the control API answers (or timeout). Cameras get their
        readers started after this so the localhost restreams exist."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if is_up():
                return True
            time.sleep(0.3)
        return False

    # ---- child process -------------------------------------------------
    def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._write_config()
                # MediaMTX's own output goes to data/mediamtx.log — when it
                # dies on boot (bad config, port in use) the reason must be
                # readable, not swallowed by DEVNULL. Bounded: fresh past 5 MB.
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
                if (self._log_file.exists()
                        and self._log_file.stat().st_size > 5 * 1024 * 1024):
                    self._log_file.unlink()
                with open(self._log_file, "ab") as log:
                    self._proc = subprocess.Popen(
                        [settings.mediamtx_binary, str(self._config_file)],
                        stdout=log, stderr=subprocess.STDOUT,
                    )
                logger.info("MediaMTX started (pid %s, log: %s)",
                            self._proc.pid, self._log_file)
            except Exception:
                logger.exception("MediaMTX spawn failed")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            while self._running and self._proc.poll() is None:
                time.sleep(1.0)
            if self._running:                       # died — respawn
                logger.warning("MediaMTX exited (code %s) — restarting",
                               self._proc.returncode)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ---- config generation ----------------------------------------------
    def _write_config(self) -> None:
        """data/mediamtx.yml from settings + the currently registered cameras.
        Runtime changes (new camera, record on/off) go through the control API;
        this file is the state MediaMTX boots with."""
        crt = _abs(settings.mediamtx_cert_dir) / "server.crt"
        key = _abs(settings.mediamtx_cert_dir) / "server.key"
        tls = tls_enabled()     # one TLS decision — same check the links use

        record_path = _abs(settings.record_dir)
        record_path.mkdir(parents=True, exist_ok=True)

        lines = [
            "# GENERATED by CERAVIS (media/mediamtx_supervisor.py) — do not edit;",
            "# it is rewritten on every service start.",
            "logLevel: info",
            "",
            "api: yes",
            f"apiAddress: 127.0.0.1:{settings.mediamtx_api_port}",
            "",
            "playback: yes",
            f"playbackAddress: 127.0.0.1:{settings.mediamtx_playback_port}",
            "",
            "rtsp: yes",
            f"rtspAddress: :{settings.mediamtx_rtsp_port}",
            "rtspTransports: [tcp]",
            "",
            "hls: yes",
            f"hlsAddress: :{settings.mediamtx_hls_port}",
            f"hlsEncryption: {'yes' if tls else 'no'}",
            "",
            "webrtc: yes",
            f"webrtcAddress: :{settings.mediamtx_webrtc_port}",
            f"webrtcEncryption: {'yes' if tls else 'no'}",
            "",
            "rtmp: no",
            "srt: no",
            "",
        ]
        if tls:
            lines += [
                f"hlsServerCert: {crt}",
                f"hlsServerKey: {key}",
                f"webrtcServerCert: {crt}",
                f"webrtcServerKey: {key}",
                "",
            ]
        lines += [
            "pathDefaults:",
            f"  recordPath: {record_path}/%path/%Y-%m-%d_%H-%M-%S-%f",
            "  recordFormat: fmp4",
            f"  recordSegmentDuration: {settings.record_segment_secs}s",
            f"  recordDeleteAfter: {settings.record_retention_days * 24}h",
            "",
        ]
        cameras = [c for c in CameraConfig().get_all() if c.is_enabled]
        if cameras:
            lines.append("paths:")
            for cam in cameras:
                lines += [
                    f"  {path_name(cam.camera_id)}:",
                    f"    source: {cam.rtsp_url}",
                    "    sourceOnDemand: no",
                    "    record: no",         # flipped at runtime on person detection
                ]
                # Dedicated recording stream (second ONVIF profile @ ~1080p).
                # The main path above stays untouched at native quality.
                if getattr(cam, "record_rtsp_url", None):
                    lines += [
                        f"  {path_name(cam.camera_id)}-rec:",
                        f"    source: {cam.record_rtsp_url}",
                        "    sourceOnDemand: no",
                        "    record: no",
                    ]
        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        self._config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("MediaMTX config written (%d camera path(s), tls=%s)",
                    len(cameras), tls)
