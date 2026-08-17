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

## One stream per camera — and the one knob that resizes it

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

`CAMERA_STREAM_MAX_HEIGHT` (default `0` = never touch a camera) is therefore a
**whole-camera** setting, applied only when you ask for it:

```bash
curl -X POST http://<device>:8000/api/v1/cameras/LIVING_ROOM/stream-profile \
     -H 'Content-Type: application/json' \
     -d '{"edgeId":"<this device>","max_height":1440}'
```

It reads the camera's supported resolutions, clamps into them, writes **only**
the resolution (bitrate, frame rate and GOP are preserved), then reads the
encoder back and reports `before / requested / after / accepted`. A camera that
silently ignores or clamps the write comes back `accepted: false` rather than as
a false success. Nothing runs at discovery — probes are read-only.

Because the bitrate is held, a lower cap spends the same bits on fewer pixels:
sharper per pixel, cheaper for every decoder, smaller on disk. What it costs is
reach, and the AI feels that first.

### Viewing on the device itself

Don't. Chrome on JetPack has no hardware video decode (NVIDIA ships no VA-API),
so the Jetson's own browser software-decodes H.264 on the CPU — competing with
YOLO for the cores it needs, and stuttering at anything near native resolution.
The AI is unaffected by this: it decodes on **NVDEC** via `nvv4l2decoder`, a
different path entirely. Watch the cameras from a phone, a laptop or the cloud.
