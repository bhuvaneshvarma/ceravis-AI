import re

from pydantic import BaseModel, model_validator

# Derived identity fields excluded ONLY when writing cameras.json (so the file
# matches the app-server shape); they stay in API responses, which the frontend
# and pipeline address cameras by. See CameraConfig.add/update.
FILE_EXCLUDE = {"camera_id", "camera_name"}


def room_id(room_name: str) -> str:
    """The room label normalized to a path/enum-safe id: 'LIVING ROOM' ->
    'LIVING_ROOM'. This is the camera's stable id AND the second segment of its
    live link (…/<edge_id>/LIVING_ROOM/), so the two always agree."""
    return re.sub(r"[^A-Z0-9]+", "_", (room_name or "").upper()).strip("_") or "CAM"


class Camera(BaseModel):
    # A camera is identified by its ROOM. The room label (chosen from the setup
    # dropdown, in caps) is the one thing collected; id/name are derived from it.
    room_name: str

    # The camera's own RTSP URL (manual entry or ONVIF discovery). MediaMTX
    # connects to it; everything else consumes MediaMTX's restream. Codec and
    # transport are auto-detected — no per-camera tuning fields.
    rtsp_url: str

    # Recording-only stream: the camera's SECOND ONVIF profile, standardized to
    # ~1080p by discovery when supported. None = record the main stream as-is.
    # The main stream (AI + live links) is never modified.
    record_rtsp_url: str | None = None

    # ONVIF control endpoint + credentials (filled by discovery; cameras.json is
    # gitignored). Enables PTZ and future re-probing / 2-way audio.
    onvif_xaddr: str | None = None
    onvif_username: str | None = None
    onvif_password: str | None = None
    onvif_profile_token: str | None = None     # the profile whose stream feeds AI
    onvif_ptz_token: str | None = None         # profile bound to PTZ (may differ)
    ptz_supported: bool = False

    is_enabled: bool = True

    # Public live links, computed on save and stored so the app server, the UI
    # and cameras.json all read ONE canonical value. webrtc_url = MediaMTX's
    # WebRTC player page (https://<domain>/<edge_id>/<ROOM>/). hls_url reserved
    # (empty for now).
    webrtc_url: str = ""
    hls_url: str = ""

    # Derived from room_name. Present in API responses (the frontend + pipeline
    # address cameras by id) but written out of cameras.json (FILE_EXCLUDE) so
    # the file matches the app-server shape. Re-derived on load.
    camera_id: str = ""
    camera_name: str = ""

    @model_validator(mode="after")
    def _derive_identity(self):
        if not (self.camera_id or "").strip():
            self.camera_id = room_id(self.room_name)
        if not (self.camera_name or "").strip():
            self.camera_name = self.room_name
        return self
