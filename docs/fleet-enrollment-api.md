# Fleet enrollment API (edge fleet agent ↔ Java FMS)

**Status: final, as built on both sides (2026-11-04).** This file is the single
source of truth. If code or another document disagrees with it, this file and
the code in `edge/fleet/` win. Agent version: **0.5.0** (`edge/fleet/fms_agent/__init__.py`).

## What enrollment is

At install time the fleet agent (`ceravis-fleet-agent.service`, code in
`edge/fleet/`) calls the FMS **once**. The FMS answers with the device's
**edge_id** and its own **device key** (`secret`). The edge_id comes **only**
from the FMS; the edge never makes one up and never reads it from
`account.json`.

- The device enrolls only when `/var/lib/ceravis-fleet-agent/identity.json` is
  missing. Install, `update.sh`, `upgrade.sh` and reboots keep that file, so an
  update **never** enrolls again. Only `setup/seal.sh` (golden image) removes it.
- After enrolling, every heartbeat is signed with the device key, not the
  enrollment key.

## Request

```http
POST /v1/device/enroll HTTP/1.1
Content-Type: application/json
User-Agent: ceravis-fleet-agent
X-Ceravis-Timestamp: 1793361045.218
X-Ceravis-Signature: 6nmUkckfyCsTaud1zyY1oygY6tFUMwxLLe/WRhSVkCY=

{"device_id":"ceravis-acg08mbv3","hardware":{...},"software":{...},"agent":{...},"status":null}
```

There is **no** `X-Ceravis-Device` header, **no** `X-Ceravis-Nonce` header and
**no** `fingerprint` (removed on purpose, 2026-11-04).

### Signature (request)

`X-Ceravis-Signature = Base64( HMAC-SHA256( enrollment key, M ) )` where `M` is
these **4 lines** joined by `\n` (no trailing newline):

```
POST
/v1/device/enroll
<X-Ceravis-Timestamp, exactly as sent>
<sha256 of the raw body bytes, 64 lowercase hex characters>
```

The **enrollment key** is the fleet key (`FMS_ENROLL_KEY`), the same on every
device, kept root-only in `/etc/ceravis-fleet-agent/enroll.env`. Hash the
**raw body bytes as sent/received**, never re-serialised JSON.

### Body fields

| Field | Type | Meaning |
|---|---|---|
| `device_id` | string | The device's name from its board serial: `ceravis-` + Crockford base32 of int("1"+serial), lowercase (`edge/common/device_identity.py`). Also its hostname and hotspot name. **The FMS identifies the device by this.** |
| `hardware` | object | `model` (board name), `ram_total_bytes`. |
| `software` | object | `edge_version` (`edge/VERSION`), `edge_commit` when known. |
| `agent` | object | `version`, `unix_time` (device clock), `boot_id`, `uptime_secs`, `hostname`, `os`, `kernel`, `l4t`, `python`, `rtt_ms`, `interval_secs`, `can_enroll`, `disk` (`root` and `data`: `total_bytes`, `free_bytes`, `used_pct` from statvfs). |
| `status` | object or null | The edge app's own `/api/v1/system/status`, verbatim; null when the edge app is not answering. |

Not sent any more: `fingerprint`, `protocol`, `agent.enroll_key_id`, `legacy_edge_id`.
Nothing from the app server's account record (`account.json`) is sent: the FMS
issues every edge_id itself, so a home installed before the FMS gets a **new**
edge_id when it enrolls (see "After enrollment" below).

## Response (as the Java FMS sends it)

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Ceravis-Signature: <see below>

{"server_time":1793792749.601,"result":"enrolled","edge_id":"2zuvi9kd83txoj","device_id":"ceravis-o3c6veqpx","secret":"<device key>","heartbeat":{"path":"/v1/device/heartbeat","interval_secs":60.0},"warnings":["device already enrolled; details updated","device clock differs from server by 431704 seconds"],"config":null}
```

### Signature (response)

`X-Ceravis-Signature = Base64( HMAC-SHA256( enrollment key, R ) )` where `R` is
these **3 lines** joined by `\n`:

```
RESPONSE
<the REQUEST's X-Ceravis-Timestamp>
<sha256 of the raw reply body bytes, 64 lowercase hex characters>
```

A reply with a missing or wrong signature is **ignored** and nothing is saved.

### Response fields, and what the device does with each

| Field | Required | What the device does |
|---|---|---|
| `edge_id` | **yes** | Must match `^[A-Za-z0-9_-]{3,64}$`, else the reply is refused. Saved in `identity.json`, handed to the app as the root-only credential `/etc/ceravis/credstore/edge_id` (`setup/sync_edge_id.sh`), used for every route (`/<edge_id>/…`, frpc). |
| `secret` | **yes** | The device key. Must be a non-empty string. Saved in `identity.json` (0600). Signs every heartbeat. Never logged. |
| `device_id` | recommended | Must equal the device's own `device_id`, else refused. If absent, accepted. |
| `server_time` | recommended | The device stores (server_time − its clock) and adds it to later timestamps, so a Jetson with a wrong clock is not rejected. |
| `warnings` | optional | Each string is written to the agent log as a warning. Must be a list. |
| `result` | optional | Not read (success = HTTP 200 + valid `edge_id` and `secret`). |
| `heartbeat` | optional | Not read today: the path is fixed in the agent, the interval comes from each heartbeat reply. |
| `config` | optional | Not read today (reserved). |
| `key_version`, `origin` | optional | Saved if present. The Java FMS does not send them. |

### Errors

| Case | Status | Body | Device behaviour |
|---|---|---|---|
| Wrong signature / key | 401 | `{"error":"unauthorized"}` | Retries later. |
| Clock too far off | 401 | `{"error":"clock_skew","server_time":<float>}` | Corrects its clock, retries. |
| Already enrolled, needs an admin reset | 409 | any | Waits ("an admin must press Reset key"). |
| Revoked | 403 | any | Waits. |

## Test vector (sample keys, not real)

| Input | Value |
|---|---|
| enrollment key | `SAMPLE-enroll-key-NOT-REAL-0123456789abcdef` |
| timestamp | `1793361045.218` |

Request body (703 bytes, exactly):

```json
{"device_id":"ceravis-acg08mbv3","hardware":{"model":"NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super","ram_total_bytes":7990648832},"software":{"edge_version":"1.2.0","edge_commit":"bcce561"},"agent":{"version":"0.5.0","unix_time":1793361045.21,"boot_id":"6c1d0f3e-2b7a-4c1e-9f55-0a8d3b7e1c42","uptime_secs":312.4,"hostname":"ceravis-acg08mbv3","os":"Ubuntu 22.04.5 LTS","kernel":"5.15.148-tegra","l4t":"R36 (release), REVISION: 4.0","python":"3.10.12","rtt_ms":null,"interval_secs":60.0,"can_enroll":true,"disk":{"root":{"total_bytes":125029539840,"free_bytes":98213572608,"used_pct":21.4},"data":{"total_bytes":125029539840,"free_bytes":98213572608,"used_pct":21.4}}},"status":null}
```

Reply body (256 bytes, exactly):

```json
{"server_time":1793361045.43,"result":"enrolled","edge_id":"ce7k2m9xq4w8n3","device_id":"ceravis-acg08mbv3","secret":"nHuchkxLPFxmobs_7wDbrWz5hmIcx7J5ymFohvnDpyE","heartbeat":{"path":"/v1/device/heartbeat","interval_secs":60.0},"warnings":[],"config":null}
```

| Expected | Value |
|---|---|
| request body sha256 | `93e03db997235d4427efd830c8e2a9c50ffbe69b061b4b42ecda168e3b62f4a0` |
| request `X-Ceravis-Signature` | `6nmUkckfyCsTaud1zyY1oygY6tFUMwxLLe/WRhSVkCY=` |
| reply body sha256 | `a96d5f0a098180b0ea388cc915006a98391a883dd7b1219d21b9c43ff36658eb` |
| reply `X-Ceravis-Signature` | `YeFltj0BkEogNt0AOhrvZxwToenNpp58JN6xq33+P+o=` |

## After enrollment: how a new edge_id reaches every part

```
FMS reply ─► agent saves identity.json ─► ceravis-edge-id.path notices the change
          ─► setup/sync_edge_id.sh writes /etc/ceravis/credstore/edge_id (root only)
          ─► systemctl --no-block try-restart ceravis   (once; nothing if unchanged)
          ─► the app starts with the new edge_id:
               • frpc routes re-keyed to /<new>  and /<new>/api|ui (old routes replaced)
               • MediaMTX config rewritten: live paths <new>/<cam>
               • every status beat to the app server carries edgeId=<new> + ceravisUserId
```

A home that routed on the app server's edge_id before (migrated with
`sync_edge_id.sh --migrate`) switches to the new one the same way; its old links
stop working once the app server uses the new edge_id. Recordings are kept
(they are stored under `<cam>-aac`, not under the edge_id).

## Heartbeat

After enrolling, the device checks in every ~60 s: `POST /v1/device/heartbeat`,
signed with the **device key** the same way as this call, with the nonce added
after the timestamp (`POST`, path, timestamp, nonce, sha256 of the raw body).
Its body is the health of every part of the device (hardware, network,
services, cameras), each with its own warnings. Specified in
**`docs/fleet-heartbeat-api.md`**.

## Test FMS on the local network

The agent accepts plain `http://` only to a **private IP** (10.x, 172.16–31.x,
192.168.x) and only with `FMS_ALLOW_HTTP_LAN=1`:

```bash
FMS_URL=http://<FMS-IP>:<PORT> FMS_ALLOW_HTTP_LAN=1 bash setup/install_fleet_agent.sh
FMS_URL=default FMS_ALLOW_HTTP_LAN=0 bash setup/install_fleet_agent.sh   # back to jetson.env's FMS
```

Both values live beside the key in `/etc/ceravis-fleet-agent/enroll.env`;
`seal.sh` removes them. Over http the reply (with the device key) crosses the
network unencrypted: a test bench only.

## Open points on the Java side (security)

1. **Re-enrollment answers 200 ("device already enrolled; details updated")
   instead of 409.** Every device holds the same enrollment key, so anyone with
   one device can claim another device's `device_id` and receive *its* key.
   Recommended: answer 409 for an enrolled `device_id` until an admin resets it.
2. **A 431,704-second (5-day) clock difference was accepted.** With no nonce,
   the timestamp window is the only replay protection: a captured request could
   be sent again later. Recommended: reject timestamps more than ±300 s off with
   `401 {"error":"clock_skew","server_time":…}` (the agent corrects itself).
3. The reply must carry `X-Ceravis-Signature` as above, or the agent ignores it.

## History

| Date | Change |
|---|---|
| 2026-11-05 | `legacy_edge_id` removed (the Java FMS does not accept it). Agent 0.5.0. |
| 2026-11-04 | Request signed over 4 lines; no fingerprint, device or nonce header; reply signed over RESPONSE/timestamp/sha256. Final reply shape from the Java FMS. Agent 0.4.0. |
| 2026-11-04 | `enroll_key_id` removed. LAN test FMS. `/etc/ceravis/versions.json`. Agent 0.3.0. |
| 2026-11-03 | FMS-issued edge_id only; `device_id`, `legacy_edge_id`, hardware, software, disk added; `protocol` removed. |
