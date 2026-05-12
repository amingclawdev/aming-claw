import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(HERE, "..");
const REPO_ROOT = resolve(PROJECT_ROOT, "..", "..");

const defaultArgs = [
  join(REPO_ROOT, "scripts", "materialize-fixture.mjs"),
  "--root",
  PROJECT_ROOT,
  "--artifact",
  join(REPO_ROOT, "docs", "fixtures", "external-governance-demo", "l4-smoke-fixture.md"),
];

const result = spawnSync(process.execPath, [...defaultArgs, ...process.argv.slice(2)], {
  stdio: "inherit",
});

process.exit(result.status ?? 1);
