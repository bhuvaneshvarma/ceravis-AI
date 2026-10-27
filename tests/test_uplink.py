"""
The internet reaches the device by Ethernet cable (its Wi-Fi runs the camera
hotspot) — what the device sees and what it tells the person at the device:

1. the Ethernet port, read from a stand-in /sys/class/net: plugged or not; the
   Wi-Fi radio, the USB-C link and virtual interfaces are never taken for it
2. the one verdict (api/setup_routes.internet_problem) the first-run screen and
   the status problems share: nothing when the app server answers, else the
   step that fixes it — plug a cable in, use a LAN port, check the router
3. the addresses shown for the home network: never the hotspot, the USB-C link
   or a container's (Docker's 172.17.0.1 once showed as "Home network")

Pure Python: runs anywhere.

Run:  PYTHONPATH=edge python tests/test_uplink.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

from common import net                                       # noqa: E402
from api import setup_routes                                 # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def iface(root: Path, name: str, kind="1", carrier: str | None = "1", device=True,
          wireless=False) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "type").write_text(kind + "\n")
    if carrier is not None:
        (d / "carrier").write_text(carrier + "\n")
    if device:
        (d / "device").mkdir()
    if wireless:
        (d / "wireless").mkdir()


print("\n1. the Ethernet port")
root = Path(tempfile.mkdtemp(prefix="ceravis-sysnet-"))
iface(root / "a", "lo", kind="772", device=False)
iface(root / "a", "wlP1p1s0", wireless=True)
iface(root / "a", "usb0")
iface(root / "a", "l4tbr0", device=False)
iface(root / "a", "enP8p1s0", carrier="1")
up = net.wired_uplink(root / "a")
check("the cable is seen on the real port", up.cable is True and up.iface == "enP8p1s0", str(up))
iface(root / "b", "wlP1p1s0", wireless=True)
iface(root / "b", "usb0", carrier="1")
iface(root / "b", "enP8p1s0", carrier="0")
down = net.wired_uplink(root / "b")
check("no cable -> not plugged (the USB-C link and Wi-Fi do not count)",
      down.cable is False and down.iface == "enP8p1s0", str(down))
iface(root / "c", "enP8p1s0", carrier=None)              # interface down: no carrier file
check("a port that is down reads as unplugged", net.wired_uplink(root / "c").cable is False)
check("not Linux -> unknown", net.wired_uplink(root / "nowhere").cable is None)

print("\n2. what the device says")
cases = [
    (True, net.WiredUplink(False, "enP8p1s0"), None, "internet works -> nothing to say"),
    (False, net.WiredUplink(False, "enP8p1s0"), "connect an Ethernet cable", "no cable"),
    (False, net.WiredUplink(True, "enP8p1s0"), "LAN port", "cable, no address from the router"),
    (False, net.WiredUplink(True, "enP8p1s0", "192.168.1.20"), "router's own internet",
     "address, but no internet"),
    (False, net.WiredUplink(None), "cannot be reached", "not knowable (dev PC)"),
]
real = setup_routes._reachable, setup_routes.wired_uplink
try:
    for reachable, port, want, label in cases:
        setup_routes._reachable = lambda r=reachable: r
        setup_routes.wired_uplink = lambda p=port: p
        said = setup_routes.internet_problem()
        check(label, said is None if want is None else (want in (said or "")), said or "")
finally:
    setup_routes._reachable, setup_routes.wired_uplink = real
check("the hotspot is a required check (the cameras' only network)",
      '_check("hotspot", "Camera hotspot on", hs_on, "required"'
      in (ROOT / "edge/api/setup_routes.py").read_text(encoding="utf-8"))

print("\n3. the addresses shown for the home network")
import socket                                                 # noqa: E402

real_ioctl, real_lan = net._linux_ioctl, net.lan_ip
on_device = {"lo": "127.0.0.1", "wlP1p1s0": "10.42.0.1", "l4tbr0": "192.168.55.1",
             "docker0": "172.17.0.1", "enP8p1s0": "192.168.1.20"}
try:
    net._linux_ioctl = lambda _req: {n: b"\0" * 20 + socket.inet_aton(ip)
                                     for n, ip in on_device.items()}
    check("only the Ethernet/Wi-Fi address — not the hotspot, the USB-C link or Docker",
          net.home_ipv4s(("10.42.0.1",)) == ["192.168.1.20"], str(net.home_ipv4s(("10.42.0.1",))))
    del on_device["enP8p1s0"]
    check("no cable, only the hotspot -> none at all (never a wrong one)",
          net.home_ipv4s(("10.42.0.1",)) == [], str(net.home_ipv4s(("10.42.0.1",))))
    net._linux_ioctl = lambda _req: {}
    net.lan_ip = lambda: "192.168.0.221"
    check("not Linux -> the default-route address", net.home_ipv4s() == ["192.168.0.221"])
finally:
    net._linux_ioctl, net.lan_ip = real_ioctl, real_lan
check("the setup screen and the install summary use it",
      "home_ipv4s((hotspot.ADDRESS,))" in (ROOT / "edge/api/setup_routes.py").read_text(encoding="utf-8")
      and "home_ipv4s((ADDRESS,))" in (ROOT / "setup/install.sh").read_text(encoding="utf-8"))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All uplink checks passed.")
