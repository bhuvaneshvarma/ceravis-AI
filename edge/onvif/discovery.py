from __future__ import annotations

"""
Find ONVIF cameras on the network — two complementary mechanisms.

1. WS-Discovery (multicast) — the primary, near-instant path. Every ONVIF
   camera answers a SOAP "Probe" multicast to 239.255.255.250:3702/UDP with its
   service address (XAddrs) and self-description scopes (name, hardware, make).
   The detail naive implementations get wrong, and the reason this used to miss
   the user's WiFi camera: the probe must leave EVERY local interface, not just
   the default route. The Jetson is multi-homed (WiFi + ethernet + its own
   hotspot + docker bridges); a camera on WiFi never sees a probe that only went
   out ethernet. We bind one socket per interface, pin IP_MULTICAST_IF to it,
   and re-probe across the window (alternating typed/untyped Probes) to survive
   UDP loss and firmware quirks.

2. Unicast sweep (fallback) — consumer WiFi routers very often block multicast
   between wireless clients ("AP / client isolation"), so WS-Discovery returns
   nothing though the camera is right there. When multicast is silent (or on an
   explicit deep scan) we sweep the local /24 on the common ONVIF ports and
   confirm each hit with an unauthenticated GetSystemDateAndTime — the ONVIF
   liveness call that needs no credentials. Bounded (clamped to /24, capped host
   count, short timeouts), thread-pooled, stdlib + requests only.

Both mechanisms return the same shape, so the wizard is agnostic to which found
the camera:
    {xaddr, ip, name, hardware, manufacturer, via}   via = "multicast"|"unicast"
"""

import logging
import select
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from common import net
from onvif.soap import message_id, strip_ns


logger = logging.getLogger("onvif")

_MCAST = ("239.255.255.250", 3702)

# ONVIF device service almost always answers on one of these TCP ports; the
# canonical device endpoint path is standardized by the spec.
_ONVIF_PORTS = (80, 8000, 8080, 2020, 8899, 8081, 85, 8181)
_DEVICE_PATH = "/onvif/device_service"

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
    xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>{mid}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>{types}</d:Probe>
  </e:Body>
</e:Envelope>"""

# Typed probe = "cameras only" (widest firmware support); untyped = "any ONVIF
# device" (catches cameras with buggy type matching). We alternate both.
_TYPED = "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
_UNTYPED = ""


def _scope_value(scopes: str, key: str) -> str:
    """Pull 'kitchen-cam' out of 'onvif://www.onvif.org/name/kitchen-cam …'."""
    for s in scopes.split():
        marker = f"/{key}/"
        if marker in s:
            return s.split(marker, 1)[1].replace("%20", " ")
    return ""


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


# ---- WS-Discovery (multicast) --------------------------------------------

def _open_sockets(ips: list[str]) -> list[socket.socket]:
    """One UDP socket per interface, each pinned to send/receive on THAT NIC."""
    socks: list[socket.socket] = []
    for ip in ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            # Pin the multicast egress + reply path to this interface.
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                         socket.inet_aton(ip))
            s.bind((ip, 0))
            s.setblocking(False)
            socks.append(s)
        except OSError as exc:
            logger.debug("discovery: skip interface %s (%s)", ip, exc)
    return socks


def _is_onvif_match(types: str, scopes: str, xaddrs: list[str]) -> bool:
    """Untyped probes also wake non-camera WS-Discovery responders — Windows PCs
    and WSD printers on :5357. Keep only real ONVIF devices: they advertise the
    NetworkVideoTransmitter type, an onvif:// scope, or an /onvif/ service path."""
    if "NetworkVideoTransmitter" in types:
        return True
    if "onvif://" in scopes:
        return True
    return any("/onvif/" in x.lower() for x in xaddrs)


def _parse_matches(data: bytes, src_ip: str, found: dict[str, dict]) -> None:
    try:
        root = strip_ns(ElementTree.fromstring(data))
    except ElementTree.ParseError:
        return
    for match in root.findall(".//ProbeMatch"):
        xaddrs = (match.findtext(".//XAddrs") or "").split()
        if not xaddrs:
            continue
        scopes = match.findtext(".//Scopes") or ""
        types = match.findtext(".//Types") or ""
        if not _is_onvif_match(types, scopes, xaddrs):
            continue                          # WSD noise (PC / printer), not a camera
        epr = (match.findtext(".//EndpointReference/Address") or "").strip()
        # Prefer the XAddr on the IP that answered (multi-homed cameras).
        xaddr = next((x for x in xaddrs if src_ip in x), xaddrs[0])
        ip = _host_of(xaddr) or src_ip
        key = epr or ip                       # EPR is the camera's stable identity
        found[key] = {
            "xaddr": xaddr,
            "ip": ip,
            "name": _scope_value(scopes, "name"),
            "hardware": _scope_value(scopes, "hardware"),
            "manufacturer": (_scope_value(scopes, "mfr")
                             or _scope_value(scopes, "manufacturer")),
            "via": "multicast",
        }


def discover_multicast(timeout: float = 4.0,
                       interfaces: list[str] | None = None) -> list[dict]:
    """Probe every local interface and collect ProbeMatch replies for `timeout`
    seconds. Re-probes up to 4 times (alternating typed/untyped) to beat UDP
    loss and slow cameras."""
    ips = interfaces if interfaces is not None else net.local_ipv4s()
    socks = _open_sockets(ips)
    if not socks:
        logger.warning("discovery: no usable network interface to probe from")
        return []

    found: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    next_send = 0.0
    bursts = 0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send and bursts < 4:
                types = _TYPED if bursts % 2 == 0 else _UNTYPED
                probe = _PROBE.format(mid=message_id(), types=types).encode()
                for s in socks:
                    try:
                        s.sendto(probe, _MCAST)
                    except OSError as exc:
                        logger.debug("discovery send failed on a socket: %s", exc)
                bursts += 1
                next_send = now + 0.8
            ready, _, _ = select.select(socks, [], [], 0.3)
            for s in ready:
                try:
                    data, (src_ip, _port) = s.recvfrom(65535)
                except OSError:
                    continue
                _parse_matches(data, src_ip, found)
    finally:
        for s in socks:
            s.close()

    cams = sorted(found.values(), key=lambda c: net._ip_sort_key(c["ip"]))
    logger.info("WS-Discovery: %d camera(s) via multicast across %d interface(s)",
                len(cams), len(socks))
    return cams


# ---- unicast sweep (multicast-blocked fallback) --------------------------

_LIVENESS_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
    '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    '</s:Body></s:Envelope>'
).encode()


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _verify_onvif(host: str, port: int, timeout: float) -> str | None:
    """Confirm an open port is really an ONVIF device (not just any web server).
    GetSystemDateAndTime is unauthenticated by spec — a real camera answers with
    a SOAP envelope. Returns the device-service xaddr on success."""
    url = f"http://{host}:{port}{_DEVICE_PATH}"
    try:
        resp = requests.post(
            url, data=_LIVENESS_ENVELOPE,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            timeout=timeout)
    except requests.RequestException:
        return None
    body = resp.content or b""
    if b"SystemDateAndTime" in body or (b"Envelope" in body and resp.ok):
        return url
    return None


def discover_unicast(timeout: float = 0.35,
                     ports: tuple[int, ...] = _ONVIF_PORTS[:4],
                     workers: int = 256,
                     networks=None,
                     max_hosts: int = 1024) -> list[dict]:
    """Sweep the local subnet(s) for ONVIF cameras when multicast is blocked.
    Two bounded phases: a fast TCP-connect scan, then a SOAP liveness check on
    only the open ports."""
    nets = networks if networks is not None else net.local_ipv4_networks()
    own = set(net.local_ipv4s())
    hosts: list[str] = []
    for n in nets:
        for h in n.hosts():
            a = str(h)
            if a not in own:
                hosts.append(a)
    if not hosts:
        return []
    if len(hosts) > max_hosts:
        hosts = hosts[:max_hosts]

    # Phase 1 — which (host, port) pairs accept a TCP connection.
    open_hits: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_tcp_open, h, p, timeout): (h, p)
                for h in hosts for p in ports}
        for fut in as_completed(futs):
            if fut.result():
                open_hits.append(futs[fut])

    # Phase 2 — of those, which actually speak ONVIF. First port per host wins.
    found: dict[str, dict] = {}
    verify_to = max(timeout * 4, 1.5)
    with ThreadPoolExecutor(max_workers=min(workers, 32)) as ex:
        futs = {ex.submit(_verify_onvif, h, p, verify_to): (h, p)
                for h, p in open_hits}
        for fut in as_completed(futs):
            h, _p = futs[fut]
            url = fut.result()
            if url and h not in found:
                found[h] = {
                    "xaddr": url, "ip": h, "name": "",
                    "hardware": "", "manufacturer": "", "via": "unicast",
                }

    cams = sorted(found.values(), key=lambda c: net._ip_sort_key(c["ip"]))
    logger.info("unicast sweep: %d ONVIF host(s) across %d subnet(s)",
                len(cams), len(nets))
    return cams


# ---- orchestration -------------------------------------------------------

def discover(timeout: float = 4.0, deep: bool = False,
             fallback: bool = True) -> list[dict]:
    """Find cameras. Multicast first (fast); if it finds nothing and `fallback`
    is on — or `deep` is requested — sweep the subnet and merge. Multicast
    entries win on overlap (they carry the camera's name/scopes)."""
    cams = discover_multicast(timeout=timeout)
    if cams and not deep:
        return cams
    if not (deep or fallback):
        return cams

    ports = _ONVIF_PORTS if deep else _ONVIF_PORTS[:4]
    try:
        swept = discover_unicast(ports=ports)
    except Exception as exc:                  # a fallback must never break scan
        logger.warning("unicast sweep failed: %s", exc)
        swept = []

    by_ip = {c["ip"]: c for c in cams}
    for c in swept:
        by_ip.setdefault(c["ip"], c)
    return sorted(by_ip.values(), key=lambda c: net._ip_sort_key(c["ip"]))


# ---- on-device diagnostics ------------------------------------------------
# Run directly to debug discovery on the Jetson (or any host):
#     PYTHONPATH=edge python -m onvif.discovery
# Prints the interfaces/subnets it will scan, then multicast and sweep results.

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("interfaces :", ", ".join(net.local_ipv4s()) or "(none)")
    print("subnets    :", ", ".join(str(n) for n in net.local_ipv4_networks())
          or "(none)")
    print("\n[1] WS-Discovery multicast …")
    for c in discover_multicast():
        print(f"   {c['ip']:<15} {c.get('name') or c.get('hardware') or '?':<20}"
              f" {c['xaddr']}")
    print("\n[2] Unicast subnet sweep (deep) …")
    for c in discover_unicast(ports=_ONVIF_PORTS):
        print(f"   {c['ip']:<15} {c['xaddr']}")
