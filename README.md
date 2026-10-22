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
  config/         Settings (env-driven; every path anchored in one place) and the
                  JSON-backed stores (cameras / zones / account)
  schemas/        Domain models (Camera, Zone, Recipient, Event)
  api/            FastAPI routers; auth.py = the sign-in gate on every /api call,
                  setup_routes.py = the first-run readiness screen
  common/         Shared helpers: device_identity (the device's name), hotspot,
                  atomic file writes, clock, crops, zones
  ingestion/      RTSP reader (via MediaMTX localhost restream) + frame buffer
  livestream/     MediaMTX backbone (supervised child) + control client. THE one
                  connection to each camera, fanned out to the AI, every viewer
                  (WebRTC/WHEP — /ui pages, cloud tunnel) and the recorder; this
                  process never serves video itself (fleet routing — see cloud/)
  recording/      Person-triggered recording of the camera's native main stream
                  (5s MPEG-TS segments, remux only) + playback index
  detection/      YOLO TRT (batched across cameras) + buffer + runner + engine wrapper
  tracking/       clean-room BoT-SORT (kalman/matching) + buffers + runner
  pose/           YOLO-Pose TRT + posture/fall classifier + runner
  reid/           OSNet TRT + FAISS hybrid gallery + target-lock manager + runner
  rules/          Fall / posture / spatial rule engine
  events/         In-process bus + enricher + SQLite writer
  alerts/         cloud_alert_publisher (HTTPS app server)
  integration/    CERAVIS app-server client (userDetails/saveCamera/alert/snapshot)
  storage/        SQLite wrapper + EventStore + cloud outbox
  bootstrap/      Pipeline assembly (build/start/stop) — keeps main.py thin
  monitoring/     Pipeline metrics + board health (temperature, power, over-current)
  enrollment/     Per-recipient folder mgmt
  static/         /ui pages (welcome = first-run status, login, live, cameras, zones)
  fleet/          The fleet agent, vendored from ceravis-FMS (do not edit here)
  VERSION         The release version — the one place it is written
  infra/env/jetson.env       Every tunable, documented; only overrides are active
  infra/systemd/             Unit templates (installed by setup/lib.sh)

setup/
  install.sh        PRODUCTION: the one command (sudo bash install.sh)
  update.sh         OTA update with health check + automatic rollback
  firstboot.sh      This device's own identity (name, hotspot, keys) — once
  seal.sh           Clean a master before copying its disk (golden image)
  tune_os.sh        No desktop, power mode, fan, held NVIDIA packages, logs
  make_release.sh   Build the release bundle (and the USB stick) on the PC
  lib.sh            The layout + the one way to install units / sudoers
  install_*.sh, export_*.sh, check_jetson.sh   the steps both use
cloud/            The frp tunnel client
```

## Install a device (production)

The device needs JetPack 6 (SDK Manager) and an internet connection.

```bash
bash setup/make_release.sh --usb E:/      # on the PC: build the bundle + USB stick
sudo bash /media/$USER/<stick>/CERAVIS/install-from-usb.sh   # on the device
```

or, with the bundle already on the device: `sudo bash install.sh`. Then open
`http://<device-ip>:8000` — the first screen shows every readiness check and the
camera hotspot; when it says READY, sign in with the care account's email.

## Use
```
First run:   http://<device-ip>:8000            (readiness, then sign in)
Live wall:   http://<device-ip>:8000/ui/live.html
Through the hotspot:  http://10.42.0.1:8000
Health:      http://<device-ip>:8000/health
Doctor:      sudo bash /opt/ceravis/current/setup/check_jetson.sh
Logs:        journalctl -u ceravis -f
```

## Update
```bash
sudo bash /opt/ceravis/current/setup/update.sh ceravis-edge-<version>.tar.gz
sudo bash /opt/ceravis/current/setup/update.sh --rollback      # back one version
```
This is the PRODUCTION branch (prod_ceravis1.1). Development, tests, the test
pages and the cloud-server files live on dev_ceravis1.1.
