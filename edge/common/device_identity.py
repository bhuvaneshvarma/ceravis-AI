"""
This device's own name — the one mechanism every other place takes it from:
the hostname, the camera hotspot's SSID, the tunnel's proxy prefix, DEVICE_ID
on status pages and in event folders, and the device record in the fleet.

The name comes from the Jetson module's serial number, burned into the module
and read from the device tree. Its code is the serial written in Crockford
base32 — a one-to-one encoding, so two different serials can NEVER give the
same code (no hash, no collision chance, nothing to look up). The same module
gets the same code after a re-flash or an SSD swap.

    serial 1424622063459  ->  code ACG08MBV3  ->  ceravis-acg08mbv3 / CERAVIS-ACG08MBV3

A serial that is not a plain number (another board) is hashed instead, and a
machine with no serial at all (a dev PC) falls back to its network card's MAC —
both marked by `source`, so a report can tell a guaranteed code from a fallback.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SERIAL_FILES = ("/proc/device-tree/serial-number",
                 "/sys/firmware/devicetree/base/serial-number")
# Crockford base32: digits and capitals without I, L, O, U — nothing on a label
# can be misread (0/O, 1/I/L) and no accidental words.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIX = "ceravis"


@dataclass(frozen=True)
class DeviceIdentity:
    code: str      # e.g. "ACG08MBV3"
    serial: str    # the raw serial ("" on a dev PC)
    source: str    # "serial" (guaranteed unique) | "serial-hash" | "mac"

    @property
    def name(self) -> str:
        """Hostname, DEVICE_ID and the frp proxy prefix: ceravis-acg08mbv3."""
        return f"{PREFIX}-{self.code.lower()}"

    @property
    def ssid(self) -> str:
        """The camera hotspot's network name: CERAVIS-ACG08MBV3."""
        return f"{PREFIX.upper()}-{self.code}"


def _base32(number: int) -> str:
    out = ""
    while True:
        number, digit = divmod(number, 32)
        out = _ALPHABET[digit] + out
        if not number:
            return out


def code_for(serial: str) -> tuple[str, str]:
    """(code, source) for a serial: digits -> exact base32, else a hash."""
    serial = serial.strip()
    if serial.isdigit():
        # A leading "1" keeps leading zeros: "0123" and "123" stay different.
        return _base32(int("1" + serial)), "serial"
    digest = int.from_bytes(hashlib.sha256(serial.encode()).digest()[:7], "big")
    return _base32(digest).rjust(12, "0"), "serial-hash"


def read_serial() -> str:
    for path in _SERIAL_FILES:
        try:
            raw = Path(path).read_bytes().replace(b"\x00", b"").decode().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if raw:
            return raw
    return ""


@lru_cache(maxsize=1)
def identity() -> DeviceIdentity:
    serial = read_serial()
    if serial:
        code, source = code_for(serial)
        return DeviceIdentity(code=code, serial=serial, source=source)
    # No serial (not a Jetson): the MAC, so a dev PC still gets a stable name.
    code, _ = code_for(f"mac:{uuid.getnode():012x}")
    return DeviceIdentity(code=code, serial="", source="mac")


if __name__ == "__main__":      # python3 -m common.device_identity  (setup scripts)
    import json
    import sys
    ident = identity()
    fields = {"code": ident.code, "name": ident.name, "ssid": ident.ssid,
              "serial": ident.serial, "source": ident.source}
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    print(fields[key] if key in fields else json.dumps(fields))
