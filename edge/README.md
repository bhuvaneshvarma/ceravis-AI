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

## One stream per camera — and how its profile is chosen

MediaMTX dials each camera **once**, on its main profile, and fans that single
compressed stream out to four consumers:

| consumer | how it reads | why it wants what it wants |
|---|---|---|
| AI pipeline | loopback RTSP `127.0.0.1:8554` | resolution is *reach* — a distant person must survive being cropped for ReID and pose |
| public live links | WebRTC/WHEP through the cloud tunnel | native quality, untouched |
| `/ui` pages | the **same** WebRTC stream (`static/live-view.js`) | whatever the viewer's browser can decode |
| recorder | MediaMTX writes the packets to disk | native quality, remux only |

They share one stream, so **they share one resolution**. There is no way to give
the recorder 1440p while the AI keeps 4K without a second connection to the
camera — and a second pull on a WiFi camera takes bandwidth straight from the
first, which starves the AI reader and destabilises live view. That is the
failure this design exists to prevent, so the second pull is not coming back.

**Which profile a camera is consumed on is decided ONCE, at registration.** The
setup wizard reads every profile the camera exposes, pre-selects the one we
recommend, and stores that profile's RTSP URL and token. Nothing on the camera is
ever rewritten — we only choose among what it already offers.

`onvif.client.recommend_profile()` is that policy, as a pure function, ranked by
what actually breaks the product:

1. **H.264, as a hard requirement.** No browser decodes HEVC over WebRTC, so an
   H.265 profile is a black screen on the /ui pages, the public links and the
   cloud alike — and recordings are remuxed as-is, so its clips are unplayable
   too. A smaller H.264 profile beats a bigger unplayable one, every time.
2. **The largest resolution at or below `CAMERA_PREFERRED_HEIGHT`** (default
   1440). More pixels is more reach for ReID and pose at distance; it also costs
   camera WiFi, decode on every viewer, and disk.
3. If every H.264 profile is taller than that, the smallest of them.

The operator can override the pick in the wizard, and H.265 rows are marked
unusable rather than merely listed.

Two traps this design exists to avoid, both of which cost real weeks:

- **A stored sub-stream URL is invisible.** A 4K camera saved with its 720p
  `/stream2` URL reports running, steady fps and zero reconnects forever, while
  the AI, the recordings and every viewer quietly run on a fraction of the
  pixels. `/system/status` now measures the LIVE resolution and alarms below
  720p, so the choice is verified rather than assumed.
- **ONVIF lies about the codec.** The ver10 encoder schema has no H.265 element,
  so an HEVC camera reports `H264`. The profile list shows what the camera claims;
  `/system/status` reports the codec **observed** off the live stream, and alarms
  on anything that is not H.264. Claims are for the picker, evidence is for the
  alarm.

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
