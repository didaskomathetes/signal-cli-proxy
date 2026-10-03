#!/bin/bash
# Watchdog for the signal-cli daemon (allinone image, Proxmox LXC-from-OCI).
#
# The entrypoint's supervise() loop only restarts a service when its process
# EXITS. On 2026-10-03 the signal-cli daemon hung (process alive, but
# /api/v1/rpc and /api/v1/events stopped responding) and nothing restarted it:
# Signal was down for ~32 minutes until a manual LXC restart.
#
# This watchdog closes that gap. It runs as ROOT (it must be able to kill the
# uid-1001 java process; the entrypoint drops privileges per service with
# setpriv, but the watchdog must NOT be setpriv'd). It probes the daemon's
# JSON-RPC endpoint every PROBE_INTERVAL seconds; after FAIL_THRESHOLD
# consecutive failed probes it kills the daemon (scoped to uid "signal", so it
# can never hit the proxy, the root supervise loops, or this script) and the
# existing supervise loop restarts it in 5 s. It then re-probes to verify the
# recovery. At most MAX_RECOVERIES kills per COOLDOWN_WINDOW seconds; once the
# budget is exhausted it stops acting and logs that manual intervention is
# needed.
#
# No environment variables: PVE's LXC-from-OCI import does not apply the
# image's Env to the container, so all configuration is hardcoded below.
#
# Logging: timestamped lines go to stderr; the entrypoint's supervise loop
# appends stderr to /var/log/watchdog.log (the same pattern as the signal-cli
# and proxy service logs). Writing the file directly as well would duplicate
# every line, since the redirect targets the same file.
#
# This is a supervised long-running process: a clean infinite loop. If it
# exits, the entrypoint's supervise loop restarts it.
set -u

# ---------------------------------------------------------------------------
# Constants (hardcoded on purpose — see the note above about env vars)
# ---------------------------------------------------------------------------
RPC_URL="http://127.0.0.1:9920/api/v1/rpc"
RPC_BODY='{"jsonrpc":"2.0","id":1,"method":"listAccounts"}'
PROBE_TIMEOUT=5        # curl -m: a wedged daemon must not block the probe
PROBE_INTERVAL=30      # seconds between probes in the normal loop
FAIL_THRESHOLD=3       # consecutive failed probes before acting (~90 s)
MAX_RECOVERIES=3       # max kills per cooldown window
COOLDOWN_WINDOW=3600   # seconds (1 hour)
RECOVERY_PROBE_INTERVAL=15  # seconds between probes while waiting for recovery
RECOVERY_MAX_WAIT=120  # give up waiting after this long, resume normal probing

# Timestamped log line to stderr; the entrypoint's supervise loop appends
# stderr to /var/log/watchdog.log (see the header note on logging).
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog: $*" >&2
}

# Probe the daemon's JSON-RPC endpoint. Success = HTTP 200 with a JSON-RPC
# "result" field. Any curl failure, non-200 status, or timeout is a failure.
# The grep requires the colon after the key (the JSON-RPC field), so a 200
# body that merely CONTAINS the word "result" (e.g. inside an error message)
# does not pass.
probe() {
    local body_file http_code
    body_file=$(mktemp) || return 1
    http_code=$(curl -s -m "$PROBE_TIMEOUT" -o "$body_file" -w '%{http_code}' \
        -X POST "$RPC_URL" \
        -H 'Content-Type: application/json' \
        -d "$RPC_BODY" 2>/dev/null) || {
        rm -f "$body_file"
        return 1
    }
    local ok=1
    if [ "$http_code" = "200" ] && grep -Eq '"result"[[:space:]]*:' "$body_file"; then
        ok=0
    fi
    rm -f "$body_file"
    return "$ok"
}

# Kill the hung daemon. Scoped to uid "signal" WITHOUT a -f pattern: the
# daemon is the only long-running process owned by that user (the entrypoint
# runs exactly one service per user — signal, sigproxy — plus this script as
# root), so every process of the uid is the daemon. Pattern-less scoping
# also removes the silent coupling to the entrypoint's daemon cmdline
# ('signal-cli ... daemon') that the previous -f pattern had. SIGTERM first,
# escalating to SIGKILL if any process of the uid is still alive after a
# short grace period: a wedged JVM may be unable to act on SIGTERM (the whole
# point is that it is unresponsive), and the supervise loop only restarts the
# daemon once the process actually exits.
# Returns 0 if a kill matched at least one process, 1 if nothing matched.
kill_daemon() {
    if ! pkill -TERM -u signal; then
        # No process owned by uid "signal" matched. Expected if the daemon
        # exited on its own between the probe and the kill (the supervise
        # loop restarts it); a persistent no-match means the uid scope no
        # longer covers the daemon, which would otherwise be silent.
        log "WARNING: no daemon process matched (pkill -TERM -u signal)"
        return 1
    fi
    sleep 3
    if pgrep -u signal >/dev/null 2>&1; then
        log "daemon still alive after SIGTERM; sending SIGKILL"
        pkill -KILL -u signal
    fi
    return 0
}

# True if fewer than MAX_RECOVERIES kills were recorded in the last
# COOLDOWN_WINDOW seconds. Prunes stale timestamps but does NOT record.
recovery_allowed() {
    local now cutoff i
    local old_times=("${RECOVERY_TIMES[@]:-}")
    now=$(date +%s)
    cutoff=$((now - COOLDOWN_WINDOW))
    RECOVERY_TIMES=()
    for i in "${old_times[@]:-}"; do
        [ -n "$i" ] && [ "$i" -ge "$cutoff" ] && RECOVERY_TIMES+=("$i")
    done
    [ "${#RECOVERY_TIMES[@]}" -lt "$MAX_RECOVERIES" ]
}

# Record a kill that actually matched a process. A no-op kill (daemon
# already exited on its own) is deliberately NOT recorded, so a
# crash-looping daemon cannot exhaust the budget on kills that matched
# nothing.
record_recovery() {
    RECOVERY_TIMES+=("$(date +%s)")
}

# Wait for the daemon to come back after a kill: re-probe every
# RECOVERY_PROBE_INTERVAL seconds. Returns 0 if it recovered (logging how
# long it took), 1 if it was still down after RECOVERY_MAX_WAIT seconds.
wait_for_recovery() {
    local killed_at waited
    killed_at=$(date +%s)
    waited=0
    while [ "$waited" -lt "$RECOVERY_MAX_WAIT" ]; do
        sleep "$RECOVERY_PROBE_INTERVAL"
        waited=$((waited + RECOVERY_PROBE_INTERVAL))
        if probe; then
            log "recovered after $(( $(date +%s) - killed_at )) s"
            return 0
        fi
    done
    log "still not responding after ${RECOVERY_MAX_WAIT} s; resuming normal probing"
    return 1
}

RECOVERY_TIMES=()
FAIL_COUNT=0
COOLDOWN_LOGGED=0

trap 'log "received termination signal; exiting"; exit 0' TERM INT

log "started (probe every ${PROBE_INTERVAL}s, act after ${FAIL_THRESHOLD} failures, max ${MAX_RECOVERIES} recoveries per ${COOLDOWN_WINDOW}s)"

while true; do
    sleep "$PROBE_INTERVAL"
    if probe; then
        if [ "$FAIL_COUNT" -gt 0 ]; then
            log "probe OK again after $FAIL_COUNT failed probe(s)"
        fi
        FAIL_COUNT=0
        COOLDOWN_LOGGED=0
        continue
    fi

    FAIL_COUNT=$((FAIL_COUNT + 1))
    log "probe failed ($FAIL_COUNT/$FAIL_THRESHOLD)"

    if [ "$FAIL_COUNT" -lt "$FAIL_THRESHOLD" ]; then
        continue
    fi

    if recovery_allowed; then
        log "daemon unresponsive after $FAIL_THRESHOLD consecutive failed probes; killing (supervise loop will restart it)"
        if kill_daemon; then
            record_recovery
            FAIL_COUNT=0
            wait_for_recovery
        fi
        # No-match: the daemon already exited on its own; the supervise loop
        # restarts it and the next probe cycle verifies the recovery.
        continue
    fi

    if [ "$COOLDOWN_LOGGED" -eq 0 ]; then
        log "cooldown exhausted: ${MAX_RECOVERIES} recoveries in the last ${COOLDOWN_WINDOW}s; manual intervention needed"
        COOLDOWN_LOGGED=1
    fi
    # Keep probing (and counting) so a recovery is logged if the daemon
    # comes back on its own; do not kill again until the window slides.
done
