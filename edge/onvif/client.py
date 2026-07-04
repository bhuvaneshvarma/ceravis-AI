from __future__ import annotations

"""
Per-camera ONVIF client: device info, media profiles, RTSP stream URIs,
recording-profile standardization, and PTZ.

An ONVIF camera exposes several "media profiles" — independent encoders on the
same sensor. Typically profile 1 is the MAIN stream (full native quality) and
profile 2+ are SUB streams (smaller). CERAVIS policy (per product decision):

  • MAIN stream: NEVER modified. The AI, WebRTC live links and HLS all consume
    it at the camera's raw native quality via MediaMTX.
  • RECORD stream: the SECOND profile, standardized to ~1080p when the camera
    supports it (SetVideoEncoderConfiguration) — recorded as its own MediaMTX
    path, still remux-only. If there is no usable second profile, recording
    falls back to the main stream untouched.
"""

import logging
from dataclasses import dataclass
from urllib.parse import quote, urlparse, urlunparse
from xml.etree import ElementTree

from onvif.soap import OnvifError, call


logger = logging.getLogger("onvif")

_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"


@dataclass
class Profile:
    token: str
    name: str
    encoding: str          # H264 / H265 / JPEG
    width: int
    height: int
    fps: float
    encoder_token: str     # VideoEncoderConfiguration token (for standardizing)
    has_ptz: bool


def with_credentials(rtsp_url: str, username: str, password: str) -> str:
    """Embed the camera credentials in an RTSP URI (what MediaMTX will dial)."""
    if not username:
        return rtsp_url
    p = urlparse(rtsp_url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    cred = f"{quote(username, safe='')}:{quote(password or '', safe='')}"
    return urlunparse(p._replace(netloc=f"{cred}@{host}"))


class OnvifCamera:
    def __init__(self, xaddr: str, username: str = "", password: str = "") -> None:
        self.xaddr = xaddr
        self.username = username
        self.password = password
        self._media_url: str | None = None
        self._ptz_url: str | None = None

    def _call(self, url: str, body: str) -> ElementTree.Element:
        return call(url, body, self.username, self.password)

    # ---- service resolution -----------------------------------------
    def _services(self) -> None:
        """Resolve the media/PTZ service URLs (GetServices, fallback default)."""
        if self._media_url is not None:
            return
        try:
            body = self._call(
                self.xaddr,
                '<GetServices xmlns="http://www.onvif.org/ver10/device/wsdl">'
                "<IncludeCapability>false</IncludeCapability></GetServices>")
            for svc in body.findall(".//Service"):
                ns = svc.findtext("Namespace") or ""
                url = svc.findtext("XAddr") or ""
                if _MEDIA_NS in ns:
                    self._media_url = url
                elif _PTZ_NS in ns:
                    self._ptz_url = url
        except OnvifError as exc:
            logger.info("GetServices failed (%s) — using device xaddr", exc)
        if not self._media_url:
            self._media_url = self.xaddr        # many cameras accept media
            self._ptz_url = self._ptz_url      # calls on the device endpoint

    # ---- device ------------------------------------------------------
    def device_info(self) -> dict:
        body = self._call(
            self.xaddr,
            '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        return {
            "manufacturer": body.findtext(".//Manufacturer") or "",
            "model": body.findtext(".//Model") or "",
            "serial": body.findtext(".//SerialNumber") or "",
        }

    # ---- media profiles ------------------------------------------------
    def profiles(self) -> list[Profile]:
        self._services()
        body = self._call(self._media_url,
                          f'<GetProfiles xmlns="{_MEDIA_NS}"/>')
        out: list[Profile] = []
        for p in body.findall(".//Profiles"):
            enc = p.find(".//VideoEncoderConfiguration")
            if enc is None:
                continue
            res = enc.find("Resolution")
            rate = enc.find("RateControl")
            out.append(Profile(
                token=p.get("token") or "",
                name=p.findtext("Name") or "",
                encoding=(enc.findtext("Encoding") or "").upper(),
                width=int((res.findtext("Width") if res is not None else 0) or 0),
                height=int((res.findtext("Height") if res is not None else 0) or 0),
                fps=float((rate.findtext("FrameRateLimit")
                           if rate is not None else 0) or 0),
                encoder_token=enc.get("token") or "",
                has_ptz=p.find(".//PTZConfiguration") is not None,
            ))
        return out

    def stream_uri(self, profile_token: str) -> str:
        """The RTSP URI for one profile, credentials embedded."""
        self._services()
        body = self._call(self._media_url, f"""
<GetStreamUri xmlns="{_MEDIA_NS}">
  <StreamSetup>
    <Stream xmlns="{_SCHEMA_NS}">RTP-Unicast</Stream>
    <Transport xmlns="{_SCHEMA_NS}"><Protocol>RTSP</Protocol></Transport>
  </StreamSetup>
  <ProfileToken>{profile_token}</ProfileToken>
</GetStreamUri>""")
        uri = body.findtext(".//Uri") or ""
        if not uri:
            raise OnvifError("camera returned no stream URI")
        return with_credentials(uri, self.username, self.password)

    # ---- recording-profile standardization ------------------------------
    def standardize_record_profile(self, profile: Profile,
                                   width: int = 1920, height: int = 1080) -> bool:
        """Best-effort: set a SUB profile's encoder to ~1080p for recording.
        Only ever called on a non-main profile; checks the camera's advertised
        options first and picks the closest supported resolution <= target.
        Returns True if the encoder now sits at the chosen resolution."""
        self._services()
        try:
            opts = self._call(self._media_url, f"""
<GetVideoEncoderConfigurationOptions xmlns="{_MEDIA_NS}">
  <ConfigurationToken>{profile.encoder_token}</ConfigurationToken>
</GetVideoEncoderConfigurationOptions>""")
            resolutions = []
            for r in opts.findall(".//ResolutionsAvailable"):
                w = int(r.findtext("Width") or 0)
                h = int(r.findtext("Height") or 0)
                if 0 < h <= height and 0 < w <= width:
                    resolutions.append((w, h))
            if not resolutions:
                return False
            best_w, best_h = max(resolutions, key=lambda wh: wh[0] * wh[1])
            if (best_w, best_h) == (profile.width, profile.height):
                return True                       # already there

            cfg = self._call(self._media_url, f"""
<GetVideoEncoderConfiguration xmlns="{_MEDIA_NS}">
  <ConfigurationToken>{profile.encoder_token}</ConfigurationToken>
</GetVideoEncoderConfiguration>""").find(".//Configuration")
            if cfg is None:
                return False
            self._call(self._media_url, f"""
<SetVideoEncoderConfiguration xmlns="{_MEDIA_NS}">
  <Configuration token="{profile.encoder_token}">
    <Name xmlns="{_SCHEMA_NS}">{cfg.findtext("Name") or "record"}</Name>
    <UseCount xmlns="{_SCHEMA_NS}">{cfg.findtext("UseCount") or 1}</UseCount>
    <Encoding xmlns="{_SCHEMA_NS}">{cfg.findtext("Encoding") or "H264"}</Encoding>
    <Resolution xmlns="{_SCHEMA_NS}">
      <Width>{best_w}</Width><Height>{best_h}</Height>
    </Resolution>
    <Quality xmlns="{_SCHEMA_NS}">{cfg.findtext("Quality") or 4}</Quality>
    <SessionTimeout xmlns="{_SCHEMA_NS}">{cfg.findtext("SessionTimeout") or "PT60S"}</SessionTimeout>
  </Configuration>
  <ForcePersistence>true</ForcePersistence>
</SetVideoEncoderConfiguration>""")
            logger.info("record profile %s standardized to %dx%d",
                        profile.token, best_w, best_h)
            return True
        except OnvifError as exc:
            logger.info("record-profile standardization skipped: %s", exc)
            return False

    # ---- PTZ -------------------------------------------------------------
    def ptz_move(self, profile_token: str,
                 pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0) -> None:
        """Start continuous motion; velocities are -1.0 .. 1.0. Call ptz_stop
        to halt (the UI sends move on press, stop on release)."""
        self._services()
        if not self._ptz_url:
            raise OnvifError("camera reports no PTZ service")
        self._call(self._ptz_url, f"""
<ContinuousMove xmlns="{_PTZ_NS}">
  <ProfileToken>{profile_token}</ProfileToken>
  <Velocity>
    <PanTilt x="{pan}" y="{tilt}" xmlns="{_SCHEMA_NS}"/>
    <Zoom x="{zoom}" xmlns="{_SCHEMA_NS}"/>
  </Velocity>
</ContinuousMove>""")

    def ptz_stop(self, profile_token: str) -> None:
        self._services()
        if not self._ptz_url:
            raise OnvifError("camera reports no PTZ service")
        self._call(self._ptz_url, f"""
<Stop xmlns="{_PTZ_NS}">
  <ProfileToken>{profile_token}</ProfileToken>
  <PanTilt>true</PanTilt><Zoom>true</Zoom>
</Stop>""")


# ---- probe: everything the wizard needs in one call ----------------------

def probe(xaddr: str, username: str, password: str,
          record_height: int = 1080) -> dict:
    """Interrogate one discovered camera. Returns device info + the chosen
    stream plan: main stream untouched (raw quality for AI + live links);
    a second profile standardized to <=record_height for recording when the
    camera has one, else recording falls back to the main stream."""
    cam = OnvifCamera(xaddr, username, password)
    info = cam.device_info()                    # also proves the credentials
    profs = cam.profiles()
    if not profs:
        raise OnvifError("camera exposes no media profiles")

    profs.sort(key=lambda p: p.width * p.height, reverse=True)
    main, subs = profs[0], profs[1:]

    record_uri = None
    record_profile = None
    for sub in subs:                            # best sub-profile for recording
        if cam.standardize_record_profile(sub, height=record_height):
            record_profile = sub
            break
    if record_profile is None and subs:
        # couldn't standardize but a sub exists — record it as-is only if it's
        # a sane recording quality; otherwise fall back to main.
        best_sub = max(subs, key=lambda p: p.width * p.height)
        if best_sub.height >= 720:
            record_profile = best_sub
    if record_profile is not None:
        record_uri = cam.stream_uri(record_profile.token)

    return {
        "device": info,
        "profiles": [vars(p) for p in profs],
        "main_uri": cam.stream_uri(main.token),
        "main_profile_token": main.token,
        "record_uri": record_uri,               # None -> record the main stream
        "ptz": any(p.has_ptz for p in profs),
    }
