#!/usr/bin/env node
// Run the local, credential-free release gate with argument-safe subprocesses.

import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");

run(process.execPath, ["scripts/sync-version.mjs", "--check"]);
run("uv", ["sync", "--all-extras", "--dev"]);
run("uv", [
  "run",
  "--no-sync",
  "ruff",
  "check",
  "src/kilntainers",
  "--select",
  "I,F401,RUF,TID",
]);
const productionModules = pythonModules(path.join(root, "src", "kilntainers"));
run("uv", ["run", "--no-sync", "ty", "check", ...productionModules]);
run("uv", [
  "run",
  "--no-sync",
  "python",
  "-m",
  "pytest",
  "src/kilntainers",
  "-m",
  "not integration and not e2e and not http_integration",
  "-q",
]);
run("uv", ["build", "--no-sources", "--clear"]);

console.log("Release checks passed.");

function run(command, args) {
  console.log(`\n> ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function pythonModules(directory) {
  const modules = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "__pycache__") modules.push(...pythonModules(absolute));
      continue;
    }
    if (entry.name.endsWith(".py") && !entry.name.startsWith("test_")) {
      modules.push(path.relative(root, absolute));
    }
  }
  return modules;
}
