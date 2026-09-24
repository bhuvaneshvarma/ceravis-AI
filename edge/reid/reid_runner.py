from __future__ import annotations

import logging
import queue
import threading
import time

from config.settings import settings
from enrollment.enrollment_manager import EnrollmentManager
from ingestion import illumination
from reid.faiss_index import FaissGallery
from reid.identity_buffer import IdentityBuffer
from reid.identity_schema import Identity
from reid.recency_buffer import RecencyBuffer
from reid.track_memory import TrackMemory
from reid.target_lock import NightContext, TargetLockManager
from reid.target_registry import TargetRegistry
from tracking.track_buffer import TrackBuffer
from tracking.track_feature_buffer import TrackFeatureBuffer


logger = logging.getLogger("reid")


class ReIDRunner:
    """
    Identity + target-lock orchestrator.

    The heavy lifting (OSNet appearance) now happens once, inside the tracker,
    and lands in the TrackFeatureBuffer. This runner is light: at reid_fps it
    reads per-track features, runs the hybrid set-to-set gallery match through
    the TargetLockManager (acquire / verify / freeze-on-occlusion / release /
    reacquire), writes identities + the shared target lock, and hands confident,
    NON-occluded appearances to a background thread for adaptive learning.

    No frame access, no inference on this path — so it can't lag the stream.

    Night vision: per camera per tick it tells the lock manager whether the
    camera is on infrared and — only when this is the single person in the
    whole home and the recipient is locked nowhere else — who a lone person
    there would be (NightContext). Night learning goes to the infrared store.
    """

    def __init__(
        self,
        track_buffer: TrackBuffer,
        feature_buffer: TrackFeatureBuffer,
        identity_buffer: IdentityBuffer,
        gallery: FaissGallery,
        target_registry: TargetRegistry | None = None,
        posture_buffer=None,
        enroll_manager: EnrollmentManager | None = None,
        face_gallery=None,
    ) -> None:
        self._tracks = track_buffer
        self._features = feature_buffer
        self._identities = identity_buffer
        self._gallery = gallery
        self._targets = target_registry or TargetRegistry()
        self._postures = posture_buffer
        self._enroll_mgr = enroll_manager or EnrollmentManager()
        # Short-term memory of how the target looks RIGHT NOW. Feeds the
        # acquire/reacquire fusion so a look-alike can't take the ID while a
        # live recent window exists. See reid/recency_buffer.py.
        self._recency = RecencyBuffer()
        # Body appearance for EVERY track plus exit records — what makes a
        # cross-room search a handful of candidates instead of an open set,
        # and what auto-populates the negative pool. See reid/track_memory.py.
        self.memory = TrackMemory()
        self._manager = TargetLockManager(gallery, recency=self._recency,
                                          memory=self.memory)
        # The recipients' enrolled faces — the second cue the lock weighs
        # (reid/face_identity.py). None / empty = body evidence only.
        self._face_gallery = face_gallery

        self._running = False
        self._thread: threading.Thread | None = None

        # Adaptive online-learning on a separate low-rate thread so its disk I/O
        # never blocks identity decisions. The tick only does a cheap, throttled
        # hand-off onto this queue.
        self._adapt_q: "queue.Queue[tuple[str, object, str, str]]" = queue.Queue(maxsize=64)
        self._adapt_thread: threading.Thread | None = None
        self._last_adapt_attempt = 0.0
        self._last_adapt_rebuild = 0.0
        # Event gate: identity is established at TRANSITIONS and propagated
        # otherwise. Remembering the last track set per camera is what makes
        # 'did anything change' answerable without a model.
        self._seen_tracks: dict[str, frozenset] = {}
        # camera -> monotonic time a person was last tracked there. "Nobody
        # else in the home" must hold for a while, not for one tick: at night
        # detection flickers, and one empty tick next door let the context rule
        # lock a second person (bench, 2026-09-24 18:48 and 18:52).
        self._last_person: dict[str, float] = {}
        self._last_match: dict[str, float] = {}
        # The recipient's current track per camera, so the instant it departs we
        # file a recipient-TAGGED exit record — the evidence that re-finds them
        # in the next room (reid/track_memory.target_continuation).
        self._target_track: dict[str, tuple[int, str]] = {}

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="reid-runner")
        self._thread.start()
        if settings.reid_adaptive_enabled:
            self._adapt_thread = threading.Thread(
                target=self._adaptive_loop, daemon=True, name="reid-adaptive")
            self._adapt_thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)
        if self._adapt_thread:
            self._adapt_thread.join(timeout)

    def _run(self) -> None:
        interval = 1.0 / settings.reid_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                logger.exception("reid tick failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _identity_event(self, camera_id: str, ids: frozenset) -> str | None:
        """Why this camera needs a gallery match THIS tick, or None to skip.

        A stable set of tracks carries its identities for free — BoT-SORT
        already knows which box is which, so re-asking FAISS every tick pays
        for an answer that cannot have changed. A recipient sitting still
        for an hour cost ~10,800 matches at a flat 3 Hz.

        The heartbeat is the safety net: a long-held lock is re-checked
        occasionally so a slow drift onto the wrong person cannot persist."""
        now = time.monotonic()
        prev = self._seen_tracks.get(camera_id)
        self._seen_tracks[camera_id] = ids

        def fire(why: str) -> str:
            # The heartbeat clock is owned HERE, not by the caller. Reading a
            # timestamp someone else writes made a fresh camera see 0.0 and
            # fire 'heartbeat' on its second tick.
            self._last_match[camera_id] = now
            return why

        if prev is None:
            return fire('first sighting')
        if ids - prev:
            return fire('new track')    # a track was born or re-entered
        if prev - ids:
            return fire('track lost')   # someone left; the lock may be stale
        if (now - self._last_match.get(camera_id, now)) >= settings.reid_heartbeat_secs:
            return fire('heartbeat')
        return None

    def _sole_recipient(self, camera_id: str, boxes: dict) -> str | None:
        """Who a lone person on this infrared camera would be — or None when
        context identity must not be used right now.

        All of: the setting is on; exactly ONE person here and NO fresh person
        on any other camera (one person in the whole home); the recipient is not
        locked on another camera (else this is somebody else); and we know who
        the recipient is — the only enrolled one, or the last one ever locked."""
        if not settings.night_context_lock or len(boxes) != 1:
            return None
        quiet = time.monotonic() - settings.night_context_others_empty_secs
        if any(cam != camera_id and seen > quiet for cam, seen in self._last_person.items()):
            return None                       # someone was elsewhere just now
        if any(cam != camera_id for cam in self._targets.all()):
            return None
        enrolled = self._gallery.recipient_ids()
        if len(enrolled) == 1:
            return enrolled[0]
        last = self._targets.last_recipient()
        return last if last in enrolled else None

    def _tick(self) -> None:
        for camera_id, track_result in self._tracks.get_all().items():
            if track_result.tracks:
                self._last_person[camera_id] = time.monotonic()
            if not track_result.tracks:
                self._seen_tracks.pop(camera_id, None)
                # The room emptied. If the recipient was locked here, they have
                # left — file their tagged exit BEFORE the memory is pruned (so it
                # can re-find them next door), drop the registry focus so every
                # camera is searched, and clear this camera's stale state.
                gone = self._target_track.pop(camera_id, None)
                if gone is not None:
                    self.memory.retire(camera_id, gone[0], recipient_id=gone[1])
                if self._targets.recipient(camera_id) is not None:
                    self._targets.unlock(camera_id)
                self.memory.prune(camera_id, set())
                self._identities.prune(camera_id, set())
                continue
            ir = illumination.is_ir(camera_id)
            # An UNLOCKED infrared camera is evaluated every tick, not only on
            # track-set changes: the night context identity needs a person to
            # have been seen steadily, which an event-only cadence would stretch
            # to the 20 s heartbeat. By day, and once locked, nothing changes.
            night_search = ir and self._targets.get(camera_id) is None
            if settings.reid_event_driven and not night_search:
                ids = frozenset(t.track_id for t in track_result.tracks)
                if self._identity_event(camera_id, ids) is None:
                    # Nothing changed — BoT-SORT carries identity, so the gallery
                    # re-match is skipped. But keep the registry lock FRESH while
                    # the target's track is still here, or its TTL lapses between
                    # heartbeats and a STATIONARY recipient reads as "unlocated":
                    # pose is target-only and would fall back to costly full-frame,
                    # and the search-grace would trip for no reason.
                    tid = self._targets.get(camera_id)
                    if tid is not None and tid in ids:
                        rid = self._targets.recipient(camera_id)
                        if rid:
                            self._targets.lock(camera_id, tid, rid)
                    continue           # nothing changed — the tracker carries it
            boxes = {t.track_id: (t.bbox.x1, t.bbox.y1, t.bbox.x2, t.bbox.y2)
                     for t in track_result.tracks}

            def feat_for(tid: int):
                rec = self._features.get(camera_id, tid)
                return rec.smooth if rec is not None else None

            face_now = time.monotonic()

            def face_for(tid: int, rid: str):
                """(face score vs rid's enrolled faces, face px) for this
                track's latest face look; (None, 0) when the look found no
                usable face; None when it has not been looked at recently."""
                if self._face_gallery is None or self._face_gallery.size == 0:
                    return (None, 0.0)          # no enrolled faces: a look can't help
                rec = self._features.get(camera_id, tid)
                max_age = settings.face_max_age_secs
                if rec is None or face_now - rec.face_looked > max_age:
                    return None
                score = (self._face_gallery.score(rec.face, rid)
                         if rec.face is not None and face_now - rec.face_at <= max_age
                         else None)
                return (None, 0.0) if score is None else (score, rec.face_px)

            night = (NightContext(ir=True,
                                  sole_recipient=self._sole_recipient(camera_id,
                                                                      boxes))
                     if ir else None)
            outcome = self._manager.update(camera_id, boxes, feat_for, night,
                                           face_for=face_for)

            # Apply the lock decision to the shared registry (pose + UI read it).
            # released = confirmed mismatch; lost = the locked track is gone from
            # this camera and no confident reacquire here. Both drop the focus so
            # the recipient is searched for on EVERY camera; the manager keeps
            # who they are for the fast same-camera return.
            if outcome.released or outcome.lost:
                self._targets.unlock(camera_id)
            if outcome.target_track_id is not None and outcome.recipient_id:
                self._targets.lock(camera_id, outcome.target_track_id,
                                   outcome.recipient_id)
                self._target_track[camera_id] = (outcome.target_track_id,
                                                 outcome.recipient_id)
            else:
                # The recipient is no longer confirmed on this camera — file
                # their tagged exit record (their last looks, for re-finding them
                # next door) before the generic prune below retires it untagged.
                gone = self._target_track.pop(camera_id, None)
                if gone is not None and gone[0] not in boxes:
                    self.memory.retire(camera_id, gone[0], recipient_id=gone[1])

            # Publish identities.
            ts = track_result.timestamp
            fid = track_result.frame_id
            # Remember what EVERY track looks like — target or not. A
            # stranger's appearance is precisely what lets us tell them apart
            # from the recipient later, and it costs nothing: the embedding
            # already exists.
            for tid in boxes:
                rec = self._features.get(camera_id, tid)
                if rec is None:
                    continue
                self.memory.observe(camera_id, tid, rec.smooth)
                # While a target is LOCKED, every other track on this camera
                # is definitively not them — the strongest negative label
                # available anywhere, and it costs nothing to collect. This
                # is what bootstraps the negative pool without asking a
                # family to enrol every visitor they ever have.
                # Only from a lock resting on a gallery verification — a night
                # guess must never teach the pool that the real recipient is a
                # stranger. (By day every lock is verified: unchanged.)
                if (outcome.target_track_id is not None and outcome.trusted
                        and tid != outcome.target_track_id):
                    self.memory.add_negative(rec.smooth)

            modality = (illumination.Modality.IR if ir
                        else illumination.Modality.COLOR).value
            for tid, (rid, is_target, score, view) in outcome.identities.items():
                self._identities.update(Identity(
                    track_id=tid, camera_id=camera_id, frame_id=fid, timestamp=ts,
                    recipient_id=rid if is_target else None,
                    is_target=is_target, confidence=float(score),
                    view_label=view if is_target else None,
                    recency_score=(outcome.recency if is_target else None),
                    identity_basis=(outcome.basis if is_target else None),
                    modality=modality))
            # The target flag is sticky, so a released lock (or the target moving
            # to a new track_id) must have the OLD track's flag cleared — else the
            # green dot and the recipient rules keep following the wrong person.
            self._identities.demote_stale_targets(
                camera_id, {tid for tid, v in outcome.identities.items() if v[1]})

            # Adaptive capture — ONLY for a confidently-matched, NON-occluded
            # target (the manager already gated occlusion), so we never learn an
            # intruder's appearance into the recipient's gallery.
            # outcome.adaptive is set exactly on a CONFIRMED, NON-OCCLUDED
            # sighting — the only look we are willing to remember as "this is
            # the target right now". Recency is pushed on every one of them;
            # the adaptive store keeps its own slower throttle underneath.
            # Tracks that vanished this tick become exit records — the
            # evidence that recognises them walking into the next room.
            self.memory.prune(camera_id, set(boxes))
            # Bound the identity map to the live track set — otherwise it grows
            # forever on a monotonically rising track_id (the buffer leak shape).
            self._identities.prune(camera_id, set(boxes))

            if outcome.adaptive is not None:
                tid, rid, score = outcome.adaptive
                rec = self._features.get(camera_id, tid)
                # The feature must be of the camera's CURRENT modality — right
                # after a switch a stale record could otherwise be filed under
                # the wrong gallery.
                if rec is not None and rec.modality == modality:
                    if not ir:
                        # A face-confirmed sighting is learnable below the body
                        # bar: that is the recipient in today's clothes.
                        confirmed = outcome.face_confirmed
                        if confirmed or score >= settings.reid_recency_min_push_score:
                            self._recency.push(rid, rec.curr)
                        self._queue_adapt(camera_id, tid, rid, score, rec.curr,
                                          gate=not confirmed)
                    elif outcome.learn_ir and settings.reid_ir_adaptive_enabled:
                        # Night: the evidence is a strong verification carried
                        # on this very track (learnable), not tonight's score.
                        self._recency.push(rid, rec.curr, modality)
                        self._queue_adapt(camera_id, tid, rid, score, rec.curr,
                                          modality=modality, gate=False)

    # ---- adaptive online learning (off the inference tick) -----------
    def _queue_adapt(self, camera_id, track_id, rid, score, emb,
                     modality: str = "color", gate: bool = True) -> None:
        if not settings.reid_adaptive_enabled:
            return
        if gate and score < settings.reid_adaptive_min_score:
            return
        now = time.monotonic()
        if now - self._last_adapt_attempt < settings.reid_adaptive_min_interval_secs:
            return
        self._last_adapt_attempt = now
        label = ""
        if self._postures is not None:
            rec = self._postures.get(camera_id, track_id)
            label = rec.posture.value if rec is not None else ""
        try:
            self._adapt_q.put_nowait((rid, emb.copy(), label, modality))
        except queue.Full:
            pass

    def _adaptive_loop(self) -> None:
        while self._running:
            try:
                rid, emb, label, modality = self._adapt_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                added = self._enroll_mgr.append_adaptive(
                    rid, emb, label,
                    cap=settings.reid_adaptive_max,
                    dedup_cos=settings.reid_adaptive_dedup_cos,
                    modality=modality)
                if not added:
                    continue
                logger.info("reid: adaptive %s sample saved for %s (label=%s)",
                            "infrared" if modality == "ir" else "colour",
                            rid, label or "—")
                now = time.monotonic()
                if now - self._last_adapt_rebuild >= settings.reid_adaptive_rebuild_secs:
                    emb_all, ids, labels, mods = self._enroll_mgr.load_gallery()
                    self._gallery.rebuild(emb_all, ids, labels, mods)
                    self._last_adapt_rebuild = now
                    logger.info("reid: adaptive store updated for %s "
                                "(gallery now %d vectors)", rid, len(ids))
            except Exception:
                logger.exception("reid: adaptive capture failed")
