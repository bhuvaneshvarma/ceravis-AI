# CERAVIS remote live access — Option B (reverse tunnel)

Make the camera live links reachable **from anywhere**, not just the home LAN,
**without paying for cloud video**. A tiny AWS box runs a reverse-proxy server
(`frp`) that the Jetson dials **out** to. That box carries only the WebRTC
**signaling** (a few KB per view); the actual video goes **peer-to-peer**
home ↔ viewer (STUN-assisted), so EC2 data transfer stays near-zero.

```
[Jetson edge]  --outbound tunnel (frpc→frps)-->  [AWS EC2: frps]  <-- viewers open
  MediaMTX :8889                                   public :8889       the link here
       \                                                                  /
        \________ video P2P, STUN-assisted, DIRECT (never touches EC2) __/
```

TURN relay (for the ~10–20% of homes behind strict/symmetric NAT where P2P
can't punch through) is a **later** add-on. Until then, remote view works for
most homes; the rest still have AI alerts + snapshots + local playback.

**This whole feature is isolated and removable** — see *Removing it* at the end.
Nothing in `edge/` changes except one optional, empty-by-default setting
(`MEDIAMTX_STUN_SERVER`).

---

## 0. Does the AWS free account cover it? — Yes

A `t3.micro` (or `t2.micro`) instance is free-tier eligible and easily inside
the signup credits. Because **media is P2P**, this box only relays signaling, so
its data-transfer-out (the part AWS bills, ~$0.09/GB) stays in the megabytes —
effectively free. (When TURN is added later, TURN *does* relay media and *will*
use egress — so we only ever relay the strict-NAT minority through it.)

---

## 1. Launch the EC2 box (AWS console)

1. **EC2 → Launch instance.**
2. **Name:** `ceravis-tunnel`.
3. **AMI:** Amazon Linux 2023 (or Ubuntu 22.04+).
4. **Instance type:** `t3.micro` (free-tier eligible).
5. **Key pair:** create/download one (`ceravis-tunnel.pem`) — this is your SSH key.
6. **Network / Security group → create, with these inbound rules:**
   | Type | Protocol | Port | Source | Why |
   |------|----------|------|--------|-----|
   | SSH | TCP | 22 | *My IP* | you, to manage it |
   | Custom TCP | TCP | 7000 | 0.0.0.0/0 | edge frpc connects here (token-protected) |
   | Custom TCP | TCP | 8889 | 0.0.0.0/0 | viewers reach the tunneled WebRTC signaling |
7. **Launch.** Note the **Public IPv4 address** (e.g. `13.51.x.x`) — call it `EC2_IP`.
   - Optional: allocate an **Elastic IP** and associate it, so the address
     survives a reboot. Recommended (the link you send the cloud is this IP).

---

## 2. Push this `cloud/` folder to the EC2 box

From your dev machine (where this repo lives). Replace `EC2_IP` and the key path:

```bash
# copy just the cloud/ folder up to the box
scp -i ~/Downloads/ceravis-tunnel.pem -r cloud \
    ec2-user@EC2_IP:~/ceravis-cloud
```

(Ubuntu AMI uses `ubuntu@` instead of `ec2-user@`.) Alternatively, if the repo
is reachable from the box: `git clone <repo> && cd <repo>/cloud`.

---

## 3. Run the tunnel SERVER on EC2

SSH in and install:

```bash
ssh -i ~/Downloads/ceravis-tunnel.pem ec2-user@EC2_IP
cd ~/ceravis-cloud

# 1) set a shared secret (same value goes on the edge)
#    edit auth.token in frps.toml — make it long and random:
openssl rand -hex 24            # copy the output
nano frps.toml                  # paste into auth.token

# 2) install + start
bash install_frps.sh
sudo systemctl status frps      # should be active (running)
```

---

## 4. Set up the edge (Jetson) client

On the Jetson (the repo is at `~/ceravis`):

```bash
cd ~/ceravis/cloud
cp frpc.toml.example frpc.toml
nano frpc.toml
#   serverAddr = "EC2_IP"
#   auth.token = "<the same secret from step 3>"

bash install_frpc.sh
sudo systemctl status frpc      # look for 'start proxy success'
```

Then point the links at the tunnel and enable STUN — add these two lines to
`edge/infra/env/jetson.env`:

```
DEVICE_STREAM_BASE=https://EC2_IP
MEDIAMTX_STUN_SERVER=stun:stun.l.google.com:19302
```

and restart the app so MediaMTX regenerates its config and the links update:

```bash
sudo systemctl restart ceravis
```

---

## 5. Test & verify

1. **Tunnel up?** On EC2: `journalctl -u frps -f`; on edge: `journalctl -u frpc -f`
   → `start proxy success`.
2. **Link generated right?** On the edge:
   ```bash
   python edge/tests/test_cloud.py        # prints the saveCamera payload
   ```
   URLs should now read `https://EC2_IP:8889/cam_001` and `media backbone: UP`.
3. **Watch from off-LAN:** open `https://EC2_IP:8889/cam_001` on a phone **on
   mobile data** (not the home WiFi). Accept the self-signed-cert warning once →
   live video. (A clean cert with no warning needs a domain — see *Later*.)
4. **Register the links:** re-sync cameras (wizard camera step, or
   `POST /api/v1/account/sync-cameras`) so the app server stores the global URLs.

If the page loads but video stalls, that home is behind strict NAT and needs the
TURN fallback (Later) — alerts, snapshots and local playback still work meanwhile.

---

## Removing it completely (nothing left behind)

Edge:
```bash
sudo systemctl disable --now frpc
sudo rm -f /etc/frp/frpc.toml /etc/systemd/system/frpc.service /usr/local/bin/frpc
# remove the two lines from edge/infra/env/jetson.env, then:
sudo systemctl restart ceravis      # links revert to the LAN IP
```
EC2: just **Stop/Terminate** the instance in the console.
Repo (optional): `git rm -r cloud/`. The only `edge/` footprint is the
`mediamtx_stun_server` setting, which is empty by default and a no-op when unset.

---

## Later (deferred, on purpose)

- **Domain + Let's Encrypt** at `frps` (frp vhost/HTTPS) → browser-trusted
  `https://edge.ceravishealth.in/cam_001`, no cert warning.
- **TURN** (coturn on the same box) for strict-NAT homes → add its URL to
  `MEDIAMTX_STUN_SERVER`'s sibling ICE config; only that minority relays media.
- **Tokenized, expiring links** minted per-view by the app server — a care
  product must not hand out permanent public URLs.
