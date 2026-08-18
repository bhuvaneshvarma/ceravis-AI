# `edge/` — architecture by function

The edge app is one FastAPI service (`main.py`) that supervises MediaMTX as a
child (one `ceravis.service`). The packages group into **three functional
domains** plus shared infrastructure. Nothing here is a mega-folder dump — each
package is a single responsibility; the domains below are how they compose.

```
edge/
│
├─ EDGE_AI  — the vision pipeline (a person walks in → an alert leaves)
│   ingestion/    RTSP reader off the MediaMTX localhost restream + frame buffer
│   detection/    YOLO TRT detect + buffer + runner (active-camera gated)
│   tracking/     clean-room BoT-SORT (Kalman + OSNet appearance) + buffers
│   pose/         YOLO-Pose TRT + posture/fall classifier + runner
│   reid/         OSNet TRT + FAISS hybrid gallery + occlusion-safe target lock
│   enrollment/   per-recipient gallery management
│   rules/        fall / posture / location rule engine
│   events/       in-process bus + enricher + SQLite writer
│   alerts/       cloud_alert_publisher (falls / no-motion → app server)
│
├─ LIVE-STREAM-SHARE  — every viewer, local or remote (paired with cloud/)
│   livestream/   MediaMTX backbone: supervised child + control client. Owns the
│                 media backbone and builds the public WebRTC/WHEP live links.
│                 stream_path() = "<edge_id>/<cam>" is the ONE path the AI reads
│                 (local RTSP), the public link addresses, the /ui pages play
│                 and the recorder writes — so the segment frp routes on and the
│                 segment MediaMTX serves are identical, and the camera is dialled
│                 exactly ONCE. This process never serves video itself.
│                 (the cloud tunnel + TLS live in ../cloud/ — see cloud/README.md)
│
├─ RECORDING  — the person-triggered playback archive
│   recording/    controller.py  person on camera → MediaMTX record on/off,
│                                 recording the camera's MAIN stream at native
│                                 quality (remux only, no second camera pull)
│                 index.py        turns the stored MPEG-TS segments into a
│                                 seekable, time-addressable HLS timeline
│                 (recording path names are deliberately SLASH-FREE — the record
│                  toggle and disk layout never depend on the live slash-path)
│
└─ shared infrastructure
    config/         settings (env-driven, infra/env/jetson.env)
    schemas/        domain models (Camera, Zone, Recipient, Event)
    configuration/  JSON-backed CRUD (cameras/zones/recipients/account)
    api/            FastAPI routers (account = cloud proxy, cameras, recordings,
                    system, discovery, network, zones, events, ai, metrics)
    integration/    CERAVIS app-server client + call log
    onvif/          dependency-free WS-Discovery / SOAP / PTZ (read-only:
                    camera encoder settings are never rewritten)
    storage/        SQLite wrapper + EventStore
    monitoring/     pipeline metrics + tegrastats
    common/         net / rtsp / clock / crops / letterbox helpers
    bootstrap/      pipeline assembly (build/start/stop) — keeps main.py thin
    tools/          status + recordings CLIs
    static/         /ui pages (dashboard, cameras, zones, monitor, recordings);
                    live-view.js plays every camera tile over MediaMTX WebRTC
```

## How live sharing routes (fleet model)

One shared domain and one shared port serve every house; frp disambiguates by
the `/<edge_id>` URL path prefix (`locations=["/<edge_id>"]`). See
[`cloud/README.md`](../cloud/README.md).

```
viewer ──HTTPS──► Caddy(:443) ──HTTP──► frps vhost(:7080) ──► frpc(edge)
                    (fleet TLS)        route by /<edge_id>      MediaMTX(:8889)
   └───────────── video P2P over UDP (WebRTC/ICE, STUN) — never via the cloud ──┘
```

Hybrid transport, the CCTV model: **signaling is TCP** (the WHEP handshake over
the frp tunnel), **video is UDP** and travels peer-to-peer. Public link:
`https://<domain>/<edge_id>/<camera>/whep`.

Set `EDGE_ID`, `DEVICE_STREAM_BASE=https://<domain>` and `MEDIAMTX_STUN_SERVER`
in `infra/env/jetson.env`, then re-sync cameras. Blank `EDGE_ID`/`DEVICE_STREAM_BASE`
= LAN-direct: links hit MediaMTX's WebRTC port on the device directly.

## One stream per camera — and the one case that needs two

MediaMTX dials each camera **once** and fans that pull out to four consumers:

| consumer | how it reads | what it needs |
|---|---|---|
| public live links | WebRTC/WHEP through the cloud tunnel | **H.264** — browsers cannot decode HEVC |
| `/ui` pages | the **same** WebRTC stream (`static/live-view.js`) | **H.264**, same reason |
| recorder | MediaMTX writes the packets to disk | **H.264** — clips are remuxed, then played in a browser |
| AI pipeline | loopback RTSP `127.0.0.1:8554` | **pixels**, any codec — it decodes on NVDEC, which handles HEVC |

Three of the four need H.264. The fourth wants the most pixels and does not care
about the codec. Usually one profile satisfies everyone and the camera stays on a
single connection.

**A camera whose biggest stream is H.265 is the exception**, and it is real: the
bench C260 offers 2560×1440 HEVC and 1280×720 H.264, nothing else. Forcing
everyone onto 720p throws away the AI's reach; forcing everyone onto 1440p is a
black screen everywhere. So that camera — and only that camera — is dialled
twice: viewers get `rtsp_url` (720p H.264), the AI additionally reads
`ai_rtsp_url` (1440p HEVC) on its own `<cam>-ai` path.

`onvif.client.recommend_streams()` decides, at registration, and the second pull
must **earn itself**: if the same profile wins both roles, or the AI candidate is
no bigger than the viewers', `ai_rtsp_url` stays empty and nothing changes. A
second stream on a WiFi camera is bandwidth taken straight from the first — that
is what destabilised this system before, so it is never opened speculatively. A
camera that later gains a usable H.264 main drops back to one connection with no
manual step.

The viewer profile is the H.264 one **nearest** `CAMERA_PREFERRED_HEIGHT`
(default 1080) — nearest, not "largest at or below". Asked for 1080p, a camera
offering 1440p and 360p must give 1440p; the at-or-below rule hands back 360p and
destroys the picture. An exact tie goes to the larger, because that is the choice
that keeps the camera on one pull.

Two traps this exists to avoid, both of which cost real weeks:

- **A stored sub-stream URL is invisible.** A 4K camera saved with its 720p
  `/stream2` URL reports running, steady fps and zero reconnects forever while
  everything downstream runs on a fraction of the pixels. `/system/status`
  measures the LIVE resolution and alarms below 720p.
- **ONVIF lies about the codec.** Its ver10 encoder schema has no H.265 element,
  so an HEVC camera reports `H264` — the C260 does exactly this, and reported
  profile `Main` on a stream ffprobe read as `High`. So every codec decision
  reads the **bitstream** (`common.rtsp.observe_stream`), never the label.

```bash
python -m tools.camera --camera LIVING_ROOM   # claim vs reality, per profile
```

```
PROFILE     RESOLUTION    ONVIF SAYS  REALLY IS   PLAYS?
  profile_1 2560x1440     H264        H265        NO      <-- the claim is WRONG
* profile_2 1280x720      H264        H264        yes
```

### Viewing on the device itself

Don't — use a phone, a laptop or the cloud. Live view in a browser **on the
Jetson** stutters badly (a picture roughly once per GOP, everything between
dropped) while the identical stream is flawless on any other machine.

What is measured, so nobody re-derives it: `chrome://gpu` on the device reports
Canvas, Compositing, Rasterization **and Video Decode** all hardware
accelerated, and HLS playback of recorded footage in that same browser is
perfect. But `chrome://webrtc-internals` shows WebRTC choosing
`decoderImplementation=FFmpeg` with `powerEfficientDecoder=false` — the software
decoder — with `packetsLost` and `nackCount` at zero, `pliCount` in the
thousands and `keyFramesDecoded` tracking `framesDecoded`. So packets all
arrive and Chrome decodes only keyframes. Chrome's media pipeline takes the
hardware decoder; its WebRTC pipeline does not. **Why is not established** —
these were also ruled out by measurement: the network, the UDP receive buffer
(`RcvbufErrors` = 0), the recorder, the AI layer, and our own JavaScript (the
fault reproduces on MediaMTX's own player page).

The AI is unaffected either way: it decodes on **NVDEC** via `nvv4l2decoder`, a
different path entirely. If you need live video on the device itself, use that
same hardware rather than a browser:

```bash
gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:8554/<edge_id>/<CAM>   protocols=tcp latency=0 ! rtph264depay ! h264parse ! nvv4l2decoder   ! nvvidconv ! autovideosink sync=false
```
