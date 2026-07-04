from pydantic import BaseModel


class Camera(BaseModel):
    camera_id: str
    camera_name: str
    room_name: str

    # The camera's own RTSP URL (manual entry or ONVIF discovery). MediaMTX
    # connects to it; everything else consumes MediaMTX's restream. Codec and
    # transport are auto-detected — no per-camera tuning fields.
    rtsp_url: str

    is_enabled: bool = True

    # Care-monitoring labeling (per the spec): cameras are numbered 1-4 and a
    # room may carry a designation — '*' if it has a bathroom entrance, '&' if it
    # houses the home's main entrance/egress. Used in alert/snapshot labels.
    camera_number: int | None = None          # 1..4
    has_bathroom_entrance: bool = False        # shown as '*'
    is_main_egress: bool = False               # shown as '&'
