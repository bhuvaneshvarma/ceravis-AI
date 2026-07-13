from __future__ import annotations

"""Networking facts about THIS device — the one place that answers "what IPs and
subnets do I own".

    lan_ip()               -> the default-route source IP (startup banner, tests)
    local_ipv4s()          -> EVERY non-loopback IPv4 the host owns (all NICs)
    local_ipv4_networks()  -> the subnet(s) the host sits on, for a targeted sweep

ONVIF discovery is the demanding consumer: the probe must go out every interface
(the camera is often on a different NIC than the default route), and when the
WiFi AP blocks multicast we sweep the local subnet instead. Both need the full
interface picture, so it lives here — stdlib only, no netifaces/psutil.
"""

import ipaddress
import logging
import socket

logger = logging.getLogger("net")


def lan_ip() -> str | None:
    """The device's primary LAN IP — the source address of the default route.
    The UDP-connect trick sends no traffic; it just asks the kernel which local
    address it would use to reach the internet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _ip_sort_key(ip: str) -> bytes:
    """Numeric sort order for dotted-quad strings (10.0.0.9 before 10.0.0.10)."""
    try:
        return socket.inet_aton(ip)
    except OSError:
        return b"\xff\xff\xff\xff"


def local_ipv4s() -> list[str]:
    """Every non-loopback IPv4 address this device owns — WiFi, ethernet, the
    Jetson's own hotspot, and any virtual adapters. Discovery sends the
    WS-Discovery probe out ALL of them: a camera on WiFi is invisible if the
    probe only leaves via the default-route NIC, which is the single most common
    reason "discovery finds nothing". Three independent sources are merged so a
    gap in any one still yields the real interface list."""
    ips: set[str] = set()

    # 1) default-route source IP — always the most relevant single NIC.
    primary = lan_ip()
    if primary:
        ips.add(primary)

    # 2) every address bound to the hostname — portable, and the reliable path
    #    on Windows where it enumerates each adapter (WiFi + virtual NICs).
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(res[4][0])
    except OSError:
        pass

    # 3) Linux (the Jetson): walk every interface directly — the authoritative
    #    source on-device, and the only one that reliably surfaces the hotspot.
    ips |= _linux_iface_ipv4s()

    usable = [a for a in ips if not a.startswith("127.")]
    return sorted(usable, key=_ip_sort_key)


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    """The subnet(s) this device sits on, for a targeted unicast sweep when
    multicast is blocked (WiFi "AP/client isolation"). The real netmask is read
    per interface on Linux; elsewhere a /24 is assumed. Anything wider than a /24
    is clamped to the /24 around our own address, so a sweep can never explode to
    thousands of hosts. Link-local (169.254/16) is skipped — nothing to find."""
    masks = _linux_iface_prefixes()          # {ip: prefixlen}, empty off-Linux
    nets: dict[str, ipaddress.IPv4Network] = {}
    for ip in local_ipv4s():
        if ip.startswith("169.254."):
            continue
        prefix = masks.get(ip, 24)
        if prefix < 24:                       # /16, /8 … -> just our /24
            prefix = 24
        try:
            net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        except ValueError:
            continue
        nets[str(net)] = net
    return list(nets.values())


# ---- Linux interface walk (SIOCGIF* ioctls; no-op on other platforms) ------

def _linux_ioctl(request: int) -> dict[str, bytes]:
    """Run one SIOCGIF* ioctl against every interface; return {name: raw sockaddr}.
    Returns {} anywhere fcntl/if_nameindex is unavailable (e.g. Windows)."""
    try:
        import fcntl
        import struct
    except ImportError:
        return {}
    if not hasattr(socket, "if_nameindex"):
        return {}
    out: dict[str, bytes] = {}
    try:
        names = [n for _idx, n in socket.if_nameindex()]
    except OSError:
        return {}
    for name in names:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ifreq = struct.pack("256s", name.encode()[:15])
            out[name] = fcntl.ioctl(s.fileno(), request, ifreq)
            s.close()
        except OSError:
            pass                              # interface has no IPv4 bound
    return out


def _linux_iface_ipv4s() -> set[str]:
    SIOCGIFADDR = 0x8915
    ips: set[str] = set()
    for raw in _linux_ioctl(SIOCGIFADDR).values():
        ips.add(socket.inet_ntoa(raw[20:24]))
    return ips


def _linux_iface_prefixes() -> dict[str, int]:
    SIOCGIFADDR = 0x8915
    SIOCGIFNETMASK = 0x891B
    addrs = _linux_ioctl(SIOCGIFADDR)
    masks = _linux_ioctl(SIOCGIFNETMASK)
    out: dict[str, int] = {}
    for name, raw in addrs.items():
        ip = socket.inet_ntoa(raw[20:24])
        mraw = masks.get(name)
        if mraw is None:
            continue
        mask = socket.inet_ntoa(mraw[20:24])
        try:
            out[ip] = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
        except ValueError:
            pass
    return out
