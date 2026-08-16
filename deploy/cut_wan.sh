#!/usr/bin/env bash
# Cut the Pi's route to the internet for N seconds — without cutting your SSH session.
#
# The AC-6 recovery arc (conversation.session_lost -> system.degraded_entered -> a CueBank phrase
# -> system.degraded_exited) needs a real outage mid-conversation. Until now that meant a human
# unplugging something, which makes it untriggerable from the laptop driving the gate — and
# AVID-189's evidence needs the outage to land *inside* a turn, which hand-timing does badly.
#
# HOW IT CUTS, AND WHY THIS WAY
#
# It blocks egress to everything *outside the LAN*, leaving the local /24 alone. SSH comes from
# 192.168.10.90 to .172 on the same subnet, so the session driving this survives while every
# route to api.openai.com dies. Bringing wlan0 down instead would also work and would strand you:
# the interface carrying the fix is the interface you just switched off.
#
# THE FAILSAFE MATTERS MORE THAN THE CUT
#
# A script that adds a block and is then killed leaves a Pi with no internet and no obvious cause
# — the worst possible outcome of a diagnostic. So the restore is scheduled with `systemd-run`
# BEFORE the block goes on, runs as an independent transient unit, and fires whether or not this
# script is alive to see it. Ctrl-C restores too; the timer is belt to that braces.
#
#   sudo deploy/cut_wan.sh 30        # 30 s outage, restored automatically
#   sudo deploy/cut_wan.sh --restore # panic button: undo any block, now
#
# ⚠️ CLAUDE.md §7.1: a stimulus the harness induces is not a measurement of the robot. A run that
# cuts the network cannot also claim a latency result — grade recovery here, and O1 somewhere else.

set -euo pipefail

TABLE="avid_gate_cut"
GRACE=30 # extra seconds before the failsafe fires, so it never races the normal restore

usage() {
    echo "usage: sudo $0 <seconds> | sudo $0 --restore" >&2
    exit 2
}

restore() {
    nft delete table inet "$TABLE" 2>/dev/null || true
    systemctl stop "avid-wan-restore.timer" 2>/dev/null || true
    echo "WAN restored ($(date +%T))"
}

[[ $# -eq 1 ]] || usage
[[ $EUID -eq 0 ]] || {
    echo "must run as root (nft needs it)" >&2
    exit 1
}

if [[ $1 == "--restore" ]]; then
    restore
    exit 0
fi

[[ $1 =~ ^[0-9]+$ ]] || usage
SECONDS_DOWN=$1

# The LAN to spare. Derived from the live default route rather than hardcoded, so a rig that moves
# to another subnet does not silently lock itself out (deploy/PI_OPERATIONS.md's "read the machine,
# never restate it").
LAN=$(ip -o -f inet addr show scope global | awk '{print $4}' | head -1)
[[ -n $LAN ]] || {
    echo "could not determine the LAN subnet — refusing to cut" >&2
    exit 1
}

trap restore EXIT INT TERM

# Schedule the failsafe FIRST. If this shell dies between here and the block going on, the worst
# case is a restore that had nothing to undo.
systemd-run --unit=avid-wan-restore --on-active="$((SECONDS_DOWN + GRACE))" \
    /usr/sbin/nft delete table inet "$TABLE" >/dev/null 2>&1 || true

nft add table inet "$TABLE"
nft add chain inet "$TABLE" output '{ type filter hook output priority 0; policy accept; }'
nft add rule inet "$TABLE" output ip daddr "$LAN" accept
nft add rule inet "$TABLE" output ip daddr 127.0.0.0/8 accept
nft add rule inet "$TABLE" output ip daddr 0.0.0.0/0 drop

echo "WAN cut for ${SECONDS_DOWN}s at $(date +%T) — LAN ${LAN} and SSH untouched"
sleep "$SECONDS_DOWN"
# `restore` runs from the EXIT trap.
