"""
Local sanity test for the clean-room BoT-SORT tracker (no TRT/torch needed).

Run:  PYTHONPATH=edge python edge/tests/test_botsort.py

Scenario 1 (crossover): two people walk through each other; appearance must
keep their IDs from swapping.
Scenario 2 (occlusion): one person is fully hidden for several frames, then
reappears; the original ID must be restored from the lost buffer.
"""
import numpy as np

from tracking.botsort import BoTSORT, STrack


def _feat(seed: int, dim: int = 512) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _noisy(v: np.ndarray, eps: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = v + eps * rng.standard_normal(v.shape).astype(np.float32)
    return n / np.linalg.norm(n)


def _mk_tracker() -> BoTSORT:
    return BoTSORT(
        track_high_thresh=0.5, track_low_thresh=0.1, new_track_thresh=0.6,
        match_thresh=0.8, track_buffer=30, proximity_thresh=0.5,
        appearance_thresh=0.25, with_reid=True, frame_rate=10.0,
    )


def _id_for_feat(tracks, target, dim=512):
    best, best_sim = None, -1
    for t in tracks:
        if t.smooth_feat is None:
            continue
        sim = float(np.dot(t.smooth_feat, target))
        if sim > best_sim:
            best_sim, best = sim, t.track_id
    return best


def test_crossover():
    STrack._count = 0
    tr = _mk_tracker()
    fA, fB = _feat(1), _feat(2)
    h, w = 200.0, 80.0
    ids_A, ids_B = [], []
    for k in range(40):
        ax = 100 + k * 12          # A: left -> right
        bx = 580 - k * 12          # B: right -> left
        dets = np.array([[ax, 300, w, h], [bx, 300, w, h]], dtype=np.float32)
        scores = np.array([0.9, 0.9], dtype=np.float32)
        feats = np.stack([_noisy(fA, 0.25, 100 + k), _noisy(fB, 0.25, 200 + k)])
        tracks = tr.update(dets, scores, feats)
        ids_A.append(_id_for_feat(tracks, fA))
        ids_B.append(_id_for_feat(tracks, fB))

    # After warm-up, the ID bound to each appearance must stay constant.
    a_stable = len(set(i for i in ids_A[5:] if i is not None)) == 1
    b_stable = len(set(i for i in ids_B[5:] if i is not None)) == 1
    print(f"[crossover] A ids={ids_A[-8:]}  stable={a_stable}")
    print(f"[crossover] B ids={ids_B[-8:]}  stable={b_stable}")
    assert a_stable and b_stable, "ID swapped during crossover!"
    print("[crossover] PASS\n")


def test_occlusion_recovery():
    STrack._count = 0
    tr = _mk_tracker()
    fA, fB = _feat(3), _feat(4)
    h, w = 200.0, 80.0
    # warm up both
    for k in range(6):
        dets = np.array([[150, 300, w, h], [450, 300, w, h]], dtype=np.float32)
        feats = np.stack([_noisy(fA, 0.2, 300 + k), _noisy(fB, 0.2, 400 + k)])
        tr.update(dets, np.array([0.9, 0.9], np.float32), feats)
    tracks = tr.update(np.array([[150, 300, w, h], [450, 300, w, h]], np.float32),
                       np.array([0.9, 0.9], np.float32),
                       np.stack([_noisy(fA, 0.2, 1), _noisy(fB, 0.2, 2)]))
    id_b_before = _id_for_feat(tracks, fB)

    # B disappears (fully occluded) for 8 frames — only A detected.
    for k in range(8):
        dets = np.array([[150, 300, w, h]], dtype=np.float32)
        tr.update(dets, np.array([0.9], np.float32),
                  np.stack([_noisy(fA, 0.2, 500 + k)]))

    # B reappears at roughly the same spot.
    tracks = tr.update(np.array([[150, 300, w, h], [455, 300, w, h]], np.float32),
                       np.array([0.9, 0.9], np.float32),
                       np.stack([_noisy(fA, 0.2, 9), _noisy(fB, 0.2, 10)]))
    id_b_after = _id_for_feat(tracks, fB)
    print(f"[occlusion] B id before={id_b_before} after={id_b_after}")
    assert id_b_before == id_b_after, "lost target got a NEW id on reappearance!"
    print("[occlusion] PASS\n")


if __name__ == "__main__":
    test_crossover()
    test_occlusion_recovery()
    print("ALL TRACKER TESTS PASSED")
