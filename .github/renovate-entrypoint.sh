#!/bin/sh
# Custom entrypoint for the self-hosted Renovate container (see
# .github/workflows/renovate.yml, docker-cmd-file).
#
# Installs a pinned `uv` so Renovate can regenerate uv.lock when it updates
# the Python (pep621) dev dependencies in pyproject.toml, then hands off to
# the normal `renovate` command.
#
# The download uses Node (always present in the Renovate runtime) so this does
# not depend on curl/wget being in the base image; extraction uses `tar`.
# The uv version is pinned (matching the Dockerfile and CI) and the tarball is
# checksum-verified before installation — the same supply-chain posture as the
# Dockerfile's signal-cli pin. Note: uv release tags carry no "v" prefix and
# the asset name is uv-<arch>.tar.gz (no version in the filename).
set -eu

UV_VERSION=0.12.18

case "$(uname -m)" in
x86_64)
    uv_arch="x86_64-unknown-linux-gnu"
    uv_sha256="89eadd7c76fc063887959510d5ba0ab1264dfd5f1143b925ddb73021a40acf16"
    ;;
aarch64)
    uv_arch="aarch64-unknown-linux-gnu"
    uv_sha256="afb6291f3f0a6b4521fc67b947822506c41dde5b60d2189dd8f3695b2ac8c9e7"
    ;;
*)
    echo "renovate-entrypoint: unsupported arch $(uname -m)" >&2
    exit 1
    ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    uv_url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${uv_arch}.tar.gz"
    uv_tmp="$(mktemp -d)"

    node -e '
        const [url, out] = process.argv.slice(1);
        fetch(url)
            .then((r) => {
                if (!r.ok) {
                    console.error("download failed: " + r.status);
                    process.exit(1);
                }
                return r.arrayBuffer();
            })
            .then((b) => {
                require("fs").writeFileSync(out, Buffer.from(b));
            })
            .catch((e) => {
                console.error(e);
                process.exit(1);
            });
    ' "${uv_url}" "${uv_tmp}/uv.tar.gz"

    # Verify the checksum before installing (supply-chain protection).
    actual="$(node -e '
        const fs = require("fs");
        const crypto = require("crypto");
        const h = crypto.createHash("sha256");
        h.update(fs.readFileSync(process.argv[1]));
        console.log(h.digest("hex"));
    ' "${uv_tmp}/uv.tar.gz")"
    if [ "$actual" != "$uv_sha256" ]; then
        echo "renovate-entrypoint: uv tarball checksum mismatch (expected ${uv_sha256}, got ${actual})" >&2
        exit 1
    fi

    tar xzf "${uv_tmp}/uv.tar.gz" -C "${uv_tmp}"
    cp "${uv_tmp}/uv-${uv_arch}/uv" /usr/local/bin/uv
    chmod 0755 /usr/local/bin/uv
    rm -rf "${uv_tmp}"
    echo "renovate-entrypoint: installed uv ${UV_VERSION}"
fi

exec renovate
