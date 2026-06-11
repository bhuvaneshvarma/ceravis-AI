# CERAVIS

Production-grade multi-camera AI surveillance for in-home elderly care.
Runs locally on an NVIDIA Jetson Orin Nano Super (edge AI), AWS only
handles device communication / media / alert fan-out — no video leaves
the home.

## Stack
FastAPI · TensorRT (YOLOv8n + Pose + OSNet ReID) · ByteTrack (supervision)
· FAISS · OpenCV+GStreamer (hardware decode) · SQLite (offline buffer)
· MQTT → AWS IoT Core · Docker.

## Repo layout
```
edge/
  config/         Settings (env-driven)
  schemas/        Domain models (Camera, Zone, Recipient, Event, Alert)
  configuration/  JSON-backed CRUD (cameras/zones/recipients)
  api/            FastAPI routers
  ingestion/      RTSP reader + frame buffer
  detection/      YOLO TRT + buffer + runner + TRT engine wrapper
  tracking/       ByteTrack adapter + buffer + runner
  pose/           YOLO-Pose TRT + buffer + runner
  reid/           OSNet TRT + FAISS gallery + runner
  rules/          Fall / inactivity / visitor rule engine
  events/         In-process bus + SQLite writer
  alerts/         MQTT publisher to AWS IoT Core
  storage/        SQLite wrapper + EventStore
  workers/        FrameScheduler (multi-rate fan-out)
  monitoring/     Pipeline metrics + tegrastats parser
  streaming/      WebSocket JPEG stream
  enrollment/     Per-recipient folder mgmt
  static/         /ui dashboard
  scripts/        export_models.py (TRT engine builder)
  entrypoint.sh   Exports models then launches uvicorn

infra/env/        edge.env (dev), jetson.env (prod)
models/           detection/ pose/ reid/ — .engine files live here
```

## Run on the Jetson
```bash
docker compose -f docker-compose.jetson.yml up -d --build
# First boot: TRT engines build in ~5 min and persist in ./models/
# Dashboard:   http://<jetson-ip>:8000/ui/dashboard.html
# Health:      http://<jetson-ip>:8000/health
# Metrics:     http://<jetson-ip>:8000/api/v1/metrics
# API docs:    http://<jetson-ip>:8000/docs
```

## Run on a laptop (no AI, ingestion + API only)
```bash
docker compose -f docker-compose.dev.yml up --build
```
