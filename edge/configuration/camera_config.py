import re

from schemas.cameras import Camera, FILE_EXCLUDE, room_id
from configuration.config_store import ConfigStore


def _id_of(raw: dict) -> str:
    """The camera id of a RAW cameras.json record. cameras.json stores the room
    (not the derived id), so derive it — tolerating older files that still carry
    an explicit camera_id."""
    return str(raw.get("camera_id") or room_id(raw.get("room_name", "")))


# ---- device labels -------------------------------------------------------
# Every camera carries a stable CAM_nn identity, allocated HERE because add() is
# the single funnel through which a record enters cameras.json and it already
# holds the whole file — so "what is taken" and "append" happen in one place and
# two concurrent saves cannot mint the same label. See schemas.cameras.Camera.

DEVICE_LABEL_PREFIX = "CAM"
_LABEL_RE = re.compile(rf"^{DEVICE_LABEL_PREFIX}_(\d+)$")


def _label_index(label: str) -> int:
    """The number in 'CAM_07' -> 7. Zero when the value isn't one of ours (blank,
    hand-typed, or a label from some other scheme), which is exactly the set that
    needs reallocating."""
    match = _LABEL_RE.match((label or "").strip().upper())
    return int(match.group(1)) if match else 0


def _format_label(index: int) -> str:
    return f"{DEVICE_LABEL_PREFIX}_{index:02d}"


def _highest_index(raws: list[dict]) -> int:
    """The largest label number present in the file — the high-water mark that
    allocation counts up from."""
    return max((_label_index(raw.get("device_label", "")) for raw in raws),
               default=0)


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

    def ensure_device_labels(self) -> int:
        """Give every stored camera a device label, exactly once.

        Two jobs, both idempotent, so this is safe to run on every boot:
          * BACKFILL — records written before labels existed have none, and a
            device upgraded in the field would otherwise push an empty `device`
            to the cloud until each camera happened to be re-saved.
          * REPAIR — a hand-edited cameras.json can leave two cameras sharing a
            label, or one in the wrong case. The first record to claim a label
            keeps it; any later clash is reallocated ABOVE the high-water mark,
            never into a gap, so no camera silently adopts another's identity.

        Returns the number of records changed — 0 means nothing was written."""

        cameras = self.store.load(
            "cameras.json"
        )

        highest = _highest_index(cameras)
        taken: set[str] = set()
        changed = 0

        for raw in cameras:

            label = (raw.get("device_label") or "").strip().upper()

            if _label_index(label) and label not in taken:

                if raw.get("device_label") != label:      # normalize casing
                    raw["device_label"] = label
                    changed += 1

            else:                       # missing, malformed, or a duplicate

                highest += 1
                raw["device_label"] = _format_label(highest)
                changed += 1

            taken.add(raw["device_label"])

        if changed:

            self.store.save(
                "cameras.json",
                cameras
            )

        return changed

    def add(
        self,
        camera: Camera
    ) -> None:

        cameras = self.store.load(
            "cameras.json"
        )

        if not (camera.device_label or "").strip():

            camera.device_label = _format_label(
                _highest_index(cameras) + 1
            )

        cameras.append(
            camera.model_dump(exclude=FILE_EXCLUDE)
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

                # A client that PUTs a record without device_label (the wizard
                # posts only the fields it collects) must not silently retire
                # this camera's identity — carry the stored one forward.
                if not (updated_camera.device_label or "").strip():
                    updated_camera.device_label = (
                        camera.get("device_label") or ""
                    )

                cameras[index] = (
                    updated_camera.model_dump(exclude=FILE_EXCLUDE)
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