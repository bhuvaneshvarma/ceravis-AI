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
        _loud_fail(edge_id,
                   "sudo/helper missing — run 'bash cloud/install_frpc.sh'")
        return False
    try:
        r = subprocess.run(["sudo", "-n", _HELPER, edge_id],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            logger.info("frpc updated with edge_id %s: %s", edge_id,
                        (r.stdout or "").strip()[:200])
            return True
        _loud_fail(edge_id, f"helper rc={r.returncode}: "
                   + (r.stderr or r.stdout or "").strip()[:200])
    except (subprocess.SubprocessError, OSError) as exc:
        _loud_fail(edge_id, f"helper error: {exc}")
    return False


def _loud_fail(edge_id: str, why: str) -> None:
    """A failed tunnel update is what silently kills every live link, so surface
    it on the monitor's Cloud Sync Console (not just the log). Best-effort."""
    logger.warning("frpc auto-apply FAILED for edge_id %s — %s. Live links stay "
                   "dead until fixed: set locations = [\"/%s\"] in "
                   "/etc/frp/frpc.toml and restart frpc.", edge_id, why, edge_id)
    try:
        from integration import call_log
        call_log.record("event", False,
                        label="Live-link tunnel NOT updated — live links are dead",
                        error=why)
    except Exception:                                 # noqa: BLE001 — never raise
        pass
