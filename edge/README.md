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
├─ LIVE-STREAM-SHARE  — remote live view (paired with cloud/)
│   livestream/   MediaMTX backbone: supervised child + control client. Owns the
│                 media backbone and builds the public WebRTC/WHEP live links.
│                 stream_path() = "<edge_id>/<cam>" is the ONE path the AI reads
│                 (local RTSP) AND the public link addresses, so the segment frp
│                 routes on and the segment MediaMTX serves are identical.
│                 (the cloud tunnel + TLS live in ../cloud/ — see cloud/README.md)
│
├─ RECORDING  — the person-triggered playback archive
│   recording/    controller.py  person on camera → MediaMTX record on/off
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
    onvif/          dependency-free WS-Discovery / SOAP / PTZ / encoder
    storage/        SQLite wrapper + EventStore
    monitoring/     pipeline metrics + tegrastats
    streaming/      WebSocket JPEG live wall (built-in UI)
    common/         net / rtsp / clock / crops / letterbox helpers
    bootstrap/      pipeline assembly (build/start/stop) — keeps main.py thin
    tools/          status + recordings CLIs
    static/         /ui pages (dashboard, cameras, zones, monitor, recordings)
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
