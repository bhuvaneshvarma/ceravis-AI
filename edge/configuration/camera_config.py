from schemas.cameras import Camera, room_id
from configuration.config_store import ConfigStore


def _id_of(raw: dict) -> str:
    """The camera id of a RAW cameras.json record. cameras.json stores the room
    (not the derived id), so derive it — tolerating older files that still carry
    an explicit camera_id."""
    return str(raw.get("camera_id") or room_id(raw.get("room_name", "")))


class CameraConfig:

    def __init__(self) -> None:

        self.store = ConfigStore()

    def get_all(self) -> list[Camera]:

        cameras = self.store.load(
            "cameras.json"
        )

        return [
            Camera(**camera)
            for camera in cameras
        ]

    def get_enabled(self) -> list[Camera]:

        return [
            camera
            for camera in self.get_all()
            if camera.is_enabled
        ]

    def get_by_id(
        self,
        camera_id: str
    ) -> Camera | None:

        for camera in self.get_all():

            if camera.camera_id == camera_id:
                return camera

        return None

    @staticmethod
    def _canon(text: str) -> str:
        return (text or "").strip().upper().replace(" ", "_")

    def get_by_label(
        self,
        label: str
    ) -> Camera | None:
        """Resolve a CameraName label (KITCHEN, LIVING_ROOM, …) — or a plain
        camera_id — by camera name, room name or id, matched case-insensitively
        with spaces as underscores. THE one cloud-facing camera addressing rule:
        PTZ and recording playback both resolve through here."""

        key = self._canon(label)

        if not key:
            return None

        for camera in self.get_all():

            if key in (
                self._canon(camera.camera_name),
                self._canon(camera.room_name),
                self._canon(camera.camera_id),
            ):
                return camera

        return None

    def add(
        self,
        camera: Camera
    ) -> None:

        cameras = self.store.load(
            "cameras.json"
        )

        cameras.append(
            camera.model_dump()
        )

        self.store.save(
            "cameras.json",
            cameras
        )

    def update(
        self,
        camera_id: str,
        updated_camera: Camera
    ) -> bool:

        cameras = self.store.load(
            "cameras.json"
        )

        updated = False

        for index, camera in enumerate(cameras):

            if (
                _id_of(camera)
                == camera_id
            ):

                cameras[index] = (
                    updated_camera.model_dump()
                )

                updated = True

                break

        if updated:

            self.store.save(
                "cameras.json",
                cameras
            )

        return updated

    def delete(
        self,
        camera_id: str
    ) -> bool:

        cameras = self.store.load(
            "cameras.json"
        )

        original_count = len(cameras)

        cameras = [
            camera
            for camera in cameras
            if (
                _id_of(camera)
                != camera_id
            )
        ]

        if (
            len(cameras)
            == original_count
        ):
            return False

        self.store.save(
            "cameras.json",
            cameras
        )

        return True