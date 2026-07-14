from pydantic import BaseModel


class Camera(BaseModel):
    camera_id: str
    camera_name: str
    room_name: str

    # The camera's own RTSP URL (manual entry or ONVIF discovery). MediaMTX
    # connects to it; everything else consumes MediaMTX's restream. Codec and
    # transport are auto-detected — no per-camera tuning fields.
    rtsp_url: str

    # Recording-only stream: the camera's SECOND ONVIF profile, standardized
    # to ~1080p by discovery when the camera supports it. None = record the
    # main stream as-is. The main stream itself is NEVER modified — AI and the
    # WebRTC/HLS live links always get the camera's raw native quality.
    record_rtsp_url: str | None = None

    # ONVIF control endpoint + credentials (filled by discovery; cameras.json
    # is gitignored). Enables PTZ and future re-probing/2-way audio.
    onvif_xaddr: str | None = None
    onvif_username: str | None = None
    onvif_password: str | None = None
    onvif_profile_token: str | None = None     # the profile whose stream feeds AI
    onvif_ptz_token: str | None = None         # profile bound to PTZ (may differ)
    ptz_supported: bool = False

    is_enabled: bool = True

    # Care-monitoring labeling (per the spec): cameras are numbered 1-4 and a
    # room may carry a designation — '*' if it has a bathroom entrance, '&' if it
    # houses the home's main entrance/egress. Used in alert/snapshot labels.
    camera_number: int | None = None          # 1..4
    has_bathroom_entrance: bool = False        # shown as '*'
    is_main_egress: bool = False               # shown as '&'
