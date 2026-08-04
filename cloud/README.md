# CERAVIS remote access — fleet tunnel (one domain, many houses)

Reach every home's **live camera links, edge API, and admin pages** from
anywhere over **one shared domain**, **without paying for cloud video**. A small
AWS box runs a reverse-proxy (`frp`) that each Jetson dials **out** to, with
**Caddy** in front for a single Let's Encrypt cert. The box carries only WebRTC
**signaling** + API traffic (KBs); the video goes **peer-to-peer over UDP**
home ↔ viewer (STUN-assisted), so EC2 egress stays near-zero.

```
edgeai.ceravishealth.in                          ┌──────── AWS EC2 ────────┐
   viewer / app / admin ──HTTPS(:443)──► Caddy ──►│ frps HTTP vhost :7080   │
                                     (1 LE cert)  │  route by URL path      │
                                                  └───────────┬─────────────┘
                                     ┌──── House A (frpc) ────┐│ outbound tunnel
   /<edge_id>/<ROOM>/  ─────────────►│  MediaMTX :8889 ◄──────┼┤  (frpc → frps)
   /api/…  ·  /ui/…     ────────────►│  uvicorn  :8000 ◄──────┼┘
                                     └────────────────────────┘
   └─── live video P2P over UDP (WebRTC/ICE, STUN) — DIRECT, never touches EC2 ──┘
```

**One domain, routed by path — every home keyed by its `edge_id`:**
- `/<edge_id>/<ROOM>/` → that home's **MediaMTX** live player page (open).
- `/<edge_id>/api/…` → that home's **uvicorn control API** (PTZ, recordings,
  timeline, camera start/stop/restart, snapshot) — **open**, and routed PER EDGE
  by the `/<edge_id>` prefix so calls always reach the right home. frps routes on
  the URL only (never the request body), so the per-edge path is what makes the
  API multi-house safe; the `edgeId` in the body stays as a second check.
- `/ui/…`, `/stream` → that home's **admin pages** (Basic-Auth at Caddy) and the
  setup JPEG feed — shared, single-operator setup surfaces. `/api` also answers
  here as a **legacy alias** during the switch to per-edge; drop it before adding
  a second house (see `frpc.toml.example`).

Adding a home needs **nothing** on EC2 — a new edge with its own `edge_id`.

**Hybrid transport (the CCTV model):** signaling/API is **TCP** (through the
tunnel over Caddy); the video is **UDP**, peer-to-peer — it never passes here.

**TURN** (relay for the ~10–20% of homes behind strict/symmetric NAT) is a later
add-on; placeholders are in the edge ICE config. Until then remote view works for
most homes; the rest keep AI alerts + snapshots + local playback.

---

## 0. AWS free tier covers it — Yes

A `t3.micro`/`t2.micro` is free-tier eligible. Because **media is P2P**, this box
only relays signaling + API (megabytes), so billed egress stays ~free. (TURN,
later, does relay media for the strict-NAT minority and uses egress.)

---

## 1. Launch the EC2 box

1. **EC2 → Launch** — Amazon Linux 2023 / Ubuntu 22.04+, `t3.micro`. Allocate an
   **Elastic IP** and associate it.
2. **Security group — inbound:**
   | Type | Port | Source | Why |
   |------|------|--------|-----|
   | SSH | 22 | *My IP* | manage the box |
   | HTTP | 80 | 0.0.0.0/0 | Caddy ACME challenge |
   | HTTPS | 443 | 0.0.0.0/0 | Caddy — the one public entry (live + API + UI) |
   | Custom TCP | 7000 | 0.0.0.0/0 | edges connect to frps (token-protected) |
   | Custom TCP | 7001 | *My IP* | *(optional)* fleet SSH over one port (tcpmux) — §6 |

   Keep **7080 CLOSED** (Caddy reaches the frps vhost over loopback).
3. **DNS:** point an A record for `edgeai.ceravishealth.in` at the Elastic IP.

---

## 2. Push this `cloud/` folder to the box

```bash
scp -i ~/Downloads/ceravis-tunnel.pem -r cloud ec2-user@EC2_IP:~/ceravis-cloud
```
(Ubuntu AMI uses `ubuntu@`.) Or `git clone` on the box and `cd cloud`.

---

## 3. Run frps + Caddy on EC2

```bash
ssh -i ~/Downloads/ceravis-tunnel.pem ec2-user@EC2_IP
cd ~/ceravis-cloud

# 1) shared tunnel secret (goes on every edge too)
openssl rand -hex 24            # copy the output
nano frps.toml                  # paste into auth.token
bash install_frps.sh

# 2) TLS front + admin login
caddy hash-password --plaintext 'YOUR_STRONG_ADMIN_PASSWORD'   # copy the hash
cp Caddyfile.example Caddyfile
nano Caddyfile                  # set the domain + paste the hash into basic_auth
bash install_caddy.sh
```

frps uses `vhostHTTPPort = 7080`; Caddy reverse-proxies `:443 → 127.0.0.1:7080`
and Basic-Auth-protects only `/ui/*`. First HTTPS request mints the cert.

---

## 4. Set up a house (edge / Jetson)

`edge_id` is **fully auto-provisioned**. Install the tunnel once with a
placeholder; then verifying the operator's email in the setup wizard makes the
edge read `deviceToken` from the app-server response, save it (`account.json` +
`EDGE_ID` in `jetson.env`), rewrite `frpc.toml`'s `mediamtx-webrtc` `locations`,
and restart frpc — no manual copy. (`install_frpc.sh` installs the
`ceravis-apply-edge-id` helper + a locked-down sudoers rule that lets the app do
that one privileged step.)

```bash
cd ~/ceravis/cloud
cp frpc.toml.example frpc.toml
nano frpc.toml
#   serverAddr = "EC2_IP"   auth.token = "<the shared secret from step 3>"
#   leave mediamtx-webrtc  locations = ["/EDGE_ID"]  # ceravis:edge-id  (auto-filled)
#   keep the ceravis-ssh block if you manage this house remotely!
bash install_frpc.sh            # installs frpc + the apply-edge-id helper + sudoers
sudo systemctl status frpc      # 'start proxy success' x3 (mediamtx-webrtc, ceravis-api, ceravis-ui)
```

`jetson.env` already ships with the domain + STUN set. After verifying the
account, restart and re-sync cameras so the app server stores the links:
```bash
sudo systemctl restart ceravis
```
```
live : https://edgeai.ceravishealth.in/<edge_id>/<ROOM>/
API  : https://edgeai.ceravishealth.in/<edge_id>/api/v1/…   (per-edge — multi-house safe)
pages: https://edgeai.ceravishealth.in/ui/setup.html
```
The app/backend must call the control API with the **`/<edge_id>` prefix** — it
is the routing key that puts each call on the right house. (LAN-direct, off the
tunnel, stays `http://<device-ip>:8000/api/v1/…` — the edge serves both.)
**Adding another house:** repeat with its own `edge_id`. Nothing changes on EC2.

---

## 5. Test & verify

1. **Tunnels up?** edge: `journalctl -u frpc -f` → three `start proxy success`
   (`mediamtx-webrtc`, `ceravis-api`, `ceravis-ui`).
   Caddy: `journalctl -u caddy -f` → cert obtained.
2. **Backbone up?** on the edge: `cd edge && python3 -m tools.status` →
   `mediamtx up: yes`, each camera `ready`.
3. **⚠ On-device WebRTC smoke test (can't be checked off the device).** frp
   forwards the full path, so MediaMTX serves the camera under the slash path
   `<edge_id>/<ROOM>`. RTSP + HLS handle slash-paths; WebRTC is the one to
   confirm — from a phone **on mobile data**, open
   `https://edgeai.ceravishealth.in/<edge_id>/<ROOM>/` → live video.
4. **API + pages:** `…/api/v1/system/status` should answer; `…/ui/setup.html`
   should prompt for the admin login.

---

## 6. Auth model

- **Admin pages (`/ui/*`)** — HTTP Basic Auth at Caddy (the `basic_auth` block).
  Humans get an id/password prompt from anywhere.
- **API (`/api/*`)** — **no** Basic Auth, so the app server calls it freely.
  Instead each control request (PTZ, recording playback) must carry this
  device's `edgeId`, and the edge verifies it **matches** the provisioned value
  (`api/control_auth`). The legacy `X-Ceravis-Control-Token` is **removed**.
  > Note: the `edge_id` also appears in the live-link URL, so treat API-by-edgeId
  > as "targets the right house", not a secret — the family-facing links are the
  > sensitive surface and stay gated by the app account. Add per-view signed
  > links (§7) before wide use.
- **SSH (optional) — one shared port, routed by edge_id (tcpmux):** the whole
  fleet SSHes through ONE server port; frps routes by the HTTP CONNECT host =
  each device's `edge_id`, the same token that keys the live links — no per-house
  port to allocate.
  1. **frps** (`/etc/frp/frps.toml`): uncomment `tcpmuxHTTPConnectPort = 7001`,
     restart frps, and open **7001 to admin IPs only** in the security group.
  2. **Each edge** (`/etc/frp/frpc.toml`): add the `ceravis-ssh` `type="tcpmux"`
     block from `frpc.toml.example` with `customDomains = ["<that device's
     edge_id>"]`, restart frpc.
  3. **Jetson sshd:** `PasswordAuthentication no`, `PubkeyAuthentication yes`
     first — the `edge_id` is only the routing key; the keys are the auth.
  4. **Connect** (any HTTP-CONNECT helper):
     `ssh -o ProxyCommand="ncat --proxy EC2_IP:7001 --proxy-type http %h %p" <user>@<edge_id>`

  *Simpler alternative* (no ProxyCommand, but a port per house): a plain
  `type="tcp"` proxy with a unique `remotePort` (2222, 2223, …) → `ssh -p 2222
  <user>@EC2_IP`.

---

## 7. Later (deferred)

- **TURN** (coturn on this box) for strict-NAT homes — add a `turn:` ICE server
  next to STUN in `livestream/mediamtx_supervisor.py`, open UDP 3478.
- **Tokenized, expiring links** minted per-view by the app server, instead of the
  permanent `edge_id`-prefixed URLs.

---

## Removing it

Edge: `sudo systemctl disable --now frpc`, remove `/etc/frp/frpc.toml` +
unit + binary, clear `EDGE_ID`/`DEVICE_STREAM_BASE`/`MEDIAMTX_STUN_SERVER` in
`jetson.env`, `sudo systemctl restart ceravis` (links revert to LAN-direct).
EC2: `sudo systemctl disable --now caddy frps` or Stop/Terminate the instance.
