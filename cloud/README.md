# CERAVIS remote live access — fleet tunnel (one domain, many houses)

Make every home's camera live links reachable **from anywhere**, over **one
shared domain and one shared port**, **without paying for cloud video**. A small
AWS box runs a reverse-proxy server (`frp`) that each Jetson dials **out** to,
with **Caddy** in front for a single Let's Encrypt certificate. That box carries
only the WebRTC **signaling** (a few KB per view); the actual video goes
**peer-to-peer over UDP** home ↔ viewer (STUN-assisted), so EC2 data transfer
stays near-zero.

```
                         ┌──────────────── AWS EC2 ────────────────┐
viewer ──HTTPS(:443)──►  │ Caddy ──HTTP──► frps vhost(:7080) ──┐   │
  the link               │ (1 LE cert)     route by /<edge_id> │   │
                         └─────────────────────────────────────┼───┘
                                                                │  outbound tunnel
                                    ┌──── House A (frpc) ────┐   │  (frpc → frps)
                                    │  MediaMTX :8889 ◄──────┼───┘  /home-A/…
                                    └────────────────────────┘
   └──────── video P2P over UDP (WebRTC/ICE, STUN) — DIRECT, never touches EC2 ────┘
```

**Multi-house routing.** Every edge dials in with `type="http"` +
`locations=["/<edge_id>"]` + the same `customDomains`. frps routes each request
to the right house purely by the **first path segment** `/<edge_id>`. Adding a
home needs **nothing** on EC2 — just a new edge with its own `edge_id`. No
per-house ports, no per-house subdomains.

**Hybrid transport (the CCTV model).** Signaling is **TCP** (the WHEP handshake,
carried through the frp tunnel over Caddy). The video itself is **UDP** and
travels **peer-to-peer** (WebRTC/ICE, STUN-assisted) — it never passes through
this box. That is why EC2 egress stays near-zero.

**TURN** (relay for the ~10–20% of homes behind strict/symmetric NAT where P2P
can't punch through) is a **later** add-on — placeholders are already in the edge
ICE config and noted below. Until then remote view works for most homes; the
rest still have AI alerts + snapshots + local playback.

---

## 0. Does the AWS free account cover it? — Yes

A `t3.micro` (or `t2.micro`) is free-tier eligible. Because **media is P2P**, this
box only relays signaling, so data-transfer-out (the part AWS bills, ~$0.09/GB)
stays in the megabytes — effectively free. (When TURN is added later it *does*
relay media for the strict-NAT minority, and *that* uses egress.)

---

## 1. Launch the EC2 box

1. **EC2 → Launch instance** — Amazon Linux 2023 or Ubuntu 22.04+, `t3.micro`.
2. Allocate an **Elastic IP** and associate it (the domain's A record points here).
3. **Security group — inbound rules:**
   | Type | Port | Source | Why |
   |------|------|--------|-----|
   | SSH | 22 | *My IP* | manage the box |
   | HTTP | 80 | 0.0.0.0/0 | Caddy ACME challenge |
   | HTTPS | 443 | 0.0.0.0/0 | Caddy — the public link entry |
   | Custom TCP | 7000 | 0.0.0.0/0 | edges connect to frps (token-protected) |

   **Do NOT open 7080** — that's the frps HTTP vhost; Caddy reaches it over
   loopback. Leaving it closed keeps the untunneled edges private.
4. **DNS:** point an A record for your domain (e.g. `edge.ceravishealth.in`) at
   the Elastic IP.

---

## 2. Push this `cloud/` folder to the box

```bash
scp -i ~/Downloads/ceravis-tunnel.pem -r cloud ec2-user@EC2_IP:~/ceravis-cloud
```
(Ubuntu AMI uses `ubuntu@`.) Or `git clone` the repo on the box and `cd cloud`.

---

## 3. Run the tunnel server (frps) + TLS front (Caddy)

```bash
ssh -i ~/Downloads/ceravis-tunnel.pem ec2-user@EC2_IP
cd ~/ceravis-cloud

# 1) shared tunnel secret (goes on every edge too)
openssl rand -hex 24            # copy the output
nano frps.toml                  # paste into auth.token
bash install_frps.sh
sudo systemctl status frps      # active (running)

# 2) TLS front — one Let's Encrypt cert for the whole fleet
cp Caddyfile.example Caddyfile
nano Caddyfile                  # set your domain
bash install_caddy.sh
sudo systemctl status caddy
```

`frps.toml` already sets `vhostHTTPPort = 7080`; Caddy reverse-proxies `:443 →
127.0.0.1:7080`. The first HTTPS request mints the cert (needs the A record live
and 80/443 open).

---

## 4. Set up a house (edge / Jetson)

Each home is identical **except its `edge_id`** — a long unguessable token that
is effectively the access key to that home's cameras (make it a UUID-style
value, not `home_1234`).

```bash
cd ~/ceravis/cloud
cp frpc.toml.example frpc.toml
nano frpc.toml
#   serverAddr    = "EC2_IP"                     (or the domain)
#   auth.token    = "<the shared secret from step 3>"
#   customDomains = ["edge.ceravishealth.in"]    (the shared domain)
#   locations     = ["/<edge_id>"]               (THIS house's token)
bash install_frpc.sh
sudo systemctl status frpc      # look for 'start proxy success'
```

Then set the matching values in `edge/infra/env/jetson.env` and restart:

```
EDGE_ID=<the same token you put in locations>
DEVICE_STREAM_BASE=https://edge.ceravishealth.in
MEDIAMTX_STUN_SERVER=stun:stun.l.google.com:19302
```
```bash
sudo systemctl restart ceravis
```

The edge now serves **plaintext** WebRTC (TLS is Caddy's job — `tls_enabled()`
auto-offloads when `DEVICE_STREAM_BASE` is set). Re-sync cameras (wizard camera
step, or `POST /api/v1/account/sync-cameras`) so the app server stores the links:

```
https://edge.ceravishealth.in/<edge_id>/<camera>/whep
```

**Adding another house later:** repeat step 4 with a new `edge_id`. Nothing on
EC2 changes.

---

## 5. Test & verify

1. **Tunnels up?** EC2: `journalctl -u frps -f`; edge: `journalctl -u frpc -f`
   → `start proxy success`. Caddy: `journalctl -u caddy -f` → cert obtained.
2. **Backbone up?** On the edge: `cd edge && python3 -m tools.status` →
   `mediamtx up: yes`, each camera `ready`.
3. **⚠ On-device WebRTC smoke test (the one thing that can't be checked off the
   device).** frp forwards the full path, so MediaMTX serves the camera under the
   slash path `<edge_id>/<cam>`. RTSP and HLS handle slash-paths; WebRTC/WHEP is
   the one to confirm. From a phone **on mobile data** (not home WiFi), open the
   built-in player page `https://edge.ceravishealth.in/<edge_id>/<cam>` → live
   video. If it plays, the `/whep` link the app stores works too. If the page
   loads but video stalls, that home is behind strict NAT and needs TURN (below).
4. **Register the links:** re-sync cameras so the app server stores the URLs.

---

## 6. (Optional) Admin pages + SSH from anywhere

The **same frpc tunnel** can also expose this edge's admin UI and SSH for remote
maintenance. Both are **off by default** and are added as extra `[[proxies]]` in
`frpc.toml` (the commented blocks are in `frpc.toml.example`).

> **⚠ Do NOT route the admin UI through the shared live-stream domain.** The edge
> API has **no built-in authentication**, and the app is not path-prefix aware,
> so a `/…` route behind Caddy would be both broken and world-open. Use a
> per-house **TCP tunnel locked to your IP**.

**Admin pages (uvicorn `:8000` — setup wizard, monitor, live wall):**
1. In `frpc.toml`, uncomment the `ceravis-admin` block; give this house a unique
   `remotePort` (e.g. `8001`).
2. In the EC2 security group open that TCP port with **Source = *My IP*** only.
3. `sudo systemctl restart frpc`, then browse **`http://EC2_IP:8001/ui/setup.html`**.
   (Plaintext but IP-locked. For public access add TLS via a separate Caddy
   `admin.<domain>` subdomain later.)

**SSH into the Jetson:**
1. Make Jetson SSH **key-only** first: `ssh-copy-id <user>@<jetson-lan-ip>`, then
   set `PasswordAuthentication no` in `/etc/ssh/sshd_config` + restart sshd.
2. Uncomment the `ceravis-ssh` block; give this house a unique `remotePort`
   (e.g. `2222`). Open it in the security group (0.0.0.0/0 is OK once key-only,
   else *My IP*).
3. `sudo systemctl restart frpc`, then from anywhere: `ssh -p 2222 <user>@EC2_IP`.

Every house gets its own `remotePort`s so they never collide — the same "one
server, disambiguate per house" idea as the live-stream path routing.

---

## 7. Later (deferred, on purpose)

- **TURN** (coturn on this same box) for strict-NAT homes. The edge ICE config
  already has the placeholder — add a `turn:<host>:3478` entry as a second ICE
  server next to the STUN one (`livestream/mediamtx_supervisor.py`), open UDP
  3478, and only that minority relays media.
- **Tokenized, expiring links** minted per-view by the app server — a care
  product should not hand out permanent public URLs. The `edge_id` prefix is
  unguessable but permanent; short-lived signed links are the next step.

---

## Removing it completely

Edge:
```bash
sudo systemctl disable --now frpc
sudo rm -f /etc/frp/frpc.toml /etc/systemd/system/frpc.service /usr/local/bin/frpc
# clear EDGE_ID / DEVICE_STREAM_BASE / MEDIAMTX_STUN_SERVER in jetson.env, then:
sudo systemctl restart ceravis      # links revert to LAN-direct
```
EC2: `sudo systemctl disable --now caddy frps`, or just **Stop/Terminate** the
instance. Repo: `git rm -r cloud/`; the only `edge/` footprint is three
env values, all no-ops when blank.
