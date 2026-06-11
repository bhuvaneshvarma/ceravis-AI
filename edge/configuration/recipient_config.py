from schemas.recipient import Recipient
from configuration.config_store import ConfigStore


class RecipientConfig:

    def __init__(self):

        self.store = ConfigStore()


    def get_all(self):

        return self.store.load(
            "recipients.json"
        )


    def add(
        self,
        recipient: Recipient
    ):

        recipients = self.get_all()

        recipients.append(
            recipient.model_dump()
        )

        self.store.save(
            "recipients.json",
            recipients
        )