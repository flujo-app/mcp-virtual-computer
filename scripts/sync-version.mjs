#!/usr/bin/env node
// Keep the npm release runner, Python distribution, import metadata, lockfile,
// and immutable MCP Registry metadata on exactly one version.

import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const packageJson = readJson("package.json");
const version = packageJson.version;
const distribution = "mcp-sandbox-computer-vm-for-ai";
const checkOnly = process.argv.includes("--check");
let failed = false;
let changed = 0;

if (!/^\d+\.\d+\.\d+$/.test(version)) {
  fail(`package.json has unsupported version ${JSON.stringify(version)}`);
}

syncText(
  "pyproject.toml",
  /(\[project\][\s\S]*?\nversion = ")[^"]+("\r?\n)/,
  `$1${version}$2`,
);
syncText(
  "src/kilntainers/__init__.py",
  /(__version__ = ")[^"]+("\r?\n?)/,
  `$1${version}$2`,
);

const pyproject = readFileSync(path.join(root, "pyproject.toml"), "utf8");
if (!new RegExp(`\\[project\\][\\s\\S]*?\\nname = "${distribution}"`).test(pyproject)) {
  fail(`pyproject.toml project name must be ${distribution}`);
}

const serverPath = path.join(root, "server.json");
const serverRaw = readFileSync(serverPath, "utf8");
const server = JSON.parse(serverRaw);
const pypiPackages = (server.packages ?? []).filter(
  (entry) => entry.registryType === "pypi",
);
if (
  server.version !== version ||
  pypiPackages.some(
    (entry) => entry.version !== version || entry.identifier !== distribution,
  )
) {
  if (checkOnly) {
    fail(`server.json does not identify ${distribution}@${version}`);
  } else {
    server.version = version;
    for (const entry of pypiPackages) {
      entry.identifier = distribution;
      entry.version = version;
    }
    const eol = serverRaw.includes("\r\n") ? "\r\n" : "\n";
    writeFileSync(
      serverPath,
      `${JSON.stringify(server, null, 2).replace(/\n/g, eol)}${eol}`,
    );
    console.log(`sync-version: server.json -> ${version}`);
    changed += 1;
  }
}

if (!checkOnly && changed > 0) {
  run("uv", ["lock"]);
}

const lock = readFileSync(path.join(root, "uv.lock"), "utf8");
const escapedDistribution = distribution.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const lockedProject = new RegExp(
  `\\[\\[package\\]\\]\\r?\\nname = "${escapedDistribution}"\\r?\\nversion = "${version}"`,
);
if (!lockedProject.test(lock)) {
  fail(`uv.lock does not contain ${distribution}@${version}`);
}

if (failed) process.exit(1);
console.log(
  checkOnly
    ? `sync-version: all release metadata matches ${version}.`
    : `sync-version: ${changed} file(s) updated to ${version}.`,
);

function syncText(relative, pattern, replacement) {
  const absolute = path.join(root, relative);
  const source = readFileSync(absolute, "utf8");
  if (!pattern.test(source)) {
    fail(`version pattern not found in ${relative}`);
    return;
  }
  const updated = source.replace(pattern, replacement);
  if (updated === source) return;
  if (checkOnly) {
    fail(`${relative} does not report ${version}`);
    return;
  }
  writeFileSync(absolute, updated);
  console.log(`sync-version: ${relative} -> ${version}`);
  changed += 1;
}

function readJson(relative) {
  return JSON.parse(readFileSync(path.join(root, relative), "utf8"));
}

function run(command, args) {
  console.log(`> ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function fail(message) {
  console.error(`sync-version: ${message}`);
  failed = true;
}
