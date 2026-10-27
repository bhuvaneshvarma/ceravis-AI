"""
The camera hotspot owns the Wi-Fi radio (edge/common/hotspot.py), driven with a
stand-in NetworkManager (every nmcli call answered from a small table):

1. creating it: AP mode, power saving off, then it is made the first to come
   up at boot (autoconnect priority) and any other saved Wi-Fi network — the
   one used during the install — stops connecting by itself (kept, not deleted)
2. another access point or a network already set not to connect is left alone
3. an existing hotspot is never replaced (cameras are paired to it), but it
   too takes the radio back from a saved Wi-Fi network
4. the fleet's own password (HOTSPOT_PASSWORD): a new hotspot gets it, an
   existing one with another password switches to it (same name), one that
   already has it is left alone
5. a failing nmcli step reports why, with the hint that fixes it

Pure Python: runs anywhere.

Run:  PYTHONPATH=edge python tests/test_hotspot.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

from config.settings import settings                        # noqa: E402

settings.data_dir = tempfile.mkdtemp(prefix="ceravis-hotspot-")

from common import hotspot                                  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


CON = hotspot.CON


class FakeNM:
    """nmcli as a table: the profiles saved on the device and their settings."""

    def __init__(self, profiles: dict[str, dict], fail_on: str = ""):
        # uuid -> {"id", "type", "autoconnect", "mode"}
        self.profiles = profiles
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def run(self, args, timeout=15.0):
        self.calls.append(list(args))
        a = args[1:]
        if self.fail_on and self.fail_on in " ".join(args):
            return 4, "Error: not authorized to control networking."
        if a[:3] == ["-t", "-f", "DEVICE,TYPE"]:
            return 0, "wlP1p1s0:wifi\nenP8p1s0:ethernet\nlo:loopback\n"
        if a[:3] == ["-t", "-f", "DEVICE,STATE"]:
            return 0, "wlP1p1s0:connected\n"
        if a[:3] == ["-t", "-f", "UUID,TYPE"]:
            return 0, "".join(f"{u}:{p['type']}\n" for u, p in self.profiles.items())
        if a[:2] == ["-g", "connection.uuid"]:
            uuid = next((u for u, p in self.profiles.items() if p["id"] == a[-1]), "")
            return (0, uuid + "\n") if uuid else (10, "no such connection")
        if a[0] == "-g" and a[1].startswith("connection.autoconnect,"):
            p = self.profiles.get(a[-1])
            return (0, f"{p['autoconnect']}\n{p['mode']}\n{p['id']}\n") if p else (10, "")
        if a[0] == "-g" and a[1] == "802-11-wireless.ssid":
            p = next((p for p in self.profiles.values() if p["id"] == CON), None)
            return (0, p.get("ssid", "") + "\n") if p else (10, "")
        if a[:3] == ["-s", "-g", "802-11-wireless-security.psk"]:
            p = next((p for p in self.profiles.values() if p["id"] == CON), None)
            return (0, p.get("psk", "") + "\n") if p else (10, "")
        if a[:3] == ["-t", "-f", "NAME"]:
            return 0, CON + "\n"
        if a[:2] == ["connection", "add"]:
            self.profiles["hs-uuid"] = {"id": CON, "type": "802-11-wireless",
                                       "autoconnect": "yes", "mode": "ap",
                                       "ssid": a[a.index("ssid") + 1]}
            return 0, "added"
        if a[:2] == ["connection", "modify"]:
            key = a[2]
            target = self.profiles.get(key) or next(
                (p for p in self.profiles.values() if p["id"] == key), None)
            if target is not None:
                for k, v in zip(a[3::2], a[4::2]):
                    if k == "connection.autoconnect":
                        target["autoconnect"] = v
                    elif k == "connection.autoconnect-priority":
                        target["priority"] = v
                    elif k == "802-11-wireless.mode":
                        target["mode"] = v
                    elif k == "wifi-sec.psk":
                        target["psk"] = v
            return 0, ""
        if a[:2] == ["connection", "delete"]:
            for u in [u for u, p in self.profiles.items() if p["id"] == a[2]]:
                del self.profiles[u]
            return 0, ""
        return 0, ""


def install(nm: FakeNM) -> None:
    hotspot._run = nm.run
    hotspot.time.sleep = lambda *_: None


print("\n1. creating the hotspot takes the radio")
nm = FakeNM({"home-uuid": {"id": "isoWhiz NW Backup", "type": "802-11-wireless",
                           "autoconnect": "yes", "mode": "infrastructure"},
             "eth-uuid": {"id": "Wired connection 1", "type": "802-3-ethernet",
                          "autoconnect": "yes", "mode": ""}})
install(nm)
r = hotspot.start("CERAVIS-TEST1", "abcdefghjk")
hs = next(p for p in nm.profiles.values() if p["id"] == CON)
check("up, AP mode, the name given", r["active"] and hs["mode"] == "ap" and hs["ssid"] == "CERAVIS-TEST1")
check("power saving off on the AP", any("802-11-wireless.powersave" in c and "2" in c for c in nm.calls))
check("the hotspot comes up first at boot (priority 100)", hs.get("priority") == "100")
check("the install's Wi-Fi no longer connects by itself",
      nm.profiles["home-uuid"]["autoconnect"] == "no")
check("…kept, not deleted", "home-uuid" in nm.profiles)
check("the Ethernet profile is untouched", nm.profiles["eth-uuid"]["autoconnect"] == "yes")
check("its name and password are saved for the setup screen",
      hotspot.saved_credentials() == {"ssid": "CERAVIS-TEST1", "password": "abcdefghjk"})

print("\n2. what is left alone")
nm = FakeNM({"other-ap": {"id": "spare-ap", "type": "802-11-wireless",
                          "autoconnect": "yes", "mode": "ap"},
             "manual": {"id": "office", "type": "802-11-wireless",
                        "autoconnect": "no", "mode": "infrastructure"}})
install(nm)
hotspot.start("CERAVIS-TEST2", "abcdefghjk")
check("another access point profile is not touched", nm.profiles["other-ap"]["autoconnect"] == "yes")
check("a network already not connecting by itself gets no needless change",
      not any(c[1:3] == ["connection", "modify"] and c[3] == "manual" for c in nm.calls))

print("\n3. an existing hotspot is kept — and still takes the radio back")
nm = FakeNM({"hs-uuid": {"id": CON, "type": "802-11-wireless", "autoconnect": "yes",
                         "mode": "ap", "ssid": "CERAVIS-KEEP"},
             "home-uuid": {"id": "isoWhiz NW Backup", "type": "802-11-wireless",
                           "autoconnect": "yes", "mode": "infrastructure"}})
install(nm)
r = hotspot.ensure_default()
check("not replaced (cameras are paired to it)",
      r["created"] is False and r["ssid"] == "CERAVIS-KEEP"
      and not any(c[1:3] == ["connection", "delete"] for c in nm.calls))
check("the saved Wi-Fi stops connecting by itself here too",
      nm.profiles["home-uuid"]["autoconnect"] == "no")
check("the hotspot keeps connecting by itself", nm.profiles["hs-uuid"]["autoconnect"] == "yes")

print("\n4. the fleet's own password (HOTSPOT_PASSWORD in the secrets file)")
import os                                                    # noqa: E402

store = Path(tempfile.mkdtemp(prefix="ceravis-credstore-"))
layout = store / "layout.env"
layout.write_text(f"CERAVIS_CREDSTORE={store}\n")
os.environ["CERAVIS_LAYOUT_FILE"] = str(layout)
check("none set -> none (each device makes its own)", hotspot.fleet_password() == "")
(store / "hotspot_password").write_text("fleet-wide-pass1\n")
check("read from the root-only credential store", hotspot.fleet_password() == "fleet-wide-pass1")

nm = FakeNM({})
install(nm)
r = hotspot.ensure_default()
hs = next(p for p in nm.profiles.values() if p["id"] == CON)
check("a new hotspot gets it", r["created"] is True and hs.get("psk") == "fleet-wide-pass1")

nm = FakeNM({"hs-uuid": {"id": CON, "type": "802-11-wireless", "autoconnect": "yes",
                         "mode": "ap", "ssid": "CERAVIS-KEEP", "psk": "OldRandomPw42"}})
install(nm)
r = hotspot.ensure_default()
hs = next(p for p in nm.profiles.values() if p["id"] == CON)
check("an existing hotspot with another password switches to it — same name",
      hs.get("psk") == "fleet-wide-pass1" and hs.get("ssid") == "CERAVIS-KEEP" and r["created"] is False,
      str(hs))
check("…and the setup screen shows the new one",
      hotspot.saved_credentials() == {"ssid": "CERAVIS-KEEP", "password": "fleet-wide-pass1"})

nm = FakeNM({"hs-uuid": {"id": CON, "type": "802-11-wireless", "autoconnect": "yes",
                         "mode": "ap", "ssid": "CERAVIS-KEEP", "psk": "fleet-wide-pass1"}})
install(nm)
hotspot.ensure_default()
check("already in force -> nothing rebuilt (cameras stay connected)",
      not any(c[1:3] == ["connection", "delete"] for c in nm.calls))
del os.environ["CERAVIS_LAYOUT_FILE"]

print("\n5. a failing step says why")
nm = FakeNM({}, fail_on="connection add")
install(nm)
try:
    hotspot.start("CERAVIS-TEST3", "abcdefghjk")
    check("refused with a reason", False)
except hotspot.HotspotError as exc:
    check("refused with the fixing hint", exc.status == 502 and "install_hotspot.sh" in str(exc),
          str(exc)[:90])

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All hotspot checks passed.")
