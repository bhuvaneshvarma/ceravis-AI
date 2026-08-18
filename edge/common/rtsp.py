from __future__ import annotations

"""
Make user-entered RTSP URLs FFmpeg/GStreamer-safe — one owner for the reserved-
character problem that bites ONVIF onboarding: a camera password with an '@'
(or ':') put literally into the URL.

    rtsp://admin:p@ss@192.168.0.250:554/stream1

FFmpeg/libav split the authority on the FIRST '@', so they read the host as
"ss@192.168.0.250" and the connection fails — even though the URL "looks" right.
The correct form percent-encodes the '@' in the password ('%40') so exactly one
'@' separates credentials from host. normalize_rtsp_url() produces that form and
is IDEMPOTENT: an already-correct '...:p%40ss@...' is returned unchanged, never
double-encoded to '%2540'. So a save path can run it unconditionally.
"""

import os
import time

from urllib.parse import quote, unquote, urlsplit, urlunsplit


def grab_one_frame(url: str, timeout_secs: float = 8.0):
    """Open an RTSP URL over interleaved TCP and return ONE freshly decoded
    BGR frame (numpy array), or None.

    The single owner of "pull one live frame from a stream" — used by the 'Add
    camera' probe and by the live snapshot tier. Forces TCP transport (a 4K /
    H.265 main stream loses large fragmented packets over the default UDP and
    never yields a frame) and waits up to `timeout_secs` for the first keyframe
    to decode (H.265 only produces a frame at a keyframe), rather than giving up
    on the first empty read. cv2 is imported lazily so importing this module on a
    box without OpenCV (tests) stays cheap."""
    import cv2

    url = normalize_rtsp_url(url)
    if not url:
        return None
    # Interleaved TCP for the FFmpeg backend — same transport the AI ingest and
    # MediaMTX pull use, so the big-packet 4K/H.265 case behaves identically.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)     # freshest frame, not a backlog
    except cv2.error:
        pass
    frame = None
    deadline = time.time() + max(0.5, timeout_secs)
    while time.time() < deadline:
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
            break
        time.sleep(0.05)
    cap.release()
    return frame


def observe_stream(url: str, timeout_secs: float = 8.0) -> dict | None:
    """What a stream REALLY carries: {"codec", "width", "height"}, or None.

    The single owner of "don't take the camera's word for it". ONVIF cannot be
    trusted on the codec: its ver10 encoder schema has no H.265 element, so a
    camera streaming HEVC reports "H264" — and H.265 is not a detail, it is a
    black screen in every browser and recordings nobody can play. So before a
    profile is chosen, its actual bitstream is read.

    ffprobe rather than OpenCV: it reports codec_name directly, exits on its own,
    and never has to decode a frame. Missing ffprobe (or an unreachable stream)
    returns None, and the caller falls back to the ONVIF claim — /system/status
    still catches a bad choice from the live path afterwards."""
    import shutil
    import subprocess

    from config.settings import settings

    url = normalize_rtsp_url(url)
    if not url:
        return None
    exe = settings.ffmpeg_binary.replace("ffmpeg", "ffprobe")
    if not shutil.which(exe):
        return None
    cmd = [exe, "-v", "error", "-rtsp_transport", "tcp",
           "-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height",
           "-of", "default=nw=1:nk=1", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=max(2.0, timeout_secs))
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
    if out.returncode != 0 or len(lines) < 3:
        return None
    codec, width, height = lines[0], lines[1], lines[2]
    try:
        return {"codec": codec.lower(), "width": int(width), "height": int(height)}
    except ValueError:
        return None


def normalize_rtsp_url(url: str) -> str:
    """Percent-encode the userinfo of an RTSP URL so it parses correctly.
    Splits credentials from host on the LAST '@' (a compliant host has none) and
    user from password on the FIRST ':'. Encodes only reserved characters; a
    URL without credentials, or already encoded, comes back unchanged."""
    url = (url or "").strip()
    if "://" not in url or "@" not in url:
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" not in netloc:
        return url
    userinfo, _, hostport = netloc.rpartition("@")
    if not userinfo or not hostport:
        return url
    user, sep, pw = userinfo.partition(":")
    # unquote-then-quote is what makes this idempotent: a raw '@' becomes '%40',
    # while an existing '%40' decodes to '@' and re-encodes to the same '%40'.
    cred = quote(unquote(user), safe="")
    if sep:
        cred += ":" + quote(unquote(pw), safe="")
    return urlunsplit(parts._replace(netloc=f"{cred}@{hostport}"))
