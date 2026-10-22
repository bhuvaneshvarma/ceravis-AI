import json

from common.atomic import write_json_atomic
from config.settings import settings


class ConfigStore:

    def __init__(self):

        self.base_path = settings.data_path

        self.base_path.mkdir(
            parents=True,
            exist_ok=True
        )


    def load(self, filename: str):

        file_path = self.base_path / filename

        if not file_path.exists():
            return []

        with open(file_path, "r") as file:
            return json.load(file)


    def save(
        self,
        filename: str,
        data
    ):

        # ATOMIC: opening the real file with "w" truncates it first, so a power
        # cut or a kill mid-write (the nightly reboot, a hard power-off) left
        # cameras.json / account.json empty or half-written — a device that
        # boots with no cameras and no account.
        write_json_atomic(self.base_path / filename, data, indent=4)