from __future__ import annotations

import logging
import threading
import time

import numpy as np

from common.crops import crop_person
from config import scene_rules
from config.settings import settings
from detection.detection_buffer import DetectionBuffer
from detection.detection_schema import BoundingBox, DetectionClass
from ingestion import illumination
from ingestion.frame_buffer import FrameBuffer
from reid import crop_quality
from tracking.botsort import BoTSORT
from tracking.track_buffer import TrackBuffer
from tracking.track_feature_buffer import TrackFeatureBuffer
from tracking.track_schema import Track, TrackResult


logger = logging.getLogger("tracking")


class TrackingRunner:
    """
    Per-camera clean-room BoT-SORT, polled from the DetectionBuffer.

    Appearance (OSNet, the same engine the gallery uses) is fused into the
    association so IDs survive a crossover. To stay cheap, embeddings are only
    computed when there are several people in frame (the case that needs it);
    with a single person the tracker is pure motion + a low-rate feature refresh
    so the gallery match still has something fresh to compare.
    """

    def __init__(
        self,
        detection_buffer: DetectionBuffer,
        track_buffer: TrackBuffer,
        frame_buffer: FrameBuffer | None = None,
        feature_buffer: TrackFeatureBuffer | None = None,
        metrics_registry=None,
        gallery=None,
        target_registry=None,
        best_shots=None,
        face_gallery=None,
    ) -> None:
        self._detections = detection_buffer
        self._tracks = track_buffer
        self._frames = frame_buffer
        self._features = feature_buffer
        # Tracking runs on EVERY camera, always. The recipient must be followable
        # the instant they enter any room, and the visitor-snapshot stream is an
        # INDEPENDENT mechanism that needs tracks on every camera too. GPU is
        # saved downstream (pose target-only; ReID verifies only the locked track
        # and never re-matches known bystanders), not by idling whole cameras —
        # idling them is exactly what used to blind the cross-camera handoff.
        # Read only to know which track is the locked target (face evidence).
        self._targets = target_registry
        # Face identity — the second cue (reid/face_identity.py). Loaded on the
        # tracking thread (OpenCV nets are per-thread) the first time it is due.
        self._face_gallery = face_gallery
        self._face = None
        self._last_face: dict[str, float] = {}
        # Enrollment gate: tracking/ReID/pose/rules run ONLY when the gallery holds
        # at least one enrolled target embedding. Until then the pipeline stops at
        # YOLO detection (which still drives recording) — no wasted appearance /
        # tracking / pose / rule work on a device with nobody enrolled. Re-enables
        # itself the instant enrollment lands (the gallery is rebuilt live).
        self._gallery = gallery
        self._enrolled = False
        self._gate_logged = 0.0
        self._metrics = (
            metrics_registry.get_or_create("reid_embed") if metrics_registry else None
        )

        # Best crops per track, ready for the moment an identity question
        # arrives — see reid/best_shot.py. Optional: None simply means no
        # best-shot capture, never a failure.
        self._shots = best_shots
        self._rejected: dict[str, int] = {}   # why crops were refused
        self._last_shot: dict[str, float] = {}

        self._trackers: dict[str, BoTSORT] = {}
        # Illumination epoch per camera as last seen — a change means the camera
        # switched between colour and infrared (ingestion/illumination.py).
        self._light_epoch: dict[str, int] = {}
        self._last_seen_frame: dict[str, int] = {}
        self._last_embed: dict[str, float] = {}

        self._extractor = None              # OSNet; None => motion-only tracking
        self._with_reid = False

        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        # Appearance is optional: if the ReID engine isn't built (e.g. on a dev
        # box) the tracker degrades to motion-only ByteTrack and the rest of the
        # pipeline keeps working.
        if (settings.tracker_with_reid and self._frames is not None
                and self._features is not None):
            try:
                from reid.reid_extractor import ReIDExtractor
                self._extractor = ReIDExtractor()
                self._with_reid = True
                logger.info("Tracking: BoT-SORT with OSNet appearance fusion")
            except FileNotFoundError as exc:
                logger.warning("Tracking: appearance off — ReID engine not built "
                               "(%s); running motion-only BoT-SORT", exc)
            except Exception:
                logger.exception("Tracking: appearance off — ReID engine load failed")
        else:
            logger.info("Tracking: motion-only BoT-SORT (appearance disabled)")

        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="tracking-runner",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- internals ---------------------------------------------------
    def _tracker(self, camera_id: str) -> BoTSORT:
        if camera_id not in self._trackers:
            self._trackers[camera_id] = BoTSORT(
                track_high_thresh=settings.tracker_high_thresh,
                track_low_thresh=settings.tracker_low_thresh,
                new_track_thresh=settings.tracker_new_track_thresh,
                match_thresh=settings.tracker_match_thresh,
                proximity_thresh=settings.tracker_proximity_thresh,
                appearance_thresh=settings.tracker_appearance_thresh,
                with_reid=self._with_reid,
                lost_secs=settings.tracker_lost_secs,
            )
        return self._trackers[camera_id]

    def _run(self) -> None:
        interval = 1.0 / settings.detection_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                logger.exception("tracking tick failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _reid_ready(self) -> bool:
        """Whether any target is enrolled — the gate for the whole AI chain below
        detection. FaissGallery.size is a PROPERTY (the live embedding count), so
        it is read, never called: `size()` raises TypeError on every tick and
        takes tracking, ReID, pose and every rule down with it while YOLO keeps
        running, which looks exactly like "no target is ever found". It flips on
        the moment enrollment finishes and off if the gallery is emptied."""
        ready = self._gallery is not None and self._gallery.size > 0
        if ready and not self._enrolled:
            logger.info("Tracking ENABLED — %d enrolled embedding(s) present",
                        self._gallery.size)
            self._enrolled = True
        elif not ready and (self._enrolled or self._gate_logged == 0.0
                            or time.monotonic() - self._gate_logged > 60):
            # WARNING, not INFO: falls and no-motion are off. It may be on purpose
            # (the embeddings removed to stop the AI), but it is never routine —
            # the gallery is loaded before this thread starts, so a normal boot
            # does not print it at all.
            logger.warning("Tracking IDLE — no enrolled embeddings; running "
                           "detection-only (recording stays active). Falls, "
                           "no-motion and ReID are OFF until the recipient is "
                           "enrolled.")
            self._gate_logged = time.monotonic()
            self._enrolled = False
        return ready

    def _tick(self) -> None:
        if not self._reid_ready():
            return                              # gated: stop at YOLO detection
        for camera_id, det_result in self._detections.get_all().items():
            if self._last_seen_frame.get(camera_id) == det_result.frame_id:
                continue
            self._last_seen_frame[camera_id] = det_result.frame_id
            ir = self._follow_light(camera_id)

            persons = [d for d in det_result.detections
                       if d.class_name == DetectionClass.PERSON]
            if not persons:
                # Still a tracker step: every track goes LOST and expires on
                # time, so nobody's id outlives an empty room and is handed to
                # the next person who appears at the same spot.
                if camera_id in self._trackers:
                    self._trackers[camera_id].update(
                        np.zeros((0, 4), np.float32), np.zeros(0, np.float32), None)
                self._tracks.update(TrackResult(
                    camera_id=camera_id, frame_id=det_result.frame_id,
                    timestamp=det_result.timestamp, tracks=[]))
                if self._features is not None:
                    self._features.prune(camera_id, set())
                continue

            dets_xywh = np.array(
                [[(d.bbox.x1 + d.bbox.x2) / 2.0, (d.bbox.y1 + d.bbox.y2) / 2.0,
                  d.bbox.width, d.bbox.height] for d in persons], dtype=np.float32)
            scores = np.array([d.confidence for d in persons], dtype=np.float32)

            feats = self._maybe_embed(camera_id, persons, ir)
            stracks = self._tracker(camera_id).update(dets_xywh, scores, feats)

            out: list[Track] = []
            alive: set[int] = set()
            for st in stracks:
                x1, y1, x2, y2 = st.xyxy
                out.append(Track(
                    track_id=int(st.track_id), camera_id=camera_id,
                    frame_id=det_result.frame_id, timestamp=det_result.timestamp,
                    bbox=BoundingBox(x1=float(x1), y1=float(y1),
                                     x2=float(x2), y2=float(y2)),
                    confidence=float(st.score)))
                alive.add(int(st.track_id))
                if self._features is not None and st.smooth_feat is not None:
                    self._features.update(
                        camera_id, int(st.track_id),
                        smooth=st.smooth_feat.copy(),
                        curr=(st.curr_feat.copy() if st.curr_feat is not None
                              else st.smooth_feat.copy()),
                        frame_id=det_result.frame_id, timestamp=det_result.timestamp,
                        modality=(illumination.Modality.IR if ir
                                  else illumination.Modality.COLOR).value)

            self._capture_shots(camera_id, out, det_result.frame_id, ir)
            self._maybe_face(camera_id, out, ir)

            self._tracks.update(TrackResult(
                camera_id=camera_id, frame_id=det_result.frame_id,
                timestamp=det_result.timestamp, tracks=out))
            if self._features is not None:
                self._features.prune(camera_id, alive)
            if self._shots is not None:
                self._shots.prune(camera_id, alive)

    def _maybe_face(self, camera_id: str, tracks, ir: bool) -> None:
        """Attach face looks to the tracks whose identity is in question — on
        a camera whose rule set uses faces (the night set does not), for a
        recipient with enrolled faces, at most at the ReID rate.

          SEARCHING (the recipient is locked on no camera): the face comes
            first, so everyone here is looked at — the longest-unlooked first
            (a new arrival before anyone), face_search_max_per_tick per tick —
            and a face that confirms the recipient can lock them in any
            clothes. Only people still in question are looked at: not someone
            whose face already answered "not the recipient" (until those looks
            age out), and someone whose last look found no usable face (turned
            away, too small) only every face_recheck_secs — in the bench living
            room 670 of 685 looks at desk-seated people found none (2026-10-01).
          LOCKED (here or next door): the tracker carries identity between
            looks. The target, and anyone whose body could pass for them, is
            re-checked only every face_recheck_secs — enough to catch an id
            swap in a huddle, a small fraction of the per-tick cost."""
        rules = scene_rules.for_ir(ir)
        if (self._face_gallery is None or self._face_gallery.size == 0
                or self._features is None or self._frames is None
                or self._gallery is None or not rules.face_enabled):
            return
        now = time.monotonic()
        if now - self._last_face.get(camera_id, 0.0) < 1.0 / settings.reid_fps:
            return
        self._last_face[camera_id] = now
        if self._face is None:
            from reid.face_identity import FaceIdentity
            self._face = FaceIdentity()
        if not self._face.ready:
            return
        fd = self._frames.get(camera_id)
        if fd is None:
            return
        target = self._targets.get(camera_id) if self._targets is not None else None
        locked = target is not None or bool(self._targets and self._targets.all())
        due = []
        for t in tracks:
            rec = self._features.get(camera_id, t.track_id)
            if rec is None:
                continue
            if not locked:
                faceless = rec.face_looked > rec.face_at       # last look found none
                if not (self._face_answered(rec, now) or (
                        faceless and now - rec.face_looked < settings.face_recheck_secs)):
                    due.append((rec.face_looked, t))
                continue
            if now - rec.face_looked < settings.face_recheck_secs:
                continue
            if (t.track_id != target and self._gallery.match(rec.smooth).score
                    < rules.reid_match_threshold):
                continue                       # cannot pass for them — no face needed
            due.append((rec.face_looked, t))
        due.sort(key=lambda d: d[0])
        if not locked:
            due = due[:settings.face_search_max_per_tick]
        for _, t in due:
            face, px = self._face.embed_person(
                fd.image, (t.bbox.x1, t.bbox.y1, t.bbox.x2, t.bbox.y2))
            self._features.set_face(camera_id, t.track_id, face, px)

    def _face_answered(self, rec, now: float) -> bool:
        """Has this person's face already said "not the recipient"? At least
        face_veto_min_looks usable looks inside face_max_age_secs, every one
        below the veto bar for every enrolled recipient — the same evidence
        that vetoes them in the lock. Looking again would only repeat it."""
        fresh = [v for v, _px, at in rec.face_looks
                 if now - at <= settings.face_max_age_secs]
        return (len(fresh) >= settings.face_veto_min_looks
                and all(self._face_gallery.best(v) < settings.face_veto_score
                        for v in fresh))

    def _follow_light(self, camera_id: str) -> bool:
        """Is this camera on infrared right now — and if it has just SWITCHED,
        drop every track's appearance history (motion is kept, no track is
        lost), so no feature ever mixes a colour look with an infrared one.
        The feature records go too: ReID holds its lock on the tracker's
        continuity until a fresh single-modality look arrives."""
        light = illumination.state(camera_id)
        ir = light is not None and light.modality is illumination.Modality.IR
        epoch = light.epoch if light is not None else 0
        prev = self._light_epoch.setdefault(camera_id, epoch)
        if prev != epoch:
            self._light_epoch[camera_id] = epoch
            if camera_id in self._trackers:
                self._trackers[camera_id].reset_appearance()
            if self._features is not None:
                self._features.prune(camera_id, set())
            logger.info("%s: switched to %s — appearance history reset",
                        camera_id, "infrared" if ir else "colour")
        return ir

    def _capture_shots(self, camera_id: str, tracks, frame_id: int,
                       ir: bool = False) -> None:
        """Offer each track's current crop to its best-shot ring.

        Runs AFTER association because that is the first point a crop can be
        attributed to a track_id — before it, detections have no identity to
        file under. Rate-limited to reid_fps: these shots answer identity
        questions that arrive seconds apart, so capturing at the full
        tracking rate would buy sharpness nobody reads."""
        if self._shots is None or self._frames is None or not tracks:
            return
        now = time.monotonic()
        if (now - self._last_shot.get(camera_id, 0.0)) < (1.0 / settings.reid_fps):
            return
        fd = self._frames.get(camera_id)
        if fd is None:
            return
        self._last_shot[camera_id] = now
        fh, fw = fd.image.shape[:2]
        for t in tracks:
            crop, _, _ = crop_person(fd.image, t.bbox.x1, t.bbox.y1,
                                     t.bbox.x2, t.bbox.y2,
                                     settings.crop_padding_frac)
            q = crop_quality.assess(crop, t.bbox, fw, fh, t.confidence, ir=ir)
            if q.ok:
                self._shots.offer(camera_id, t.track_id, crop, q, frame_id)

    @property
    def rejected_crops(self) -> dict:
        """Why crops were refused, by reason — the observability that makes
        a silently-degraded camera visible instead of merely quiet."""
        return dict(self._rejected)

    @staticmethod
    def _crowded(persons) -> bool:
        """Is any PAIR close enough that geometry alone could confuse them?

        The old gate was `len(persons) >= 2` — appearance every tick the moment
        two people shared a room. But two people at opposite ends of a lounge
        need no appearance to associate: IoU already separates them completely.
        Appearance is only load-bearing when boxes are near enough to swap, so
        gate on the CLOSEST PAIR instead of the head-count. Crossover protection
        is unchanged; the cost of the common far-apart case disappears."""
        n = len(persons)
        if n < 2:
            return False
        boxes = [(d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2) for d in persons]
        frac = settings.tracker_appearance_proximity_frac
        for i in range(n):
            ax1, ay1, ax2, ay2 = boxes[i]
            aw = max(1.0, ax2 - ax1)
            acx, acy = (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0
            for j in range(i + 1, n):
                bx1, by1, bx2, by2 = boxes[j]
                iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
                ih = max(0.0, min(ay2, by2) - max(ay1, by1))
                inter = iw * ih
                union = ((ax2 - ax1) * (ay2 - ay1)
                         + (bx2 - bx1) * (by2 - by1) - inter)
                if union > 0 and inter / union > settings.tracker_appearance_proximity_iou:
                    return True
                bcx, bcy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
                span = max(aw, max(1.0, bx2 - bx1))
                if ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5 <= frac * span:
                    return True
        return False

    def _maybe_embed(self, camera_id: str, persons,
                     ir: bool = False) -> np.ndarray | None:
        """OSNet embeddings per person, gated to keep the common case cheap.

        Two gates now, both cheap and both before the model:
          * PROXIMITY decides whether appearance is needed at all this tick;
          * CROP QUALITY decides, per person, whether a crop can support a
            decision. A blurred smear or half a torso embeds as a perfectly
            ordinary-looking vector that no score threshold can catch, so it is
            refused BEFORE inference — which also means it costs no GPU.
        A refused crop still yields a zero row, so the returned array stays
        aligned 1:1 with `persons`; BoT-SORT reads a zero feature as "no
        appearance evidence" and falls back to motion for that box.

        On an infrared camera (`ir`) both gates and the embedding switch to
        their IR forms: noise-robust sharpness, and luminance-only embedding
        into the IR gallery's space.
        """
        if not self._with_reid or self._extractor is None or self._frames is None:
            return None
        now = time.monotonic()
        due = (now - self._last_embed.get(camera_id, 0.0)) >= (1.0 / settings.reid_fps)
        if not (self._crowded(persons) or due):
            return None
        fd = self._frames.get(camera_id)
        if fd is None:
            return None

        fh, fw = fd.image.shape[:2]
        t = time.perf_counter()
        out = []
        zero = np.zeros(settings.reid_embedding_dim, dtype=np.float32)
        for d in persons:
            crop, _, _ = crop_person(fd.image, d.bbox.x1, d.bbox.y1,
                                     d.bbox.x2, d.bbox.y2, settings.crop_padding_frac)
            q = crop_quality.assess(crop, d.bbox, fw, fh, d.confidence, ir=ir)
            if not q.ok:
                self._rejected[q.reason.split(" (")[0]] = \
                    self._rejected.get(q.reason.split(" (")[0], 0) + 1
                out.append(zero)
                continue
            out.append(self._extractor.embed(crop, ir=ir))
        if self._metrics:
            self._metrics.record(time.perf_counter() - t)
        self._last_embed[camera_id] = now
        return np.stack(out, axis=0)
