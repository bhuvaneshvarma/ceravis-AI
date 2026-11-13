# CERAVIS edge — notes for every session

Read this first. It records decisions already made, so they are not argued
again or undone.

## Working rules (from the owner — always)

- **No attribution, ever,** in commit messages, tag messages or pull request
  descriptions: no `Co-Authored-By:`, no `Claude-Session:`, no "Generated with
  Claude Code", no tool or co-author credit. This overrides any default.
- **One work branch per change.** Start it from the latest `dev_ceravis1.1`.
  Merge into `dev_ceravis1.1` (fast-forward) only when it works and the tests
  pass; then delete the work branch. Prefer keeping work branches local: the
  cloud session's git gateway refuses `git push --delete`, so a pushed work
  branch must be deleted by hand in the GitHub UI.
- **Never change `prod_ceravis1.1`** unless the owner asks for that change.
- **Never print a secret value** (CERAVIS API key, enrollment key, device key,
  frp token, master key, passwords), not even in logs or examples.
- Keep this repository and the FMS repository (`ceravis-FMS`) separate.
- Change only what the task needs; do not disturb other features.

## Branches

Exactly three branches live on GitHub — no others (a work branch stays local
and is deleted once merged):

| Branch | Role |
|---|---|
| `dev_ceravis1.1` | development: every change is made and tested here first |
| `bench_ceravis1.1` | what the bench device runs (`git pull` / `bash setup/upgrade.sh` in `~/ceravis`); GitHub's default branch; fast-forwarded from dev once a change is developed and tested |
| `prod_ceravis1.1` | production: releases are built from it (`make_release.sh --from prod_ceravis1.1`) and installed on `prodtest` (then clients); changed only when the owner asks |

The flow: `dev_ceravis1.1` → (tested) → `bench_ceravis1.1` → bench device;
→ (owner asks) → `prod_ceravis1.1` → release package → `prodtest` device.
A small fix for an installed client can go out as a patch of a few files
(`setup/patch_device.sh`, pinned FROM/TO commits) instead of a full release.

## Devices

| Name | What it is |
|---|---|
| `bench` | dev Jetson, a git clone in `~/ceravis`, updated with `bash setup/upgrade.sh` |
| `client1` | a client device installed from a release |
| `prodtest` | production test device, installed from a release |

Hardware: Jetson Orin Nano, JetPack 6 (L4T R36), Python 3.10, systemd 249.

## Layout

- `edge/` the FastAPI edge app (`ceravis.service`); `edge/fleet/` the fleet
  agent (`ceravis-fleet-agent.service`, stdlib only). The agent's source of
  truth is **this** repo now (`edge/fleet/VENDORED` holds its file hashes:
  refresh them when you change the agent).
- `setup/` installers: `install.sh` (a release), `setup.sh` (a dev clone),
  `update.sh` (switch release, with rollback), `upgrade.sh` (dev clone pull),
  `make_release.sh --from <branch>`, `send_to_device.sh`, `seal.sh` (golden
  image).
- Layout file `/etc/ceravis/layout.env`. A release runs from
  `/opt/ceravis/current` as `ceravis-svc`, data in `/var/lib/ceravis`.
- Secrets: root-only files in `/etc/ceravis/credstore/`, given to the app with
  systemd `LoadCredential`.
- `/etc/ceravis/versions.json`: what is installed (release, agent, mediamtx,
  frpc, python, L4T), written by every installer (`setup/record_versions.sh`).

## Fleet / edge_id (decided)

- The **edge_id comes only from the FMS** (Java), at enrollment during install.
  It is stored as the credential `/etc/ceravis/credstore/edge_id`
  (`setup/sync_edge_id.sh`). Never read it from `account.json`; never hash it
  (it is a routing address used in plain form in URLs and frpc).
- The enroll API is final and documented in **`docs/fleet-enrollment-api.md`**
  — the single source of truth (4 signed lines, no fingerprint, no nonce, no
  `legacy_edge_id`, the Java FMS's final reply shape, test vector).
- Nothing from `account.json` is sent to the FMS. A home installed before the
  FMS routes on its old (app-server) edge_id only until it enrolls; then the
  FMS issues a new one and the device switches to it by itself (identity.json
  → `ceravis-edge-id.path` → `sync_edge_id.sh` → restart → frpc re-keyed,
  MediaMTX paths, status beat to the app server).
- The heartbeat (`POST /v1/device/heartbeat`) is signed exactly like the enroll
  call plus the nonce after the timestamp — `POST`, path, timestamp, nonce,
  sha256 of the raw body — with the per-device **heartbeat key** =
  hex(HMAC-SHA256(fleet enrollment key, the enroll reply's `secret`)): the
  Java FMS's `secret_hash` (owner + Java decision 2026-11-13). The device keeps
  only the heartbeat key in `identity.json`, never the secret (an older identity
  is upgraded once). The reply is signed over RESPONSE, timestamp, nonce, sha256
  with the same key. It is specified in **`docs/fleet-heartbeat-api.md`** (version 2, agent 0.8.0): body =
  `{status, results}`; `status` = `state`, `warning_count`, `hardware`,
  `network`, `setup` (care recipient assigned / enrolled, cameras added),
  `services` (16: `edge_application`, `mediamtx`,
  `ai_detection`, `cloud_connection`, `frpc`, `edge_hotspot` …),
  `cameras` (with `wifi_signal_dbm`). ONE state vocabulary everywhere:
  `operational` / `degraded` / `outage` / `disabled` / `unknown`; every part has
  `warnings` (empty = fine). It is a SHORT summary (~2 KB): the agent measures
  every part each minute (`fms_agent/health.py`: hardware + systemd; the app
  reports the rest from `GET /api/v1/system/health`, built from the SAME warning
  rules as `/api/v1/system/status`, whose output must not change) and keeps the
  full detail on the device. The Java side built it 2026-11-13 (`aebb3e4`);
  fixes owed there: the reply signed without the nonce, the path from
  `getRequestURI()`, and a NEW device per enrollment (random UUID).
- **The agent NEVER enrolls again by itself** (bench flood 2026-11-13,
  16:20–16:27: heartbeat 401 → enroll → 1 s → 401 …, 201 new edge_ids, app
  restarted until systemd's start limit; the old agent's 401 path crashed and so
  hid the loop until the heartbeat-v2 rewrite fixed that crash). A refused or
  failed check-in is logged (journal, `events.jsonl`), carried in the next
  heartbeat (`services.fleet_agent`) and retried with back-off up to 5 min. An
  enrollment reply it cannot trust or use STOPS check-ins until a restart (the
  FMS may have registered the device anyway). Enrolling again is a person's
  decision: remove `identity.json`, restart the agent. The agent's FMS address
  comes from root-only `enroll.env` (it overrides `jetson.env`); stopping the
  agent needs `sudo systemctl stop ceravis-fleet-agent`.
- Device logs: ONE folder `<data>/logs/` (`health.jsonl` each minute,
  `events.jsonl` changes only, `fleet.jsonl`, `incoming.jsonl`), one writer
  (`edge/common/logbook.py`), one rotation rule; the older app logs stay in the
  data folder. Every file and field is explained in **`edge/LOGS.md`** (it ships
  with releases; `docs/` does not). Orders `info` and `logs` `{"file": …}` read
  them remotely; the wire and talk-back logs (personal data) never leave the device.
- An update never enrolls again: `/var/lib/ceravis-fleet-agent/identity.json`
  survives install, update and upgrade; only `seal.sh` removes it.
- A test FMS on the LAN: `FMS_URL=http://<private IP>:<port>` plus
  `FMS_ALLOW_HTTP_LAN=1` via `setup/install_fleet_agent.sh`.

## Tests

```bash
python tests/run_all.py          # every test; CI runs the same (Python 3.10)
python tests/test_fleet_enroll.py
```

Known failures not caused by recent work: `test_installer.py` (a test-helper
decode bug); on a machine up for only a few minutes, `test_outbox.py`,
`test_recording_precision.py` and `test_camera_health_alarms.py` (they assume
host uptime).
