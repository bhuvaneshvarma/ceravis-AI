# Cloud PTZ — complete call manual

How the ceravishealth app (web **and** mobile) **pans and tilts a camera** in a
home, end to end: one endpoint, one JSON body, one auth rule.

```
   Browser / App  ──►  your backend  ──►  frp tunnel  ──►  edge device  ──►  ONVIF camera
                       (adds nothing)      (per-home URL)   (validates)      (ContinuousMove)
```

Source of truth: [edge/api/camera_routes.py:259](edge/api/camera_routes.py:259)
(`ptz_by_label`), [edge/api/control_auth.py](edge/api/control_auth.py),
[edge/onvif/client.py:350](edge/onvif/client.py:350).

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
{
  "edgeId":      "NrPq8...",      // REQUIRED — this home's edge_id (deviceToken)
  "cameraLabel": "KITCHEN",       // REQUIRED — which camera
  "action":      "move",          // "move" (default) | "stop"
  "pan":         -0.4,            // -1.0 .. 1.0   negative = left,  positive = right
  "tilt":        0.0,             // -1.0 .. 1.0   negative = down,  positive = up
  "durationMs":  300              // how long to move; clamped to 2000 ms
}
```

| Field | Aliases accepted | Type | Required | Default | Meaning |
|---|---|---|---|---|---|
| `edgeId` | `edge_id` | string | **yes** (when the device is provisioned) | — | Must equal THIS device's edge_id. The whole authentication. |
| `cameraLabel` | `camera_label`, `cameraNumber` | string | **yes** | `""` | Room label (`KITCHEN`, `LIVING_ROOM`, …) or the raw `camera_id`. Case-insensitive, spaces = underscores. |
| `action` | — | string | no | `"move"` | `"stop"` halts immediately. Anything else = move. |
| `pan` | — | number | no | `0` | Pan **velocity**, −1.0 … 1.0. Not an angle. |
| `tilt` | — | number | no | `0` | Tilt **velocity**, −1.0 … 1.0. Not an angle. |
| `durationMs` | `duration_ms` | number | no | `0` → ceiling | Auto-stop after this many ms. Clamped to `ptz_max_move_ms` (**2000 ms**). `0`/missing ⇒ the full 2000 ms. |
| ~~`zoom`~~ | — | — | — | — | **Ignored on purpose.** Zoom is client-side digital zoom in the player; the edge only drives pan/tilt. |

**camelCase and snake_case are interchangeable on every field** (`_field()` takes
the first non-null key), so the backend can send its natural shape.

### The two rules that decide move vs stop

```python
stop = (action == "stop") or not (pan or tilt)
```

1. `"action": "stop"` → **stop**, whatever pan/tilt say.
2. `pan == 0 and tilt == 0` → **stop**, even if `"action": "move"`.
   *So you cannot send a "move" with zero velocity — it is read as a stop.*

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

A move request, in order:

1. **Auth** — `check_edge_id(edgeId)`; rejection is logged to the Cloud Sync Console.
2. **Resolve the camera** — `cameraLabel` → camera, matched against
   `camera_name`, `room_name` or `camera_id` (`CameraConfig.get_by_label`, the
   ONE cloud-facing addressing rule; recording playback resolves identically).
3. **Capability check** — the camera must have `ptz_supported == true` **and** an
   `onvif_xaddr` (i.e. it was added via ONVIF discovery, not typed in by hand).
4. **Remember the framing** (`GetStatus`) — once per override session, *before*
   the camera moves, so it can return to where the AI had the recipient framed.
5. **Move** — ONVIF `ContinuousMove` with `Velocity{PanTilt x=pan y=tilt, Zoom 0}`
   on `onvif_ptz_token` (falling back to `onvif_profile_token`).
6. **Arm the auto-stop** — a `threading.Timer` fires `Stop` after the clamped
   duration. A new move on the same camera **cancels and replaces** the previous
   timer.
7. **Arm the idle-revert** — after `ptz_revert_secs` (**15 s**) with *no further
   PTZ request on that camera*, the edge issues `AbsoluteMove` back to the
   position captured in step 4. Every request (move **and** stop) re-arms this
   window.

### Why the command is self-terminating

The edge **always** auto-stops. If the network drops, the app crashes, or the
"stop" is lost, the motor still halts within 2 s — a lost command can never leave
a camera spinning. This is why `durationMs` is a ceiling, not a promise, and why
you do not *need* to send a stop for correctness (you should still send it for
feel — see §6).

### Why the camera drifts back after 15 s

The AI is locked on the recipient in software; a viewer's pan/tilt physically
moves the camera off them. The idle-revert returns the framing so tracking
resumes on its own. Best-effort: cameras that don't answer `GetStatus` or reject
`AbsoluteMove` simply never capture a home and never revert. Set
`PTZ_REVERT_SECS=0` to disable.

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
  "auto_stop_ms": 300
}
```

### Stop — release the button

```bash
curl -X POST https://edgeai.ceravishealth.in/NrPq8xxxxxxx/api/v1/cameras/ptz \
  -H 'Content-Type: application/json' \
  -d '{"edgeId":"NrPq8xxxxxxx","cameraLabel":"KITCHEN","action":"stop"}'
```

**200 response**
```json
{
  "status": "stopped",
  "camera_id": "KITCHEN",
  "label": "KITCHEN"
}
```

### Tilt up, no duration (falls back to the 2 s ceiling)

```bash
curl -X POST http://192.168.0.221:8000/api/v1/cameras/ptz \
  -H 'Content-Type: application/json' \
  -d '{"edgeId":"NrPq8xxxxxxx","cameraLabel":"LIVING ROOM","tilt":0.6}'
```

```json
{ "status": "moving", "camera_id": "LIVING_ROOM", "label": "LIVING_ROOM", "auto_stop_ms": 2000 }
```

Note `"LIVING ROOM"` (with a space) resolved to `LIVING_ROOM`, and `label` in the
response is the **canonicalised** form — upper-cased, spaces → underscores.

### Frontend — hold to move, release to stop

```js
const ptz = (body) =>
  fetch(`${API_BASE}/api/v1/cameras/ptz`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edgeId, cameraLabel, ...body }),
  }).then(r => r.ok ? r.json() : r.json().then(j => Promise.reject(j)));

// One button, e.g. "pan left"
btn.onpointerdown  = () => ptz({ action: "move", pan: -0.6, durationMs: 800 });
btn.onpointerup    = () => ptz({ action: "stop" });
btn.onpointerleave = () => ptz({ action: "stop" });
```

For a **held** button, re-send the move every ~500 ms while held (each one
re-arms the 2 s auto-stop) and send one `stop` on release. Do **not** send a move
per animation frame — one every few hundred ms is plenty and keeps the tunnel quiet.

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

| Control | pan | tilt | durationMs |
|---|---|---|---|
| ◀ pan left | `-0.6` | `0` | `300`–`800` |
| ▶ pan right | `0.6` | `0` | `300`–`800` |
| ▲ tilt up | `0` | `0.6` | `300`–`800` |
| ▼ tilt down | `0` | `-0.6` | `300`–`800` |
| release / stop | — | — | `action: "stop"` |
| ＋ / － zoom | — | — | **client-side digital zoom, no call** |

Velocity is speed, not distance: `0.6` for a responsive nudge, `0.2`–`0.3` for
fine framing. A tap = one short-duration move; a hold = repeated moves + a stop.

---

## 7. Responses & errors

| Status | Body / detail | Cause | What to do |
|---|---|---|---|
| `200` | `{"status":"moving", …, "auto_stop_ms":N}` | Move accepted. | Show the pad as active. |
| `200` | `{"status":"stopped", …}` | Stop accepted. | Normal. |
| `401` | `edgeId required` | No `edgeId` in the body on a provisioned device. | Backend bug — always send it. |
| `409` | `edge_id mismatch: request for 'X', this device is 'Y'` | Wrong house addressed. | Fix the home→edge_id mapping. |
| `404` | `no camera for label 'X'` | Label matches no `camera_name` / `room_name` / `camera_id`. | Use a label from `GET /api/v1/cameras`. |
| `400` | `camera 'X' has no PTZ` | `ptz_supported` false or no `onvif_xaddr` (added manually). | Hide the PTZ pad for that camera; re-add it via ONVIF discovery to enable PTZ. |
| `502` | `PTZ failed: <onvif error>` | Camera unreachable, bad ONVIF credentials, or it rejected the move. | Transient — retry once; if it persists, check the camera on the setup page. |
| `422` | FastAPI validation | Body wasn't a JSON object, or `pan`/`tilt`/`durationMs` weren't numeric. | Fix the payload. |

FastAPI error bodies are `{"detail": "<message>"}`.

**Hide the pad, don't discover it by 400:** `GET /api/v1/cameras` returns each
camera's `ptz_supported`; render pan/tilt only when it's `true`.

---

## 8. Observability — every hit is visible

Each PTZ request (accepted **and** rejected) lands in two places:

- **App log** (`journalctl -u ceravis`):
  `PTZ KITCHEN — move pan=-0.40 tilt=+0.00 300ms`,
  `PTZ KITCHEN — rejected: edgeId auth (this device is 'NrPq8…')`,
  `PTZ KITCHEN — reverted to the recipient's framing (idle)`.
- **Monitor → Cloud Sync Console** — a `ptz` entry with the ok/fail flag, the
  HTTP status and the same label text (`call_log.record("ptz", …)`).

So "did the app's button reach the device, and what did the camera say" is
answerable without guessing.

---

## 9. The LAN-only sibling endpoint (do not use from the cloud)

```
POST /api/v1/cameras/{camera_id}/ptz
Body: { "pan": -1..1, "tilt": -1..1, "zoom": -1..1 }   or   { "action": "stop" }
```

This is the **setup page's** pad ([edge/static/cameras.html](edge/static/cameras.html)),
addressed by raw `camera_id`. Differences from the cloud endpoint:

- **No `edgeId` auth** — it is a LAN admin surface.
- **`zoom` IS honoured** (optical zoom via ONVIF), because it's an installer tool.
- **No auto-stop timer** — the browser sends the stop on pointer-up. A lost stop
  here *does* keep the motor moving until the next command.
- Uses `onvif_profile_token` only.

It still shares the idle-revert. Backend integrations should use the label-based
`/api/v1/cameras/ptz` in §1 exclusively.

---

## 10. Tuning (device side)

Both live in [edge/config/settings.py:155](edge/config/settings.py:155); override
per-device in `infra/env/jetson.env` or the machine-local `jetson.local.env`:

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `ptz_max_move_ms` | `PTZ_MAX_MOVE_MS` | `2000` | Hard ceiling on one move; `durationMs` is clamped to it and a missing duration becomes it. |
| `ptz_revert_secs` | `PTZ_REVERT_SECS` | `15.0` | Idle seconds before returning to the pre-override framing. `0` disables the revert. |

Restart the service after changing either: `sudo systemctl restart ceravis`.

---

## 11. Copy-paste for each team

**Backend team**
> Add one proxy rule: `POST /api/v1/cameras/ptz` on the edge, reached at
> `https://edgeai.ceravishealth.in/<edge_id>/api/v1/cameras/ptz`. **No headers,
> no token** — the legacy `X-Ceravis-Control-Token` is removed. Put the home's
> `edge_id` in **both** the URL path (that is how the tunnel finds the house) and
> the JSON body as `edgeId` (that is the authentication). Body:
> `{edgeId, cameraLabel, action:"move"|"stop", pan:-1..1, tilt:-1..1, durationMs}`.
> camelCase or snake_case both work. `zoom` is not sent — zoom is client-side.
> Expect `200` with `{status, camera_id, label, auto_stop_ms}`; map `401/409` to
> "wrong device", `404` to "unknown camera", `400` to "camera has no PTZ", `502`
> to "camera unreachable".

**Frontend / mobile team**
> Render the pan/tilt pad only for cameras whose `GET /api/v1/cameras` entry has
> `ptz_supported: true`. On press send
> `{action:"move", pan, tilt, durationMs:300–800}` (`±0.6` velocity; negative pan
> = left, negative tilt = down), repeat every ~500 ms while held, and send
> `{action:"stop"}` on release. Zoom buttons do **not** call the API — zoom the
> video element locally. The edge auto-stops after 2 s no matter what, so a
> dropped stop is safe; and ~15 s after the user stops touching the pad the
> camera returns to its AI framing by itself — that drift-back is intended
> behaviour, not a bug.
