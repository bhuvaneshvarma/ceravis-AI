# CERAVIS

Production-grade multi-camera AI surveillance for in-home elderly care.
Runs natively on an NVIDIA Jetson Orin Nano Super (edge AI), AWS only
handles device communication / media / alert fan-out — no video leaves
the home.

## Design principle

Use the JetPack stack the device already has — CUDA, TensorRT (+ trtexec),
GStreamer-enabled OpenCV, numpy — and add only small pure-Python deps via pip.
No Docker, no duplicate CUDA/TensorRT downloads, no torch at runtime:
inference is pure TensorRT; torch (CPU-only) is used once, in a disposable
venv, just to export YOLO weights to ONNX.

## Stack

FastAPI · TensorRT FP16 (YOLO detect + pose, OSNet ReID) · clean-room BoT-SORT
(Kalman + appearance, torch-free) · FAISS · OpenCV+GStreamer (hardware decode
w/ software fallback) · SQLite (offline buffer) · HTTPS → CERAVIS app server
(userDetails / saveCamera / saveAlert / saveSnapshot) · systemd.

## Repo layout
```
edge/
  config/         Settings (env-driven)
  schemas/        Domain models (Camera, Zone, Recipient, Event)
  configuration/  JSON-backed CRUD (cameras/zones/recipients/account)
  api/            FastAPI routers (incl. account = cloud proxy)
  ingestion/      RTSP reader + frame buffer
  detection/      YOLO TRT + buffer + runner (active-camera gated) + engine wrapper
  tracking/       clean-room BoT-SORT (kalman/matching) + buffers + runner
  pose/           YOLO-Pose TRT + posture/fall classifier + runner
  reid/           OSNet TRT + FAISS hybrid gallery + target-lock manager + runner
  rules/          Fall / posture / spatial / visit-session rule engine
  events/         In-process bus + enricher + SQLite writer
  alerts/         broadcaster (WS) + cloud_alert_publisher (HTTPS app server)
  integration/    CERAVIS app-server client (userDetails/saveCamera/alert/snapshot)
  storage/        SQLite wrapper + EventStore
  monitoring/     Pipeline metrics + tegrastats parser
  streaming/      WebSocket + MJPEG live stream
  enrollment/     Per-recipient folder mgmt
  static/         /ui dashboard, camera + zone labeling pages
  models/         detection/ pose/ reid/ — .onnx + .engine (gitignored)
  scripts/
    install_native.sh   One-time: apt + pip deps on JetPack
    export_engines.sh   One-time: ONNX (CPU venv) -> trtexec FP16 engines
    install_service.sh  One-time: systemd unit (start at boot)
    export_models.py    The exporter both scripts drive

infra/env/jetson.env       All tunables (FPS, thresholds, paths, cloud API)
infra/systemd/             ceravis.service template
```

## Setup on the Jetson (JetPack 6.x flashed via SDK Manager)
```bash
git clone https://github.com/bhuvaneshvarma/ceravis-AI.git ~/ceravis
cd ~/ceravis/edge

bash scripts/install_native.sh    # apt + pip deps          (~10 min)
nano data/cameras.json            # your real RTSP URL(s)
bash scripts/export_engines.sh    # build TRT engines       (~20 min, one-time)
bash scripts/install_service.sh   # start now + at every boot
```

## Use
```
Dashboard:   http://<jetson-ip>:8000/ui/dashboard.html
Cameras:     http://<jetson-ip>:8000/ui/cameras.html   (add/label/preview)
Zones:       http://<jetson-ip>:8000/ui/zones.html     (draw zones)
Health:      http://<jetson-ip>:8000/health
Metrics:     http://<jetson-ip>:8000/api/v1/metrics
API docs:    http://<jetson-ip>:8000/docs
Logs:        journalctl -u ceravis -f
```

## Update workflow
```bash
cd ~/ceravis && git pull && sudo systemctl restart ceravis
```
