#!/usr/bin/env bash
# =============================================================
# Build the ReID engine — OSNet (torchreid) by default
# =============================================================
# Same pattern as export_engines.sh: a disposable CPU venv produces the ONNX
# (torch is only needed here, never at runtime), then JetPack's trtexec builds
# the FP16 engine. After this, restart ceravis and ReID activates.
#
# Run once:  bash scripts/export_reid.sh
set -euo pipefail

EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(dirname "$EDGE_DIR")"
VENV="${VENV:-/tmp/ceravis-export-venv}"

cd "$EDGE_DIR"
set -a
# shellcheck disable=SC1091
. "$REPO_DIR/infra/env/jetson.env"
set +a

ONNX="${REID_ONNX_PATH:-models/reid/reid.onnx}"
MODEL="${REID_MODEL_NAME:-osnet_x1_0}"
H="${REID_INPUT_HEIGHT:-256}"; W="${REID_INPUT_WIDTH:-128}"

if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip
fi
echo "== installing CPU torch + torchreid (one-time) =="
"$VENV/bin/pip" install torch torchvision --index-url https://download.pytorch.org/whl/cpu
"$VENV/bin/pip" install torchreid

echo "== exporting $MODEL -> $ONNX =="
mkdir -p "$(dirname "$ONNX")"
REID_ONNX_PATH="$ONNX" REID_MODEL_NAME="$MODEL" REID_H="$H" REID_W="$W" \
"$VENV/bin/python" - <<'PY'
import os, torch, torchreid
name = os.environ["REID_MODEL_NAME"]; out = os.environ["REID_ONNX_PATH"]
h, w = int(os.environ["REID_H"]), int(os.environ["REID_W"])
model = torchreid.models.build_model(name, num_classes=1000, pretrained=True)
model.eval()                       # eval() makes the forward return the embedding
dummy = torch.randn(1, 3, h, w)
torch.onnx.export(
    model, dummy, out,
    input_names=["input"], output_names=["output"],
    dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
    opset_version=12,
)
print("ONNX written:", out)
PY

echo "== building TensorRT engine (trtexec, torch-free) =="
python3 scripts/export_models.py

echo
echo "ReID engine built. Restart the service:  sudo systemctl restart ceravis"
echo "Then re-enroll (or re-queue) a recipient — embeddings will generate."
