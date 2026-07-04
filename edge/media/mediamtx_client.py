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

import requests

from config.settings import settings


logger = logging.getLogger("media")

_TIMEOUT = 3.0


class MediaMTXError(Exception):
    """MediaMTX unreachable or a call failed."""


def path_name(camera_id: str) -> str:
    """A camera_id sanitized into a valid MediaMTX path name."""
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", (camera_id or "").strip()) or "cam"


def record_path_name(camera) -> str:
    """The path that gets RECORDED for this camera: the dedicated -rec path
    (the camera's second ONVIF profile, standardized to ~1080p) when one is
    configured, else the main path (native quality, remuxed as-is)."""
    base = path_name(camera.camera_id)
    return f"{base}-rec" if getattr(camera, "record_rtsp_url", None) else base


# ---- URLs handed to consumers ---------------------------------------

def local_rtsp_url(camera_id: str) -> str:
    """The localhost restream the AI ingestion reads (loopback TCP)."""
    return f"rtsp://127.0.0.1:{settings.mediamtx_rtsp_port}/{path_name(camera_id)}"


def webrtc_url(camera_id: str, public_base: str) -> str:
    """The WebRTC page for one camera — the HTTPS live link sent to the cloud.
    `public_base` is scheme://host (no port); the WebRTC port is appended
    unless the base already carries an explicit path (reverse proxy)."""
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
        "source": source_url,
        "sourceOnDemand": False,     # stay connected: AI + recorder always need it
        "record": False,             # recording is toggled at runtime per person
    }


def _sync_named_path(name: str, source_url: str) -> None:
    cfg = _path_config(source_url)
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
    logger.info("MediaMTX path synced: %s <- %s", name, source_url)


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
    dedicated recording stream is configured (removed again if it no longer is)."""
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


def remove_camera(camera_id: str) -> None:
    base = path_name(camera_id)
    _remove_named_path(base)
    try:
        _remove_named_path(f"{base}-rec")
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


def open_clip(path: str, start: str, duration: float):
    """Stream a recorded slice as MP4. Returns the live requests.Response
    (stream=True) — the API layer forwards its chunks to the client."""
    try:
        r = requests.get(
            _playback("/get"),
            params={"path": path, "start": start,
                    "duration": duration, "format": "mp4"},
            stream=True, timeout=_TIMEOUT)
        if not r.ok:
            r.close()
            raise MediaMTXError(f"clip: HTTP {r.status_code}")
        return r
    except requests.RequestException as exc:
        raise MediaMTXError(f"clip: {exc}") from exc
