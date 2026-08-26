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
    # This is the AI's frame path and nothing else's — no viewer is ever served
    # from it (live video comes straight off MediaMTX over WebRTC), so every
    # setting here is tuned purely for getting the freshest possible frame to
    # YOLO. Capture rate is decoupled from inference: the frame buffer keeps only
    # the latest frame, so each consumer (detection/pose/reid) samples the
    # newest frame at ITS own rate. 0 = uncapped (drain at the camera's native
    # rate); any positive value is a ceiling on how often we decode.
    #
    # 15 fps: a deliberate ceiling, NOT a limitation. Nothing samples this buffer
    # faster than pose (12 fps); detection and tracking take 10, ReID 3, rules 1.
    # Uncapped, the reader decoded the camera's full native rate (~20-25 fps) and
    # colour-converted every frame to BGR — at 4K that is a 25 MB frame, so ~500
    # MB/s per camera of NVDEC + memory-bus traffic, HALF of which was overwritten
    # unread. 15 leaves every consumer a frame at most 66 ms old (well inside one
    # of its own intervals) and hands the saved bandwidth back to inference.
    # Raise it only if a consumer above is ever tuned past 15 fps.
    target_camera_fps: float = 15.0
    read_timeout_secs: float = 3.0
    reconnect_delay_secs: float = 5.0
    max_reconnect_delay_secs: float = 30.0
    frame_stale_secs: float = 5.0
    # Self-healing watchdog: if a reader that is RUNNING delivers NO frame for
    # this long, its loopback session has silently stalled (cv2.read() blocked
    # with no error while MediaMTX still serves the path to live view) — the
    # watchdog releases the capture to force a reconnect, so the AI recovers with
    # no manual restart. Comfortably above one frame interval at any real fps.
    camera_stall_reconnect_secs: float = 8.0

    # ---- MediaMTX (media backbone) -----------------------------------
    # MediaMTX is supervised as a CHILD of this process (one systemd service).
    # It connects to each camera exactly ONCE and fans the compressed stream
    # out to every consumer without re-encoding: the AI pipeline (local RTSP),
    # the browser/cloud (WebRTC + HLS over HTTPS), and the disk recorder
    # (person-triggered fMP4 segments). If the binary is missing the app falls
    # back to reading cameras directly (dev machines / degraded mode).
    mediamtx_binary: str = "/usr/local/bin/mediamtx"
    mediamtx_rtsp_port: int = 8554         # local restream the AI reads
    mediamtx_api_port: int = 9997          # control API (localhost only)
    mediamtx_hls_port: int = 8888          # HLS over HTTPS
    mediamtx_webrtc_port: int = 8889       # WebRTC signaling (WHEP) — the tunneled port
    # Fixed UDP port MediaMTX gathers WebRTC MEDIA on. The hybrid CCTV model:
    # signaling is TCP (WHEP over the frp tunnel), the video itself is UDP and
    # peer-to-peer. Keeping ONE fixed port stable lets STUN hole-punch it for
    # off-LAN viewers. It carries media DIRECTLY (never through the cloud), so it
    # is NOT tunneled by frp — it just has to be reachable from the home NAT.
    mediamtx_webrtc_udp_port: int = 8189
    mediamtx_cert_dir: str = "data/certs"  # server.crt/server.key (installer generates)
    # The resolution VIEWERS get, decided ONCE at registration: the wizard picks
    # the H.264 profile NEAREST this height and stores its RTSP URL. Nothing on
    # the camera is ever rewritten — we choose among what it already offers.
    #
    # "Nearest", not "largest at or below": a camera offering 1440p and 360p,
    # asked for 1080p, must give 1440p. The at-or-below rule hands back 360p and
    # silently destroys the picture.
    #
    # H.264 is not negotiable for this stream — it feeds the public live links,
    # the /ui tiles and the recordings, and no browser decodes HEVC over WebRTC.
    # Where a camera's BIGGEST stream is HEVC, the AI separately reads that one
    # (Camera.ai_rtsp_url) so tracking keeps its reach; see
    # onvif.client.recommend_streams for when that second pull happens at all.
    camera_preferred_height: int = 1080
    # Optional STUN server so MediaMTX advertises the device's PUBLIC address in
    # WebRTC ICE — needed for remote (off-LAN) live view via the cloud/ tunnel
    # (Option B). Empty = LAN-only (default: remote access OFF, zero change). A
    # public STUN is free and carries NO media (that stays P2P), e.g.
    # stun:stun.l.google.com:19302. Removing remote access = clear this again.
    mediamtx_stun_server: str = ""
    # Jitterbuffer depth for the AI's localhost RTSP pull (ms). 0 = NO BUFFER,
    # and that is correct here: the link is loopback interleaved TCP, which
    # delivers every packet, in order, with no loss — there is no jitter to
    # absorb, so any value above zero is pure added delay between the camera and
    # YOLO. (MediaMTX already absorbed the real network jitter on the camera
    # side; reassembly happens there, not here.) Raise it ONLY if a future
    # source is genuinely lossy — for a loopback pull it should stay 0.
    rtsp_latency_ms: int = 0

    # ---- Recording (person-triggered, the camera's native main stream) ----
    # Records ONLY while YOLO sees a person on that camera (+ post-roll), and
    # records the camera's MAIN stream — the one connection MediaMTX already
    # holds for live view and the AI. Remux only (the Orin Nano has no hardware
    # encoder, so re-encoding on the device is off the table), which means
    # footage is full native quality at zero extra camera bandwidth and zero
    # extra CPU. Disk is bounded by person-triggering plus the rolling window
    # below, not by degrading the picture. There is deliberately no sub-stream
    # option: a second pull on a WiFi camera steals bandwidth from the first and
    # destabilises live view and the AI together.
    record_enabled: bool = True
    record_dir: str = "data/recordings"
    record_segment_secs: int = 15
    # Rolling retention: keep only the last N hours of person-clips. Because we
    # record ONLY on person detection, self-expiring each segment N hours after
    # it was written yields exactly a rolling N-hour window of "who was in frame"
    # per camera — a camera that saw nobody stores nothing. MediaMTX does the
    # deletion (recordDeleteAfter).
    record_retention_hours: int = 12
    record_post_roll_secs: float = 15.0    # keep recording this long after the last person
    record_poll_secs: float = 0.5          # detection-buffer poll cadence
    # Recordings carry AAC AUDIO (video + AAC = the one MP4 combo every player
    # and browser accepts). The cameras speak G.711/PCM, which MP4 can only
    # hold as the 2023 'ipcm' box many players reject — so a tiny per-camera
    # FFmpeg (supervised by MediaMTX via runOnInit) republishes the main
    # stream with the VIDEO COPIED untouched and only the ~8 kHz mono audio
    # re-encoded to AAC (~32 kbps — negligible CPU and disk). Live view is
    # untouched: WebRTC speaks G.711 natively, which is why live audio always
    # worked. False = video-only clips (also the automatic fallback when
    # ffmpeg is missing on the device).
    record_audio: bool = True
    ffmpeg_binary: str = "ffmpeg"           # audio-only transcode for recordings

    # ---- Fall incident clip -----------------------------------------
    # On a FALL alert, a short clip around the incident is merged from the
    # already-recorded segments (the one before + the one containing it + the one
    # after) and sent to the cloud via saveSnapshot with the SAME alertId and
    # annotation as the alert/still snapshot — so the reviewer gets the moving
    # footage alongside the frame, linked by alertId. No re-encode (the stored
    # MPEG-TS is concatenated -c copy), so nothing runs on the Orin encoder.
    # Best-effort: if recording was OFF or no footage covers the instant the clip
    # is simply skipped (the alert + snapshot already went). Needs ffmpeg.
    fall_clip_enabled: bool = True
    fall_clip_pre_secs: float = 15.0        # footage kept BEFORE the fall instant
    fall_clip_post_secs: float = 15.0       # footage kept AFTER the fall instant
    fall_clip_cooldown_secs: float = 60.0   # one clip per camera per incident


    # ---- ONVIF (WiFi camera discovery / PTZ) --------------------------
    onvif_discovery_secs: float = 4.0      # WS-Discovery multicast listen window
    # When multicast finds nothing (common on WiFi with AP/client isolation),
    # sweep the local /24 for ONVIF devices. Bounded + thread-pooled; safe to
    # leave on. A "deep" scan (?deep=1) always sweeps and widens the port list.
    onvif_unicast_fallback: bool = True
    # This device's stable identity AND fleet routing token (a long unguessable
    # value like "home-9f3c1e2b…"). Two jobs:
    #   1) Live-link routing: it is the FIRST path segment of every public live
    #      link — https://<domain>/<edge_id>/<cam>/whep — and the key frp routes
    #      on (locations=["/<edge_id>"]). One shared domain + one shared port
    #      serve the whole fleet, disambiguated purely by this segment. Because it
    #      is effectively the access key to a house's cameras, make it long and
    #      unguessable, NOT "home_1234".
    #   2) PTZ/recording safety: the control endpoints OBEY ONLY commands whose
    #      edgeId matches this, so a misrouted command can't touch the wrong home.
    # Empty = single LAN device: no live-link prefix and no control check.
    edge_id: str = ""
    # Hard ceiling on a single PTZ move (ms). The edge always auto-stops after
    # this even if no stop/duration arrives, so a lost command can never leave a
    # motor spinning — which is why the cloud PTZ call has no stop action at all.
    # durationMs in the request is clamped to this.
    ptz_max_move_ms: int = 2000

    # ---- Hotspot (Jetson as WiFi AP for the household cameras) --------
    hotspot_interface: str = ""            # "" = auto-pick the first wifi device
    hotspot_connection_name: str = "ceravis-hotspot"

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
    # engine with setup/export_reid.sh. FastReid BoT_R50 works too: drop its
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

    # ---- Adaptive capture: SOLITUDE, not merely non-overlap ---------
    # Adaptive learning used to resume the moment boxes stopped overlapping.
    # Far too late: crop_person pads by crop_padding_frac, so a neighbour who
    # is merely STANDING NEAR already contributes pixels to the vector about
    # to be filed under the recipient's name. The drift is self-reinforcing —
    # a contaminated vector raises the neighbour's score, which makes them a
    # better capture candidate next time — and every number involved looks
    # healthy the whole way down.
    reid_adaptive_solitude_frac: float = 2.5   # no other centre within N box-widths

    # ---- Track memory (reid/track_memory.py) ------------------------
    # Body appearance for EVERY track, not just the target: a stranger's look
    # is what lets us tell them apart from the recipient later, and their exit
    # record turns a cross-room search from open-set into a handful of
    # candidates. Faces are deliberately not stored for non-targets.
    track_memory_per_track: int = 3          # embeddings kept per live track
    track_memory_max_exits: int = 64         # exit records retained
    track_memory_transit_secs: float = 45.0  # plausible room-to-room walk
    track_memory_min_score: float = 0.60     # weak continuation is no answer
    track_memory_edge_frac: float = 0.12     # within this of an edge = left by it

    # ---- Auto-harvested negative pool -------------------------------
    # Solves the negative-gallery bootstrap: you cannot enrol household
    # members before you know who they are, and no family enrols every
    # visitor. Every confidently REJECTED track donates its look anonymously,
    # so the pool fills with exactly the people who really walk through this
    # house. Used only to REJECT, never to accept.
    reid_negative_pool_max: int = 200
    reid_negative_veto_score: float = 0.70   # looks like a known non-target

    # ---- Visitor motion snapshots (rules/visitor_rule.py) -----------
    # A snapshot of anyone who is NOT the recipient, while they are MOVING.
    # v1 fired on a fixed time cadence, so a visitor asleep on the sofa
    # produced the same burst as one walking around; motion is the trigger now.
    # A visitor is ANY fresh non-target track INCLUDING an unidentified one —
    # v1 required an identity record, which made the people it most needed to
    # capture invisible to it.
    visitor_snapshots_enabled: bool = True
    # Box-centre displacement as a fraction of box HEIGHT. Normalising by
    # height is what lets one threshold work at both ends of a room.
    visitor_motion_frac: float = 0.04
    # M-of-N, not N-consecutive: real movement is intermittent (someone pauses
    # mid-stride) while box jitter is independent tick to tick and cancels.
    visitor_motion_window: int = 5
    visitor_motion_hits: int = 2
    visitor_snapshot_cooldown_secs: float = 20.0   # per track
    visitor_snapshots_per_hour: int = 60           # global; protects the outbox

    # ---- Crop quality gate (reid/crop_quality.py) -------------------
    # Refuse to embed a crop that cannot support a decision. Most catastrophic
    # ReID errors are the network embedding garbage CONFIDENTLY — a blurred
    # smear or half a person returns a perfectly ordinary-looking vector, and
    # no score threshold can tell that its INPUT was meaningless. Gating before
    # the model also means a bad crop costs no GPU at all.
    crop_min_area_px: int = 3000        # ~55x55; below this it is upsampled noise
    crop_min_aspect: float = 1.2        # h/w — a person is taller than wide
    crop_max_aspect: float = 5.0        # beyond this it is not one standing person
    crop_edge_margin_frac: float = 0.01  # box within this of the edge = truncated
    crop_reject_truncated: bool = True   # half a torso must not become an identity
    crop_min_sharpness: float = 12.0     # Laplacian variance floor (motion blur)
    # Saturation points for the RANKING score (not gates): a crop at or above
    # these is 'as good as it needs to be' on that axis.
    crop_good_area_px: int = 30000
    crop_good_sharpness: float = 120.0

    # ---- Best-shot buffer (reid/best_shot.py) -----------------------
    # Identity questions arrive at awkward moments — mid-stride, mid-doorway.
    # Keeping the best few recent crops per track lets an identity event embed
    # the BEST look rather than the latest one, which costs nothing extra
    # because the crops already exist. Raising the frame rate instead would not
    # help: a blurred subject at 20 Hz is still blurred.
    best_shot_capacity: int = 4          # shots kept per track
    best_shot_max_age_secs: float = 6.0  # older shots are not evidence about now

    # ---- Appearance gating: proximity, not head-count ---------------
    # OSNet used to run every tick whenever 2+ people were in frame. But two
    # people at opposite ends of a room need no appearance to associate —
    # geometry is sufficient. Appearance is only load-bearing when a pair is
    # close enough to confuse, so gate on the closest pair instead.
    tracker_appearance_proximity_iou: float = 0.02   # any overlap at all
    tracker_appearance_proximity_frac: float = 1.5   # centres within N box-widths

    # ---- Event-triggered gallery matching ---------------------------
    # Identity does not need re-establishing every frame; it needs establishing
    # at TRANSITIONS and propagating otherwise. A recipient sitting still for an
    # hour cost ~10,800 matches at a flat 3 Hz. The heartbeat is the safety net
    # that re-checks a long-held lock occasionally.
    reid_event_driven: bool = True
    reid_heartbeat_secs: float = 20.0
    # ---- Recency memory (short-term "how they look RIGHT NOW") ------
    # The gallery is general (every outfit we ever stored) and therefore blunt at
    # reacquisition: two people can both clear a general threshold. This keeps the
    # target's last N confident, non-occluded embeddings so acquire/reacquire can
    # (a) BOOST the true target, who still looks exactly like they did seconds
    # ago, and (b) VETO a look-alike who squeaks over the gallery threshold but
    # matches nothing in the live recent window. Vectors only — no frames.
    reid_recency_enabled: bool = True
    reid_recency_max: int = 12               # embeddings kept per recipient
    reid_recency_ttl_secs: float = 120.0     # older looks stop counting as "recent"
    reid_recency_weight: float = 0.45        # blend: (1-w)*gallery + w*recency
    reid_recency_min_score: float = 0.45     # VETO floor — only when memory exists
    reid_recency_min_push_score: float = 0.65  # only remember confident sightings

    # ---- Pipeline focus / efficiency --------------------------------
    crop_padding_frac: float = 0.08          # margin around a person box for crops
    target_only_pose: bool = True            # once ReID locks the target, pose that crop only
    target_lock_ttl_secs: float = 5.0        # keep target lock this long after last sighting
    # Focus the GPU-HEAVY chain on the recipient's camera: once the target is
    # locked on a camera, tracking (BoT-SORT + OSNet) / ReID / pose run ONLY
    # there; the others idle until the lock lapses (target leaves / TTL), when the
    # tracker scans every camera again to re-find them. Saves GPU on multi-camera
    # homes. Detection (YOLO) is deliberately NOT scoped by this — it always runs
    # on every camera so person-triggered recording covers the whole home and the
    # target can be re-found anywhere; the focus is applied in TrackingRunner.
    active_camera_only: bool = True

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
    # (Appearance used to be gated on a HEAD-COUNT here. Superseded by the
    #  proximity gate above — tracker_appearance_proximity_* — because two
    #  people at opposite ends of a room need no appearance to associate.)

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
    walking_motion_window_secs: float = 1.5
    walking_motion_body_fraction: float = 0.6       # body-lengths / sec to qualify
    walking_confirm_frames: int = 3                 # consecutive frames before WALKING
    walking_min_pixels: float = 12.0                # absolute displacement floor
    # Sit<->stand transitions must be corroborated by head vertical motion
    # (head rises to stand, falls to sit) — stops a small seated shift, or the
    # legs leaving the frame, from flipping the label. View-invariant.
    posture_transition_head_frac: float = 0.15      # head move (× body length) to corroborate
    posture_transition_confirm_frames: int = 3      # frames of corroboration to switch
    fall_torso_angle_deg: float = 60.0              # > = horizontal
    fall_confirmation_frames: int = 3
    fall_cooldown_secs: float = 30.0
    # ---- Fall: detection IS the alert (prioritised, no wait) ---------
    # A confirmed FALLEN label (fall_confirmation_frames of ~horizontal torso)
    # whose head is at/near the ground raises the fall alert the instant it is
    # seen — no post-fall immobility wait, because a person who fell is a fall
    # to report whether or not they then lie still. The near-floor test is the
    # one discriminator that separates a fall (head drops to the floor) from
    # bending over (torso horizontal, head still high).
    # Floor reference: a zone whose name contains this keyword marks the ground
    # plane. A standing/bending person's head sits ABOVE (outside) it; a fallen
    # person's head drops INTO it — view-tolerant, no calibration.
    floor_zone_keyword: str = "floor"
    # With NO floor zone drawn we can't test "near the floor", so a confirmed
    # horizontal is taken as a fall (fail loud). Set True to instead require a
    # floor zone match before alerting (fewer false positives, but misses falls
    # outside the drawn zone) — draw a floor zone and this stays a good default.
    fall_require_near_floor: bool = False
    # Furniture zones give a height reference: a fall also counts when the body
    # drops BELOW the height of nearby furniture (table/chair/bed/counter) — the
    # "whole body lower than a table/chair/bed" rule. Draw these as named zones.
    furniture_zone_keywords: str = ("bed,couch,sofa,recliner,chair,bench,lounge,"
                                    "table,counter,desk,dresser,stove")

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
    # Rolling retention for the device's own record of what happened: the rows
    # in the `events` table AND the snapshot JPEGs they point at, both expired
    # once they are older than this many days (0 = keep forever).
    #
    # Snapshots were the last unbounded writer on the data volume. Recordings
    # self-expire after RECORD_RETENTION_HOURS and the upload spool is capped by
    # OUTBOX_MAX_BLOB_MB, but a still is written for EVERY event — including the
    # once-a-minute welfare bursts, which alone can be ~300 frames a day — and
    # nothing ever deleted one. At ~200 KB a frame that is a few hundred MB a
    # month, growing until the disk the recordings live on is full.
    #
    # 14 days is far longer than anything reviews these locally (the cloud has
    # its own copy of every frame that mattered within seconds of the event) and
    # far longer than the 24h upload window, so an expiring snapshot is never
    # one a queued upload is still waiting on — and could not be anyway: the
    # outbox spools its own copy of the bytes. See EventStore.sweep_retention.
    event_retention_days: int = 14
    rest_zone_keywords: str = "bed,couch,sofa,recliner,chair,bench,lounge"
    # Room-to-room moves (LocationRule): after the recipient leaves view,
    # the previous room stays valid as the transition origin for this long (an
    # uncovered hallway between two cameras) — beyond it the trail is dropped
    # and the next sighting starts fresh with no transition event.
    room_transition_max_gap_secs: float = 600.0

    # ---- CERAVIS application server (cloud) ------------------------
    # The Spring app that owns user accounts. Setup verifies the operator's email
    # against it before onboarding continues. Configure the server's address +
    # the API key. Leave base_url empty to run the device standalone.
    ceravis_api_base_url: str = "https://app.ceravishealth.in/ch"
    # API key sent as the "X-API-Key" header on EVERY app-server call. Override
    # via CERAVIS_API_KEY (it's a secret — rotate there, see jetson.env).
    ceravis_api_key: str = ("sk-0r1g6k7j8l9m0n1o2p3q4r5s6t7u8v9w0x1y2z3a4b5c6d7"
                            "e8f9g0h1i2j3k4l5m6n7o8p9q0r1s2t3u4v5w6x7y8z9")
    ceravis_api_timeout_secs: float = 8.0
    # Externally-reachable base for the live links sent to the app server. In the
    # fleet model this is the shared domain fronted by the cloud Caddy, e.g.
    # https://edge.ceravishealth.in — TLS and the /<edge_id> path routing are
    # terminated upstream, so the link is <base>/<edge_id>/<cam>/whep with NO
    # port. Blank = LAN-direct: auto-derive from the host the browser used and hit
    # MediaMTX's WebRTC port on the device. Fleet mode requires edge_id set too.
    device_stream_base: str = ""
    # ALERT + snapshot event types (recipient-gated). Falls + the CRITICAL
    # no-motion welfare alert.
    cloud_alert_event_types: str = "fall,no_motion"
    cloud_alert_recipient_only: bool = True
    cloud_alert_severities: str = "critical,warning"   # fallback if event_types is blank
    # Snapshot-ONLY event types (no saveAlert): posture transitions, the two
    # long-dwell bursts (no_motion_snapshot / no_transition_snapshot), and the
    # recipient's zone/room moves (area_transition / room_transition).
    cloud_snapshot_event_types: str = (
        "standing_up,sitting_down,walking_started,walking_stopped,"
        "no_motion_snapshot,no_transition_snapshot,"
        "area_transition,room_transition,"
        "visitor_motion_snapshot")

    # ---- Device status heartbeat (edge -> app server presence) ------
    # A tiny periodic POST telling the app server this device is ALIVE and which
    # of its cameras are actually ingesting into MediaMTX right now. Two truths
    # it carries that nothing else does:
    #   1) PRESENCE. The edge can only ever report itself ON — it must be running
    #      to send at all. A powered-off or internet-cut device is detected by
    #      the ABSENCE of these beats, never by a message from it, so the app
    #      server must mark a device OFFLINE after it misses ~3 in a row.
    #   2) PER-CAMERA on/off, measured at the MediaMTX ingestion point (path
    #      ready = the camera's video is really reaching the whole application),
    #      which is the truthful signal: a camera can be pingable yet not
    #      streaming, and this reports the state that actually feeds the system.
    # Purely additive and best-effort — a failure here never touches cameras, AI
    # or recording. Identified by edgeId (the app server can map edgeId ->
    # account on its own); ceravisUserId is sent alongside in the BODY (never the
    # URL — it is account-identifying). No X-API-Key on this call — the status
    # endpoint is unauthenticated. Blank url = feature OFF (device standalone).
    status_heartbeat_url: str = "https://app.ceravishealth.in/api/v1/status"
    status_heartbeat_interval_secs: float = 60.0

    # ---- Scheduled reboot + reboot authorisation --------------------
    # A nightly restart clears whatever a 24h run accumulated — leaked handles,
    # fragmented memory, a wedged driver — at the hour a care home is quietest.
    # systemd owns the SCHEDULE (infra/systemd/ceravis-reboot.timer); these
    # values describe it for the status surface and drive the installer, so the
    # timer and the API never disagree about when it runs.
    reboot_scheduled_enabled: bool = True
    reboot_window_start_hour: int = 3        # 03:00 local, randomised over 1h
    # A reboot must not strand an undelivered fall. The outbox is durable so
    # nothing is LOST, but delivery is delayed by the boot time — which is
    # exactly the delay an alert cannot afford. The scheduled run defers to
    # tomorrow; a human can still override deliberately.
    reboot_defer_on_pending_alerts: bool = True
    # Manual reboot is password-gated: the edge_id that authenticates every
    # other control endpoint travels in the URL of every fleet request, so it
    # is not a secret. Attempts are rate-limited so the PBKDF2 hash cannot be
    # ground down online.
    reboot_max_attempts: int = 5
    reboot_lockout_secs: float = 300.0
    # Long enough for the HTTP response and the log line to flush before the
    # kernel goes down — a reboot nobody was told about looks like a crash.
    reboot_delay_secs: float = 3.0
    reboot_command: str = "sudo -n /bin/systemctl reboot"

    # ---- Cloud outbox (the offline-safe upload queue) ---------------
    # Every event-path upload (saveAlert + its saveSnapshot stills and fall
    # clips) is queued in SQLite and sent from there FALLS FIRST, then oldest
    # first, so a network outage delays delivery instead of losing the incident.
    # The device keeps working offline throughout; the queue drains itself the
    # moment the link is back (the status heartbeat kicks it), and a fall goes
    # ahead of whatever ambient backlog piled up in front of it. A job's media is
    # deleted only when its call SUCCEEDS, so nothing generated is ever discarded
    # before it lands, and a delivering device holds no spool at all.
    #
    # NOTHING IS DROPPED FOR AN ERROR. Every failure — a dead link, a 5xx, a 4xx
    # (including a server mid-maintenance returning 400), even 401/404 — just
    # schedules another attempt. The ONLY thing that ever gives up on a job is
    # the outer age window below: a job undelivered for this long is dropped, the
    # single safety bound so a server that never returns cannot fill the disk
    # forever. 48h covers a long weekend of downtime.
    outbox_window_secs: float = 172800.0     # 48h: the ONLY thing that drops a job
    # Disk-safety caps — the last-resort valve if the volume genuinely fills
    # during a long outage. Eviction is lowest-priority-oldest first, so only
    # AMBIENT wallpaper is shed; a fall or alert is never the victim. Sized for
    # ~48h of normal traffic so they do not bite in ordinary operation.
    outbox_max_items: int = 5000             # pending jobs
    outbox_max_blob_mb: float = 1024.0       # spooled stills + clips on disk
    # Retry backoff (exponential, jittered) between attempts on a failing job.
    # The cap keeps a recovered link draining within ~a minute even without the
    # heartbeat kick. A failing job backs off here while the sender steps around
    # it to deliver others — it never blocks the queue.
    outbox_backoff_base_secs: float = 2.0
    outbox_backoff_max_secs: float = 30.0
    outbox_poll_secs: float = 1.0
    # How long a finished (sent or dropped) job stays as a receipt for the sync
    # console before the row is pruned.
    outbox_history_secs: float = 21600.0     # 6h

    # ---- Long-dwell welfare checks (StillnessRule) ------------------
    # A 75-min slot: WINDOW minutes quiet, then one snapshot per minute for
    # (COUNT) minutes, then the slot resets and repeats.
    #   NO MOTION   = the whole pose skeleton is frozen (no keypoint moving) for
    #                 the window -> CRITICAL alert + a snapshot each minute (the
    #                 serious case: possible collapse / unconsciousness).
    #   NO TRANSITION = posture unchanged (still active/moving, e.g. seated and
    #                 knitting) for the window -> snapshot-only burst (suppressed
    #                 while no-motion is active).
    stillness_window_secs: float = 3600.0        # 60 min quiet before the burst
    stillness_burst_interval_secs: float = 60.0  # one snapshot per minute
    stillness_burst_count: int = 15              # for 15 minutes (-> 75-min slot)
    pose_move_frac: float = 0.15                 # keypoint drift (×torso length) = MOTION

    # ---- Target motion detector (rules/target_motion.py) -------------
    # Replaces the old max-over-keypoints statistic, which called 72.9% of ticks
    # "motion" on a genuinely motionless person and made no_motion unreachable.
    # POSE channel — gross body movement only. Pose cannot resolve a seated
    # person's hand movement (the wrist sits near 0.44 confidence with ~13 px of
    # jitter while the hand moves 18-34 px), so it is not asked to; the pixel
    # channel carries sensitivity.
    pose_still_window: int = 5               # frames in the robust median (both sides)
    pose_still_min_conf: float = 0.35        # below this a joint is ignored entirely
    pose_still_conf_weight: float = 2.0      # thr × (1 + k(1-conf)) — mirrors the noise
    pose_still_min_joints: int = 3           # joints that must move together
    pose_still_scale_bbox_frac: float = 0.25  # scale floor — torso foreshortens when seated
    # PIXEL channel — the primary, sensitive one. ROI is anchored in FRAME
    # coordinates and never follows the tracker box: 3 px of box jitter measures
    # 0.047, MORE than a real 25 px hand movement at 0.040.
    pixel_still_enabled: bool = True
    pixel_still_size: int = 32               # signature is size×size, area-downscaled
    pixel_still_pad_frac: float = 0.10       # ROI padding around the anchor bbox
    pixel_still_min_roi_px: int = 24         # below this the ROI is too small to judge
    # SELF-CALIBRATING trigger: max(pixel_move_thresh, p20(recent MAD) × ratio).
    # A fixed absolute MAD cannot suit every camera — real sensors carry H.264
    # blocking, auto-exposure micro-adjustments and mains flicker that a clean
    # synthetic model badly underestimates, which is exactly how the first
    # attempt shipped a threshold far below a real scene's noise floor and left
    # the channel asserting motion on every tick. pixel_move_thresh is now only
    # an absolute MINIMUM; the ratio does the real work.
    pixel_move_thresh: float = 0.0030        # absolute floor, deliberately low
    pixel_move_ratio: float = 3.0            # × the measured scene noise floor
    pixel_noise_window: int = 120            # samples in the noise estimate
    pixel_noise_min_samples: int = 20        # never fire before the scene is learnt
    # FUSION — M of the last N ticks, not M CONSECUTIVE. Real movement is
    # intermittent (hand moves, pauses, moves) and consecutive-tick hysteresis is
    # blind to exactly that, while a sliding window still cancels noise.
    motion_confirm_m: int = 2
    motion_confirm_n: int = 3

    device_id: str = "edge-0001"

    model_config = SettingsConfigDict(
        # Resolve relative to the repo, not the process cwd, so the env file
        # is found whether launched by systemd, a shell, or an IDE.
        #
        # ONE env file, tracked in git. Nothing at runtime writes to it:
        # the edge_id lives in data/account.json (gitignored, and what
        # effective_edge_id() reads first), so this file stays a pure,
        # hand-edited config that `git pull` never collides with.
        env_file=str(
            Path(__file__).resolve().parents[1] / "infra" / "env" / "jetson.env"),
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
