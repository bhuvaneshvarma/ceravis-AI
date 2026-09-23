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
├─ TALK-BACK  — the only audio that goes the OTHER way
│   talkback/     the camera's own local speaker endpoint (TP-Link port 8800):
│                 mpegts.py       G.711 A-law + the private stream_type 0x90 TS
│                 protocol.py     Digest handshake + one talk session, asyncio
│                 credentials.py  the home's password HASHES (never the password)
│                 lines.py        the edge's own talk LINE to every camera, kept
│                                 open and re-opened; its record is /talkback/health
│                 sessions.py     the FLOOR: who may speak into which camera now
│                 audit.py        the talk log: who spoke, where, when, how long
│                 guard.py        stops our retries locking a camera out
│                 A line asks for the TALK session only (never the preview one),
│                 so the one-pull-per-camera rule holds and ingestion, recording
│                 and live view are untouched.
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
    tools/          status + recordings + talkback CLIs
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

## Talk-back — speaking into the room

Off by default. It is a speaker in someone's home, so the device stays silent
until it is deliberately commissioned:

```bash
# edge/infra/env/jetson.env — the ONE env file, hand-edited, tracked in git
TALKBACK_ENABLED=true
```

Then once for the whole home (Setup -> Cameras -> Talk-back does the same):

```bash
cd edge
python3 -m tools.talkback set                     # prompts, stores HASHES only
python3 -m tools.talkback test                    # every camera, silently
python3 -m tools.talkback lines                   # the line record
python3 -m tools.talkback log                     # who spoke, into which room, how long
python3 -m tools.talkback duplex --camera KITCHEN # is the room audible WHILE talking
python3 -m tools.talkback tone --camera KITCHEN   # a beep, if you want a noise
```

The password is the **TP-Link account password** for the Tapo app the camera is
paired to — *not* the camera's RTSP/ONVIF credentials, and not the edge_id. It
is hashed on the way in (`data/talkback.json`, 0600, gitignored) and the
plaintext is never stored, logged or returned by any endpoint.

Carers use the **live wall**. Each tile carries two controls:

* **Listen** unmutes the camera's own microphone. It is already in the WHEP
  stream the tile plays (the live wall is the only page that negotiates audio —
  every other page keeps its video-only SDP), so listening costs no connection,
  no protocol and no credential. Every room is its OWN toggle: a carer can
  listen to as many rooms as they like at once, and to any of them while
  talking. Each button reads the live audio path rather than a remembered flag,
  and its ring shows that room's level — so "which room was that?" is answered
  by the tile the ring moved on.
* **Hold to talk** speaks through the camera for as long as the button is held
  — there is no time limit. Each camera has its own socket
  (`/api/v1/talkback/{camera_id}/stream`); connecting claims that camera's
  FLOOR, and the edge keeps its own talk LINE to every camera open
  (`TALKBACK_LINE_ALWAYS`), so no press waits for a camera handshake. After
  release the floor stays the carer's for `TALKBACK_FLOOR_HOLD_SECS` (5 s) so
  they can answer the resident, then it is free for anyone — while the socket
  itself stays open a minute, so the next press is instant. Two carers can talk
  into two cameras at the same time; never two into one. Each tile shows who is
  talking (from `GET /api/v1/talkback/cameras`).
* **Hold Space** on the live wall talks into the SELECTED camera (the tile last
  clicked, ringed in orange; or the only camera there is) — the same press and
  release as its button. Ignored while typing, with Ctrl/Alt/Cmd, in a dialog,
  and for key repeat; the turn ends on key up, window blur or a hidden tab.
* Every talk is **logged**: who (the name and user id the carer's app sends),
  which room, when, and how many seconds they spoke — and every refusal, with
  why. `data/talkback_log.jsonl` (size-capped, gitignored),
  `GET /api/v1/talkback/log`, `python3 -m tools.talkback log`.

The rules that are structural, not cosmetic — the button is **press-and-hold**
(a toggle leaves hot microphones in living rooms); **one voice per camera** at a
time, refused rather than queued; and **listening stays on while you talk** (full
duplex), with echo cancelled at both ends — the camera's own, and the browser's
on the carer's microphone — instead of by muting the room.

How long a camera keeps a line open is firmware-specific and is MEASURED, not
assumed: every line records each connection's length and why it ended, in
`python3 -m tools.talkback lines` and `/api/v1/talkback/health`. If a firmware
drops silent lines, `TALKBACK_LINE_KEEPALIVE_SECS` sends one silent frame after
that many idle seconds. `TALKBACK_LINE_ALWAYS=false` opens lines only while a
carer is connected, which leaves the camera's speaker to the Tapo app when
nobody is talking.

A browser will only hand out a microphone on a **secure page**, so talk-back
works on the fleet address (`https://edgeai.ceravishealth.in/<edge_id>/ui/`) and
on `localhost`, and never on a plain `http://<device-ip>:8000` page. The UI says
so rather than failing silently.

What it costs: one idle TCP connection per camera (the line), one WebSocket per
carer per camera while they use it, and while someone talks ~75 kbit/s out and well under 1%
of one CPU core, no GPU. It never opens the camera's preview session, so the
media backbone still pulls each camera exactly once.

```bash
python3 -m tools.talkback list        # what is commissioned, and where
python tests/test_talkback.py         # offline proof of the bytes and the rules
python tests/test_talkback_ws.py      # a real server + client, end to end
# tests/talkback-ui.html               # the browser side, in any browser
```
