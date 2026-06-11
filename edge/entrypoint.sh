#!/usr/bin/env bash
# CERAVIS edge container entrypoint.
#
# 1. Build any missing TensorRT engines (Jetson-only — engines are GPU-arch-specific).
# 2. Launch FastAPI.
set -euo pipefail

echo "[ceravis] export_models.py …"
python3 /app/scripts/export_models.py || \
  echo "[ceravis] model export failed — continuing; some runners may stay disabled"

echo "[ceravis] starting uvicorn"
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
