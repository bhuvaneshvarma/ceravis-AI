"""
The model pipeline (setup/export_models.py): one catalogue, two stages.

1. the catalogue names every model the app loads, where the app loads it from
   (MODELS_DIR), with the detector's dynamic-batch build shapes
2. a downloaded model is kept only when its SHA-256 matches — never a .part or
   a wrong file at the real path
3. an engine is built from its ONNX with trtexec, skipped when built or when
   the ONNX is missing, and retried without batch shapes for a fixed-batch ONNX
3b. engines carry a fingerprint: another release's engine is reused only when
   ONNX, TensorRT and board are the same; a TensorRT change rebuilds it
4. the leftover .pt weights are removed once the ONNX exist — and nothing else

No torch, no TensorRT: trtexec is a stand-in. Runs anywhere.

Run:  PYTHONPATH=edge python tests/test_export_models.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = Path(tempfile.mkdtemp(prefix="ceravis-models-"))
os.environ["MODELS_DIR"] = str(MODELS)
sys.path.insert(0, str(ROOT / "edge"))
sys.path.insert(0, str(ROOT / "setup"))

import export_models as em                                  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print("\n1. the catalogue")
cat = {m.name: m for m in em.catalog()}
check("every model the app loads",
      set(cat) == {"detection", "pose", "reid", "face detector", "face recognizer"}, str(set(cat)))
check("all under MODELS_DIR", all(str(m.onnx).startswith(str(MODELS)) for m in cat.values()))
det = cat["detection"]
check("detection: dynamic batch up to DETECTION_MAX_BATCH",
      det.dynamic and det.shapes == ["--minShapes=images:1x3x640x640",
                                     "--optShapes=images:4x3x640x640",
                                     "--maxShapes=images:4x3x640x640"], str(det.shapes))
check("pose: one frame, no shapes", not cat["pose"].dynamic and cat["pose"].shapes is None)
check("ReID: torchreid OSNet, batch 1..8",
      isinstance(cat["reid"], em.ReidModel) and cat["reid"].arch == "osnet_x1_0"
      and "--maxShapes=input:8x3x256x128" in cat["reid"].shapes)
check("YuNet has no engine (OpenCV runs it), AuraFace does",
      cat["face detector"].engine is None and cat["face recognizer"].engine is not None)

print("\n2. a downloaded model")
src = MODELS / "upstream.onnx"
src.write_bytes(b"model-bytes")
good = hashlib.sha256(b"model-bytes").hexdigest()
m = em.DownloadedModel("t", MODELS / "dl" / "good.onnx", src.as_uri(), good)
check("matching SHA-256 -> kept", m.ensure_onnx() and m.onnx.read_bytes() == b"model-bytes")
bad = em.DownloadedModel("t", MODELS / "dl" / "bad.onnx", src.as_uri(), "0" * 64)
check("wrong SHA-256 -> refused", not bad.ensure_onnx() and not bad.onnx.exists())
check("…and no .part left behind", not list((MODELS / "dl").glob("*.part")))
gone = em.DownloadedModel("t", MODELS / "dl" / "gone.onnx", (MODELS / "nope").as_uri(), good)
check("a failed download -> refused, nothing left", not gone.ensure_onnx()
      and not list((MODELS / "dl").glob("gone*")))
check("present -> not fetched again", m.ensure_onnx())

print("\n3. engine builds")
calls: list[list[str]] = []
fixed_batch = {"on": False}


def fake_run(cmd, *a, **k):
    calls.append(cmd)
    if fixed_batch["on"] and any("Shapes=" in c for c in cmd):
        return subprocess.CompletedProcess(cmd, 1)
    out = next(c.split("=", 1)[1] for c in cmd if c.startswith("--saveEngine="))
    Path(out).write_bytes(b"engine")
    return subprocess.CompletedProcess(cmd, 0)


real_run, real_trtexec = em.subprocess.run, em._trtexec
em.subprocess.run, em._trtexec = fake_run, (lambda: "trtexec")
try:
    det.onnx.parent.mkdir(parents=True, exist_ok=True)
    det.onnx.write_bytes(b"onnx")
    det.build_engine()
    check("built with FP16 and the batch shapes",
          det.engine.exists() and "--fp16" in calls[-1]
          and "--maxShapes=images:4x3x640x640" in calls[-1], " ".join(calls[-1][1:4]))
    n = len(calls)
    det.build_engine()
    check("already built -> not built again", len(calls) == n)
    cat["pose"].build_engine()
    check("no ONNX -> skipped, no trtexec run", len(calls) == n and not cat["pose"].engine.exists())
    det.engine.unlink()
    fixed_batch["on"] = True
    det.build_engine()
    check("a fixed-batch ONNX: retried without shapes",
          det.engine.exists() and not any("Shapes=" in c for c in calls[-1]))
    fixed_batch["on"] = False

    print("\n3b. fingerprints: reuse only what is provably the same")
    trt = {"v": "10.3.0"}
    real_ver, real_board = em._tensorrt_version, em._board
    em._tensorrt_version, em._board = (lambda: trt["v"]), (lambda: "nvidia,p3768-0000+p3767-0005")
    try:
        det.engine.unlink()
        det.build_engine()
        fp = json.loads(em._fingerprint_path(det.engine).read_text())
        check("a build records its fingerprint",
              fp["tensorrt"] == "10.3.0" and fp["onnx_sha256"] == hashlib.sha256(b"onnx").hexdigest())
        # the next release on this device: same ONNX bytes, the running models as the source
        old_models, new_models = MODELS, Path(tempfile.mkdtemp(prefix="ceravis-newrel-"))
        os.environ["MODELS_DIR"] = str(new_models)
        new = {m.name: m for m in em.catalog()}["detection"]
        new.onnx.parent.mkdir(parents=True, exist_ok=True)
        new.onnx.write_bytes(b"onnx")
        n = len(calls)
        new.build_engine(old_models)
        check("same ONNX, TensorRT, board -> reused, not rebuilt",
              new.engine.exists() and len(calls) == n)
        new.engine.unlink()
        new.onnx.write_bytes(b"onnx-v2")                        # a changed model
        new.build_engine(old_models)
        check("a changed ONNX -> built, the old engine NOT taken", len(calls) == n + 1)
        trt["v"] = "10.7.0"                                    # JetPack upgraded TensorRT
        n = len(calls)
        new.build_engine()
        check("TensorRT changed -> the stale engine is rebuilt", len(calls) == n + 1)
        n = len(calls)
        new.build_engine()
        check("…and once rebuilt it is kept (no rebuild at every start)", len(calls) == n)
        legacy = new.engine.with_name("legacy.engine")
        legacy.write_bytes(b"e")
        m = em.Model("legacy", new.onnx, legacy)
        n = len(calls)
        m.build_engine()
        check("an engine from before fingerprints is trusted as built", len(calls) == n)
        os.environ["MODELS_DIR"] = str(old_models)
    finally:
        em._tensorrt_version, em._board = real_ver, real_board
finally:
    em.subprocess.run, em._trtexec = real_run, real_trtexec

print("\n3c. the service's start-up check is quick")


def no_trt():
    raise AssertionError("TensorRT imported on the quick path")


real_ver = em._tensorrt_version
em._tensorrt_version = no_trt
try:
    for m in em.catalog():
        if m.engine is not None:
            m.engine.parent.mkdir(parents=True, exist_ok=True)
            m.engine.write_bytes(b"e")
    check("every engine there -> done at once, TensorRT never loaded",
          em.main(["--quick"]) == 0)
finally:
    em._tensorrt_version = real_ver

print("\n4. leftover weights")
edge = Path(tempfile.mkdtemp(prefix="ceravis-edge-"))
for name in ("yolo26m.pt", "yolo26m-pose.pt", "keep.pt", "main.py"):
    (edge / name).write_bytes(b"x")
em.remove_leftover_weights(edge)
left = sorted(p.name for p in edge.iterdir())
check("the export's .pt removed, everything else kept", left == ["keep.pt", "main.py"], str(left))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All model pipeline checks passed.")
