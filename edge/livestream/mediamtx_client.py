from __future__ import annotations

"""
Thin client for the local MediaMTX control API.

MediaMTX is the media backbone: it connects to each camera once and re-serves
the compressed stream to every consumer (AI, WebRTC/HLS live view, recorder)
without re-encoding. This client is how the Python side talks to it:

  control API (127.0.0.1:{api_port}/v3) — add/patch/remove paths, toggle
                                          per-path recording, read path state

Recorded footage is NOT read back through MediaMTX: the recorded segments are
served straight off disk (see recording/index), so there is no playback server
here. The control API is bound to localhost by the generated config — nothing
here is reachable from the network. All calls are short-timeout and raise
MediaMTXError on failure so callers can degrade gracefully.

Fleet routing (live links): every public live link is
`https://<domain>/<edge_id>/<cam>/whep`. The `<edge_id>` first segment is what
frp routes on (locations=["/<edge_id>"]) so one shared domain + one shared port
serve every house, disambiguated purely by that segment. MediaMTX therefore has
to serve the camera under the slash path `<edge_id>/<cam>` (stream_path). Only
the LIVE path carries the prefix; recording path names stay slash-free so the
recorder and its runtime record-toggle never depend on slash-paths.
"""

import logging
import re
import shutil
from pathlib import Path
from urllib.parse import quote

import requests

from common.rtsp import normalize_rtsp_url
from config.settings import settings


logger = logging.getLogger("media")

_TIMEOUT = 3.0
_EDGE_ROOT = Path(__file__).resolve().parents[1]


class MediaMTXError(Exception):
    """MediaMTX unreachable or a call failed."""


def path_name(camera_id: str) -> str:
    """A camera_id sanitized into a valid MediaMTX path name (slash-free)."""
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", (camera_id or "").strip()) or "cam"


def edge_prefix() -> str:
    """`<edge_id>/` when this device carries a fleet routing token, else ''.
    The token is the FIRST path segment of every public live link and the key
    frp routes on (locations=["/<edge_id>"]) — one shared domain, one shared
    port, disambiguated purely by this segment. Resolved from the account
    (edgeId handed back by the app server) first, then jetson.env's EDGE_ID, so a
    freshly-verified device routes with no restart. Empty = LAN-only, no prefix.

    The value is already canonical (normalized once at account ingest by
    account_config.canonical_edge_id) — it is used verbatim here so the segment
    frp routes on and the segment MediaMTX serves are byte-identical to the
    backend's deviceToken. No second transform: see [[ceravis-devicetoken-verbatim]]."""
    from configuration.account_config import effective_edge_id
    eid = effective_edge_id()
    return f"{eid}/" if eid else ""


def stream_path(camera_id: str) -> str:
    """The MediaMTX path name for a camera's LIVE stream: '<edge_id>/<cam>' on a
    fleet device, else '<cam>'. This ONE path is what the AI reads over local
    RTSP AND what the public WebRTC link addresses, so the '<edge_id>/' segment
    frp routes on and the segment MediaMTX serves are always identical."""
    return f"{edge_prefix()}{path_name(camera_id)}"


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


def aac_republish_cmd(src_path: str, out_path: str) -> str:
    """The per-camera FFmpeg command MediaMTX supervises (runOnInit): pull the
    record source over loopback TCP, COPY the video untouched, re-encode only
    the camera's G.711/PCM audio to AAC (16 kHz mono, 32 kbps), and publish it
    to out_path — the slash-free path that actually gets recorded. The '?' on
    the audio map keeps mic-less cameras publishing video-only instead of
    dying. (src_path may be the slash live path; RTSP handles slashes fine.)"""
    port = settings.mediamtx_rtsp_port
    return (f"{settings.ffmpeg_binary} -hide_banner -loglevel warning "
            f"-rtsp_transport tcp -i rtsp://127.0.0.1:{port}/{src_path} "
            f"-map 0:v:0 -map 0:a:0? -c:v copy "
            f"-c:a aac -ar 16000 -ac 1 -b:a 32k "
            f"-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:{port}/{out_path}")


def _rec_stub(camera) -> str:
    """Slash-free base for this camera's recording artifacts: '<cam>-rec' when a
    dedicated second-profile stream exists, else '<cam>'. Never carries the
    edge_id prefix, so recorded path names (and the runtime record-toggle) stay
    slash-free regardless of fleet mode."""
    base = path_name(camera.camera_id)
    return f"{base}-rec" if getattr(camera, "record_rtsp_url", None) else base


def record_source_name(camera) -> str:
    """The path whose stream FEEDS recording, before the audio step: the
    dedicated -rec path (the camera's second ONVIF profile, compact 1080p
    H.264) when configured; otherwise the LIVE main path (stream_path) is reused
    as the source — no extra camera pull. This is only ever a PULL source
    (loopback RTSP), so a slash here is harmless."""
    if getattr(camera, "record_rtsp_url", None):
        return f"{path_name(camera.camera_id)}-rec"
    return stream_path(camera.camera_id)


def record_path_name(camera) -> str:
    """The path that actually gets RECORDED (and read back for playback): the
    slash-free -aac republish when AAC audio is active (video copied, audio AAC
    — plays everywhere), else the record source. With AAC active this is always
    slash-free, so set_record toggles a plain path name and recordings land in a
    flat directory; the only slash case is the fallback of recording the live
    path directly (no ffmpeg AND no second profile), which the control API
    handles by percent-encoding."""
    if audio_transcode_active():
        return f"{_rec_stub(camera)}-aac"
    return record_source_name(camera)


# ---- URLs handed to consumers ---------------------------------------

def local_rtsp_url(camera_id: str) -> str:
    """The localhost restream the AI ingestion reads (loopback TCP). Reads the
    LIVE path (stream_path) so the AI and the public WebRTC link share it."""
    return f"rtsp://127.0.0.1:{settings.mediamtx_rtsp_port}/{stream_path(camera_id)}"


def tls_enabled() -> bool:
    """Whether MediaMTX serves HLS/WebRTC over HTTPS on this device.

    Fleet/proxy mode (DEVICE_STREAM_BASE set): the cloud Caddy terminates TLS and
    the edge is reached over a PLAIN-HTTP frp tunnel, so the edge MUST serve
    plaintext — return False even if LAN certs happen to exist (serving HTTPS
    here would break the http-type tunnel). This one rule keeps "who terminates
    TLS" unambiguous: the proxy when there is one, else the edge.

    LAN-direct (no DEVICE_STREAM_BASE): serve HTTPS when the installer-generated
    cert pair exists. This is the SAME check the supervisor makes when writing
    mediamtx.yml, so the scheme advertised in LAN links always matches what
    MediaMTX actually serves."""
    if settings.device_stream_base.strip():
        return False
    cert_dir = Path(settings.mediamtx_cert_dir)
    cert_dir = cert_dir if cert_dir.is_absolute() else (_EDGE_ROOT / cert_dir)
    return (cert_dir / "server.crt").is_file() and (cert_dir / "server.key").is_file()


def stream_base(host: str) -> str:
    """`scheme://host` for the live links sent to the cloud — THE one base
    builder (used by the account camera-sync and tests/test_cloud.py).
    DEVICE_STREAM_BASE (the fleet domain / reverse proxy) wins when set;
    otherwise the device host with the scheme MediaMTX is really serving (https
    only when the cert pair exists)."""
    override = settings.device_stream_base.strip()
    if override:
        return (override.rstrip("/")
                .replace("wss://", "https://").replace("ws://", "http://"))
    host = (host or "localhost").rsplit(":", 1)[0]        # strip any port
    return f"{'https' if tls_enabled() else 'http'}://{host}"


def webrtc_url(camera_id: str, public_base: str) -> str:
    """The public WebRTC live link for one camera — the URL stored on the app
    server and in cameras.json. It is the WebRTC BASE with NO trailing slash, so
    the cloud's WHEP player forms the endpoint cleanly as `<url>/whep` (a trailing
    slash would produce `…//whep`). `public_base` is scheme://host (no port, see
    stream_base).

    Fleet/proxy model (DEVICE_STREAM_BASE set): `<base>/<edge_id>/<ROOM>` — TLS
    and the /<edge_id> path routing terminate upstream (Caddy -> frps vhost ->
    frpc -> MediaMTX), so one shared domain serves every house.

    LAN-direct (no DEVICE_STREAM_BASE): hits MediaMTX's WebRTC port on this host
    directly, `<scheme>://<host>:<webrtc_port>/<ROOM>`."""
    base = public_base.rstrip("/")
    path = stream_path(camera_id)
    if settings.device_stream_base.strip():
        return f"{base}/{path}"
    scheme, _, host = base.partition("://")
    scheme = scheme or "http"
    host = (host or base).rsplit(":", 1)[0]
    return f"{scheme}://{host}:{settings.mediamtx_webrtc_port}/{path}"


# ---- control API ------------------------------------------------------

def _api(path: str) -> str:
    return f"http://127.0.0.1:{settings.mediamtx_api_port}/v3{path}"


def _enc(name: str) -> str:
    """Percent-encode a MediaMTX path NAME for the control-API URL. The live
    path carries a '/' (`<edge_id>/<cam>`); left raw it would be read as a URL
    separator and break the endpoint, so slashes become %2F."""
    return quote(name, safe="")


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
        r = requests.post(_api(f"/config/paths/add/{_enc(name)}"), json=cfg,
                          timeout=_TIMEOUT)
        if r.status_code == 400 and "already" in r.text.lower():
            r = requests.patch(_api(f"/config/paths/patch/{_enc(name)}"), json=cfg,
                               timeout=_TIMEOUT)
        if not r.ok:
            raise MediaMTXError(f"sync path {name}: HTTP {r.status_code} {r.text[:200]}")
    except requests.RequestException as exc:
        raise MediaMTXError(f"sync path {name}: {exc}") from exc
    logger.info("MediaMTX path synced: %s %s", name, desc)


def _sync_named_path(name: str, source_url: str) -> None:
    _sync_path_config(name, _path_config(source_url), f"<- {source_url}")


def _aac_path_config(src_path: str, out_path: str) -> dict:
    """Publisher path fed by the MediaMTX-supervised FFmpeg republish — no
    `source` key: FFmpeg ANNOUNCEs into it; runOnInitRestart respawns it."""
    return {
        "runOnInit": aac_republish_cmd(src_path, out_path),
        "runOnInitRestart": True,
        "record": False,             # flipped at runtime on person detection
    }


def _remove_named_path(name: str) -> None:
    try:
        r = requests.delete(_api(f"/config/paths/delete/{_enc(name)}"), timeout=_TIMEOUT)
        if not r.ok and r.status_code != 404:
            raise MediaMTXError(f"remove path {name}: HTTP {r.status_code}")
    except requests.RequestException as exc:
        raise MediaMTXError(f"remove path {name}: {exc}") from exc
    logger.info("MediaMTX path removed: %s", name)


def sync_camera(camera) -> None:
    """Mirror one camera into MediaMTX: the LIVE main path (stream_path, carrying
    the edge_id prefix) always; a -rec path when a dedicated recording stream is
    configured (removed again if it no longer is); and the slash-free -aac
    republish path that actually gets recorded when AAC audio is active (stale
    variants cleaned up on every sync)."""
    main = stream_path(camera.camera_id)
    _sync_named_path(main, camera.rtsp_url)

    base = path_name(camera.camera_id)                 # slash-free recording base
    rec = getattr(camera, "record_rtsp_url", None)
    if rec:
        _sync_named_path(f"{base}-rec", rec)
    else:
        try:
            _remove_named_path(f"{base}-rec")
        except MediaMTXError:
            pass                                       # never existed — fine

    current_aac = record_path_name(camera) if audio_transcode_active() else None
    for cand in (f"{base}-aac", f"{base}-rec-aac"):
        if cand == current_aac:
            _sync_path_config(cand,
                              _aac_path_config(record_source_name(camera), cand),
                              "(AAC republish)")
        else:
            try:
                _remove_named_path(cand)
            except MediaMTXError:
                pass


def remove_camera(camera_id: str) -> None:
    _remove_named_path(stream_path(camera_id))
    base = path_name(camera_id)
    for extra in (f"{base}-rec", f"{base}-aac", f"{base}-rec-aac"):
        try:
            _remove_named_path(extra)
        except MediaMTXError:
            pass


def set_record(path: str, on: bool) -> None:
    """Toggle disk recording for one MediaMTX path at runtime."""
    try:
        r = requests.patch(_api(f"/config/paths/patch/{_enc(path)}"),
                           json={"record": bool(on)}, timeout=_TIMEOUT)
        if not r.ok:
            raise MediaMTXError(f"record {path}: HTTP {r.status_code} {r.text[:200]}")
    except requests.RequestException as exc:
        raise MediaMTXError(f"record {path}: {exc}") from exc


def path_info(camera_id: str) -> dict | None:
    """Runtime state of the LIVE path (ready, tracks/codecs, readers) or None."""
    try:
        r = requests.get(_api(f"/paths/get/{_enc(stream_path(camera_id))}"),
                         timeout=_TIMEOUT)
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
