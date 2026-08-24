# Cloud PTZ — complete call manual

How the ceravishealth app (web **and** mobile) **pans and tilts a camera** in a
home, end to end: one endpoint, one JSON body, one auth rule, **two actions**.

```
   Browser / App  ──►  your backend  ──►  frp tunnel  ──►  edge device  ──►  ONVIF camera
                       (adds nothing)      (per-home URL)   (validates)      (ContinuousMove)
```

Source of truth: [edge/api/camera_routes.py:183](edge/api/camera_routes.py:183)
(the route), [edge/api/ptz_control.py](edge/api/ptz_control.py) (the core that
actually drives the camera), [edge/api/control_auth.py](edge/api/control_auth.py).

---

## 1. The endpoint

```
POST /api/v1/cameras/ptz
Content-Type: application/json
```

| Where you call from | Full URL |
|---|---|
| **Cloud / backend (the real one)** | `https://edgeai.ceravishealth.in/<edge_id>/api/v1/cameras/ptz` |
| **Inside the home LAN** | `http://<jetson-ip>:8000/api/v1/cameras/ptz` |

The `/<edge_id>` prefix is how the fleet tunnel picks the house — **frps routes by
URL only, never by the request body**. The edge strips its own prefix internally
(`_FleetEdgePrefix` in [edge/main.py:83](edge/main.py:83)), so the path the edge
serves is the same `/api/v1/cameras/ptz` in both rows.

There is **no** `X-Ceravis-Control-Token` and no API key on this call. That
legacy header was removed for good — see §3.

---

## 2. Request body — every field

```jsonc
// move
{
  "edgeId":      "NrPq8...",      // REQUIRED — this home's edge_id (deviceToken)
  "cameraLabel": "KITCHEN",       // REQUIRED — which camera
  "action":      "move",          // "move" (default) | "revert"
  "pan":         -0.4,            // -1.0 .. 1.0   negative = left,  positive = right
  "tilt":        0.0,             // -1.0 .. 1.0   negative = down,  positive = up
  "durationMs":  300              // how long to move; clamped to 2000 ms
}

// revert — put the camera back where it was before the user started panning
{ "edgeId": "NrPq8...", "cameraLabel": "KITCHEN", "action": "revert" }
```

| Field | Aliases accepted | Type | Required | Default | Meaning |
|---|---|---|---|---|---|
| `edgeId` | `edge_id` | string | **yes** (when the device is provisioned) | — | Must equal THIS device's edge_id. The whole authentication. |
| `cameraLabel` | `camera_label`, `cameraNumber` | string | **yes** | `""` | Room label (`KITCHEN`, `LIVING_ROOM`, …) or the raw `camera_id`. Case-insensitive, spaces = underscores. |
| `action` | — | string | no | `"move"` | `move` or `revert`. Nothing else is accepted. |
| `pan` | — | number | move only | `0` | Pan **velocity**, −1.0 … 1.0 (clamped). Not an angle. |
| `tilt` | — | number | move only | `0` | Tilt **velocity**, −1.0 … 1.0 (clamped). Not an angle. |
| `durationMs` | `duration_ms` | number | no | `0` → ceiling | Auto-stop after this many ms. Clamped to `ptz_max_move_ms` (**2000 ms**). `0`/missing ⇒ the full 2000 ms. |
| ~~`zoom`~~ | — | — | — | — | **Not accepted.** Zoom is client-side digital zoom in the player; the edge only drives pan/tilt. |

**camelCase and snake_case are interchangeable on every field** (`field()` takes
the first non-null key), so the backend can send its natural shape.

### The two actions — and the one that no longer exists

| action | What it does |
|---|---|
| `move` | Starts pan/tilt at the given velocities. **The edge stops it by itself** after `durationMs`. |
| `revert` | Drives the camera back to the framing it held **before the first move of this override**, and forgets it. Idempotent. |

> **There is no `stop`.** A move is self-terminating (§4), so there was never
> anything for a caller to stop — sending one only added a call that could be
> lost. `{"action":"stop"}` now returns a `400` that says exactly this. A move
> with `pan` and `tilt` both zero is also a `400`, not a silent stop.

---

## 3. Authentication — the edgeId match, nothing else

The request must carry the `edgeId` provisioned for that device (the
`deviceToken` from `userDetails`, used byte-identical everywhere — live links,
frp routing, this call). Only the app server that provisioned the device knows
it, so **the match IS the authentication** ([edge/api/control_auth.py](edge/api/control_auth.py)).

| Device state | Request `edgeId` | Result |
|---|---|---|
| Provisioned | matches | proceed |
| Provisioned | missing/empty | **401** `edgeId required` |
| Provisioned | different value | **409** `edge_id mismatch: request for 'X', this device is 'Y'` |
| Not provisioned (LAN dev box) | anything, incl. missing | accepted (nothing to check against yet) |

> Treat API-by-edgeId as "targets the right house", **not** as a secret — the
> edge_id also appears in the public live-link URL. Keep the family-facing links
> gated by the app account.

---

## 4. What the edge actually does with your call

### `action: "move"`

1. **Auth** — `check_edge_id(edgeId)`; a rejection is logged to the Cloud Sync Console.
2. **Resolve the camera** — `cameraLabel` → camera, matched against
   `camera_name`, `room_name` or `camera_id` (`CameraConfig.get_by_label`, the
   ONE cloud-facing addressing rule; recording playback resolves identically).
3. **Capability check** — the camera must have `ptz_supported == true` **and** an
   `onvif_xaddr` (i.e. it was added via ONVIF discovery, not typed in by hand).
4. **Remember the framing** — ONVIF `GetStatus`, once per override session,
   *before* the camera moves. This is what `revert` goes back to.
5. **Move** — ONVIF `ContinuousMove` with `Velocity{PanTilt x=pan y=tilt, Zoom 0}`
   on `onvif_ptz_token` (falling back to `onvif_profile_token`).
6. **Arm the auto-stop** — a timer issues `Stop` after the clamped duration. A new
   move on the same camera **cancels and replaces** the pending timer.

### `action: "revert"`

1–3 as above, then: cancel any in-flight auto-stop, `AbsoluteMove` back to the
remembered framing, and forget it. Nothing remembered (never moved this session,
or the camera doesn't report its position) ⇒ **`200` with `"status":"unchanged"`**
— safe to call blindly, e.g. every time the user closes the camera view.

### Why the command is self-terminating

The edge **always** auto-stops. If the network drops, the app crashes, or a
follow-up call is lost, the motor still halts within 2 s — a lost command can
never leave a camera spinning. This is why `durationMs` is a ceiling, not a
promise, and why the API has no stop action.

### Where "revert" gets its position

The pre-override framing is captured **in memory** the first time a camera is
moved, and held until a revert (or the camera is deleted). It is deliberately not
persisted: after an edge restart there is no override in progress to undo, and
the AI's own tracking re-frames the recipient. A camera that doesn't answer
`GetStatus`, or rejects `AbsoluteMove`, simply never reverts — you get
`"unchanged"` or a `502`, never a guessed position.

---

## 5. Sample calls

### Move — pan left for 300 ms

```bash
curl -X POST https://edgeai.ceravishealth.in/NrPq8xxxxxxx/api/v1/cameras/ptz \
  -H 'Content-Type: application/json' \
  -d '{"edgeId":"NrPq8xxxxxxx","cameraLabel":"KITCHEN","action":"move","pan":-0.4,"tilt":0,"durationMs":300}'
```

**200 response**
```json
{
  "status": "moving",
  "camera_id": "KITCHEN",
  "label": "KITCHEN",
  "pan": -0.4,
  "tilt": 0.0,
  "auto_stop_ms": 300
}
```

`pan`/`tilt` come back as **applied** (after clamping to ±1.0), and
`auto_stop_ms` is the duration actually armed after clamping to the ceiling.

### Revert — put the camera back

```bash
curl -X POST https://edgeai.ceravishealth.in/NrPq8xxxxxxx/api/v1/cameras/ptz \
  -H 'Content-Type: application/json' \
  -d '{"edgeId":"NrPq8xxxxxxx","cameraLabel":"KITCHEN","action":"revert"}'
```

**200 response — it moved back**
```json
{ "status": "reverted", "reverted": true, "camera_id": "KITCHEN", "label": "KITCHEN" }
```

**200 response — it was already home (nothing to undo)**
```json
{ "status": "unchanged", "reverted": false, "camera_id": "KITCHEN", "label": "KITCHEN" }
```

Branch on the boolean `reverted`, not on the string, if you need to know which
happened.

### Tilt up, no duration (falls back to the 2 s ceiling)

```bash
curl -X POST http://192.168.0.221:8000/api/v1/cameras/ptz \
  -H 'Content-Type: application/json' \
  -d '{"edgeId":"NrPq8xxxxxxx","cameraLabel":"LIVING ROOM","tilt":0.6}'
```

```json
{ "status": "moving", "camera_id": "LIVING_ROOM", "label": "LIVING_ROOM",
  "pan": 0.0, "tilt": 0.6, "auto_stop_ms": 2000 }
```

Note `"LIVING ROOM"` (with a space) resolved to `LIVING_ROOM`, and `label` in the
response is the **canonicalised** form — upper-cased, spaces → underscores.

### Frontend — hold to move, tap to recentre

```js
const ptz = (body) =>
  fetch(`${API_BASE}/api/v1/cameras/ptz`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edgeId, cameraLabel, ...body }),
  }).then(r => r.ok ? r.json() : r.json().then(j => Promise.reject(j)));

// A direction button: nudge on tap, repeat while held. NOTHING on release —
// the edge stops the camera itself.
let held = null;
btn.onpointerdown = () => {
  const nudge = () => ptz({ action: "move", pan: -0.6, durationMs: 600 });
  nudge();
  held = setInterval(nudge, 400);          // each call re-arms the auto-stop
};
btn.onpointerup = btn.onpointerleave = () => clearInterval(held);

// "Recentre" button, and/or when the user closes the camera view:
recentreBtn.onclick = () => ptz({ action: "revert" });
```

Re-send the move every ~400–500 ms while held; each one re-arms the auto-stop, so
motion is continuous. Do **not** send one per animation frame.

### Backend proxy (all it has to do)

```
POST  /homes/{homeId}/cameras/{label}/ptz          (your public API)
  └─► POST https://edgeai.ceravishealth.in/{edge_id}/api/v1/cameras/ptz
      body: { edgeId: <edge_id>, cameraLabel: <label>, action, pan, tilt, durationMs }
```

No headers to inject, no secret to hold, no redirect to follow. The only thing
the proxy must get right is putting the home's `edge_id` **in the URL path** and
**in the body**.

---

## 6. Suggested control mapping (what the UI should send)

| Control | action | pan | tilt | durationMs |
|---|---|---|---|---|
| ◀ pan left | `move` | `-0.6` | `0` | `300`–`800` |
| ▶ pan right | `move` | `0.6` | `0` | `300`–`800` |
| ▲ tilt up | `move` | `0` | `0.6` | `300`–`800` |
| ▼ tilt down | `move` | `0` | `-0.6` | `300`–`800` |
| ↩ recentre / view closed | `revert` | — | — | — |
| release of a held button | *nothing — it stops itself* | | | |
| ＋ / － zoom | *nothing — client-side digital zoom* | | | |

Velocity is speed, not distance: `0.6` for a responsive nudge, `0.2`–`0.3` for
fine framing.

---

## 7. Responses & errors

| Status | Body / detail | Cause | What to do |
|---|---|---|---|
| `200` | `{"status":"moving", …, "auto_stop_ms":N}` | Move accepted. | Show the pad as active. |
| `200` | `{"status":"reverted","reverted":true}` | Camera drove back. | Normal. |
| `200` | `{"status":"unchanged","reverted":false}` | Nothing to revert. | Normal — not an error. |
| `400` | `action must be 'move' or 'revert' — a move stops itself, so there is no stop to send` | A `stop` (or unknown action) was sent. | Drop the stop call. |
| `400` | `a move needs a non-zero pan or tilt` | Zero-velocity move. | Send a real velocity, or `revert`. |
| `400` | `'pan' must be a number between -1 and 1` / `'durationMs' must be a number of milliseconds` | Non-numeric field. | Fix the payload. |
| `400` | `camera 'X' has no PTZ` | `ptz_supported` false or no `onvif_xaddr` (added manually). | Hide the pad for that camera; re-add it via ONVIF discovery to enable PTZ. |
| `401` | `edgeId required` | No `edgeId` on a provisioned device. | Backend bug — always send it. |
| `404` | `no camera for label 'X'` | Label matches no `camera_name` / `room_name` / `camera_id`. | Use a label from `GET /api/v1/cameras`. |
| `409` | `edge_id mismatch: …` | Wrong house addressed. | Fix the home→edge_id mapping. |
| `502` | `PTZ failed: <onvif error>` / `PTZ revert failed: …` | Camera unreachable, bad ONVIF credentials, or it rejected the command. | Transient — retry once; if it persists, check the camera on the setup page. |
| `422` | FastAPI validation | The body wasn't a JSON object. | Fix the payload. |

FastAPI error bodies are `{"detail": "<message>"}`.

**Hide the pad, don't discover it by 400:** `GET /api/v1/cameras` returns each
camera's `ptz_supported`; render pan/tilt only when it's `true`.

---

## 8. Observability — every hit is visible

Each PTZ request (accepted **and** rejected) lands in two places:

- **App log** (`journalctl -u ceravis`):
  `PTZ KITCHEN — move pan=-0.40 tilt=+0.00 300ms`,
  `PTZ KITCHEN — revert`,
  `PTZ KITCHEN — rejected: action must be 'move' or 'revert' …`.
- **Monitor → Cloud Sync Console** — a `ptz` entry with the ok/fail flag, the
  HTTP status and the same label text.

So "did the app's button reach the device, and what did the camera say" is
answerable without guessing.

---

## 9. The LAN-only sibling endpoint (do not use from the cloud)

```
POST /api/v1/cameras/{camera_id}/ptz
Body: { "pan": -1..1, "tilt": -1..1, "zoom": -1..1 }   or   { "action": "stop" }
```

This is the **installer's pad** on the setup page
([edge/static/cameras.html](edge/static/cameras.html)), addressed by raw
`camera_id`. Differences from the cloud endpoint, all deliberate:

- **No `edgeId` auth** — it is a LAN admin surface, behind the pages' login.
- **`zoom` IS honoured** (optical zoom via ONVIF) — it is the aiming tool.
- **It keeps `stop`, and has no auto-stop** — the browser holds the button, so a
  held move must not time out mid-aim.

It shares the same core, so a cloud `revert` also undoes an installer's nudge.
Backend integrations should use the label-based `/api/v1/cameras/ptz` exclusively.

---

## 10. Tuning (device side)

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `ptz_max_move_ms` | `PTZ_MAX_MOVE_MS` | `2000` | Hard ceiling on one move; `durationMs` is clamped to it and a missing duration becomes it. |

It lives in [edge/config/settings.py:157](edge/config/settings.py:157); override
per-device in `infra/env/jetson.env` or the machine-local `jetson.env`, then
`sudo systemctl restart ceravis`.

---

## 11. Copy-paste for each team

**Backend team**
> One proxy rule: `POST /api/v1/cameras/ptz` on the edge, reached at
> `https://edgeai.ceravishealth.in/<edge_id>/api/v1/cameras/ptz`. **No headers,
> no token** — the legacy `X-Ceravis-Control-Token` is removed. Put the home's
> `edge_id` in **both** the URL path (that is how the tunnel finds the house) and
> the JSON body as `edgeId` (that is the authentication). Body:
> `{edgeId, cameraLabel, action:"move"|"revert", pan:-1..1, tilt:-1..1, durationMs}`;
> camelCase or snake_case both work. **There is no `stop` action** — a move stops
> itself after `durationMs` (max 2 s), and sending `stop` returns `400`. `zoom` is
> not sent; it is client-side. Expect `200`
> `{status, camera_id, label, pan, tilt, auto_stop_ms}` for a move and
> `{status, reverted}` for a revert; map `401/409` to "wrong device", `404` to
> "unknown camera", `400` to "bad request / no PTZ", `502` to "camera unreachable".

**Frontend / mobile team**
> Render the pan/tilt pad only for cameras whose `GET /api/v1/cameras` entry has
> `ptz_supported: true`. On press send
> `{action:"move", pan, tilt, durationMs:300–800}` (`±0.6` velocity; negative pan
> = left, negative tilt = down) and repeat every ~400 ms while held. **Send
> nothing on release** — the camera stops itself. Zoom buttons do **not** call the
> API; zoom the video element locally. Give the user a **↩ recentre** control that
> sends `{action:"revert"}`, and fire the same call when the camera view is
> closed, so the AI's framing of the recipient is restored the moment the user is
> done. `revert` is safe to send at any time: a camera that never moved answers
> `"unchanged"`.
