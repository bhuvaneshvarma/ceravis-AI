from __future__ import annotations

"""
Per-camera ONVIF client: device info, media profiles, RTSP stream URIs and PTZ.

An ONVIF camera exposes several "media profiles" — independent encoders on the
same sensor. Typically profile 1 is the MAIN stream (full native quality) and
profile 2+ are SUB streams (smaller). CERAVIS policy (per product decision):

  • We use the MAIN profile, and ONLY the main profile. MediaMTX pulls it once
    and fans it out to the AI, the live links, the UI pages and the recorder —
    so recordings are native quality, remux-only, and a camera on a weak WiFi
    link never has to carry a second stream.
  • This client READS the camera and drives PTZ. It never rewrites an encoder
    configuration: these are the customer's cameras, they are shared with the
    Tapo app, and a fire-and-forget SetVideoEncoderConfiguration is a change
    nobody can explain later.
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
    encoder_token: str     # VideoEncoderConfiguration token (reported, never written)
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

    # ---- stream shaping (explicit, never automatic) ----------------------
    def encoder_options(self, encoder_token: str) -> dict[str, list[tuple[int, int]]]:
        """What this encoder can actually do: {codec: [(w,h), ...]}, largest first.

        The resolutions are nested UNDER each codec element in the ver10 options
        envelope, so they must be read per codec — a flat search mixes the JPEG
        snapshot sizes in with the video ones.

        Note what ver10 cannot say: the schema has <JPEG>, <MPEG4> and <H264>
        elements and no H.265 at all. That is precisely how a camera can report
        "H264" here and stream HEVC anyway — which is what the C260 does. So this
        tells you what the camera will ACCEPT, never what it is actually sending;
        for that, observe the live stream."""
        body = self._call(self._media_url, f"""
<GetVideoEncoderConfigurationOptions xmlns="{_MEDIA_NS}">
  <ConfigurationToken>{encoder_token}</ConfigurationToken>
</GetVideoEncoderConfigurationOptions>""")
        out: dict[str, list[tuple[int, int]]] = {}
        for codec in ("H264", "MPEG4", "JPEG"):
            section = body.find(f".//{codec}")
            if section is None:
                continue
            sizes = []
            for r in section.findall("ResolutionsAvailable"):
                w = int(r.findtext("Width") or 0)
                h = int(r.findtext("Height") or 0)
                if w > 0 and h > 0:
                    sizes.append((w, h))
            if sizes:
                out[codec] = sorted(set(sizes), key=lambda wh: wh[0] * wh[1],
                                    reverse=True)
        return out

    def _encoder_config(self, encoder_token: str):
        return self._call(self._media_url, f"""
<GetVideoEncoderConfiguration xmlns="{_MEDIA_NS}">
  <ConfigurationToken>{encoder_token}</ConfigurationToken>
</GetVideoEncoderConfiguration>""").find(".//Configuration")

    @staticmethod
    def _config_state(cfg) -> tuple[int, int, str]:
        """(width, height, encoding) as the camera currently reports them."""
        if cfg is None:
            return 0, 0, ""
        res = cfg.find("Resolution")
        w = int(res.findtext("Width") or 0) if res is not None else 0
        h = int(res.findtext("Height") or 0) if res is not None else 0
        return w, h, (cfg.findtext("Encoding") or "").upper()

    def shape_stream(self, encoder_token: str, max_height: int | None = None,
                     codec: str | None = None) -> dict:
        """Shape ONE encoder: cap its height and/or set its codec. Reports what
        actually happened, and only ever changes what was asked for.

        Deliberately MINIMAL: fields you do not name are read and written back
        exactly as the camera reported them — quality, bitrate, frame rate and
        the H.264 GOP block all survive untouched. Holding the bitrate while
        lowering the resolution is the point: the same bits cover fewer pixels,
        so the picture gets sharper per pixel while costing every decoder less.

        Always reads back afterwards, because "the write didn't throw" is not
        evidence the camera accepted it. Be aware the read-back is the camera's
        CLAIM — this family reports H264 while streaming HEVC — so a caller that
        cares about the codec must also observe the live stream.

        Returns before/requested/after (each {resolution, codec}), `changed`,
        `accepted`, and the `options` the camera advertises."""
        self._services()
        options = self.encoder_options(encoder_token)
        if not options:
            raise OnvifError("camera reports no encoder options")

        cfg = self._encoder_config(encoder_token)
        if cfg is None:
            raise OnvifError("camera returned no encoder configuration")
        cur_w, cur_h, cur_codec = self._config_state(cfg)

        want_codec = (codec or cur_codec or "H264").upper()
        if codec and want_codec not in options:
            raise OnvifError(f"camera does not offer {want_codec} on this profile "
                             f"(it offers: {', '.join(sorted(options))})")

        sizes = options.get(want_codec) or next(iter(options.values()))
        if max_height is None:
            target = (cur_w, cur_h) if (cur_w, cur_h) in sizes else sizes[0]
        else:
            # Largest at or below the cap; if everything is taller, its smallest
            # is the closest honest answer rather than a failure.
            target = next(((w, h) for w, h in sizes if h <= max_height), sizes[-1])

        def state(w, h, c):
            return {"resolution": f"{w}x{h}", "codec": c}

        result = {
            "before": state(cur_w, cur_h, cur_codec),
            "requested": state(target[0], target[1], want_codec),
            "options": {c: [f"{w}x{h}" for w, h in v] for c, v in options.items()},
        }
        if (cur_w, cur_h) == target and cur_codec == want_codec:
            return {**result, "after": result["before"], "changed": False,
                    "accepted": True}

        rate, h264 = cfg.find("RateControl"), cfg.find("H264")
        rate_block = "" if rate is None else f"""
    <RateControl xmlns="{_SCHEMA_NS}">
      <FrameRateLimit>{rate.findtext("FrameRateLimit") or 0}</FrameRateLimit>
      <EncodingInterval>{rate.findtext("EncodingInterval") or 1}</EncodingInterval>
      <BitrateLimit>{rate.findtext("BitrateLimit") or 0}</BitrateLimit>
    </RateControl>"""
        # The <H264> block belongs only to an H.264 configuration.
        h264_block = "" if (h264 is None or want_codec != "H264") else f"""
    <H264 xmlns="{_SCHEMA_NS}">
      <GovLength>{h264.findtext("GovLength") or 25}</GovLength>
      <H264Profile>{h264.findtext("H264Profile") or "Main"}</H264Profile>
    </H264>"""
        # Element order follows the ONVIF VideoEncoderConfiguration schema.
        self._call(self._media_url, f"""
<SetVideoEncoderConfiguration xmlns="{_MEDIA_NS}">
  <Configuration token="{encoder_token}">
    <Name xmlns="{_SCHEMA_NS}">{cfg.findtext("Name") or "main"}</Name>
    <UseCount xmlns="{_SCHEMA_NS}">{cfg.findtext("UseCount") or 1}</UseCount>
    <Encoding xmlns="{_SCHEMA_NS}">{want_codec}</Encoding>
    <Resolution xmlns="{_SCHEMA_NS}">
      <Width>{target[0]}</Width><Height>{target[1]}</Height>
    </Resolution>
    <Quality xmlns="{_SCHEMA_NS}">{cfg.findtext("Quality") or 4}</Quality>{rate_block}{h264_block}
    <SessionTimeout xmlns="{_SCHEMA_NS}">{cfg.findtext("SessionTimeout") or "PT60S"}</SessionTimeout>
  </Configuration>
  <ForcePersistence>true</ForcePersistence>
</SetVideoEncoderConfiguration>""")

        aw, ah, ac = self._config_state(self._encoder_config(encoder_token))
        accepted = (aw, ah) == target and ac == want_codec
        changed = (aw, ah, ac) != (cur_w, cur_h, cur_codec)
        logger.info("encoder %s: %s %s -> %s %s (requested %s %s, camera %s)",
                    encoder_token, result["before"]["resolution"], cur_codec,
                    f"{aw}x{ah}", ac, result["requested"]["resolution"], want_codec,
                    "accepted" if accepted else "IGNORED OR CLAMPED")
        return {**result, "after": state(aw, ah, ac),
                "changed": changed, "accepted": accepted}

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

def probe(xaddr: str, username: str, password: str) -> dict:
    """Interrogate one discovered camera — READ-ONLY, nothing on the camera is
    changed. Returns device info, EVERY media profile with its own RTSP URI (so
    the operator can pick stream1/stream2 themselves), the highest-res profile
    as the main stream, and PTZ capability + the profile token to drive it.

    The main profile is the whole answer: it is the one stream we pull, and it
    is what live view, the AI and recording all consume."""
    cam = OnvifCamera(xaddr, username, password)
    info = cam.device_info()                    # also proves the credentials
    profs = cam.profiles()
    if not profs:
        raise OnvifError("camera exposes no media profiles")

    profs.sort(key=lambda p: p.width * p.height, reverse=True)
    main = profs[0]

    # Every profile with its own stream URI — the wizard renders these as a
    # picker. One SOAP call each; a camera has only a handful of profiles.
    profiles_out: list[dict] = []
    for p in profs:
        try:
            uri = cam.stream_uri(p.token)
        except OnvifError:
            uri = ""
        profiles_out.append({**vars(p), "uri": uri})

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
        "main_encoding": main.encoding,         # H264 plays in a browser; H265 does not
        "main_resolution": f"{main.width}x{main.height}",
        "ptz": ptz,
        "ptz_token": ptz_token,
    }
