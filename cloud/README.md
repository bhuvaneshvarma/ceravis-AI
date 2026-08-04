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

**One domain, routed by path:**
- `/<edge_id>/<ROOM>/` → that home's **MediaMTX** live player page (open).
- `/api/…` → that home's **uvicorn API** — **open** (no Basic Auth), each control
  request carries the `edgeId` and the edge verifies it matches.
- `/ui/…` → that home's **admin pages** — **Basic-Auth** prompt at Caddy.

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

frps uses `vhostHTTPPort = 7080`; Caddy reverse-proxies `:443 → 127.0.0.1:7080`,
Basic-Auth-protects only `/ui/*`, and owns CORS for the whole domain (see §6) so
the app's live links play with no login. First HTTPS request mints the cert.

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
sudo systemctl status frpc      # 'start proxy success' for both proxies
```

`jetson.env` already ships with the domain + STUN set. After verifying the
account, restart and re-sync cameras so the app server stores the links:
```bash
sudo systemctl restart ceravis
```
```
live : https://edgeai.ceravishealth.in/<edge_id>/<ROOM>/
API  : https://edgeai.ceravishealth.in/api/v1/…
pages: https://edgeai.ceravishealth.in/ui/setup.html
```
**Adding another house:** repeat with its own `edge_id`. Nothing changes on EC2.

---

## 5. Test & verify

1. **Tunnels up?** edge: `journalctl -u frpc -f` → two `start proxy success`.
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

### Live link fails with a CORS error (`No 'Access-Control-Allow-Origin' header`)

The browser blocks the WebRTC preflight because the response to the `OPTIONS`
request had no `Access-Control-Allow-Origin`. With the current `Caddyfile` Caddy
answers that preflight itself, so this means either the box is running an OLD
Caddyfile or the request never reaches Caddy's CORS handler. Check, in order:

```bash
# 1) Preflight must return 204 + Access-Control-Allow-Origin (from Caddy).
curl -i -X OPTIONS https://edgeai.ceravishealth.in/<edge_id>/<ROOM>/whep \
     -H 'Origin: https://app.ceravishealth.in' \
     -H 'Access-Control-Request-Method: POST'
#    204 with 'access-control-allow-origin: *'  -> Caddy is correct; go to (2).
#    401 / login prompt                         -> Basic-Auth is over-scoped:
#        redeploy Caddyfile.example (auth must match ONLY /ui) + reload caddy.
#    404 / 502                                  -> not a CORS problem (see 2/3).

# 2) Is the edge reachable through the tunnel? (404 here = frps has no route.)
curl -i https://edgeai.ceravishealth.in/api/v1/system/status
#    If this 404s, the edge's frpc is down OR its locations don't match the
#    edge_id in the URL (a re-verify/redeploy can leave frpc.toml stale):
#      edge:  grep -n 'ceravis:edge-id' /etc/frp/frpc.toml   # must be /<edge_id>
#             sudo ceravis-apply-edge-id <edge_id> && systemctl status frpc

# 3) Is MediaMTX up on the edge?  cd edge && python3 -m tools.status
```

> **Room name must match the link we send.** MediaMTX serves each camera under
> the exact path in the `url` we PUT to `saveCamera` (e.g. `…/<edge_id>/LIVING-ROOM`).
> The app must open **that URL verbatim** and append `/whep` — not rebuild it from
> the room label (`LIVING_ROOM` → `LIVING ROOM`), which points at a path that does
> not exist and 404s *after* CORS passes.

---

## 6. Auth model

- **Live links (`/<edge_id>/<ROOM>/…`)** — **no** login. They play with nothing
  but the URL; the app already gates them behind the family's account. The
  browser reaches them cross-origin (the app runs on `app.ceravishealth.in`), so
  Caddy **owns CORS** for the domain: it answers the WebRTC preflight (`OPTIONS`)
  itself and stamps exactly one `Access-Control-Allow-Origin` on every response.
  This lives in one place (`Caddyfile`) — never re-add auth in front of a live
  link, and never set CORS headers on the edge/MediaMTX side too (two
  `Access-Control-Allow-Origin` values make the browser reject the response).
- **Admin pages (`/ui/*`)** — HTTP Basic Auth at Caddy (the `basic_auth` block).
  Humans get an id/password prompt from anywhere. This is the **only** login on
  the domain; it is scoped to `/ui` and excludes `OPTIONS` so a CORS preflight is
  never challenged.
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
