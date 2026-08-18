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

    # The camera's own RTSP URL (manual entry or ONVIF discovery) — its MAIN
    # profile, at native quality. This is the ONE connection made to the camera:
    # MediaMTX dials it and fans the same compressed stream out to the AI, the
    # live links, the UI pages and the disk recorder. There is deliberately no
    # second (sub-stream) URL: on a WiFi camera a second pull is bandwidth taken
    # straight from the first. Codec and transport are auto-detected — no
    # per-camera tuning fields.
    rtsp_url: str

    # Hardware descriptors, filled best-effort from the camera's ONVIF
    # GetDeviceInformation on save (camera_routes._enrich_device_info). They map
    # onto the app-server saveCamera shape as: device <- manufacturer,
    # model <- model, supplier <- serial (the `supplier` KEY carries the serial
    # number; the backend will rename that key to serialNumber later).
    manufacturer: str = ""
    model: str = ""
    serial: str = ""

    # An OPTIONAL second stream, read by the AI alone. Empty on almost every
    # camera, and that is the good case: it means one profile serves both roles
    # and the camera is dialled ONCE.
    #
    # It exists because the two consumers can genuinely conflict. Viewers need
    # H.264 (browsers do not decode HEVC, and recordings are remuxed into a
    # browser-played container). The AI wants pixels and does not care about the
    # codec — it decodes on NVDEC, which handles HEVC natively. On a camera whose
    # biggest stream is HEVC, `rtsp_url` above carries the playable H.264 one and
    # this carries the bigger one, so tracking keeps its reach without making
    # live view a black screen. Set by the setup wizard; see
    # onvif.client.recommend_streams for when it is populated at all.
    ai_rtsp_url: str = ""
    ai_profile_token: str | None = None

    # RESERVED — always empty. There is no second recording stream any more (the
    # main stream is what gets recorded, see livestream/mediamtx_client), but the
    # key stays because the app-server saveCamera contract carries recordRtspUrl
    # and every stored record must have the SAME shape. Nothing reads it, and the
    # validator below forces it empty, so it can never quietly resurrect a second
    # pull on a camera. Fields get added to this record over time; none are
    # removed out from under the backend.
    record_rtsp_url: str = ""

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
        # Reserved field, held empty on every load and save — so a value left in
        # an older cameras.json (or posted by a client) converges away instead of
        # lingering as a second, contradictory idea of which stream we record.
        self.record_rtsp_url = ""
        return self
