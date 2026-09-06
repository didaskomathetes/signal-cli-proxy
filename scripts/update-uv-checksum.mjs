#!/usr/bin/env node
// Recompute the uv release checksums after UV_VERSION is bumped in
// .github/renovate-entrypoint.sh.
//
// Renovate's regex manager (see renovate.json5) updates UV_VERSION but cannot
// compute the new per-arch release-artifact checksums, so this post-upgrade
// task downloads the exact tarballs the entrypoint will fetch (x86_64 and
// aarch64) and rewrites the two uv_sha256 values. This keeps the entrypoint's
// sha256 supply-chain check green on the Renovate PR.
//
// Depends only on Node (guaranteed present in the Renovate runtime) — no curl,
// wget, or tar required.

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const ENTRYPOINT = ".github/renovate-entrypoint.sh";

const src = readFileSync(ENTRYPOINT, "utf8");

const versionMatch = src.match(/^UV_VERSION=(.+)$/m);
if (!versionMatch) {
  console.error("update-uv-checksum: UV_VERSION not found in entrypoint");
  process.exit(1);
}
const version = versionMatch[1].trim();

const arches = ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"];

let out = src;
let changed = false;

for (const arch of arches) {
  const url = `https://github.com/astral-sh/uv/releases/download/${version}/uv-${arch}.tar.gz`;
  console.log(`update-uv-checksum: fetching ${url}`);

  let res;
  try {
    res = await fetch(url, { redirect: "follow" });
  } catch (err) {
    console.error(`update-uv-checksum: download failed: ${err.message}`);
    process.exit(1);
  }
  if (!res.ok) {
    console.error(`update-uv-checksum: download failed (HTTP ${res.status})`);
    process.exit(1);
  }

  const buf = Buffer.from(await res.arrayBuffer());
  const sha = createHash("sha256").update(buf).digest("hex");

  const re = new RegExp(`(uv_arch="${arch}"\\n[ \\t]*uv_sha256=")[0-9a-f]{64}(")`);
  if (!re.test(out)) {
    console.error(`update-uv-checksum: could not locate uv_sha256 for ${arch}`);
    process.exit(1);
  }
  const before = out;
  out = out.replace(re, `$1${sha}$2`);
  if (out !== before) {
    changed = true;
  }
  console.log(`update-uv-checksum: uv ${version} (${arch}) -> ${sha}`);
}

if (changed) {
  writeFileSync(ENTRYPOINT, out);
  console.log("update-uv-checksum: entrypoint checksums updated");
} else {
  console.log("update-uv-checksum: checksums already up to date");
}
