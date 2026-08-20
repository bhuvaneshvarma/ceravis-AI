"""
Local test for recency-fused target acquisition (no TRT, no FAISS needed).

The scenario is the one that actually goes wrong in a home: the recipient's
appearance has DRIFTED from what was enrolled (different clothes today), so they
only weakly clear the gallery, while a visitor happens to resemble the OLD
enrolled photos more closely. Gallery-alone therefore ranks the VISITOR first.

Run:  PYTHONPATH=edge python edge/tests/test_recency.py
"""
import math
import time

import numpy as np

from config.settings import settings
from reid.recency_buffer import RecencyBuffer
from reid.target_lock import TargetLockManager


D = 8


def _vec(**parts) -> np.ndarray:
    v = np.zeros(D, dtype=np.float32)
    for i, c in parts.items():
        v[int(i)] = c
    return v / np.linalg.norm(v)


ENROLLED = _vec(**{"0": 1.0})                                  # the stored view
TARGET_NOW = _vec(**{"0": 0.62, "1": math.sqrt(1 - 0.62 ** 2)})  # drifted, cos .62
LOOKALIKE = _vec(**{"0": 0.70, "2": math.sqrt(1 - 0.70 ** 2)})   # cos .70 — closer!

VISITOR_TID, TARGET_TID = 7, 9
BOXES = {VISITOR_TID: (100, 100, 180, 400),
         TARGET_TID: (300, 100, 380, 400)}
FEATS = {VISITOR_TID: LOOKALIKE, TARGET_TID: TARGET_NOW}


def _feat_for(tid):
    return FEATS[tid]


class _StubGallery:
    """Holds only the ENROLLED view — the real case after a wardrobe change."""

    rows = np.stack([ENROLLED], axis=0)

    def match(self, feat):
        class M:
            pass
        m = M()
        m.score = float((self.rows @ np.asarray(feat, dtype=np.float32)).max())
        m.recipient_id = "ravi"
        m.view_label = None
        m.is_match = m.score >= settings.reid_match_threshold
        return m


def test_premise():
    """Both clear the gallery, and the WRONG one ranks higher."""
    g = _StubGallery()
    gl, gt = g.match(LOOKALIKE).score, g.match(TARGET_NOW).score
    assert g.match(LOOKALIKE).is_match and g.match(TARGET_NOW).is_match
    assert gl > gt, "premise broken — visitor must outscore the target"
    print(f"[premise] visitor={gl:.3f} > recipient={gt:.3f}  both over "
          f"{settings.reid_match_threshold}  PASS")


def test_gallery_alone_picks_wrong():
    out = TargetLockManager(_StubGallery()).update("cam-A", BOXES, _feat_for)
    assert out.target_track_id == VISITOR_TID
    print(f"[gallery only] locks track {out.target_track_id} (the visitor)  PASS")


def test_recency_picks_right_and_vetoes():
    rb = RecencyBuffer()
    rb.push("ravi", TARGET_NOW)          # last confident look before they vanished
    out = TargetLockManager(_StubGallery(), recency=rb).update(
        "cam-B", BOXES, _feat_for)
    assert out.target_track_id == TARGET_TID, "recency failed to correct the pick"
    visitor_rec = rb.score("ravi", LOOKALIKE)
    assert visitor_rec < settings.reid_recency_min_score, "visitor was not vetoed"
    print(f"[recency] locks track {out.target_track_id} (the recipient), "
          f"visitor recency={visitor_rec:.3f} < {settings.reid_recency_min_score}  PASS")


def test_cold_start_never_blocks_first_lock():
    """No memory must fall back to the gallery, not refuse to lock."""
    out = TargetLockManager(_StubGallery(), recency=RecencyBuffer()).update(
        "cam-C", BOXES, _feat_for)
    assert out.target_track_id is not None
    print(f"[cold start] still locks (track {out.target_track_id})  PASS")


def test_ttl_expiry_falls_back():
    rb = RecencyBuffer()
    rb.push("ravi", TARGET_NOW)
    original = settings.reid_recency_ttl_secs
    try:
        settings.reid_recency_ttl_secs = 0.05
        time.sleep(0.1)
        assert rb.score("ravi", TARGET_NOW) is None, "entry should have aged out"
        out = TargetLockManager(_StubGallery(), recency=rb).update(
            "cam-D", BOXES, _feat_for)
        assert out.target_track_id is not None
        print(f"[ttl expiry] memory aged out, gallery alone again "
              f"(track {out.target_track_id})  PASS")
    finally:
        settings.reid_recency_ttl_secs = original


def test_window_is_bounded():
    rb = RecencyBuffer()
    for _ in range(settings.reid_recency_max * 3):
        rb.push("ravi", TARGET_NOW)
    assert rb._live_count("ravi") == settings.reid_recency_max
    print(f"[bounded] window capped at {settings.reid_recency_max}  PASS")


if __name__ == "__main__":
    test_premise()
    test_gallery_alone_picks_wrong()
    test_recency_picks_right_and_vetoes()
    test_cold_start_never_blocks_first_lock()
    test_ttl_expiry_falls_back()
    test_window_is_bounded()
    print("ALL RECENCY TESTS PASSED")
