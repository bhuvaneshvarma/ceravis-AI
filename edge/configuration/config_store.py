import json
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

        file_path = self.base_path / filename

        with open(file_path, "w") as file:
            json.dump(
                data,
                file,
                indent=4
            )