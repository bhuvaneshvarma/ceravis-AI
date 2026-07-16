# Recording playback — integration guide (event → footage)

How the ceravishealth frontend/backend connects an **alert or snapshot** to the
**recorded video** of that moment. Same shape and same authentication as the
Cloud PTZ controls, so if you have PTZ working this is a five-minute add.

---

## 1. What it does, in one line

Every time YOLO sees a person, the edge records that camera (compact 1080p
H.264, remux-only). When a caregiver taps **“Play footage”** next to an alert,
you send us the alert’s **timestamp** and we stream back the clip that **starts
15 seconds before** that moment — so they see the lead-up, not just the
aftermath. You keep asking for the next 15 seconds to continue playing.

Footage is kept for a **rolling 12 hours** per camera (only the parts where
someone was present). Older footage is gone, so “Play footage” only works for
events inside that window.

---

## 2. The call flow (identical to PTZ)

```
   Browser  ──►  ceravishealth backend  ──►  frp tunnel  ──►  edge device
 (taps Play)     (adds the secret token)     (secure)        (streams MP4 back)
```

- The **browser never holds the secret token.** It calls your backend.
- Your **backend** adds the `X-Ceravis-Control-Token` header and forwards the
  request to the edge through the same frp tunnel PTZ already uses.
- The **edge** streams the MP4 back.

---

## 3. The main endpoint — “give me the footage at this time”

```
GET  /api/v1/recordings/{camera_id}/at
```

**Headers**

| Header | Value | Notes |
|---|---|---|
| `X-Ceravis-Control-Token` | the shared secret | Same secret as PTZ (`edge_control_token`). Required only when the edge has it set (it is, in production). |

**Query parameters**

| Name | Example | Meaning |
|---|---|---|
| `ts` | `2026-07-16T20:00:05+05:30` | **The alert/snapshot timestamp**, exactly as you received it. |
| `pre` | `15` | Seconds of lead-up before `ts`. Default `15`. |
| `duration` | `15` | Length of this chunk in seconds. Default `15`. |
| `edge_id` | `home_1234` | Which home this is for (safety guard, same as PTZ). |

**What comes back:** a normal `video/mp4` stream you can drop into a `<video>`
element (via a blob) or a MediaSource buffer. Two response headers tell you what
you got:

| Response header | Meaning |
|---|---|
| `X-Clip-Start` | The exact instant this chunk starts (i.e. `ts − pre`). |
| `X-Clip-Duration` | The chunk length in seconds. |

**Example (what your backend sends to the edge):**

```bash
curl -H "X-Ceravis-Control-Token: THE_SECRET" \
  "https://EDGE_THROUGH_TUNNEL/api/v1/recordings/cam_001/at?ts=2026-07-16T20:00:05%2B05:30&pre=15&duration=15&edge_id=home_1234" \
  --output clip.mp4
```

> `%2B` is just `+` URL-encoded. Always URL-encode the `ts` value.

---

## 4. Continuing playback (“queue the next”)

To keep playing past the first 15 seconds, call the **same endpoint** with `ts`
moved **forward** by the duration you already played. Use `pre=0` for the
follow-on chunks (you only want the lead-up once, at the start):

```
1st chunk:  ts = 2026-07-16T20:00:05+05:30 , pre = 15    → plays 19:59:50 … 20:00:05
2nd chunk:  ts = 2026-07-16T20:00:05+05:30 , pre = 0     → plays 20:00:05 … 20:00:20
3rd chunk:  ts = 2026-07-16T20:00:20+05:30 , pre = 0     → plays 20:00:20 … 20:00:35
```

Simple rule: **next `ts` = previous `X-Clip-Start` + `X-Clip-Duration`.**
Fetch the next chunk slightly before the current one ends so playback is smooth.

---

## 5. “Is there footage to show?” (optional, recommended)

Before showing the Play button — or when a chunk returns empty — ask whether
footage exists around that instant:

```
GET  /api/v1/recordings/{camera_id}/window?ts=<ISO8601>&edge_id=home_1234
```

Returns:

```json
{
  "camera_id": "cam_001",
  "instant": "2026-07-16T20:00:05+05:30",
  "covered": true,
  "ranges": [ { "start": "…", "duration": 42.0 }, … ]
}
```

- `covered: true` → we have video at that moment; show/enable Play.
- `covered: false` → nobody was recorded then, or it’s older than 12 hours;
  grey out Play.
- `ranges` → every recorded stretch for this camera, so you can build a scrubber
  and know when the continuation will run out.

---

## 6. About the timestamp (important, and simple)

The **whole edge runs on one clock: the device’s local time.** Alerts,
snapshots and recordings are all stamped on it. So:

- **Send `ts` back exactly as you received it on the alert/snapshot.** Don’t
  convert it. If it has a timezone offset (e.g. `+05:30` or `Z`), we honour it;
  if it has none, we read it as the device’s local time. Either way it lands on
  the right footage.

---

## 7. Responses & errors

| Status | Meaning | What to do |
|---|---|---|
| `200` + `video/mp4` | Here’s the clip. | Play it. |
| `400` | `ts` wasn’t a valid date. | Fix the timestamp format. |
| `401` | Missing/wrong control token. | Backend must send `X-Ceravis-Control-Token`. |
| `404` | No footage at that time. | Older than 12 h, or nobody was recorded — show “no footage”. |
| `409` | `edge_id` didn’t match this home. | You addressed the wrong device. |
| `503` | Recording backbone is down. | Transient — retry; alert ops if it persists. |

---

## 8. What to tell each team (copy-paste)

**Backend team**
> Add a “play recording” proxy endpoint. It takes a `camera_id`, the alert
> `timestamp`, and the `home/edge_id`, then calls the edge at
> `GET /api/v1/recordings/{camera_id}/at?ts=<timestamp>&pre=15&duration=15&edge_id=<id>`
> through the existing frp tunnel, adding the header
> `X-Ceravis-Control-Token: <the same secret we use for PTZ>`. Stream the
> `video/mp4` response straight back to the app, and pass through the
> `X-Clip-Start` / `X-Clip-Duration` headers. For continuation, the app will
> call again with `ts` advanced; just forward it. Never expose the token to the
> browser.

**Frontend team**
> On an alert/snapshot, add a “Play footage” button. On tap, call the backend
> proxy with the alert’s `camera_id` and its `timestamp` (send it unchanged).
> You’ll get an MP4 — play it in a `<video>` (from a blob) or via MediaSource.
> When it’s ~2 s from the end, request the next chunk: same call, but `ts` =
> previous `X-Clip-Start` + `X-Clip-Duration`, and `pre=0`. Stop when the backend
> returns 404 (footage ran out). Optionally call `/window?ts=…` first to decide
> whether to show the button at all.
