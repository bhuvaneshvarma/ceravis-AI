from __future__ import annotations

"""
Association cost matrices + linear assignment for the BoT-SORT tracker.

Costs are in [0, 1] (0 = perfect match). We use scipy's Hungarian solver
(`linear_sum_assignment`), with the faster `lap.lapjv` used automatically if it
happens to be installed. scipy is already present on the Jetson (pulled in by
the apt stack supervision needs), so there is no new runtime dependency.
"""

import numpy as np

try:
    import lap  # type: ignore
    _HAVE_LAP = True
except ImportError:  # pragma: no cover
    lap = None  # type: ignore
    _HAVE_LAP = False

from scipy.optimize import linear_sum_assignment


def linear_assignment(cost_matrix: np.ndarray, thresh: float):
    """
    Solve the assignment problem and split rows/cols by a cost gate.

    Returns (matches, unmatched_a, unmatched_b) where matches is an (M, 2)
    array of [row, col] index pairs whose cost is <= thresh.
    """
    if cost_matrix.size == 0:
        return (np.empty((0, 2), dtype=int),
                tuple(range(cost_matrix.shape[0])),
                tuple(range(cost_matrix.shape[1])))

    matches, unmatched_a, unmatched_b = [], [], []
    if _HAVE_LAP:
        _, x, y = lap.lapjv(cost_matrix, extend_cost=True, cost_limit=thresh)
        for ix, mx in enumerate(x):
            if mx >= 0:
                matches.append([ix, mx])
        unmatched_a = np.where(x < 0)[0]
        unmatched_b = np.where(y < 0)[0]
    else:
        rows, cols = linear_sum_assignment(cost_matrix)
        matched_rows, matched_cols = set(), set()
        for r, c in zip(rows, cols):
            if cost_matrix[r, c] <= thresh:
                matches.append([r, c])
                matched_rows.add(r)
                matched_cols.add(c)
        unmatched_a = [r for r in range(cost_matrix.shape[0]) if r not in matched_rows]
        unmatched_b = [c for c in range(cost_matrix.shape[1]) if c not in matched_cols]

    matches = (np.asarray(matches, dtype=int) if matches
               else np.empty((0, 2), dtype=int))
    return matches, tuple(unmatched_a), tuple(unmatched_b)


def _xywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    out = boxes.copy().astype(np.float32)
    out[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
    out[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
    out[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.0
    out[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.0
    return out


def ious(atlbr: np.ndarray, btlbr: np.ndarray) -> np.ndarray:
    """IoU between two sets of [x1, y1, x2, y2] boxes."""
    if atlbr.size == 0 or btlbr.size == 0:
        return np.zeros((len(atlbr), len(btlbr)), dtype=np.float32)
    area_a = (atlbr[:, 2] - atlbr[:, 0]) * (atlbr[:, 3] - atlbr[:, 1])
    area_b = (btlbr[:, 2] - btlbr[:, 0]) * (btlbr[:, 3] - btlbr[:, 1])

    lt = np.maximum(atlbr[:, None, :2], btlbr[None, :, :2])
    rb = np.minimum(atlbr[:, None, 2:], btlbr[None, :, 2:])
    wh = np.clip(rb - lt, 0, None)
    inter = wh[..., 0] * wh[..., 1]
    union = area_a[:, None] + area_b[None, :] - inter
    return np.where(union > 0, inter / union, 0.0).astype(np.float32)


def iou_distance(tracks, detections) -> np.ndarray:
    """1 - IoU cost between predicted track boxes and detection boxes (xywh)."""
    if not tracks or not detections:
        return np.zeros((len(tracks), len(detections)), dtype=np.float32)
    a = _xywh_to_xyxy(np.asarray([t.xywh for t in tracks], dtype=np.float32))
    b = _xywh_to_xyxy(np.asarray([d.xywh for d in detections], dtype=np.float32))
    return 1.0 - ious(a, b)


def embedding_distance(tracks, detections, metric: str = "cosine") -> np.ndarray:
    """
    Cosine distance between each track's smoothed (EMA) feature and each
    detection's current feature. Result in [0, 1]; missing features -> 1.
    """
    cost = np.ones((len(tracks), len(detections)), dtype=np.float32)
    if cost.size == 0:
        return cost
    for i, t in enumerate(tracks):
        tf = t.smooth_feat
        if tf is None:
            continue
        for j, d in enumerate(detections):
            df = d.curr_feat
            if df is None or df.shape[0] != tf.shape[0]:
                continue
            # features are L2-normalized -> dot product is cosine similarity
            cost[i, j] = max(0.0, 1.0 - float(np.dot(tf, df)))
    return cost


def fuse_motion_appearance(iou_cost: np.ndarray, emb_cost: np.ndarray,
                           proximity_thresh: float,
                           appearance_thresh: float) -> np.ndarray:
    """
    BoT-SORT cost fusion: take the appearance cost, but veto it where the
    boxes are too far apart (IoU below proximity) or the appearance is too
    weak (above the appearance gate), then take the element-wise minimum with
    the IoU cost. This lets appearance rescue an association the IoU lost
    during a crossover, without letting a look-alike across the room steal it.
    """
    if emb_cost.size == 0:
        return iou_cost
    emb = emb_cost.copy()
    emb[iou_cost > proximity_thresh] = 1.0      # too far -> ignore appearance
    emb[emb > appearance_thresh] = 1.0          # too different -> ignore appearance
    return np.minimum(iou_cost, emb)
