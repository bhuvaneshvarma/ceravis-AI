#!/usr/bin/env python3
"""
The AI models, in two stages — both idempotent (finished work is skipped):

  1. ONNX  (--onnx-only)  made ONCE, on any machine with the tools: the PC
     builds them for the release bundle (setup/make_release.sh), so a device
     needs neither internet nor torch. YOLO26 detection + pose are exported
     with ultralytics, the ReID body model with torchreid (CPU torch, in a
     throwaway environment — inference never uses torch), and the face models
     are downloaded, pinned to an exact upstream revision and checked by
     SHA-256. The .pt weights the export downloads are deleted afterwards.

  2. engines (default)    ONNX -> FP16 TensorRT engine with JetPack's own
     trtexec (/usr/src/tensorrt/bin/trtexec, no Python deps). Engines are
     specific to the GPU and the TensorRT that built them, so they are built
     on the device itself and never copied from another one. Each engine gets
     a fingerprint (<engine>.json: its ONNX's SHA-256, the TensorRT version,
     the board, the build shapes):
       * --reuse-from DIR (update.sh: the running release's models) takes an
         engine from DIR only when its fingerprint matches exactly — the same
         ONNX on the same TensorRT on this same board — else it is built;
       * an engine whose fingerprint no longer matches this device (TensorRT
         changed with a JetPack update) is rebuilt, never loaded stale.

Where each file goes comes from the app's own settings (MODELS_DIR included),
so the engines land exactly where the app loads them from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _env(key: str, default: str) -> str:
    v = os.environ.get(key, "").strip()
    return v or default


def _settings():
    """The app's own settings. None inside a bare export venv without
    pydantic-settings: the environment variables are used then."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
    try:
        from config.settings import settings
        return settings
    except Exception:
        return None


def _cfg(name: str, default: str) -> str:
    s = _settings()
    return str(getattr(s, name)) if s is not None else _env(name.upper(), default)


def _models_dir() -> Path:
    return Path(_cfg("models_dir", str(Path(__file__).resolve().parents[1] / "edge" / "models")))


def _have(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _trtexec() -> str | None:
    found = shutil.which("trtexec")
    if found:
        return found
    jetpack_path = Path("/usr/src/tensorrt/bin/trtexec")
    return str(jetpack_path) if jetpack_path.exists() else None


# ONNX opset for the exports: 17 is read by every TensorRT the device may carry
# (JetPack 6: TensorRT 10.3) and holds every operator these models use.
ONNX_OPSET = 17


def _tensorrt_version() -> str | None:
    try:
        import tensorrt
        return str(tensorrt.__version__)
    except Exception:
        return None


def _board() -> str:
    try:
        return Path("/proc/device-tree/compatible").read_bytes().replace(b"\0", b" ").decode().strip()
    except OSError:
        return ""


def _fingerprint_path(engine: Path) -> Path:
    return engine.with_name(engine.name + ".json")


class Model:
    """One AI model: its ONNX file (stage 1) and, when it runs on TensorRT, the
    engine built from it on the device (stage 2). Subclasses say how the ONNX
    is made; the engine build is the same for all."""

    def __init__(self, name: str, onnx: Path, engine: Path | None = None,
                 shapes: list[str] | None = None) -> None:
        self.name, self.onnx, self.engine, self.shapes = name, onnx, engine, shapes

    # ---- stage 1 ----------------------------------------------------
    def ensure_onnx(self) -> bool:
        """True when the ONNX is there (made now, or already)."""
        if _have(self.onnx):
            return True
        self.onnx.parent.mkdir(parents=True, exist_ok=True)
        return self._make_onnx()

    def _make_onnx(self) -> bool:
        raise NotImplementedError

    # ---- stage 2 ----------------------------------------------------
    # What makes two engines the same; the ONNX's size and time only spare
    # hashing it again at every start.
    IDENTITY = ("onnx_sha256", "tensorrt", "board", "shapes")

    def fingerprint(self, known: dict | None = None) -> dict | None:
        """What an engine built here, now, from this ONNX would be; None when it
        cannot be known (no TensorRT here, no ONNX)."""
        trt = _tensorrt_version()
        if trt is None or not _have(self.onnx):
            return None
        st = self.onnx.stat()
        unchanged = bool(known) and (known.get("onnx_size"), known.get("onnx_mtime_ns")) \
            == (st.st_size, st.st_mtime_ns)
        return {"onnx_sha256": known["onnx_sha256"] if unchanged else _sha256(self.onnx),
                "onnx_size": st.st_size, "onnx_mtime_ns": st.st_mtime_ns,
                "tensorrt": trt, "board": _board(), "shapes": self.shapes or []}

    @classmethod
    def _same(cls, a: dict | None, b: dict | None) -> bool:
        return bool(a) and bool(b) and all(a.get(k) == b.get(k) for k in cls.IDENTITY)

    @staticmethod
    def _recorded(engine: Path) -> dict | None:
        try:
            return json.loads(_fingerprint_path(engine).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def build_engine(self, reuse_from: Path | None = None) -> None:
        """ONNX -> FP16 TensorRT engine via JetPack's trtexec — or, with
        reuse_from, that folder's engine when its fingerprint matches exactly."""
        if self.engine is None:
            return
        if not _have(self.onnx):
            print(f"[skip] {self.engine.name}: missing {self.onnx}")
            return
        have = self._recorded(self.engine) if _have(self.engine) else None
        want = self.fingerprint(have)
        if _have(self.engine):
            # An engine from before fingerprints (none recorded) is trusted as built.
            if want is None or have is None or self._same(have, want):
                print(f"[skip] {self.engine.name} already built")
                return
            print(f"[stale] {self.engine.name} was built for TensorRT {have.get('tensorrt')} "
                  "or another ONNX — building it again")
            self.engine.unlink()
        if reuse_from is not None and want is not None and self._reuse(reuse_from, want):
            return
        self._build()
        if want is not None and _have(self.engine):
            _fingerprint_path(self.engine).write_text(json.dumps(want, indent=2),
                                                      encoding="utf-8")

    def _reuse(self, folder: Path, want: dict) -> bool:
        """Take the same engine from another release on THIS device — only when
        its fingerprint proves it identical; then it is copied, never moved."""
        try:
            old = folder / self.engine.relative_to(_models_dir())
        except ValueError:
            return False
        if not _have(old) or not self._same(self._recorded(old), want):
            return False
        self.engine.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, self.engine)
        _fingerprint_path(self.engine).write_text(json.dumps(want, indent=2), encoding="utf-8")
        print(f"[reuse] {self.engine.name}: identical ONNX, TensorRT and board")
        return True

    def _build(self) -> None:
        trtexec = _trtexec()
        if trtexec is None:
            print("[error] trtexec not found — is the JetPack TensorRT "
                  "component installed? (expected /usr/src/tensorrt/bin/trtexec)")
            return
        self.engine.parent.mkdir(parents=True, exist_ok=True)
        cmd = [trtexec, f"--onnx={self.onnx}", "--fp16", f"--saveEngine={self.engine}",
               "--memPoolSize=workspace:2048"] + (self.shapes or [])
        print(f"[trtexec] {self.onnx.name} -> {self.engine.name}  (FP16, ~3-8 min)")
        rc = subprocess.run(cmd).returncode
        if rc != 0 and self.shapes:
            # An ONNX exported with a fixed batch rejects explicit shapes: build
            # it as the one-frame engine it is.
            print("[trtexec] retrying without batch shapes (fixed-batch ONNX)")
            rc = subprocess.run([a for a in cmd if a not in self.shapes]).returncode
        if rc == 0 and _have(self.engine):
            print(f"[ok] {self.engine}")
        else:
            print(f"[error] trtexec failed (rc={rc}) for {self.engine.name}")


class YoloModel(Model):
    """YOLO26 detection / pose, exported with ultralytics. dynamic=True gives
    the ONNX a dynamic batch, so its engine detects on several cameras' frames
    in one call (detection/detection_runner.py)."""

    def __init__(self, name: str, weights: str, engine: Path, imgsz: int,
                 batch: int = 1) -> None:
        shapes = None
        if batch > 1:
            # Ultralytics names the input "images"; batch 1..N at the fixed size.
            s = f"3x{imgsz}x{imgsz}"
            shapes = [f"--minShapes=images:1x{s}", f"--optShapes=images:{batch}x{s}",
                      f"--maxShapes=images:{batch}x{s}"]
        super().__init__(name, engine.with_suffix(".onnx"), engine, shapes)
        self.weights, self.imgsz, self.dynamic = weights, imgsz, batch > 1

    def _make_onnx(self) -> bool:
        try:
            from ultralytics import YOLO
        except ImportError:
            print(f"[skip] {self.onnx.name}: ultralytics not installed — the release "
                  "bundle carries it (setup/make_release.sh), or run "
                  "setup/export_engines.sh once")
            return False
        print(f"[onnx] {self.weights} -> {self.onnx}  (imgsz={self.imgsz}, cpu, "
              f"{'dynamic batch' if self.dynamic else 'batch 1'}, opset {ONNX_OPSET})")
        # Downloaded into a scratch folder, so the .pt never lingers anywhere.
        # YOLO26 exports its end-to-end (NMS-free) head by default — output is
        # final detections [x1,y1,x2,y2,conf,cls], which is exactly what
        # yolo_detector.py / yolo_pose.py parse.
        here = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="ceravis-onnx-") as tmp:
            os.chdir(tmp)
            try:
                model = YOLO(self.weights)
                try:
                    out = model.export(format="onnx", imgsz=self.imgsz, device="cpu",
                                       dynamic=self.dynamic, opset=ONNX_OPSET)
                except Exception as exc:
                    if not self.dynamic:
                        raise
                    print(f"[onnx] dynamic export failed ({exc}) — exporting batch 1")
                    out = model.export(format="onnx", imgsz=self.imgsz, device="cpu",
                                       opset=ONNX_OPSET)
                shutil.move(out, self.onnx)
            finally:
                os.chdir(here)
        print(f"[ok] {self.onnx}")
        return True


class ReidModel(Model):
    """The body-appearance (ReID) model: torchreid's OSNet, the very model the
    app's ReID thresholds were measured with (config/settings.py). A dynamic
    batch, so one engine serves one crop or several."""

    def __init__(self, arch: str, onnx: Path, engine: Path, h: int, w: int) -> None:
        super().__init__("reid", onnx, engine, [
            f"--minShapes=input:1x3x{h}x{w}", f"--optShapes=input:1x3x{h}x{w}",
            f"--maxShapes=input:8x3x{h}x{w}"])
        self.arch, self.h, self.w = arch, h, w

    def _make_onnx(self) -> bool:
        try:
            import torch
            import torchreid
        except ImportError:
            print(f"[skip] {self.onnx.name}: torch/torchreid not installed — the release "
                  "bundle carries it (setup/make_release.sh), or run "
                  "setup/export_engines.sh once (ReID stays off until then)")
            return False
        print(f"[onnx] {self.arch} -> {self.onnx}  ({self.h}x{self.w}, dynamic batch)")
        model = torchreid.models.build_model(self.arch, num_classes=1000, pretrained=True)
        model.eval()                       # eval() makes the forward return the embedding
        part = self.onnx.with_name(self.onnx.name + ".part")
        # The classic (TorchScript) exporter, as the model was always exported;
        # torch 2.9+ would otherwise pick the new dynamo one.
        legacy = {"dynamo": False} if tuple(
            int(x) for x in torch.__version__.split(".")[:2]) >= (2, 4) else {}
        torch.onnx.export(
            model, torch.randn(1, 3, self.h, self.w), str(part),
            input_names=["input"], output_names=["output"],
            dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
            opset_version=12, **legacy)
        os.replace(part, self.onnx)
        print(f"[ok] {self.onnx}")
        return True


class DownloadedModel(Model):
    """A model published as ONNX: fetched once from an exact upstream revision
    and kept only if its SHA-256 matches. Written to a .part file first, so an
    interrupted or wrong download never sits at the real path; a timeout so it
    can never stall a start."""

    def __init__(self, name: str, onnx: Path, url: str, sha256: str,
                 engine: Path | None = None) -> None:
        super().__init__(name, onnx, engine)
        self.url, self.sha256 = url, sha256.lower()

    def _make_onnx(self) -> bool:
        if not self.url:
            print(f"[skip] {self.onnx.name}: no download address set")
            return False
        from urllib.request import urlopen
        part = self.onnx.with_name(self.onnx.name + ".part")
        print(f"[download] {self.url}")
        try:
            with urlopen(self.url, timeout=60) as resp, open(part, "wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
        except Exception as exc:
            part.unlink(missing_ok=True)
            print(f"[error] {self.onnx.name} download failed ({exc}) — {self.name} stays off")
            return False
        if self.sha256 and _sha256(part) != self.sha256:
            part.unlink(missing_ok=True)
            print(f"[error] {self.onnx.name} does not match its SHA-256 — discarded")
            return False
        os.replace(part, self.onnx)
        print(f"[ok] {self.onnx}")
        return True


def catalog() -> list[Model]:
    """Every model the app uses, from its settings — the one list both stages,
    the installer and the release bundle work from."""
    s = _settings()
    det_engine = Path(_cfg("detection_model_path", "models/detection/yolo26m.engine"))
    pose_engine = Path(_cfg("pose_model_path", "models/pose/yolo26m-pose.engine"))
    reid_engine = Path(_cfg("reid_model_path", "models/reid/reid.engine"))
    reid_onnx = Path(_cfg("reid_onnx_path", str(reid_engine.with_suffix(".onnx"))))
    h, w = int(_cfg("reid_input_height", "256")), int(_cfg("reid_input_width", "128"))
    reid_url = _cfg("reid_onnx_url", "")
    models: list[Model] = [
        YoloModel("detection", _cfg("detection_weights", "yolo26m.pt"), det_engine,
                  imgsz=int(_cfg("detection_input_size", "640")),
                  batch=max(1, int(_cfg("detection_max_batch", "4")))),
        YoloModel("pose", _cfg("pose_weights", "yolo26m-pose.pt"), pose_engine,
                  imgsz=int(_cfg("pose_input_size", "640"))),
        # A prebuilt ReID ONNX at REID_ONNX_URL replaces the torchreid export.
        (DownloadedModel("reid", reid_onnx, reid_url, "", reid_engine) if reid_url
         else ReidModel(_cfg("reid_model_name", "osnet_x1_0"), reid_onnx, reid_engine, h, w)),
    ]
    if s is not None and s.face_enabled:
        # YuNet runs on OpenCV (CPU); AuraFace on TensorRT. The addresses and
        # hashes live in edge/config/settings.py, the one place they are defined.
        models += [
            DownloadedModel("face detector", Path(s.face_detector_path),
                            s.face_detector_url, s.face_detector_sha256),
            DownloadedModel("face recognizer", Path(s.face_recognizer_onnx_path),
                            s.face_recognizer_url, s.face_recognizer_sha256,
                            Path(s.face_recognizer_path)),
        ]
    return models


def remove_leftover_weights(edge: Path | None = None) -> None:
    """The .pt weights are only ever an input to the ONNX export; once every
    ONNX exists nothing reads them. Older exports left them in edge/."""
    edge = edge or Path(__file__).resolve().parents[1] / "edge"
    for name in {_cfg("detection_weights", "yolo26m.pt"), _cfg("pose_weights", "yolo26m-pose.pt")}:
        stale = edge / Path(name).name
        if stale.suffix == ".pt" and stale.is_file():
            stale.unlink()
            print(f"[clean] removed {stale} (only the ONNX export needed it)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    # Stage 1 alone (needs the export tools). The engine builds then happen in a
    # separate, torch-free process — keeping ~2 GB of RAM free for trtexec,
    # which otherwise can fail CUDA init on the shared-memory Orin Nano ("no
    # CUDA-capable device is detected").
    ap.add_argument("--onnx-only", action="store_true", help="stage 1: the ONNX models only")
    ap.add_argument("--reuse-from", type=Path, metavar="DIR",
                    help="take an engine from DIR only when its fingerprint matches exactly")
    ap.add_argument("--list", action="store_true",
                    help="print each model's ONNX path under MODELS_DIR (missing ones marked)")
    # The service's start-up self-heal: when every engine is there, nothing else
    # is checked (no TensorRT import — seconds off every start). Install and
    # update run the full check, fingerprints included.
    ap.add_argument("--quick", action="store_true",
                    help="exit at once when every engine exists")
    args = ap.parse_args(argv)
    onnx_only = args.onnx_only
    models = catalog()
    if args.list:
        for m in models:
            rel = m.onnx.relative_to(_models_dir()).as_posix()
            print(rel if _have(m.onnx) else f"missing {rel}")
        return 0
    if args.quick and all(_have(m.engine) for m in models if m.engine is not None):
        return 0
    ready = [m.ensure_onnx() for m in models]
    if all(ready):
        remove_leftover_weights()
    if onnx_only:
        missing = [m.name for m, ok in zip(models, ready) if not ok]
        print(f"[done] ONNX pass complete — {'missing: ' + ', '.join(missing) if missing else 'all present'}"
              f" in {_models_dir()}")
        return 1 if missing else 0
    for m in models:
        m.build_engine(args.reuse_from)
    return 0


if __name__ == "__main__":
    sys.exit(main())
