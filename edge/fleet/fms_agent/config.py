"""Agent settings, read from the environment. On a Jetson, systemd loads them
from the edge's own edge/infra/env/jetson.env (FMS_URL, FMS_ENROLL_KEY — the
same for every device), so a device needs no per-device configuration at all."""
from __future__ import annotations

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
            # machine (development) — never across a network.
            if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
                raise ConfigError(f"FMS_URL must be https:// (got {url!r})")
        # systemd's StateDirectory= sets STATE_DIRECTORY (a path list) to /var/lib/ceravis-fleet-agent.
        state = (env.get("FMS_STATE_DIR") or env.get("STATE_DIRECTORY", "").split(os.pathsep)[0]
                 or "/var/lib/ceravis-fleet-agent")
        return cls(url=url, enroll_key=env.get("FMS_ENROLL_KEY", "").strip(),
                   status_url=env.get("FMS_EDGE_STATUS_URL", DEFAULT_STATUS_URL).strip(),
                   state_dir=Path(state),
                   fingerprint=env.get("FMS_FINGERPRINT", "").strip())
