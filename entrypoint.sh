#!/bin/bash
# All-in-one entrypoint for the signal-proxy LXC image (Proxmox LXC-from-OCI).
#
# Runs both services of the stack, each as its own unprivileged user, and
# restarts either on failure (mirrors systemd's Restart=on-failure +
# RestartSec=5):
#
#   signal-cli  daemon, JSON-RPC on 127.0.0.1:9920 (loopback only)
#   proxy       allowlist proxy on 0.0.0.0:9921 (the only published surface)
#
# If $SIGNAL_ALLOWED_USERS is set (comma-separated E.164 numbers), it is
# materialized into /etc/signal/allowlist at startup. The proxy reloads the
# file every 30 s, so editing the file at runtime also works.
#
# If $SIGNAL_PROXY_DNS_SERVERS is set (comma-separated IPs), it is written to
# /etc/resolv.conf. This image has no network manager, so this is the reliable
# way to give it DNS (PVE may also set it via the container "nameserver"
# option; this is a self-contained fallback).
set -u

# The Temurin base image sets JAVA_HOME and a JDK PATH in the OCI image
# config, but PVE's LXC-from-OCI import does NOT apply the image's Env to the
# container (only Entrypoint/WorkingDir/User are transferred). Fall back to
# the known JDK location so the image is self-contained on any runtime.
export JAVA_HOME="${JAVA_HOME:-/opt/java/openjdk}"
export PATH="${JAVA_HOME/bin}:${PATH}"

ALLOWLIST_FILE=/etc/signal/allowlist
ALLOWLIST_USERS="${SIGNAL_ALLOWED_USERS:-}"
DNS_SERVERS="${SIGNAL_PROXY_DNS_SERVERS:-}"

if [ -n "$ALLOWLIST_USERS" ]; then
    install -d -m 755 /etc/signal
    printf '%s\n' "$ALLOWLIST_USERS" | tr ',' '\n' | sed '/^[[:space:]]*$/d' > "$ALLOWLIST_FILE"
    # The proxy runs as "sigproxy"; make the file readable by that user only
    # (root owner, sigproxy group, no world access).
    chown root:sigproxy "$ALLOWLIST_FILE"
    chmod 440 "$ALLOWLIST_FILE"
    echo "entrypoint: allowlist written to $ALLOWLIST_FILE" >&2
else
    echo "entrypoint: WARNING: SIGNAL_ALLOWED_USERS is not set; the proxy will start with an EMPTY allowlist (all traffic blocked) until $ALLOWLIST_FILE exists" >&2
fi

if [ -n "$DNS_SERVERS" ]; then
    if printf '%s\n' "$DNS_SERVERS" | tr ',' '\n' | sed '/^[[:space:]]*$/d' \
        | sed 's/^/nameserver /' > /etc/resolv.conf 2>/dev/null; then
        echo "entrypoint: /etc/resolv.conf written from SIGNAL_PROXY_DNS_SERVERS" >&2
    else
        echo "entrypoint: WARNING: could not write /etc/resolv.conf (read-only?); DNS may not work" >&2
    fi
fi

# Restart loop for one service, run as its own user in its own session
# (process group), so shutdown can stop the whole tree cleanly.
supervise() {
    local name="$1" user="$2"
    shift 2
    while true; do
        echo "entrypoint: starting $name" >&2
        setpriv --reuid="$user" --regid="$user" --init-groups "$@" >>"/var/log/$name.log" 2>&1
        echo "entrypoint: $name exited; restarting in 5 s" >&2
        sleep 5
    done
}
export -f supervise

setsid bash -c 'supervise signal-cli signal /opt/signal-cli/bin/signal-cli --data-dir /data/signal-cli daemon --http 127.0.0.1:9920' &
CLI_PID=$!

setsid bash -c 'supervise proxy sigproxy /usr/local/bin/python3.12 /opt/signal-proxy/signal-allowlist-proxy.py --allowlist /etc/signal/allowlist --signal-cli http://127.0.0.1:9920 --port 9921' &
PROXY_PID=$!

# Forward shutdown signals to both service process groups, then exit.
shutdown() {
    echo "entrypoint: shutting down" >&2
    kill -TERM -- "-$CLI_PID" "-$PROXY_PID" 2>/dev/null
    wait
    exit 0
}
trap shutdown TERM INT

wait
