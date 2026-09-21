# Talk-back — complete call manual

How the ceravishealth app (web **and** mobile) **speaks into a room through the
camera's own speaker**, and **listens back**, end to end.

```
  Carer's browser  ──►  your backend  ──►  frp tunnel  ──►  edge device  ──►  Tapo camera
   mic -> A-law       (adds nothing,       (per-home URL)   (one speaker      (port 8800,
   20 ms frames        or is skipped                         per camera)       G.711 A-law)
   over a WebSocket    entirely)
```

Source of truth: [edge/api/talkback_routes.py](../edge/api/talkback_routes.py)
(the routes), [edge/talkback/sessions.py](../edge/talkback/sessions.py) (who may
hold a speaker), [edge/talkback/protocol.py](../edge/talkback/protocol.py) (the
camera wire), [edge/static/talk.js](../edge/static/talk.js) (a complete, working
browser client you can copy).

> **Read §7 before writing any code.** Two rules decide whether this works at
> all: the page must be **https**, and the audio must be **G.711 A-law at
> 8 kHz**. Everything else is detail.

---

## 1. Base URL and authentication

| Where you call from | Base |
|---|---|
| **Cloud / backend (the real one)** | `https://edgeai.ceravishealth.in/<edge_id>/api/v1/talkback` |
| **Inside the home LAN (dev only)** | `http://<jetson-ip>:8000/api/v1/talkback` |

The `/<edge_id>` prefix is how the fleet tunnel picks the house — **frps routes
by URL only, never by the request body**. The edge strips its own prefix
internally, so the path it serves is the same in both rows.

**Authentication is the `edge_id`**, exactly as PTZ and recordings do it. There
is no API key and no bearer token.

| Method | Where the edge_id goes |
|---|---|
| `GET`, `DELETE`, `WebSocket` | query parameter `?edge_id=...` (alias: `edgeId`) |
| `PUT`, `POST` | JSON body field `edgeId` (alias: `edge_id`) |

The WebSocket carries it as a **query parameter** because a browser cannot set
headers on a WebSocket handshake. It is checked **before** the socket is
accepted, so a wrong caller never reaches the camera and never makes a noise.

| Result | Meaning |
|---|---|
| `401` | `edgeId` missing |
| `409` | `edgeId` does not match this device |
| `503` | talk-back is switched off on this device (`TALKBACK_ENABLED`) |

---

## 2. `GET /cameras` — what can be talked to

Read-only, touches no network, safe on every page load.

```
GET /<edge_id>/api/v1/talkback/cameras?edge_id=<edge_id>
```

**200**

```json
{
  "enabled": true,
  "cameras": [
    {
      "camera_id": "cam_1",
      "camera_name": "LOUNGE",
      "room_name": "LIVING_ROOM",
      "host": "10.42.0.250",
      "configured": true,
      "credential_updated_at": "2026-09-09T14:22:05+05:30",
      "enabled": true,
      "busy": false
    },
    {
      "camera_id": "cam_2",
      "camera_name": "BEDROOM",
      "room_name": "BEDROOM",
      "host": "10.42.0.218",
      "configured": false,
      "credential_updated_at": null,
      "enabled": true,
      "busy": false
    }
  ],
  "active": {}
}
```

| Field | Meaning |
|---|---|
| `enabled` | Device-wide switch. `false` ⇒ show nothing; every other call returns 503. |
| `configured` | This camera has been **commissioned** (§4). `false` ⇒ offer "Enable talk", not a microphone. |
| `busy` | Somebody is holding this camera's speaker **right now**. |
| `active` | Live sessions, same shape as `/health` below. Empty when nobody is talking. |

`credential_updated_at` is the **fact** of a credential and when it was set. No
hash material is ever returned — a hash in an API response can be ground offline.

---

## 3. `GET /health` — is the channel healthy

The live gauge. Poll it from a dashboard; do **not** poll it per page render.

```
GET /<edge_id>/api/v1/talkback/health?edge_id=<edge_id>
```

**200**

```json
{
  "enabled": true,
  "active_sessions": 1,
  "active": {
    "cam_1": {
      "holder": "203.0.113.44",
      "client_id": "cmf8q2x1-3-k9d2af",
      "seconds": 12.4,
      "frames": 620,
      "frames_sent": 618,
      "frames_dropped": 2,
      "bytes_sent": 98880,
      "queued_ms": 20,
      "peak_queued_ms": 140
    }
  },
  "limits": {
    "hold_secs": 90.0,
    "max_turn_secs": 300.0,
    "sample_rate": 8000,
    "frame_bytes": 160,
    "codec": "alaw"
  }
}
```

**`queued_ms` is the number that answers "why does it sound delayed".** It is
milliseconds of the carer's voice still sitting in the device's socket to the
camera, not yet heard in the room.

| `queued_ms` | Reading |
|---|---|
| `0 – 60` | Healthy. This is the normal state on a LAN. |
| `60 – 400` | The link is struggling; the carer will hear themselves lag. |
| `>= 400` | Frames are being **dropped** on purpose — newest kept, oldest lost. |
| `>= 4000` | The camera stopped reading; the session is torn down and the client reconnects. |

`frames_dropped` rising while `queued_ms` stays low is normal after a blip — it
is the backlog being discarded rather than played late.

---

## 4. `PUT /{camera_id}/credential` — commission one camera

The **one** moment a TP-Link account password is handled. It is hashed on the
device and discarded; nothing stores it, logs it, or can read it back.

```
PUT /<edge_id>/api/v1/talkback/cam_1/credential
Content-Type: application/json
```

```json
{ "edgeId": "NrPq8...", "password": "the TP-Link ACCOUNT password" }
```

**200**

```json
{ "camera_id": "cam_1", "configured": true,
  "updated_at": "2026-09-21T11:04:18+05:30" }
```

| Result | Meaning |
|---|---|
| `400` | Blank password. A blank can never silently replace a working credential. |
| `401` / `409` | edge_id missing / wrong. |

> **This is NOT the RTSP/ONVIF camera password.** It is the **TP-Link cloud
> account** password — the one used to sign into the Tapo app. The camera
> authenticates local callers against a cached copy of that credential pushed by
> TP-Link's cloud.
>
> **If a correct password is rejected with 401:** the account password was
> changed while the camera had no internet, so the camera still wants the old
> one. **Remove the camera in the Tapo app and re-add it.** That is the only
> known fix, and it works.

### `DELETE /{camera_id}/credential`

```
DELETE /<edge_id>/api/v1/talkback/cam_1/credential?edge_id=<edge_id>
```

```json
{ "camera_id": "cam_1", "configured": false, "removed": true }
```

---

## 5. `POST /{camera_id}/test` — prove the chain, silently

Opens a real speaker session and closes it **without sending audio**. This is
the handover check: it proves reachability, the credential and the firmware
without startling anyone in the room.

```
POST /<edge_id>/api/v1/talkback/cam_1/test
Content-Type: application/json
```

```json
{ "edgeId": "NrPq8..." }
```

**200**

```json
{ "ok": true, "camera_id": "cam_1", "host": "10.42.0.250",
  "session_id": "6", "elapsed_ms": 103, "auth": "sha256" }
```

`elapsed_ms` is the real handshake cost, and it is the floor on how long a first
press takes. Anything under ~300 ms is healthy.

### Error codes — every one of them

| HTTP | `code` | What actually happened |
|---|---|---|
| `404` | `no_camera` | No camera with that id or label. |
| `428` | `no_credential` | Not commissioned yet — call §4 first. |
| `409` | `no_host` | The camera has no usable address on file. |
| `409` | `busy` | Someone is already speaking to this camera. |
| `401` | `unauthorized` | The camera rejected the credential (see the re-pair note in §4). |
| `502` | `unreachable` | No answer on port 8800 — offline, or the firmware has no talk port. |
| `502` | `refused` / `protocol` / `closed` | The camera answered, but not with talk-back. |
| `504` | `timeout` / `stalled` | The camera went quiet mid-handshake, or stopped reading. |

Errors carry a structured detail:

```json
{ "detail": { "code": "no_credential",
              "message": "BEDROOM has no talk-back credential yet. ..." } }
```

---

## 6. `WS /{camera_id}/stream` — the live microphone

```
wss://edgeai.ceravishealth.in/<edge_id>/api/v1/talkback/cam_1/stream
    ?client_id=<your-id>&edge_id=<edge_id>
```

### 6.1 What you send

| Frame | Content |
|---|---|
| **binary** | Raw **8 kHz mono G.711 A-law**. 160 bytes = 20 ms. Send as you capture. |
| **text** | `{"type":"ping"}` — keeps proxies from idling the socket out. |
| **text** | `{"type":"stop"}` — a clean hang-up. |

A binary frame larger than 8000 bytes (1 second) is ignored as a client bug.

> **Control frames deliberately do NOT refresh the hold window.** Only speech
> does. A chatty client cannot hold a household's speaker without saying a word.

### 6.2 What you receive

**On success, immediately:**

```json
{ "type": "open", "camera_id": "cam_1", "session_id": "6",
  "sample_rate": 8000, "codec": "alaw", "frame_bytes": 160,
  "mic_gain": 1.0, "hold_secs": 90.0, "max_turn_secs": 300.0,
  "client_id": "cmf8q2x1-3-k9d2af" }
```

Do not send audio before this frame arrives.

**Roughly once a second while speech is flowing:**

```json
{ "type": "stats", "seconds": 12.4, "bytes": 98880,
  "frames_sent": 618, "frames_dropped": 2,
  "queued_ms": 20, "peak_queued_ms": 140 }
```

**On a mid-session failure:**

```json
{ "type": "error", "code": "stalled",
  "message": "The camera stopped accepting audio." }
```

### 6.3 Close codes

| Code | Meaning | Retry? |
|---|---|---|
| `1000` / `1001` | The device closed the channel on schedule (hold window, max turn). | **No** |
| `4401` | edge_id missing or wrong. | **No** |
| `4409` | Someone **else** holds this camera's speaker. | **No** |
| `4503` | Talk-back is switched off on this device. | **No** |
| `4500` | The camera handshake failed. The `reason` says why. | **No** |
| `1006` / `1011` | The network dropped. | **Yes** — see §6.4 |

The close `reason` is the device's own sentence, already written for a human.
Show it. Codes only carry a class; reasons are capped at 123 bytes by the
WebSocket protocol itself.

### 6.4 `client_id` — how a carer gets back in

**Mint one id per microphone control, keep it for the life of that control, and
send it on every connect including reconnects.**

A dropped WebSocket does not always tell the device it dropped. Without
`client_id`, the holder of a camera would be a socket that no longer exists, and
the carer it belonged to would be refused from **their own** microphone until the
90-second hold window expired — exactly when getting back matters most.

A connect carrying the **same `client_id` as the current holder reclaims that
session**. A different id, or no id at all, is refused with `4409` as normal.
Because the id is minted by the client per microphone, this can only ever hand a
session back to the control that already had it.

Recommended client behaviour, and what [talk.js](../edge/static/talk.js) does:

* retry on `1006`/`1011` only, never on the **No** rows above
* backoff `300 ms -> 1.8x -> 3 s`, giving up after **15 s total**
* keep capturing while reconnecting, into a **5-frame (100 ms) queue**, and drop
  the **oldest** frames when it overflows — the newest are the words still being
  said
* show a distinct "Reconnecting…" state. A carer shown nothing presses again,
  and a second press is a second session.

### 6.5 The rules the device enforces

| Rule | Value | Why |
|---|---|---|
| **One speaker per camera** | hard | Two people in one room at once is unusable, not degraded. Refused, never queued. |
| **Hold window** | `hold_secs` (90 s) | The session stays open between presses, so only the first pays the ~200 ms handshake. Released after this much silence. |
| **Max continuous speech** | `max_turn_secs` (300 s) | The stuck-button guard. A gap of more than 1 s resets it. |
| **Backpressure** | 400 ms / 4000 ms | Late speech is dropped; a camera that stopped reading ends the session. |

Holding is **not free**: a camera has one speaker, and holding it locks out other
carers **and the Tapo app**. Hang up when the user navigates away.

---

## 7. Integrating into app.ceravishealth.in

### 7.1 The two rules that decide everything

**1. The page must be https.** Browsers refuse `getUserMedia` outside a secure
context. Talk-back works on `https://edgeai.ceravishealth.in/...` and on
`localhost`, and **never** on `http://<jetson-ip>:8000`. This is the single most
common "it is broken" report, and it is not a bug.

**2. The audio must be G.711 A-law, 8 kHz, mono.** The camera accepts exactly
one thing. Send anything else and you get silence with no error — the camera
gives no feedback on a stream it cannot decode.

### 7.2 Where the audio is encoded

**In the browser, not on your backend.** The browser already has the samples;
encoding there costs a device nothing and keeps your backend out of the audio
path entirely. Use an `AudioWorklet`.

[edge/static/talk-worklet.js](../edge/static/talk-worklet.js) is a complete
implementation — resample to 8 kHz, soft-limit with `tanh` (never a plain
multiply: G.711 clips hard, so a plain gain becomes distortion), encode A-law,
post 160-byte frames. Copy it.

Ask for the microphone with all three processors on:

```js
navigator.mediaDevices.getUserMedia({
  audio: { channelCount: 1, echoCancellation: true,
           noiseSuppression: true, autoGainControl: true },
  video: false,
})
```

The camera runs its own AEC, but the room's echo comes back through the
**carer's** speaker too. Ask for all three.

### 7.3 Your backend is optional

The WebSocket goes **browser -> fleet tunnel -> edge**. Your backend does not
need to be in the media path and should not be: every hop you add is latency in
a healthcare intercom. The backend's only job is to tell the app the `edge_id`
of the house being viewed.

If you must proxy, proxy the WebSocket **frame for frame with no buffering**, and
preserve `X-Forwarded-For` — the device uses it to name who is holding a camera
in the "someone else is speaking" message.

### 7.4 Minimum viable integration

```js
// 1. One id per microphone control, for the life of that control.
const clientId = "app-" + crypto.randomUUID();

// 2. Only offer a mic for cameras that are enabled AND configured.
const inv = await fetch(`${BASE}/cameras?edge_id=${edgeId}`).then(r => r.json());
if (!inv.enabled) return;                       // device-wide switch is off
const cam = inv.cameras.find(c => c.camera_id === id);
if (!cam.configured) return showCommissionButton(cam);

// 3. Open on the FIRST press, not on page load. It takes a household's speaker.
const ws = new WebSocket(
  `${WSS}/${id}/stream?client_id=${clientId}&edge_id=${edgeId}`);
ws.binaryType = "arraybuffer";

ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.type === "open")  startCapture();       // not before
  if (m.type === "stats") showLatency(m.queued_ms);
  if (m.type === "error") fail(m.message);
};

// 4. Send 160-byte A-law frames, oldest-dropped under backpressure.
onFrame(frame => {
  outbox.push(frame);
  while (outbox.length && ws.bufferedAmount < 960) ws.send(outbox.shift());
  while (outbox.length > 5) outbox.shift();     // drop the OLDEST, keep newest
});

// 5. Reconnect on 1006/1011 only, with the SAME clientId.
ws.onclose = ev => {
  if ([1000, 1001, 4401, 4409, 4503, 4500].includes(ev.code))
    return fail(ev.reason);
  scheduleRejoin();                             // same clientId
};
```

### 7.5 Press-and-hold, not a toggle

**Do not ship a toggle.** A toggle leaves hot microphones open in living rooms.
Press-and-hold cannot: releasing, losing the page, switching tabs or letting the
pointer slip all end the turn. Hang up on `visibilitychange` — a backgrounded tab
must not sit on a household's speaker.

### 7.6 Listening back, and why it must duck

The camera's microphone is already in the WHEP live stream. To hear a room, add a
`recvonly` audio transceiver to the existing WebRTC offer — **no second
connection to the camera**.

**Listening must be half-duplex with talking.** A live speaker and a live
microphone in one room howl; camera-side AEC is not enough. Mute the incoming
audio for as long as the carer is speaking.

Mute it on the **track** (`track.enabled = false`), not only on the element — a
muted element still decodes, and an element's `muted` flag is the one piece of
state an autoplay retry is allowed to fight with. And read the *track* back when
you paint the button: a listen toggle that remembers its own state drifts out of
step with the audio path every time the control is rebuilt, and a toggle that is
out of step by one is a toggle that will not switch off.

### 7.7 The latency budget

| Leg | Cost |
|---|---|
| Browser capture + A-law encode | ~20–40 ms (one frame, plus the graph) |
| Browser -> edge over the fleet tunnel | the network, typically 20–80 ms |
| Edge -> camera (`TCP_NODELAY` on, no per-frame drain) | ~1–5 ms on a LAN |
| Camera decode -> speaker | ~40–80 ms, and not ours to change |

**The first press** additionally pays the camera handshake (~100–300 ms,
measurable with §5). Every press after it, within `hold_secs`, pays none of it.

Bandwidth is ~75 kbit/s out and is never the bottleneck. If it sounds delayed,
read `queued_ms` from §3 — that is where the answer is.

---

## 8. Commissioning from the command line

For a technician at a handover, with no app involved:

```bash
cd edge
python3 -m tools.talkback list                     # what is configured
python3 -m tools.talkback set  --camera LOUNGE     # prompts for the password
python3 -m tools.talkback test --camera LOUNGE     # silent proof of the chain
python3 -m tools.talkback tone --camera LOUNGE     # makes an actual sound
python3 -m tools.talkback diagnose --camera LOUNGE # when "unauthorized" hides three faults
```

`diagnose` tries twelve credential shapes with one cheap local connection each
and labels which the camera accepts. Use it before escalating a 401.
