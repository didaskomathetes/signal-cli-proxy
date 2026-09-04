# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-04

### Added

- Versioned releases: `v*` tags trigger a release workflow that builds and
  pushes container images to GHCR and creates a GitHub Release with the image
  digests.
- Container images (nested GHCR packages under `signal-cli-proxy`):
  - `signal-cli` — signal-cli daemon (JSON-RPC on 9920, internal only)
  - `proxy` — allowlist proxy (port 9921)
  - `allinone` — both services plus a small entrypoint, for Proxmox
    LXC-from-OCI deployments (PVE >= 8.1)
- `allinone` image: the allowlist can be provided via the
  `SIGNAL_ALLOWED_USERS` environment variable (comma-separated E.164 numbers);
  the entrypoint materializes it into `/etc/signal/allowlist` at startup.
- Single multi-target `Dockerfile` (replaces `Dockerfile` + `Dockerfile.proxy`);
  the signal-cli version and checksum are `ARG`s with the current pins as
  defaults — the Dockerfile is the single source of truth for the stack.
- `CHANGELOG.md` and a Deployment section in the README (Docker / Proxmox LXC /
  building from source).
