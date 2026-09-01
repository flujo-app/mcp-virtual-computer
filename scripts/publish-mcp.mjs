#!/usr/bin/env node
// Validate or publish this PyPI-backed server to the official MCP Registry.
// The pinned publisher binary is downloaded into the ignored .tools directory
// and verified before it is executed.
//
//   npm run registry:validate
//   npm run registry:release
//   npm run registry:release -- --login github-oidc
//   npm run registry:release -- --no-login

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const DEFAULT_REGISTRY = "https://registry.modelcontextprotocol.io";
const PUBLISHER_VERSION = "1.8.1";
const PUBLISHER_TARGETS = {
  "win32-x64": {
    asset: "mcp-publisher_windows_amd64.tar.gz",
    sha256: "399ad0d6e00a50812b563a71d8bfbff5160c085e6b13aac6ec083d98d5ff7c45",
  },
  "win32-arm64": {
    asset: "mcp-publisher_windows_arm64.tar.gz",
    sha256: "2e3a3435e27afcaa35bec9cb573bab99db9e980d7fb49f90f32a59d03d4a3fa3",
  },
  "linux-x64": {
    asset: "mcp-publisher_linux_amd64.tar.gz",
    sha256: "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc",
  },
  "linux-arm64": {
    asset: "mcp-publisher_linux_arm64.tar.gz",
    sha256: "8dd75a6cf6845688b5d4e46df58d3ca26d5c8d233bb0626606e1db82c5e883e4",
  },
  "darwin-x64": {
    asset: "mcp-publisher_darwin_amd64.tar.gz",
    sha256: "88126981225e7714fcc6b7a10cdba4a80ae5901e9740a8c06d0d5195c8bc294c",
  },
  "darwin-arm64": {
    asset: "mcp-publisher_darwin_arm64.tar.gz",
    sha256: "e45e520892460732a4bdf37255576415d4a53ec171f8b913faf15bb1aef7cb77",
  },
};
const LOGIN_METHODS = ["github", "github-oidc", "dns", "http", "none"];
const LOGIN_FLAGS = [
  "--domain",
  "--private-key",
  "--kv-vault",
  "--kv-key-name",
  "--kms-resource",
  "--signer-type",
  "--algorithm",
  "--token",
];

const args = process.argv.slice(2);
const dryRun = takeFlag("--dry-run");
const force = takeFlag("--force");
const noLogin = takeFlag("--no-login");
const registryUrl = (takeOption("--registry") ?? DEFAULT_REGISTRY).replace(
  /\/+$/,
  "",
);
const loginMethod = takeOption("--login") ?? "github";
const loginExtras = collectLoginExtras();
if (!LOGIN_METHODS.includes(loginMethod)) {
  fail(`unsupported login method ${JSON.stringify(loginMethod)}`);
}
if (args.length > 0) fail(`unknown argument(s): ${args.join(", ")}`);

const normalizedRoot = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const packageJson = JSON.parse(
  await readFile(path.join(normalizedRoot, "package.json"), "utf8"),
);
const serverPath = path.join(normalizedRoot, "server.json");
const server = JSON.parse(await readFile(serverPath, "utf8"));
const readme = await readFile(path.join(normalizedRoot, "README.md"), "utf8");
const serverName = server.name;
const version = packageJson.version;

console.log(`Publishing ${serverName}@${version} to ${registryUrl}`);
if (dryRun) console.log("DRY RUN - validation only; nothing will be published\n");

verifyMetadata();
for (const entry of server.packages.filter(
  (item) => item.registryType === "pypi",
)) {
  await verifyPublishedOnPypi(entry);
}

const listed = await publishedRegistryVersions();
if (listed.includes(version) && !force) {
  console.log(`${serverName}@${version} is already in the MCP Registry.`);
  process.exit(0);
}

const publisher = await ensurePublisher();
console.log("\nValidating server.json with mcp-publisher ...");
runPublisher(publisher, ["validate", serverPath]);

if (dryRun) {
  console.log("\nRegistry validation passed. Re-run registry:release to publish.");
  process.exit(0);
}

if (!noLogin && loginMethod !== "none") {
  console.log(`\nAuthenticating with ${registryUrl} using ${loginMethod} ...`);
  runPublisher(publisher, [
    "login",
    loginMethod,
    "--registry",
    registryUrl,
    ...loginExtras,
  ]);
}

console.log(`\nUploading ${serverName}@${version} ...`);
runPublisher(publisher, ["publish", serverPath]);
console.log(`Published ${serverName}@${version}`);
console.log(
  `Verify: ${registryUrl}/v0.1/servers?search=${encodeURIComponent(serverName)}`,
);

function verifyMetadata() {
  const problems = [];
  const distribution = "mcp-virtual-computer";
  const marker = `mcp-name: ${serverName}`;
  const expectedEnvironmentDefaults = {
    COMPUTER_ID: "agent-workstation",
    DESKTOP_ENVIRONMENT: "false",
    NETWORK_ACCESS: "true",
    AUTO_INSTALL_DOCKER: "true",
    EXPOSE_LIFECYCLE_TOOLS: "false",
  };
  if (server.version !== version) {
    problems.push(`server.json version does not match package.json ${version}`);
  }
  if (!readme.includes(marker)) {
    problems.push(`README.md is missing the PyPI ownership marker ${marker}`);
  }
  if (!Array.isArray(server.packages) || server.packages.length === 0) {
    problems.push("server.json declares no packages");
  }
  for (const entry of server.packages ?? []) {
    if (entry.registryType !== "pypi") continue;
    if (entry.identifier !== distribution) {
      problems.push(`PyPI identifier must be ${distribution}`);
    }
    if (entry.version !== version) {
      problems.push(`PyPI entry must use version ${version}`);
    }
    const environment = new Map(
      (entry.environmentVariables ?? []).map((item) => [item.name, item]),
    );
    for (const [name, expectedDefault] of Object.entries(
      expectedEnvironmentDefaults,
    )) {
      if (environment.get(name)?.default !== expectedDefault) {
        problems.push(`${name} must default to ${expectedDefault}`);
      }
    }
  }
  if (!(server.icons ?? []).some((icon) => icon.mimeType === "image/png")) {
    problems.push("server.json must declare a PNG icon");
  }
  if (!serverName.startsWith("io.github.flujo-app/")) {
    problems.push("GitHub authentication requires the io.github.flujo-app/ namespace");
  }
  if (problems.length > 0) fail(problems.join("\n  - "));
  console.log(`  metadata OK (${distribution}@${version})`);
}

async function verifyPublishedOnPypi(entry) {
  const url = `https://pypi.org/pypi/${entry.identifier}/${entry.version}/json`;
  const response = await fetch(url);
  if (response.status === 404) {
    return report(
      `${entry.identifier} ${entry.version} is not on PyPI yet. Run npm run release first.`,
    );
  }
  if (!response.ok) fail(`PyPI lookup failed with HTTP ${response.status}`);
  const metadata = await response.json();
  const marker = `mcp-name: ${serverName}`;
  if (!String(metadata.info?.description ?? "").includes(marker)) {
    return report(`published PyPI README does not contain ${marker}`);
  }
  console.log(`  PyPI ownership marker OK (${entry.identifier}@${entry.version})`);
}

function report(message) {
  if (!dryRun) fail(message);
  console.warn(`  warning: ${message}`);
}

async function publishedRegistryVersions() {
  const url = `${registryUrl}/v0.1/servers/${encodeURIComponent(serverName)}/versions`;
  const response = await fetch(url);
  if (response.status === 404) return [];
  if (!response.ok) {
    console.warn(`  warning: registry lookup returned HTTP ${response.status}`);
    return [];
  }
  const body = await response.json();
  return (body.servers ?? [])
    .map((item) => item.server?.version)
    .filter((item) => typeof item === "string");
}

async function ensurePublisher() {
  if (process.env.MCP_PUBLISHER_BIN) return process.env.MCP_PUBLISHER_BIN;
  const target = PUBLISHER_TARGETS[`${process.platform}-${process.arch}`];
  if (!target) {
    fail(`no pinned mcp-publisher build for ${process.platform}-${process.arch}`);
  }
  const directory = path.join(
    normalizedRoot,
    ".tools",
    "mcp-publisher",
    PUBLISHER_VERSION,
  );
  const binary = path.join(
    directory,
    process.platform === "win32" ? "mcp-publisher.exe" : "mcp-publisher",
  );
  const stampPath = path.join(directory, "install.json");
  const stamp = await readFile(stampPath, "utf8").catch(() => undefined);
  if (stamp && JSON.parse(stamp).sha256 === target.sha256) return binary;

  const url = `https://github.com/modelcontextprotocol/registry/releases/download/v${PUBLISHER_VERSION}/${target.asset}`;
  console.log(`  downloading mcp-publisher ${PUBLISHER_VERSION} ...`);
  const response = await fetch(url);
  if (!response.ok) fail(`publisher download failed with HTTP ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== target.sha256) {
    fail(`publisher checksum mismatch: expected ${target.sha256}, got ${actual}`);
  }
  await mkdir(directory, { recursive: true });
  const archive = path.join(directory, target.asset);
  await writeFile(archive, bytes);
  const extraction = spawnSync("tar", ["-xzf", archive, "-C", directory], {
    stdio: "inherit",
  });
  if (extraction.error) throw extraction.error;
  if (extraction.status !== 0) fail("could not extract mcp-publisher");
  if (process.platform !== "win32") await chmod(binary, 0o755);
  await writeFile(
    stampPath,
    JSON.stringify(
      { version: PUBLISHER_VERSION, asset: target.asset, sha256: target.sha256 },
      null,
      2,
    ),
  );
  return binary;
}

function runPublisher(binary, arguments_) {
  console.log(`> mcp-publisher ${arguments_.join(" ")}`);
  const result = spawnSync(binary, arguments_, {
    cwd: normalizedRoot,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function takeFlag(name) {
  const index = args.indexOf(name);
  if (index === -1) return false;
  args.splice(index, 1);
  return true;
}

function takeOption(name) {
  const index = args.indexOf(name);
  if (index === -1) return undefined;
  const value = args[index + 1];
  if (!value || value.startsWith("--")) fail(`${name} requires a value`);
  args.splice(index, 2);
  return value;
}

function collectLoginExtras() {
  const extras = [];
  for (const flag of LOGIN_FLAGS) {
    const value = takeOption(flag);
    if (value !== undefined) extras.push(flag, value);
  }
  return extras;
}

function fail(message) {
  throw new Error(`MCP Registry release aborted: ${message}`);
}
