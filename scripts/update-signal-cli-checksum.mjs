#!/usr/bin/env node
// Recompute the signal-cli release checksum after SIGNAL_CLI_VERSION is bumped.
//
// Renovate's regex manager (see renovate.json5) updates the version ARG but
// cannot compute the new release-artifact checksum, so this post-upgrade task
// downloads the exact tarball the Dockerfile will fetch and rewrites
// SIGNAL_CLI_SHA256. This keeps the build's `sha256sum -c` supply-chain check
// green on the Renovate PR.
//
// Depends only on Node (guaranteed present in the Renovate runtime) — no curl,
// wget, or tar required.

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const DOCKERFILE = "Dockerfile";

const src = readFileSync(DOCKERFILE, "utf8");

const versionMatch = src.match(/^ARG SIGNAL_CLI_VERSION=(.+)$/m);
if (!versionMatch) {
  console.error("update-signal-cli-checksum: SIGNAL_CLI_VERSION not found in Dockerfile");
  process.exit(1);
}
const version = versionMatch[1].trim();

const url = `https://github.com/AsamK/signal-cli/releases/download/v${version}/signal-cli-${version}.tar.gz`;
console.log(`update-signal-cli-checksum: fetching ${url}`);

let res;
try {
  res = await fetch(url, { redirect: "follow" });
} catch (err) {
  console.error(`update-signal-cli-checksum: download failed: ${err.message}`);
  process.exit(1);
}
if (!res.ok) {
  console.error(`update-signal-cli-checksum: download failed (HTTP ${res.status})`);
  process.exit(1);
}

const buf = Buffer.from(await res.arrayBuffer());
const sha = createHash("sha256").update(buf).digest("hex");

const existing = src.match(/^ARG SIGNAL_CLI_SHA256=(.+)$/m);
if (existing && existing[1].trim() === sha) {
  console.log("update-signal-cli-checksum: checksum already up to date");
  process.exit(0);
}

const out = src.replace(/^ARG SIGNAL_CLI_SHA256=.+$/m, `ARG SIGNAL_CLI_SHA256=${sha}`);
writeFileSync(DOCKERFILE, out);
console.log(`update-signal-cli-checksum: signal-cli ${version} -> ${sha}`);
