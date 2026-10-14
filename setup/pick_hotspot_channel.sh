#!/usr/bin/env bash
# Pick the best WiFi channel for the camera hotspot — by measuring the cameras
# themselves, never by scanning.
#
# Why no scan: on this radio (Realtek RTL8822CE, rtw88 driver) a scan while it
# runs the hotspot (`iw ... scan ap-force`) can hang the WiFi driver and take
# the device off the network. This script moves the hotspot to each candidate
# channel in turn, waits for every camera to rejoin and settle, pings them, and
# keeps the channel where they answer with the least loss and delay.
#
#   bash setup/pick_hotspot_channel.sh --check   # measure the CURRENT channel only; changes nothing (~40 s)
#   bash setup/pick_hotspot_channel.sh           # try channels 1, 6, 11 and keep the best (~12-15 min)
#
# The full survey is best started in the background, so it finishes even if the
# SSH session drops:
#   nohup bash setup/pick_hotspot_channel.sh > ~/hotspot-channel.log 2>&1 &
#   tail -f ~/hotspot-channel.log
#
# Each channel change drops the cameras for ~10-30 s (live view and recording
# pause; the app reconnects by itself). No sudo: the service user drives
# NetworkManager through the grant setup/install_hotspot.sh installs. Run it
# over Ethernet / the remote tunnel — not from a laptop joined to the hotspot.
# Stopped early (pkill -f pick_hotspot_channel), it puts the old channel back.
set -uo pipefail

CON=ceravis-hotspot
CHANNELS="${CHANNELS:-1 6 11}"   # the three 2.4 GHz channels that do not overlap
SETTLE="${SETTLE:-120}"          # seconds the cameras get to settle before measuring
REJOIN="${REJOIN:-120}"          # seconds to wait for every camera to answer again
PINGS="${PINGS:-60}"             # pings per camera per channel, 0.5 s apart
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CAMERAS_JSON="$ROOT/edge/data/cameras.json"

say() { echo "[$(date +%H:%M:%S)] $*"; }
die() { say "STOP: $*"; exit 1; }

# ---- preflight ------------------------------------------------------------
command -v nmcli >/dev/null || die "nmcli not found"
nmcli -t -f NAME connection show | grep -qx "$CON" || die "no '$CON' connection (start the hotspot first)"
[ "$(nmcli -g GENERAL.STATE connection show "$CON" 2>/dev/null)" = "activated" ] \
    || die "the hotspot is not running (start it on the Hotspot page first)"
if [ "$(id -u)" -ne 0 ] && ! nmcli -t -f PERMISSION,VALUE general permissions 2>/dev/null \
        | grep -q "settings.modify.system:yes"; then
    die "this user may not change NetworkManager — run 'bash setup/install_hotspot.sh' once"
fi
IFACE="$(nmcli -g connection.interface-name connection show "$CON")"
SAVED_CH="$(nmcli -g 802-11-wireless.channel connection show "$CON")"   # 0 = automatic

# Camera name=IP pairs from the device's own camera list (the IP is what the
# hotspot gave each camera; the password in rtsp_url is never printed).
mapfile -t CAMS < <(python3 - "$CAMERAS_JSON" <<'PY'
import json, sys
from urllib.parse import urlsplit
for c in json.load(open(sys.argv[1])):
    host = urlsplit(c.get("rtsp_url") or "").hostname
    if host:
        print(f"{c.get('room_name') or c.get('device_label') or 'camera'}={host}")
PY
)
[ "${#CAMS[@]}" -gt 0 ] || die "no cameras found in $CAMERAS_JSON"

current_channel() { iw dev "$IFACE" info 2>/dev/null | awk '/channel/{print $2; exit}'; }

# "loss% avg_ms max_ms" for one camera; an unreachable camera reads 100 9999 9999.
measure() {
    local out loss rtt
    out="$(ping -c "$PINGS" -i 0.5 -W 1 "$1" 2>/dev/null)"
    loss="$(grep -oE '[0-9.]+% packet loss' <<<"$out" | grep -oE '^[0-9.]+')"
    rtt="$(grep -oE '= [0-9.]+/[0-9.]+/[0-9.]+' <<<"$out" | tr -d '= ' | awk -F/ '{printf "%.0f %.0f", $2, $3}')"
    echo "${loss:-100} ${rtt:-9999 9999}"
}

# Every camera answers a ping again (or the wait runs out).
wait_rejoin() {
    local deadline=$((SECONDS + REJOIN)) cam all
    while [ "$SECONDS" -lt "$deadline" ]; do
        all=1
        for cam in "${CAMS[@]}"; do
            ping -c 1 -W 1 "${cam#*=}" >/dev/null 2>&1 || { all=0; break; }
        done
        [ "$all" -eq 1 ] && return 0
        sleep 3
    done
    return 1
}

set_channel() {
    nmcli connection modify "$CON" 802-11-wireless.channel "$1" \
        && nmcli -w 60 connection up "$CON" >/dev/null
}

# Measure every camera at once; prints one line per camera and returns
# "worst_loss mean_avg worst_max" in $RESULT.
RESULT=""
measure_all() {
    local cam tmp n=0 sum=0 worst_loss=0 worst_max=0 loss avg max
    tmp="$(mktemp -d)"
    for cam in "${CAMS[@]}"; do measure "${cam#*=}" > "$tmp/${cam#*=}" & done
    wait
    for cam in "${CAMS[@]}"; do
        read -r loss avg max < "$tmp/${cam#*=}"
        printf "      %-14s %-15s loss %5s%%   avg %5s ms   worst %5s ms\n" \
               "${cam%%=*}" "${cam#*=}" "$loss" "$avg" "$max"
        worst_loss="$(awk -v a="$worst_loss" -v b="$loss" 'BEGIN{print (b>a)?b:a}')"
        worst_max="$(awk -v a="$worst_max" -v b="$max" 'BEGIN{print (b>a)?b:a}')"
        sum=$((sum + avg)); n=$((n + 1))
    done
    rm -rf "$tmp"
    RESULT="$worst_loss $((sum / n)) $worst_max"
}

say "hotspot '$CON' on $IFACE — channel $(current_channel) now (saved setting: $SAVED_CH, 0 = automatic)"
say "cameras: ${CAMS[*]}"

# ---- --check: measure where we are, change nothing ------------------------
if [ "${1:-}" = "--check" ]; then
    say "measuring channel $(current_channel) — $PINGS pings per camera, nothing is changed"
    measure_all
    read -r wl ma wm <<<"$RESULT"
    say "worst loss ${wl}%, average ${ma} ms, worst single ping ${wm} ms"
    say "healthy = 0% loss and an average under ~30 ms"
    exit 0
fi

# ---- survey ---------------------------------------------------------------
restore() {
    trap - INT TERM
    say "stopped early — putting the saved channel ($SAVED_CH) back"
    set_channel "$SAVED_CH" && wait_rejoin
    exit 130
}
trap restore INT TERM

say "trying channels: $CHANNELS — each change drops the cameras for ~10-30 s"
SUMMARY=""
for ch in $CHANNELS; do
    echo
    say "== channel $ch =="
    if [ "$(current_channel)" != "$ch" ]; then
        set_channel "$ch" || { say "could not start the hotspot on channel $ch — skipped"; continue; }
    fi
    if ! wait_rejoin; then
        say "a camera did not come back within ${REJOIN}s on channel $ch — counted as unusable"
        SUMMARY+="$ch 100 9999 9999 no-rejoin"$'\n'
        continue
    fi
    say "all cameras back; letting them settle ${SETTLE}s before measuring"
    sleep "$SETTLE"
    say "measuring ($PINGS pings per camera)"
    measure_all
    SUMMARY+="$ch $RESULT ok"$'\n'
done

echo
say "== result (lower is better: loss first, then average delay) =="
printf "      %-8s %-11s %-12s %-12s\n" channel "worst loss" "avg delay" "worst ping"
while read -r ch wl ma wm st; do
    [ -n "$ch" ] || continue
    if [ "$st" = "ok" ]; then
        printf "      %-8s %-11s %-12s %-12s\n" "$ch" "${wl}%" "${ma} ms" "${wm} ms"
    else
        printf "      %-8s %s\n" "$ch" "camera did not rejoin"
    fi
done <<<"$SUMMARY"

BEST="$(awk '$5=="ok"' <<<"$SUMMARY" | sort -k2,2n -k3,3n | awk 'NR==1{print $1}')"
trap - INT TERM
if [ -z "$BEST" ]; then
    say "no channel worked — putting the saved channel ($SAVED_CH) back"
    set_channel "$SAVED_CH" && wait_rejoin
    exit 1
fi
if [ "$(current_channel)" != "$BEST" ]; then
    say "best: channel $BEST — moving the hotspot there (cameras drop once more for ~10-30 s)"
    set_channel "$BEST" && wait_rejoin
else
    nmcli connection modify "$CON" 802-11-wireless.channel "$BEST"
fi
say "DONE — hotspot on channel $(current_channel), saved (kept after reboots)"
