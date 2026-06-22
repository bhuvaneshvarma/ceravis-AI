from __future__ import annotations

from configuration.config_store import ConfigStore


class AccountConfig:
    """Persists the verified operator account (account.json) so the device
    remembers who set it up — including the ceravisUserId used to associate
    alerts with the cloud account."""

    FILE = "account.json"

    def __init__(self) -> None:
        self.store = ConfigStore()

    def get(self) -> dict:
        data = self.store.load(self.FILE)
        return data if isinstance(data, dict) else {}

    def save(self, account: dict) -> None:
        self.store.save(self.FILE, account)
