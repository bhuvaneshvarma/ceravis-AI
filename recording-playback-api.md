# Recording playback — integration guide

How the ceravishealth app plays back **recorded footage** with a real review
player (pause, scrub, seek) and a CCTV-style timeline. Same authentication as the
Cloud PTZ controls, so if PTZ works this is a short add.

There are exactly **two endpoints** and one recording rule to know.

---

## 1. What it does, in one line

Every time YOLO sees a person, the edge records that camera as **1080p H.264 +
AAC audio**, kept for a **rolling 12 hours**. To review it, the app asks the edge
**“where is there footage?”** (draws a timeline bar) and **“play from this
moment”** (gets a seekable video link). That's it.

Because recording is person-triggered, most of the 12 h is empty — you only ever
store and see the stretches where someone was actually present.

---

## 2. The connection (identical to PTZ)

```
   Browser  ──►  vite proxy / backend  ──►  frp tunnel  ──►  edge device
                 (adds the 2 secrets)       (secure)         (answers)
```

The browser never holds the secrets. For **now**, the vite dev server injects
them on every `/api` call, so app code stays clean:

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
          Authorization: "Basic " + Buffer.from("admin:YOUR_FRP_PASSWORD").toString("base64"),
          // edge control token — the SAME secret PTZ uses (edge_control_token)
          "X-Ceravis-Control-Token": "YOUR_EDGE_TOKEN",
        },
      },
    },
  },
};
```

> Dev-only: the proxy exists only while `npm run dev` runs. In production the
> backend adds the same two headers instead. **The URLs never change.**

**Addressing a camera:** the `{camera}` slot takes the SAME label as PTZ —
`KITCHEN`, `BEDROOM`, `LIVING_ROOM`, `LOUNGE`, … (case-insensitive) — or the raw
`camera_id`. `edge_id` (the home, e.g. `home_1234`) is the same value PTZ uses.

---

## 3. Endpoint A — the timeline (where footage exists)

```
GET  /api/v1/recordings/{camera}/timeline?edge_id={homeId}
```

**Sample call**
```
GET /api/v1/recordings/KITCHEN/timeline?edge_id=home_1234
```

**Sample response**
```json
{
  "camera_id": "cam_001",
  "now":            "2026-07-16T20:00:00+05:30",
  "window_start":   "2026-07-16T08:00:00+05:30",
  "retention_hours": 12,
  "recorded_seconds": 5520.0,
  "segments": [
    { "start": "2026-07-16T09:15:00+05:30", "end": "2026-07-16T09:47:00+05:30", "seconds": 1920.0 },
    { "start": "2026-07-16T14:30:00+05:30", "end": "2026-07-16T15:30:00+05:30", "seconds": 3600.0 }
  ]
}
```

**How to use it:** draw a bar spanning `window_start` → `now`. Shade each
`segments[i]` block. Those shaded blocks are the only places with video and the
only clickable spots. Re-fetch it when the player opens (and poll every ~30 s) so
it always reflects the rolling window — the oldest ages out, the newest appears.

---

## 4. Endpoint B — the playback link (seekable, auto-continuing)

```
GET  /api/v1/recordings/{camera}/playback.m3u8?ts={ISO8601}&edge_id={homeId}
```

**Sample call**
```
GET /api/v1/recordings/KITCHEN/playback.m3u8?ts=2026-07-16T14:30:00+05:30&edge_id=home_1234
```

**Sample response** — a `307` redirect to the generated playlist:
```http
HTTP/1.1 307 Temporary Redirect
Location: /api/v1/recordings/hls/9f3a1c7b2e5d4a80/index.m3u8
```
Your HLS player follows that automatically and streams the video. You never
touch the `hls/...` URL yourself.

**How to use it** — hand the clicked time to an HLS player:
```js
import Hls from "hls.js";                     // ~100 KB; Safari/iOS need nothing

const url = `/api/v1/recordings/KITCHEN/playback.m3u8?ts=${clickedTs}&edge_id=home_1234`;

if (video.canPlayType("application/vnd.apple.mpegurl")) {
  video.src = url;                            // Safari / iOS — native HLS
} else {
  const hls = new Hls();                      // Chrome / Firefox / Android
  hls.loadSource(url);
  hls.attachMedia(video);
}
// The player now has pause + a scrub bar + seek. It fetches every segment
// ITSELF — you make NO further calls. One link plays continuously.
```

- `ts` is sent **raw** — no `encodeURIComponent`; the edge repairs the offset’s
  `+` itself.
- Omit `ts` to play the camera’s **most recent** stretch.
- Optional `duration=<seconds>` caps how much footage the link spans (default
  covers everything from `ts` onward).

**Two different bars, don’t confuse them:** the *timeline bar* (Endpoint A) shows
**where** footage is across the whole 12 h. The *player’s own scrubber* seeks
**within** the footage currently playing. Both are useful; they’re different axes.

> First `playback.m3u8` for a given time takes ~1 s (the edge is packaging the
> footage); reloads are instant. Gaps between recorded stretches are skipped —
> the video jump-cuts to the next time someone was present.

---

## 5. Where the video lives, and what it costs

- Recordings live **only on the edge device’s disk** — never uploaded to the
  cloud, S3, or the app server.
- A playback link isn’t a stored file; it’s a **question** (“this camera, this
  time”). The edge packages the footage on demand with a **copy-only** step (no
  re-encoding — near-zero CPU), caches it briefly, and auto-cleans it.
- **Cloud cost:** storage = zero. The only cost is the EC2 tunnel relaying bytes
  **while someone is watching** (~2 Mbps ≈ 1 GB/hour ≈ $0.09 egress). Watching
  from inside the home LAN touches the cloud not at all.

---

## 6. The timestamp rule (prevents every “wrong footage” bug)

The whole edge runs on **one clock: the device’s local time** — alerts,
snapshots and recordings all share it. So **send `ts` back exactly as you got it**
on the alert/snapshot. Don’t convert it. With an offset (`+05:30` or `Z`) we
honour it; with none we read it as device-local. Either way it lands right.

---

## 7. Responses & errors

| Status | Meaning | What to do |
|---|---|---|
| `200` / `307` | Timeline JSON / redirect to the playlist. | Normal. |
| `400` | `ts` wasn’t a valid date. | Fix the timestamp string. |
| `401` | Missing/wrong control token. | The proxy/backend must send `X-Ceravis-Control-Token`. |
| `404` | No footage there (older than 12 h, or nobody present). | Show “no footage”. |
| `409` | `edge_id` didn’t match this home. | Wrong device addressed. |
| `503` | Recording backbone / ffmpeg down. | Transient — retry; alert ops if it persists. |

---

## 8. What to tell each team (copy-paste)

**Backend team**
> Add two proxy endpoints that forward to the edge through the existing frp
> tunnel, adding the header `X-Ceravis-Control-Token: <same secret as PTZ>`:
> (1) `GET /api/v1/recordings/{cameraLabel}/timeline?edge_id=<id>` → return the
> JSON as-is. (2) `GET /api/v1/recordings/{cameraLabel}/playback.m3u8?ts=<t>&edge_id=<id>`
> → this returns a 307 redirect and then playlist + `.ts` segments under
> `/api/v1/recordings/hls/...`; just proxy that whole path through (follow
> redirects, stream bytes). Never expose the token to the browser.

**Frontend team**
> Build a review panel with two pieces. (1) Call `…/timeline` and draw a bar from
> `window_start`→`now`, shading each `segments[i]`. (2) When the user clicks a
> shaded spot, load `…/playback.m3u8?ts=<that time>&edge_id=<home>` into an HLS
> player (`<video>` src on Safari/iOS, else `hls.js`). You get pause + scrub +
> seek for free and the player streams continuously — no per-segment calls. Send
> the timestamp raw (no encoding). That’s the whole feature.
