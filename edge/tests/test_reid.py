#!/usr/bin/env python3
"""
Standalone ReID engine self-test.

Loads the SAME ReIDExtractor + TensorRT engine the service uses and embeds a
crop — so if the engine is present but won't load (corrupt, TRT version
mismatch, wrong dim, CUDA-context/threading bug …) you see the REAL error and
traceback here, instead of the generic "run export_reid.sh".

IMPORTANT: by default the load+embed runs in a BACKGROUND thread, exactly like
the enrollment worker and the ReID runner do. The earlier main-thread-only
test could pass while the worker still failed with
"invalid device context - no currently active context"; this reproduces the
real deployment condition. Use --main-thread to force the old behaviour.

Usage (on the Jetson):
    python3 tests/test_reid.py                 # dummy crop, worker-thread
    python3 tests/test_reid.py /path/img.jpg   # embed a real image
    python3 tests/test_reid.py --main-thread   # run on the main thread

Exit code 0 = engine loaded and produced a valid embedding off-thread.
"""
import os
import sys
import threading

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = os.path.dirname(HERE)
sys.path.insert(0, EDGE)

from config.settings import settings                 # noqa: E402


def _run_once(img_path: str | None) -> int:
    try:
        from reid.reid_extractor import ReIDExtractor
        extractor = ReIDExtractor()
    except FileNotFoundError as exc:
        print(f"[reid] ENGINE NOT BUILT: {exc}")
        print("[reid] -> run: bash setup/export_reid.sh")
        return 2
    except Exception:
        import traceback
        print("[reid] ENGINE FAILED TO LOAD (real reason below):")
        traceback.print_exc()
        return 3

    crop = None
    if img_path:
        import cv2
        crop = cv2.imread(img_path)
        if crop is None:
            print(f"[reid] could not read image: {img_path}")
            return 1
        print(f"[reid] image   : {img_path} {crop.shape}")
    if crop is None:
        crop = (np.random.rand(256, 128, 3) * 255).astype(np.uint8)

    try:
        emb = extractor.embed(crop)
    except Exception:
        import traceback
        print("[reid] EMBED FAILED (real reason below):")
        traceback.print_exc()
        return 3

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


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--main-thread"]
    main_thread = "--main-thread" in sys.argv
    img_path = args[0] if args else None

    print(f"[reid] engine  : {settings.reid_model_path}")
    print(f"[reid] input   : {settings.reid_input_width}x{settings.reid_input_height}"
          f"  dim={settings.reid_embedding_dim}")
    print(f"[reid] mode    : {'main thread' if main_thread else 'worker thread'}")

    if main_thread:
        return _run_once(img_path)

    # Reproduce the service: build + use the engine off the main thread.
    result: dict[str, int] = {}
    t = threading.Thread(target=lambda: result.__setitem__("rc", _run_once(img_path)),
                         name="reid-selftest")
    t.start()
    t.join()
    return result.get("rc", 1)


if __name__ == "__main__":
    sys.exit(main())
