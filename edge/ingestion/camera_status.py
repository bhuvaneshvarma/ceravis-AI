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