FROM eclipse-temurin:25-jre

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/signal-cli

# Install the full signal-cli distribution (daemon + JSON-RPC client + libs).
# The "Linux-client" tarball only contains the JSON-RPC client, which cannot
# run the daemon, so we use the full distribution tarball instead.
# The checksum pins the exact release artifact (supply-chain protection).
RUN curl -fsSL \
    "https://github.com/AsamK/signal-cli/releases/download/v0.14.6/signal-cli-0.14.6.tar.gz" \
    -o /tmp/signal-cli.tar.gz \
    && echo "e90f4faea709b3c0a55909646a2b94289b9779ba9c8fd5c6eaa847d3f67312eb  /tmp/signal-cli.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/signal-cli.tar.gz -C /tmp \
    && cp -a /tmp/signal-cli-0.14.6/. /opt/signal-cli/ \
    && rm -rf /tmp/signal-cli-0.14.6 /tmp/signal-cli.tar.gz \
    && chmod +x /opt/signal-cli/bin/signal-cli

# Directory for account/session data (persisted via a Docker volume).
# The daemon runs as the unprivileged "signal" user (uid 1001); the session
# keys in this directory are sensitive, so they must not be root-owned.
RUN mkdir -p /data/signal-cli \
    && useradd -u 1001 -m signal \
    && chown -R signal:signal /data/signal-cli \
    && chmod 1777 /tmp

VOLUME /data/signal-cli

EXPOSE 9920

# Run the daemon exposing the JSON-RPC interface over HTTP at /api/v1/rpc.
# No -a account is given so the daemon loads all local accounts and also
# supports startLink/finishLink to provision new linked devices at runtime.
# (Linking is done via `docker exec` on the internal port, never by the agent.)
USER signal

CMD ["/opt/signal-cli/bin/signal-cli", "--data-dir", "/data/signal-cli", "daemon", "--http", "0.0.0.0:9920"]
