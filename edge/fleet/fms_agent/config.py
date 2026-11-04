"""Agent settings, read from the environment. On a Jetson, systemd loads FMS_URL
from the edge's own edge/infra/env/jetson.env and the secret FMS_ENROLL_KEY from
the root-only /etc/ceravis-fleet-agent/enroll.env — both the same for every
device, so a device needs no per-device configuration at all. Once enrolled it
signs with its own key, but it keeps the enrollment key: enrolling again — after
an admin's Reset key, or into a rebuilt FMS — needs it.

enroll.env may also name another FMS (FMS_URL, e.g. a test server on this
network); it is loaded after jetson.env, so it wins."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_STATUS_URL = "http://127.0.0.1:8000/api/v1/system/status"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    url: str            # the FMS, e.g. https://fms.ceravishealth.in ("" = not configured)
    enroll_key: str     # the fleet enrollment key — proves this runs the CERAVIS image
    status_url: str     # the edge's own health surface, on this machine
    state_dir: Path     # where the enrolled identity is kept (never in the repo)
    fingerprint: str = ""   # override the hardware fingerprint source (simulator/tests only)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.enroll_key)

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        url = env.get("FMS_URL", "").strip().rstrip("/")
        if url:
            parsed = urlparse(url)
            local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
            # Orders arrive on this channel. Plain http is allowed only to this
            # machine (development) — never across a network — or, when
            # FMS_ALLOW_HTTP_LAN=1, to a test FMS at a private-network address.
            if parsed.scheme != "https" and not (
                    parsed.scheme == "http" and (local or lan_test_fms(url, env))):
                raise ConfigError(f"FMS_URL must be https:// (got {url!r}); a test FMS on this "
                                  "network over http needs FMS_ALLOW_HTTP_LAN=1 and an IP address")
        # systemd's StateDirectory= sets STATE_DIRECTORY (a path list) to /var/lib/ceravis-fleet-agent.
        state = (env.get("FMS_STATE_DIR") or env.get("STATE_DIRECTORY", "").split(os.pathsep)[0]
                 or "/var/lib/ceravis-fleet-agent")
        return cls(url=url, enroll_key=env.get("FMS_ENROLL_KEY", "").strip(),
                   status_url=env.get("FMS_EDGE_STATUS_URL", DEFAULT_STATUS_URL).strip(),
                   state_dir=Path(state),
                   fingerprint=env.get("FMS_FINGERPRINT", "").strip())


def lan_test_fms(url: str, env=os.environ) -> bool:
    """An http:// FMS allowed for testing: FMS_ALLOW_HTTP_LAN=1, and the address
    is a private-network IP literal (10.x, 172.16-31.x, 192.168.x, fc00::/7) —
    never a name, which could resolve anywhere. Over plain http the enrollment
    reply (this device's key) crosses the local network readable: a test bench
    only, never a home."""
    if env.get("FMS_ALLOW_HTTP_LAN", "").strip() != "1":
        return False
    parsed = urlparse(url)
    try:
        ip = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        return False
    return parsed.scheme == "http" and ip.is_private and not ip.is_loopback
