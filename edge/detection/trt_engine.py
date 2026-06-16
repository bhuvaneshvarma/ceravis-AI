from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import numpy as np

# TensorRT and PyCUDA are provided by JetPack — not pip installable.
# We import lazily so dev (laptop) can still load other modules.
#
# NB: we deliberately do NOT `import pycuda.autoinit`. autoinit creates a CUDA
# context bound to the importing (main) thread; that context is not current on
# any other thread, so using an engine from a background worker thread raises
# "invalid device context - no currently active context". Detection, pose,
# ReID and the enrollment worker each run in their own thread, so instead we
# retain the device's PRIMARY context (shareable across threads) and push/pop
# it around every CUDA call — see `_cuda_context()` and the push/pop guards.
try:
    import tensorrt as trt  # type: ignore
    import pycuda.driver as cuda  # type: ignore
    _TRT_AVAILABLE = True
except ImportError:  # pragma: no cover
    trt = None  # type: ignore
    cuda = None  # type: ignore
    _TRT_AVAILABLE = False


logger = logging.getLogger("detection")

# The edge/ project root (this file is edge/detection/trt_engine.py). Engine
# paths in config are relative to it, so resolving against this root makes the
# loader independent of the process working directory — the same path works
# under systemd (cwd=edge), a shell launched from the repo root, an IDE, or a
# standalone test script.
_EDGE_ROOT = Path(__file__).resolve().parents[1]

# Process-wide CUDA primary context, created once and shared by every engine
# on every thread. Unlike a context made with Device.make_context(), the
# primary context can be current on multiple threads at once, so detection,
# pose and ReID can run concurrently on their own streams.
_CUDA_CTX = None
_CUDA_CTX_LOCK = threading.Lock()


def _cuda_context():
    """Lazily init CUDA and retain the device-0 primary context (thread-safe)."""
    global _CUDA_CTX
    if _CUDA_CTX is None:
        with _CUDA_CTX_LOCK:
            if _CUDA_CTX is None:
                cuda.init()
                _CUDA_CTX = cuda.Device(0).retain_primary_context()
    return _CUDA_CTX


class TensorRTEngine:
    """
    Generic TensorRT inference wrapper.

    Optimisations for Jetson Orin Nano Super:
      - Single execution context (no per-frame alloc)
      - Pinned host buffers (faster H2D / D2H)
      - Pre-allocated output reshape views
      - Reusable for YOLO, Pose, OSNet ReID
    """

    _TRT_LOGGER = None

    def __init__(self, engine_path: str) -> None:
        if not _TRT_AVAILABLE:
            raise RuntimeError(
                "TensorRT/PyCUDA not available. "
                "This module only runs on a Jetson with JetPack."
            )

        TensorRTEngine._TRT_LOGGER = (
            TensorRTEngine._TRT_LOGGER
            or trt.Logger(trt.Logger.WARNING)
        )

        engine = Path(engine_path)
        if not engine.is_absolute():
            engine = _EDGE_ROOT / engine
        self._engine_path = engine
        if not self._engine_path.exists():
            raise FileNotFoundError(
                f"TensorRT engine not found: {self._engine_path}. "
                "Run scripts/export_models.py to build it."
            )

        logger.info("Loading TRT engine: %s", self._engine_path)

        self._input_indices: list[int] = []
        self._output_indices: list[int] = []
        self._names: list[str] = []
        self._host_buffers: list[np.ndarray] = []
        self._device_buffers: list = []
        self._shapes: list[tuple[int, ...]] = []
        self._dtypes: list[type] = []
        self._bindings: list[int] = []

        # Deserialize, create the execution context, the stream and all device
        # buffers under an active CUDA context. push/pop keeps construction
        # valid regardless of which thread builds the engine (the enrollment
        # worker and ReID runner build theirs off the main thread).
        self._cuda_ctx = _cuda_context()
        self._cuda_ctx.push()
        try:
            self._runtime = trt.Runtime(TensorRTEngine._TRT_LOGGER)
            with open(self._engine_path, "rb") as f:
                self._engine = self._runtime.deserialize_cuda_engine(f.read())
            if self._engine is None:
                raise RuntimeError("Failed to deserialize TRT engine")

            self._context = self._engine.create_execution_context()
            self._stream = cuda.Stream()

            # TensorRT 10 (JetPack 6.1+) removed execute_async_v2; TRT 8.5+
            # already supports the v3 named-tensor API, so prefer it.
            self._use_v3 = hasattr(self._context, "execute_async_v3")

            self._allocate_buffers()
        finally:
            self._cuda_ctx.pop()

        logger.info(
            "TRT engine ready: %s (api=%s)",
            self._engine_path.name, "v3" if self._use_v3 else "v2",
        )

    # ---- alloc -------------------------------------------------------
    def _allocate_buffers(self) -> None:
        # Pass 1 — resolve dynamic input dims (-1) to batch 1: we always
        # infer one sample at a time (e.g. dynamic-batch ReID engines).
        for idx in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(idx)
            if self._engine.get_tensor_mode(name) != trt.TensorIOMode.INPUT:
                continue
            shape = tuple(self._engine.get_tensor_shape(name))
            if any(d < 0 for d in shape):
                self._context.set_input_shape(
                    name, tuple(1 if d < 0 else d for d in shape))

        # Pass 2 — allocate pinned host + device buffers.
        for idx in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(idx)
            # Context shapes are resolved after set_input_shape above.
            shape = tuple(self._context.get_tensor_shape(name))
            if any(d < 0 for d in shape):
                shape = tuple(1 if d < 0 else d for d in shape)
            dtype = trt.nptype(self._engine.get_tensor_dtype(name))
            size = int(np.prod(shape))

            # Pinned host buffer = faster DMA on Jetson
            host = cuda.pagelocked_empty(size, dtype)
            dev = cuda.mem_alloc(host.nbytes)

            self._names.append(name)
            self._host_buffers.append(host)
            self._device_buffers.append(dev)
            self._bindings.append(int(dev))
            self._shapes.append(shape)
            self._dtypes.append(dtype)

            if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self._input_indices.append(idx)
            else:
                self._output_indices.append(idx)

            if self._use_v3:
                self._context.set_tensor_address(name, int(dev))

    # ---- inference ---------------------------------------------------
    def infer(self, input_tensor: np.ndarray) -> list[np.ndarray]:
        """
        Run one inference pass. Returns list of output arrays
        (views into pinned host buffers — caller must copy if storing).
        """
        if not self._input_indices:
            raise RuntimeError("No input bindings")

        # Make the shared primary context current on THIS thread for the whole
        # transfer/execute/sync cycle, then restore the previous stack on exit.
        self._cuda_ctx.push()
        try:
            in_idx = self._input_indices[0]
            np.copyto(self._host_buffers[in_idx], input_tensor.ravel())

            cuda.memcpy_htod_async(
                self._device_buffers[in_idx],
                self._host_buffers[in_idx],
                self._stream,
            )

            if self._use_v3:
                # TRT 10 path — tensor addresses were registered at init.
                self._context.execute_async_v3(stream_handle=self._stream.handle)
            else:
                # Legacy TRT 8 path.
                self._context.execute_async_v2(
                    self._bindings,
                    stream_handle=self._stream.handle,
                )

            for out_idx in self._output_indices:
                cuda.memcpy_dtoh_async(
                    self._host_buffers[out_idx],
                    self._device_buffers[out_idx],
                    self._stream,
                )

            self._stream.synchronize()

            outputs: list[np.ndarray] = []
            for out_idx in self._output_indices:
                outputs.append(
                    self._host_buffers[out_idx]
                    .reshape(self._shapes[out_idx])
                    .copy()
                )
            return outputs
        finally:
            self._cuda_ctx.pop()

    # ---- introspection ----------------------------------------------
    @property
    def input_shape(self) -> tuple[int, ...]:
        return self._shapes[self._input_indices[0]]

    @property
    def output_shapes(self) -> list[tuple[int, ...]]:
        return [self._shapes[i] for i in self._output_indices]
