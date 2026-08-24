from __future__ import annotations

"""
ONVIF capability DUMP — a read-only interrogation of one camera.

Tries every ONVIF `Get*` operation this codebase knows about against a camera and
writes the full result of each into ONE reviewable text file, so you can see
exactly what a given model (e.g. a Tapo) exposes and what is readable/tunable —
then decide what CERAVIS should actually use.

READ-ONLY BY DESIGN: it issues only `Get*` (and the unauthenticated
GetSystemDateAndTime); it NEVER calls a `Set*`/`Add*`/`Remove*`, so it cannot
change a single camera setting. It is a diagnostic, entirely separate from the
runtime — nothing else imports it.

Run from the edge/ directory:
    # by registered camera (uses the ONVIF creds in cameras.json):
    python -m tools.onvif_dump KITCHEN
    # or explicitly:
    python -m tools.onvif_dump --xaddr http://192.168.0.250:2020/onvif/device_service \
                               --user admin --pass 'secret'

Output: camerainfo/<host>.txt at the repo root.
"""

import argparse
import sys
import xml.dom.minidom as minidom
from pathlib import Path
from xml.etree import ElementTree

from onvif.soap import OnvifError, call
from common import clock

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ONVIF service namespaces (the wsdl targetNamespace each op lives under).
NS = {
    "device": "http://www.onvif.org/ver10/device/wsdl",
    "media": "http://www.onvif.org/ver10/media/wsdl",
    "ptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "imaging": "http://www.onvif.org/ver20/imaging/wsdl",
    "events": "http://www.onvif.org/ver10/events/wsdl",
    "analytics": "http://www.onvif.org/ver20/analytics/wsdl",
    "deviceio": "http://www.onvif.org/ver10/deviceIO/wsdl",
}

# No-argument Get* operations, grouped by service. Each is best-effort — a camera
# that doesn't implement one just yields a SOAP fault we record and move past.
NOARG_OPS: dict[str, list[str]] = {
    "device": [
        "GetDeviceInformation", "GetSystemDateAndTime", "GetCapabilities",
        "GetServices", "GetServiceCapabilities", "GetScopes", "GetHostname",
        "GetDNS", "GetNTP", "GetNetworkInterfaces", "GetNetworkProtocols",
        "GetNetworkDefaultGateway", "GetZeroConfiguration", "GetDiscoveryMode",
        "GetRemoteDiscoveryMode", "GetUsers", "GetWsdlUrl", "GetGeoLocation",
        "GetRelayOutputs", "GetSystemUris",
    ],
    "media": [
        "GetProfiles", "GetVideoSources", "GetVideoSourceConfigurations",
        "GetVideoEncoderConfigurations", "GetAudioSources",
        "GetAudioSourceConfigurations", "GetAudioEncoderConfigurations",
        "GetAudioOutputs", "GetAudioOutputConfigurations", "GetOSDs",
        "GetMetadataConfigurations", "GetVideoAnalyticsConfigurations",
        "GetServiceCapabilities",
    ],
    "ptz": ["GetServiceCapabilities", "GetNodes", "GetConfigurations"],
    "imaging": ["GetServiceCapabilities"],
    "events": ["GetServiceCapabilities", "GetEventProperties"],
    "analytics": ["GetServiceCapabilities", "GetSupportedAnalyticsModules"],
    "deviceio": ["GetVideoSources", "GetAudioSources", "GetRelayOutputs",
                 "GetDigitalInputs"],
}


class _Dumper:
    def __init__(self, xaddr: str, user: str, password: str) -> None:
        self.xaddr = xaddr
        self.user = user
        self.password = password
        self._urls: dict[str, str] = {}
        self.lines: list[str] = []

    # ---- transport ---------------------------------------------------
    def _call(self, service: str, op: str, inner: str = "") -> ElementTree.Element:
        url = self._urls.get(service, self.xaddr)
        body = f'<{op} xmlns="{NS[service]}">{inner}</{op}>' if inner \
            else f'<{op} xmlns="{NS[service]}"/>'
        return call(url, body, self.user, self.password)

    def _section(self, title: str) -> None:
        self.lines += ["", "=" * 78, title, "=" * 78]

    def _record(self, service: str, op: str, inner: str = "") -> ElementTree.Element | None:
        """Run one Get op; append its pretty XML (or the fault) to the report."""
        try:
            el = self._call(service, op, inner)
            self.lines += [f"\n----- {service}.{op} {inner or ''}".rstrip() + " -----",
                           _pretty(el)]
            return el
        except OnvifError as exc:
            self.lines.append(f"\n----- {service}.{op} -----  (not available: {exc})")
            return None

    # ---- service discovery -------------------------------------------
    def resolve_services(self) -> None:
        """Map each service namespace to its XAddr via GetServices, defaulting any
        unresolved service to the device endpoint (many cheap cameras answer every
        service on the one URL)."""
        try:
            body = call(self.xaddr,
                        f'<GetServices xmlns="{NS["device"]}">'
                        "<IncludeCapability>true</IncludeCapability></GetServices>",
                        self.user, self.password)
            for svc in body.findall(".//Service"):
                ns = svc.findtext("Namespace") or ""
                url = svc.findtext("XAddr") or ""
                for key, base in NS.items():
                    if base == ns and url:
                        self._urls[key] = url
        except OnvifError as exc:
            self.lines.append(f"(GetServices failed: {exc} — using the device "
                              f"endpoint for every service)")
        for key in NS:
            self._urls.setdefault(key, self.xaddr)
        self._section("RESOLVED SERVICE ENDPOINTS")
        for key in NS:
            self.lines.append(f"  {key:10s} -> {self._urls[key]}")

    # ---- the dump ----------------------------------------------------
    def run(self) -> None:
        self._section(f"ONVIF DUMP  {self.xaddr}  ({clock.now_iso()})")
        self.resolve_services()

        for service, ops in NOARG_OPS.items():
            self._section(f"{service.upper()} — no-argument Get operations")
            for op in ops:
                self._record(service, op)

        # ---- token-parameterized ops (need profiles / sources first) ----
        profiles = self._tokens("media", "GetProfiles", ".//Profiles")
        vsrc = self._tokens("media", "GetVideoSources", ".//VideoSources")
        encoders = self._encoder_tokens()

        self._section("MEDIA — per-profile stream + snapshot URIs, OSDs")
        for tok in profiles:
            self._record("media", "GetStreamUri",
                         "<StreamSetup>"
                         '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                         '<Transport xmlns="http://www.onvif.org/ver10/schema">'
                         "<Protocol>RTSP</Protocol></Transport></StreamSetup>"
                         f"<ProfileToken>{tok}</ProfileToken>")
            self._record("media", "GetSnapshotUri", f"<ProfileToken>{tok}</ProfileToken>")

        self._section("MEDIA — per-encoder configuration OPTIONS (what's tunable)")
        for tok in encoders:
            self._record("media", "GetVideoEncoderConfigurationOptions",
                         f"<ConfigurationToken>{tok}</ConfigurationToken>")

        self._section("IMAGING — per video source (settings + tunable options)")
        for tok in vsrc:
            self._record("imaging", "GetImagingSettings",
                         f"<VideoSourceToken>{tok}</VideoSourceToken>")
            self._record("imaging", "GetOptions",
                         f"<VideoSourceToken>{tok}</VideoSourceToken>")
            self._record("imaging", "GetMoveOptions",
                         f"<VideoSourceToken>{tok}</VideoSourceToken>")

        self._section("PTZ — per-profile status + presets")
        for tok in profiles:
            self._record("ptz", "GetStatus", f"<ProfileToken>{tok}</ProfileToken>")
            self._record("ptz", "GetPresets", f"<ProfileToken>{tok}</ProfileToken>")

    # ---- helpers -----------------------------------------------------
    def _tokens(self, service: str, op: str, path: str) -> list[str]:
        el = self._record(service, op)
        if el is None:
            return []
        out = []
        for node in el.findall(path):
            tok = node.get("token")
            if tok:
                out.append(tok)
        return out

    def _encoder_tokens(self) -> list[str]:
        try:
            el = self._call("media", "GetVideoEncoderConfigurations")
        except OnvifError:
            return []
        return [c.get("token") for c in el.findall(".//Configurations")
                if c.get("token")]


def _pretty(elem: ElementTree.Element) -> str:
    raw = ElementTree.tostring(elem, encoding="unicode")
    try:
        return "\n".join(l for l in minidom.parseString(raw)
                         .toprettyxml(indent="  ").splitlines() if l.strip())
    except Exception:
        return raw


def _camera_creds(label: str) -> tuple[str, str, str]:
    """Resolve xaddr + ONVIF user/pass from cameras.json for a registered label."""
    from configuration.camera_config import CameraConfig
    cam = CameraConfig().get_by_label(label)
    if cam is None:
        sys.exit(f"no registered camera matches {label!r}")
    if not cam.onvif_xaddr:
        sys.exit(f"camera {label!r} has no stored ONVIF endpoint (onvif_xaddr) — "
                 f"pass --xaddr/--user/--pass explicitly")
    return cam.onvif_xaddr, cam.onvif_username or "", cam.onvif_password or ""


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Read-only ONVIF capability dump for one camera.")
    ap.add_argument("label", nargs="?",
                    help="registered camera room/label (creds from cameras.json)")
    ap.add_argument("--xaddr", help="ONVIF device service URL (instead of a label)")
    ap.add_argument("--user", default="", help="ONVIF username")
    ap.add_argument("--pass", dest="password", default="", help="ONVIF password")
    ap.add_argument("-o", "--out", help="output file (default camerainfo/<host>.txt)")
    args = ap.parse_args()

    if args.xaddr:
        xaddr, user, password = args.xaddr, args.user, args.password
    elif args.label:
        xaddr, user, password = _camera_creds(args.label)
    else:
        ap.error("give a registered camera label, or --xaddr with --user/--pass")

    dumper = _Dumper(xaddr, user, password)
    print(f"[onvif-dump] interrogating {xaddr} (read-only) …")
    dumper.run()

    host = xaddr.split("//", 1)[-1].split("/", 1)[0].replace(":", "_") or "camera"
    out = Path(args.out) if args.out else (_REPO_ROOT / "camerainfo" / f"{host}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(dumper.lines) + "\n", encoding="utf-8")
    print(f"[onvif-dump] wrote {len(dumper.lines)} lines -> {out}")


if __name__ == "__main__":
    main()
