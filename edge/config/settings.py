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

    # ---- Streaming / RTSP transport ---------------------------------
    # "auto" (default) picks per camera at connect time with zero config: a
    # WIRED egress (clean, e.g. direct Ethernet) -> UDP + low jitter buffer
    # (minimal lag); a WIRELESS egress (lossy WiFi) -> TCP, which re-sends lost
    # packets and kills the "melting/mesh" corruption. Force "tcp"/"udp" to
    # override globally. rtsp_latency_ms is the TCP/auto-TCP jitter buffer;
    # rtsp_udp_latency_ms is the (low) buffer used for UDP links.
    rtsp_transport: str = "auto"
    rtsp_latency_ms: int = 200
    rtsp_udp_latency_ms: int = 50
    # Live WebSocket stream only — does NOT change what the AI engines see.
    stream_jpeg_quality: int = 70
    stream_max_width: int = 0          # 0 = full resolution; e.g. 960 downscales the wall feed

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

    # ---- Hybrid set-to-set matching ---------------------------------
    # The query is scored against EVERY stored vector of each recipient and
    # aggregated, instead of trusting a single top-1 vector. A lock needs a
    # strong score AND a clear margin over the runner-up AND a majority of the
    # recipient's vectors agreeing — so a fleeting look-alike can't steal the ID.
    reid_hybrid_alpha: float = 0.6           # weight on best-single vs mean-top-k
    reid_hybrid_top_k: int = 5               # how many top vectors form the consensus
    reid_match_margin: float = 0.06          # best - runner-up recipient must exceed this
    reid_hybrid_min_votes: int = 1           # ≥N of the recipient's vectors above floor
    reid_hybrid_vote_floor: float = 0.45     # cosine floor for a vector to "vote"

    # ---- Adaptive ReID (online learning) ----------------------------
    # While the target is matched with HIGH confidence, novel body embeddings
    # are captured live (vectors only — no frames) into a per-recipient adaptive
    # store that also participates in matching. This absorbs appearance drift
    # (clothing/shawl changes). Enrolled embeddings are never overwritten.
    reid_adaptive_enabled: bool = True
    reid_adaptive_max: int = 100             # per recipient; over cap, the most
                                             # redundant vector is dropped (keeps
                                             # diverse outfits, not just newest)
    reid_adaptive_min_score: float = 0.70    # only capture above this match score
    reid_adaptive_dedup_cos: float = 0.92    # skip near-duplicates of existing vectors
    reid_adaptive_min_interval_secs: float = 4.0  # min seconds between capture attempts
    reid_adaptive_rebuild_secs: float = 5.0  # min seconds between gallery rebuilds

    # ---- Pipeline focus / efficiency --------------------------------
    crop_padding_frac: float = 0.08          # margin around a person box for crops
    target_only_pose: bool = True            # once ReID locks the target, pose that crop only
    target_lock_ttl_secs: float = 5.0        # keep target lock this long after last sighting

    # ---- Tracking (clean-room BoT-SORT) -----------------------------
    # Two-stage ByteTrack association + Kalman + OSNet appearance fusion. The
    # appearance term (the SAME OSNet that feeds the gallery) is what stops the
    # target ID jumping to a person who crosses in front; a lost track is kept
    # for tracker_track_buffer frames and re-matched by appearance on return.
    tracker_high_thresh: float = 0.5
    tracker_low_thresh: float = 0.1
    tracker_new_track_thresh: float = 0.6
    tracker_track_buffer: int = 30           # frames a lost track survives for re-id
    tracker_match_thresh: float = 0.8
    tracker_proximity_thresh: float = 0.5    # appearance is vetoed below this IoU
    tracker_appearance_thresh: float = 0.25  # max appearance distance to fuse
    tracker_with_reid: bool = True           # fuse OSNet appearance into association
    # Appearance is computed for ALL people when ≥ this many are present (the
    # crossover case that needs it); with one person, OSNet runs only at reid_fps
    # to refresh the target feature for gallery matching — so the common single-
    # occupant case stays as cheap as before (no per-frame embedding).
    tracker_appearance_min_persons: int = 2

    # ---- Target lock manager (occlusion-safe single-target follow) --
    # IoU above which the target is considered occluded by another person — the
    # lock is then FROZEN (identity not updated, adaptive capture paused so the
    # intruder can't poison the gallery) until they separate.
    target_occlusion_iou: float = 0.30
    # Verified mismatches (target track scores below threshold while NOT occluded)
    # needed before the lock is released — so we drop a bad lock fast instead of
    # riding it until the TTL on the wrong person.
    target_mismatch_release_checks: int = 2
    # On reappearance after a lost track, a candidate must be within this many
    # body-widths of the target's last known position to be eligible (spatial
    # gate) on top of the appearance match.
    target_reacquire_max_dist_frac: float = 6.0

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
    # Sit<->stand transitions must be corroborated by head vertical motion
    # (head rises to stand, falls to sit) — stops a small seated shift, or the
    # legs leaving the frame, from flipping the label. View-invariant.
    posture_transition_head_frac: float = 0.15      # head move (× body length) to corroborate
    posture_transition_confirm_frames: int = 3      # frames of corroboration to switch
    sitting_min_secs: float = 5.0
    standing_min_secs: float = 2.0
    fall_torso_angle_deg: float = 60.0              # > = horizontal
    fall_confirmation_frames: int = 3
    fall_cooldown_secs: float = 30.0
    # ---- Scene-aware fall FSM ---------------------------------------
    # A fall is an EVENT with phases, not a single horizontal frame:
    #   upright -> rapid downward motion (impact) -> horizontal & near-floor
    #   -> immobile for a few seconds.
    # Requiring the impact velocity AND the post-fall stillness near the floor
    # crushes the false positives that angle-alone produced (bending, sitting on
    # the floor, exercising, a steep camera angle).
    fall_velocity_body_frac: float = 1.2     # head drop speed (body-lengths/sec) = impact
    fall_velocity_window_secs: float = 0.6   # window the drop speed is measured over
    fall_immobility_secs: float = 3.0        # must lie still this long to confirm
    fall_immobility_body_frac: float = 0.35  # max centroid drift (× body) to count as still
    # Floor reference: a zone whose name contains this keyword marks the ground
    # plane. A standing person's head sits ABOVE (outside) it; a fallen person's
    # head drops INTO it — a view-tolerant "near the floor" test. If no floor
    # zone is drawn the FSM still works on velocity + immobility (just without
    # the ground gate).
    floor_zone_keyword: str = "floor"
    fall_require_near_floor: bool = False    # if True, only confirm when head is near the floor

    # ---- Storage ---------------------------------------------------
    data_dir: str = "data"
    sqlite_path: str = "data/ceravis.db"

    # ---- Events / alerts -------------------------------------------
    # Event snapshots are saved S3-mirrored: <events_dir>/<device_id>/<date>/
    # <event_id>.jpg, so the cutover to the CERAVIS Health S3 bucket is a path
    # swap. rest_zone_keywords: a fall whose foot point is inside a zone whose
    # name contains one of these is treated as intentional lying (not an alarm).
    events_dir: str = "data/events"
    event_snapshot_quality: int = 80
    rest_zone_keywords: str = "bed,couch,sofa,recliner,chair,bench,lounge"
    # Recipient alone + no area change for this long -> inactivity event (also
    # the re-emit interval for the periodic activity snapshot). Default 30 min.
    inactivity_secs: float = 1800.0
    # Visit sessions: a visitor must be gone this long to close a visit; mid-
    # visit "still here" snapshot cadence.
    visit_absence_secs: float = 60.0
    visit_mid_secs: float = 300.0

    # ---- CERAVIS application server (cloud) ------------------------
    # The Spring app that owns user accounts. Setup verifies the operator's email
    # against it before onboarding continues. The ONLY thing to configure is the
    # server's address; no auth token (the app server accepts the email request
    # directly). Leave base_url empty to run the device standalone.
    ceravis_api_base_url: str = ""           # e.g. https://app.ceravishealth.in/ch
    ceravis_api_timeout_secs: float = 8.0
    # Externally-reachable base for the camera WebSocket streams sent to the app
    # server (e.g. ws://192.168.1.50:8000 or wss://edge.ceravis.in). Blank =
    # auto-derive from the host the browser used to reach the device.
    device_stream_base: str = ""

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
