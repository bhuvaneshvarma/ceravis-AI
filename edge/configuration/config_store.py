import json
import os
from pathlib import Path


class ConfigStore:

    def __init__(self):

        self.base_path = Path("data")

        self.base_path.mkdir(
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

        # ATOMIC: write a sibling temp file, flush it to disk, then rename over
        # the original. Opening the real file with "w" truncates it first, so a
        # power cut or a kill mid-write (the nightly reboot, a hard power-off)
        # left cameras.json / account.json empty or half-written — a device that
        # boots with no cameras and no account. The rename is all-or-nothing.
        file_path = self.base_path / filename
        tmp_path = file_path.with_name(file_path.name + ".tmp")

        with open(tmp_path, "w") as file:
            json.dump(
                data,
                file,
                indent=4
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(tmp_path, file_path)