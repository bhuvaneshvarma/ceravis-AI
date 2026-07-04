from __future__ import annotations

"""
WS-Discovery — how every ONVIF camera announces itself.

We multicast a tiny SOAP "Probe" ("any NetworkVideoTransmitter out there?") to
239.255.255.250:3702/UDP and collect the replies for a few seconds. Each reply
carries the camera's ONVIF service address (XAddrs) plus self-description
scopes (name, hardware). Works on whatever network the Jetson is on — the
household LAN or its own hotspot.
"""

import logging
import socket
import time
from xml.etree import ElementTree

from onvif.soap import message_id, strip_ns


logger = logging.getLogger("onvif")

_MCAST = ("239.255.255.250", 3702)

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
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


def _scope_value(scopes: str, key: str) -> str:
    """Pull 'kitchen-cam' out of 'onvif://www.onvif.org/name/kitchen-cam …'."""
    for s in scopes.split():
        marker = f"/{key}/"
        if marker in s:
            return s.split(marker, 1)[1].replace("%20", " ")
    return ""


def discover(timeout: float = 3.0) -> list[dict]:
    """Probe the local network; return [{xaddr, ip, name, hardware}, ...]."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    probe = _PROBE.format(mid=message_id()).encode()
    try:
        sock.sendto(probe, _MCAST)
        sock.sendto(probe, _MCAST)               # UDP — a second shot is cheap
    except OSError as exc:
        logger.warning("discovery send failed: %s", exc)
        sock.close()
        return []

    found: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, (ip, _port) = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            root = strip_ns(ElementTree.fromstring(data))
        except ElementTree.ParseError:
            continue
        for match in root.findall(".//ProbeMatch"):
            xaddrs = (match.findtext(".//XAddrs") or "").split()
            scopes = match.findtext(".//Scopes") or ""
            if not xaddrs:
                continue
            # prefer the XAddr on the ip that answered (multi-homed cameras)
            xaddr = next((x for x in xaddrs if ip in x), xaddrs[0])
            found[ip] = {
                "xaddr": xaddr,
                "ip": ip,
                "name": _scope_value(scopes, "name"),
                "hardware": _scope_value(scopes, "hardware"),
            }
    sock.close()
    cams = sorted(found.values(), key=lambda c: c["ip"])
    logger.info("WS-Discovery: %d camera(s) found", len(cams))
    return cams
