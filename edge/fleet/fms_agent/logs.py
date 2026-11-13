"""The agent's three local logs, in the device's one log folder (<data>/logs):

    health.jsonl   every minute: the full detail of every part (Health.observe)
                   with the heartbeat summary built from it
    events.jsonl   only what CHANGED since the minute before: a part's state, a
                   warning raised or cleared, a device joining or leaving the
                   hotspot, the agent starting or stopping, a check-in problem
                   appearing or clearing
    fleet.jsonl    every exchange with the fleet server — each check-in in full: the
                   request body as sent and the reply — and every order it sent

Written whether or not the fleet server can be reached — a device that is
offline keeps its own record. What every field means: edge/LOGS.md.
"""
from __future__ import annotations

from . import __version__, collect

collect.use_edge_common()
from common.logbook import Logbook, log_dir       # noqa: E402  the ONE writer every log uses

MB = 1024 * 1024


class DeviceLogs:
    def __init__(self, data_dir) -> None:
        folder = log_dir(data_dir)
        self.health = Logbook(folder / "health.jsonl", max_bytes=25 * MB, backups=10)
        self.events = Logbook(folder / "events.jsonl", max_bytes=5 * MB, backups=4)
        self.fleet = Logbook(folder / "fleet.jsonl", max_bytes=10 * MB, backups=4)   # ≈ 2 weeks of check-ins
        self._summary: dict | None = None
        self._devices: dict[str, dict] | None = None

    # ---- each minute --------------------------------------------------------
    def observation(self, detail: dict, summary: dict) -> None:
        self.health.write({**detail, "summary": summary})
        for event in self._changes(detail, summary):
            self.events.write(event)
        self._summary = summary

    def _changes(self, detail: dict, summary: dict) -> list[dict]:
        events: list[dict] = []
        hw = detail["hardware"]
        if self._summary is None:
            events.append({"event": "agent_started", "version": __version__,
                           "boot_id": hw["boot_id"], "uptime_secs": hw["uptime_secs"],
                           "last_boot": hw["last_boot"], "state": summary["state"]})
        else:
            before = dict(_parts(self._summary))
            for part, now in _parts(summary):
                was = before.get(part, {"state": None, "warnings": []})
                if was["state"] != now["state"]:
                    events.append({"event": "state_changed", "part": part,
                                   "from": was["state"], "to": now["state"]})
                for w in now["warnings"]:
                    if w not in was["warnings"]:
                        events.append({"event": "warning_raised", "part": part, "warning": w})
                for w in was["warnings"]:
                    if w not in now["warnings"]:
                        events.append({"event": "warning_cleared", "part": part, "warning": w})
        devices = {d.get("mac"): d for d in
                   (((detail["app"] or {}).get("network") or {}).get("hotspot") or {}).get("devices") or []
                   if d.get("mac")}
        if detail["app"] is not None:
            if self._devices is not None:
                for mac in devices.keys() - self._devices.keys():
                    d = devices[mac]
                    events.append({"event": "hotspot_device_joined", "mac": mac, "ip": d.get("ip"),
                                   "hostname": d.get("hostname"), "signal_dbm": d.get("signal_dbm")})
                for mac in self._devices.keys() - devices.keys():
                    events.append({"event": "hotspot_device_left", "mac": mac,
                                   "ip": self._devices[mac].get("ip")})
            self._devices = devices
        return events

    def stopped(self) -> None:
        self.events.write({"event": "agent_stopped", "version": __version__})

    def check_in(self, problem: str | None, stopped: bool = False) -> None:
        """The check-in with the fleet server changed: a problem, or fine again."""
        if problem is None:
            self.events.write({"event": "fleet_check_in_ok"})
        else:
            self.events.write({"event": "fleet_check_in_stopped" if stopped else "fleet_check_in_failed",
                               "problem": problem})

    # ---- the fleet server ---------------------------------------------------
    def exchange(self, kind: str, ok: bool, **facts) -> None:
        self.fleet.write({"kind": kind, "ok": ok, **facts})

    def order(self, order: dict, result: dict) -> None:
        self.fleet.write({"kind": "order", "id": result.get("id"), "order": order.get("kind"),
                          "args": order.get("args"), "ok": result.get("ok"),
                          "duration_ms": result.get("duration_ms"),
                          "output_chars": len(result.get("output") or "")})


def _parts(summary: dict):
    """(name, {state, warnings}) for every part of a heartbeat summary."""
    yield "device", {"state": summary["state"], "warnings": []}
    yield "hardware", summary["hardware"]
    yield "network", summary["network"]
    yield "setup", summary["setup"]
    for name, part in summary["services"].items():
        yield f"services.{name}", part
    for camera in summary["cameras"]:
        yield f"cameras.{camera['camera_id']}", camera
