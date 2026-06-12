from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

# TensorRT and PyCUDA are provided by JetPack — not pip installable.
# We import lazily so dev (laptop) can still load other modules.
try:
    import tensorrt as trt  # type: ignore
    import pycuda.driver as cuda  # type: ignore
    import pycuda.autoinit  # noqa: F401  # type: ignore
    _TRT_AVAILABLE = True
except ImportError:  # pragma: no cover
    trt = None  # type: ignore
    cuda = None  # type: ignore
    _TRT_AVAILABLE = False


logger = logging.getLogger("detection")


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

        self._engine_path = Path(engine_path)
        if not self._engine_path.exists():
            raise FileNotFoundError(
                f"TensorRT engine not found: {self._engine_path}. "
                "Run scripts/export_models.py to build it."
            )

        logger.info("Loading TRT engine: %s", self._engine_path)

        self._runtime = trt.Runtime(TensorRTEngine._TRT_LOGGER)
        with open(self._engine_path, "rb") as f:
            self._engine = self._runtime.deserialize_cuda_engine(f.read())
        if self._engine is None:
            raise RuntimeError("Failed to deserialize TRT engine")

        self._context = self._engine.create_execution_context()
        self._stream = cuda.Stream()

        self._input_indices: list[int] = []
        self._output_indices: list[int] = []
        self._names: list[str] = []
        self._host_buffers: list[np.ndarray] = []
        self._device_buffers: list = []
        self._shapes: list[tuple[int, ...]] = []
        self._dtypes: list[type] = []
        self._bindings: list[int] = []

        # TensorRT 10 (JetPack 6.1+) removed execute_async_v2; TRT 8.5+
        # already supports the v3 named-tensor API, so prefer it.
        self._use_v3 = hasattr(self._context, "execute_async_v3")

        self._allocate_buffers()
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

        outputs: list[np.ndarray] = []
        for out_idx in self._output_indices:
            cuda.memcpy_dtoh_async(
                self._host_buffers[out_idx],
                self._device_buffers[out_idx],
                self._stream,
            )

        self._stream.synchronize()

        for out_idx in self._output_indices:
            outputs.append(
                self._host_buffers[out_idx]
                .reshape(self._shapes[out_idx])
                .copy()
            )
        return outputs

    # ---- introspection ----------------------------------------------
    @property
    def input_shape(self) -> tuple[int, ...]:
        return self._shapes[self._input_indices[0]]

    @property
    def output_shapes(self) -> list[tuple[int, ...]]:
        return [self._shapes[i] for i in self._output_indices]
