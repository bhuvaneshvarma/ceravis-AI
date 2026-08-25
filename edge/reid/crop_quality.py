from __future__ import annotations

"""
Is this crop good enough to make an identity decision from?

The single highest-yield addition to an identity pipeline, and the cheapest.
Most catastrophic ReID errors are not threshold problems — they are the network
embedding garbage *confidently*: a motion-blurred smear, a person half out of
frame, a 20-pixel figure at the far end of a room. The embedding comes back
looking like a perfectly ordinary vector, lands somewhere arbitrary in the
gallery, and no score threshold can tell that its INPUT was meaningless.

So the gate sits BEFORE the model, not after it. A crop that cannot support a
decision is never embedded at all — which also means it never costs GPU.

Four independent reasons to refuse, each catching a different real failure:

  AREA        a distant figure carries too few pixels to identify. OSNet resizes
              to 256x128 regardless, so a 30x15 crop is upsampled noise.
  ASPECT      a person is roughly 2-3x taller than wide. Far outside that and
              the box is not a standing person — it is a merged pair, a
              reflection, or a detection sitting on furniture.
  TRUNCATION  a box against the frame edge is a PARTIAL person. Half a torso
              embeds as a confident vector for a body that was never seen.
  SHARPNESS   variance of the Laplacian. Motion blur destroys exactly the
              texture detail appearance matching depends on, and a moving
              person is when blur is worst — which is also when identity
              questions get asked.

The score is also what BEST-SHOT selection ranks on (reid/best_shot.py), so one
definition of "good crop" serves both gating and choosing.
"""

import numpy as np
from dataclasses import dataclass

from config.settings import settings

try:
    import cv2
    _HAVE_CV2 = True
except ImportError:                                  # pragma: no cover
    cv2 = None                                       # type: ignore
    _HAVE_CV2 = False


@dataclass(frozen=True, slots=True)
class Quality:
    """Verdict plus the numbers behind it — `reason` is what makes a rejected
    crop debuggable instead of a silent gap in the identity timeline."""
    ok: bool
    score: float                 # 0..1, higher is better; ranks best-shot
    reason: str                  # "" when ok
    area_px: int = 0
    aspect: float = 0.0
    sharpness: float = 0.0
    truncated: bool = False


def _sharpness(crop: np.ndarray) -> float:
    """Variance of the Laplacian, normalised. Cheap and the standard blur proxy.

    Computed on a DOWNSCALED grayscale copy: full-resolution Laplacian on every
    candidate crop would cost more than the gate saves, and blur is a
    low-frequency property that survives downscaling."""
    if not _HAVE_CV2 or crop.size == 0:
        return 1.0                               # cannot judge -> do not block
    g = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h, w = g.shape[:2]
    if h > 128:
        g = cv2.resize(g, (max(8, int(w * 128 / h)), 128),
                       interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g.astype(np.float32), cv2.CV_32F).var())


def assess(crop: np.ndarray, bbox, frame_w: int, frame_h: int,
           confidence: float = 1.0) -> Quality:
    """Judge one person crop. `bbox` is the source box in FRAME coordinates —
    needed for the truncation test, which cannot be seen from the crop alone."""
    if crop is None or crop.size == 0:
        return Quality(False, 0.0, "empty crop")

    h, w = crop.shape[:2]
    area = int(h * w)
    aspect = (h / w) if w > 0 else 0.0

    if area < settings.crop_min_area_px:
        return Quality(False, 0.0, f"too small ({area}px)", area, aspect)

    if not (settings.crop_min_aspect <= aspect <= settings.crop_max_aspect):
        return Quality(False, 0.0, f"implausible aspect ({aspect:.2f})",
                       area, aspect)

    # Truncation: a box hard against the frame edge is a partial person. The
    # margin is fractional so it scales with resolution.
    m = settings.crop_edge_margin_frac
    mx, my = frame_w * m, frame_h * m
    truncated = (bbox.x1 <= mx or bbox.y1 <= my
                 or bbox.x2 >= frame_w - mx or bbox.y2 >= frame_h - my)
    if truncated and settings.crop_reject_truncated:
        return Quality(False, 0.0, "truncated at the frame edge",
                       area, aspect, 0.0, True)

    sharp = _sharpness(crop)
    if sharp < settings.crop_min_sharpness:
        return Quality(False, 0.0, f"too blurred (lap var {sharp:.1f})",
                       area, aspect, sharp, truncated)

    # ---- passed: build the ranking score -----------------------------
    # Each term saturates at 1.0, so a crop that is merely "big enough" does not
    # outrank a sharp one just by being large. Sharpness is weighted highest
    # because it is what appearance matching actually consumes.
    a = min(1.0, area / float(max(1, settings.crop_good_area_px)))
    s = min(1.0, sharp / float(max(1e-6, settings.crop_good_sharpness)))
    c = max(0.0, min(1.0, confidence))
    score = 0.5 * s + 0.3 * a + 0.2 * c
    if truncated:
        score *= 0.6                              # allowed through, but ranked down
    return Quality(True, round(score, 4), "", area, aspect, sharp, truncated)
