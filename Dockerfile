# syntax=docker/dockerfile:1

# signal-cli-proxy: one file, three build targets.
#
#   --target signal-cli   daemon image (JSON-RPC on 9920, internal only)
#   --target proxy        allowlist proxy image (port 9921, the only published surface)
#   --target allinone     both services + entrypoint (for Proxmox LXC-from-OCI)
#
# A release tag pins the whole stack (proxy + signal-cli + JRE). The
# signal-cli version and checksum below are the single source of truth for
# the daemon artifact (supply-chain: checksum-verified). Base images are
# pinned to exact versions (no floating tags) so a release tag always builds
# against known bases; bump them deliberately.

ARG SIGNAL_CLI_VERSION=0.14.7
ARG SIGNAL_CLI_SHA256=0e1eefdf4a2109edf7c899c9d1667167c54ac12c3ec824f27db7c1dac4fa7506

# ---------------------------------------------------------------------------
# signal-cli daemon image
# ---------------------------------------------------------------------------
FROM eclipse-temurin:25.0.4_7-jre AS signal-cli

ARG SIGNAL_CLI_VERSION
ARG SIGNAL_CLI_SHA256

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/signal-cli

# Install the full signal-cli distribution (daemon + JSON-RPC client + libs).
# The "Linux-client" tarball only contains the JSON-RPC client, which cannot
# run the daemon, so we use the full distribution tarball instead.
# The checksum pins the exact release artifact (supply-chain protection).
RUN curl -fsSL \
    "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
    -o /tmp/signal-cli.tar.gz \
    && echo "${SIGNAL_CLI_SHA256}  /tmp/signal-cli.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/signal-cli.tar.gz -C /tmp \
    && cp -a "/tmp/signal-cli-${SIGNAL_CLI_VERSION}/." /opt/signal-cli/ \
    && rm -rf "/tmp/signal-cli-${SIGNAL_CLI_VERSION}" /tmp/signal-cli.tar.gz \
    && chmod +x /opt/signal-cli/bin/signal-cli

# Directory for account/session data (persisted via a Docker volume or the
# LXC rootfs). The daemon runs as the unprivileged "signal" user (uid 1001);
# the session keys in this directory are sensitive, so they must not be
# root-owned.
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

# Healthcheck for `docker run` (docker-compose overrides it).
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD curl -f http://localhost:9920/api/v1/check || exit 1

CMD ["/opt/signal-cli/bin/signal-cli", "--data-dir", "/data/signal-cli", "daemon", "--http", "0.0.0.0:9920"]

# ---------------------------------------------------------------------------
# proxy build stage: reproducible venv with uv from the lock file
# ---------------------------------------------------------------------------
FROM python:3.12.14-slim-bookworm AS proxy-builder

# uv is a single static binary; pull it from the official image (pinned)
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv
WORKDIR /app
# Copy dependency metadata first so this layer is cached unless deps change
COPY pyproject.toml uv.lock ./
RUN uv venv /opt/venv --python 3.12 \
    && uv sync --frozen --no-dev --python /opt/venv/bin/python

# ---------------------------------------------------------------------------
# proxy image
# ---------------------------------------------------------------------------
FROM python:3.12.14-slim-bookworm AS proxy

# curl is needed for the container healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Bring in the fully-populated virtual environment from the builder
COPY --from=proxy-builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged user for the proxy (same name/uid as the allinone image). The
# proxy only reads the code and the allowlist; it never writes, so the
# world-readable venv is enough (no chown of the venv needed).
RUN useradd -u 1002 -m -s /usr/sbin/nologin sigproxy

WORKDIR /app
COPY signal-allowlist-proxy.py .
RUN chown sigproxy:sigproxy /app/signal-allowlist-proxy.py \
    && chmod 555 /app/signal-allowlist-proxy.py

# Run unprivileged. The allowlist is a bind mount and must be readable by
# "sigproxy" (uid 1002) — keep it world-readable (e.g. chmod 644) on the host.
USER sigproxy

EXPOSE 9921

# Healthcheck for `docker run` (docker-compose overrides it).
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD curl -f http://localhost:9921/api/v1/check || exit 1

CMD ["python", "signal-allowlist-proxy.py", "--allowlist", "/etc/signal/allowlist", "--signal-cli", "http://signal-cli:9920", "--port", "9921"]

# ---------------------------------------------------------------------------
# all-in-one image (Proxmox LXC-from-OCI): both services, one entrypoint
# ---------------------------------------------------------------------------
FROM signal-cli AS allinone

# Back to root: the entrypoint drops privileges per service (setpriv).
USER root

# Proxy user, unprivileged and distinct from the signal user (uid 1001).
# Named "sigproxy" (uid 1002) because the temurin base image already ships a
# "proxy" user (uid 13) and an "ubuntu" user (uid 1000). The entrypoint
# references this user by name, so the exact uid is not significant.
RUN useradd -u 1002 -m -s /usr/sbin/nologin sigproxy

# The proxy is stdlib-only (no runtime deps), so it needs no venv — just a
# working Python 3.12 interpreter. Copy a self-contained one (binary, its
# libpython, and the stdlib) from the builder; the temurin base already
# provides libc/libm. The stdlib path matches the builder's compiled-in prefix.
COPY --from=proxy-builder /usr/local/bin/python3.12 /usr/local/bin/python3.12
COPY --from=proxy-builder /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/libpython3.12.so.1.0
COPY --from=proxy-builder /usr/local/lib/python3.12 /usr/local/lib/python3.12

# Proxy code
COPY signal-allowlist-proxy.py /opt/signal-proxy/signal-allowlist-proxy.py
RUN chown sigproxy:sigproxy /opt/signal-proxy/signal-allowlist-proxy.py \
    && chmod 555 /opt/signal-proxy/signal-allowlist-proxy.py

# Entrypoint: materializes the allowlist from $SIGNAL_ALLOWED_USERS (if set)
# and supervises both services (restart on failure, clean shutdown).
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 9921

# Healthcheck checks the proxy (the only published surface), overriding the
# daemon check inherited from the signal-cli stage. Ignored by Proxmox
# LXC-from-OCI; useful if the image is run with Docker.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD curl -f http://localhost:9921/api/v1/check || exit 1

ENTRYPOINT ["/entrypoint.sh"]
