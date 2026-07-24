from __future__ import annotations

"""
Apply this device's edge_id to the frp tunnel at account verification.

The web app runs as an unprivileged user, so it can't edit /etc/frp/frpc.toml or
restart frpc itself. Instead it calls a LOCKED-DOWN privileged helper —
`/usr/local/bin/ceravis-apply-edge-id` (cloud/apply_edge_id.sh, installed by
cloud/install_frpc.sh with a NOPASSWD sudoers rule for EXACTLY that one command).
The helper validates the token, rewrites only the marked `locations` line, verifies
the config, and restarts frpc.

Everything here is BEST-EFFORT: any failure (no sudo, helper not installed, dev
box) is logged and swallowed, never raised — the edge_id is already saved to
account.json + jetson.env, so onboarding never fails and a manual frpc setup
(`bash cloud/install_frpc.sh`) stays possible.
"""

import logging
import os
import shutil
import subprocess
import time

logger = logging.getLogger("integration")

_HELPER = "/usr/local/bin/ceravis-apply-edge-id"


def _helper_present() -> bool:
    return os.path.isfile(_HELPER) and os.access(_HELPER, os.X_OK)


def apply_edge_id_async(edge_id: str) -> None:
    """Run apply_edge_id after a short delay — long enough for the verify HTTP
    response (which may travel through the very frpc we're about to restart) to
    have flushed to the browser. Intended as a FastAPI BackgroundTask."""
    time.sleep(1.5)
    apply_edge_id(edge_id)


def apply_edge_id(edge_id: str) -> bool:
    """Patch frpc.toml's mediamtx-webrtc `locations` to /<edge_id> and restart
    frpc, via the privileged helper. Returns True on success. Never raises."""
    edge_id = (edge_id or "").strip()
    if not edge_id:
        return False
    if not shutil.which("sudo") or not _helper_present():
        logger.warning(
            "edge_id %s NOT auto-applied to the tunnel (sudo/helper missing). "
            "Run 'bash cloud/install_frpc.sh' to install the helper, or set "
            "locations = [\"/%s\"] in cloud/frpc.toml and restart frpc.",
            edge_id, edge_id)
        return False
    try:
        r = subprocess.run(["sudo", "-n", _HELPER, edge_id],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            logger.info("frpc updated with edge_id %s and restarted", edge_id)
            return True
        logger.warning("frpc auto-apply failed (rc=%s): %s", r.returncode,
                       (r.stderr or r.stdout or "").strip()[:300])
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("frpc auto-apply error: %s", exc)
    return False
