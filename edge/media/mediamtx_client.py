from __future__ import annotations

"""
Thin client for the local MediaMTX control + playback APIs.

MediaMTX is the media backbone: it connects to each camera once and re-serves
the compressed stream to every consumer (AI, WebRTC/HLS live view, recorder)
without re-encoding. This client is how the Python side talks to it:

  control API (127.0.0.1:{api_port}/v3)   — add/patch/remove paths, toggle
                                            per-path recording, read path state
  playback API (127.0.0.1:{playback_port}) — list recorded segments, fetch any
                                            time-slice as a playable MP4

Both APIs are bound to localhost by the generated config — nothing here is
reachable from the network. All calls are short-timeout and raise
MediaMTXError on failure so callers can degrade gracefully.
"""

import logging
import re
import shutil
from pathlib import Path

import requests

from common.rtsp import normalize_rtsp_url
from config.settings import settings


logger = logging.getLogger("media")

_TIMEOUT = 3.0
_EDGE_ROOT = Path(__file__).resolve().parents[1]


class MediaMTXError(Exception):
    """MediaMTX unreachable or a call failed."""


def path_name(camera_id: str) -> str:
    """A camera_id sanitized into a valid MediaMTX path name."""
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", (camera_id or "").strip()) or "cam"


# ---- AAC-audio recording (per-camera FFmpeg republish) ----------------

_FFMPEG_OK: bool | None = None


def ffmpeg_available() -> bool:
    """Is the ffmpeg binary present? Checked once per process, loud when not."""
    global _FFMPEG_OK
    if _FFMPEG_OK is None:
        _FFMPEG_OK = shutil.which(settings.ffmpeg_binary) is not None
        if not _FFMPEG_OK:
            logger.warning(
                "ffmpeg not found ('%s') — recordings fall back to VIDEO-ONLY "
                "(no AAC audio). Install ffmpeg on the device.",
                settings.ffmpeg_binary)
    return _FFMPEG_OK


def audio_transcode_active() -> bool:
    """AAC-audio recordings are enabled AND ffmpeg exists to do the work."""
    return bool(settings.record_audio) and ffmpeg_available()


def aac_republish_cmd(src_path: str) -> str:
    """The per-camera FFmpeg command MediaMTX supervises (runOnInit): pull the
    record stream over loopback TCP, COPY the video untouched, re-encode only
    the camera's G.711/PCM audio to AAC (16 kHz mono, 32 kbps), and publish it
    back as <src>-aac — the path that actually gets recorded. The '?' on the
    audio map keeps mic-less cameras publishing video-only instead of dying."""
    port = settings.mediamtx_rtsp_port
    return (f"{settings.ffmpeg_binary} -hide_banner -loglevel warning "
            f"-rtsp_transport tcp -i rtsp://127.0.0.1:{port}/{src_path} "
            f"-map 0:v:0 -map 0:a:0? -c:v copy "
            f"-c:a aac -ar 16000 -ac 1 -b:a 32k "
            f"-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:{port}/{src_path}-aac")


def record_source_name(camera) -> str:
    """The path whose stream feeds recording, BEFORE the audio step: the
    dedicated -rec path (the camera's second ONVIF profile, compact 1080p
    H.264) when configured, else the main path (recorded as-is)."""
    base = path_name(camera.camera_id)
    return f"{base}-rec" if getattr(camera, "record_rtsp_url", None) else base


def record_path_name(camera) -> str:
    """The path that actually gets RECORDED for this camera: the -aac republish
    (video copied, audio AAC — plays everywhere) when AAC audio is active, else
    the record source itself (video-only clips)."""
    src = record_source_name(camera)
    return f"{src}-aac" if audio_transcode_active() else src


# ---- URLs handed to consumers ---------------------------------------

def local_rtsp_url(camera_id: str) -> str:
    """The localhost restream the AI ingestion reads (loopback TCP)."""
    return f"rtsp://127.0.0.1:{settings.mediamtx_rtsp_port}/{path_name(camera_id)}"


def tls_enabled() -> bool:
    """True when the installer-generated cert pair exists — the SAME check the
    supervisor makes when writing mediamtx.yml, so the scheme we advertise in
    live links always matches what MediaMTX actually serves. (An https link
    pointing at a plaintext port is one of the ways links go dead.)"""
    cert_dir = Path(settings.mediamtx_cert_dir)
    cert_dir = cert_dir if cert_dir.is_absolute() else (_EDGE_ROOT / cert_dir)
    return (cert_dir / "server.crt").is_file() and (cert_dir / "server.key").is_file()


def stream_base(host: str) -> str:
    """`scheme://host` for the live links sent to the cloud — THE one base
    builder (used by the account camera-sync and tests/test_cloud.py).
    DEVICE_STREAM_BASE (a reverse proxy) wins when set; otherwise the device
    host with the scheme MediaMTX is really serving (https only when the
    cert pair exists)."""
    override = settings.device_stream_base.strip()
    if override:
        return (override.rstrip("/")
                .replace("wss://", "https://").replace("ws://", "http://"))
    host = (host or "localhost").rsplit(":", 1)[0]        # strip any port
    return f"{'https' if tls_enabled() else 'http'}://{host}"


def webrtc_url(camera_id: str, public_base: str) -> str:
    """The WebRTC page for one camera — the live link sent to the cloud.
    `public_base` is scheme://host (no port, see stream_base); the WebRTC port
    is appended unless the base already carries an explicit path (reverse
    proxy)."""
    base = public_base.rstrip("/")
    name = path_name(camera_id)
    # A reverse-proxy base like https://edge.example.com/mtx keeps its path;
    # a bare host gets the WebRTC port appended.
    host_only = re.match(r"^https?://[^/]+$", base or "")
    if host_only:
        host = base.split("://", 1)[1].rsplit(":", 1)[0]
        scheme = base.split("://", 1)[0]
        return f"{scheme}://{host}:{settings.mediamtx_webrtc_port}/{name}"
    return f"{base}/{name}"


# ---- control API ------------------------------------------------------

def _api(path: str) -> str:
    return f"http://127.0.0.1:{settings.mediamtx_api_port}/v3{path}"


def is_up() -> bool:
    try:
        return requests.get(_api("/config/global/get"), timeout=1.0).ok
    except requests.RequestException:
        return False


def _path_config(source_url: str) -> dict:
    return {
        "source": normalize_rtsp_url(source_url),
        "sourceOnDemand": False,     # stay connected: AI + recorder always need it
        "record": False,             # recording is toggled at runtime per person
        # Pull the camera over interleaved TCP. The default (UDP-first) drops the
        # large fragmented packets of a 4K / H.265 stream, so the feed shows
        # blank/no frames while a smaller sub-stream works. `rtspTransport` is
        # the v1.9.3 per-path source key (verified against the reference config).
        "rtspTransport": "tcp",
    }


def _sync_path_config(name: str, cfg: dict, desc: str = "") -> None:
    try:
        r = requests.post(_api(f"/config/paths/add/{name}"), json=cfg,
                          timeout=_TIMEOUT)
        if r.status_code == 400 and "already" in r.text.lower():
            r = requests.patch(_api(f"/config/paths/patch/{name}"), json=cfg,
                               timeout=_TIMEOUT)
        if not r.ok:
            raise MediaMTXError(f"sync path {name}: HTTP {r.status_code} {r.text[:200]}")
    except requests.RequestException as exc:
        raise MediaMTXError(f"sync path {name}: {exc}") from exc
    logger.info("MediaMTX path synced: %s %s", name, desc)


def _sync_named_path(name: str, source_url: str) -> None:
    _sync_path_config(name, _path_config(source_url), f"<- {source_url}")


def _aac_path_config(src_path: str) -> dict:
    """Publisher path fed by the MediaMTX-supervised FFmpeg republish — no
    `source` key: FFmpeg ANNOUNCEs into it; runOnInitRestart respawns it."""
    return {
        "runOnInit": aac_republish_cmd(src_path),
        "runOnInitRestart": True,
        "record": False,             # flipped at runtime on person detection
    }


def _remove_named_path(name: str) -> None:
    try:
        r = requests.delete(_api(f"/config/paths/delete/{name}"), timeout=_TIMEOUT)
        if not r.ok and r.status_code != 404:
            raise MediaMTXError(f"remove path {name}: HTTP {r.status_code}")
    except requests.RequestException as exc:
        raise MediaMTXError(f"remove path {name}: {exc}") from exc
    logger.info("MediaMTX path removed: %s", name)


def sync_camera(camera) -> None:
    """Mirror one camera into MediaMTX: main path always; a -rec path when a
    dedicated recording stream is configured (removed again if it no longer
    is); and the -aac republish path that actually gets recorded when AAC
    audio is active (stale variants cleaned up on every sync)."""
    base = path_name(camera.camera_id)
    _sync_named_path(base, camera.rtsp_url)
    rec = getattr(camera, "record_rtsp_url", None)
    if rec:
        _sync_named_path(f"{base}-rec", rec)
    else:
        try:
            _remove_named_path(f"{base}-rec")
        except MediaMTXError:
            pass                                 # never existed — fine
    current = f"{record_source_name(camera)}-aac" if audio_transcode_active() else None
    for cand in (f"{base}-aac", f"{base}-rec-aac"):
        if cand == current:
            _sync_path_config(cand, _aac_path_config(record_source_name(camera)),
                              "(AAC republish)")
        else:
            try:
                _remove_named_path(cand)
            except MediaMTXError:
                pass


def remove_camera(camera_id: str) -> None:
    base = path_name(camera_id)
    _remove_named_path(base)
    for extra in (f"{base}-rec", f"{base}-aac", f"{base}-rec-aac"):
        try:
            _remove_named_path(extra)
        except MediaMTXError:
            pass


def set_record(path: str, on: bool) -> None:
    """Toggle disk recording for one MediaMTX path at runtime."""
    try:
        r = requests.patch(_api(f"/config/paths/patch/{path}"),
                           json={"record": bool(on)}, timeout=_TIMEOUT)
        if not r.ok:
            raise MediaMTXError(f"record {path}: HTTP {r.status_code} {r.text[:200]}")
    except requests.RequestException as exc:
        raise MediaMTXError(f"record {path}: {exc}") from exc


def path_info(camera_id: str) -> dict | None:
    """Runtime state of a path (ready, tracks/codecs, readers) or None."""
    try:
        r = requests.get(_api(f"/paths/get/{path_name(camera_id)}"), timeout=_TIMEOUT)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def path_codec(camera_id: str) -> str | None:
    """'h264' / 'h265' as reported by the path's live tracks, else None."""
    info = path_info(camera_id)
    for t in (info or {}).get("tracks", []):
        tl = str(t).lower()
        if "265" in tl:
            return "h265"
        if "264" in tl:
            return "h264"
    return None


# ---- playback API (recordings) ---------------------------------------

def _playback(path: str) -> str:
    return f"http://127.0.0.1:{settings.mediamtx_playback_port}{path}"


def list_recordings(path: str) -> list[dict]:
    """Recorded time-ranges for one path: [{start, duration}, ...]."""
    try:
        r = requests.get(_playback("/list"), params={"path": path},
                         timeout=_TIMEOUT)
        if r.status_code == 404:
            return []
        if not r.ok:
            raise MediaMTXError(f"list recordings: HTTP {r.status_code}")
        return r.json() or []
    except (requests.RequestException, ValueError) as exc:
        raise MediaMTXError(f"list recordings: {exc}") from exc
