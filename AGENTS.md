# Agent Instructions

This repo is **signal-cli-proxy**: a filtering allowlist proxy in front of
[signal-cli](https://github.com/AsamK/signal-cli)'s HTTP JSON-RPC API, plus the
Docker stack that runs signal-cli as a linked device behind it.

The proxy is the **trusted security boundary**. The client (e.g. the Hermes
agent) is assumed *untrusted*; the proxy enforces the allowlist on both
outgoing and incoming traffic so a compromised client can never widen it.

## Commands

The project is managed with [uv](https://docs.astral.sh/uv/) (Python 3.12,
stdlib-only runtime — no runtime dependencies).

```bash
uv sync                 # create .venv/ from uv.lock (installs dev tools)
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pytest           # run the test suite
```

Run the proxy locally (against a local signal-cli on :9920):

```bash
cp allowlist/allowlist.example allowlist/allowlist   # then edit
uv run python signal-allowlist-proxy.py \
  --allowlist ./allowlist/allowlist \
  --signal-cli http://localhost:9920 \
  --port 9921
```

Run the full Docker stack (prebuilt GHCR images; `docker compose build` to
rebuild from source):

```bash
docker compose up -d
docker compose ps        # both services should be "healthy"
```

Build the images locally (multi-target `Dockerfile`):

```bash
docker build --target signal-cli -t signal-cli:dev .
docker build --target proxy    -t proxy:dev .
docker build --target allinone -t allinone:dev .
```

## Security model (read before touching the proxy)

The proxy is **fail-closed** by design. When in doubt, block.

- **Outgoing (default-deny):** only methods in `ALLOWED_METHODS`
  (`signal-allowlist-proxy.py`) are forwarded. Everything else — `startLink`,
  `finishLink`, `updateGroup`, `joinGroup`, `trust`, `unregister`, ... — is
  rejected with `403`.
- **Outgoing (recipients):** any allowed call carrying a `recipient` (string or
  list) is forwarded only if every recipient resolves to an allowlisted E.164
  number. Group (`groupId`) and username (`username`) addressing are rejected.
  An `account` param may only reference accounts the proxy knows.
- **Incoming:** the proxy holds the upstream SSE stream and drops `receive`
  events whose sender is not allowlisted, plus all group events.
- Recipients may be addressed by UUID; the proxy resolves UUID → number via the
  contact list and blocks anything it cannot resolve to an allowlisted number.

### Rules for changes to the proxy

- **Never** add a method to `ALLOWED_METHODS` without a security review. Each
  method must be justified, and any method that can reach other people must be
  recipient-checked.
- **Never** weaken a check to make something "work". If a legitimate use case
  is blocked, extend the check explicitly (and add a test) — don't remove it.
- Keep the fail-closed posture: unknown/ambiguous input → block.
- Any change to enforcement logic **must** come with a test in `tests/` that
  pins the new behavior. The existing tests are the spec for the boundary.

## Conventions

- Python 3.12, stdlib only at runtime. Do not add runtime dependencies without
  strong justification (the proxy's value is that it is small and auditable).
- Lint with `ruff` (config in `pyproject.toml`); keep `ruff check .` and
  `ruff format --check .` clean.
- Keep the proxy a single, readable file. Prefer clarity over cleverness.

## Files

| Path | Purpose |
|------|---------|
| `signal-allowlist-proxy.py` | the proxy (stdlib only) |
| `Dockerfile` | multi-target: `signal-cli` (daemon, 9920 internal), `proxy` (uv venv, `--no-dev`), `allinone` (both + entrypoint, for LXC) |
| `entrypoint.sh` | `allinone` entrypoint: allowlist from `$SIGNAL_ALLOWED_USERS`, DNS from `$SIGNAL_PROXY_DNS_SERVERS`, supervises both services |
| `docker-compose.yml` | wires the two containers (prebuilt GHCR images); only 9921 is published |
| `allowlist/allowlist.example` | allowlist template (copy to `allowlist/allowlist`) |
| `tests/` | security-boundary tests (the spec) |
| `.github/workflows/ci.yml` | CI: ruff + format + pytest |
| `.github/workflows/release.yml` | on `v*` tags: build + push GHCR images, create GitHub Release |
| `CHANGELOG.md` | release history |

## Do not commit

- `.env` (linked account number)
- `allowlist/allowlist` (real allowlisted numbers)
- `signal-session/` (account credentials)

These are gitignored. Verify with `git status` before committing.
