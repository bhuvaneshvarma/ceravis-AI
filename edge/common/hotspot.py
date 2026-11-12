"""
The camera hotspot — the Jetson's own Wi-Fi network that the household cameras
(and, during setup, the installer's phone) join. One module drives it, through
NetworkManager (AP mode + ipv4 shared, so NM runs DHCP/NAT): the web route
(api/network_routes) and first boot (`python3 -m common.hotspot ensure`) both
call it. The device itself is always 10.42.0.1 on this network.

The name and password in force are kept in data/hotspot.json (0600) so the
first-run setup screen can show them before anyone has signed in. A fleet-wide
password (HOTSPOT_PASSWORD in the install's secrets file) is used by every
device instead of a random one — fleet_password().

The service user needs NetworkManager rights — setup/install_hotspot.sh
installs the polkit rule that grants them.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import time
from pathlib import Path

from common.atomic import write_json_atomic
from config.settings import settings

logger = logging.getLogger("network")

ADDRESS = "10.42.0.1"        # NetworkManager's fixed address on its own shared network
CON = settings.hotspot_connection_name
# Wi-Fi passwords a person can read off a label and type: no 0/O, 1/l/I.
_PW_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


class HotspotError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _run(args: list[str], timeout: float = 15.0) -> tuple[int, str]:
    """Run one command; (returncode, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def wifi_device() -> str | None:
    """The AP-capable Wi-Fi interface (override via HOTSPOT_INTERFACE)."""
    if settings.hotspot_interface.strip():
        return settings.hotspot_interface.strip()
    code, out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return None


def _ensure_wifi_up(device: str) -> None:
    """A hotspot needs the Wi-Fi radio ON. On Ethernet-uplink installs the radio
    is often soft-blocked (rfkill) or switched off, which makes `connection up`
    fail with a misleading "no suitable device found". Turn it on and wait for
    the device to leave the "unavailable" state. Best-effort — if this can't
    help, the up step still surfaces the real error."""
    _run(["nmcli", "radio", "wifi", "on"])
    for _ in range(10):                        # up to ~5s: unavailable -> ready
        code, out = _run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], timeout=5)
        if code == 0 and any(
            (p := line.split(":"))[0] == device and len(p) >= 2 and p[1] != "unavailable"
            for line in out.splitlines()
        ):
            return
        time.sleep(0.5)


def is_active() -> bool:
    code, out = _run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"])
    return code == 0 and CON in out.splitlines()


def configured_ssid() -> str:
    code, out = _run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", CON])
    return out.strip() if code == 0 else ""


# `iw station dump` line -> the name it is reported under. Signal is what the
# device hears from that station (dBm: -50 strong, -70 fair, -80 weak); retries
# and failures are frames the radio had to resend or gave up on.
_STATION_FIELDS = {
    "signal": "signal_dbm", "signal avg": "signal_avg_dbm",
    "tx retries": "tx_retries", "tx failed": "tx_failed",
    "tx bitrate": "tx_bitrate_mbps", "rx bitrate": "rx_bitrate_mbps",
    "connected time": "connected_secs", "inactive time": "inactive_ms",
}


def _number(text: str):
    m = re.match(r"-?\d+(?:\.\d+)?", text.strip())
    if not m:
        return None
    return float(m.group()) if "." in m.group() else int(m.group())


def _stations(device: str) -> dict[str, dict]:
    """{mac: radio facts} for every device associated with the hotspot."""
    code, out = _run(["iw", "dev", device, "station", "dump"], timeout=5)
    stations: dict[str, dict] = {}
    current: dict | None = None
    for line in out.splitlines() if code == 0 else []:
        m = re.match(r"Station\s+([0-9a-f:]{17})", line, re.I)
        if m:
            current = stations.setdefault(m.group(1).lower(), {})
            continue
        key, sep, value = line.strip().partition(":")
        name = _STATION_FIELDS.get(key.strip())
        if current is not None and sep and name and _number(value) is not None:
            current[name] = _number(value)
    return stations


def _addresses(device: str) -> dict[str, dict]:
    """{mac: {ip, hostname}} from NetworkManager's DHCP leases, with the kernel's
    neighbour table (/proc/net/arp) as the fallback for the IP."""
    found: dict[str, dict] = {}
    try:
        for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) >= 6 and f[5] == device and f[3] != "00:00:00:00:00:00":
                found[f[3].lower()] = {"ip": f[0], "hostname": ""}
    except OSError:
        pass
    for lf in Path("/var/lib/NetworkManager").glob("dnsmasq-*.leases"):
        try:
            for line in lf.read_text().splitlines():
                f = line.split()
                if len(f) >= 4:
                    found[f[1].lower()] = {"ip": f[2], "hostname": "" if f[3] == "*" else f[3]}
        except OSError:
            continue
    return found


def clients(device: str) -> list[dict]:
    """Devices currently on the hotspot: each MAC the radio hears (`iw station
    dump`) with its signal and link quality, and its IP and hostname."""
    addresses = _addresses(device)
    return [{"mac": mac, "ip": addresses.get(mac, {}).get("ip", ""),
             "hostname": addresses.get(mac, {}).get("hostname", ""), **radio}
            for mac, radio in _stations(device).items()]


def radio(device: str) -> dict:
    """The hotspot radio's own settings: name on air, channel, width, power and
    whether power saving is on (it must be off: it delays every camera frame)."""
    out: dict = {}
    code, text = _run(["iw", "dev", device, "info"], timeout=5)
    if code == 0:
        for pattern, key in ((r"^\s*ssid (.+)$", "ssid"), (r"channel (\d+)", "channel"),
                             (r"channel \d+ \((\d+) MHz\)", "frequency_mhz"),
                             (r"width: (\d+) MHz", "width_mhz"), (r"txpower ([\d.]+) dBm", "tx_power_dbm")):
            m = re.search(pattern, text, re.M)
            if m:
                out[key] = m.group(1).strip() if key == "ssid" else _number(m.group(1))
    code, text = _run(["iw", "dev", device, "get", "power_save"], timeout=5)
    m = re.search(r"Power save:\s*(\w+)", text) if code == 0 else None
    out["power_save"] = m.group(1).lower() if m else None
    return out


def health() -> dict:
    """Everything about the hotspot in one read: on or off, its radio, and every
    device on it. Used by /api/v1/system/health (the fleet heartbeat and logs)."""
    device = wifi_device()
    if device is None:
        return {"supported": False, "on": False, "devices": []}
    on = is_active()
    return {"supported": True, "interface": device, "on": on,
            **(radio(device) if on else {}), "devices": clients(device) if on else []}


def _own_the_radio() -> None:
    """The radio is the cameras' from now on. The hotspot comes up first at
    every boot (top autoconnect priority), and any other Wi-Fi network saved
    on this device — the one used during the install, say — stops connecting
    by itself, so it can never take the radio back. Such a profile is kept: it
    can still be brought up by hand. Best-effort; each change is logged."""
    _run(["nmcli", "connection", "modify", CON, "connection.autoconnect-priority", "100"])
    code, own = _run(["nmcli", "-g", "connection.uuid", "connection", "show", CON])
    own = own.strip() if code == 0 else ""
    code, out = _run(["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"])
    if code != 0:
        return
    for line in out.splitlines():
        uuid, _, kind = line.partition(":")
        if kind != "802-11-wireless" or not uuid or uuid == own:
            continue
        code, detail = _run(["nmcli", "-g", "connection.autoconnect,802-11-wireless.mode,"
                             "connection.id", "connection", "show", uuid])
        values = detail.splitlines() if code == 0 else []
        if len(values) >= 2 and values[0] == "yes" and values[1] != "ap":
            _run(["nmcli", "connection", "modify", uuid, "connection.autoconnect", "no"])
            logger.info("hotspot: saved Wi-Fi '%s' no longer connects by itself "
                        "(the radio is the cameras' hotspot)", values[2] if len(values) > 2 else uuid)


def _credentials_file() -> Path:
    return settings.data_path / "hotspot.json"


def saved_credentials() -> dict:
    try:
        return dict(json.loads(_credentials_file().read_text()))
    except (OSError, ValueError):
        return {}


def new_password(length: int = 12) -> str:
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def fleet_password() -> str:
    """The fleet's own hotspot password — HOTSPOT_PASSWORD in the install's
    secrets file, kept by the installer in the root-only credential store (so a
    copy of a sealed master gets it at first boot too); "" when none is set."""
    from config.settings import layout_value
    store = Path(layout_value("CERAVIS_CREDSTORE", "/etc/ceravis/credstore"))
    try:
        return (store / "hotspot_password").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _current_password() -> str:
    code, out = _run(["nmcli", "-s", "-g", "802-11-wireless-security.psk", "connection", "show", CON])
    return out.strip() if code == 0 else ""


def start(ssid: str, password: str) -> dict:
    """Create (or rebuild) the hotspot with this name and password and bring it
    up. Raises HotspotError with an HTTP-style status and a hint."""
    ssid = ssid.strip()
    if not ssid:
        raise HotspotError(400, "SSID is required")
    if len(password) < 8:
        raise HotspotError(400, "WPA2 requires a password of at least 8 characters")
    device = wifi_device()
    if device is None:
        raise HotspotError(400, "no WiFi adapter found on this device")

    _ensure_wifi_up(device)
    _run(["nmcli", "connection", "delete", CON])          # rebuild from scratch
    steps = [
        ["nmcli", "connection", "add", "type", "wifi", "ifname", device,
         "con-name", CON, "autoconnect", "yes", "ssid", ssid],
        # powersave 2 = OFF: an access point that dozes between beacons holds
        # back every camera packet (200-1900 ms pings, streams stalling).
        ["nmcli", "connection", "modify", CON,
         "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
         "802-11-wireless.powersave", "2",
         "ipv4.method", "shared", "ipv6.method", "disabled",
         "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password],
        ["nmcli", "connection", "up", CON],
    ]
    for args in steps:
        code, out = _run(args, timeout=30)
        if code != 0:
            logger.warning("hotspot step failed: %s -> %s", " ".join(args[:4]), out)
            low = out.lower()
            if "not authorized" in low:
                hint = (" (service user lacks NetworkManager rights — run "
                        "setup/install_hotspot.sh)")
            elif "no suitable device" in low or "not available" in low:
                hint = (" (WiFi radio unavailable — run `sudo rfkill unblock wifi` "
                        "then `nmcli radio wifi on`)")
            else:
                hint = ""
            raise HotspotError(502, f"hotspot setup failed: {out.strip()[:200]}{hint}")
    _own_the_radio()
    try:
        write_json_atomic(_credentials_file(), {"ssid": ssid, "password": password},
                          mode=0o600)
    except OSError:
        logger.warning("hotspot is up but its details could not be saved", exc_info=True)
    logger.info("hotspot up: ssid=%s device=%s", ssid, device)
    return {"active": True, "ssid": ssid, "device": device}


def stop() -> None:
    code, out = _run(["nmcli", "connection", "down", CON], timeout=20)
    if code != 0 and "not an active" not in out.lower():
        raise HotspotError(502, f"hotspot stop failed: {out.strip()[:200]}")
    logger.info("hotspot down")


def ensure_default() -> dict:
    """First boot and every install: this device's own hotspot — CERAVIS-<code>
    with the fleet's password (fleet_password), else a new random one. One
    already set up keeps its name (cameras may be paired to it); only the
    fleet's password, when set, replaces a different one."""
    from common.device_identity import identity
    fixed = fleet_password()
    ssid = configured_ssid()
    if ssid and fixed and _current_password() != fixed:
        logger.info("hotspot %s: the fleet's password takes over", ssid)
        return {**start(ssid, fixed), "created": False}
    if ssid:
        if not is_active():
            _run(["nmcli", "connection", "up", CON], timeout=30)
        _own_the_radio()
        return {"active": is_active(), "ssid": ssid, "created": False}
    result = start(identity().ssid, fixed or new_password())
    return {**result, "created": True}


if __name__ == "__main__":           # python3 -m common.hotspot ensure
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if sys.argv[1:] == ["ensure"]:
        try:
            print(json.dumps(ensure_default()))
        except HotspotError as exc:
            print(f"hotspot: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print("usage: python3 -m common.hotspot ensure", file=sys.stderr)
        sys.exit(2)
