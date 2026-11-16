from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from config.config_store import ConfigStore

logger = logging.getLogger("account")


def canonical_edge_id(raw: str | None) -> str:
    """THE single normalizer for an edge_id / the app server's deviceToken, so a
    value compares byte-identical wherever it lands.

    It is stored VERBATIM — never remapped. The only transform is stripping
    surrounding whitespace and any stray leading/trailing '/', which would break
    the `/<edge_id>` path routing. A value that still contains a path separator
    or whitespace after that is refused (returns '') and logged, rather than
    silently mangled into one that no longer matches its issuer's."""
    eid = (raw or "").strip().strip("/").strip()
    if not eid:
        return ""
    if any(c.isspace() for c in eid) or "/" in eid:
        logger.warning("edge_id rejected: contains '/' or whitespace (%r) — the "
                       "app server must issue a path-safe deviceToken", raw)
        return ""
    return eid


class AccountConfig:
    """Persists the verified care account (account.json) so the device remembers
    who it serves — including the ceravisUserId used to associate alerts with
    the cloud account. It holds no edge_id: the FMS issues that to the device
    (effective_edge_id). An account.json written before the FMS may still carry
    the app server's old `edgeId`; only setup/sync_edge_id.sh --migrate reads it,
    once, to keep such a home routable until it enrolls."""

    FILE = "account.json"

    def __init__(self) -> None:
        self.store = ConfigStore()

    def get(self) -> dict:
        data = self.store.load(self.FILE)
        return data if isinstance(data, dict) else {}

    def save(self, account: dict) -> None:
        self.store.save(self.FILE, account)

    def clear(self) -> None:
        """No care account on this device any more (maintenance.recipient_reset)."""
        (self.store.base_path / self.FILE).unlink(missing_ok=True)


def effective_edge_id() -> str:
    """THE one resolver for this device's edge_id — the routing token that is the
    first segment of every live link, the key frp routes on, and the value the
    control endpoints verify.

    The FMS issues it when the fleet agent enrolls this device. Like every other
    secret it then lives in ONE root-only file, <credstore>/edge_id (written by
    setup/sync_edge_id.sh), which systemd hands the service as the credential
    `edge_id` -> settings.edge_id. A tool run by hand as root reads that file
    itself. '' until the device has enrolled. (EDGE_ID in jetson.env overrides
    it, for a bench without a fleet only.)"""
    from config.settings import settings
    return settings.edge_id.strip() or _edge_id_from_credstore()


@lru_cache(maxsize=1)
def _edge_id_from_credstore() -> str:
    """The credstore's edge_id file, for a process outside the service (a root
    shell tool). Unreadable — the service account, a dev PC, the tests — is ''."""
    from config.settings import layout_value
    store = layout_value("CERAVIS_CREDSTORE", "")
    if not store:
        return ""
    try:
        return canonical_edge_id((Path(store) / "edge_id").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""


def patient_user_id():
    """The cloud patientUserId (= ceravisUserId) of this device's verified
    account, or None. THE userId every patient-scoped /v1/ai/* call carries
    (saveCamera, getPatientPostures, uploadEmbeddingFile)."""
    return AccountConfig().get().get("ceravisUserId")


def account_recipient() -> dict | None:
    """The ONE care recipient this device serves: the verified account holder.

    This is the unification of the old recipients.json into account.json —
    there is no separate recipient store. The identifier is the account's own
    `ceravisUserId` used verbatim (= the cloud patientUserId): it keys the
    enrollment media/embeddings on disk AND is the userId every /v1/ai/* upload
    carries — no derived/prefixed key. None until the account is verified."""
    acct = AccountConfig().get()
    pid = acct.get("ceravisUserId")
    if not pid:
        return None
    name = " ".join(str(x) for x in (acct.get("firstName"),
                                     acct.get("lastName")) if x)
    return {
        "recipient_id": str(pid),                 # = ceravisUserId, verbatim
        "full_name": name or acct.get("email") or f"Patient {pid}",
        "is_primary_recipient": True,
        "patient_user_id": pid,
    }
