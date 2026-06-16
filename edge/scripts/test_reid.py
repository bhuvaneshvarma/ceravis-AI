#!/usr/bin/env python3
"""
Standalone ReID engine self-test.

Loads the SAME ReIDExtractor + TensorRT engine the service uses and embeds a
dummy crop — so if the engine is present but won't load (corrupt, TRT version
mismatch, wrong dim, …) you see the REAL error and traceback here, instead of
the generic "run export_reid.sh" the enrollment worker used to show.

Usage (on the Jetson):
    python3 scripts/test_reid.py                 # dummy crop
    python3 scripts/test_reid.py /path/img.jpg   # embed a real image

Exit code 0 = engine loaded and produced a valid embedding.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = os.path.dirname(HERE)
sys.path.insert(0, EDGE)

from config.settings import settings                 # noqa: E402


def main() -> int:
    print(f"[reid] engine  : {settings.reid_model_path}")
    print(f"[reid] input   : {settings.reid_input_width}x{settings.reid_input_height}"
          f"  dim={settings.reid_embedding_dim}")

    try:
        from reid.reid_extractor import ReIDExtractor
        extractor = ReIDExtractor()
    except FileNotFoundError as exc:
        print(f"[reid] ENGINE NOT BUILT: {exc}")
        print("[reid] -> run: bash scripts/export_reid.sh")
        return 2
    except Exception:
        import traceback
        print("[reid] ENGINE FAILED TO LOAD (real reason below):")
        traceback.print_exc()
        return 3

    # Use a real image if given, else a synthetic crop.
    crop = None
    if len(sys.argv) > 1:
        import cv2
        crop = cv2.imread(sys.argv[1])
        if crop is None:
            print(f"[reid] could not read image: {sys.argv[1]}")
            return 1
        print(f"[reid] image   : {sys.argv[1]} {crop.shape}")
    if crop is None:
        crop = (np.random.rand(256, 128, 3) * 255).astype(np.uint8)

    emb = extractor.embed(crop)
    norm = float(np.linalg.norm(emb))
    print(f"[reid] embedding: shape={emb.shape} dtype={emb.dtype} L2-norm={norm:.4f}")

    if emb.shape[0] != settings.reid_embedding_dim:
        print(f"[reid] DIM MISMATCH: got {emb.shape[0]}, config says "
              f"{settings.reid_embedding_dim} — set REID_EMBEDDING_DIM to match.")
        return 4
    if norm < 0.5:
        print("[reid] WARNING: embedding norm ~0 — engine output looks empty.")
        return 5
    print("[reid] OK — ReID engine loads and produces valid embeddings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
