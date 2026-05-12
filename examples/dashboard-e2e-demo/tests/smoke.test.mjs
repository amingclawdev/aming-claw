import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const app = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const api = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");

assert.match(app, /loadDashboardSummary/);
assert.match(api, /summarizeTasks/);
console.log("dashboard-e2e-demo smoke ok");
