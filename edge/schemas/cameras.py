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