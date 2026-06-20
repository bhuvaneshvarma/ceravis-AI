from enum import Enum
from pydantic import BaseModel


class CameraCodec(str, Enum):
    H264 = "h264"
    H265 = "h265"


class Camera(BaseModel):
    camera_id: str
    camera_name: str
    room_name: str

    rtsp_url: str

    codec: CameraCodec = CameraCodec.H264

    is_enabled: bool = True

    # Per-camera RTSP tuning (override the global settings). A clean direct-
    # Ethernet camera wants "udp" + a low jitter buffer for minimal lag; a
    # lossy WiFi camera wants "tcp". None = use the global RTSP_TRANSPORT /
    # RTSP_LATENCY_MS defaults.
    transport: str | None = None              # "tcp" | "udp" | None
    rtsp_latency_ms: int | None = None        # jitter buffer ms; None = global