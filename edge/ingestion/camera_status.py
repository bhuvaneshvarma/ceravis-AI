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

    @property
    def is_stale(self) -> bool:

        return self.health_state not in (
            CameraHealthState.RUNNING,
            CameraHealthState.CONNECTING
        )