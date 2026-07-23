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
  ingestion/      RTSP reader (via MediaMTX localhost restream) + frame buffer
  livestream/     MediaMTX backbone (supervised child) + control client; builds
                  the public WebRTC/WHEP live links (fleet routing — see cloud/)
  recording/      Person-triggered recording (15s MPEG-TS segments) + playback index
  detection/      YOLO TRT + buffer + runner (active-camera gated) + engine wrapper
  tracking/       clean-room BoT-SORT (kalman/matching) + buffers + runner
  pose/           YOLO-Pose TRT + posture/fall classifier + runner
  reid/           OSNet TRT + FAISS hybrid gallery + target-lock manager + runner
  rules/          Fall / posture / spatial rule engine
  events/         In-process bus + enricher + SQLite writer
  alerts/         cloud_alert_publisher (HTTPS app server)
  integration/    CERAVIS app-server client (userDetails/saveCamera/alert/snapshot)
  storage/        SQLite wrapper + EventStore
  bootstrap/      Pipeline assembly (build/start/stop) — keeps main.py thin
  monitoring/     Pipeline metrics + tegrastats parser
  streaming/      WebSocket JPEG stream (built-in UI); external live view is
                  MediaMTX WebRTC (WHEP), fronted by the cloud tunnel — see cloud/
  enrollment/     Per-recipient folder mgmt
  static/         /ui dashboard, camera + zone labeling pages
  models/         detection/ pose/ reid/ — .onnx + .engine (gitignored)
  tests/          Pure-python regression + on-device cloud sanity (test_*.py)
  infra/env/jetson.env       All tunables (FPS, thresholds, paths, cloud API)
  infra/systemd/             ceravis.service template

setup/            One-time provisioning (flash to the device / pendrive):
  setup.sh              End-to-end bring-up (runs everything below)
  install_native.sh     apt + pip deps on JetPack
  install_mediamtx.sh   MediaMTX binary + self-signed TLS cert (media backbone)
  export_engines.sh     ONNX (CPU venv) -> trtexec FP16 engines
  export_reid.sh        OSNet ReID engine
  install_service.sh    systemd unit (start at boot)
  check_jetson.sh       Dependency/model doctor
  export_models.py      The exporter both export scripts drive
```
The `edge/` app is what you upgrade (git pull / docker); `setup/` is the one-time
device provisioning a technician runs on-site.

Within `edge/` the code reads by function — three domains: the **Edge_AI** vision
pipeline (ingestion → detection → tracking → pose → reid → rules → events →
alerts), **Recording** (`recording/`), and **Live-stream-share** (`livestream/`
on the edge + `cloud/` for the shared fleet tunnel + TLS). See
[`edge/README.md`](edge/README.md) and [`cloud/README.md`](cloud/README.md).

## Setup on the Jetson (JetPack 6.x flashed via SDK Manager)
```bash
git clone https://github.com/bhuvaneshvarma/ceravis-AI.git ~/ceravis
cd ~/ceravis

bash setup/setup.sh               # one command: deps + engines + service + doctor
# (or step by step:)
bash setup/install_native.sh      # apt + pip deps          (~10 min)
nano edge/data/cameras.json       # your real RTSP URL(s)
bash setup/export_engines.sh      # build TRT engines       (~20 min, one-time)
bash setup/install_service.sh     # start now + at every boot
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
