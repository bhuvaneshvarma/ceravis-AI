from __future__ import annotations

import logging

from configuration.config_store import ConfigStore

logger = logging.getLogger("account")


def canonical_edge_id(raw: str | None) -> str:
    """THE single normalizer for a device edge_id / deviceToken — applied in ONE
    place (account ingest) so the value is byte-identical everywhere it lands:
    account.json, jetson.env, frpc `locations`, the MediaMTX live path, the
    public live-link segment, and the control-auth compare.

    The app server owns the token, so it is stored VERBATIM — never remapped.
    The only transform is stripping surrounding whitespace and any stray leading/
    trailing '/', which would break the `/<edge_id>` path routing (the backend
    already guarantees no interior '/'). A token that still contains a path
    separator or whitespace after that is refused (returns '') and logged, rather
    than silently mangled into a value that no longer matches the backend's."""
    eid = (raw or "").strip().strip("/").strip()
    if not eid:
        return ""
    if any(c.isspace() for c in eid) or "/" in eid:
        logger.warning("edge_id rejected: contains '/' or whitespace (%r) — the "
                       "app server must issue a path-safe deviceToken", raw)
        return ""
    return eid


class AccountConfig:
    """Persists the verified operator account (account.json) so the device
    remembers who set it up — including the ceravisUserId used to associate
    alerts with the cloud account and the edgeId used to route live links."""

    FILE = "account.json"

    def __init__(self) -> None:
        self.store = ConfigStore()

    def get(self) -> dict:
        data = self.store.load(self.FILE)
        return data if isinstance(data, dict) else {}

    def save(self, account: dict) -> None:
        self.store.save(self.FILE, account)

    def get_edge_id(self) -> str:
        """This device's edge_id as handed back by the app server at account
        verification (stored in account.json). '' when not yet verified."""
        return str(self.get().get("edgeId") or "").strip()


def effective_edge_id() -> str:
    """THE one resolver for this device's edge_id — the routing token that is the
    first segment of every live link, the key frp routes on, and the value the
    control endpoints verify. The account.json value (from userDetails) wins so a
    freshly-verified device routes correctly with no restart; jetson.env's
    EDGE_ID is the persisted fallback used on the next boot."""
    from config.settings import settings
    return AccountConfig().get_edge_id() or settings.edge_id.strip()


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
