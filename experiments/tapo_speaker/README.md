# Tapo camera speaker (two-way audio) — throwaway experiment

**This folder is not part of the product.** It is outside `edge/`, imports nothing
from it, is imported by nothing, touches no config, starts no service, and is
deleted with:

```bash
rm -rf experiments/
```

Nothing else in the repo changes when it is gone.

---

## 1. Is it feasible? Yes — and simpler than the method you quoted

The method you described (pure Python, no middleman server, local IP, session
auth, raw audio piped over a socket) is exactly right. Two corrections to it,
both of which make the job **easier**, not harder:

| The quoted method says | What the camera actually does |
|---|---|
| "HTTPS/REST, heavily encrypted with device-specific session keys" | Plain **HTTP over TCP on port 8800** — no TLS. Only the camera→client *media* is AES-encrypted (key derived from a nonce in the response header). The client→camera **speaker path is sent in the clear** (`X-If-Encrypt: 0`), so there is no crypto to implement at all on the send path. |
| "log in with your cloud credentials, maintain the token session" | One **HTTP Digest** handshake, then the socket stays open. No token, no refresh, no cloud round-trip. The digest password is `MD5(cloud_password).upper()` (or `SHA256(...)` when the challenge says `encrypt_type="3"`) with username `admin`. |
| "PCM, AAC or Opus" | **G.711 A-law, 8 kHz mono, only** — carried in an MPEG-TS elementary stream with TP-Link's private `stream_type 0x90`. (`0x91` = PCMU/16000 appears on some newer firmware, but on the *receive* side.) |

Not `pytapo`. pytapo covers PTZ, settings and SD-card download; it has no
speaker/talk API (its `media_stream.session` can carry arbitrary payloads, but
you would still be writing the talk session and the TS muxing yourself). The
reference implementation for this direction is **go2rtc**'s `pkg/tapo`
(MIT-licensed, no AGPL). `tapo_talk.py` is an independent Python
reimplementation of that wire format — **no go2rtc binary, no Go, no vendored
code, and no pip packages: standard library only.**

The whole protocol is five steps and is documented at the top of `tapo_talk.py`.

### What you need that you do not have yet

The camera's **TP-Link cloud-account password** — the password of the Tapo app
account the camera is bound to. It is *not* the RTSP/camera account
(`isw` / `isw175`) we already store in `cameras.json`, and not the ONVIF
credentials. That is the one blocker to testing; nothing else is needed.

### Honest risk

TP-Link has changed this protocol before, and there are open reports of
two-way audio breaking on some newer firmwares (go2rtc issues #1272 on a C220,
#1494, #1835). So this is *probable*, not *certain*, on your exact C260
firmware — which is why `probe` exists: it proves auth + speaker session
without sending a byte of audio. If `probe` succeeds, the rest is just bytes.

---

## 2. Load on the edge device: negligible

Measured on this dev box (Jetson is slower, so scale ~3-5x and it is still noise):

| | cost |
|---|---|
| TS muxing, 60 s of speech | **0.28 s CPU** (~0.5 % of one core while talking, zero when silent) |
| Network out | **75 kbit/s** while talking (64 kbit/s G.711 + TS overhead) |
| Memory | one socket + a few hundred kB of buffers |
| Camera connections | **one extra short-lived TCP connection to port 8800, only while talking** |
| GPU / NVDEC | **zero** — no video is pulled on this socket |
| Processes | one `ffmpeg` for ~50 ms to decode the clip, then it exits. Nothing resident. |

Three things keep it that cheap:

1. We open the **talk session only** (`seq 3`), never the preview session
   (`seq 1`) — so the camera never starts a second video stream for us. This
   matters: the whole point of the one-stream policy is that MediaMTX pulls each
   camera exactly once.
2. Audio is converted **once, up front**, not on a per-packet path.
3. It is short-lived and event-driven — connect, talk, disconnect. Nothing runs
   between announcements.

If this ever graduates into the product, the shape that keeps the load at zero is
the same: no persistent talk socket, no polling, connect only when there is
something to say.

---

## 3. Yes — this is the same audio family we already use for recordings

Today (`edge/config/settings.py`, `edge/livestream/mediamtx_client.py`):

* the camera's main RTSP stream carries **G.711 at ~8 kHz mono** alongside the video;
* MediaMTX supervises one FFmpeg per camera (`runOnInit`) that **copies the video
  untouched** and re-encodes only that G.711 audio to **AAC (16 kHz mono, 32 kbps)**, into
  the `<cam>-aac` path — that is the stream the recorder writes, so clips are
  video + AAC, the one MP4 combo every player accepts;
* live view (WebRTC) speaks G.711 natively, so it needs none of that.

So the camera already speaks G.711 to us on the way **in**. This experiment is
the same codec on the way **out** — A-law instead of the µ-law/A-law the stream
happens to carry, at the same 8 kHz. The AAC transcode is only about MP4
playback compatibility and is unrelated to the speaker; we do **not** send AAC
to the camera, and nothing here touches the recording pipeline.

---

## 4. Running it on the Jetson

```bash
git fetch origin
git checkout experiment/tapo-speaker      # separate branch, ceravis1.1 untouched
cd experiments/tapo_speaker
```

**Step 0 — offline sanity check (no camera, no network):**

```bash
python3 selftest.py
```

**Step 1 — prove auth and a speaker session, sending no audio:**

```bash
python3 tapo_talk.py probe --host 192.168.0.250 --cloud-password 'YOUR_TAPO_ACCOUNT_PASSWORD'
```

Expected: a `challenge:` line, `authenticated (HTTP 200)`, then
`PROBE OK`. If it prints `HTTP 401`, the password is wrong — remember it is the
**cloud-account** password, not `isw175`.

**Step 2 — make the camera beep (the real proof):**

```bash
python3 tapo_talk.py tone --host 192.168.0.250 --cloud-password 'PW' --seconds 2
```

**Step 3 — play a real clip / speak:**

```bash
python3 tapo_talk.py play --host 192.168.0.250 --cloud-password 'PW' --file alert.wav
python3 tapo_talk.py say  --host 192.168.0.250 --cloud-password 'PW' --text "Please stay seated, help is coming"
```

`play` accepts anything the device's ffmpeg reads (wav/mp3/m4a/...). `say` needs
`espeak-ng` (`sudo apt install espeak-ng`); if you would rather not install it,
render the sentence to a `.wav` anywhere and use `play`.

To keep the password out of your shell history:

```bash
read -rs TAPO_CLOUD_PASSWORD && export TAPO_CLOUD_PASSWORD
python3 tapo_talk.py probe --host 192.168.0.250
```

Useful flags: `--port` (default 8800), `--mode half` (if `aec` is refused),
`--repeat N`, `--volume 1.5`, `--quiet`.

The other bench camera is `192.168.0.251`. Note the port is **8800**, not the
`2020` we use for ONVIF.

---

## 5. If it does not work — what each failure means

| Symptom | Cause |
|---|---|
| `Connection refused` on 8800 | Port not open on this firmware/model. Nothing to do from our side. |
| `expected a 401 Digest challenge` | Something else is listening on 8800; wrong host. |
| `authentication failed: HTTP 401` | Wrong password (must be the TP-Link **cloud account** password), or the account is not the one the camera is bound to. |
| `camera refused the talk session` / timeout | Firmware dropped the talk API, or the mic/speaker is disabled in the Tapo app. Try `--mode half`. |
| Session opens, no sound | Speaker volume is off in the Tapo app; or the firmware wants `0x91`/PCMU. Try a longer `--seconds 5` tone first. |
| Works once, then dead until reboot | Known Tapo firmware bug (a talk session is not released). Wait ~60 s between attempts. |

Whatever happens, no state is written anywhere: this tool has no config file, no
data directory and no service.
