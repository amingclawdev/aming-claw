#!/usr/bin/env node
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const dashboardDir = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(dashboardDir, "../..");
const source = path.join(dashboardDir, "dist");
const target = path.join(repoRoot, "agent", "governance", "dashboard_dist");

if (!existsSync(path.join(source, "index.html"))) {
  throw new Error(`dashboard dist is missing index.html: ${source}`);
}

mkdirSync(target, { recursive: true });
for (const entry of readdirSync(target)) {
  if (entry === "__init__.py" || entry === "__pycache__") continue;
  rmSync(path.join(target, entry), { recursive: true, force: true });
}

cpSync(source, target, {
  recursive: true,
  force: true,
  filter: (src) => !src.endsWith(".map"),
});

console.log(`synced dashboard dist to ${path.relative(repoRoot, target)}`);
