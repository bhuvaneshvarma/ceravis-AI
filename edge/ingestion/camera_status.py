from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CameraHealthState(str, Enum):
    CONNECTING = "connecting"

    RUNNING = "running"

    RECONNECTING = "reconnecting"

    OFFLINE = "offline"

    ERROR = "error"


@dataclass(slots=True)
class CameraStatus:

    camera_id: str

    camera_name: str

    room_name: str

    is_running: bool

    health_state: CameraHealthState

    frames_captured: int

    reconnect_count: int

    last_frame_time: datetime | None
    current_fps: float = 0.0

    # The resolution we are ACTUALLY decoding, measured from the frames
    # themselves — not what the camera was configured with or what anyone
    # assumes. A camera pointed at its sub-stream by mistake looks completely
    # healthy on every other field here (running, good fps, no reconnects)
    # while quietly feeding the AI, the recordings and every live link a
    # fraction of the pixels. Nothing reported it, so nothing caught it.
    frame_width: int = 0
    frame_height: int = 0

    @property
    def is_stale(self) -> bool:

        return self.health_state not in (
            CameraHealthState.RUNNING,
            CameraHealthState.CONNECTING
        )


# A modern IP camera's MAIN profile is 1080p or better; its sub-stream is 720p
# or less. So a camera that is happily running at or below this is nearly always
# configured with the wrong RTSP path, not deliberately set low.
SUBSTREAM_MAX_HEIGHT = 720


def substream_warning(camera_id: str, width: int, height: int) -> str | None:
    """The message for a camera that looks like it is on its sub-stream, or None.

    This exists because a mis-pointed camera is invisible: it reports running,
    healthy fps and zero reconnects while feeding the AI, the recordings and
    every live link a fraction of the pixels. Resolution is the only field that
    tells the truth, so it gets checked rather than merely displayed."""
    if not (width and height) or height > SUBSTREAM_MAX_HEIGHT:
        return None
    return (f"{camera_id}: streaming only {width}x{height} — check its rtsp_url "
            f"points at the camera's MAIN profile, not a sub-stream")


def codec_warning(camera_id: str, codec: str | None) -> str | None:
    """The message for a camera whose codec nothing downstream can play, or None.

    H.264 is not a preference here, it is a requirement. No browser decodes HEVC
    over WebRTC, so live view is a BLACK SCREEN — on the /ui pages, the public
    links and the cloud alike. Recordings are remuxed as-is, so the clips are
    unplayable too. A camera can also be set to HEVC without ONVIF admitting it:
    the ver10 encoder schema has no H.265 element, so the camera reports "H264"
    while sending HEVC. That is why this checks the OBSERVED codec off the live
    stream rather than anything the camera claims about itself."""
    if not codec or codec.lower() == "h264":
        return None
    return (f"{camera_id}: streaming {codec.upper()} — no browser can play it, so "
            f"live view is black and its recordings are unplayable. Set this "
            f"camera's main profile to H.264.")