#!/bin/sh
# Custom entrypoint for the self-hosted Renovate container (see
# .github/workflows/renovate.yml, docker-cmd-file).
#
# Installs `uv` so Renovate can regenerate uv.lock when it updates the Python
# (pep621) dev dependencies in pyproject.toml, then hands off to the normal
# `renovate` command.
#
# The download uses Node (always present in the Renovate runtime) so this does
# not depend on curl/wget being in the base image; extraction uses `tar`.
set -eu

if ! command -v uv >/dev/null 2>&1; then
    uv_version="$(node -e '
        fetch("https://api.github.com/repos/astral-sh/uv/releases/latest")
            .then((r) => r.json())
            .then((j) => console.log(j.tag_name.replace(/^v/, "")))
            .catch(() => process.exit(1));
    ')"

    case "$(uname -m)" in
    x86_64) uv_arch="x86_64-unknown-linux-gnu" ;;
    aarch64) uv_arch="aarch64-unknown-linux-gnu" ;;
    *)
        echo "renovate-entrypoint: unsupported arch $(uname -m)" >&2
        exit 1
        ;;
    esac

    uv_url="https://github.com/astral-sh/uv/releases/download/v${uv_version}/uv-${uv_version}-${uv_arch}.tar.gz"
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

    tar xzf "${uv_tmp}/uv.tar.gz" -C "${uv_tmp}"
    cp "${uv_tmp}/uv" /usr/local/bin/uv
    chmod 0755 /usr/local/bin/uv
    rm -rf "${uv_tmp}"
    echo "renovate-entrypoint: installed uv ${uv_version}"
fi

exec renovate
