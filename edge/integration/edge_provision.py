from __future__ import annotations

"""
Apply this device's edge_id to the frp tunnel at account verification.

The web app runs as an unprivileged user, so it can't edit /etc/frp/frpc.toml or
restart frpc itself. Instead it calls a LOCKED-DOWN privileged helper —
`/usr/local/bin/ceravis-apply-edge-id` (cloud/apply_edge_id.sh, installed by
cloud/install_frpc.sh with a NOPASSWD sudoers rule for EXACTLY that one command).
The helper validates the token, rewrites only the per-edge routing keys (the
`locations` of the live + API proxies and, if SSH is enabled, the `customDomains`
of the tcpmux proxy), verifies the config, and restarts frpc.

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


def helper_installed() -> bool:
    """Whether the privileged frpc helper is installed AND callable via sudo -n —
    i.e. whether an auto-apply can actually re-key the tunnel, or whether it will
    silently no-op and the operator must run the manual command. Public so the
    verify handler can tell the UI the truth instead of promising remote access a
    device without the helper can't deliver."""
    return bool(shutil.which("sudo")) and _helper_present()


def apply_edge_id_async(edge_id: str) -> None:
    """Run apply_edge_id after a short delay — long enough for the verify HTTP
    response (which may travel through the very frpc we're about to restart) to
    have flushed to the browser. Intended as a FastAPI BackgroundTask."""
    time.sleep(1.5)
    apply_edge_id_verified(edge_id)


def apply_edge_id(edge_id: str) -> bool:
    """Key every frpc proxy to <edge_id> (live/API paths + the SSH CONNECT host)
    and restart frpc, via the privileged helper. Returns True. Never raises."""
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


# =====================================================================
# Verification — "it was applied" must be a fact you can check, not a hope
# =====================================================================

_FRPC_CONF = "/etc/frp/frpc.toml"


def tunnel_status(edge_id: str | None = None) -> dict:
    """What the tunnel config ACTUALLY carries right now.

    apply_edge_id() reports whether the helper exited 0, which is not the same
    question as whether the tunnel is keyed to this device. A helper that ran
    against a config with no matching proxy block, or a config replaced by hand
    afterwards, both exit 0 and leave every live link dead. So read the file
    back and compare.
    """
    from configuration.account_config import effective_edge_id
    want = (edge_id or effective_edge_id() or "").strip()
    out = {"edge_id": want or None, "config": _FRPC_CONF,
           "installed": _helper_present(), "readable": False,
           "keyed": False, "reason": None}
    if not want:
        out["reason"] = "no edge_id on this device yet (account not verified)"
        return out
    try:
        with open(_FRPC_CONF, encoding="utf-8") as fh:
            conf = fh.read()
    except OSError as exc:
        # Unreadable is NOT the same as wrong: the file is root-owned on some
        # installs, so say so plainly instead of reporting a false negative.
        out["reason"] = f"cannot read {_FRPC_CONF} ({exc.__class__.__name__})"
        return out
    out["readable"] = True
    out["keyed"] = f'"/{want}"' in conf
    out["reason"] = (None if out["keyed"] else
                     f"frpc.toml does not route /{want} — live links are dead "
                     f"until 'sudo {_HELPER} {want}' succeeds")
    return out


def apply_edge_id_verified(edge_id: str, attempts: int = 3,
                           delay_secs: float = 3.0) -> bool:
    """apply_edge_id, then CHECK, and retry a few times before giving up.

    The single-shot version fails permanently on a transient cause — frpc mid
    restart, /etc briefly read-only after a boot, sudo not yet warm. Losing the
    tunnel means losing every live link AND the remote route used to diagnose
    it, so this is worth a few seconds of persistence.
    """
    edge_id = (edge_id or "").strip()
    if not edge_id:
        return False
    for attempt in range(1, max(1, attempts) + 1):
        apply_edge_id(edge_id)
        st = tunnel_status(edge_id)
        if st["keyed"] or not st["readable"]:
            # Unreadable config: the helper is the only writer and it reported
            # success, so treat that as done rather than retrying forever.
            if st["keyed"]:
                logger.info("frpc verified: routing /%s (attempt %d)",
                            edge_id, attempt)
            return True
        logger.warning("frpc NOT yet routing /%s (attempt %d/%d) — %s",
                       edge_id, attempt, attempts, st["reason"])
        if attempt < attempts:
            time.sleep(delay_secs)
    _loud_fail(edge_id, f"still not routing /{edge_id} after {attempts} attempts")
    return False


def provisioning_report(edge_id: str | None = None) -> dict:
    """One honest, shared summary of this device's remote-access provisioning:
    is the privileged helper installed, is frpc REALLY routing this edge_id right
    now, and — when it isn't — the EXACT commands to fix it by hand. Consumed by
    the verify response, GET /account and the monitor, so the UI and the operator
    never see three different stories about whether the tunnel is keyed.

    `fleet` says whether this device even uses the shared tunnel (DEVICE_STREAM_BASE
    set): a single-LAN device has no frpc to key, so the UI must not nag about it.
    """
    from config.settings import settings
    st = tunnel_status(edge_id)
    eid = st["edge_id"]
    return {
        "edge_id": eid,
        "fleet": bool(settings.device_stream_base.strip()),
        "helper_installed": st["installed"],
        "keyed": st["keyed"],
        "reason": st["reason"],
        # The by-hand fallback, ready to copy, when the auto-apply can't run.
        "install_cmd": "bash cloud/install_frpc.sh",
        "apply_cmd": (f"sudo {_HELPER} {eid}" if eid else None),
    }
