# Device health and logs — reference

What this device records about itself, where, and what every word means. It
ships with every release, so it is on every device next to the logs it explains.
The fleet heartbeat (what is sent to the Fleet Management Server every minute)
is specified in `docs/fleet-heartbeat-api.md` in the source repository; it is a
short summary of the `health.jsonl` record described here.

**After a warning, read `events.jsonl` first.** It says what changed and when.
Then read the full minute in `health.jsonl`.

## Where everything is

The data folder is `/var/lib/ceravis` on an installed device, or
`<repository>/edge/data` on a development clone (for example the bench).

| File | In | Written by | Holds | Kept |
|---|---|---|---|---|
| `health.jsonl` | `logs/` | fleet agent | **every minute**: every part of the device in full detail, and the heartbeat summary built from it | 25 MB × 11 files ≈ 2 weeks |
| `events.jsonl` | `logs/` | fleet agent | **only what changed**: a part's state, a warning raised or cleared, a device joining or leaving the hotspot, the agent starting or stopping | 5 MB × 5 files (months) |
| `fleet.jsonl` | `logs/` | fleet agent | every check-in with the fleet server (accepted or failed, why), every order received and its result | 5 MB × 5 files |
| `incoming.jsonl` | `logs/` | edge app | every request **into** the device: writes and failures one line each, routine reads counted per minute | 10 MB × 6 files |
| `cloud_calls.jsonl` | data folder | edge app | every call **to** the CERAVIS app server (one line each) and every detection | the newest 300, past 1 MB |
| `ceravis_api_wire.jsonl` | data folder | edge app | the exact request and response of every app-server call (personal data: read on the device only) | the newest 1,500 calls, past 16 MB |
| `talkback_log.jsonl` | data folder | edge app | every talk-back session: who, which room, how long, audio quality (personal data: on the device only) | 5 MB + 1 older file |
| `mediamtx.log` | data folder | video server | the video server's own log | started fresh past 5 MB |
| `boot_state.json` | data folder | edge app | this boot's id, why the device started, whether the app stopped cleanly | — |
| `identity.json` | `/var/lib/ceravis-fleet-agent` | fleet agent | this device's `edge_id`, `device_id` and **heartbeat key** (it signs every check-in; the enrollment's secret is not kept). Readable by the agent alone — never print it | for the device's life |
| system journal | `journalctl` | every service | text logs: the app, the agent, frpc, NetworkManager, the kernel | 500 MB |

The four files in `logs/` share one rule: past their size a file becomes `.1`,
`.1` becomes `.2` …, and the oldest is deleted — the disk never fills.

## How to read them

```bash
cd /var/lib/ceravis/logs                    # a development clone: <repository>/edge/data/logs
tail -n 40 events.jsonl                     # what changed, newest last
grep warning_raised events.jsonl | tail     # the latest warnings
tail -n 1 health.jsonl | python3 -m json.tool | less    # the last minute, in full
```

One camera's Wi-Fi signal and frame rate, minute by minute:

```bash
python3 -c "import json
for line in open('health.jsonl'):
    r = json.loads(line)
    for c in r['summary']['cameras']:
        print(r['ts'][:16], c['camera_id'], c['state'], c['fps'], c['wifi_signal_dbm'])" | tail -n 30
```

The text logs:

```bash
journalctl -u ceravis -n 200 --no-pager                 # the CERAVIS application
journalctl -u ceravis-fleet-agent -n 100 --no-pager     # the fleet agent
journalctl -u frpc -n 100 --no-pager                    # the remote-access tunnel
journalctl -u NetworkManager -n 100 --no-pager          # the hotspot and the Ethernet
journalctl -k -n 100 --no-pager                         # the kernel (disk, memory, Wi-Fi driver)
```

From the fleet server, without logging in: the order `logs` with
`{"file": "events", "lines": 200}` (also `health`, `fleet`, `incoming`,
`cloud_calls`, `mediamtx`), or `{"unit": "ceravis", "lines": 200}` for a
journal; the order `info` for every installed version.

## States

Every part — the device, the hardware, the networks, each service, each camera —
has one `state` and a `warnings` list (one plain sentence per problem; empty =
fine).

| State | Means |
|---|---|
| `operational` | working normally |
| `degraded` | working, but something needs attention — the warnings say what |
| `outage` | not working — the warnings say why |
| `disabled` | off on purpose or not installed (a camera switched off, no tunnel on a LAN-only home). Never a fault |
| `unknown` | cannot be checked right now (the CERAVIS app is not answering, so what only it sees is unknown) |

**The device** is in `outage` when it cannot watch over the person: the
application, the video server, AI detection or the camera hotspot is in
`outage`, or every camera that is switched on is. Otherwise it is `degraded`
when there is any warning, and `operational` when there is none.

## The services, by name

| Name | What it is | Checked through |
|---|---|---|
| `edge_application` | the CERAVIS application: web API, AI, recording, cloud uploads, talk-back | `ceravis.service`, and the app answering |
| `mediamtx` | the video server inside the application (MediaMTX): live view and recording | the app |
| `ai_detection` | person, fall and no-motion detection — the safety function | the app: the detector running and analysing frames |
| `cloud_connection` | uploads to the CERAVIS app server: alerts, snapshots, clips | the app |
| `frpc` | the tunnel for live view and remote support (frpc) | `frpc.service`, its route, its live connection to the tunnel server |
| `fleet_agent` | the program that records this health and checks in with the fleet server; warns when the last check-in failed, `outage` when check-ins stopped | `ceravis-fleet-agent.service`, and its last check-in |
| `nightly_reboot` | the safety-checked nightly reboot | `ceravis-reboot.timer`, and its last run |
| `edge_hotspot` | the camera hotspot (NetworkManager), the cameras' only network | `NetworkManager.service`, and the hotspot on |
| `ssh_access` | secure shell, for support engineers | `ssh.service` |
| `time_sync` | the clock kept right (NTP) — event times and signatures depend on it | `systemd-timesyncd.service`, and synchronised |
| `dns_resolver` | name lookups (DNS) | `systemd-resolved.service` |
| `local_discovery` | `<name>.local` on the home network (mDNS); optional | `avahi-daemon.service` |
| `fan_control` | the fan's speed control | `nvfancontrol.service` |
| `system_logging` | the system journal | `systemd-journald.service` |
| `security_updates` | nightly Ubuntu security updates | `apt-daily-upgrade.timer` |
| `os_hardening` | services switched off for good stay off — Bluetooth (it shares the hotspot's antenna), Docker, printing, snaps, crash reporters … | 12 units |

Recorded in the detail only: `ceravis-edge-id.path`, `ceravis-reboot.service`,
`ceravis-firstboot.service`, `wpa_supplicant`, the USB-C service link
(`nv-l4t-usb-device-mode`), `nvpmodel`, the zram swap (`nvzramconfig`),
`unattended-upgrades`.

## `health.jsonl` — one line per minute

| Field | Meaning |
|---|---|
| `ts` | when (device-local time, with its UTC offset) |
| `kind` | `snapshot` |
| `hardware` | the board, read by the agent (table below) |
| `units` | every systemd unit above: `load` (`loaded` / `not-found` / `masked`), `active` (`active` / `inactive` / `failed` / `activating` …), `sub`, `result` (why it last stopped: `success`, `exit-code`, `signal`, `resources` …), `uptime_secs`, `restarts` (since boot), `memory_mb` (RAM it uses) |
| `updates` | `pending` (updates waiting), `security` (of which security), `reboot_required` |
| `versions` | every installed component (`/etc/ceravis/versions.json`: release, agent, MediaMTX, frpc, Python, JetPack) |
| `agent` | the fleet agent: `version`, `fms_rtt_ms` (round trip to the fleet server), `interval_secs`, `can_enroll` |
| `app` | the application's full report (`GET /api/v1/system/health`, below), or `null` when it is not answering |
| `app_error` | why it is not answering (`null` when it is) |
| `summary` | the heartbeat summary sent for this minute (the same fields as the heartbeat) |

### `hardware` (agent)

| Field | Meaning |
|---|---|
| `model`, `hostname`, `boot_id` | the board; the device's network name; changes at every boot |
| `uptime_secs` | seconds since it booted |
| `last_boot` | `reason` and `at`: why and when it last started (table below) |
| `os` | `name`, `kernel`, `l4t` (NVIDIA JetPack base), `python` |
| `cpu` | `cores`, `load_pct` (over the last minute), `load_avg` (1, 5, 15 min) |
| `memory` | `total_bytes`, `available_bytes`, `used_pct`, `swap_total_bytes`, `swap_used_pct` |
| `temperature_c` | every thermal sensor by name: `cpu`, `gpu`, `tj` (hottest point of the chip), `soc0` … |
| `fan` | `pct` (how hard it is driven), `rpm` (measured speed) |
| `power_mode` | NVIDIA power mode: `15W`, `25W`, `MAXN_SUPER` … |
| `disk` | `root` (system) and `data` (recordings, database): `total_bytes`, `free_bytes`, `used_pct` |
| `read_only` | disks the kernel switched to read-only after errors (empty = fine) |
| `oom_kills` | processes stopped for lack of memory since boot |
| `journal_mb` | size of the system journal |
| `clock` | `unix_time`, `ntp_synchronized`, `timezone` |

### `app` (the application's report)

| Part | Fields |
|---|---|
| `backend` | `version`, `commit`, `restart_needed` (newer code waits for a restart), `uptime_secs`, `device_id`, `recording` (`enabled`, `cameras_recording`), `storage` (`recordings_gb`, `retention_hours`, `oldest_segment`, `event_snapshots_gb`, `event_retention_days`) |
| `hardware` | from tegrastats: `gpu_load_pct`, `cpu_load_pct`, `temperature_c`, `board_power_w`, `over_current_events` (times the board slowed itself for drawing too much power), `last_boot` |
| `setup` | `care_recipient` (`assigned`, `not_enrolled` = assigned but pictures not enrolled, `not_assigned`), `cameras_added`; warns "no care recipient assigned yet", "no cameras added yet" — never names the person |
| `network.internet` | `connected`, `latency_ms` (to connect to the app server) |
| `network.uplink` | the Ethernet port: `interface`, `cable` (plugged in), `ip`, `gateway` (the router), `speed_mbps` |
| `network.hotspot` | `on`, `ssid`, `channel`, `frequency_mhz`, `width_mhz`, `tx_power_dbm`, `power_save` (must be `off`: on, it delays camera video), `devices` |
| `network.hotspot.devices[]` | each device on the hotspot: `mac`, `ip`, `hostname`, `signal_dbm` (now), `signal_avg_dbm`, `tx_retries` (frames resent), `tx_failed` (frames given up), `tx_bitrate_mbps` / `rx_bitrate_mbps` (link speed), `connected_secs`, `inactive_ms` |
| `ai` | `detector_running`, `last_analysis_age_secs` (since the detector last analysed a frame), `gallery_size` (enrolled reference images of the person), `fall_detection` (falls and no-motion watched now), `pipelines` (each AI stage: frames per second and time per frame) |
| `services` | `mediamtx` (`uptime_secs`, `restarts`), `app_server` (`recent_calls`, `recent_errors`, `last_call_ok`, `uploads_pending`, `oldest_pending_secs`), `tunnel` (`routes_edge_id`, `connected`), `time_sync` (`ntp_synchronized`, `timezone`) |
| `cameras[]` | `camera_id`, `name` (model), `room`, `enabled`, `state`, `stream` (`running`, `connecting`, `reconnecting`, `offline`, `error`), `fps`, `resolution`, `codec`, `profile`, `uptime_secs`, `reconnects`, `last_frame_age_secs`, `frames_captured`, `stream_ready`, `viewers`, `recording` (`on`, `writing_ok`, `newest_segment_age_secs`, `segments_last_hour`), `night_vision`, `ptz`, `wifi` (`ip`, `signal_dbm`, `tx_retries`, `tx_failed`, `tx_bitrate_mbps`, `connected_secs`) |

## `events.jsonl` — only what changed

| `event` | Fields |
|---|---|
| `agent_started` | `version`, `boot_id`, `uptime_secs`, `last_boot`, `state` — after a boot, an update or an agent restart |
| `agent_stopped` | `version` — an orderly stop |
| `fleet_check_in_failed` | `problem` — a check-in failed (refused, unreachable, an untrusted reply); retried with back-off |
| `fleet_check_in_stopped` | `problem` — check-ins STOPPED until the agent is restarted (an enrollment reply that cannot be trusted, or no heartbeat key) |
| `fleet_check_in_ok` | check-ins work again |
| `state_changed` | `part` (`device`, `hardware`, `network`, `services.<name>`, `cameras.<id>`), `from`, `to` |
| `warning_raised` / `warning_cleared` | `part`, `warning` |
| `hotspot_device_joined` | `mac`, `ip`, `hostname`, `signal_dbm` |
| `hotspot_device_left` | `mac`, `ip` |

## Enrolling again — a person's decision

The agent never enrolls again by itself: a refused check-in is logged, reported
in the next heartbeat and retried with back-off (up to every 5 minutes). Only
when the FMS has really lost this device or reset its key, enroll it again by
hand:

```bash
sudo rm /var/lib/ceravis-fleet-agent/identity.json
sudo systemctl restart ceravis-fleet-agent
```

After `fleet_check_in_stopped`, fix the cause (see `problem`), then restart the
agent: `sudo systemctl restart ceravis-fleet-agent`.

## `fleet.jsonl` — the fleet server

| `kind` | Fields |
|---|---|
| `heartbeat` | `ok`; accepted: `rtt_ms`, `state`, `warning_count`, `results_sent`, `orders`, `next_secs`; failed: `status` (HTTP), `error` |
| `enroll` | `ok`; `edge_id` and `origin`, or `status` and `error` |
| `order` | `id`, `order` (its kind), `args`, `ok`, `duration_ms`, `output_chars` |

## `incoming.jsonl` — requests into the device

| `kind` | Fields |
|---|---|
| `request` | `method`, `path`, `query` (parameter NAMES only), `status`, `duration_ms`, `origin` (`fleet`: through the tunnel · `local`: this device · `lan`: the home network), `client` (its IP) |
| `polling` | `window_secs`, `requests` (`"GET /path": count`) — successful reads, counted |

## Why the device last started (`last_boot`)

| Reason | Means |
|---|---|
| `scheduled_reboot` | the nightly reboot |
| `manual_reboot` | a person rebooted it from CERAVIS (password-gated) |
| `system_restart` | an orderly shutdown outside CERAVIS: an engineer, an update |
| `unexpected_restart` | **no orderly shutdown: a power cut or a system crash** — a warning for 15 minutes after it |
| `unknown` | the first start after installation |

## Limits that raise a warning

| What | Warning at |
|---|---|
| memory used | ≥ 90 % |
| a disk (system, data) | ≥ 90 % full; switched to read-only |
| temperature | ≥ 85 °C (`THERMAL_WARN_C`) |
| power | over-current: the board slowed itself (the power supply is too weak) |
| a camera's Wi-Fi signal | below −75 dBm |
| AI detection | not running, or no frame analysed for 2 minutes while cameras deliver video |
| uploads to the app server | the oldest waiting 15 minutes |
| a service | stopped, failed, starting / stopping, or restarted since the last heartbeat |

Wi-Fi signal (dBm, closer to 0 is stronger): above −60 strong · −60 to −70
good · −70 to −75 fair · below −75 weak (frames drop, then the camera
disconnects).

## Never recorded

Passwords, keys, tokens, camera credentials or RTSP addresses with credentials,
request or response bodies of calls into the device, query-parameter values,
snapshots. The care recipient's details appear only in the app-server wire log
and the talk-back log, which never leave the device and are erased when the care
recipient is removed.
