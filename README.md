# Signal CLI Allowlist Proxy

[![CI](https://github.com/didaskomathetes/signal-cli-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/didaskomathetes/signal-cli-proxy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

A Docker setup that runs [signal-cli](https://github.com/AsamK/signal-cli) as a
linked device, fronted by a **filtering allowlist proxy**. Designed to be
consumed by the Hermes agent on another VM — or any client that needs a hard,
enforced ceiling on who it can talk to over Signal.

The proxy is the **trusted security boundary**: the (untrusted) client can only
ever talk to the proxy, and the proxy enforces the allowlist on both outgoing
*and* incoming traffic.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Main Machine (your control VM)                                 │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  signal-cli container  (port 9920 — INTERNAL ONLY)        │  │
│  │  - signal-cli daemon, HTTP JSON-RPC + SSE                 │  │
│  │  - NOT published to the host / network                    │  │
│  │  - Session data persisted in Docker volume                │  │
│  └────────────────────────────────────────────────────────────┘  │
│            ▲  (Docker internal network, service name "signal-cli")│
│            │                                                     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Allowlist Proxy  (port 9921 — the ONLY published port)   │  │
│  │  - uv-managed Python proxy (stdlib only)                  │  │
│  │  - Blocks sends to non-allowlisted recipients             │  │
│  │  - Drops incoming messages from non-allowlisted senders   │  │
│  │  - Forwards everything else to signal-cli                 │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────┬───────────────────────────────┘
                                   │  <main-machine>:9921
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Hermes Agent VM (isolated / untrusted)                         │
│  - Connects ONLY to the proxy (SIGNAL_HTTP_URL=...:9921)        │
│  - Its own allowlist may only be a SUBSET of the proxy allowlist│
└──────────────────────────────────────────────────────────────────┘
```

Key point: **port 9920 is never published.** The agent cannot reach signal-cli
directly, so it cannot bypass the allowlist.

## Attachments (media handoff)

Outbound attachments (voice notes, images, documents) travel **inside the RPC
body as RFC 2397 base64 data URIs**, not as file paths:

```
"attachments": ["data:audio/ogg;filename=tts_....ogg;base64,<b64>"]
```

This is deliberate: the agent VM and the signal-cli container live on
**different hosts**, so a bare path (`/root/.hermes/cache/audio/...`) would
only resolve on the agent's filesystem and fail in signal-cli with
`NoSuchFileException`. Inlining the bytes in the data URI removes the
cross-host filesystem coupling entirely — the proxy stays a pure pipe.

- signal-cli (pinned here to v0.14.6) parses data URIs in
  `AttachmentHelper` (`data:` prefix → `DataURI`), so no container-side
  change is needed.
- The proxy's `MAX_BODY_BYTES` (50 MiB) is sized for this: base64 inflates
  the body ~33%, so **~37 MB raw per attachment** is the practical ceiling.
  Larger bodies get an explicit `413 request body too large`.

If outbound media suddenly fails with `NoSuchFileException`, the adapter is
sending bare paths again (e.g. an unpatched/older Hermes build) — that is the
failure mode, and the fix is on the adapter side, not here.

## Security Model

- The allowlist is enforced **by the proxy**, not by the Hermes agent. Even a
  fully compromised agent (root on its VM, prompt-injected, etc.) cannot send to
  or receive from anyone outside the allowlist.
- Enforcement is **bidirectional**:
  - **Outgoing (default-deny):** the proxy only forwards the JSON-RPC methods
    in `ALLOWED_METHODS` (see `signal-allowlist-proxy.py`); everything else —
    `startLink`/`finishLink`, `updateGroup`, `joinGroup`, `trust`,
    `unregister`, ... — is rejected with `403`. A compromised agent therefore
    cannot link new devices, create or modify groups, or touch account
    settings.
  - **Outgoing (recipients):** any allowed call that carries a `recipient`
    (string or list) is only forwarded if every recipient resolves to an
    allowlisted number. Group addressing (`groupId`) and username addressing
    (`username`) are rejected, and an `account` parameter may only reference
    accounts the proxy knows about.
  - **Incoming:** the proxy holds the upstream SSE stream and drops `receive`
    events whose sender (`envelope.sourceNumber` / `envelope.source`) is not
    allowlisted, as well as all group events, before they reach the agent.
- The allowlist file is mounted **read-only** into the proxy only. signal-cli
  has no access to it.
- The agent's own allowlist (if it has one) must be a **subset** of the proxy
  allowlist — the proxy is the hard ceiling.
- **No authentication:** the proxy trusts whoever can reach port 9921.
  Restrict that port to the agent VM with a firewall (e.g. `ufw`/`nftables`
  rule on the main machine); do not expose 9921 to the internet.

## Files

| Path | Purpose |
|------|---------|
| `Dockerfile` | multi-target image build: `signal-cli` (daemon, JSON-RPC on 9920), `proxy` (uv-managed, reproducible venv, `--no-dev`), `allinone` (both + entrypoint, for LXC) |
| `entrypoint.sh` | `allinone` entrypoint: materializes the allowlist from `$SIGNAL_ALLOWED_USERS`, supervises both services |
| `signal-allowlist-proxy.py` | the proxy (Python stdlib only) |
| `pyproject.toml` / `uv.lock` | proxy dependency management (uv) |
| `allowlist/allowlist.example` | allowlist template (copy to `allowlist/allowlist`, which is gitignored) |
| `docker-compose.yml` | wires the two containers together (prebuilt GHCR images, build fallback) |
| `tests/` | security-boundary tests (the spec for the proxy) |
| `.github/workflows/ci.yml` | CI: ruff lint + format + pytest |
| `.github/workflows/release.yml` | on `v*` tags: build + push GHCR images, create GitHub Release |
| `CHANGELOG.md` | release history |
| `AGENTS.md` | instructions for AI coding agents (symlinked as `CLAUDE.md`) |

## Quick Start

### 1. Configure the allowlist

Copy the template and edit `allowlist/allowlist` — one E.164 number per line.
The real file is gitignored so your numbers never end up in the repo:

```bash
cp allowlist/allowlist.example allowlist/allowlist
```

```
+15551234567
```

### 2. Start the containers

```bash
docker compose up -d
docker compose ps          # both should be "healthy"
```

This pulls the prebuilt release images from GHCR. To build from source
instead (e.g. to test an unreleased change), run `docker compose build` first.

### 3. Link your Signal account

The account is linked at runtime (stored in the `signal-session` volume).

```bash
# 1) Start a link session -> returns a deviceLinkUri
docker exec signal-cli curl -fsS -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"startLink","id":"1"}' \
  http://localhost:9920/api/v1/rpc
```

Render the returned `deviceLinkUri` as a QR (e.g. `uv run --with qrcode ...`)
and scan it:

1. Open **Signal** on your phone
2. **Settings → Linked Devices → Link New Device**
3. Scan the QR

Then finish the link (blocks until the phone confirms):

```bash
docker exec signal-cli curl -fsS --max-time 280 -X POST \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"finishLink","id":"2","params":{"deviceLinkUri":"<URI>","deviceName":"HermesAgent"}}' \
  http://localhost:9920/api/v1/rpc
```

Verify:

```bash
docker exec signal-cli curl -fsS -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"listAccounts","id":"3"}' \
  http://localhost:9920/api/v1/rpc
```

> Linking is done against the **internal** 9920 (via `docker exec`) because 9920
> is not published. The proxy's RPC forward timeout is 60s, which is too short
> for `finishLink`.

### 4. Configure the Hermes agent (on the agent VM)

Point the agent at the **proxy** only:

```bash
cat > ~/.hermes/.env << 'EOF'
SIGNAL_HTTP_URL=http://<main-machine>:9921
SIGNAL_ACCOUNT=+15551234567
# Optional, and must be a SUBSET of the proxy allowlist:
SIGNAL_ALLOWED_USERS=+15551234567
EOF
```

## Control Points

| What | Where | How |
|------|-------|-----|
| **Allowed numbers** | Main machine | `allowlist/allowlist` (reloaded automatically) |
| **Linked account** | Main machine | Docker volume `signal-session` (linked at runtime) |
| **Container restart** | Main machine | `docker compose restart` |
| **Hermes config** | Agent VM | `~/.hermes/.env` or `hermes gateway setup` |

## Deployment

### Docker (prebuilt images)

Release tags (`v*`) build and push three images to GHCR (nested packages under
`signal-cli-proxy`):

| Image | Purpose |
|-------|---------|
| `ghcr.io/didaskomathetes/signal-cli-proxy/signal-cli:vX.Y.Z` | signal-cli daemon (9920, internal only) |
| `ghcr.io/didaskomathetes/signal-cli-proxy/proxy:vX.Y.Z` | allowlist proxy (9921) |
| `ghcr.io/didaskomathetes/signal-cli-proxy/allinone:vX.Y.Z` | both services + entrypoint (for LXC) |

`docker compose up -d` (see Quick Start) pulls the first two. Pin the version
tag you want in `docker-compose.yml` (or use `:latest`).

### Proxmox LXC (LXC-from-OCI, PVE >= 8.1)

The `allinone` image is designed to run as a Proxmox LXC created from an OCI
image (a PVE "technology preview"). One container, both services, no Docker:

1. **Pull the image** — Proxmox GUI: *Datacenter → Storage → (a storage) →
   Container Templates → Pull from OCI registry*, then enter
   `ghcr.io/didaskomathetes/signal-cli-proxy/allinone:vX.Y.Z`.
2. **Create the LXC** from that template (GUI wizard or `pct create`), e.g.:

   ```bash
   pct create 98 local:oci/<pulled-image> \
     --hostname signal-proxy \
     --cores 1 --memory 1024 \
     --net0 name=eth0,bridge=vmbr0,ip=192.168.1.100/24,gw=192.168.1.1 \
     --unprivileged 1 \
     --env SIGNAL_ALLOWED_USERS=+15551234567,+4917657908578
   ```

   `--env` sets the runtime environment; `SIGNAL_ALLOWED_USERS` is a
   comma-separated list of E.164 numbers. The entrypoint writes it to
   `/etc/signal/allowlist` at startup; the proxy reloads that file every 30 s,
   so you can also edit the file at runtime.

   Optionally, `SIGNAL_PROXY_DNS_SERVERS` (comma-separated IPs) is written to
   `/etc/resolv.conf` at startup. The image has no network manager, so this is
   the self-contained way to give the LXC DNS (e.g.
   `--env SIGNAL_PROXY_DNS_SERVERS=1.1.1.1,9.9.9.9`); PVE's container
   "nameserver" option works too.
3. **Start it** and check the proxy: `curl http://192.168.1.100:9921/health`.

Notes:

- Only 9921 should be reachable; keep 9920 loopback-only (it is, by default).
- The account is linked at runtime against the internal 9920 — use
  `pct exec 98 -- curl ... http://127.0.0.1:9920/api/v1/rpc` (see Quick Start
  step 3, substituting `pct exec` for `docker exec`).
- There is no systemd in the image; the entrypoint supervises both processes
  and restarts either on failure.

### Building from source

```bash
docker build --target signal-cli -t signal-cli:dev .
docker build --target proxy    -t proxy:dev .
docker build --target allinone -t allinone:dev .
```

The signal-cli version and checksum are `ARG`s at the top of the `Dockerfile`
(defaults are the current pins); override with `--build-arg` if needed.

## Useful Commands

```bash
docker compose ps                                   # health
docker logs -f signal-allowlist-proxy               # proxy logs
docker logs -f signal-cli                           # signal-cli logs

# Health checks
curl http://localhost:9921/api/v1/check             # proxy (published)
curl http://localhost:9921/health                   # proxy status JSON
docker exec signal-cli curl -fsS -o /dev/null -w "%{http_code}\n" \
  http://localhost:9920/api/v1/check                # signal-cli (internal)

# Test allowlist enforcement (should be 403)
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"send","id":"x","params":{"recipient":"+1234567890","message":"t"}}' \
  http://localhost:9921/api/v1/rpc

# Test method allowlist (should be 403 — startLink is not allowed)
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"startLink","id":"x"}' \
  http://localhost:9921/api/v1/rpc
```

## Development (proxy)

The proxy is a `uv`-managed Python project (stdlib only, no runtime deps).

```bash
uv sync                 # create .venv/ from uv.lock (installs dev tools)
uv run python signal-allowlist-proxy.py \
  --allowlist ./allowlist/allowlist \
  --signal-cli http://localhost:9920 \
  --port 9921           # run locally against a local signal-cli
```

### Lint, format, test

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # formatting check
uv run pytest                  # run the security-boundary test suite
```

The tests in `tests/` pin the proxy's fail-closed behavior (what is allowed,
what is blocked). Any change to the enforcement logic should come with a test.

Add a dependency later with `uv add <pkg>`; `uv.lock` keeps builds reproducible.
The Docker image installs from `uv.lock` (`uv sync --frozen --no-dev`).

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Proxy "unhealthy" | Proxy image needs `curl` for the healthcheck — rebuild: `docker compose build allowlist-proxy` |
| "Method requires valid account parameter" | Account not linked yet — complete the linking step |
| `finishLink` "Connection closed" | The `deviceLinkUri` expired — run `startLink` again and scan quickly |
| Messages not received | Check the sender is in `allowlist/allowlist` |
| Allowlist change not applied | The proxy reloads the file automatically; check proxy logs for "Allowlist reloaded" |
| signal-cli "Permission denied" after upgrade | The image now runs as non-root (uid 1001). An existing volume created by the old root image is root-owned. One-time fix (check the exact volume name with `docker volume ls`): `docker run --rm -v <project>_signal-session:/data --entrypoint sh alpine chown -R 1001:1001 /data` |

## Security Notes

- The proxy is the only published surface (9921); signal-cli (9920) is internal.
- The allowlist is mounted read-only into the proxy only.
- Signal's end-to-end encryption protects message content in transit.
- The `signal-session` volume contains account credentials — protect it like a password.
- **Groups are blocked by default:** group methods (`updateGroup`, `joinGroup`,
  `send` with `groupId`, ...) are not in `ALLOWED_METHODS`, and incoming group
  events are dropped. If the agent ever needs group support, add the specific
  methods to `ALLOWED_METHODS` in `signal-allowlist-proxy.py` and extend the
  recipient check to cover `groupId` — do not simply allow all group methods.
- **Extending the method allowlist:** `ALLOWED_METHODS` is deliberately
  minimal (chat + receipts + contact lookups). Add methods one by one, and
  make sure any new method that can reach other people is recipient-checked.

## License

MIT — see [LICENSE](LICENSE).
