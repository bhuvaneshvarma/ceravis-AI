"""
The TensorRT wrapper (edge/detection/trt_engine.py) sizes every buffer from the
engine itself — driven here with a stand-in TensorRT/PyCUDA that checks every
shape against the engine's optimisation profile the way TensorRT does:

1. a dynamic ultralytics export (batch, height AND width open; the profile
   pins 640x640) runs at 640x640 — never at 1x1 — and batches up to max_batch
2. a batch larger than the engine allows is refused
3. a static one-frame engine (pose) is unchanged: batch 1, no shape calls
4. a dynamic-batch engine asked for one frame (ReID, enrollment) runs batch 1
5. a dynamic input without a readable profile is an error, not a guess

Pure Python: runs anywhere.

Run:  PYTHONPATH=edge python tests/test_trt_engine.py
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


# ---- a stand-in TensorRT --------------------------------------------------
class _Mode:
    INPUT, OUTPUT = "in", "out"


class FakeEngine:
    """inputs: {name: (shape with -1s, profile (min, opt, max) or None)};
    outputs: {name: fn(input shape) -> output shape}."""

    def __init__(self, inputs, outputs):
        self.inputs, self.outputs = inputs, outputs
        self.names = [*inputs, *outputs]
        self.num_io_tensors = len(self.names)

    def get_tensor_name(self, i):
        return self.names[i]

    def get_tensor_mode(self, name):
        return _Mode.INPUT if name in self.inputs else _Mode.OUTPUT

    def get_tensor_shape(self, name):
        return self.inputs[name][0] if name in self.inputs else (-1,)

    def get_tensor_profile_shape(self, name, _profile):
        prof = self.inputs[name][1]
        if prof is None:
            raise RuntimeError("no profile")
        return prof

    def get_tensor_dtype(self, _name):
        return np.float32

    def create_execution_context(self):
        return FakeContext(self)


class FakeContext:
    def __init__(self, engine):
        self.engine, self.set, self.calls = engine, {}, []

    def set_input_shape(self, name, shape):
        shape = tuple(shape)
        static, prof = self.engine.inputs[name]
        for i, d in enumerate(shape):
            if static[i] >= 0 and d != static[i]:
                raise ValueError(f"{name} dim {i}: {d} != fixed {static[i]}")
            if prof and not prof[0][i] <= d <= prof[2][i]:
                raise ValueError(f"{name} dim {i}: {d} outside profile "
                                 f"[{prof[0][i]}, {prof[2][i]}]")
        self.set[name] = shape
        self.calls.append(shape)

    def get_tensor_shape(self, name):
        if name in self.engine.inputs:
            return self.set.get(name, self.engine.inputs[name][0])
        first = next(iter(self.engine.inputs))
        return self.engine.outputs[name](self.set.get(first, self.engine.inputs[first][0]))

    def set_tensor_address(self, *_):
        pass

    def execute_async_v3(self, stream_handle):
        pass


_current = {}


class FakeRuntime:
    def __init__(self, _logger):
        pass

    def deserialize_cuda_engine(self, _blob):
        return _current["engine"]


trt = types.ModuleType("tensorrt")
trt.Logger = type("Logger", (), {"WARNING": 2, "__init__": lambda self, *_: None})
trt.Runtime = FakeRuntime
trt.TensorIOMode = _Mode
trt.nptype = lambda dt: dt


class _Ctx:
    def push(self):
        pass

    def pop(self):
        pass


cuda = types.ModuleType("pycuda.driver")
cuda.init = lambda: None
cuda.Device = lambda _i: types.SimpleNamespace(retain_primary_context=lambda: _Ctx())
cuda.Stream = lambda: types.SimpleNamespace(handle=0, synchronize=lambda: None)
cuda.pagelocked_empty = lambda size, dtype: np.zeros(size, dtype)
cuda.mem_alloc = lambda nbytes: 1
cuda.memcpy_htod_async = lambda *_: None
cuda.memcpy_dtoh_async = lambda *_: None
pycuda = types.ModuleType("pycuda")
pycuda.driver = cuda
sys.modules.update({"tensorrt": trt, "pycuda": pycuda, "pycuda.driver": cuda})

from detection import trt_engine                        # noqa: E402

ENGINE_FILE = Path(tempfile.mkdtemp(prefix="ceravis-trt-")) / "x.engine"
ENGINE_FILE.write_bytes(b"engine")


def load(engine, max_batch=1):
    _current["engine"] = engine
    return trt_engine.TensorRTEngine(str(ENGINE_FILE), max_batch=max_batch)


def detector(batch_max=4):
    """What ultralytics dynamic=True + export_models.py's trtexec shapes give."""
    return FakeEngine(
        {"images": ((-1, 3, -1, -1),
                    ((1, 3, 640, 640), (batch_max, 3, 640, 640), (batch_max, 3, 640, 640)))},
        {"output0": lambda s: (s[0], 300, 6)})


print("\n1. dynamic batch + height + width (the detector)")
eng = load(detector(), max_batch=4)
check("runs at 640x640, not 1x1", eng.input_shape == (1, 3, 640, 640), str(eng.input_shape))
check("batches up to 4", eng.max_batch == 4)
check("output sized per frame", eng.output_shapes == [(1, 300, 6)], str(eng.output_shapes))
out = eng.infer(np.zeros((3, 3, 640, 640), np.float32))
check("a 3-frame call -> 3 rows out", out[0].shape == (3, 300, 6), str(out[0].shape))
check("…set at 3x3x640x640", eng._context.calls[-1] == (3, 3, 640, 640))

print("\n2. too many frames")
try:
    eng.infer(np.zeros((5, 3, 640, 640), np.float32))
    check("5 frames on a max-4 engine is refused", False)
except ValueError:
    check("5 frames on a max-4 engine is refused", True)

print("\n3. a static one-frame engine (pose)")
pose = load(FakeEngine({"images": ((1, 3, 640, 640), None)},
                       {"output0": lambda s: (1, 300, 57)}))
check("shape as built", pose.input_shape == (1, 3, 640, 640) and pose.max_batch == 1)
check("no shape set on a static engine", pose._context.calls == [])
check("one frame in, one out", pose.infer(np.zeros((1, 3, 640, 640), np.float32))[0].shape
      == (1, 300, 57))

print("\n4. dynamic batch asked for one frame (ReID, enrollment)")
reid = load(FakeEngine({"input": ((-1, 3, 256, 128),
                                  ((1, 3, 256, 128), (1, 3, 256, 128), (8, 3, 256, 128)))},
                       {"output": lambda s: (s[0], 512)}))
check("batch 1 at 256x128", reid.input_shape == (1, 3, 256, 128) and reid.max_batch == 1)
one = load(detector(), max_batch=1)
check("the detector at max_batch=1 is one frame at 640x640",
      one.input_shape == (1, 3, 640, 640) and one.max_batch == 1)
capped = load(detector(batch_max=2), max_batch=4)
check("asked for 4, the profile allows 2 -> 2", capped.max_batch == 2)

print("\n5. no profile to read")
try:
    load(FakeEngine({"images": ((-1, 3, -1, -1), None)}, {"output0": lambda s: (1, 300, 6)}))
    check("refused with a clear error", False)
except RuntimeError as exc:
    check("refused with a clear error", "optimisation profile" in str(exc), str(exc)[:80])

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All TensorRT wrapper checks passed.")
