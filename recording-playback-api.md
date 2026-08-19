# Recorded playback & timeline — complete call manual

How the ceravishealth app (web **and** mobile) reviews **recorded footage**: what
exists (`timeline`) and how to play it back with a real review player — pause,
scrub, seek to any second, roll across gaps (`playback.m3u8`).

Same authentication as the Cloud PTZ call, so if PTZ works this is a short add.

```
   Browser / App  ──►  your backend  ──►  frp tunnel  ──►  edge device
                       (adds nothing)      (per-home URL)   (reads its own disk)
```

Source of truth: [edge/api/recording_routes.py](edge/api/recording_routes.py),
[edge/recording/index.py](edge/recording/index.py).

---

## 1. The calls

| Call | What it answers |
|---|---|
| `GET /api/v1/recordings/timeline` | **Where footage exists, for EVERY camera — one call.** |
| `GET /api/v1/recordings/{camera}/timeline` | The same, for one camera. |
| `GET /api/v1/recordings/{camera}/playback.m3u8` | One seekable playlist for that camera's whole window. |
| `GET /api/v1/recordings/{camera}/segment/{file}` | One stored clip (the player fetches these itself). |

| Where you call from | Base |
|---|---|
| **Cloud / backend (the real one)** | `https://edgeai.ceravishealth.in/<edge_id>/api/v1/recordings/…` |
| **Inside the home LAN** | `http://<jetson-ip>:8000/api/v1/recordings/…` |

The `/<edge_id>` prefix is how the fleet tunnel picks the house — **frps routes by
URL only**. The edge strips its own prefix internally, so the path it serves is
identical in both rows.

**One idea makes the whole thing work: the recording carries its own wall-clock
time, so the player seeks by _date_.**

---

## 2. What it does, in one line

Every time the AI sees a person, the edge records that camera as **1080p H.264 +
AAC audio** in **15-second segments**, kept for a **rolling 12 hours**. To review
it, the app asks **"where is there footage?"** (draws the bars) and loads **one
playlist per camera** into a video player, which then **seeks to any moment by
date** — all client-side, no more calls.

Because recording is person-triggered, most of the 12 h is empty — you only store
and see the stretches where someone was actually present. The player jumps across
the empty gaps on its own.

---

## 3. Authentication — the edgeId match, nothing else

Every call carries this device's `edgeId` as the **`edge_id` query parameter**,
and the edge verifies it MATCHES the value provisioned for it (the `deviceToken`
from `userDetails`). Only the app server that provisioned the device knows it, so
**the match IS the authentication** ([edge/api/control_auth.py](edge/api/control_auth.py)).

```
…/api/v1/recordings/timeline?edge_id=NrPq8xxxxxxx
```

| Device state | `edge_id` sent | Result |
|---|---|---|
| Provisioned | matches | proceed |
| Provisioned | missing | **401** `edgeId required` |
| Provisioned | different | **409** `edge_id mismatch: …` |
| Not provisioned (LAN dev box) | anything | accepted |

> **There is NO `X-Ceravis-Control-Token`.** That header was removed for good —
> if an older note told you to inject it, delete that rule. There is no header to
> add on any of these calls, which is what makes the playlist and its segments
> playable by a native player that cannot attach headers.

---

## 4. Timeline — ALL cameras in one call

```
GET /api/v1/recordings/timeline?edge_id={edgeId}
```

| Parameter | In | Type | Required | Meaning |
|---|---|---|---|---|
| `edge_id` | query | string | yes (provisioned device) | This home's edge_id. |

That is the entire request — **no camera parameter**. Dropping the camera from
the path is what switches the endpoint into all-cameras mode; everything else
(auth, window, segment shape) is identical to the single-camera form.

### Sample call

```bash
curl "https://edgeai.ceravishealth.in/NrPq8xxxxxxx/api/v1/recordings/timeline?edge_id=NrPq8xxxxxxx"
```

### Sample response — `200`

```json
{
  "now":             "2026-08-19T20:00:00+05:30",
  "window_start":    "2026-08-19T08:00:00+05:30",
  "retention_hours": 12,
  "camera_count":    3,
  "recorded_seconds": 7320.0,
  "cameras": [
    {
      "camera_id": "BEDROOM",
      "label":     "BEDROOM",
      "recorded_seconds": 0.0,
      "segments": []
    },
    {
      "camera_id": "KITCHEN",
      "label":     "KITCHEN",
      "recorded_seconds": 3720.0,
      "segments": [
        { "start": "2026-08-19T09:15:00+05:30", "end": "2026-08-19T09:47:00+05:30", "seconds": 1920.0 },
        { "start": "2026-08-19T18:30:00+05:30", "end": "2026-08-19T19:00:00+05:30", "seconds": 1800.0 }
      ]
    },
    {
      "camera_id": "LIVING_ROOM",
      "label":     "LIVING ROOM",
      "recorded_seconds": 3600.0,
      "segments": [
        { "start": "2026-08-19T08:00:00+05:30", "end": "2026-08-19T08:30:00+05:30", "seconds": 1800.0 },
        { "start": "2026-08-19T18:00:00+05:30", "end": "2026-08-19T18:30:00+05:30", "seconds": 1800.0 }
      ]
    }
  ]
}
```

### Every field

| Field | Meaning |
|---|---|
| `now` | The edge's clock at the instant it answered. The right edge of every bar. |
| `window_start` | `now − retention_hours`. The left edge. Anything older is deleted. |
| `retention_hours` | How far back footage is kept (**12**). |
| `camera_count` | How many cameras are configured on this device. |
| `recorded_seconds` (top level) | Total footage across all cameras in the window. |
| `cameras[]` | **One entry per configured camera, sorted by `camera_id`** — including cameras with no footage. |
| `cameras[].camera_id` | The camera's id (`LIVING_ROOM`). |
| `cameras[].label` | The addressing label — **pass this back** as `{camera}` to playback/snapshot, or as `cameraLabel` to PTZ. |
| `cameras[].recorded_seconds` | Total footage for that camera in the window. |
| `cameras[].segments[]` | The stretches with footage, oldest first: `start`, `end` (ISO-8601, edge-local) and `seconds`. |
| `cameras[].error` | **Only present when that camera's storage could not be read.** Its `segments` is empty because we *don't know*, not because nobody was home. |

**The window is stated once, not per camera** — every camera in one response is
measured against the same `now`, so the bars line up exactly.

### The two guarantees worth knowing

1. **A camera with no footage is still listed** (empty `segments`). So this one
   response is also the complete "what can I review right now" answer — you never
   need a second call to discover a camera exists.
2. **One broken camera never fails the call.** It comes back with `error` set
   while every other camera answers normally. Show that row as *unavailable*, not
   as *empty* — the difference matters when someone is checking on a relative.

---

## 5. Timeline — ONE camera

```
GET /api/v1/recordings/{camera}/timeline?edge_id={edgeId}
```

| Parameter | In | Type | Required | Meaning |
|---|---|---|---|---|
| `camera` | path | string | yes | `camera_id` **or** the label (`KITCHEN`, `LIVING ROOM`, `living_room` — case-insensitive, spaces = underscores). |
| `edge_id` | query | string | yes (provisioned device) | This home's edge_id. |

```bash
curl "https://edgeai.ceravishealth.in/NrPq8xxxxxxx/api/v1/recordings/LIVING_ROOM/timeline?edge_id=NrPq8xxxxxxx"
```

```json
{
  "now":             "2026-08-19T20:00:00+05:30",
  "window_start":    "2026-08-19T08:00:00+05:30",
  "retention_hours": 12,
  "camera_id":       "LIVING_ROOM",
  "label":           "LIVING ROOM",
  "recorded_seconds": 3600.0,
  "segments": [
    { "start": "2026-08-19T08:00:00+05:30", "end": "2026-08-19T08:30:00+05:30", "seconds": 1800.0 },
    { "start": "2026-08-19T18:00:00+05:30", "end": "2026-08-19T18:30:00+05:30", "seconds": 1800.0 }
  ]
}
```

Identical to one `cameras[]` entry above, with the window fields flattened
alongside it. **Unchanged from before** — every previous key is still there;
`label` was added so both forms describe a camera the same way.

**Which to use:** the all-cameras call for a review screen or any multi-camera
view (one request instead of N); the per-camera call when the user is already
inside one camera.

### How to draw the bar

Draw a rail from `window_start` → `now`, then shade each `segments[i]`. Those
shaded blocks are the only places with video and the only clickable spots. A
click maps to a wall-clock instant → `seekToDate(...)` in the player (§8).

**Call it when the screen opens — do not poll it hard.** Once a player holds the
playlist, the playlist itself keeps the bar current for free (§7). A slow refresh
(~30 s) of the all-cameras call is fine for a multi-camera overview where no
player is running.

---

## 6. Playback — one seekable playlist per camera

```
GET /api/v1/recordings/{camera}/playback.m3u8?edge_id={edgeId}
```

| Parameter | In | Type | Required | Meaning |
|---|---|---|---|---|
| `camera` | path | string | yes | Same addressing as the timeline. |
| `edge_id` | query | string | yes (provisioned device) | Also copied onto every segment URI, so segment fetches authenticate themselves. |
| `ts` | query | ISO-8601 | no | The instant of interest (e.g. an alert). Accepted and ignored for selection — the whole window is served and the client seeks. |

Returns **one HLS playlist for the entire retention window** (`200`,
`application/vnd.apple.mpegurl` — no redirect):

```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:15
#EXT-X-MEDIA-SEQUENCE:119095844        ← where this window starts in the stream
#EXT-X-DISCONTINUITY-SEQUENCE:0        ← how many gaps already scrolled past it
#EXT-X-PROGRAM-DATE-TIME:2026-08-19T18:00:00+05:30     ← the wall-clock anchor
#EXTINF:15.000,
segment/2026-08-19_18-00-00-000000.ts?edge_id=NrPq8xxxxxxx
#EXTINF:15.000,
segment/2026-08-19_18-00-15-000000.ts?edge_id=NrPq8xxxxxxx
#EXT-X-DISCONTINUITY                                    ← nobody was present here
#EXT-X-PROGRAM-DATE-TIME:2026-08-19T18:30:00+05:30     ← next stretch re-anchored
#EXTINF:15.000,
segment/2026-08-19_18-30-00-000000.ts?edge_id=NrPq8xxxxxxx
                                        ← and NO #EXT-X-ENDLIST, ever
```

Four things make this work:

1. **`EXT-X-PROGRAM-DATE-TIME`** = the real time of each stretch. This is what
   lets the player **seek to an exact instant by date** and show true wall-clock
   time on the scrubber.
2. **`EXT-X-DISCONTINUITY`** between stretches = the player **rolls across the
   empty gaps by itself**, in the same stream, with **no new call**.
3. **No `EXT-X-ENDLIST` — ever.** That tag would mean "this recording is
   finished". An NVR archive never is, so every HLS player re-fetches this URL by
   itself, forever, and **footage recorded after the link was opened appears in
   the already-loaded player automatically** (§7).
4. **`EXT-X-MEDIA-SEQUENCE` / `EXT-X-DISCONTINUITY-SEQUENCE`** = the stream's own
   numbering, so when the oldest footage ages out the player knows exactly what
   was dropped. Ignore both numbers — never cache or rewrite them.

Segment URIs are **relative**, so they resolve against the playlist's own URL and
inherit the `/<edge_id>/api/…` prefix automatically. There is no
`/recordings/hls/...` path — if an old note mentions one, ignore it.

---

## 7. It keeps itself current — you do not re-request it

| When | What happens | Calls your app makes |
|---|---|---|
| A new 15 s clip is recorded | The player's own reload appends it to the same timeline | **none** |
| Recording resumes after an hour of nobody | Same, with an `EXT-X-DISCONTINUITY` before it | **none** |
| Footage passes 12 h and expires | It drops off the front; the player follows via the sequence numbers | **none** |
| The user drags to another time | Client-side `seekToDate(...)` | **none** |

**Why:** a playlist with no `EXT-X-ENDLIST` is "still growing" to every HLS
player, so hls.js, AVPlayer and ExoPlayer all re-request it on a timer (~7–15 s)
out of the box. Load the URL once and leave the player alone.

**Those reloads are nearly free.** The body carries a strong `ETag` and
`Cache-Control: no-cache`; a reload with nothing new gets **`304 Not Modified`,
no body**. A viewer parked on a camera all day costs a few hundred bytes a minute.

> **Backend requirement (one line):** the proxy must pass `If-None-Match`,
> `ETag` and `Cache-Control` through untouched, and must **never cache the
> `.m3u8` body**. Cache the manifest and every viewer freezes at the moment of
> the first cache fill — the one way to break this.

The only case needing another call is a camera with **no footage at all** when
opened: that is a `404`, and there is no playlist to reload. Retry on a slow timer
(~30 s), or wait until the timeline first reports footage; from that moment the
player takes over for good.

### Drawing the bar with no polling at all

The playlist the player is already reloading contains the same information the
timeline gives. Every fragment carries its wall-clock time, so consecutive
fragments merge into exactly the recorded stretches:

```js
hls.on(Hls.Events.LEVEL_LOADED, (_, data) => {
  const runs = []; let cur = null;
  for (const f of data.details.fragments) {
    if (f.programDateTime == null) continue;
    const end = f.programDateTime + f.duration * 1000;
    if (cur && Math.abs(f.programDateTime - cur.end) <= 1500) cur.end = end;
    else runs.push(cur = { start: f.programDateTime, end });   // gap -> new stretch
  }
  drawBar(runs);                       // same shape as timeline's segments[]
});
```

iOS/Android expose the same data: `AVPlayerItem.seekableTimeRanges` +
`currentDate()`, and ExoPlayer's `HlsMediaPlaylist.segments`.

---

## 8. How to hit it — load once, then seek by date

### Web (hls.js — Chrome/Firefox/Android browsers; also desktop Safari)

```js
import Hls from "hls.js";

const url = `${BASE}/api/v1/recordings/${cameraLabel}/playback.m3u8?edge_id=${edgeId}`;

let ready = false, pending = null;
const hls = new Hls({ backBufferLength: 90 });
hls.loadSource(url);
hls.attachMedia(videoEl);
hls.on(Hls.Events.LEVEL_LOADED, () => {
  if (ready) return;                            // only the first load
  ready = true;
  if (pending != null) { seekToDate(pending); pending = null; }
  else jumpToLatest();                          // default view = newest footage
});

// Seek to a wall-clock instant (ms since epoch) using PROGRAM-DATE-TIME.
function seekToDate(ms) {
  if (!ready) { pending = ms; return; }
  const frags = hls.levels[hls.currentLevel]?.details?.fragments || [];
  let t = null;
  for (const f of frags) {
    if (f.programDateTime == null) continue;
    if (ms >= f.programDateTime && ms < f.programDateTime + f.duration * 1000) {
      t = f.start + (ms - f.programDateTime) / 1000; break;
    }
  }
  if (t == null) {                              // in a gap -> snap to next footage
    const later = frags.find(f => f.programDateTime >= ms);
    t = later ? later.start : 0;
  }
  videoEl.currentTime = Math.max(0, t);
  videoEl.play();
}

// Default view: open the newest recorded STRETCH from its START — use the last
// segments[i].start from the timeline call.
function jumpToLatest() {
  if (latestSegmentStartMs) seekToDate(latestSegmentStartMs + 200);
}
// The real time on screen: hls.playingDate -> a Date, exact, even across gaps.
```

> **Mistake to avoid #1:** never open playback at the very end. On a camera that
> isn't recording at that instant the end *is* the last frame — the player stops
> immediately and it looks broken. Always seek to a real moment (the alert time,
> or the start of the newest stretch).
>
> **Mistake to avoid #2:** do **not** re-create the player or re-call
> `loadSource()` to "get the new clips". The player already does that (§7), and
> tearing it down throws away the user's position and buffer.

### iOS (AVPlayer)

```swift
let item = AVPlayerItem(url: playbackURL)          // same playback.m3u8 URL
let player = AVPlayer(playerItem: item)
// when the user taps 18:30:15 on the timeline:
let target = ISO8601DateFormatter().date(from: "2026-08-19T18:30:15+05:30")!
item.seek(to: target, completionHandler: nil)      // works because of PROGRAM-DATE-TIME
```

No custom networking is needed: the `edge_id` travels in the URL, so AVPlayer
fetches the manifest and every segment on its own.

### Android (ExoPlayer)

Load the same URL once with an `HlsMediaSource`; to seek by wall-clock, map the
target date onto the window using the manifest's program-date-time (each
`HlsMediaPlaylist.Segment` exposes `relativeStartTimeUs` plus the playlist's
`startTimeUs`), then `player.seekTo(...)`.

---

## 9. Two different bars — don't confuse them

- **The timeline bar (§4/§5)** shows **where** footage is across the whole 12 h.
  You draw it; you shade `segments[i]`; a click becomes a `seekToDate(...)`.
- **The player's own scrubber** seeks **within** the loaded footage. With
  `PROGRAM-DATE-TIME` it reads true wall-clock time, so both bars agree.

---

## 10. Responses & errors

| Status | Where | Meaning | What to do |
|---|---|---|---|
| `200` | all | Timeline JSON, or the playlist. | Normal. |
| `200` + `error` on a camera | all-cameras timeline | That camera's storage was unreadable. | Show it as *unavailable*, not *empty*. |
| `304` | playlist | Unchanged since your `If-None-Match`. | Nothing — the player handles it. Proxies must pass it through. |
| `401` | all | `edgeId required`. | Always send `?edge_id=`. |
| `404` | `{camera}/timeline`, playback, snapshot | Unknown camera (`no camera for 'X'`), or no footage in the window yet. | Check the label; for playback, show "no footage" and retry slowly. |
| `409` | all | `edge_id mismatch`. | Wrong device addressed. |
| `503` | state/toggle | Recording backbone down (MediaMTX not running). | Transient — retry; alert ops if it persists. |

The all-cameras timeline does **not** 404 when there are no cameras or no
footage: it answers `200` with `"cameras": []` or empty `segments`. "Nothing to
review" is an answer, not an error.

---

## 11. Where the video lives, and what it costs

- Recordings live **only on the edge device's disk** — never uploaded to the
  cloud, S3 or the app server.
- The recorder writes 15-second MPEG-TS segments named with their own start time
  (`2026-08-19_18-30-15-000000.ts`). **Those exact files are what playback
  serves** — nothing is re-cut, re-encoded or copied, so playback costs no CPU
  and no extra disk, and the playlist returns instantly.
- Segments older than the window delete themselves.
- **Cloud cost:** storage = zero. The only cost is the tunnel relaying bytes
  **while someone is watching** (~2 Mbps ≈ 1 GB/hour). Watching from inside the
  home LAN never touches the cloud.

---

## 12. The timestamp rule (prevents every "wrong footage" bug)

The whole edge runs on **one clock: the device's local time** — alerts, snapshots
and recordings all share it, and the playlist's `PROGRAM-DATE-TIME` uses it too.
So when you turn an **alert** into a playback seek, convert its timestamp to a
`Date`/epoch and hand it to `seekToDate(...)`; the player lands on the right
instant regardless of the viewer's timezone.

---

## 13. Copy-paste for each team

**Backend team**
> Forward the whole `/api/v1/recordings/*` path to the edge through the existing
> frp tunnel, at `https://edgeai.ceravishealth.in/<edge_id>/api/v1/recordings/…`.
> **No headers, no token** — the legacy `X-Ceravis-Control-Token` is removed; auth
> is the `?edge_id=<edge_id>` query parameter, and the same value goes in the URL
> path so the tunnel finds the house. Three endpoints matter:
> `GET …/timeline?edge_id=` (**all cameras, one call**),
> `GET …/{cameraLabel}/timeline?edge_id=` (one camera) and
> `GET …/{cameraLabel}/playback.m3u8?edge_id=` (an HLS playlist, `200`, whose
> relative `segment/...` URIs resolve back under the same path — one proxy rule
> covers them). **Two rules on the playlist:** pass `If-None-Match`/`ETag`/
> `Cache-Control` straight through, and **never cache the `.m3u8` body**.

**Frontend / mobile team**
> Build the review screen from **one** `…/timeline?edge_id=` call: it returns
> every camera with its `label` and `segments[]`, plus the shared
> `window_start`/`now`/`retention_hours`. Draw one rail per camera from
> `window_start`→`now` and shade the segments. A camera with an `error` field is
> *unavailable*, not empty. When the user opens a camera, load
> `…/{label}/playback.m3u8?edge_id=` into an HLS player **once** (hls.js on web,
> AVPlayer on iOS, ExoPlayer on Android) and thereafter **seek by date** — the
> player pauses, scrubs, rolls across gaps and picks up newly recorded clips
> **with no further calls**, so never re-create the player or re-download the
> manifest to "refresh". Keep the bar fresh from the player's own playlist
> reloads (§7) rather than polling. Never open playback at the very end of the
> footage — seek to the alert time or the start of the newest stretch.
