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

from urllib.parse import quote, unquote, urlsplit, urlunsplit


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
