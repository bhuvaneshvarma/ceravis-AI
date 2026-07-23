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

from common.net import lan_ip
from common.rtsp import normalize_rtsp_url
from config.settings import settings
from configuration.camera_config import CameraConfig
from integration import call_log
from livestream.mediamtx_client import (
    aac_republish_cmd, audio_transcode_active, is_up, path_name,
    record_path_name, record_source_name, stream_path, tls_enabled,
)


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
            "# GENERATED by CERAVIS (livestream/mediamtx_supervisor.py) — do not edit;",
            "# it is rewritten on every service start.",
            "logLevel: info",
            "",
            "api: yes",
            f"apiAddress: 127.0.0.1:{settings.mediamtx_api_port}",
            "",
            # No playback server: playback reads the recorded segments straight
            # off disk (media/recording_index), so MediaMTX never re-serves them.
            "",
            "rtsp: yes",
            f"rtspAddress: :{settings.mediamtx_rtsp_port}",
            # MediaMTX's key for allowed RTSP transports is `protocols` — an
            # unknown key (e.g. `rtspTransports`) makes MediaMTX exit on boot
            # with "unknown field", so NOTHING binds and every live link dies.
            "protocols: [tcp]",
            "",
            "hls: yes",
            f"hlsAddress: :{settings.mediamtx_hls_port}",
            f"hlsEncryption: {'yes' if tls else 'no'}",
            "",
            "webrtc: yes",
            f"webrtcAddress: :{settings.mediamtx_webrtc_port}",
            # Hybrid transport, the CCTV model: SIGNALING is TCP (the WHEP
            # handshake on this HTTP port, carried through the frp tunnel), the
            # VIDEO is UDP and travels peer-to-peer (WebRTC/ICE) — it never
            # passes through the cloud. This one FIXED UDP port is that media
            # path; keeping it fixed keeps the home-NAT mapping stable so STUN
            # hole punching reaches off-LAN viewers.
            f"webrtcLocalUDPAddress: :{settings.mediamtx_webrtc_udp_port}",
            # Edge serves plaintext WebRTC; TLS terminates upstream at the cloud
            # Caddy in the fleet model (certs only exist here for LAN-direct).
            f"webrtcEncryption: {'yes' if tls else 'no'}",
        ]
        # WebRTC must advertise reachable ICE candidates. On the LAN the device's
        # own IP is that host; off-LAN, STUN adds the public reflexive candidate
        # so the UDP media can punch through home NAT straight to the viewer.
        ip = lan_ip()
        if ip:
            lines.append(f"webrtcAdditionalHosts: [{ip}]")
        stun = settings.mediamtx_stun_server.strip()
        if stun:
            lines += ["webrtcICEServers2:", f"  - url: {stun}"]
            # FUTURE (strict/symmetric NAT, ~10-20% of homes): add a TURN relay
            # as a SECOND ICE server so the minority that can't punch UDP falls
            # back to a relayed path — coturn on the same EC2 box, only that
            # minority relays media. Leave the shape here for when it lands:
            #   - url: turn:<host>:3478
            #     username: <user>
            #     password: <pass>
        lines += [
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
            # Pull every camera source over interleaved TCP — UDP-first drops the
            # big fragmented packets of 4K/H.265 feeds (blank/no frames).
            "  rtspTransport: tcp",
            f"  recordPath: {record_path}/%path/%Y-%m-%d_%H-%M-%S-%f",
            # MPEG-TS, not fmp4, on purpose: a TS segment is self-contained (no
            # separate init header), which makes each recorded file a VALID HLS
            # SEGMENT as-is. Playback therefore lists the stored files directly
            # instead of re-cutting a duplicate copy. See recording/index.
            "  recordFormat: mpegts",
            f"  recordSegmentDuration: {settings.record_segment_secs}s",
            # Rolling window: each person-clip self-deletes this many hours after
            # it was written, so the disk holds only the latest N hours of "who
            # was in frame" — never a full N hours of continuous footage.
            f"  recordDeleteAfter: {settings.record_retention_hours}h",
            "",
        ]
        cameras = [c for c in CameraConfig().get_all() if c.is_enabled]
        audio = audio_transcode_active()
        if cameras:
            lines.append("paths:")
            for cam in cameras:
                base = path_name(cam.camera_id)        # slash-free recording base
                live = stream_path(cam.camera_id)      # '<edge_id>/<cam>' live path
                # LIVE main path — what the AI reads (local RTSP) and the public
                # WebRTC link addresses. Quoted because the name carries a '/'.
                lines += [
                    f'  "{live}":',
                    f"    source: {normalize_rtsp_url(cam.rtsp_url)}",
                    "    sourceOnDemand: no",
                    "    record: no",         # flipped at runtime on person detection
                ]
                # Dedicated recording stream (second ONVIF profile @ ~1080p),
                # slash-free. The main path above stays untouched at native quality.
                rec = getattr(cam, "record_rtsp_url", None)
                if rec:
                    lines += [
                        f"  {base}-rec:",
                        f"    source: {normalize_rtsp_url(rec)}",
                        "    sourceOnDemand: no",
                        "    record: no",
                    ]
                # AAC republish — the slash-free path that actually gets recorded.
                # MediaMTX spawns + auto-restarts the FFmpeg (runOnInit); it copies
                # the video and re-encodes only the G.711 audio to AAC so clips are
                # H.264+AAC, the combo every player and browser accepts.
                if audio:
                    out = record_path_name(cam)        # slash-free <cam>[-rec]-aac
                    src = record_source_name(cam)      # live path or <cam>-rec
                    lines += [
                        f"  {out}:",
                        f"    runOnInit: {aac_republish_cmd(src, out)}",
                        "    runOnInitRestart: yes",
                        "    record: no",
                    ]
        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        self._config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("MediaMTX config written (%d camera path(s), tls=%s, "
                    "aac_audio=%s)", len(cameras), tls, audio)
