from __future__ import annotations

import logging
import threading

import numpy as np

from config.settings import resolve_path

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
      - Batches: an engine built with a dynamic batch dimension runs up to
        `max_batch` samples in one call (`infer` with a leading batch axis);
        buffers are sized for that batch once. A fixed-batch engine — or
        max_batch=1, the default — behaves exactly as a one-sample engine.
    """

    _TRT_LOGGER = None

    def __init__(self, engine_path: str, max_batch: int = 1) -> None:
        if not _TRT_AVAILABLE:
            raise RuntimeError(
                "TensorRT/PyCUDA not available. "
                "This module only runs on a Jetson with JetPack."
            )

        TensorRTEngine._TRT_LOGGER = (
            TensorRTEngine._TRT_LOGGER
            or trt.Logger(trt.Logger.WARNING)
        )

        # Relative engine paths anchor at edge/, whatever the working folder.
        self._engine_path = resolve_path(engine_path)
        if not self._engine_path.exists():
            raise FileNotFoundError(
                f"TensorRT engine not found: {self._engine_path}. "
                "Run setup/export_models.py to build it."
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
        self._requested_batch = max(1, int(max_batch))
        self._max_batch = 1          # what this engine really takes (set below)
        self._dynamic_batch = False

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
    def _resolve_input_shape(self, name: str, shape: tuple[int, ...]) -> tuple[int, ...]:
        """The concrete shape a dynamic input runs at, read from the engine's
        own optimisation profile — never guessed. Every dynamic dimension takes
        the profile's 'opt' value: an ultralytics dynamic export leaves height
        and width open too, and the engine is built for exactly one size there.
        A dynamic BATCH (dim 0) is the largest batch both we asked for and the
        profile allows (one at a time on the legacy v2 API)."""
        try:
            low, opt, high = (tuple(int(d) for d in s)
                              for s in self._engine.get_tensor_profile_shape(name, 0))
        except Exception as exc:
            raise RuntimeError(
                f"{self._engine_path.name}: input '{name}' has dynamic dimensions "
                f"{shape} but no readable optimisation profile ({exc})") from exc
        dims = [opt[i] if d < 0 else d for i, d in enumerate(shape)]
        if shape[0] < 0:
            want = self._requested_batch if self._use_v3 else 1
            dims[0] = max(low[0], min(want, high[0]))
        return tuple(dims)

    def _allocate_buffers(self) -> None:
        # Pass 1 — resolve dynamic input dims from the engine's profile
        # (_resolve_input_shape). Buffers are allocated at that size once.
        for idx in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(idx)
            if self._engine.get_tensor_mode(name) != trt.TensorIOMode.INPUT:
                continue
            shape = tuple(self._engine.get_tensor_shape(name))
            if not any(d < 0 for d in shape):
                continue
            resolved = self._resolve_input_shape(name, shape)
            self._dynamic_batch = shape[0] < 0 and self._use_v3
            self._max_batch = resolved[0] if shape[0] < 0 else 1
            self._context.set_input_shape(name, resolved)

        # Pass 2 — allocate pinned host + device buffers.
        for idx in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(idx)
            # Context shapes are resolved after set_input_shape above; one still
            # open here would size a buffer wrongly, so it is an error.
            shape = tuple(self._context.get_tensor_shape(name))
            if any(d < 0 for d in shape):
                raise RuntimeError(
                    f"{self._engine_path.name}: tensor '{name}' shape {shape} "
                    "is unresolved after setting the input shapes")
            dtype = trt.nptype(self._engine.get_tensor_dtype(name))
            size = int(np.prod(shape))

            # Pinned host buffer = faster DMA on Jetson
            host = cuda.pagelocked_empty(size, dtype)
            dev = cuda.mem_alloc(host.nbytes)

            self._names.append(name)
            self._host_buffers.append(host)
            self._device_buffers.append(dev)
            self._bindings.append(int(dev))
            # The ONE-sample shape, as callers have always seen it (input_shape,
            # output_shapes); a batch scales dim 0.
            self._shapes.append((1,) + shape[1:] if self._max_batch > 1 else shape)
            self._dtypes.append(dtype)

            if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self._input_indices.append(idx)
            else:
                self._output_indices.append(idx)

            if self._use_v3:
                self._context.set_tensor_address(name, int(dev))

    # ---- inference ---------------------------------------------------
    @property
    def max_batch(self) -> int:
        """How many samples one `infer` call may carry (1 = one at a time)."""
        return self._max_batch

    def infer(self, input_tensor: np.ndarray) -> list[np.ndarray]:
        """
        Run one inference pass over `input_tensor` — one sample, or up to
        `max_batch` stacked on the first axis. Returns the output arrays
        (copies), each with that same leading batch size.
        """
        if not self._input_indices:
            raise RuntimeError("No input bindings")
        in_idx = self._input_indices[0]
        per_sample = int(np.prod(self._shapes[in_idx][1:]))
        n = max(1, input_tensor.size // max(1, per_sample))
        if n > self._max_batch:
            raise ValueError(f"batch {n} exceeds this engine's max_batch {self._max_batch}")

        # Make the shared primary context current on THIS thread for the whole
        # transfer/execute/sync cycle, then restore the previous stack on exit.
        self._cuda_ctx.push()
        try:
            if self._dynamic_batch:
                self._context.set_input_shape(
                    self._names[in_idx], (n,) + self._shapes[in_idx][1:])
            count = input_tensor.size
            host_in = self._host_buffers[in_idx]
            np.copyto(host_in[:count], input_tensor.ravel())

            cuda.memcpy_htod_async(
                self._device_buffers[in_idx],
                host_in[:count],
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

            out_shapes = []
            for out_idx in self._output_indices:
                shape = self._shapes[out_idx]
                if self._dynamic_batch:
                    shape = tuple(self._context.get_tensor_shape(self._names[out_idx]))
                out_shapes.append(shape)
                cnt = int(np.prod(shape))
                cuda.memcpy_dtoh_async(
                    self._host_buffers[out_idx][:cnt],
                    self._device_buffers[out_idx],
                    self._stream,
                )

            self._stream.synchronize()

            outputs: list[np.ndarray] = []
            for out_idx, shape in zip(self._output_indices, out_shapes):
                cnt = int(np.prod(shape))
                outputs.append(self._host_buffers[out_idx][:cnt].reshape(shape).copy())
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
