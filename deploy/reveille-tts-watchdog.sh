#!/usr/bin/env bash
# reveille-tts-watchdog -- keep the synthesizer answering, escalating only as far
# as the failure demands (S3.1, ruled 12084 at operator ask 12082).
#
# WHY A WATCHDOG AND NOT JUST Restart=on-failure. systemd restarts a unit whose
# PROCESS died. The failure this exists for is the opposite: the container is up,
# the HTTP port answers, and the model behind it is gone -- CUDA fell out from
# under it, or it loaded onto the CPU after the card went missing and now serves
# at 1.6x realtime instead of 0.39. systemd sees a healthy process and does
# nothing, forever. So the probe asserts the MODEL, not the port.
#
# THE ESCALATION IS ORDERED BY BLAST RADIUS, and each step only happens because
# the cheaper one already failed three times in a row:
#   3 consecutive fails  -> restart the unit          (the container is wedged)
#   6 consecutive fails  -> reload nvidia_uvm, restart (the driver is wedged --
#                           this host suspends, and suspend wedges nvidia_uvm)
#   9 consecutive fails  -> reboot the host           (nothing softer worked)
#   nvidia-smi itself dead -> reboot immediately, no ladder: if the driver cannot
#                           be talked to at all, restarting a container is theatre.
#
# NEVER TWICE IN AN HOUR. A reboot loop on a host that cannot fix itself is worse
# than a host that is down and obviously down: it destroys the evidence every
# time it comes back. The timestamp is persisted so the guard survives the very
# reboot it is guarding.
#
# THE BROKER IS NEVER TOUCHED. A synthesis outage is not a broker outage -- the
# voice worker retries, messages still flow, and restarting the bus to fix a
# speaker would take the fleet down for a cosmetic failure.
set -uo pipefail

UNIT="${WATCHDOG_UNIT:-reveille-tts.service}"
URL="${WATCHDOG_URL:-http://127.0.0.1:8004}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/var/lib/reveille-tts-watchdog}"

# Thresholds. Consecutive failures, not cumulative: any single good probe clears
# the count, because a model that answers is a model that works.
FAIL_RESTART=3
FAIL_UVM=6
FAIL_REBOOT=9
# The model load is minutes on a cold cache. Probing a unit younger than this
# would count a normal start as a failure and restart it mid-load, forever.
GRACE_SECONDS=900
# Minimum seconds between two reboots this script initiates.
REBOOT_GUARD_SECONDS=3600

FAIL_FILE="$STATE_DIR/consecutive-failures"
REBOOT_FILE="$STATE_DIR/last-reboot-epoch"

log() { echo "reveille-tts-watchdog: $*"; }

mkdir -p "$STATE_DIR"

# --- Is the unit young enough that a failed probe means nothing yet? ----------
unit_age_seconds() {
    local since now
    since=$(systemctl show "$UNIT" -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
    [ -n "$since" ] && [ "$since" != "0" ] || { echo 0; return; }
    now=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
    echo $(( (now - since) / 1000000 ))
}

# --- The probe: the MODEL is loaded, and it is not on the CPU -----------------
# A server that fell back to CPU is not a healthy server here. It answers every
# request, so nothing else in the stack will ever notice, and it does it at 1.6x
# realtime -- which is worse than being down, because it is silently slow.
probe_ok() {
    local body
    body=$(curl -fsS -m 10 "$URL/api/model-info" 2>/dev/null) || return 1
    printf '%s' "$body" | grep -q '"loaded"[[:space:]]*:[[:space:]]*true' || return 1
    printf '%s' "$body" | grep -q '"device"[[:space:]]*:[[:space:]]*"cpu"' && return 1
    return 0
}

gpu_alive() { nvidia-smi -L >/dev/null 2>&1; }

reboot_allowed() {
    local last now
    last=$(cat "$REBOOT_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    [ $(( now - last )) -ge "$REBOOT_GUARD_SECONDS" ]
}

do_reboot() {
    if ! reboot_allowed; then
        log "reboot WITHHELD: another reboot within ${REBOOT_GUARD_SECONDS}s. Host needs a human."
        return
    fi
    date +%s > "$REBOOT_FILE"
    log "REBOOTING host: $1"
    systemctl reboot
}

# --- One pass ----------------------------------------------------------------
if probe_ok; then
    prev=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
    echo 0 > "$FAIL_FILE"
    [ "$prev" -gt 0 ] && log "recovered after $prev consecutive failure(s)"
    exit 0
fi

# The card is gone entirely -- no amount of container restarting reaches that.
if ! gpu_alive; then
    echo 0 > "$FAIL_FILE"
    do_reboot "nvidia-smi does not answer; the driver is unreachable"
    exit 0
fi

age=$(unit_age_seconds)
if [ "$age" -lt "$GRACE_SECONDS" ]; then
    log "probe failed but $UNIT is ${age}s old (<${GRACE_SECONDS}s): still loading, not counted"
    exit 0
fi

fails=$(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$FAIL_FILE"
log "probe failed ($fails consecutive)"

if [ "$fails" -ge "$FAIL_REBOOT" ]; then
    echo 0 > "$FAIL_FILE"
    do_reboot "$fails consecutive failures; unit restart and driver reload both failed"
elif [ "$fails" -ge "$FAIL_UVM" ]; then
    log "reloading nvidia_uvm, then restarting $UNIT"
    systemctl stop "$UNIT"
    # Order matters: nothing may hold the module while it is removed.
    modprobe -r nvidia_uvm 2>&1 | sed 's/^/  /' || log "  rmmod nvidia_uvm failed (something still holds it)"
    modprobe nvidia_uvm 2>&1 | sed 's/^/  /' || log "  modprobe nvidia_uvm failed"
    systemctl start "$UNIT"
elif [ "$fails" -ge "$FAIL_RESTART" ]; then
    log "restarting $UNIT"
    systemctl restart "$UNIT"
fi
