"""
Local test for ONVIF discovery — the ProbeMatch parser + ONVIF/WSD filter, the
scope extractor, and the interface/subnet enumeration. No camera or TRT needed;
the multicast replies are canned so the whole suite runs in milliseconds.

Run:        PYTHONPATH=edge python edge/tests/test_discovery.py
Live scan:  PYTHONPATH=edge python edge/tests/test_discovery.py --live

The canned payloads mirror what real hardware sends: a Reolink-style ONVIF
camera (must be found) and a Windows WSD responder on :5357 (must be filtered —
this is the noise the untyped probe wakes up).
"""
import sys

from common import net
from onvif.discovery import (
    _is_onvif_match,
    _parse_matches,
    _scope_value,
    discover,
)


# A real ONVIF camera's ProbeMatch (namespaced, as it arrives on the wire).
_CAMERA_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <a:EndpointReference>
          <a:Address>urn:uuid:2419d68a-c260-1111-a1b2-9caf2c0d5e10</a:Address>
        </a:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>
        <d:Scopes>onvif://www.onvif.org/name/Front%20Door
          onvif://www.onvif.org/hardware/C260
          onvif://www.onvif.org/Profile/Streaming</d:Scopes>
        <d:XAddrs>http://192.168.0.250:2020/onvif/device_service</d:XAddrs>
        <d:MetadataVersion>1</d:MetadataVersion>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </s:Body>
</s:Envelope>"""

# A Windows/WSD responder on :5357 — NOT a camera. Must be filtered out.
_WSD_NOISE_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof">
  <s:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <a:EndpointReference>
          <a:Address>urn:uuid:37eab463-656d-4794-b705-d6f157e63152</a:Address>
        </a:EndpointReference>
        <d:Types>wsdp:Device pub:Computer</d:Types>
        <d:Scopes>http://schemas.microsoft.com/windows/2006/08/wdp/print</d:Scopes>
        <d:XAddrs>http://192.168.0.174:5357/37eab463-656d-4794-b705-d6f157e63152/</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </s:Body>
</s:Envelope>"""


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


def test_scope_value():
    print("scope extraction")
    s = ("onvif://www.onvif.org/name/Front%20Door "
         "onvif://www.onvif.org/hardware/C260")
    ok = True
    ok &= _check("name url-decoded", _scope_value(s, "name") == "Front Door")
    ok &= _check("hardware read", _scope_value(s, "hardware") == "C260")
    ok &= _check("missing key -> ''", _scope_value(s, "location") == "")
    return ok


def test_onvif_filter():
    print("ONVIF vs WSD filter")
    ok = True
    ok &= _check("NetworkVideoTransmitter type -> onvif",
                 _is_onvif_match("dn:NetworkVideoTransmitter", "", []))
    ok &= _check("onvif:// scope -> onvif",
                 _is_onvif_match("", "onvif://www.onvif.org/name/x", []))
    ok &= _check("/onvif/ xaddr -> onvif",
                 _is_onvif_match("", "", ["http://1.2.3.4/onvif/device_service"]))
    ok &= _check("windows WSD device -> rejected",
                 not _is_onvif_match("wsdp:Device pub:Computer", "",
                                     ["http://1.2.3.4:5357/uuid/"]))
    return ok


def test_parse_camera():
    print("parse a real camera reply")
    found = {}
    _parse_matches(_CAMERA_REPLY, "192.168.0.250", found)
    ok = _check("exactly one camera parsed", len(found) == 1)
    if not ok:
        return False
    cam = next(iter(found.values()))
    ok &= _check("ip", cam["ip"] == "192.168.0.250")
    ok &= _check("name url-decoded", cam["name"] == "Front Door")
    ok &= _check("hardware", cam["hardware"] == "C260")
    ok &= _check("xaddr", cam["xaddr"].endswith("/onvif/device_service"))
    ok &= _check("via multicast", cam["via"] == "multicast")
    ok &= _check("keyed by EndpointReference (stable identity)",
                 "urn:uuid:2419d68a-c260-1111-a1b2-9caf2c0d5e10" in found)
    return ok


def test_parse_filters_noise():
    print("WSD noise is filtered out")
    found = {}
    _parse_matches(_WSD_NOISE_REPLY, "192.168.0.174", found)
    return _check("windows/printer responder ignored", len(found) == 0)


def test_enumeration():
    print("interface / subnet enumeration")
    ips = net.local_ipv4s()
    nets = net.local_ipv4_networks()
    ok = True
    ok &= _check("at least one non-loopback IPv4", len(ips) >= 1)
    ok &= _check("no loopback in list", all(not a.startswith("127.") for a in ips))
    ok &= _check("subnets no wider than /24",
                 all(n.prefixlen >= 24 for n in nets))
    print(f"    interfaces: {ips}")
    print(f"    subnets:    {[str(n) for n in nets]}")
    return ok


def _live():
    print("live discovery (multicast + fallback)…")
    for c in discover():
        tag = c.get("name") or c.get("hardware") or c.get("manufacturer") or "?"
        print(f"    {c['ip']:<15} {tag:<18} via {c['via']:<9} {c['xaddr']}")


if __name__ == "__main__":
    if "--live" in sys.argv:
        _live()
        raise SystemExit(0)
    results = [
        test_scope_value(),
        test_onvif_filter(),
        test_parse_camera(),
        test_parse_filters_noise(),
        test_enumeration(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} groups passed")
    raise SystemExit(0 if passed == len(results) else 1)
