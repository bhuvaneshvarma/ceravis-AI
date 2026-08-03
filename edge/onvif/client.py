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
from datetime import datetime, timezone
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
        self._ptz_advertised = False           # camera exposes a PTZ service
        self._resolved = False

    def _call(self, url: str, body: str) -> ElementTree.Element:
        return call(url, body, self.username, self.password)

    # ---- service resolution -----------------------------------------
    def _services(self) -> None:
        """Resolve the media/PTZ service URLs. Tries GetServices, then the older
        GetCapabilities, and finally defaults any still-missing service to the
        device endpoint — many cheap cameras expose EVERY service on one URL
        (e.g. .../onvif/service), so a single endpoint answers all calls."""
        if self._resolved:
            return
        self._resolved = True
        try:
            body = self._call(
                self.xaddr,
                '<GetServices xmlns="http://www.onvif.org/ver10/device/wsdl">'
                "<IncludeCapability>false</IncludeCapability></GetServices>")
            for svc in body.findall(".//Service"):
                ns = svc.findtext("Namespace") or ""
                url = svc.findtext("XAddr") or ""
                if not url:
                    continue
                if _MEDIA_NS in ns:
                    self._media_url = url
                elif _PTZ_NS in ns:
                    self._ptz_url = url
                    self._ptz_advertised = True
        except OnvifError as exc:
            logger.info("GetServices failed (%s) — trying GetCapabilities", exc)
        if not (self._media_url and self._ptz_advertised):
            self._capabilities_fallback()
        # Single-endpoint cameras: default whatever is still unset to the device
        # xaddr so calls still go somewhere the camera answers.
        self._media_url = self._media_url or self.xaddr
        self._ptz_url = self._ptz_url or self._media_url

    def _capabilities_fallback(self) -> None:
        """Older cameras answer GetCapabilities but not GetServices. It reports
        the Media/PTZ service addresses and — crucially — whether PTZ exists."""
        try:
            body = self._call(
                self.xaddr,
                '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
                "<Category>All</Category></GetCapabilities>")
        except OnvifError as exc:
            logger.info("GetCapabilities failed: %s", exc)
            return
        media = body.find(".//Media/XAddr")
        ptz = body.find(".//PTZ/XAddr")
        if media is not None and media.text and not self._media_url:
            self._media_url = media.text
        if ptz is not None and ptz.text:
            self._ptz_url = self._ptz_url or ptz.text
            self._ptz_advertised = True
        elif body.find(".//PTZ") is not None:
            self._ptz_advertised = True

    def has_ptz_service(self) -> bool:
        """Whether the camera advertises a PTZ service. This is the reliable
        'can this camera pan/tilt/zoom' signal — far more so than a profile's
        embedded PTZConfiguration, which many PTZ cameras simply omit."""
        self._services()
        return self._ptz_advertised

    # ---- device ------------------------------------------------------
    def device_info(self) -> dict:
        """GetDeviceInformation — the camera's hardware identity. Maps onto the
        saveCamera record: manufacturer -> supplier, model -> model. There is NO
        friendly "device name" in this call (the ONVIF spec has none), so the
        edge keeps camera_id (the room) as `device`; firmware/serial/hardware are
        returned too for onboarding display / future mapping."""
        body = self._call(
            self.xaddr,
            '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        return {
            "manufacturer": body.findtext(".//Manufacturer") or "",
            "model": body.findtext(".//Model") or "",
            "firmware": body.findtext(".//FirmwareVersion") or "",
            "serial": body.findtext(".//SerialNumber") or "",
            "hardware": body.findtext(".//HardwareId") or "",
        }

    def system_datetime(self) -> datetime:
        """The camera's OWN clock, via GetSystemDateAndTime (ONVIF's one
        unauthenticated call — the same one discovery uses for liveness).
        Returns an aware UTC datetime parsed from <UTCDateTime>.

        This is the clock the camera burns into the video as its OSD timestamp.
        Comparing it to the edge clock (common.clock) tells us whether a
        snapshot's reported time — which we stamp from the EDGE clock — will line
        up with the time painted into the pixels. Raises OnvifError if the camera
        doesn't report a usable UTC time."""
        body = self._call(
            self.xaddr,
            '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        utc = body.find(".//UTCDateTime")
        if utc is None:
            raise OnvifError("camera did not report UTCDateTime")
        d, t = utc.find("Date"), utc.find("Time")

        def _int(el, tag) -> int:
            return int((el.findtext(tag) if el is not None else 0) or 0)

        try:
            return datetime(_int(d, "Year"), _int(d, "Month"), _int(d, "Day"),
                            _int(t, "Hour"), _int(t, "Minute"), _int(t, "Second"),
                            tzinfo=timezone.utc)
        except ValueError as exc:
            raise OnvifError(f"camera reported an invalid datetime: {exc}")

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
                                   width: int = 1920, height: int = 1080,
                                   bitrate_kbps: int = 2048, fps: int = 15,
                                   prefer_h264: bool = True) -> bool:
        """Best-effort: shape a SUB profile into a compact recording stream —
        <=1080p, H.264, a capped bitrate and frame-rate — so the CAMERA emits a
        small stream MediaMTX only has to remux (no device re-encode). Only ever
        called on a non-main profile; picks the closest supported resolution <=
        target. Returns True when the encoder was updated (best-effort: a camera
        that rejects any field falls back to recording the stream as-is)."""
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

            cfg = self._call(self._media_url, f"""
<GetVideoEncoderConfiguration xmlns="{_MEDIA_NS}">
  <ConfigurationToken>{profile.encoder_token}</ConfigurationToken>
</GetVideoEncoderConfiguration>""").find(".//Configuration")
            if cfg is None:
                return False
            # Force H.264 for in-browser playback; keep the camera's codec only if
            # the operator opted out of the H.264 preference.
            encoding = "H264" if prefer_h264 else (cfg.findtext("Encoding") or "H264")
            # Codec block only when we're actually writing H.264 (the ver10 schema
            # has no H.265 element — an H.265 camera keeps its codec via <Encoding>).
            gov = max(1, int(fps) * 2)            # ~2-second GOP
            codec_block = (f"""
    <H264 xmlns="{_SCHEMA_NS}">
      <GovLength>{gov}</GovLength>
      <H264Profile>Main</H264Profile>
    </H264>""" if encoding == "H264" else "")
            # Element order follows the ONVIF VideoEncoderConfiguration schema:
            # Name, UseCount, Encoding, Resolution, Quality, RateControl, H264,
            # SessionTimeout. RateControl (frame-rate + bitrate) is the disk lever.
            self._call(self._media_url, f"""
<SetVideoEncoderConfiguration xmlns="{_MEDIA_NS}">
  <Configuration token="{profile.encoder_token}">
    <Name xmlns="{_SCHEMA_NS}">{cfg.findtext("Name") or "record"}</Name>
    <UseCount xmlns="{_SCHEMA_NS}">{cfg.findtext("UseCount") or 1}</UseCount>
    <Encoding xmlns="{_SCHEMA_NS}">{encoding}</Encoding>
    <Resolution xmlns="{_SCHEMA_NS}">
      <Width>{best_w}</Width><Height>{best_h}</Height>
    </Resolution>
    <Quality xmlns="{_SCHEMA_NS}">{cfg.findtext("Quality") or 4}</Quality>
    <RateControl xmlns="{_SCHEMA_NS}">
      <FrameRateLimit>{int(fps)}</FrameRateLimit>
      <EncodingInterval>1</EncodingInterval>
      <BitrateLimit>{int(bitrate_kbps)}</BitrateLimit>
    </RateControl>{codec_block}
    <SessionTimeout xmlns="{_SCHEMA_NS}">{cfg.findtext("SessionTimeout") or "PT60S"}</SessionTimeout>
  </Configuration>
  <ForcePersistence>true</ForcePersistence>
</SetVideoEncoderConfiguration>""")
            logger.info("record profile %s standardized: %dx%d %s @ %d fps, %d kbps",
                        profile.token, best_w, best_h, encoding, int(fps),
                        int(bitrate_kbps))
            return True
        except OnvifError as exc:
            logger.info("record-profile standardization skipped: %s", exc)
            return False

    def remove_profile_audio(self, profile_token: str) -> bool:
        """Best-effort: detach the audio encoder (and source) from ONE profile so
        its RTSP stream is VIDEO-ONLY. Used on the RECORD profile: these cameras
        speak G.711/PCM audio, which fMP4 can only store as LPCM in the 'ipcm'
        box — a 2023 spec that GStreamer/Totem, pre-6.0 FFmpeg and several
        browsers don't read, so ONE audio track makes the whole clip
        "unplayable". Never called on the main profile — live view keeps audio."""
        self._services()
        ok = False
        for op in ("RemoveAudioEncoderConfiguration",
                   "RemoveAudioSourceConfiguration"):
            try:
                self._call(self._media_url,
                           f'<{op} xmlns="{_MEDIA_NS}">'
                           f'<ProfileToken>{profile_token}</ProfileToken></{op}>')
                ok = True
            except OnvifError as exc:
                # normal on cameras whose profile carries no audio to begin with
                logger.info("%s skipped: %s", op, exc)
        if ok:
            logger.info("record profile %s: audio detached (video-only recording)",
                        profile_token)
        return ok

    def restore_profile_audio(self, profile_token: str) -> bool:
        """Best-effort inverse of remove_profile_audio: attach the camera's
        first audio source + encoder configuration to a profile so its stream
        carries audio again. Used on the RECORD profile when recordings should
        include audio (the edge transcodes it to AAC) — converges a profile a
        previous video-only probe stripped. Harmless when already attached."""
        self._services()
        ok = False
        for get_op, add_op in (
                ("GetAudioSourceConfigurations", "AddAudioSourceConfiguration"),
                ("GetAudioEncoderConfigurations", "AddAudioEncoderConfiguration")):
            try:
                body = self._call(self._media_url,
                                  f'<{get_op} xmlns="{_MEDIA_NS}"/>')
                cfg = body.find(".//Configurations")
                token = cfg.get("token") if cfg is not None else None
                if not token:
                    continue                      # camera has no audio at all
                self._call(self._media_url, f"""
<{add_op} xmlns="{_MEDIA_NS}">
  <ProfileToken>{profile_token}</ProfileToken>
  <ConfigurationToken>{token}</ConfigurationToken>
</{add_op}>""")
                ok = True
            except OnvifError as exc:
                logger.info("%s skipped: %s", add_op, exc)
        if ok:
            logger.info("record profile %s: audio attached (AAC recordings)",
                        profile_token)
        return ok

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

    def ptz_status(self, profile_token: str) -> tuple[float, float, float] | None:
        """The camera's CURRENT absolute PTZ position (pan, tilt, zoom) via
        GetStatus, or None when it doesn't report one. Used to remember where the
        camera was framing the target before a manual override, so it can return
        there. Never raises for an unsupported camera — returns None."""
        self._services()
        if not self._ptz_url:
            return None
        try:
            body = self._call(self._ptz_url,
                              f'<GetStatus xmlns="{_PTZ_NS}">'
                              f'<ProfileToken>{profile_token}</ProfileToken></GetStatus>')
        except OnvifError:
            return None
        pos = body.find(".//Position")
        if pos is None:
            return None
        pt, zoom_el = pos.find("PanTilt"), pos.find("Zoom")
        try:
            pan = float(pt.get("x")) if pt is not None else 0.0
            tilt = float(pt.get("y")) if pt is not None else 0.0
            zoom = float(zoom_el.get("x")) if zoom_el is not None else 0.0
        except (TypeError, ValueError):
            return None
        return (pan, tilt, zoom)

    def ptz_absolute_move(self, profile_token: str,
                          pan: float, tilt: float, zoom: float) -> None:
        """Move to an ABSOLUTE PTZ position (what ptz_status returned) — used to
        revert to the pre-override framing. Raises OnvifError if the camera has
        no PTZ service or rejects absolute positioning."""
        self._services()
        if not self._ptz_url:
            raise OnvifError("camera reports no PTZ service")
        self._call(self._ptz_url, f"""
<AbsoluteMove xmlns="{_PTZ_NS}">
  <ProfileToken>{profile_token}</ProfileToken>
  <Position>
    <PanTilt x="{pan}" y="{tilt}" xmlns="{_SCHEMA_NS}"/>
    <Zoom x="{zoom}" xmlns="{_SCHEMA_NS}"/>
  </Position>
</AbsoluteMove>""")


# ---- probe: everything the wizard needs in one call ----------------------

def probe(xaddr: str, username: str, password: str,
          record_height: int = 1080, record_bitrate_kbps: int = 2048,
          record_fps: int = 15, prefer_h264: bool = True,
          record_audio: bool = False) -> dict:
    """Interrogate one discovered camera. Returns device info, EVERY media
    profile with its own RTSP URI (so the operator can pick stream1/stream2
    themselves), the highest-res profile as the default main stream, a
    standardized recording stream (compact 1080p H.264, video-only unless
    record_audio), and PTZ capability + the profile token to drive it."""
    cam = OnvifCamera(xaddr, username, password)
    info = cam.device_info()                    # also proves the credentials
    profs = cam.profiles()
    if not profs:
        raise OnvifError("camera exposes no media profiles")

    profs.sort(key=lambda p: p.width * p.height, reverse=True)
    main, subs = profs[0], profs[1:]

    # Every profile with its own stream URI — the wizard renders these as a
    # picker. One SOAP call each; a camera has only a handful of profiles.
    profiles_out: list[dict] = []
    for p in profs:
        try:
            uri = cam.stream_uri(p.token)
        except OnvifError:
            uri = ""
        profiles_out.append({**vars(p), "uri": uri})

    record_uri = None
    record_profile = None
    for sub in subs:                            # best sub-profile for recording
        if cam.standardize_record_profile(
                sub, height=record_height, bitrate_kbps=record_bitrate_kbps,
                fps=record_fps, prefer_h264=prefer_h264):
            record_profile = sub
            break
    if record_profile is None and subs:
        # couldn't standardize but a sub exists — record it as-is only if it's
        # a sane recording quality; otherwise fall back to main.
        best_sub = max(subs, key=lambda p: p.width * p.height)
        if best_sub.height >= 720:
            record_profile = best_sub
    if record_profile is not None:
        # Audio policy on the RECORD profile only (main/live never touched).
        # record_audio=True (default): make sure the camera audio is present —
        # the media backbone transcodes it to AAC for the clips. False: detach
        # it, so no MP4-hostile PCM ('ipcm') track poisons video-only clips.
        if record_audio:
            cam.restore_profile_audio(record_profile.token)
        else:
            cam.remove_profile_audio(record_profile.token)
        record_uri = cam.stream_uri(record_profile.token)

    # PTZ is supported if the camera exposes a PTZ service (reliable) OR a
    # profile embeds a PTZConfiguration. Drive it with a PTZ-bound profile when
    # one exists, else the main profile (single-config cameras accept any).
    ptz = cam.has_ptz_service() or any(p.has_ptz for p in profs)
    ptz_token = next((p.token for p in profs if p.has_ptz), main.token)

    main_out = next((p for p in profiles_out if p["token"] == main.token), None)
    return {
        "device": info,
        "profiles": profiles_out,               # [{token,name,encoding,w,h,fps,has_ptz,uri}]
        "main_uri": main_out["uri"] if main_out else "",
        "main_profile_token": main.token,
        "record_uri": record_uri,               # None -> record the main stream
        "ptz": ptz,
        "ptz_token": ptz_token,
    }
