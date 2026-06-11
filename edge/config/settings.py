from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """
    Global settings — loaded from infra/env/jetson.env.
    Everything tunable is here; env vars override these defaults.
    """

    # ---- Application -------------------------------------------------
    app_name: str = "CERAVIS"
    app_version: str = "0.1.0"
    environment: str = "development"
    is_production: bool = False
    log_level: str = "INFO"

    # ---- Ingestion (RTSP) -------------------------------------------
    target_camera_fps: float = 5.0
    stream_fps: float = 10.0
    read_timeout_secs: float = 3.0
    reconnect_delay_secs: float = 5.0
    max_reconnect_delay_secs: float = 30.0
    frame_stale_secs: float = 5.0

    # ---- Detection (YOLO26m) ----------------------------------------
    detection_weights: str = "yolov8m.pt"     # source weights, override to yolo26m.pt
    detection_model_path: str = "/models/detection/yolo26m.engine"
    detection_confidence_threshold: float = 0.35
    detection_input_size: int = 640
    detection_fps: float = 5.0

    # ---- Pose (YOLO26m-Pose) ---------------------------------------
    pose_weights: str = "yolov8m-pose.pt"     # source weights, override to yolo26m-pose.pt
    pose_model_path: str = "/models/pose/yolo26m-pose.engine"
    pose_input_size: int = 640
    pose_confidence_threshold: float = 0.35
    pose_fps: float = 2.0

    # ---- ReID (FastReid BoT_R50) ------------------------------------
    reid_model_path: str = "/models/reid/fastreid_bot_r50.engine"
    reid_input_height: int = 256
    reid_input_width: int = 128
    reid_embedding_dim: int = 2048           # BoT_R50 = 2048; ibn-R50 = 2048; mobilenet = 256
    reid_fps: float = 2.0
    reid_match_threshold: float = 0.55       # cosine on FastReid; tune per gallery

    # ---- Tracking (ByteTrack) ---------------------------------------
    tracker_high_thresh: float = 0.5
    tracker_low_thresh: float = 0.1
    tracker_new_track_thresh: float = 0.6
    tracker_track_buffer: int = 30
    tracker_match_thresh: float = 0.8

    # ---- Posture (sitting / standing / walking / fallen) ------------
    walking_motion_threshold_pixels: float = 25.0   # centroid disp / sec
    walking_motion_window_secs: float = 1.5
    sitting_min_secs: float = 5.0
    standing_min_secs: float = 2.0
    fall_torso_angle_deg: float = 60.0              # > = horizontal
    fall_confirmation_frames: int = 3
    fall_cooldown_secs: float = 30.0

    # ---- Storage ---------------------------------------------------
    data_dir: str = "data"
    sqlite_path: str = "data/ceravis.db"

    # ---- Cloud / MQTT ----------------------------------------------
    mqtt_endpoint: str = ""
    mqtt_port: int = 8883
    mqtt_topic_prefix: str = "ceravis/edge"
    device_id: str = "edge-0001"

    # ---- Model download URLs (used by export_models.py) -------------
    fastreid_onnx_url: str = ""               # override to point at your FastReid ONNX
    fastreid_onnx_path: str = "/models/reid/fastreid_bot_r50.onnx"

    model_config = SettingsConfigDict(
        env_file="infra/env/jetson.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
