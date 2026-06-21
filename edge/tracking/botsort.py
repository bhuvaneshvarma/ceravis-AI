from __future__ import annotations

"""
Clean-room BoT-SORT multi-object tracker (torch-free).

This is a from-scratch implementation of the BoT-SORT algorithm — NOT a copy of
ultralytics' (AGPL) code — so it is safe to ship in a commercial product. It is
functionally equivalent:

  * Kalman filter on (cx, cy, w, h) — BoT-SORT's width/height state.
  * Two-stage ByteTrack association (high-score, then low-score) so weak/occluded
    detections still keep their IDs.
  * Appearance fusion in the high-score stage: OSNet ReID embeddings (the SAME
    model that feeds the FAISS gallery) disambiguate two boxes that overlap —
    this is what stops the target's ID jumping to a person who crosses in front.
  * Per-track EMA appearance feature ("smooth_feat") for noise-robust matching.
  * Lost-track buffer: a track that vanishes (full occlusion) is kept "lost" for
    `track_buffer` frames and re-associated by appearance when it reappears — so
    the original ID is restored once the occluder moves away.

Camera-motion compensation (GMC) is intentionally omitted: the cameras are
static wall units, so GMC would only add per-frame optical-flow cost.
"""

from enum import IntEnum

import numpy as np

from tracking.kalman_filter import KalmanFilterWH
from tracking import matching


class TrackState(IntEnum):
    NEW = 0
    TRACKED = 1
    LOST = 2
    REMOVED = 3


def _xyxy_to_xywh(x1, y1, x2, y2) -> np.ndarray:
    return np.array([(x1 + x2) / 2.0, (y1 + y2) / 2.0, x2 - x1, y2 - y1],
                    dtype=np.float32)


class STrack:
    """One tracklet. Detections are also wrapped as STracks for distance calc."""

    _count = 0
    shared_kalman = KalmanFilterWH()

    def __init__(self, xywh: np.ndarray, score: float,
                 feat: np.ndarray | None = None) -> None:
        self._xywh = np.asarray(xywh, dtype=np.float32)
        self.score = float(score)

        self.mean: np.ndarray | None = None
        self.covariance: np.ndarray | None = None
        self.track_id = 0
        self.state = TrackState.NEW
        self.is_activated = False
        self.frame_id = 0
        self.start_frame = 0
        self.tracklet_len = 0

        self.smooth_feat: np.ndarray | None = None
        self.curr_feat: np.ndarray | None = None
        self.alpha = 0.9                     # EMA momentum for appearance
        if feat is not None:
            self.update_features(feat)

    # ---- id ----------------------------------------------------------
    @staticmethod
    def next_id() -> int:
        STrack._count += 1
        return STrack._count

    # ---- geometry ----------------------------------------------------
    @property
    def xywh(self) -> np.ndarray:
        if self.mean is None:
            return self._xywh
        return self.mean[:4].copy()

    @property
    def xyxy(self) -> np.ndarray:
        cx, cy, w, h = self.xywh
        return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
                        dtype=np.float32)

    # ---- appearance --------------------------------------------------
    def update_features(self, feat: np.ndarray) -> None:
        n = np.linalg.norm(feat)
        feat = feat / n if n > 0 else feat
        self.curr_feat = feat
        if self.smooth_feat is None:
            self.smooth_feat = feat.copy()
        else:
            self.smooth_feat = self.alpha * self.smooth_feat + (1 - self.alpha) * feat
            self.smooth_feat /= (np.linalg.norm(self.smooth_feat) + 1e-9)

    # ---- kalman ------------------------------------------------------
    def predict(self) -> None:
        if self.mean is None:
            return
        mean = self.mean.copy()
        if self.state != TrackState.TRACKED:
            mean[6] = 0     # freeze width velocity while lost
            mean[7] = 0     # freeze height velocity while lost
        self.mean, self.covariance = self.shared_kalman.predict(mean, self.covariance)

    @staticmethod
    def multi_predict(stracks: list["STrack"]) -> None:
        for st in stracks:
            st.predict()

    # ---- lifecycle ---------------------------------------------------
    def activate(self, frame_id: int) -> None:
        self.mean, self.covariance = self.shared_kalman.initiate(self._xywh)
        self.tracklet_len = 0
        self.state = TrackState.TRACKED
        self.is_activated = frame_id == 1       # first frame activates immediately
        self.track_id = self.next_id()
        self.frame_id = frame_id
        self.start_frame = frame_id

    def re_activate(self, det: "STrack", frame_id: int, new_id: bool = False) -> None:
        self.mean, self.covariance = self.shared_kalman.update(
            self.mean, self.covariance, det._xywh)
        if det.curr_feat is not None:
            self.update_features(det.curr_feat)
        self.tracklet_len = 0
        self.state = TrackState.TRACKED
        self.is_activated = True
        self.frame_id = frame_id
        self.score = det.score
        if new_id:
            self.track_id = self.next_id()

    def update(self, det: "STrack", frame_id: int) -> None:
        self.frame_id = frame_id
        self.tracklet_len += 1
        self.mean, self.covariance = self.shared_kalman.update(
            self.mean, self.covariance, det._xywh)
        if det.curr_feat is not None:
            self.update_features(det.curr_feat)
        self.state = TrackState.TRACKED
        self.is_activated = True
        self.score = det.score

    def mark_lost(self) -> None:
        self.state = TrackState.LOST

    def mark_removed(self) -> None:
        self.state = TrackState.REMOVED


# ---------------------------------------------------------------------
def _join(a: list[STrack], b: list[STrack]) -> list[STrack]:
    seen = {t.track_id for t in a}
    return a + [t for t in b if t.track_id not in seen]


def _sub(a: list[STrack], b: list[STrack]) -> list[STrack]:
    ids = {t.track_id for t in b}
    return [t for t in a if t.track_id not in ids]


def _remove_duplicate(a: list[STrack], b: list[STrack]):
    """Drop near-identical tracks (IoU>0.85) keeping the longer-lived one."""
    if not a or not b:
        return a, b
    pdist = matching.iou_distance(a, b)
    pairs = np.where(pdist < 0.15)
    dupa, dupb = set(), set()
    for p, q in zip(*pairs):
        timep = a[p].frame_id - a[p].start_frame
        timeq = b[q].frame_id - b[q].start_frame
        if timep > timeq:
            dupb.add(q)
        else:
            dupa.add(p)
    resa = [t for i, t in enumerate(a) if i not in dupa]
    resb = [t for i, t in enumerate(b) if i not in dupb]
    return resa, resb


class BoTSORT:
    """One tracker instance per camera."""

    def __init__(self, *, track_high_thresh: float, track_low_thresh: float,
                 new_track_thresh: float, match_thresh: float,
                 track_buffer: int, proximity_thresh: float,
                 appearance_thresh: float, with_reid: bool,
                 frame_rate: float = 10.0) -> None:
        self.track_high_thresh = track_high_thresh
        self.track_low_thresh = track_low_thresh
        self.new_track_thresh = new_track_thresh
        self.match_thresh = match_thresh
        self.proximity_thresh = proximity_thresh
        self.appearance_thresh = appearance_thresh
        self.with_reid = with_reid

        self.tracked_stracks: list[STrack] = []
        self.lost_stracks: list[STrack] = []
        self.removed_stracks: list[STrack] = []
        self.frame_id = 0
        self.max_time_lost = int(frame_rate / 30.0 * track_buffer)

    def _get_dists(self, tracks: list[STrack], dets: list[STrack]) -> np.ndarray:
        iou = matching.iou_distance(tracks, dets)
        if self.with_reid:
            emb = matching.embedding_distance(tracks, dets)
            return matching.fuse_motion_appearance(
                iou, emb, self.proximity_thresh, self.appearance_thresh)
        return iou

    def update(self, dets_xywh: np.ndarray, scores: np.ndarray,
               feats: np.ndarray | None) -> list[STrack]:
        """
        dets_xywh: (N, 4) [cx, cy, w, h]; scores: (N,); feats: (N, D) or None.
        Returns the currently active tracks.
        """
        self.frame_id += 1
        activated: list[STrack] = []
        refind: list[STrack] = []
        lost: list[STrack] = []
        removed: list[STrack] = []

        scores = np.asarray(scores, dtype=np.float32)
        remain_high = scores >= self.track_high_thresh
        inds_low = (scores > self.track_low_thresh) & (~remain_high)

        def make(mask) -> list[STrack]:
            out = []
            idxs = np.where(mask)[0]
            for i in idxs:
                f = feats[i] if (self.with_reid and feats is not None) else None
                out.append(STrack(dets_xywh[i], scores[i], f))
            return out

        detections = make(remain_high)
        detections_low = make(inds_low)

        # Split current tracks into confirmed vs not-yet-confirmed.
        unconfirmed = [t for t in self.tracked_stracks if not t.is_activated]
        tracked = [t for t in self.tracked_stracks if t.is_activated]

        # ---- stage 1: high-score dets vs (tracked + lost), IoU+appearance ----
        strack_pool = _join(tracked, self.lost_stracks)
        STrack.multi_predict(strack_pool)
        dists = self._get_dists(strack_pool, detections)
        matches, u_track, u_det = matching.linear_assignment(dists, self.match_thresh)
        for it, idet in matches:
            track, det = strack_pool[it], detections[idet]
            if track.state == TrackState.TRACKED:
                track.update(det, self.frame_id)
                activated.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind.append(track)

        # ---- stage 2: low-score dets vs remaining TRACKED tracks, IoU only ----
        r_tracked = [strack_pool[i] for i in u_track
                     if strack_pool[i].state == TrackState.TRACKED]
        dists = matching.iou_distance(r_tracked, detections_low)
        matches, u_track2, _ = matching.linear_assignment(dists, 0.5)
        for it, idet in matches:
            track, det = r_tracked[it], detections_low[idet]
            if track.state == TrackState.TRACKED:
                track.update(det, self.frame_id)
                activated.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind.append(track)
        for it in u_track2:
            track = r_tracked[it]
            if track.state != TrackState.LOST:
                track.mark_lost()
                lost.append(track)

        # ---- unconfirmed tracks (single-detection) vs remaining high dets ----
        detections = [detections[i] for i in u_det]
        dists = self._get_dists(unconfirmed, detections)
        matches, u_unconf, u_det = matching.linear_assignment(dists, 0.7)
        for it, idet in matches:
            unconfirmed[it].update(detections[idet], self.frame_id)
            activated.append(unconfirmed[it])
        for it in u_unconf:
            unconfirmed[it].mark_removed()
            removed.append(unconfirmed[it])

        # ---- init new tracks from strong leftover detections ----
        for idet in u_det:
            det = detections[idet]
            if det.score < self.new_track_thresh:
                continue
            det.activate(self.frame_id)
            activated.append(det)

        # ---- expire lost tracks past the buffer ----
        for track in self.lost_stracks:
            if self.frame_id - track.frame_id > self.max_time_lost:
                track.mark_removed()
                removed.append(track)

        # ---- bookkeeping ----
        self.tracked_stracks = [t for t in self.tracked_stracks
                                if t.state == TrackState.TRACKED]
        self.tracked_stracks = _join(self.tracked_stracks, activated)
        self.tracked_stracks = _join(self.tracked_stracks, refind)
        self.lost_stracks = _sub(self.lost_stracks, self.tracked_stracks)
        self.lost_stracks.extend(lost)
        self.lost_stracks = _sub(self.lost_stracks, removed)
        self.tracked_stracks, self.lost_stracks = _remove_duplicate(
            self.tracked_stracks, self.lost_stracks)
        self.removed_stracks.extend(removed)
        if len(self.removed_stracks) > 1000:
            self.removed_stracks = self.removed_stracks[-1000:]

        return [t for t in self.tracked_stracks if t.is_activated]
