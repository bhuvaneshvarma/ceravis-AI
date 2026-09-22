# Talk-back — complete call manual

How the ceravishealth app (web **and** mobile) **speaks into a room through the
camera's own speaker**, and **listens back**, end to end.

```
  Carer's browser  ──►  your backend  ──►  frp tunnel  ──►  edge device  ──►  Tapo camera
   mic -> A-law       (adds nothing,       (per-home URL)   (keeps a line     (port 8800,
   20 ms frames over   or is skipped                         open to each      G.711 A-law)
   that camera's own   entirely)                             camera; one
   WebSocket                                                 voice at a time)
```

Source of truth: [edge/api/talkback_routes.py](../edge/api/talkback_routes.py)
(the routes), [edge/talkback/sessions.py](../edge/talkback/sessions.py) (the
floor: who may speak), [edge/talkback/lines.py](../edge/talkback/lines.py) (the
edge's line to each camera), [edge/talkback/protocol.py](../edge/talkback/protocol.py)
(the camera wire), [edge/static/talk.js](../edge/static/talk.js) (a complete, working
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
headers on a WebSocket handshake. It is checked before anything else, so a wrong
caller never reaches a camera and never makes a noise.

The socket is **accepted first** and then refused with a reason (§6.3). Closing a
WebSocket before accepting it becomes an HTTP 403 on the handshake, which a
browser reports as code `1006` with an empty reason, so no refusal could ever say
why. Until 2026-09-22 that is exactly what happened.

| Result | Meaning |
|---|---|
| `401` | `edgeId` missing |
| `409` | `edgeId` does not match this device |
| `503` | talk-back is switched off on this device (`TALKBACK_ENABLED`) |

---

## 2. `GET /cameras` — what can be talked to

Read-only, touches no network, safe on every page load. While a live view is
open, read it again every **15 s**: it is how every carer's screen learns who is
talking into which room (`floor`) and whether a camera's line went down.

```
GET /<edge_id>/api/v1/talkback/cameras?edge_id=<edge_id>
```

**200**

```json
{
  "enabled": true,
  "home_configured": true,
  "cameras": [
    {
      "camera_id": "LOUNGE",
      "camera_name": "LOUNGE",
      "room_name": "LOUNGE",
      "host": "10.42.0.250",
      "configured": true,
      "credential_scope": "home",
      "credential_updated_at": "2026-09-21T16:40:02+05:30",
      "enabled": true,
      "busy": false,
      "floor": { "state": "free" },
      "readiness": {
        "state": "ready",
        "message": "Ready.",
        "detail": "",
        "checked_at": "2026-09-21T16:40:03+05:30",
        "elapsed_ms": 103,
        "line": "open"
      }
    },
    {
      "camera_id": "LIVING_ROOM",
      "camera_name": "LIVING ROOM",
      "room_name": "LIVING ROOM",
      "host": "10.42.0.251",
      "configured": true,
      "credential_scope": "home",
      "credential_updated_at": "2026-09-21T16:40:02+05:30",
      "enabled": true,
      "busy": false,
      "floor": { "state": "free" },
      "readiness": {
        "state": "rejected",
        "message": "The camera refused this home's TP-Link password. If the password is right, remove the camera in the Tapo app and add it again — it is holding an old copy.",
        "detail": "The camera rejected the credential. ...",
        "checked_at": "2026-09-21T16:40:04+05:30",
        "elapsed_ms": null,
        "line": "closed"
      }
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `enabled` | Device-wide switch. `false` ⇒ show nothing; every other call returns 503. |
| `home_configured` | This home's one TP-Link password has been set (§4). |
| `configured` | This camera has a credential — its own, or the home's. |
| `credential_scope` | `"home"` (the one password for the home) or `"camera"` (an override for a camera on a different TP-Link account). |
| `readiness.state` | **What the camera's talk line says right now.** Draw the button from this — see the table below. |
| `readiness.message` | A sentence for the carer. Show it as-is. |
| `readiness.detail` | The camera's own reason, for an installer (e.g. the refused-password checklist). |
| `readiness.line` | `open` / `closed`: whether the edge's talk line to this camera is up. |
| `floor` | Who may speak into this camera now: `{"state": "free"}`, or `{"state": "speaking" \| "holding", "by": "Nurse Priya", "client_id": "…"}`. Show "Nurse Priya is talking" on that tile. |
| `busy` | `true` when the floor is not free. |

**Draw the talk control from `readiness.state`**, not from `configured` alone.
The device checks every camera by itself from boot, so it already knows whether
a press would work — a carer should never find out by being refused mid-word.

| `readiness.state` | What to show |
|---|---|
| `ready` | The hold-to-talk microphone. |
| `connecting` | The microphone. (The line is opening — at boot, or after the camera ended it.) |
| `needs_password` | A "Set up talk" button that opens the home password (§4). |
| `rejected` | A "Talk: password refused" button that opens the same dialog, with `readiness.message`. |
| `paused` | The same, with `readiness.detail` saying until when (lock-out guard, §6.4). |
| `unreachable` | The microphone, labelled "Speaker offline" — pressing still tries, it may be back. |
| `in_use` | The microphone, labelled "Speaker in use" — someone is talking from the Tapo app. |
| `unsupported` | **Nothing.** This camera has no talk-back; a dead control is worse than none. |

`credential_updated_at` is the **fact** of a credential and when it was set. No
hash material is ever returned — a hash in an API response can be ground offline.

### The camera line — why a press never waits for the camera

The edge keeps **its own talk session open to every camera** from boot (the
*line*), and re-opens it by itself when the camera ends it. A press therefore
never waits for the camera's ~100–300 ms handshake, and an open line is itself the
proof that the camera works: there is no separate periodic check.

| When the line fails | Retried | Why |
|---|---|---|
| the camera ended it after being open a while | at once | normal — reboot, idle timeout |
| the camera ended it straight after granting it | growing wait | it is refusing something |
| `rejected` (password) | every 60 min | slow **on purpose**: cameras lock accounts out for repeated failed logins |
| `unreachable` | 2 s, doubling to 2 min | a rebooting camera comes back fast |
| `needs_password` | when a password arrives | nothing to try until then |

Any password change — through this API or the CLI — retries every line at once.

`TALKBACK_LINE_ALWAYS=false` opens lines only while at least one carer is
connected, which leaves the camera's speaker free for the Tapo app when nobody is
talking. **How long a camera keeps a line is firmware-specific and measured, not
assumed** — see the line record in §3.

**Zero-input cameras.** A camera with no credential gets one attempt, once per
boot, with its **own stream password** (already in camera setup). If its firmware
accepts that, it is commissioned with nothing typed at all.

---

## 3. `GET /health` — the lines, the floors, and the line record

The live gauge. Poll it from a dashboard; do **not** poll it per page render.

```
GET /<edge_id>/api/v1/talkback/health?edge_id=<edge_id>
```

**200**

```json
{
  "enabled": true,
  "home_configured": true,
  "settings": { "line_always": true, "line_keepalive_secs": 0.0,
                "floor_hold_secs": 5.0, "codec": "alaw",
                "sample_rate": 8000, "frame_bytes": 160 },
  "lines": {
    "LOUNGE": {
      "state": "ready", "line": "open", "elapsed_ms": 103,
      "open_secs": 5421.3, "opens": 2, "drops": 1, "idle_secs": 12.0,
      "frames_sent": 3012, "frames_dropped": 0, "queued_ms": 0,
      "history": [
        { "at": "2026-09-22T09:10:02+05:30", "event": "open", "connect_ms": 98 },
        { "at": "2026-09-22T10:40:11+05:30", "event": "dropped", "after_secs": 5409.2 },
        { "at": "2026-09-22T10:40:12+05:30", "event": "open", "connect_ms": 103 }
      ]
    }
  },
  "talkers": 2,
  "floors": {
    "LOUNGE": { "state": "speaking", "by": "Nurse Priya", "client_id": "app-…",
                "user_id": "1042", "holder": "203.0.113.44",
                "since": "2026-09-22T15:40:02+05:30", "seconds": 4.2, "talk_secs": 3.9 }
  },
  "guard": {}
}
```

**The line record answers "how long does a camera keep the line".** Every line
keeps its last 20 events: each `open` (with how long the camera took), each
`dropped` (with how long it had been open), each `failed` (with why). If a
firmware turns out to close *silent* lines after some time, set
`TALKBACK_LINE_KEEPALIVE_SECS` below that time and one silent frame is sent after
that many idle seconds. The CLI prints the same record:
`python3 -m tools.talkback lines`.

**`queued_ms` answers "why does it sound delayed"**: milliseconds of the carer's
voice still waiting on the device to reach the camera.

| `queued_ms` | Reading |
|---|---|
| `0 – 60` | Healthy. This is the normal state on a LAN. |
| `60 – 400` | The link is struggling; the carer will hear themselves lag. |
| `>= 400` | Frames are being **dropped** on purpose — newest kept, oldest lost. |
| `>= 4000` | The camera stopped reading; the line is closed and re-opened clean. |

### `GET /log` — who spoke into which room, when, and for how long

```
GET /<edge_id>/api/v1/talkback/log?edge_id=<edge_id>[&camera=LOUNGE][&limit=100]
```

Newest first, up to `limit` (1–1000, default 100). One entry per **talk** (a
carer's floor, from grant to the end of the hold) and per **refusal**:

```json
{ "entries": [
  { "event": "talk", "camera_id": "LOUNGE", "camera_name": "LOUNGE",
    "name": "Nurse Priya", "user_id": "1042", "client_id": "app-…",
    "holder": "203.0.113.44",
    "started_at": "2026-09-22T15:40:02+05:30", "ended_at": "2026-09-22T15:40:31+05:30",
    "talk_secs": 18.4, "held_secs": 23.9, "ended": "released" },
  { "event": "refused", "camera_id": "LOUNGE", "camera_name": "LOUNGE",
    "name": "Nurse Arun", "user_id": "1077", "client_id": "app-…",
    "holder": "198.51.100.7", "at": "2026-09-22T15:40:10+05:30",
    "code": "busy", "message": "Nurse Priya is speaking to LOUNGE." }
] }
```

`talk_secs` counts actual speech (silence is not counted); `held_secs` is from the
grant to the last word. `ended`: `released`, `taken` (a higher priority),
`line_lost` (the camera went away), `shutdown`. `name` and `user_id` are what the
carer's app sent (§6.1): the edge records them, it cannot verify them. The log
lives on the device (`data/talkback_log.jsonl`, capped at ~2×5 MB).

---

## 4. `PUT /credential` — the ONE password for the whole home

Every camera in a home is paired to one TP-Link account, so talk-back needs one
password per **home**, not per camera. Set it once — the edge UI asks for it in
**Setup → Cameras**, under "Talk-back" — and every camera uses it, including
cameras added later. This is the only setup talk-back has.

It is the **one** moment a TP-Link account password is handled. It is hashed on
the device and discarded; nothing stores it, logs it, or can read it back.

```
PUT /<edge_id>/api/v1/talkback/credential
Content-Type: application/json
```

```json
{ "edgeId": "NrPq8...", "password": "the TP-Link ACCOUNT password" }
```

The device then **checks every camera silently and answers with each verdict**,
so whoever typed the password sees the result in the same response (up to ~30 s
if a camera is offline):

**200**

```json
{
  "configured": true,
  "scope": "home",
  "updated_at": "2026-09-21T16:40:02+05:30",
  "replaced_camera_overrides": ["LOUNGE", "LIVING_ROOM"],
  "ready": 1,
  "total": 2,
  "all_ready": false,
  "cameras": {
    "LOUNGE":      { "state": "ready",    "message": "Ready.", "elapsed_ms": 103, "...": "..." },
    "LIVING_ROOM": { "state": "rejected", "message": "The camera refused this home's TP-Link password. ...", "...": "..." }
  }
}
```

A **200 with `all_ready: false` is not an error** — the password was stored; one
or more cameras did not accept it. Show each camera's `message`.

Setting the home password **replaces the per-camera passwords an operator typed
earlier**: a password change makes every one of them wrong at once. Credentials
the device proved for itself (a camera's own stream password) are kept.

`DELETE /<edge_id>/api/v1/talkback/credential?edge_id=<edge_id>` forgets it.

### `POST /check` — "check again"

Re-checks every camera now and returns the same `cameras` / `ready` / `total` /
`all_ready` shape. The device already does this on its own schedule; this is
for right after re-pairing a camera in the Tapo app.

```
POST /<edge_id>/api/v1/talkback/check
Content-Type: application/json

{ "edgeId": "NrPq8..." }
```

### `PUT /{camera_id}/credential` — an override for one camera

Only for a home whose cameras sit on **two different** TP-Link accounts. Same
body as above; the camera's own entry then wins over the home password.

```
PUT /<edge_id>/api/v1/talkback/LOUNGE/credential
Content-Type: application/json
```

```json
{ "edgeId": "NrPq8...", "password": "the OTHER TP-Link account's password" }
```

**200**

```json
{ "camera_id": "LOUNGE", "configured": true, "scope": "camera",
  "updated_at": "2026-09-21T11:04:18+05:30" }
```

`DELETE /<edge_id>/api/v1/talkback/LOUNGE/credential?edge_id=<edge_id>` removes
the override; the camera falls back to the home password.

| Result (all three writes) | Meaning |
|---|---|
| `400` | Blank password. A blank can never silently replace a working credential. |
| `401` / `409` | edge_id missing / wrong. |

> **This is NOT the RTSP/ONVIF camera password.** It is the **TP-Link cloud
> account** password — the one used to sign into the Tapo app. The camera
> authenticates local callers against a cached copy of that credential pushed by
> TP-Link's cloud.
>
> **If a correct password is refused (`readiness.state: rejected`)**, check in
> this order (the device gives the same list, from `edge/talkback/advice.py`):
>
> 1. In the Tapo app, **Me > Tapo Lab > Third-Party Compatibility is ON**.
> 2. The camera **belongs to** the account whose password was entered. A camera
>    *shared* from another account expects the **owner's** password.
> 3. That account signs in with an **email and password**, not Google or Apple.
> 4. Enter that account's current password again.
> 5. Only then: remove the camera in the Tapo app and add it again. This can
>    reset the camera's stream password, WiFi and address.
>
> Re-pairing is last on purpose. On 2026-09-21 both bench cameras refused the
> current password straight after being re-paired, with internet, and the code
> from the day it last worked was refused too. So it is not the one fix. You do not
> need to touch this device after fixing the account: it re-checks by itself, or
> at once with `POST /check`.

---

## 5. `POST /{camera_id}/test` — is this camera's line open

The handover check. If the camera's line is open it answers at once; if not, it
tries to open it now and says why it cannot. **Silent** — a line carries no sound
until somebody speaks. `"force": true` skips the lock-out pause, for a technician
who has just fixed the account.

```
POST /<edge_id>/api/v1/talkback/cam_1/test
Content-Type: application/json
```

```json
{ "edgeId": "NrPq8..." }
```

**200**

```json
{ "ok": true, "camera_id": "cam_1", "host": "10.42.0.250", "line": "open",
  "connect_ms": 103, "open_secs": 5421.3, "auth": "sha256" }
```

`connect_ms` is what the camera took to grant the line when it last opened.

### Error codes — every one of them

| HTTP | `code` | What actually happened |
|---|---|---|
| `404` | `no_camera` | No camera with that id or label. |
| `428` | `no_credential` | Not commissioned yet — call §4 first. |
| `409` | `no_host` | The camera has no usable address on file. |
| `424` | `unauthorized` | **The camera** refused the TP-Link password. Deliberately not `401`: a `401` from this API always means *your edge_id* is wrong. `message` carries the checklist. |
| `429` | `cooldown` | Paused after repeated refusals, so our own retries cannot lock the camera out (§6.4). `Retry-After` is set. Setting the password again lifts it at once. |
| `502` | `unreachable` | No answer on port 8800 — offline, or the firmware has no talk port. |
| `502` | `refused` | The camera's speaker is in use from the Tapo app, or switched off in it. |
| `502` | `protocol` | Something answered on the talk port, but not a Tapo talk endpoint. |
| `504` | `timeout` | The line did not open in time. |

Errors carry a structured detail:

```json
{ "detail": { "code": "no_credential",
              "message": "BEDROOM has no talk-back credential yet. ..." } }
```

---

## 6. `WS /{camera_id}/stream` — a carer's microphone, into one camera

```
wss://edgeai.ceravishealth.in/<edge_id>/api/v1/talkback/<camera_id>/stream
    ?edge_id=<edge_id>&client_id=<id>&name=<carer display name>&user_id=<your user id>
```

One socket per carer **per camera**. Connecting claims that camera's floor, so a
socket is opened on the first press. It may stay open between presses (the next
press is then instant) without holding the room: the floor is let go on release,
whatever the socket does.

### 6.1 Connect

| Query | Required | Meaning |
|---|---|---|
| `edge_id` | yes | The home's edge id. Checked before anything else. |
| `<camera_id>` (path) | yes | The camera, as `GET /cameras` names it — the same id as the camera's live-stream path. A label (`LIVING ROOM`) is accepted too. |
| `client_id` | recommended | Your id for this carer's control, up to 64 chars. Send the same one on a reconnect: a carer whose network drops mid-sentence and comes back within the floor hold keeps the room. |
| `name` | recommended | The carer's display name, up to 40 characters, from your signed-in user. Shown to other carers ("Nurse Priya is speaking to LOUNGE") and written to the talk log. |
| `user_id` | recommended | Your backend's id for the carer, up to 64 characters. Written to the talk log only. |

`name` and `user_id` are what your app says they are: the edge records them and
cannot verify them. Aliases `clientId`, `userName`, `userId` are accepted.

### 6.2 What you send

| Frame | Content | When |
|---|---|---|
| **binary** | 160 bytes of **8 kHz mono G.711 A-law** = 20 ms | every 20 ms while the button is held, after `open` |
| text `{"type":"release"}` | the button came up | on release |
| text `{"type":"ping"}` | keeps proxies from idling the socket | every 20 s |
| text `{"type":"stop"}` | closing the socket | once, then close |

Speech on an open socket whose floor had been let go **takes it again** — if it
is free. Frames of pure digital silence (a muted microphone: every byte `0xD5` or
`0x55`) never take a room and are not forwarded. Frames over 8000 bytes are
ignored.

### 6.3 What you receive

```json
{ "type": "open", "camera_id": "LOUNGE", "client_id": "app-…",
  "codec": "alaw", "sample_rate": 8000, "frame_bytes": 160, "mic_gain": 1.0,
  "hold_secs": 60.0, "floor_hold_secs": 5.0, "max_turn_secs": 0 }
```

| Message | Meaning |
|---|---|
| `open` | The floor is yours and the camera's line is up. Send nothing before it. `mic_gain`: apply it before encoding. `hold_secs`: how long this socket may idle between presses before the edge closes it (close yours a little before). `floor_hold_secs`: how long the room stays yours after release. `max_turn_secs: 0`: no limit while held. |
| `{"type":"stats","frames_sent":…,"frames_dropped":…,"bytes_sent":…,"queued_ms":…,"peak_queued_ms":…}` | About once a second while you speak. |
| `{"type":"error","code":…,"message":…}` | A refusal, sent **just before** the close below. Show `message` as-is. |

**Every refusal is an `error` frame and then a close.** The close reason repeats
it as `"<code>: <sentence>"`, cut to 123 bytes.

| Close | `code` | Meaning | Reconnect? |
|---|---|---|---|
| `4409` | `busy` | Another carer is speaking to this camera, or spoke in the last few seconds. The sentence names them, and says when it frees. | No — show it |
| `4409` | `taken` | A higher-priority user took the floor (reserved for a future doctor role; nobody has priority today). | No — show it |
| `4429` | `cooldown` | Paused after repeated refused passwords (§6.4). | No — show setup |
| `4500` | `unauthorized` / `no_credential` | The camera's password. | No — show setup; **never retry** |
| `4500` | `unreachable` / `refused` / `timeout` | The camera did not open its speaker (offline, in use from the Tapo app, slow). | On the next press |
| `4500` | `protocol` / `no_camera` | Not a talk-back camera / unknown id. | No |
| `4401` | `edge_id` | Missing or wrong edge_id. | No |
| `4503` | `disabled` | Talk-back is switched off on this device. | No |
| `1000` | — | Closed normally (you sent `stop`, or it idled past `hold_secs`). | On the next press |
| `1006` / `1011` / other | — | The network or a proxy dropped it. | **Yes**, while the button is held: same `client_id`, 0.3 s → ×1.8 → 3 s, for up to 15 s |

### 6.4 The rules

| Rule | Value | Why |
|---|---|---|
| **One voice per camera** | hard | Two instructions at once are unintelligible to the person in the room. Refused, never queued. |
| **Different cameras at the same time** | allowed | Two carers can talk into two rooms at once; one carer can too, on two sockets. |
| **No time limit** | while held | A carer speaks for as long as they hold the button. |
| **Release** | `{"type":"release"}`, the socket closing, or 1 s without speech | The floor becomes *holding*. |
| **Floor hold** | `floor_hold_secs` (5 s) | After release the room stays that carer's, so they can answer the resident; speaking again carries on instantly. Then it is free for anyone. |
| **Idle socket** | `hold_secs` (60 s) | A socket with no speech for this long is closed with `1000`. It held no room by then. |
| **Priority** | — | A higher priority takes the floor. Everyone is equal today; only a verified source may ever raise it. |
| **Backpressure** | 400 ms / 4000 ms | Late speech is dropped; a camera that stopped reading has its line closed and re-opened clean. |
| **Lock-out guard** | 3 refusals in a row | The same password refused 3 times in a row pauses that camera for 15 min, then 30, 60 and at most 3 h. While paused nothing dials it (HTTP `429`, WS `4429`, readiness `paused`). Setting the password again lifts it. |

### 6.5 Listening at the same time

Listening is **full duplex**: the room stays audible while a carer talks, so a
resident who answers mid-sentence is heard. Echo is cancelled at both ends — the
camera runs its own echo cancellation (`TALKBACK_MODE=aec`), and the carer's
microphone must be opened with `echoCancellation: true` (§7.2). Headphones remove
whatever echo is left on a loud speakerphone. **Do not mute Listen when the button
is pressed.**

**"The room goes silent while I talk" — find which side does it.** Nothing on the
edge mutes listening while talking. The live wall's Listen ring shows the room's
level as *received* (WebRTC stats, before any volume control), and keeps moving
while On air. Three causes, three checks:

| What you see | Cause | Fix |
|---|---|---|
| The Listen button says **"Muted while talking"** | That page runs the OLD code (before `0d0ed01`) — the device was not updated | Pull on the device; `GET /health` → `code.commit` must be the new one, `code.restart_needed` false |
| `python3 -m tools.talkback duplex --camera X` says **HALF DUPLEX IN THE CAMERA** | The camera's firmware silences its own microphone while its speaker plays | Nothing an app can change: note the camera model and firmware, and check for a firmware update |
| The probe says FULL DUPLEX and the ring moves, but it *sounds* silent | The carer's app mutes Listen on press, or the computer/phone lowers other audio while the microphone is open (Windows: Sound → Communications → "Do nothing") | Fix the app; change the OS setting, or use headphones |

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

Listening stays on while the carer talks (§6.5), so the room's voice plays from
the carer's own speaker while their microphone is live: echo cancellation here is
what keeps it out of the room. Ask for all three.

### 7.3 Your backend is optional

The socket goes **browser -> fleet tunnel -> edge**. Your backend does not
need to be in the media path and should not be: every hop you add is latency in
a healthcare intercom. The backend's only job is to tell the app the `edge_id`
of the house being viewed.

If you must proxy, proxy the WebSocket **frame for frame with no buffering**, and
preserve `X-Forwarded-For` — the device uses it to name who is holding a camera
in the "someone else is speaking" message.

### 7.4 Minimum viable integration

```js
// 1. When the live view opens (and every 15 s): which cameras can talk, who is talking.
const inv = await fetch(`${BASE}/cameras?edge_id=${edgeId}`).then(r => r.json());
if (!inv.enabled) return;                          // device-wide switch is off
drawButtons(inv.cameras);                          // readiness.state + floor.by

const clientId = "app-" + crypto.randomUUID();    // one per carer's control, kept
let ws = null, open = false, outbox = [];

// 2. Press: open this camera's socket (or reuse it). "open" = the room is yours.
function press(cameraId) {
  if (ws && open) return startSendingAudio();
  ws = new WebSocket(`${WSS}/${encodeURIComponent(cameraId)}/stream?edge_id=${edgeId}` +
    `&client_id=${clientId}&name=${encodeURIComponent(me.name)}&user_id=${me.id}`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "open")  { open = true; startSendingAudio(); }
    if (m.type === "error") showRefusal(m.message);           // show as-is; a close follows
  };
  ws.onclose = ev => { open = false; ws = null; /* reconnect only on 1006/1011 while held */ };
}

// 3. While held: 160-byte A-law frames every 20 ms, oldest dropped under backpressure.
onFrame(frame => {
  outbox.push(frame);
  while (outbox.length && open && ws.bufferedAmount < 960) ws.send(outbox.shift());
  while (outbox.length > 5) outbox.shift();
});

// 4. Release: stop sending, say so. Keep the socket (ping every 20 s) for the next press.
function release() { stopSendingAudio(); if (open) ws.send(JSON.stringify({ type: "release" })); }
```

The complete, tested client is [edge/static/talk.js](../edge/static/talk.js).

### 7.5 Press-and-hold, not a toggle

**Do not ship a toggle.** A toggle leaves hot microphones open in living rooms.
Press-and-hold cannot: releasing, losing the page, switching tabs or letting the
pointer slip all end the turn. Release on `visibilitychange` — a backgrounded tab
must not keep a live microphone. (The socket itself may stay open: after release
it holds no room.)

### 7.6 Listening back — at the same time as talking

The camera's microphone is already in the WHEP live stream. To hear a room, add a
`recvonly` audio transceiver to the existing WebRTC offer — **no second
connection to the camera**. Keep it playing while the carer talks (§6.5).

Mute it on the **track** (`track.enabled = false`), not only on the element — a
muted element still decodes, and an element's `muted` flag is the one piece of
state an autoplay retry is allowed to fight with. And read the *track* back when
you paint the button: a listen toggle that remembers its own state drifts out of
step with the audio path every time the control is rebuilt, and a toggle that is
out of step by one is a toggle that will not switch off.

**Rooms are independent.** Each tile's Listen is its own toggle: a carer may
listen to several rooms at once, and to any of them while talking into one. Show
each room's level (WebRTC `inbound-rtp.audioLevel`) so a carer can see which room
a sound came from; that, not muting the others, is what keeps several rooms
legible. The first click on each must come from a user gesture.

### 7.7 The latency budget

| Leg | Cost |
|---|---|
| Browser capture + A-law encode | ~20–40 ms (one frame, plus the graph) |
| Browser -> edge over the fleet tunnel | the network, typically 20–80 ms |
| Edge -> camera (`TCP_NODELAY` on, no per-frame drain) | ~1–5 ms on a LAN |
| Camera decode -> speaker | ~40–80 ms, and not ours to change |

**No press pays for a camera handshake**: the camera's line is already open. The
first press on a camera pays one WebSocket connection through the tunnel (~one
round trip); every press after it, within `hold_secs`, reuses the open socket.
The browser starting the microphone (~100–300 ms) is paid on the first press
after a while.

Bandwidth is ~75 kbit/s out and is never the bottleneck. If it sounds delayed,
read `queued_ms` from §3 — that is where the answer is.

---

## 8. Commissioning from the command line

For a technician at a handover, with no app involved:

```bash
cd edge
python3 -m tools.talkback list                     # what is configured
python3 -m tools.talkback set                      # THE home password; checks every camera
python3 -m tools.talkback test                     # silent check of every camera
python3 -m tools.talkback set  --camera LOUNGE     # an override for one camera
python3 -m tools.talkback test --camera LOUNGE     # silent proof for one camera
python3 -m tools.talkback lines                    # the line record (§3)
python3 -m tools.talkback log                      # who spoke, where, how long (§3)
python3 -m tools.talkback duplex --camera LOUNGE   # is the room audible WHILE talking (§6.5)
python3 -m tools.talkback tone --camera LOUNGE     # a beep, through the service's own session
python3 -m tools.talkback diagnose --camera LOUNGE # when "unauthorized" hides three faults
```

`set` and `test` without `--camera` exit 0 only when every camera is ready, so a
commissioning script can gate on them. A password set here is picked up by the
running service within 15 seconds — it watches the credential file.

`diagnose` tries twelve credential shapes with one cheap local connection each
and labels which the camera accepts. Use it before escalating a refusal.
