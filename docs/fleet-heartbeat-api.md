# Fleet heartbeat API (edge fleet agent → Java FMS)

**Status: specified and built on the edge (agent 0.6.0, 2026-11-05); not yet built
on the Java side.** The agent sends it every ~60 s after enrolling
(`docs/fleet-enrollment-api.md`). Until the FMS has this endpoint the agent logs
one `HTTP 404` warning and retries with back-off; it never enrolls again for it.

The heartbeat is the device's regular check-in: **"I am alive; here is the health
of every part of me; here are the answers to your last orders."** The FMS answers
**"call again in N seconds; here are new orders."** It is a summary to keep
everyone informed and to raise alerts; deep diagnosis is done afterwards by
connecting to the device or with a specific order (`logs`, …).

## Request

```http
POST /v1/device/heartbeat HTTP/1.1
Content-Type: application/json
User-Agent: ceravis-fleet-agent
X-Ceravis-Device: ce7k2m9xq4w8n3
X-Ceravis-Timestamp: 1793795400.512
X-Ceravis-Nonce: 4b1f0c9e7a2d6e5f8c3b1a0d9e8f7c6b
X-Ceravis-Signature: zSOJZXIK3JC/UeM4h6cG4x3J11vr20eEWtX77gKMzK0=
```

| Header | Meaning |
|---|---|
| `X-Ceravis-Device` | the device's **edge_id** (from enrollment): tells the FMS whose key to check with |
| `X-Ceravis-Timestamp` | send time, Unix seconds; reject if more than ~±300 s off (`401 clock_skew`) |
| `X-Ceravis-Nonce` | 32 random hex characters, new for every call; reject one already seen |
| `X-Ceravis-Signature` | see below |

### Signature (request) — 6 lines, the DEVICE key

`X-Ceravis-Signature = Base64( HMAC-SHA256( device key, M ) )`, `M` = these 6 lines
joined by `\n` (no trailing newline). The device key is the `secret` the FMS
returned at enrollment.

```
<X-Ceravis-Device: the edge_id>
POST
/v1/device/heartbeat
<sha256 of the raw body bytes, 64 lowercase hex>
<X-Ceravis-Timestamp, exactly as sent>
<X-Ceravis-Nonce>
```

## Body

```
{ "status": { state, warning_count, hardware, backend, services, cameras },
  "results": [ answers to earlier orders ] }
```

Not sent any more (2026-11-05): `protocol`, the `agent` object, `status_error`
(their facts now live in `status.hardware`, `status.services.fleet_agent` and
`status.backend.warnings`).

**One rule for every part:** each part has a `state` and a `warnings` list.
`warnings` is **empty when nothing is wrong**; each entry is one plain sentence
saying what is wrong and what to do. The FMS can alert on any non-empty list.

### Sample body (formatted for reading)

```json
{
  "status": {
    "state": "warning",
    "warning_count": 2,
    "hardware": {
      "state": "ok",
      "model": "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super",
      "uptime_secs": 86412,
      "boot_id": "6c1d0f3e-2b7a-4c1e-9f55-0a8d3b7e1c42",
      "os": {
        "name": "Ubuntu 22.04.5 LTS",
        "kernel": "5.15.148-tegra",
        "l4t": "R36 (release), REVISION: 4.0",
        "python": "3.10.12"
      },
      "cpu": {
        "cores": 6,
        "load_pct": 38.2,
        "load_avg": [
          2.11,
          1.94,
          1.87
        ]
      },
      "gpu": {
        "load_pct": 64
      },
      "memory": {
        "total_bytes": 7990648832,
        "available_bytes": 3101245440,
        "used_pct": 61.2,
        "swap_total_bytes": 3995324416,
        "swap_used_pct": 4.1
      },
      "temperature_c": {
        "cpu": 52.1,
        "gpu": 50.3,
        "soc0": 49.8,
        "soc1": 50.1,
        "soc2": 49.6,
        "tj": 53.0
      },
      "power": {
        "board_w": 7.8,
        "over_current_events": 0
      },
      "disk": {
        "root": {
          "total_bytes": 125029539840,
          "free_bytes": 72913821696,
          "used_pct": 41.7
        },
        "data": {
          "total_bytes": 125029539840,
          "free_bytes": 72913821696,
          "used_pct": 41.7
        }
      },
      "clock": {
        "unix_time": 1793795400.512,
        "ntp_synchronized": true,
        "timezone": "Asia/Kolkata"
      },
      "network": {
        "hostname": "ceravis-acg08mbv3"
      },
      "warnings": []
    },
    "backend": {
      "state": "running",
      "version": "1.3.0",
      "commit": "e36ddd4",
      "restart_needed": false,
      "uptime_secs": 86398,
      "device_id": "ceravis-acg08mbv3",
      "edge_id": "ce7k2m9xq4w8n3",
      "ai": {
        "gallery_size": 3
      },
      "recording": {
        "enabled": true,
        "cameras_recording": 2
      },
      "storage": {
        "recordings_gb": 41.7,
        "retention_hours": 72,
        "oldest_segment": "2026-11-02T11:20:04+05:30",
        "event_snapshots_gb": 0.38,
        "event_retention_days": 14
      },
      "warnings": []
    },
    "services": {
      "ceravis": {
        "state": "running",
        "unit": "ceravis.service",
        "uptime_secs": 86400,
        "restarts": 0,
        "memory_bytes": 812345678,
        "warnings": []
      },
      "frpc": {
        "state": "running",
        "unit": "frpc.service",
        "uptime_secs": 86400,
        "restarts": 0,
        "memory_bytes": 21000000,
        "warnings": []
      },
      "fleet_agent": {
        "state": "running",
        "unit": "ceravis-fleet-agent.service",
        "uptime_secs": 86401,
        "restarts": 0,
        "memory_bytes": 31000000,
        "warnings": [],
        "version": "0.6.0",
        "fms_rtt_ms": 41.3,
        "interval_secs": 60.0,
        "can_enroll": true
      },
      "edge_id_watch": {
        "state": "running",
        "unit": "ceravis-edge-id.path",
        "uptime_secs": 86400,
        "restarts": null,
        "memory_bytes": null,
        "warnings": []
      },
      "reboot_timer": {
        "state": "running",
        "unit": "ceravis-reboot.timer",
        "uptime_secs": 86400,
        "restarts": null,
        "warnings": []
      },
      "mediamtx": {
        "state": "running",
        "uptime_secs": 86400,
        "restarts": 0,
        "warnings": []
      },
      "app_server": {
        "state": "ok",
        "url": "https://pilot.ceravishealth.in/ch",
        "recent_calls": 50,
        "recent_errors": 0,
        "last_call_ok": true,
        "uploads_pending": 0,
        "oldest_pending_secs": null,
        "warnings": []
      },
      "internet": {
        "state": "ok",
        "warnings": []
      },
      "tunnel": {
        "state": "ok",
        "routes_edge_id": true,
        "edge_id": "ce7k2m9xq4w8n3",
        "detail": null,
        "warnings": []
      }
    },
    "cameras": [
      {
        "camera_id": "LOUNGE",
        "name": "Tapo C220",
        "room": "LOUNGE",
        "enabled": true,
        "state": "running",
        "fps": 14.8,
        "resolution": "2560x1440",
        "codec": "h264",
        "profile": "High",
        "uptime_secs": 86380,
        "reconnects": 0,
        "last_frame_age_secs": 0.1,
        "frames_captured": 1278512,
        "stream_ready": true,
        "viewers": 2,
        "recording": {
          "on": true,
          "writing_ok": true,
          "newest_segment_age_secs": 3.9,
          "segments_last_hour": 60
        },
        "night_vision": false,
        "ptz": true,
        "warnings": []
      },
      {
        "camera_id": "KITCHEN",
        "name": "Tapo C200",
        "room": "KITCHEN",
        "enabled": true,
        "state": "reconnecting",
        "fps": 0.0,
        "resolution": null,
        "codec": null,
        "profile": null,
        "uptime_secs": null,
        "reconnects": 7,
        "last_frame_age_secs": 312.4,
        "frames_captured": 0,
        "stream_ready": false,
        "viewers": 0,
        "recording": {
          "on": true,
          "writing_ok": false,
          "newest_segment_age_secs": 318.0,
          "segments_last_hour": 52
        },
        "night_vision": false,
        "ptz": false,
        "warnings": [
          "KITCHEN: reconnecting — no video from this camera right now",
          "KITCHEN: recording but no segments landing"
        ]
      }
    ]
  },
  "results": [
    {
      "id": "cmd_80",
      "ok": true,
      "output": "pong from ceravis-acg08mbv3",
      "duration_ms": 2
    }
  ]
}
```

<details><summary>The exact bytes signed in this sample (3586 bytes)</summary>

```json
{"status":{"state":"warning","warning_count":2,"hardware":{"state":"ok","model":"NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super","uptime_secs":86412,"boot_id":"6c1d0f3e-2b7a-4c1e-9f55-0a8d3b7e1c42","os":{"name":"Ubuntu 22.04.5 LTS","kernel":"5.15.148-tegra","l4t":"R36 (release), REVISION: 4.0","python":"3.10.12"},"cpu":{"cores":6,"load_pct":38.2,"load_avg":[2.11,1.94,1.87]},"gpu":{"load_pct":64},"memory":{"total_bytes":7990648832,"available_bytes":3101245440,"used_pct":61.2,"swap_total_bytes":3995324416,"swap_used_pct":4.1},"temperature_c":{"cpu":52.1,"gpu":50.3,"soc0":49.8,"soc1":50.1,"soc2":49.6,"tj":53.0},"power":{"board_w":7.8,"over_current_events":0},"disk":{"root":{"total_bytes":125029539840,"free_bytes":72913821696,"used_pct":41.7},"data":{"total_bytes":125029539840,"free_bytes":72913821696,"used_pct":41.7}},"clock":{"unix_time":1793795400.512,"ntp_synchronized":true,"timezone":"Asia/Kolkata"},"network":{"hostname":"ceravis-acg08mbv3"},"warnings":[]},"backend":{"state":"running","version":"1.3.0","commit":"e36ddd4","restart_needed":false,"uptime_secs":86398,"device_id":"ceravis-acg08mbv3","edge_id":"ce7k2m9xq4w8n3","ai":{"gallery_size":3},"recording":{"enabled":true,"cameras_recording":2},"storage":{"recordings_gb":41.7,"retention_hours":72,"oldest_segment":"2026-11-02T11:20:04+05:30","event_snapshots_gb":0.38,"event_retention_days":14},"warnings":[]},"services":{"ceravis":{"state":"running","unit":"ceravis.service","uptime_secs":86400,"restarts":0,"memory_bytes":812345678,"warnings":[]},"frpc":{"state":"running","unit":"frpc.service","uptime_secs":86400,"restarts":0,"memory_bytes":21000000,"warnings":[]},"fleet_agent":{"state":"running","unit":"ceravis-fleet-agent.service","uptime_secs":86401,"restarts":0,"memory_bytes":31000000,"warnings":[],"version":"0.6.0","fms_rtt_ms":41.3,"interval_secs":60.0,"can_enroll":true},"edge_id_watch":{"state":"running","unit":"ceravis-edge-id.path","uptime_secs":86400,"restarts":null,"memory_bytes":null,"warnings":[]},"reboot_timer":{"state":"running","unit":"ceravis-reboot.timer","uptime_secs":86400,"restarts":null,"warnings":[]},"mediamtx":{"state":"running","uptime_secs":86400,"restarts":0,"warnings":[]},"app_server":{"state":"ok","url":"https://pilot.ceravishealth.in/ch","recent_calls":50,"recent_errors":0,"last_call_ok":true,"uploads_pending":0,"oldest_pending_secs":null,"warnings":[]},"internet":{"state":"ok","warnings":[]},"tunnel":{"state":"ok","routes_edge_id":true,"edge_id":"ce7k2m9xq4w8n3","detail":null,"warnings":[]}},"cameras":[{"camera_id":"LOUNGE","name":"Tapo C220","room":"LOUNGE","enabled":true,"state":"running","fps":14.8,"resolution":"2560x1440","codec":"h264","profile":"High","uptime_secs":86380,"reconnects":0,"last_frame_age_secs":0.1,"frames_captured":1278512,"stream_ready":true,"viewers":2,"recording":{"on":true,"writing_ok":true,"newest_segment_age_secs":3.9,"segments_last_hour":60},"night_vision":false,"ptz":true,"warnings":[]},{"camera_id":"KITCHEN","name":"Tapo C200","room":"KITCHEN","enabled":true,"state":"reconnecting","fps":0.0,"resolution":null,"codec":null,"profile":null,"uptime_secs":null,"reconnects":7,"last_frame_age_secs":312.4,"frames_captured":0,"stream_ready":false,"viewers":0,"recording":{"on":true,"writing_ok":false,"newest_segment_age_secs":318.0,"segments_last_hour":52},"night_vision":false,"ptz":false,"warnings":["KITCHEN: reconnecting \u2014 no video from this camera right now","KITCHEN: recording but no segments landing"]}]},"results":[{"id":"cmd_80","ok":true,"output":"pong from ceravis-acg08mbv3","duration_ms":2}]}
```
</details>

### `status` (the summary)

| Field | Meaning |
|---|---|
| `state` | the worst of all parts: `ok` (no warnings), `warning` (at least one), `down` (the CERAVIS app is not answering, or `ceravis.service` is stopped/failed) |
| `warning_count` | how many warnings in all parts together |

### `status.hardware` — the board itself (measured by the agent; sensors from the app)

| Field | Meaning |
|---|---|
| `state` | `ok` / `warning` |
| `model` | the board, e.g. Jetson Orin Nano |
| `uptime_secs` | seconds since the device booted |
| `boot_id` | changes at every boot: a new value = the device rebooted |
| `os` | `name`, `kernel`, `l4t` (NVIDIA JetPack base), `python` |
| `cpu` | `cores`, `load_pct` (average since the last heartbeat), `load_avg` (1, 5, 15 min) |
| `gpu.load_pct` | GPU load, from the app's tegrastats (null when unknown) |
| `memory` | `total_bytes`, `available_bytes`, `used_pct`, `swap_total_bytes`, `swap_used_pct` |
| `temperature_c` | every thermal sensor by name (`cpu`, `gpu`, `tj` = hottest point of the chip, `soc0`…) |
| `power` | `board_w` (power the board draws), `over_current_events` (times it slowed itself for drawing too much) |
| `disk` | `root` (system disk) and `data` (recordings, database): `total_bytes`, `free_bytes`, `used_pct` |
| `clock` | `unix_time` (the device's clock: compare with yours), `ntp_synchronized`, `timezone` |
| `network.hostname` | the device's network name |

Warnings: memory ≥ 90 %, system disk ≥ 90 %, recordings/data disk ≥ 90 %, running
hot (≥ `THERMAL_WARN_C`, 85 °C), over-current, clock not synchronised. While the
app answers, its own rules judge disk/heat/clock; when it does not, the agent
checks them itself, so the device is never reported blind.

### `status.backend` — the CERAVIS app on the device (`ceravis.service`)

| Field | Meaning |
|---|---|
| `state` | `running` / `warning` / `down` (not answering: then it holds only `warnings` with the reason) |
| `version`, `commit`, `restart_needed` | the release running, and whether newer code waits for a restart |
| `uptime_secs` | seconds since the app started |
| `device_id`, `edge_id` | who this device is |
| `ai.gallery_size` | enrolled faces/people the AI tracks (0 = fall and no-motion detection are off) |
| `recording` | `enabled`, `cameras_recording` |
| `storage` | `recordings_gb`, `retention_hours`, `oldest_segment`, `event_snapshots_gb`, `event_retention_days` |

Warnings: AI chain off, code on disk differs from the code running, a camera
warning for a camera that is no longer listed.

### `status.services` — every service and server

Each entry: `state`, `warnings`, and its details.

| Key | What it is | Details |
|---|---|---|
| `ceravis` | systemd `ceravis.service` (the app) | `unit`, `uptime_secs`, `restarts`, `memory_bytes` |
| `frpc` | systemd `frpc.service` (the tunnel client) | same; `not_installed` on a LAN-only home (no warning) |
| `fleet_agent` | systemd `ceravis-fleet-agent.service` (this agent) | same + `version`, `fms_rtt_ms`, `interval_secs`, `can_enroll` |
| `edge_id_watch` | systemd `ceravis-edge-id.path` (hands a new edge_id to the app) | same |
| `reboot_timer` | systemd `ceravis-reboot.timer` (nightly reboot) | `unit`, `uptime_secs`, `restarts` |
| `mediamtx` | the video server inside the app | `uptime_secs`, `restarts` |
| `app_server` | the link to the CERAVIS app server | `url`, `recent_calls`, `recent_errors`, `last_call_ok`, `uploads_pending`, `oldest_pending_secs` |
| `internet` | the device's internet connection | — |
| `tunnel` | whether the tunnel really routes this edge_id | `routes_edge_id`, `edge_id`, `detail` |

systemd `state`: `running`, `stopped`, `failed`, `starting`, `stopping`,
`not_installed`, `unknown`. Warnings: a service that must run is stopped or
failed; it restarted since the last heartbeat (said once); MediaMTX down (with its
last log lines); API key problem, uploads backing up or rejected; no internet;
the tunnel does not route this edge_id. The app's services are `unknown` while
the app is not answering.

### `status.cameras[]` — every camera (`null` = unknown: the app is not answering)

| Field | Meaning |
|---|---|
| `camera_id`, `name`, `room`, `enabled` | which camera |
| `state` | `running`, `connecting`, `reconnecting`, `offline`, `error`, `disabled` |
| `fps` | frames per second decoded now |
| `resolution`, `codec`, `profile` | measured off the live stream (H.264 is required) |
| `uptime_secs` | seconds since this camera connected (null when not running) |
| `reconnects` | times it reconnected since the app started |
| `last_frame_age_secs` | seconds since its last frame |
| `frames_captured` | frames since the app started |
| `stream_ready`, `viewers` | the video server serves it; how many are watching or reading |
| `recording` | `on`, `writing_ok`, `newest_segment_age_secs`, `segments_last_hour` |
| `night_vision`, `ptz` | infrared mode now; can pan/tilt/zoom |

Warnings: no video right now, sub-stream resolution, a codec browsers cannot
play, recording on but no segments landing.

### `results[]` — answers to earlier orders

`id` (the FMS's order id), `ok`, `output` (text; the last 64,000 characters),
`duration_ms`. Kept on the device until a heartbeat carrying them is accepted.

## Response

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Ceravis-Signature: OVzZWv43Xe37qOC/ycxgkeTiebqoSDnkDGLWKYnMllI=

{"server_time":1793795400.73,"next_poll_after_secs":60,"commands":[{"id":"cmd_81","kind":"logs","args":{"unit":"ceravis","lines":100}}]}
```

| Field | What the device does |
|---|---|
| `server_time` | corrects its clock for later signatures |
| `next_poll_after_secs` | waits this long before the next heartbeat (5 to 3600 s); 1 s when it has answers to send |
| `commands` | runs each order, answers in the next heartbeat |

### Signature (response) — 4 lines, the DEVICE key

```
<the edge_id>
RESPONSE
<the REQUEST's X-Ceravis-Nonce>
<sha256 of the raw reply bytes>
```

A reply with a missing or wrong signature is ignored: no order runs.

### Orders (the only ones a device runs)

| `kind` | `args` | Does |
|---|---|---|
| `ping` | — | answers `pong from <hostname>` |
| `restart_service` | `{"unit": "ceravis" \| "frpc"}` | restarts that service |
| `logs` | `{"unit": "ceravis" \| "frpc" \| "ceravis-fleet-agent", "lines": 1-500}` | returns its latest log lines |

Anything else is refused (`ok: false, output: "refused: …"`).

## Errors

| Answer | Device does |
|---|---|
| `401 {"error":"clock_skew","server_time":…}` | corrects its clock, retries after 1 s |
| `401` (any other) | its key is no longer valid (admin reset): enrolls again |
| `404`, `5xx`, no network | logs once, retries after 5, 10, 20 … up to 300 s |

## Test vector (sample device key, not real)

| Input | Value |
|---|---|
| device key | `nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE` |
| edge_id / timestamp / nonce | `ce7k2m9xq4w8n3` / `1793795400.512` / `4b1f0c9e7a2d6e5f8c3b1a0d9e8f7c6b` |
| request body | the 3586 bytes in "The exact bytes signed" above |
| request body sha256 | `d26cc2ecf2e30aad62908d37dd8c156e70908ddb1881853512ea51408afb9503` |
| request `X-Ceravis-Signature` | `zSOJZXIK3JC/UeM4h6cG4x3J11vr20eEWtX77gKMzK0=` |
| reply body | `{"server_time":1793795400.73,"next_poll_after_secs":60,"commands":[{"id":"cmd_81","kind":"logs","args":{"unit":"ceravis","lines":100}}]}` |
| reply sha256 | `dee9f21c86d6ec0901be1279f7d13612d22746beea194f6050f252c5a6a2ba57` |
| reply `X-Ceravis-Signature` | `OVzZWv43Xe37qOC/ycxgkeTiebqoSDnkDGLWKYnMllI=` |

## Where each part comes from (edge side)

| Part | Measured by |
|---|---|
| `hardware` | the agent (`edge/fleet/fms_agent/health.py`: /proc, /sys, statvfs, timedatectl) + the app's tegrastats (GPU, power) |
| systemd services | the agent (`systemctl show`) |
| `backend`, `mediamtx`, `app_server`, `internet`, `tunnel`, `cameras` | the app: `GET /api/v1/system/health` (loopback), built from the same rules as `/api/v1/system/status` |
