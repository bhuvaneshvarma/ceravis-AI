# Recording playback — integration guide

How the ceravishealth app (web **and** mobile) plays back **recorded footage**
with a real review player — pause, scrub, seek to any second, roll across gaps —
and a CCTV-style timeline. Same authentication as the Cloud PTZ controls, so if
PTZ works this is a short add.

There are exactly **two endpoints**, and one idea that makes it professional:
**the recording carries its own wall-clock time, so the player seeks by _date_.**

---

## 1. What it does, in one line

Every time the AI sees a person, the edge records that camera as **1080p H.264 +
AAC audio**, kept for a **rolling 12 hours**. To review it, the app asks the edge
**“where is there footage?”** (draws a timeline bar) and loads **one playlist for
the whole 12 h** into a video player. The player then **seeks to any moment by
date** — all on the client, no more calls.

Because recording is person-triggered, most of the 12 h is empty — you only ever
store and see the stretches where someone was actually present. The player jumps
across the empty gaps on its own.

---

## 2. The connection (identical to PTZ)

```
   Browser / App  ──►  your backend / proxy  ──►  frp tunnel  ──►  edge device
                       (adds the 2 secrets)        (secure)         (answers)
```

The client never holds the secrets. Your proxy forwards the **whole**
`/api/v1/recordings/*` path to the edge through the existing frp tunnel and adds
**one header on every request** (the playlist request *and* each little video
segment request that follows):

```
X-Ceravis-Control-Token: <the SAME secret PTZ uses (edge_control_token)>
```

> A single proxy rule — forward `/api/v1/recordings/` + inject that header —
> covers all three sub-paths below (`timeline`, `playback.m3u8`, `segment/*`).
> The URLs never change between dev and prod.

**Addressing a camera:** the `{camera}` slot takes the SAME label as PTZ —
`KITCHEN`, `BEDROOM`, `LIVING_ROOM`, `LOUNGE`, … (case-insensitive) — or the raw
`camera_id`. `edge_id` (the home, e.g. `home_1234`) is the same value PTZ uses.

---

## 3. Endpoint A — the timeline (where footage exists)

```
GET  /api/v1/recordings/{camera}/timeline?edge_id={homeId}
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
`segments[i]` block — those shaded blocks are the only places with video and the
only clickable spots. Poll it every ~30 s so it reflects the rolling window (the
oldest ages out, the newest appears). This bar is your *availability* view; the
player’s own scrubber is a *different* axis (see §6).

---

## 4. Endpoint B — the playback timeline (ONE call, seek by date)

```
GET  /api/v1/recordings/{camera}/playback.m3u8?edge_id={homeId}
```

Returns **one HLS playlist for the entire retention window** (Content-Type
`application/vnd.apple.mpegurl`, HTTP 200 — no redirect). It lists the recorded
segments by their real files, **tags each recorded stretch with its true
wall-clock time**, and **joins the stretches across the empty gaps**:

```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-TARGETDURATION:15
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PROGRAM-DATE-TIME:2026-07-16T14:30:00+05:30     ← the wall-clock anchor
#EXTINF:15.000,
segment/2026-07-16_14-30-00-000000.ts
#EXTINF:15.000,
segment/2026-07-16_14-30-15-000000.ts
#EXT-X-DISCONTINUITY                                    ← nobody was present here
#EXT-X-PROGRAM-DATE-TIME:2026-07-16T15:10:00+05:30     ← next stretch re-anchored
#EXTINF:15.000,
segment/2026-07-16_15-10-00-000000.ts
#EXT-X-ENDLIST                                          ← only once recording stopped
```

Three things make this professional and are the whole reason it works:

1. **`#EXT-X-PROGRAM-DATE-TIME`** = the real time of each stretch. This is what
   lets the player **seek to an exact instant by date** and show real wall-clock
   time on the scrubber. Without it, a player can only seek by “seconds from the
   start” and cannot map “14:30:15” to a position.
2. **`#EXT-X-DISCONTINUITY`** between stretches = the player **rolls across the
   empty gaps by itself** (jump-cut to the next time someone was present) in the
   same stream — **no new call**.
3. **No `#EXT-X-ENDLIST` while recording** = the playlist is left open, so the
   player keeps picking up **newly recorded** segments and you can follow live.
   `ENDLIST` appears once the person leaves and recording stops.

There is **no `ts` parameter** anymore. Seeking is the client’s job (the player
already knows every segment’s time), which keeps this one URL the single,
cacheable source of a camera’s whole timeline.

The relative `segment/...` URIs resolve to
`/api/v1/recordings/{camera}/segment/<file>` — **the same path**, so your one
proxy rule already covers them. (There is **no** `/recordings/hls/...` path — if
you saw that in an old note, ignore it.)

---

## 5. How to hit it — load once, then seek by date

The pattern is identical on every client: **load the playlist once**, then
**seek by date** whenever the user clicks the timeline. You never re-request for
a new moment.

### Web (hls.js — Chrome/Firefox/Android browsers; also works on desktop Safari)

```js
import Hls from "hls.js";

const url = `/api/v1/recordings/${cameraLabel}/playback.m3u8?edge_id=${homeId}`;
// (Your proxy injects X-Ceravis-Control-Token; the browser sends none.)

let ready = false, pending = null;
const hls = new Hls({ backBufferLength: 90 });
hls.loadSource(url);
hls.attachMedia(videoEl);
hls.on(Hls.Events.LEVEL_LOADED, () => {         // playlist parsed
  if (ready) return;                            // only the first load
  ready = true;
  if (pending != null) { seekToDate(pending); pending = null; }
  else jumpToLatest();                          // default view = newest footage
});

// Seek playback to a wall-clock instant (ms since epoch) using PROGRAM-DATE-TIME.
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
function jumpToLatest() {
  const s = videoEl.seekable;
  if (s.length) { videoEl.currentTime = s.end(s.length - 1) - 1; videoEl.play(); }
}

// The real wall-clock time on screen (for the "Viewing HH:MM:SS" readout + playhead):
//   hls.playingDate  ->  a Date, exact, even across gaps.
```

Wire it up: call `…/timeline`, draw the availability bar, and on a click compute
the clicked wall-clock time and call `seekToDate(thatTime)`. Nothing else.

### iOS app (AVPlayer — the easy one)

iOS speaks HLS natively **and** understands `PROGRAM-DATE-TIME`, so seeking by
time is a one-liner:

```swift
let item = AVPlayerItem(url: playbackURL)          // same /playback.m3u8 URL
let player = AVPlayer(playerItem: item)
// ... later, when the user taps 14:30:15 on the timeline:
let target = ISO8601DateFormatter().date(from: "2026-07-16T14:30:15+05:30")!
item.seek(to: target, completionHandler: nil)      // seekToDate — needs PDT (we send it)
```

`item.seek(to: Date)` **only works because** the playlist carries
`EXT-X-PROGRAM-DATE-TIME`. (Your networking layer adds the token header on the
manifest and segment requests, e.g. via an `AVAssetResourceLoaderDelegate` or by
pointing AVPlayer at your app’s proxy.)

### Android app (ExoPlayer)

ExoPlayer plays the same HLS URL and parses `PROGRAM-DATE-TIME`. Load once with
an `HlsMediaSource`; to seek by wall-clock, map the target date onto the window
using the manifest’s program-date-time (each `HlsMediaPlaylist.Segment` exposes
`relativeStartTimeUs` + the playlist’s `startTimeUs`), then `player.seekTo(...)`.

---

## 6. Two different bars — don’t confuse them

- **The timeline bar (Endpoint A)** shows **where** footage is across the whole
  12 h. You draw it; you shade `segments[i]`; a click on it becomes a
  `seekToDate(...)`.
- **The player’s own scrubber** seeks **within** the footage currently loaded.
  With `PROGRAM-DATE-TIME` it now reads true wall-clock time, and both bars agree.

---

## 7. Where the video lives, and what it costs

- Recordings live **only on the edge device’s disk** — never uploaded to the
  cloud, S3, or the app server.
- The recorder writes 15-second MPEG-TS segments named with their own start time
  (`2026-07-16_14-30-15-000000.ts`). **Those exact files are what playback
  serves** — the playlist just lists them. Nothing is re-cut, re-encoded or
  copied, so playback costs no CPU and no extra disk, and the playlist returns
  instantly.
- Segments older than the retention window delete themselves.
- **Cloud cost:** storage = zero. The only cost is the EC2 tunnel relaying bytes
  **while someone is watching** (~2 Mbps ≈ 1 GB/hour ≈ $0.09 egress). Watching
  from inside the home LAN touches the cloud not at all.

---

## 8. The timestamp rule (prevents every “wrong footage” bug)

The whole edge runs on **one clock: the device’s local time** — alerts,
snapshots and recordings all share it, and the playlist’s `PROGRAM-DATE-TIME`
uses it too. So when you turn an **alert** into a playback seek, convert its
timestamp to a `Date`/epoch and hand it to `seekToDate(...)` — the player lands
on the right instant regardless of the viewer’s timezone.

---

## 9. Responses & errors

| Status | Meaning | What to do |
|---|---|---|
| `200` | Timeline JSON, or the playlist. | Normal. |
| `401` | Missing/wrong control token. | The proxy/backend must send `X-Ceravis-Control-Token`. |
| `404` | No footage in the 12 h window yet. | Show “no footage”. |
| `409` | `edge_id` didn’t match this home. | Wrong device addressed. |
| `503` | Recording backbone down. | Transient — retry; alert ops if it persists. |

---

## 10. What to tell each team (copy-paste)

**Backend team**
> Add one proxy rule: forward the whole `/api/v1/recordings/*` path to the edge
> through the existing frp tunnel, injecting the header
> `X-Ceravis-Control-Token: <same secret as PTZ>` on every request (playlist AND
> the `segment/*.ts` requests that follow). Two endpoints matter:
> `GET …/{cameraLabel}/timeline?edge_id=` (JSON) and
> `GET …/{cameraLabel}/playback.m3u8?edge_id=` (an HLS playlist, HTTP 200, whose
> relative `segment/...` URIs resolve back under the same `/recordings/{cam}/`
> path). No redirects, no `ts` param, no `/recordings/hls/...` path — those were
> removed. Never expose the token to the client.

**Frontend / mobile team**
> Build a review panel with two pieces. (1) Call `…/timeline` and draw a bar from
> `window_start`→`now`, shading each `segments[i]`. (2) Load
> `…/playback.m3u8?edge_id=<home>` into an HLS player **once** (hls.js on web,
> AVPlayer on iOS, ExoPlayer on Android). When the user clicks a time on the bar,
> **seek by date** — `seekToDate(...)` (web, via `programDateTime`) or
> `AVPlayerItem.seek(to: Date)` (iOS). The player pauses/scrubs, rolls across
> gaps on its own, and follows live — with **no further calls per moment**. Use
> the stream’s own time (`hls.playingDate`) for the “now viewing” readout. That’s
> the whole feature.
