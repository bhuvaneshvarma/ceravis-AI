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

Clips are **video-only by design**: the cameras’ G.711/PCM audio can’t be stored
in MP4 in a way every player understands (it lands as the 2023-era `ipcm` box,
which older players and some browsers reject outright), so the recording stream
carries no audio. Live view keeps its audio — this affects recordings only.

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

## 2b. Quick start for NOW — frontend-only (vite dev proxy)

While the backend proxy isn’t built yet, the frontend can talk to the edge
directly: the **vite dev server** injects both secrets on every `/api` call, so
browser code never touches them and a plain `<video src="/api/…">` just works.

```js
// vite.config.js
export default {
  server: {
    proxy: {
      "/api": {
        target: "http://44.204.108.154:8000",   // frp vhostHTTPPort (edge via tunnel)
        changeOrigin: true,
        headers: {
          // frp tunnel Basic-Auth (httpUser/httpPassword from frpc.toml)
          Authorization:
            "Basic " + Buffer.from("admin:YOUR_FRP_PASSWORD").toString("base64"),
          // edge control token — the SAME secret PTZ uses (edge_control_token)
          "X-Ceravis-Control-Token": "YOUR_EDGE_TOKEN",
        },
      },
    },
  },
};
```

The playback “request” — it’s a GET that returns video, so the fields ride in
the URL. **Only two fields are ever needed: the camera label and the
timestamp.** The 15 s lead-up (`pre`) and the 15 s chunk length (`duration`)
are edge-side defaults — don’t send them.

```js
const req = {
  camera: "KITCHEN",          // same label as PTZ cameraLabel (or camera_id)
  ts:     alert.timestamp,    // the alert/snapshot time — send it RAW, unchanged
  edgeId: "home_1234",        // same as PTZ edgeId
};

// no encoding needed — send the timestamp raw; the edge repairs the '+' itself
videoEl.src = `/api/v1/recordings/${req.camera}/at?ts=${req.ts}&edge_id=${req.edgeId}`;
```

Queueing the next chunk is one rule: **add 15 seconds to `ts` and call again**;
play the clips back-to-back. Stop on 404 — the footage ran out.

> ⚠ Dev-only: this works because the vite dev server is there to inject the
> headers. A production build has no dev server — that’s when the backend proxy
> (section 2) takes over. The URLs stay identical; app code doesn’t change.

---

## 3. The main endpoint — “give me the footage at this time”

```
GET  /api/v1/recordings/{camera}/at
```

**`{camera}` takes the SAME label as PTZ** — `KITCHEN`, `BEDROOM`,
`LIVING_ROOM`, `LOUNGE`, … (case-insensitive, spaces or underscores work) — or
the raw `camera_id`. It matches the camera’s name, its room, or its id, exactly
like the PTZ `cameraLabel`.

**Headers**

| Header | Value | Notes |
|---|---|---|
| `X-Ceravis-Control-Token` | the shared secret | Same secret as PTZ (`edge_control_token`). Required only when the edge has it set (it is, in production). |

**Query parameters**

| Name | Example | Meaning |
|---|---|---|
| `ts` | `2026-07-16T20:00:05+05:30` | **The alert/snapshot timestamp**, exactly as you received it — raw, no URL-encoding needed. |
| `edge_id` | `home_1234` | Which home this is for (safety guard, same as PTZ). |
| `pre` | *(omit)* | Optional. Lead-up seconds before `ts`. **Defaults to 15 on the edge.** |
| `duration` | *(omit)* | Optional. Chunk length in seconds. **Defaults to 15.** |

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
  "https://EDGE_THROUGH_TUNNEL/api/v1/recordings/KITCHEN/at?ts=2026-07-16T20:00:05+05:30&edge_id=home_1234" \
  --output clip.mp4
```

> Send the timestamp **raw** — the edge knows URL decoding turns the offset’s
> `+` into a space and repairs it itself. (An encoded `%2B` works too.)

---

## 4. Continuing playback (“queue the next”)

One rule: **next `ts` = previous `ts` + 15 seconds.** Nothing else changes — you
never touch `pre` or `duration`. Because every call starts 15 s before its own
`ts`, the chunks tile perfectly, no gaps and no overlaps:

```
call 1:  ts = 20:00:05   → plays 19:59:50 … 20:00:05   (the lead-up to the event)
call 2:  ts = 20:00:20   → plays 20:00:05 … 20:00:20
call 3:  ts = 20:00:35   → plays 20:00:20 … 20:00:35
```

Fetch the next chunk a couple of seconds before the current one ends so playback
feels seamless. Stop when a call returns **404** — the footage ran out (nobody
was recorded after that, or it aged past the 12-hour window).

The `X-Clip-Start` / `X-Clip-Duration` response headers still say exactly what
each chunk covers if you want a timeline. And if you ever want fewer
round-trips, ask for bigger chunks (`duration=60`) and advance `ts` by that —
the playback server stitches the stored 15 s segments together for you.

---

## 5. “Is there footage to show?” (optional, recommended)

Before showing the Play button — or when a chunk returns empty — ask whether
footage exists around that instant:

```
GET  /api/v1/recordings/{camera}/window?ts=<ISO8601>&edge_id=home_1234
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

## 6b. Where the video actually lives, and what it costs

There are **no pre-made clip files and no pre-made URLs.** The URL is just a
question — “this camera, this time”. Nothing exists until someone asks:

1. Recordings live ONLY on the edge device’s own disk (`data/recordings/…`),
   as rolling 15 s segments. Nothing is ever uploaded to the cloud.
2. When a playback URL is hit, the edge cuts the requested time-slice out of
   those on-disk segments **at that moment** (a remux — no re-encoding, near
   zero CPU) and streams it out. When the response ends, nothing is left
   behind — no temp files, no clip library to manage.
3. Segments older than 12 hours delete themselves, so the disk never grows.

**Cost:** the cloud stores nothing (no S3, no uploads, no per-clip storage).
The only cloud cost is the EC2 tunnel relaying bytes WHILE someone is actually
watching: a 15 s chunk at ~2 Mbps ≈ 4 MB, so ~1 GB per hour of continuous
remote viewing (≈ $0.09 of AWS egress). Watching from inside the home LAN
(device IP directly) touches the cloud zero.

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
> Add a “play recording” proxy endpoint. It takes a `cameraLabel` (same values
> as PTZ: KITCHEN, BEDROOM, …), the alert `timestamp`, and the `home/edge_id`,
> then calls the edge at
> `GET /api/v1/recordings/{cameraLabel}/at?ts=<timestamp>&edge_id=<id>`
> through the existing frp tunnel, adding the header
> `X-Ceravis-Control-Token: <the same secret we use for PTZ>`. That’s the whole
> request — the 15 s lead-up and 15 s length are edge defaults. Stream the
> `video/mp4` response straight back to the app, and pass through the
> `X-Clip-Start` / `X-Clip-Duration` headers. For continuation, the app calls
> again with `ts` moved 15 s forward; just forward it. Never expose the token to
> the browser.

**Frontend team**
> On an alert/snapshot, add a “Play footage” button. On tap, call the backend
> proxy (or, for now, the vite-proxied URL in §2b) with the alert’s camera label
> (KITCHEN, …) and its `timestamp` — send the timestamp raw and unchanged, no
> encoding.
> You’ll get an MP4 — set it straight as a `<video>` src. When it’s ~2 s from
> the end, queue the next chunk: **same call with `ts` + 15 seconds**. Stop when
> you get 404 (footage ran out). Optionally call `/window?ts=…` first to decide
> whether to show the button at all.
