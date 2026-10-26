"""
Process-wide settings that must be in place BEFORE numpy, faiss or scipy are
imported — so every entry point (serve.py, main.py, a tool) calls
cap_math_threads() as its very first statement. This module imports nothing
heavy itself.

Math-library worker pools: ONE thread each. Each bundled OpenBLAS (pip numpy,
faiss-cpu, apt scipy) and each OpenMP runtime otherwise sizes itself to every
core and BUSY-WAITS between the tiny matrix jobs that tracking / ReID / FAISS
issue dozens of times a second. Measured on the bench (2026-09-23): 5 spinning
pool threads ≈ 2.9 of the 6 cores, for work that takes microseconds. Our
concurrency comes from our own runner threads, not from splitting a
100-vector search across cores. setdefault: the unit/env can still override
one for an experiment.
"""
from __future__ import annotations

import os

_POOLS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
          "GOTO_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def cap_math_threads() -> None:
    for var in _POOLS:
        os.environ.setdefault(var, "1")
    os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")   # never spin an idle pool
