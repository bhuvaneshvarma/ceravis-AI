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
    # Capture rate decoupled from inference. The frame buffer keeps only the
    # latest frame, so each consumer (detection/pose/stream) samples the
    # newest frame at ITS own rate — raising capture just removes the
    # 5 fps ceiling without queueing or extra GPU work (decode is on NVDEC,
    # dedicated hardware, not the GPU compute cores).
    target_camera_fps: float = 15.0
    stream_fps: float = 15.0           # WebSocket live view smoothness (CPU JPEG)
    read_timeout_secs: float = 3.0
    reconnect_delay_secs: float = 5.0
    max_reconnect_delay_secs: float = 30.0
    frame_stale_secs: float = 5.0

    # ---- Detection (YOLO26m) ----------------------------------------
    # Detection must scan the full frame, so it's the heaviest stage. 10 fps
    # is ample to catch new people; ByteTrack carries IDs between detections.
    # For 3-4 cameras dial this to 6-8 to keep GPU headroom.
    detection_weights: str = "yolo26m.pt"     # ultralytics auto-downloads
    detection_model_path: str = "models/detection/yolo26m.engine"
    detection_confidence_threshold: float = 0.35
    detection_input_size: int = 640
    detection_fps: float = 10.0

    # ---- Pose (YOLO26m-Pose) ---------------------------------------
    # Pose only runs when a person is present (idle-gated), and once a target
    # is locked it runs on the target's CROP only (smaller, sharper, focused).
    pose_weights: str = "yolo26m-pose.pt"     # ultralytics auto-downloads
    pose_model_path: str = "models/pose/yolo26m-pose.engine"
    pose_input_size: int = 640
    pose_confidence_threshold: float = 0.35
    pose_fps: float = 12.0

    # ---- ReID (OSNet by default; FastReid supported) ----------------
    # OSNet x1_0 is light + accurate — ideal for the Orin Nano. Build its
    # engine with scripts/export_reid.sh. FastReid BoT_R50 works too: drop its
    # ONNX at reid_onnx_path and set reid_embedding_dim=2048.
    reid_model_name: str = "osnet_x1_0"      # torchreid model for export_reid.sh
    reid_model_path: str = "models/reid/reid.engine"
    reid_onnx_path: str = "models/reid/reid.onnx"
    reid_onnx_url: str = ""                   # optional: download a prebuilt ONNX
    reid_input_height: int = 256
    reid_input_width: int = 128
    reid_embedding_dim: int = 512            # osnet_x1_0 = 512; BoT_R50 = 2048
    reid_fps: float = 3.0
    reid_match_threshold: float = 0.55       # cosine; tune per gallery

    # ---- Adaptive ReID (online learning) ----------------------------
    # While the target is matched with HIGH confidence, novel body embeddings
    # are captured live (vectors only — no frames) into a per-recipient adaptive
    # store that also participates in matching. This absorbs appearance drift
    # (clothing/shawl changes). Enrolled embeddings are never overwritten.
    reid_adaptive_enabled: bool = True
    reid_adaptive_max: int = 50              # capped FIFO per recipient (newest kept)
    reid_adaptive_min_score: float = 0.70    # only capture above this match score
    reid_adaptive_dedup_cos: float = 0.92    # skip near-duplicates of existing vectors
    reid_adaptive_rebuild_secs: float = 5.0  # min seconds between gallery rebuilds

    # ---- Pipeline focus / efficiency --------------------------------
    crop_padding_frac: float = 0.08          # margin around a person box for crops
    target_only_pose: bool = True            # once ReID locks the target, pose that crop only
    target_lock_ttl_secs: float = 5.0        # keep target lock this long after last sighting

    # ---- Tracking (ByteTrack) ---------------------------------------
    tracker_high_thresh: float = 0.5
    tracker_low_thresh: float = 0.1
    tracker_new_track_thresh: float = 0.6
    tracker_track_buffer: int = 30
    tracker_match_thresh: float = 0.8

    # ---- Posture (sitting / standing / walking / fallen) ------------
    # Walking is scale-normalized (motion relative to the person's own body
    # size) and temporally confirmed, so a chair-swivel near the camera is no
    # longer mistaken for walking. The legacy pixel threshold is retained for
    # back-compat but no longer drives the decision.
    walking_motion_threshold_pixels: float = 25.0   # legacy (superseded)
    walking_motion_window_secs: float = 1.5
    walking_motion_body_fraction: float = 0.6       # body-lengths / sec to qualify
    walking_confirm_frames: int = 3                 # consecutive frames before WALKING
    walking_min_pixels: float = 12.0                # absolute displacement floor
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

    model_config = SettingsConfigDict(
        # Resolve relative to the repo, not the process cwd, so the env file
        # is found whether launched by systemd, a shell, or an IDE.
        env_file=str(Path(__file__).resolve().parents[2] / "infra" / "env" / "jetson.env"),
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
