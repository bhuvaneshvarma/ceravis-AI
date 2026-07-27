from __future__ import annotations

from configuration.config_store import ConfigStore


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
    there is no separate recipient store. `recipient_id` keys the enrollment
    media/embeddings on disk AND maps to the cloud patient (patient_user_id =
    ceravisUserId), so a person's vectors upload against their own account.
    None until the account is verified."""
    acct = AccountConfig().get()
    pid = acct.get("ceravisUserId")
    if not pid:
        return None
    name = " ".join(str(x) for x in (acct.get("firstName"),
                                     acct.get("lastName")) if x)
    return {
        "recipient_id": f"ceravis-{pid}",
        "full_name": name or acct.get("email") or f"Patient {pid}",
        "is_primary_recipient": True,
        "patient_user_id": pid,
    }
