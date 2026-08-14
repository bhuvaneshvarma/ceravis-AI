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

**One domain, EVERYTHING routed per home under `/<edge_id>/…`:**
- `/<edge_id>/<ROOM>/` → that home's **MediaMTX** live player page (open).
- `/<edge_id>/api/…` → that home's **uvicorn control API** (PTZ, recordings,
  timeline, camera start/stop/restart, snapshot) — open.
- `/<edge_id>/ui/…` → that home's **admin pages** (Basic-Auth at Caddy).

No video crosses the tunnel as pixels: the admin pages play the same
`/<edge_id>/<ROOM>/whep` WebRTC stream as the live links.

frps routes on the URL **only** (never the request body), so putting the
`edge_id` in the PATH is what makes it multi-house safe: every call can only
reach the home whose `edge_id` it carries — no shared route can collide. There is
**no** shared `/api` or `/ui` anymore; the `edgeId` in the body stays
as a second check. Two frpc proxies do it: `mediamtx-webrtc` (`/<edge_id>` →
:8889) and `ceravis-api` (`/<edge_id>/api|ui` → :8000); frps picks the
longest match, so `whep` falls to MediaMTX and the rest to uvicorn.

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
   | Custom TCP | 7001 | *My IP* | fleet SSH for the whole fleet, one port (tcpmux) — §6 |

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
`EDGE_ID` in `jetson.env`), key **every** frpc proxy to it, and restart frpc — no
manual copy. (`install_frpc.sh` installs the `ceravis-apply-edge-id` helper + a
locked-down sudoers rule that lets the app do that one privileged step.)

One `edge_id`, written by machine into all three routes — so none can drift:

| proxy | key | value written |
|-------|-----|---------------|
| `mediamtx-webrtc` | `locations` | `["/<edge_id>"]` — a URL **path**, leading slash |
| `ceravis-api` | `locations` | `["/<edge_id>/api", …/ui]` |
| `ceravis-ssh` | `customDomains` | `["<edge_id>"]` — a CONNECT **host**, **no** slash |

```bash
cd ~/ceravis/cloud
cp frpc.toml.example frpc.toml
nano frpc.toml
#   serverAddr = "EC2_IP"   auth.token = "<the shared secret from step 3>"
#   leave every  # ceravis:edge-id / # ceravis:ssh-edge-id  line as-is (auto-filled)
#   comment out the ceravis-ssh block only if this house must NOT be SSH-able
bash install_frpc.sh            # installs frpc + the apply-edge-id helper + sudoers
sudo systemctl status frpc      # 'start proxy success' x3 (webrtc, api, ssh)
```

`jetson.env` already ships with the domain + STUN set. After verifying the
account, restart and re-sync cameras so the app server stores the links:
```bash
sudo systemctl restart ceravis
```
```
live  : https://edgeai.ceravishealth.in/<edge_id>/<ROOM>/
API   : https://edgeai.ceravishealth.in/<edge_id>/api/v1/…
pages : https://edgeai.ceravishealth.in/<edge_id>/ui/setup.html
```
Every fleet URL carries the **`/<edge_id>` prefix** — it is the routing key that
puts each call on the right house. (LAN-direct, off the tunnel, stays
`http://<device-ip>:8000/api/v1/…` and `/ui/…` — the edge strips the prefix
internally, so it serves both forms.)
**Adding another house:** repeat with its own `edge_id`. Nothing changes on EC2.

---

## 5. Test & verify

1. **Tunnels up?** edge: `journalctl -u frpc -f` → two `start proxy success`
   (`mediamtx-webrtc`, `ceravis-api`).
   Caddy: `journalctl -u caddy -f` → cert obtained.
2. **Backbone up?** on the edge: `cd edge && python3 -m tools.status` →
   `mediamtx up: yes`, each camera `ready`.
3. **⚠ On-device WebRTC smoke test (can't be checked off the device).** frp
   forwards the full path, so MediaMTX serves the camera under the slash path
   `<edge_id>/<ROOM>`. RTSP + HLS handle slash-paths; WebRTC is the one to
   confirm — from a phone **on mobile data**, open
   `https://edgeai.ceravishealth.in/<edge_id>/<ROOM>/` → live video.
4. **API + pages:** `…/<edge_id>/api/v1/system/status` should answer;
   `…/<edge_id>/ui/setup.html` should prompt for the admin login.

---

## 6. Auth model

- **Admin pages (`/<edge_id>/ui/*`)** — HTTP Basic Auth at Caddy (the
  `basic_auth` block, matched by `path_regexp ^/[^/]+/ui`). Humans get an
  id/password prompt from anywhere.
- **API (`/<edge_id>/api/*`)** — **no** Basic Auth, so the app server calls it
  freely. Instead each control request (PTZ, recording playback) must carry this
  device's `edgeId`, and the edge verifies it **matches** the provisioned value
  (`api/control_auth`). The legacy `X-Ceravis-Control-Token` is **removed**.
  > Note: the `edge_id` also appears in the live-link URL, so treat API-by-edgeId
  > as "targets the right house", not a secret — the family-facing links are the
  > sensitive surface and stay gated by the app account. Add per-view signed
  > links (§7) before wide use.
- **SSH — one shared port, routed by edge_id (tcpmux):** the whole fleet SSHes
  through ONE server port; frps routes by the HTTP CONNECT host = each device's
  `edge_id`, the same token that keys the live links — no per-house port to
  allocate, nothing to change on EC2 when a house is added. The session inside is
  end-to-end encrypted; frps sees only the `edge_id` it routes on.
  1. **frps** (`/etc/frp/frps.toml`): `tcpmuxHTTPConnectPort = 7001` (shipped
     enabled), restart frps, and open **7001 to admin IPs only** in the SG.
  2. **Each edge**: nothing manual — the `ceravis-ssh` block ships in
     `frpc.toml.example` and `ceravis-apply-edge-id` fills in its `customDomains`.
  3. **Jetson sshd:** `PasswordAuthentication no`, `PubkeyAuthentication yes`
     first — the `edge_id` is only the routing key; the keys are the auth.
  4. **Connect.** Reaching a CONNECT-multiplexed port needs a client that speaks
     HTTP CONNECT, which Windows has none of, so `cloud/fleet_ssh_proxy.py` is
     that client — stdlib Python, nothing to install, same command on every OS:

     ```bash
     python cloud/fleet_ssh_proxy.py --via EC2_IP:7001 --check <edge_id>
     ```
     `--check` proves the path without ssh: it opens the tunnel and reads the
     Jetson's sshd banner back, so a failure names its own layer (port shut, no
     edge under that `edge_id`, or sshd down). Then:
     ```bash
     ssh -o ProxyCommand="python cloud/fleet_ssh_proxy.py --via EC2_IP:7001 %h %p" ceravis@<edge_id>
     ```
     Better, put it in `~/.ssh/config` once and the daily command is `ssh house-a`:
     ```
     Host house-a
         HostName     <edge_id>
         User         ceravis
         ProxyCommand python C:/path/to/cloud/fleet_ssh_proxy.py --via EC2_IP:7001 %h %p
     ```
     (`ncat --proxy EC2_IP:7001 --proxy-type http %h %p` is equivalent where Nmap
     is installed. `ssh` reporting **`CreateProcessW failed error:2`** or
     **`posix_spawnp: No such file or directory`** means the ProxyCommand binary
     is missing on *your* machine — ssh never reached the network, so it says
     nothing about the tunnel.)

  **`HTTP/1.1 404 Not Found` from `--check`** is the one failure whose cause is
  *not* where you'd look. It means frps has no tcpmux route for that `edge_id` —
  and a correct `frpc.toml` does **not** disprove it: a proxy that fails to
  register stays failed until **frpc** restarts, so enabling
  `tcpmuxHTTPConnectPort` *after* the edges are up leaves a flawless config with
  nothing registered (the other proxies in the same frpc keep working, which is
  what makes it convincing). The running state is the authority, not the file:
  ```bash
  sudo journalctl -u frps --no-pager | grep -iE 'tcpmux|ceravis-ssh' | tail   # EC2
  sudo systemctl restart frpc && sudo journalctl -u frpc -n 40 --no-pager     # edge
  ```
  Also check `customDomains` holds the **bare** `edge_id` — a leading slash is the
  live-link `locations` shape and 404s here. `ceravis-apply-edge-id` writes both
  shapes correctly, so this only bites a hand-edited config.

  *Simpler alternative* (no ProxyCommand, but a port per house, and every one of
  them must be opened in the SG): a plain `type="tcp"` proxy with a unique
  `remotePort` (2222, 2223, …) → `ssh -p 2222 <user>@EC2_IP`. Fine for one bench
  device; it does not scale to a fleet.

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
