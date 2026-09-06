# Handoff: `v0.1.0` release tag (GHCR images)

**Date:** 2026-09-06
**Status:** ⏳ Open — branch ready, tag not yet created/pushed
**Branch:** `feat/productionized-deployment`
**Related plan:** `nix-config` → `docs/plans/signal-proxy-cicd-plan.md`

## TL;DR

The productionization work (multi-target `Dockerfile`, release workflow,
`entrypoint.sh`, `CHANGELOG.md`, README Deployment section) is complete on
`feat/productionized-deployment`. What remains is the **tag**: merge the branch
to `main`, create and push the `v0.1.0` tag (which triggers
`.github/workflows/release.yml` → builds + pushes 3 GHCR images + creates a
GitHub Release), then run `terraform apply` in `nix-config` to create the
LXC-from-OCI.

Until the tag is pushed, the `nix-config` apply fails with **GHCR 403**
(`ghcr.io/didaskomathetes/signal-cli-proxy/allinone:v0.1.0` does not exist
yet). That 403 is expected and harmless — it clears once the release workflow
has pushed the image.

## Done

- [x] Multi-target `Dockerfile` (`signal-cli` / `proxy` / `allinone`) + `entrypoint.sh`
- [x] Release workflow (`.github/workflows/release.yml`)
- [x] `docker-compose.yml` + README Deployment section + `CHANGELOG.md`
- [x] `ruff check` / `ruff format --check` / `pytest` clean; all 3 targets
      `docker build`ed; `allinone` smoke-tested (`/health`, allowlist enforced,
      DNS written)
- [x] Terraform in `nix-config` (`signal_proxy.tf` + `locals.tf`) rewritten for
      LXC-from-OCI, `fmt` + `validate` green

## Remaining (the tag)

1. **Review + merge** `feat/productionized-deployment` → `main` (open a PR).
2. **Tag** the merged commit on `main`:
   ```bash
   git checkout main && git pull
   git tag v0.1.0
   ```
3. **Push the tag** — this triggers the release workflow:
   ```bash
   git push origin v0.1.0
   ```
4. **Watch the workflow** (`Release`): `test` (ruff + pytest) →
   `build-and-push` (3 images) → `Create GitHub Release`.
5. **Verify the image exists** (proves the 403 is gone):
   ```bash
   docker buildx imagetools inspect \
     ghcr.io/didaskomathetes/signal-cli-proxy/allinone:v0.1.0
   ```
6. **Verify PVE ≥ 9.1** on pve2 (LXC-from-OCI needs ≥ 8.1, `host_managed`
   networking needs ≥ 9.1):
   ```bash
   pveversion
   ```
7. **`nix-config`: `terraform apply`** — pulls the image and creates the
   LXC. Back up `/data/signal-cli` first and restore the linked-device session
   afterwards (same path as the migration doc).

## Verification

- GHCR: `allinone:v0.1.0` (and `:latest`) resolvable via `imagetools inspect`.
- GitHub Release `v0.1.0` created with the image digests in the notes.
- `nix-config` `terraform plan` no longer shows the OCI image as new/failed.
- After apply: LXC up, `curl http://<lxc-ip>:9921/health` → ok, allowlist
  enforced (non-allowlisted sender → dropped).

## Risks / notes

- **PVE version:** if pve2 is < 9.1, `host_managed` networking is unavailable —
  fall back to Option C (CI-built `.vztmpl` release asset) per the plan.
- **Upgrades = container re-creation:** a new tag means destroy + recreate; the
  linked-device session must be migrated (back up `/data/signal-cli` first).
- **GHCR public access:** public repo → public packages (free tier). The image
  contains no secrets; session data is never in the image.
- **Supply chain:** `nix-config` pins by tag (`local.signal_proxy_version =
  "v0.1.0"`); the release notes carry the digest for optional `@sha256:`
  pinning.
