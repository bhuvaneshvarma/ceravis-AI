# Fleet heartbeat API (edge fleet agent → Java FMS) — version 2

**Status: version 2 — built on the edge (fleet agent 0.8.0, 2026-11-13); the Java
FMS implements it (`aebb3e4` onward).** It replaces version 1 (agent 0.6.0): see
[Changes from version 1](#changes-from-version-1). The agent sends it every ~60 s
after enrolling (`docs/fleet-enrollment-api.md`). Whatever the FMS answers, the
device **never enrolls again by itself**: a refused or failed check-in is logged,
reported in the next heartbeat and retried with back-off (5 s doubling to 5 min).

The heartbeat is the device's regular check-in: **"I am alive; here is the state
of every part of me; here are the answers to your last orders."** The FMS answers
**"call again in N seconds; here are new orders."**

It is deliberately short (about 2 KB): each part's **state**, the few numbers
worth a graph, and **warnings** in plain sentences. Everything else — every
sensor, every service's memory and uptime, each hotspot device's radio figures,
the full camera detail — is recorded on the device every minute
(`<data>/logs/health.jsonl`, `events.jsonl`, `edge/LOGS.md`) and read when a
warning needs it: with the `logs` order below, or over SSH.

## Request

```http
POST /v1/device/heartbeat HTTP/1.1
Content-Type: application/json
User-Agent: ceravis-fleet-agent
X-Ceravis-Device: ce7k2m9xq4w8n3
X-Ceravis-Timestamp: 1793795400.512
X-Ceravis-Nonce: 4b1f0c9e7a2d6e5f8c3b1a0d9e8f7c6b
X-Ceravis-Signature: HeA/cgrpOErVEqfk/mjCGzQqoJ+s/5ODMYY+Hzom8ZE=
```

| Header | Meaning |
|---|---|
| `X-Ceravis-Device` | the device's **edge_id** (from enrollment): tells the FMS whose key to check the signature with. Not part of the signed lines |
| `X-Ceravis-Timestamp` | send time, Unix seconds with 3 decimals; reject if more than ±300 s off (`401 clock_skew`) |
| `X-Ceravis-Nonce` | 32 random lowercase hex characters, new for every call; reject one already seen (keep each for 10 minutes) |
| `X-Ceravis-Signature` | see below |

### Signature (request) — the enrollment call's lines plus the nonce, the HEARTBEAT key

Signed **exactly as the enrollment call is** (`docs/fleet-enrollment-api.md`),
with one line added: the nonce, right after the timestamp.

```
X-Ceravis-Signature = Base64( HMAC-SHA256( heartbeat key, M ) )

M = POST
    /v1/device/heartbeat
    <X-Ceravis-Timestamp, exactly as sent>
    <X-Ceravis-Nonce, exactly as sent>
    <sha256 of the raw body bytes, 64 lowercase hex characters>
```

| Line | Enrollment (fleet enrollment key) | Heartbeat (heartbeat key) |
|---|---|---|
| 1 | `POST` | `POST` |
| 2 | `/v1/device/enroll` | `/v1/device/heartbeat` |
| 3 | `X-Ceravis-Timestamp` | `X-Ceravis-Timestamp` |
| 4 | sha256 of the raw body (hex) | **`X-Ceravis-Nonce`** |
| 5 | — | sha256 of the raw body (hex) |
| reply | `RESPONSE`, request timestamp, sha256 of the raw reply | `RESPONSE`, request timestamp, **request nonce**, sha256 of the raw reply |

- `M` is those 5 lines joined by a single `\n` (line feed), **no trailing newline**.
- **heartbeat key** — derived on both sides, never sent:

  ```
  heartbeat key = hex( HMAC-SHA256( key = fleet enrollment key, message = secret ) )
  ```

  `secret` is the value in this device's enrollment reply; `hex` is 64 lowercase
  characters. The FMS stores exactly this value (`secret_hash`); the device keeps
  it in `identity.json` (`heartbeat_key`) and does **not** keep the secret. The
  heartbeat key's UTF-8 bytes (its 64 hex characters) are the HMAC key, for the
  request and for the reply.
- **raw body bytes** = the request body exactly as received, byte for byte —
  never parsed, re-serialised or pretty-printed before hashing.
- The body hash is **hex** (64 lowercase characters) inside `M`; the HMAC result
  (32 bytes) is **Base64-encoded once** (standard alphabet, with `=` padding,
  44 characters) into the header. Nothing is hashed or encoded twice.

Worked example (the test vector below; sample keys, not real):

```
fleet enrollment key  = SAMPLE-enroll-key-NOT-REAL-0123456789abcdef
secret (enroll reply) = nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE
heartbeat key         = a60b8fdf7d37ee298c1db069cf871ffe2f5f2363ece396a7f98d87c140ff52e0

M (5 lines):
POST
/v1/device/heartbeat
1793795400.512
4b1f0c9e7a2d6e5f8c3b1a0d9e8f7c6b
d4b8228555468a4c0cd13d0c039738c13b1ad21866cb07e6c983805aa89bcc5d

X-Ceravis-Signature = HeA/cgrpOErVEqfk/mjCGzQqoJ+s/5ODMYY+Hzom8ZE=
```

## Body

```
{ "status": { state, warning_count, hardware, network, setup, services, cameras },
  "results": [ answers to earlier orders ] }
```

Who the device is never travels in the body: the edge_id is the
`X-Ceravis-Device` header; the board, OS and versions are read with the `info`
order when needed.

### One state vocabulary for every part

Every part — the device, `hardware`, `network`, `setup`, each service, each camera — has
a `state` from these five words, and a `warnings` list.

| `state` | Colour | Means |
|---|---|---|
| `operational` | green | working normally; `warnings` is empty |
| `degraded` | amber | working, but something needs attention (the warnings say what) |
| `outage` | red | not working (the warnings say why) |
| `disabled` | grey | off on purpose or not installed — e.g. a camera switched off, no tunnel on a LAN-only home. Never a fault, never a warning |
| `unknown` | grey | cannot be checked right now — e.g. the CERAVIS app is not answering, so what only it can see is unknown |

**`warnings`**: one plain sentence per problem, saying what is wrong and what to
do. **Empty = fine.** The FMS can alert on any non-empty list without knowing
what the part is.

**Device state** (`status.state`):

| `status.state` | When |
|---|---|
| `outage` | the device cannot watch over the person: `edge_application`, `mediamtx`, `ai_detection` or `edge_hotspot` is in `outage`, or every camera that is switched on is in `outage` |
| `degraded` | otherwise, at least one warning anywhere |
| `operational` | no warnings anywhere |

`offline` is **not** a device state: it is the FMS's own conclusion when no
heartbeat arrives (see [What the FMS does](#what-the-fms-does)).

### Sample 1 — operational (everything working; one order answered)

```json
{
  "status": {
    "state": "operational",
    "warning_count": 0,
    "hardware": {
      "state": "operational",
      "uptime_secs": 86412,
      "last_boot": "scheduled_reboot",
      "cpu_pct": 38,
      "gpu_pct": 64,
      "ram_used_gb": 4.9,
      "ram_total_gb": 7.4,
      "disk_used_gb": 52.3,
      "disk_total_gb": 116.4,
      "temperature_c": 53.4,
      "fan_pct": 38,
      "power_w": 7.8,
      "power_mode": "25W",
      "warnings": []
    },
    "network": {
      "state": "operational",
      "internet": "connected",
      "latency_ms": 41.6,
      "hotspot": "on",
      "hotspot_devices": 3,
      "warnings": []
    },
    "setup": {
      "state": "operational",
      "care_recipient": "assigned",
      "cameras_added": 2,
      "warnings": []
    },
    "services": {
      "edge_application": {
        "state": "operational",
        "version": "1.3.0",
        "warnings": []
      },
      "mediamtx": {
        "state": "operational",
        "warnings": []
      },
      "ai_detection": {
        "state": "operational",
        "warnings": []
      },
      "cloud_connection": {
        "state": "operational",
        "pending_uploads": 0,
        "warnings": []
      },
      "frpc": {
        "state": "operational",
        "warnings": []
      },
      "fleet_agent": {
        "state": "operational",
        "version": "0.8.0",
        "warnings": []
      },
      "nightly_reboot": {
        "state": "operational",
        "warnings": []
      },
      "edge_hotspot": {
        "state": "operational",
        "warnings": []
      },
      "ssh_access": {
        "state": "operational",
        "warnings": []
      },
      "time_sync": {
        "state": "operational",
        "warnings": []
      },
      "dns_resolver": {
        "state": "operational",
        "warnings": []
      },
      "local_discovery": {
        "state": "operational",
        "warnings": []
      },
      "fan_control": {
        "state": "operational",
        "warnings": []
      },
      "system_logging": {
        "state": "operational",
        "warnings": []
      },
      "security_updates": {
        "state": "operational",
        "pending_security": 2,
        "warnings": []
      },
      "os_hardening": {
        "state": "operational",
        "warnings": []
      }
    },
    "cameras": [
      {
        "camera_id": "LOUNGE",
        "room": "LOUNGE",
        "state": "operational",
        "fps": 14.8,
        "reconnects": 0,
        "recording": true,
        "night_vision": false,
        "ptz": true,
        "wifi_signal_dbm": -53,
        "warnings": []
      },
      {
        "camera_id": "KITCHEN",
        "room": "KITCHEN",
        "state": "operational",
        "fps": 15.0,
        "reconnects": 0,
        "recording": true,
        "night_vision": true,
        "ptz": false,
        "wifi_signal_dbm": -61,
        "warnings": []
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

### Sample 2 — degraded (one camera without video, one with a weak signal, the app restarted once, the internet was lost for 17 minutes in the night)

Two cameras with problems is still `degraded`, not `outage`: the other parts
watch on. The internet loss is reported once it is back — the device could not
send it while offline.

```json
{
  "status": {
    "state": "degraded",
    "warning_count": 5,
    "hardware": {
      "state": "operational",
      "uptime_secs": 86412,
      "last_boot": "scheduled_reboot",
      "cpu_pct": 38,
      "gpu_pct": 64,
      "ram_used_gb": 4.9,
      "ram_total_gb": 7.4,
      "disk_used_gb": 52.3,
      "disk_total_gb": 116.4,
      "temperature_c": 53.4,
      "fan_pct": 38,
      "power_w": 7.8,
      "power_mode": "25W",
      "warnings": []
    },
    "network": {
      "state": "degraded",
      "internet": "connected",
      "latency_ms": 41.6,
      "hotspot": "on",
      "hotspot_devices": 3,
      "warnings": [
        "the internet connection was lost from 02:14 to 02:31 (17 min)"
      ]
    },
    "setup": {
      "state": "operational",
      "care_recipient": "assigned",
      "cameras_added": 2,
      "warnings": []
    },
    "services": {
      "edge_application": {
        "state": "degraded",
        "version": "1.3.0",
        "warnings": [
          "ceravis.service restarted 1 time since the last heartbeat"
        ]
      },
      "mediamtx": {
        "state": "operational",
        "warnings": []
      },
      "ai_detection": {
        "state": "operational",
        "warnings": []
      },
      "cloud_connection": {
        "state": "operational",
        "pending_uploads": 0,
        "warnings": []
      },
      "frpc": {
        "state": "operational",
        "warnings": []
      },
      "fleet_agent": {
        "state": "operational",
        "version": "0.8.0",
        "warnings": []
      },
      "nightly_reboot": {
        "state": "operational",
        "warnings": []
      },
      "edge_hotspot": {
        "state": "operational",
        "warnings": []
      },
      "ssh_access": {
        "state": "operational",
        "warnings": []
      },
      "time_sync": {
        "state": "operational",
        "warnings": []
      },
      "dns_resolver": {
        "state": "operational",
        "warnings": []
      },
      "local_discovery": {
        "state": "operational",
        "warnings": []
      },
      "fan_control": {
        "state": "operational",
        "warnings": []
      },
      "system_logging": {
        "state": "operational",
        "warnings": []
      },
      "security_updates": {
        "state": "operational",
        "pending_security": 2,
        "warnings": []
      },
      "os_hardening": {
        "state": "operational",
        "warnings": []
      }
    },
    "cameras": [
      {
        "camera_id": "LOUNGE",
        "room": "LOUNGE",
        "state": "degraded",
        "fps": 14.9,
        "reconnects": 3,
        "recording": true,
        "night_vision": false,
        "ptz": true,
        "wifi_signal_dbm": -79,
        "warnings": [
          "LOUNGE: weak Wi-Fi signal (-79 dBm) — move the camera closer to the device or clear what is in between"
        ]
      },
      {
        "camera_id": "KITCHEN",
        "room": "KITCHEN",
        "state": "outage",
        "fps": 0.0,
        "reconnects": 3,
        "recording": true,
        "night_vision": false,
        "ptz": false,
        "wifi_signal_dbm": null,
        "warnings": [
          "KITCHEN: reconnecting — no video from this camera right now",
          "KITCHEN: recording but no segments landing"
        ]
      }
    ]
  },
  "results": []
}
```

### Sample 3 — outage (the CERAVIS app has failed; `unknown` parts)

The agent still reports: the hardware and every systemd service it measures
itself. What only the app can see — the video server, AI, cloud link, networks
and cameras — is `unknown`; the cameras stay listed (as last seen) so a UI can
grey them out instead of dropping them.

```json
{
  "status": {
    "state": "outage",
    "warning_count": 2,
    "hardware": {
      "state": "operational",
      "uptime_secs": 86412,
      "last_boot": "scheduled_reboot",
      "cpu_pct": 12,
      "gpu_pct": null,
      "ram_used_gb": 4.9,
      "ram_total_gb": 7.4,
      "disk_used_gb": 52.3,
      "disk_total_gb": 116.4,
      "temperature_c": 53.4,
      "fan_pct": 38,
      "power_w": null,
      "power_mode": "25W",
      "warnings": []
    },
    "network": {
      "state": "unknown",
      "internet": "unknown",
      "latency_ms": null,
      "hotspot": "unknown",
      "hotspot_devices": null,
      "warnings": []
    },
    "setup": {
      "state": "unknown",
      "care_recipient": "unknown",
      "cameras_added": null,
      "warnings": []
    },
    "services": {
      "edge_application": {
        "state": "outage",
        "version": "1.3.0",
        "warnings": [
          "ceravis.service has FAILED (exit-code)",
          "ceravis.service restarted 4 times since the last heartbeat"
        ]
      },
      "mediamtx": {
        "state": "unknown",
        "warnings": []
      },
      "ai_detection": {
        "state": "unknown",
        "warnings": []
      },
      "cloud_connection": {
        "state": "unknown",
        "pending_uploads": null,
        "warnings": []
      },
      "frpc": {
        "state": "operational",
        "warnings": []
      },
      "fleet_agent": {
        "state": "operational",
        "version": "0.8.0",
        "warnings": []
      },
      "nightly_reboot": {
        "state": "operational",
        "warnings": []
      },
      "edge_hotspot": {
        "state": "operational",
        "warnings": []
      },
      "ssh_access": {
        "state": "operational",
        "warnings": []
      },
      "time_sync": {
        "state": "operational",
        "warnings": []
      },
      "dns_resolver": {
        "state": "operational",
        "warnings": []
      },
      "local_discovery": {
        "state": "operational",
        "warnings": []
      },
      "fan_control": {
        "state": "operational",
        "warnings": []
      },
      "system_logging": {
        "state": "operational",
        "warnings": []
      },
      "security_updates": {
        "state": "operational",
        "pending_security": 2,
        "warnings": []
      },
      "os_hardening": {
        "state": "operational",
        "warnings": []
      }
    },
    "cameras": [
      {
        "camera_id": "LOUNGE",
        "room": "LOUNGE",
        "state": "unknown",
        "fps": null,
        "reconnects": null,
        "recording": null,
        "night_vision": null,
        "ptz": null,
        "wifi_signal_dbm": null,
        "warnings": []
      },
      {
        "camera_id": "KITCHEN",
        "room": "KITCHEN",
        "state": "unknown",
        "fps": null,
        "reconnects": null,
        "recording": null,
        "night_vision": null,
        "ptz": null,
        "wifi_signal_dbm": null,
        "warnings": []
      }
    ]
  },
  "results": []
}
```

### Sample 4 — disabled parts (a LAN-only home without the tunnel (`frpc` not installed), a camera switched off)

`disabled` is never a warning: this device is `operational`.

```json
{
  "status": {
    "state": "operational",
    "warning_count": 0,
    "hardware": {
      "state": "operational",
      "uptime_secs": 86412,
      "last_boot": "scheduled_reboot",
      "cpu_pct": 38,
      "gpu_pct": 64,
      "ram_used_gb": 4.9,
      "ram_total_gb": 7.4,
      "disk_used_gb": 52.3,
      "disk_total_gb": 116.4,
      "temperature_c": 53.4,
      "fan_pct": 38,
      "power_w": 7.8,
      "power_mode": "25W",
      "warnings": []
    },
    "network": {
      "state": "operational",
      "internet": "connected",
      "latency_ms": 41.6,
      "hotspot": "on",
      "hotspot_devices": 3,
      "warnings": []
    },
    "setup": {
      "state": "operational",
      "care_recipient": "assigned",
      "cameras_added": 2,
      "warnings": []
    },
    "services": {
      "edge_application": {
        "state": "operational",
        "version": "1.3.0",
        "warnings": []
      },
      "mediamtx": {
        "state": "operational",
        "warnings": []
      },
      "ai_detection": {
        "state": "operational",
        "warnings": []
      },
      "cloud_connection": {
        "state": "operational",
        "pending_uploads": 0,
        "warnings": []
      },
      "frpc": {
        "state": "disabled",
        "warnings": []
      },
      "fleet_agent": {
        "state": "operational",
        "version": "0.8.0",
        "warnings": []
      },
      "nightly_reboot": {
        "state": "operational",
        "warnings": []
      },
      "edge_hotspot": {
        "state": "operational",
        "warnings": []
      },
      "ssh_access": {
        "state": "operational",
        "warnings": []
      },
      "time_sync": {
        "state": "operational",
        "warnings": []
      },
      "dns_resolver": {
        "state": "operational",
        "warnings": []
      },
      "local_discovery": {
        "state": "disabled",
        "warnings": []
      },
      "fan_control": {
        "state": "operational",
        "warnings": []
      },
      "system_logging": {
        "state": "operational",
        "warnings": []
      },
      "security_updates": {
        "state": "operational",
        "pending_security": 2,
        "warnings": []
      },
      "os_hardening": {
        "state": "operational",
        "warnings": []
      }
    },
    "cameras": [
      {
        "camera_id": "LOUNGE",
        "room": "LOUNGE",
        "state": "operational",
        "fps": 14.8,
        "reconnects": 0,
        "recording": true,
        "night_vision": false,
        "ptz": true,
        "wifi_signal_dbm": -55,
        "warnings": []
      },
      {
        "camera_id": "BEDROOM",
        "room": "BEDROOM",
        "state": "disabled",
        "fps": null,
        "reconnects": 3,
        "recording": false,
        "night_vision": false,
        "ptz": false,
        "wifi_signal_dbm": null,
        "warnings": []
      }
    ]
  },
  "results": []
}
```

### Sample 5 — a device installed but not set up yet (no care recipient assigned, no cameras added)

`setup` says so in a line, so the fleet shows at once which devices run but
serve nobody yet. It never names a person.

```json
{
  "status": {
    "state": "degraded",
    "warning_count": 2,
    "hardware": {
      "state": "operational",
      "uptime_secs": 86412,
      "last_boot": "scheduled_reboot",
      "cpu_pct": 38,
      "gpu_pct": 64,
      "ram_used_gb": 4.9,
      "ram_total_gb": 7.4,
      "disk_used_gb": 52.3,
      "disk_total_gb": 116.4,
      "temperature_c": 53.4,
      "fan_pct": 38,
      "power_w": 7.8,
      "power_mode": "25W",
      "warnings": []
    },
    "network": {
      "state": "operational",
      "internet": "connected",
      "latency_ms": 41.6,
      "hotspot": "on",
      "hotspot_devices": 0,
      "warnings": []
    },
    "setup": {
      "state": "degraded",
      "care_recipient": "not_assigned",
      "cameras_added": 0,
      "warnings": [
        "no care recipient assigned yet — sign in on this device and assign one",
        "no cameras added yet — add the home's cameras in this device's setup"
      ]
    },
    "services": {
      "edge_application": {
        "state": "operational",
        "version": "1.3.0",
        "warnings": []
      },
      "mediamtx": {
        "state": "operational",
        "warnings": []
      },
      "ai_detection": {
        "state": "operational",
        "warnings": []
      },
      "cloud_connection": {
        "state": "operational",
        "pending_uploads": 0,
        "warnings": []
      },
      "frpc": {
        "state": "operational",
        "warnings": []
      },
      "fleet_agent": {
        "state": "operational",
        "version": "0.8.0",
        "warnings": []
      },
      "nightly_reboot": {
        "state": "operational",
        "warnings": []
      },
      "edge_hotspot": {
        "state": "operational",
        "warnings": []
      },
      "ssh_access": {
        "state": "operational",
        "warnings": []
      },
      "time_sync": {
        "state": "operational",
        "warnings": []
      },
      "dns_resolver": {
        "state": "operational",
        "warnings": []
      },
      "local_discovery": {
        "state": "operational",
        "warnings": []
      },
      "fan_control": {
        "state": "operational",
        "warnings": []
      },
      "system_logging": {
        "state": "operational",
        "warnings": []
      },
      "security_updates": {
        "state": "operational",
        "pending_security": 2,
        "warnings": []
      },
      "os_hardening": {
        "state": "operational",
        "warnings": []
      }
    },
    "cameras": []
  },
  "results": []
}
```

<details><summary>The exact bytes signed in sample 1 (1929 bytes) — the test vector body</summary>

```json
{"status":{"state":"operational","warning_count":0,"hardware":{"state":"operational","uptime_secs":86412,"last_boot":"scheduled_reboot","cpu_pct":38,"gpu_pct":64,"ram_used_gb":4.9,"ram_total_gb":7.4,"disk_used_gb":52.3,"disk_total_gb":116.4,"temperature_c":53.4,"fan_pct":38,"power_w":7.8,"power_mode":"25W","warnings":[]},"network":{"state":"operational","internet":"connected","latency_ms":41.6,"hotspot":"on","hotspot_devices":3,"warnings":[]},"setup":{"state":"operational","care_recipient":"assigned","cameras_added":2,"warnings":[]},"services":{"edge_application":{"state":"operational","version":"1.3.0","warnings":[]},"mediamtx":{"state":"operational","warnings":[]},"ai_detection":{"state":"operational","warnings":[]},"cloud_connection":{"state":"operational","pending_uploads":0,"warnings":[]},"frpc":{"state":"operational","warnings":[]},"fleet_agent":{"state":"operational","version":"0.8.0","warnings":[]},"nightly_reboot":{"state":"operational","warnings":[]},"edge_hotspot":{"state":"operational","warnings":[]},"ssh_access":{"state":"operational","warnings":[]},"time_sync":{"state":"operational","warnings":[]},"dns_resolver":{"state":"operational","warnings":[]},"local_discovery":{"state":"operational","warnings":[]},"fan_control":{"state":"operational","warnings":[]},"system_logging":{"state":"operational","warnings":[]},"security_updates":{"state":"operational","pending_security":2,"warnings":[]},"os_hardening":{"state":"operational","warnings":[]}},"cameras":[{"camera_id":"LOUNGE","room":"LOUNGE","state":"operational","fps":14.8,"reconnects":0,"recording":true,"night_vision":false,"ptz":true,"wifi_signal_dbm":-53,"warnings":[]},{"camera_id":"KITCHEN","room":"KITCHEN","state":"operational","fps":15.0,"reconnects":0,"recording":true,"night_vision":true,"ptz":false,"wifi_signal_dbm":-61,"warnings":[]}]},"results":[{"id":"cmd_80","ok":true,"output":"pong from ceravis-acg08mbv3","duration_ms":2}]}
```
</details>

## Field reference

### `status`

| Field | Meaning |
|---|---|
| `state` | the device: `operational` / `degraded` / `outage` (table above) |
| `warning_count` | how many warnings in all parts together |

### `status.hardware` — the board itself

| Field | Unit | Meaning |
|---|---|---|
| `uptime_secs` | s | since the device booted — when it drops, the device restarted |
| `last_boot` | | why it last started: `scheduled_reboot` (the nightly reboot), `manual_reboot` (a person, from CERAVIS), `system_restart` (an orderly shutdown outside CERAVIS — an engineer, an update), `unexpected_restart` (no orderly shutdown: a **power cut or a system crash**), `unknown` (first start after installation) |
| `cpu_pct` | % | processor load, averaged over the last minute (whole number) |
| `gpu_pct` | % | graphics/AI processor load (whole number); `null` when unknown |
| `ram_used_gb` / `ram_total_gb` | GB | memory in use / installed (1 decimal) |
| `disk_used_gb` / `disk_total_gb` | GB | the data disk (recordings, database) |
| `temperature_c` | °C | the hottest point of the chip (1 decimal) |
| `fan_pct` | % | how hard the fan is driven |
| `power_w` | W | power the board draws now |
| `power_mode` | | NVIDIA power mode in force: `15W`, `25W`, `MAXN_SUPER` … |

Warnings: memory ≥ 90 %, a disk ≥ 90 % full, a disk switched to read-only after
errors, running hot (≥ 85 °C), over-current (the power supply is too weak),
processes stopped for lack of memory, and — for 15 minutes after it — an
unexpected restart.

### `status.network` — the two networks

The internet comes by Ethernet cable; the Wi-Fi radio runs the camera hotspot.

| Field | Meaning |
|---|---|
| `internet` | `connected` / `disconnected` / `unknown`: can the device reach the CERAVIS app server |
| `latency_ms` | time to connect to the app server (ms), `null` when disconnected |
| `hotspot` | the camera hotspot: `on` / `off` / `unknown` |
| `hotspot_devices` | devices on the hotspot now (cameras, an installer's phone) |

Warnings: no cable, no address from the router, the router has no internet —
each with what to do; and once back online: **"the internet connection was lost
from 02:14 to 02:31 (17 min)"**.

### `status.setup` — is the device set up to watch over someone

| Field | Meaning |
|---|---|
| `care_recipient` | `assigned`; `not_enrolled` (assigned, but their pictures are not enrolled yet — `ai_detection` warns); `not_assigned` (no care recipient yet); `unknown` (the app is not answering) |
| `cameras_added` | how many cameras are added on this device (`null` when unknown) |

Warnings: **"no care recipient assigned yet — sign in on this device and assign
one"**; **"no cameras added yet — add the home's cameras in this device's setup"**.

### `status.services` — every service and server, in this order

Each entry: `state`, `warnings`, and (some) one or two details.

| Key | What it is | Checked through | Details |
|---|---|---|---|
| `edge_application` | the CERAVIS application: web API, AI, recording, cloud uploads, talk-back | systemd `ceravis.service` + the app answering | `version` |
| `mediamtx` | the video server (MediaMTX) inside the application: live view, recording | the app | — |
| `ai_detection` | person, fall and no-motion detection — the safety function | the app: detector running and analysing frames | — |
| `cloud_connection` | uploads to the CERAVIS app server (alerts, snapshots, clips) | the app | `pending_uploads` (waiting to be sent) |
| `frpc` | the tunnel (frpc) for live view and remote support | systemd `frpc.service` + its route for this edge_id + its live connection to the tunnel server | — |
| `fleet_agent` | this heartbeat's sender | systemd `ceravis-fleet-agent.service` + its last check-in (warns "the last check-in failed: …"; `outage` when check-ins stopped) | `version` |
| `nightly_reboot` | the safety-checked nightly reboot | systemd `ceravis-reboot.timer` + its last run | — |
| `edge_hotspot` | the camera hotspot (NetworkManager) | systemd `NetworkManager.service` + the hotspot on | — |
| `ssh_access` | secure shell, for support engineers | systemd `ssh.service` | — |
| `time_sync` | the clock kept right (NTP) | systemd `systemd-timesyncd.service` + synchronised | — |
| `dns_resolver` | name lookups (DNS) | systemd `systemd-resolved.service` | — |
| `local_discovery` | `<name>.local` on the home network (mDNS) | systemd `avahi-daemon.service` (optional) | — |
| `fan_control` | the fan's speed control | systemd `nvfancontrol.service` | — |
| `system_logging` | the system journal | systemd `systemd-journald.service` | — |
| `security_updates` | nightly Ubuntu security updates | systemd `apt-daily-upgrade.timer` | `pending_security` (waiting) |
| `os_hardening` | services switched off for good stay off (Bluetooth shares the hotspot's antenna; Docker, printing, snaps …) | systemd, 12 units | — |

From systemd: a stopped service that must run, or a failed one, is an `outage`;
starting/stopping or restarted since the last heartbeat is `degraded`; not
installed or masked is `disabled`.

### `status.cameras[]` — every camera

| Field | Meaning |
|---|---|
| `camera_id`, `room` | which camera, and the room it watches |
| `state` | `operational`; `degraded` (video, with a problem: recording not landing, weak signal, wrong stream); `outage` (no video); `disabled` (switched off); `unknown` (the app is not answering) |
| `fps` | frames per second received now |
| `reconnects` | times it reconnected since the app started |
| `recording` | recording switched on (`true`/`false`); failing is a warning |
| `night_vision` | infrared (night) mode on now |
| `ptz` | can pan/tilt/zoom |
| `wifi_signal_dbm` | the camera's Wi-Fi signal as the device hears it (dBm): above −60 strong, −60 to −70 good, −70 to −75 fair, **below −75 weak** (a warning); `null` when not on the hotspot |

### Warnings said once

Some warnings report something that **happened** rather than a condition: a
restart since the last heartbeat, processes stopped for lack of memory, the
internet lost from … to …. Each is repeated in every heartbeat **until one
carrying it is accepted** (HTTP 200), then never again — none is lost to a
failed check-in.

### `results[]` — answers to earlier orders

`id` (the FMS's order id), `ok`, `output` (text; the last 64,000 characters),
`duration_ms`. Kept on the device until a heartbeat carrying them is accepted.

## Response

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Ceravis-Signature: KeCyec715iztxBKB3+MjqKBnJmC05YC2ReZ0FfDjpLA=

{"server_time":1793795400.73,"next_poll_after_secs":60,"commands":[{"id":"cmd_81","kind":"logs","args":{"file":"events","lines":100}}]}
```

| Field | What the device does |
|---|---|
| `server_time` | corrects its clock for later signatures |
| `next_poll_after_secs` | waits this long before the next heartbeat (5 to 3600 s); 1 s when it has answers to send |
| `commands` | runs each order, answers in the next heartbeat |

### Signature (response) — 4 lines, the HEARTBEAT key

Built like the enrollment reply's signature (`RESPONSE`, the request's
timestamp, the reply's sha256), with the request's nonce added:

```
X-Ceravis-Signature = Base64( HMAC-SHA256( heartbeat key, R ) )

R = RESPONSE
    <the REQUEST's X-Ceravis-Timestamp, exactly as the device sent it>
    <the REQUEST's X-Ceravis-Nonce>
    <sha256 of the raw reply body bytes, 64 lowercase hex characters>
```

The word `RESPONSE` keeps a reply's signature from ever passing as a request's.
The raw reply bytes are exactly the bytes sent — sign after serialising, and
never change the body afterwards. A reply with a missing or wrong signature is
ignored: no order runs.

Worked example (the test vector below):

```
R (4 lines):
RESPONSE
1793795400.512
4b1f0c9e7a2d6e5f8c3b1a0d9e8f7c6b
1280ef69d546e07d4fefaa6c33954ef2610293fb3164556510838a8e9a57f7eb

X-Ceravis-Signature = KeCyec715iztxBKB3+MjqKBnJmC05YC2ReZ0FfDjpLA=
```

### Orders (the only ones a device runs)

| `kind` | `args` | Does |
|---|---|---|
| `ping` | — | answers `pong from <hostname>` |
| `info` | — | answers what is installed (JSON text): `device_id`, `hostname`, `model`, `ram_total_bytes`, `edge_version`, `edge_commit`, `agent_version`, `os`, `kernel`, `l4t`, `python`, `power_mode`, `installed` (every component's version: MediaMTX, frpc, JetPack …) |
| `restart_service` | `{"unit": "ceravis" \| "frpc"}` | restarts that service |
| `logs` | `{"unit": "ceravis" \| "frpc" \| "ceravis-fleet-agent", "lines": 1-500}` | that service's latest journal lines |
| `logs` | `{"file": "events" \| "health" \| "fleet" \| "incoming" \| "cloud_calls" \| "mediamtx", "lines": 1-500}` | the device's own log file, newest lines last (`edge/LOGS.md`). `events` first: it says what changed and when |

Anything else is refused (`ok: false, output: "refused: …"`). The app-server
wire log and the talk-back log carry personal data and are never sent: they are
read on the device only.

## The complete exchange (sample 1)

**Request** — headers and the exact body bytes:

```http
POST /v1/device/heartbeat HTTP/1.1
Host: fms.ceravishealth.in
Content-Type: application/json
User-Agent: ceravis-fleet-agent
Content-Length: 1929
X-Ceravis-Device: ce7k2m9xq4w8n3
X-Ceravis-Timestamp: 1793795400.512
X-Ceravis-Nonce: 4b1f0c9e7a2d6e5f8c3b1a0d9e8f7c6b
X-Ceravis-Signature: HeA/cgrpOErVEqfk/mjCGzQqoJ+s/5ODMYY+Hzom8ZE=

{"status":{"state":"operational","warning_count":0,"hardware":{"state":"operational","uptime_secs":86412,"last_boot":"scheduled_reboot","cpu_pct":38,"gpu_pct":64,"ram_used_gb":4.9,"ram_total_gb":7.4,"disk_used_gb":52.3,"disk_total_gb":116.4,"temperature_c":53.4,"fan_pct":38,"power_w":7.8,"power_mode":"25W","warnings":[]},"network":{"state":"operational","internet":"connected","latency_ms":41.6,"hotspot":"on","hotspot_devices":3,"warnings":[]},"setup":{"state":"operational","care_recipient":"assigned","cameras_added":2,"warnings":[]},"services":{"edge_application":{"state":"operational","version":"1.3.0","warnings":[]},"mediamtx":{"state":"operational","warnings":[]},"ai_detection":{"state":"operational","warnings":[]},"cloud_connection":{"state":"operational","pending_uploads":0,"warnings":[]},"frpc":{"state":"operational","warnings":[]},"fleet_agent":{"state":"operational","version":"0.8.0","warnings":[]},"nightly_reboot":{"state":"operational","warnings":[]},"edge_hotspot":{"state":"operational","warnings":[]},"ssh_access":{"state":"operational","warnings":[]},"time_sync":{"state":"operational","warnings":[]},"dns_resolver":{"state":"operational","warnings":[]},"local_discovery":{"state":"operational","warnings":[]},"fan_control":{"state":"operational","warnings":[]},"system_logging":{"state":"operational","warnings":[]},"security_updates":{"state":"operational","pending_security":2,"warnings":[]},"os_hardening":{"state":"operational","warnings":[]}},"cameras":[{"camera_id":"LOUNGE","room":"LOUNGE","state":"operational","fps":14.8,"reconnects":0,"recording":true,"night_vision":false,"ptz":true,"wifi_signal_dbm":-53,"warnings":[]},{"camera_id":"KITCHEN","room":"KITCHEN","state":"operational","fps":15.0,"reconnects":0,"recording":true,"night_vision":true,"ptz":false,"wifi_signal_dbm":-61,"warnings":[]}]},"results":[{"id":"cmd_80","ok":true,"output":"pong from ceravis-acg08mbv3","duration_ms":2}]}
```

**Reply** — `200`, signed:

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Ceravis-Signature: KeCyec715iztxBKB3+MjqKBnJmC05YC2ReZ0FfDjpLA=

{"server_time":1793795400.73,"next_poll_after_secs":60,"commands":[{"id":"cmd_81","kind":"logs","args":{"file":"events","lines":100}}]}
```

**Refusals** (not signed; the device acts on the status code):

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"error":"clock_skew","server_time":1793795712.204}
```

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"error":"invalid_signature"}
```

## Errors

| Answer | Device does |
|---|---|
| `401 {"error":"clock_skew","server_time":…}` | corrects its clock, retries after 1 s |
| `401` / `403` / any other refusal | logs it once, reports it in the next heartbeat (`services.fleet_agent`), retries with back-off up to every 5 minutes. **Never enrolls again by itself** — that is a person's decision: remove `/var/lib/ceravis-fleet-agent/identity.json`, restart the agent |
| `404`, `5xx`, no network | logs once, retries after 5, 10, 20 … up to 300 s — and keeps its minute-by-minute record on the device meanwhile |
| a reply whose signature is wrong | not acted on (no order runs); reported in the next heartbeat; retried with back-off |

**Enrollment** (`docs/fleet-enrollment-api.md`): an enrollment reply the device
cannot trust or use — a wrong signature, no valid edge_id and secret, another
device's — **stops check-ins** until the agent is restarted. The FMS may have
registered the device anyway; trying again could only register it again.

## Test vector (sample keys, not real)

| Input | Value |
|---|---|
| fleet enrollment key | `SAMPLE-enroll-key-NOT-REAL-0123456789abcdef` |
| secret (from the enrollment reply) | `nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE` |
| heartbeat key = hex(HMAC-SHA256(enrollment key, secret)) | `a60b8fdf7d37ee298c1db069cf871ffe2f5f2363ece396a7f98d87c140ff52e0` |
| edge_id / timestamp / nonce | `ce7k2m9xq4w8n3` / `1793795400.512` / `4b1f0c9e7a2d6e5f8c3b1a0d9e8f7c6b` |
| request body | the 1929 bytes in "The exact bytes signed" above |
| request body sha256 | `d4b8228555468a4c0cd13d0c039738c13b1ad21866cb07e6c983805aa89bcc5d` |
| request `X-Ceravis-Signature` | `HeA/cgrpOErVEqfk/mjCGzQqoJ+s/5ODMYY+Hzom8ZE=` |
| reply body | `{"server_time":1793795400.73,"next_poll_after_secs":60,"commands":[{"id":"cmd_81","kind":"logs","args":{"file":"events","lines":100}}]}` |
| reply sha256 | `1280ef69d546e07d4fefaa6c33954ef2610293fb3164556510838a8e9a57f7eb` |
| reply `X-Ceravis-Signature` | `KeCyec715iztxBKB3+MjqKBnJmC05YC2ReZ0FfDjpLA=` |

## What the FMS does

1. **Verifies** every heartbeat: the signature, a nonce not seen before, the
   timestamp within ±300 s. A failed check is answered `401` and stored nowhere.
2. **Decides offline**: no accepted heartbeat for 3 × `next_poll_after_secs`
   (3 minutes at 60 s) → the device is **offline** — the FMS's own state, shown
   above the device's last reported one. A device's `uptime_secs` after it comes
   back tells a restart (small uptime; see `last_boot`) from a network outage
   (uptime kept; the device then reports the internet loss itself).
3. **Keeps** the latest status of every device, and a history for graphs. Worth
   a graph: `cpu_pct`, `gpu_pct`, `ram_used_gb`, `temperature_c`, `power_w`,
   `fan_pct`, `latency_ms`, `hotspot_devices`, and per camera `fps` and
   `wifi_signal_dbm`.
4. **Alerts on change**, not on every heartbeat: when a warning first appears
   (and when a part's state worsens), and again when it clears.
5. **Answers** with `next_poll_after_secs` and any orders.

## Where each part comes from (edge side)

| Part | Measured by |
|---|---|
| `hardware` | the agent (`edge/fleet/fms_agent/health.py`: /proc, /sys, statvfs, nvpmodel) + the app's tegrastats (GPU load, board power) |
| systemd services, `os_hardening`, `security_updates` | the agent (`systemctl show`, Ubuntu's update notifier) |
| `network`, `setup`, `mediamtx`, `ai_detection`, `cloud_connection`, the tunnel route and connection, `time_sync`, `cameras` | the app: `GET /api/v1/system/health` (loopback), built from the same warning rules as `/api/v1/system/status` |

## Changes from version 1

| Version 1 (agent 0.6.0) | Version 2 (agent 0.8.0) |
|---|---|
| states `ok`, `warning`, `down`, `running`, `stopped`, `failed`, `not_installed` … | one vocabulary everywhere: `operational`, `degraded`, `outage`, `disabled`, `unknown` |
| `status.backend` | gone: it is `services.edge_application` (the same service, `ceravis.service`) |
| `hardware.model`, `os`, `boot_id`, `cpu.cores`, `load_avg`, every temperature sensor, swap, root + data disks, `clock`, `network.hostname` | gone from the heartbeat (kept in the device's log; `info` order); `boot_id` replaced by `last_boot` |
| bytes (`memory.total_bytes` …), nested `cpu`/`gpu`/`memory`/`power` objects | flat numbers in GB and whole % (`ram_used_gb`, `cpu_pct` …) |
| `services.frpc` + `services.tunnel` (with the edge_id) | one `services.frpc`, no edge_id |
| `services.internet` | `status.network` (internet + hotspot) |
| `services.edge_id_watch` | gone (the edge_id is fixed for a device's life) |
| `services.app_server`, `reboot_timer` | `cloud_connection`, `nightly_reboot` (`mediamtx` and `frpc` keep their names) |
| signature: 6 lines (edge_id, POST, path, sha256, timestamp, nonce); reply 4 lines (edge_id, RESPONSE, nonce, sha256) | the enrollment call's lines plus the nonce: POST, path, timestamp, nonce, sha256; reply RESPONSE, timestamp, nonce, sha256 |
| — | new: `ai_detection`, `edge_hotspot`, `ssh_access`, `time_sync`, `dns_resolver`, `local_discovery`, `fan_control`, `system_logging`, `security_updates`, `os_hardening`; `fan_pct`, `power_mode`, `last_boot`, `latency_ms`, `hotspot`, `hotspot_devices`, camera `wifi_signal_dbm` |
| per service `unit`, `uptime_secs`, `restarts`, `memory_bytes`; per camera `name`, `resolution`, `codec`, `profile`, `uptime_secs`, `last_frame_age_secs`, `frames_captured`, `stream_ready`, `viewers`, recording detail | gone from the heartbeat, kept in the device's log every minute |
| orders `ping`, `restart_service`, `logs` (journal) | + `info`; `logs` also reads the device's log files |
| signed with the enrollment reply's `secret` | signed with the **heartbeat key** = hex(HMAC-SHA256(fleet enrollment key, secret)) — the FMS's `secret_hash`; the device no longer keeps the secret |
| a heartbeat `401` made the device enroll again | never: logged, reported, retried with back-off; an enrollment reply it cannot trust stops check-ins |
| — | `status.setup`: care recipient assigned / enrolled, cameras added |
