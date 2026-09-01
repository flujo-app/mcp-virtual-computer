#!/usr/bin/env node
// Cut a PyPI and GitHub release while keeping the familiar `npm run release`
// interface used by sibling FLUJO projects. npm is only the task runner; a
// dedicated GitHub Actions workflow publishes through PyPI Trusted Publishing.
//
//   npm run release                 patch bump
//   npm run release minor           minor bump
//   npm run release major           major bump
//   npm run release -- 1.2.3        exact version
//   npm run release -- --dry-run    preflight and tests; publish nothing

import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const repository = "flujo-app/mcp-virtual-computer";
const distribution = "mcp-virtual-computer";
const releaseWorkflow = "release.yml";
const args = process.argv.slice(2);
const checkOnly = takeFlag("--check");
const dryRun = takeFlag("--dry-run");
const positional = args.filter((value) => !value.startsWith("--"));
const bump = positional[0] ?? "patch";

if (args.some((value) => value.startsWith("--"))) {
  fail(`unknown option(s): ${args.filter((value) => value.startsWith("--")).join(", ")}`);
}
if (positional.length > 1) {
  fail(`expected one version argument, received: ${positional.join(", ")}`);
}
if (!/^(patch|minor|major|\d+\.\d+\.\d+)$/.test(bump)) {
  fail(`unknown version ${JSON.stringify(bump)}; use patch, minor, major, or x.y.z`);
}

if (checkOnly) {
  run("uv", ["--version"]);
  run("gh", ["--version"]);
  runNpm(["--version"]);
  run(process.execPath, ["scripts/sync-version.mjs", "--check"]);
  console.log("Release command self-check passed.");
  process.exit(0);
}

if (git(["branch", "--show-current"]) !== "main") {
  fail("releases must be cut from main");
}
if (git(["status", "--porcelain"]) !== "") {
  fail("working tree is not clean; commit or stash changes first");
}

const remote = releaseRemote();
console.log(`Using Git remote ${remote} for ${repository}.`);
run("git", [
  "fetch",
  remote,
  "main",
  "+refs/tags/v*:refs/tags/v*",
]);
if (git(["rev-parse", "HEAD"]) !== git(["rev-parse", `${remote}/main`])) {
  fail(`local main and ${remote}/main differ; pull or push first`);
}
run("gh", ["auth", "status"]);

const current = readPackageVersion();
const next = nextVersion(current, bump);
if (next === current) fail(`version is already ${current}`);
console.log(`Release version: ${current} -> ${next}`);

const existing = await pypiVersion(next);
if (existing) fail(`${distribution} ${next} already exists on PyPI`);

runNpm(["run", "check"]);
if (dryRun) {
  console.log(
    `\nDry run passed. Would create v${next}, push main and its tag, then dispatch GitHub Trusted Publishing for ${distribution} ${next}.`,
  );
  process.exit(0);
}

runNpm(["version", bump, "-m", "Release v%s"]);
const version = readPackageVersion();
const tag = `v${version}`;
runNpm(["run", "check"]);

const artifactPrefix = `${distribution.replace(/-/g, "_")}-${version}`;
const artifacts = readdirSync(path.join(root, "dist"))
  .filter(
    (name) =>
      name.startsWith(artifactPrefix) &&
      (name.endsWith(".whl") || name.endsWith(".tar.gz")),
  )
  .map((name) => path.join("dist", name));
if (artifacts.length !== 2) {
  fail(
    `expected one wheel and one source archive for ${artifactPrefix}, found ${artifacts.length}`,
  );
}
run("git", ["push", remote, "main", tag]);
run("gh", [
  "workflow",
  "run",
  releaseWorkflow,
  "--repo",
  repository,
  "--ref",
  tag,
]);
const releaseSha = git(["rev-parse", "HEAD"]);
const workflowRun = await waitForWorkflow(releaseSha);
run("gh", [
  "run",
  "watch",
  String(workflowRun.databaseId),
  "--repo",
  repository,
  "--exit-status",
]);
await waitForPypi(version);

console.log(`\nReleased ${distribution} ${version}:`);
console.log(`  PyPI:   https://pypi.org/project/${distribution}/${version}/`);
console.log(`  GitHub: https://github.com/${repository}/releases/tag/${tag}`);
console.log("  MCP:    run `npm run registry:release` after PyPI propagation completes");

function releaseRemote() {
  const remotes = git(["remote"]).split(/\r?\n/).filter(Boolean);
  for (const candidate of remotes) {
    const url = git(["remote", "get-url", candidate]).toLowerCase();
    if (url.includes(repository.toLowerCase())) return candidate;
  }
  fail(`no Git remote points to ${repository}`);
}

function readPackageVersion() {
  return JSON.parse(readFileSync(path.join(root, "package.json"), "utf8")).version;
}

function nextVersion(current, requested) {
  if (/^\d+\.\d+\.\d+$/.test(requested)) return requested;
  const [major, minor, patch] = current.split(".").map(Number);
  if (requested === "major") return `${major + 1}.0.0`;
  if (requested === "minor") return `${major}.${minor + 1}.0`;
  return `${major}.${minor}.${patch + 1}`;
}

async function pypiVersion(version) {
  const response = await fetch(
    `https://pypi.org/pypi/${distribution}/${version}/json`,
  );
  if (response.status === 404) return false;
  if (!response.ok) fail(`PyPI lookup failed with HTTP ${response.status}`);
  return true;
}

async function waitForPypi(version) {
  for (let attempt = 0; attempt < 24; attempt += 1) {
    if (await pypiVersion(version)) return;
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  fail(`${distribution} ${version} was uploaded but did not appear on PyPI`);
}

async function waitForWorkflow(sha) {
  console.log("\nWaiting for the Trusted Publishing workflow ...");
  for (let attempt = 0; attempt < 24; attempt += 1) {
    const result = spawnSync(
      "gh",
      [
        "run",
        "list",
        "--repo",
        repository,
        "--workflow",
        releaseWorkflow,
        "--commit",
        sha,
        "--event",
        "workflow_dispatch",
        "--limit",
        "1",
        "--json",
        "databaseId,url",
      ],
      { cwd: root, encoding: "utf8" },
    );
    if (result.status === 0) {
      const runs = JSON.parse(result.stdout || "[]");
      if (runs.length > 0) return runs[0];
    }
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  fail(
    `Trusted Publishing did not start; inspect https://github.com/${repository}/actions/workflows/${releaseWorkflow}`,
  );
}

function git(arguments_) {
  const result = spawnSync("git", arguments_, {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    fail(result.stderr.trim() || `git ${arguments_.join(" ")} failed`);
  }
  return result.stdout.trim();
}

function runNpm(arguments_) {
  // npm is a shell shim on Windows, which breaks arguments containing spaces
  // (such as the version commit message). Re-enter npm through its JS CLI when
  // this script was launched by `npm run`, preserving the argument boundaries.
  if (process.env.npm_execpath) {
    run(process.execPath, [process.env.npm_execpath, ...arguments_]);
    return;
  }
  run("npm", arguments_, { shell: process.platform === "win32" });
}

function run(command, arguments_, extra = {}) {
  console.log(`\n> ${command} ${arguments_.join(" ")}`);
  const result = spawnSync(command, arguments_, {
    cwd: root,
    stdio: "inherit",
    ...extra,
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

function fail(message) {
  console.error(`\nRelease aborted: ${message}`);
  process.exit(1);
}
