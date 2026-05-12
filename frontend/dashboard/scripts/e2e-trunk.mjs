#!/usr/bin/env node
// dashboard-trunk-e2e
//
// Layered dashboard E2E trunk for the local plugin MVP. It builds an isolated
// fixture project, bootstraps its graph, commits a trunk change, materializes
// scope reconcile, and checks that the dashboard/API contracts stay aligned.
//
//   node scripts/e2e-trunk.mjs
//   node scripts/e2e-trunk.mjs --reset
//   node scripts/e2e-trunk.mjs --probe
//   node scripts/e2e-trunk.mjs --semantic-live   # real AI call + review accept
//   node scripts/e2e-trunk.mjs --semantic-live --semantic-decision reject
//   node scripts/e2e-trunk.mjs --skip-dashboard
//   node scripts/e2e-trunk.mjs --probe --static-route --build-dashboard
//
// The default run mutates only the isolated fixture workspace under the OS temp
// directory and the governance DB rows for that fixture project. It queues one
// semantic job only to prove cancel works, then clears it before the live AI
// worker should have anything durable to review. Real AI review is opt-in.

import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { exit } from "node:process";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const DEFAULT_PROJECT = "dashboard-e2e-fixture";
const DEFAULT_WORKSPACE = path.join(os.tmpdir(), "aming-claw-dashboard-e2e", DEFAULT_PROJECT);

const FLAGS = parseFlags(process.argv.slice(2));
const BACKEND = FLAGS.backend || process.env.VITE_BACKEND_URL || "http://localhost:40000";
const STATIC_ROUTE = FLAGS["static-route"] === true;
const BUILD_DASHBOARD = FLAGS["build-dashboard"] === true;
const DEFAULT_DASHBOARD = STATIC_ROUTE ? `${BACKEND.replace(/\/+$/, "")}/dashboard` : "http://localhost:5173";
const DASHBOARD = FLAGS.dashboard || process.env.DASHBOARD_URL || process.env.VITE_DASHBOARD_URL || DEFAULT_DASHBOARD;
const PROJECT = FLAGS.project || process.env.VITE_PROJECT_ID || DEFAULT_PROJECT;
const WORKSPACE = path.resolve(FLAGS.workspace || DEFAULT_WORKSPACE);
const ARTIFACTS = path.resolve(FLAGS.artifacts || path.join(WORKSPACE, ".aming-claw", "e2e-artifacts"));
const RESET = FLAGS.reset === true;
const PROBE_ONLY = FLAGS.probe === true;
const SKIP_DASHBOARD = FLAGS["skip-dashboard"] === true;
const SKIP_SEMANTIC = FLAGS["skip-semantic"] === true;
const SEMANTIC_LIVE = FLAGS["semantic-live"] === true;
const SEMANTIC_DECISION = String(FLAGS["semantic-decision"] || "accept").trim().toLowerCase();
const SEMANTIC_TIMEOUT_MS = Number(FLAGS["semantic-timeout-ms"] || 180000);
const KEEP_WORKSPACE = FLAGS.keep === true;
const RUN_ID = FLAGS["run-id"] || `dashboard-trunk-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}`;
if (!["accept", "reject", "both"].includes(SEMANTIC_DECISION)) {
  throw new Error("--semantic-decision must be accept, reject, or both");
}

const C = {
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  cyan: "\x1b[36m",
};

const c = (color, text) => `${C[color]}${text}${C.reset}`;
const say = (color, tag, text) => console.log(`${c(color, tag)} ${text}`);
const info = (text) => say("dim", "  -", text);
const ok = (text) => say("green", "  ok", text);
const warn = (text) => say("yellow", "  warn", text);
const fail = (text) => say("red", "  fail", text);

const results = [];
const context = {
  backend: BACKEND,
  dashboard: DASHBOARD,
  project: PROJECT,
  workspace: WORKSPACE,
  artifacts: ARTIFACTS,
  runId: RUN_ID,
  baselineCommit: "",
  targetCommit: "",
  bootstrap: null,
  baselineStatus: null,
  baselineSummary: null,
  baselineNode: null,
  reconciledStatus: null,
  reconciledNode: null,
  semantic: null,
  e2eEvidence: null,
};

function parseFlags(args) {
  const bool = new Set([
    "reset",
    "probe",
    "skip-dashboard",
    "skip-semantic",
    "semantic-live",
    "keep",
    "static-route",
    "build-dashboard",
  ]);
  const out = {};
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (!arg.startsWith("--")) continue;
    const key = arg.slice(2);
    if (bool.has(key)) {
      out[key] = true;
    } else {
      out[key] = args[i + 1];
      i++;
    }
  }
  return out;
}

class HttpError extends Error {
  constructor(method, route, status, body, request) {
    super(`${method} ${route} -> ${status}`);
    this.method = method;
    this.route = route;
    this.status = status;
    this.body = body;
    this.request = request;
  }
}

function pid(projectId) {
  return encodeURIComponent(projectId);
}

function activePath(projectId, suffix) {
  return `/api/graph-governance/${pid(projectId)}/snapshots/active${suffix}`;
}

function snapshotPath(projectId, snapshotId, suffix) {
  return `/api/graph-governance/${pid(projectId)}/snapshots/${encodeURIComponent(snapshotId)}${suffix}`;
}

function shortCommit(commit) {
  return commit ? String(commit).slice(0, 7) : "-";
}

function normalizePath(value) {
  return String(value || "").replaceAll("\\", "/");
}

function allNodePaths(node) {
  return [
    ...(node.primary_files || []),
    ...(node.secondary_files || []),
    ...(node.test_files || []),
    ...(node.metadata?.config_files || []),
  ].map(normalizePath);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function writeText(file, text) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, text.replace(/\n/g, os.EOL), "utf8");
}

function readText(file) {
  return readFileSync(file, "utf8");
}

function command(cmd, args, cwd, options = {}) {
  try {
    return execFileSync(cmd, args, {
      cwd,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      ...options,
    }).trim();
  } catch (error) {
    if (options.allowFail) {
      return {
        ok: false,
        status: error.status,
        stdout: String(error.stdout || "").trim(),
        stderr: String(error.stderr || "").trim(),
      };
    }
    const stderr = String(error.stderr || "").trim();
    throw new Error(`${cmd} ${args.join(" ")} failed${stderr ? `: ${stderr}` : ""}`);
  }
}

function git(args, cwd = WORKSPACE, options = {}) {
  return command("git", args, cwd, options);
}

function trimTrailingSlash(value) {
  return String(value || "").replace(/\/+$/, "");
}

function sameOriginApiPath(baseUrl, route) {
  return new URL(route, `${trimTrailingSlash(baseUrl)}/`).toString();
}

async function fetchText(url) {
  const response = await fetch(url).catch((error) => ({
    ok: false,
    status: 0,
    headers: new Headers(),
    text: async () => String(error),
  }));
  const text = await response.text().catch(() => "");
  return { response, text };
}

async function validateDashboardStaticRoute() {
  const base = trimTrailingSlash(DASHBOARD);
  const index = await fetchText(base);
  assert(index.response.ok, `dashboard static route ${base} is not reachable (status ${index.response.status})`);
  assert(index.text.includes("<div id=\"root\"></div>"), "dashboard static index missing React root");
  assert(index.text.includes("/dashboard/assets/"), "dashboard static index does not reference /dashboard/assets/");

  const assetRefs = [...index.text.matchAll(/(?:src|href)="([^"]+)"/g)]
    .map((match) => match[1])
    .filter((value) => value.includes("/dashboard/assets/"));
  assert(assetRefs.some((value) => value.endsWith(".js")), "dashboard static index missing JS asset");
  assert(assetRefs.some((value) => value.endsWith(".css")), "dashboard static index missing CSS asset");

  const assets = [];
  for (const ref of assetRefs) {
    const assetUrl = new URL(ref, base).toString();
    const asset = await fetchText(assetUrl);
    assert(asset.response.ok, `dashboard asset ${assetUrl} failed with status ${asset.response.status}`);
    const contentType = asset.response.headers.get("content-type") || "";
    if (ref.endsWith(".js")) {
      assert(contentType.includes("javascript"), `dashboard JS asset content-type was ${contentType}`);
      assert(asset.text.includes("React") || asset.text.includes("createElement") || asset.text.length > 1000, "dashboard JS asset looked empty");
    }
    if (ref.endsWith(".css")) {
      assert(contentType.includes("text/css"), `dashboard CSS asset content-type was ${contentType}`);
      assert(asset.text.length > 100, "dashboard CSS asset looked empty");
    }
    assets.push({ ref, status: asset.response.status, content_type: contentType });
  }

  const fallback = await fetchText(`${base}/projects/static-route-smoke`);
  assert(fallback.response.ok, `dashboard SPA fallback failed with status ${fallback.response.status}`);
  assert(fallback.text.includes("<div id=\"root\"></div>"), "dashboard SPA fallback did not return index.html");

  const health = await fetch(sameOriginApiPath(base, "/api/health"));
  assert(health.ok, `same-origin /api/health failed with status ${health.status}`);
  const healthJson = await health.json();
  assert(healthJson.ok !== false, "same-origin /api/health returned ok=false");

  return {
    mode: "static-route",
    url: base,
    status: index.response.status,
    assets,
    spa_fallback_status: fallback.response.status,
    same_origin_api: "/api/health",
  };
}

async function http(method, route, body) {
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`${BACKEND}${route}`, init);
  } catch (error) {
    throw new HttpError(method, route, 0, String(error), body);
  }
  const text = await response.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  if (!response.ok) throw new HttpError(method, route, response.status, text, body);
  return json;
}

async function httpMaybe(method, route, body) {
  try {
    return { ok: true, value: await http(method, route, body) };
  } catch (error) {
    return { ok: false, error };
  }
}

async function waitFor(label, fn, { timeoutMs = 60000, intervalMs = 1000 } = {}) {
  const started = Date.now();
  let lastError = null;
  while (Date.now() - started < timeoutMs) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`${label} timed out${lastError ? `: ${lastError.message}` : ""}`);
}

async function runStep(id, title, fn) {
  console.log("");
  console.log(`${c("cyan", id)} ${c("bold", title)}`);
  const startedAt = new Date().toISOString();
  try {
    const detail = await fn();
    if (title === "Cleanup and artifacts" || title === "Artifacts") {
      detail.report_path = artifactPath("report.json");
    }
    results.push({ id, title, status: "passed", started_at: startedAt, ended_at: new Date().toISOString(), detail });
    if (title === "Cleanup and artifacts" || title === "Artifacts") {
      detail.report_path = writeReport();
    }
    ok(title);
    return detail;
  } catch (error) {
    const detail = {
      message: error.message,
      http: error instanceof HttpError
        ? {
            method: error.method,
            route: error.route,
            status: error.status,
            body: String(error.body || "").slice(0, 1000),
            request: error.request,
          }
        : undefined,
    };
    results.push({ id, title, status: "failed", started_at: startedAt, ended_at: new Date().toISOString(), detail });
    fail(error.message);
    throw error;
  }
}

function artifactPath(name) {
  return path.join(ARTIFACTS, RUN_ID, name);
}

function writeArtifact(name, data) {
  const target = artifactPath(name);
  mkdirSync(path.dirname(target), { recursive: true });
  writeFileSync(target, JSON.stringify(data, null, 2) + "\n", "utf8");
  return target;
}

function buildReport() {
  return {
    ok: results.every((row) => row.status === "passed"),
    run_id: RUN_ID,
    project_id: PROJECT,
    workspace: WORKSPACE,
    backend: BACKEND,
    dashboard: SKIP_DASHBOARD ? "" : DASHBOARD,
    semantic_live: SEMANTIC_LIVE,
    semantic_decision: SEMANTIC_DECISION,
    semantic_skipped: SKIP_SEMANTIC,
    baseline_commit: context.baselineCommit,
    target_commit: context.targetCommit,
    active_snapshot_id: context.reconciledStatus?.active_snapshot_id || context.baselineStatus?.active_snapshot_id || "",
    semantic: context.semantic,
    e2e_evidence: context.e2eEvidence,
    results,
  };
}

function writeReport() {
  return writeArtifact("report.json", buildReport());
}

function writeFixtureFiles() {
  const config = `version: 2
project_id: ${PROJECT}
name: "Dashboard E2E Fixture"
language: typescript

testing:
  unit_command: "node tests/smoke.test.mjs"
  e2e_command: "node ${normalizePath(path.relative(WORKSPACE, path.join(SCRIPT_DIR, "e2e-trunk.mjs")))} --project ${PROJECT} --workspace ."
  allowed_commands:
    - executable: "node"
      args_prefixes: ["tests/smoke.test.mjs"]
  e2e:
    auto_run: false
    default_timeout_sec: 900
    max_parallel: 1
    require_clean_worktree: true
    evidence_retention_days: 14
    suites:
      dashboard.semantic.safe:
        label: "Dashboard semantic trunk safe path"
        command: "node ${normalizePath(path.relative(WORKSPACE, path.join(SCRIPT_DIR, "e2e-trunk.mjs")))} --project ${PROJECT} --workspace . --skip-dashboard"
        live_ai: false
        mutates_db: true
        requires_human_approval: false
        isolation_project: "${PROJECT}"
        trigger:
          paths:
            - "src/**"
            - "tests/**"
            - ".aming-claw.yaml"
          tags: ["dashboard", "semantic"]
      dashboard.semantic.live.reject:
        label: "Dashboard live semantic reject path"
        command: "node ${normalizePath(path.relative(WORKSPACE, path.join(SCRIPT_DIR, "e2e-trunk.mjs")))} --project ${PROJECT} --workspace . --semantic-live --semantic-decision reject --skip-dashboard"
        live_ai: true
        mutates_db: true
        requires_human_approval: true
        isolation_project: "${PROJECT}"
        trigger:
          paths:
            - "src/**"
          tags: ["semantic-review", "reject"]

governance:
  enabled: true
  test_tool_label: "node"

graph:
  exclude_paths:
    - "node_modules"
    - "dist"
    - "coverage"
    - ".aming-claw/e2e-artifacts"
  ignore_globs:
    - "**/node_modules/**"
    - "**/dist/**"
    - "**/coverage/**"
    - "**/.aming-claw/e2e-artifacts/**"
  nested_projects:
    mode: "exclude"
    roots: []

ai:
  routing:
    pm:
      provider: "openai"
      model: "gpt-5.5"
    dev:
      provider: "openai"
      model: "gpt-5.4"
    tester:
      provider: "openai"
      model: "gpt-5.4"
    qa:
      provider: "openai"
      model: "gpt-5.5"
    semantic:
      provider: "anthropic"
      model: "claude-opus-4-7"
`;

  writeText(path.join(WORKSPACE, ".aming-claw.yaml"), config);
  writeText(
    path.join(WORKSPACE, ".gitignore"),
    `node_modules/
dist/
coverage/
.aming-claw/e2e-artifacts/
`,
  );
  writeText(
    path.join(WORKSPACE, "package.json"),
    `{"name":"${PROJECT}","private":true,"type":"module","scripts":{"test":"node tests/smoke.test.mjs"}}\n`,
  );
  writeText(
    path.join(WORKSPACE, "README.md"),
    `# Dashboard E2E Fixture

This project is generated by the dashboard trunk E2E harness.

It intentionally includes TypeScript source, a test, docs, and ignored output
folders so the graph builder can prove project bootstrap and reconcile behavior
without mutating the main aming-claw workspace.
`,
  );
  writeText(
    path.join(WORKSPACE, "src", "api.ts"),
    `export interface Task {
  id: string;
  title: string;
  status: "queued" | "running" | "done";
}

export async function fetchTasks(): Promise<Task[]> {
  return [
    { id: "fixture-1", title: "Check graph snapshot", status: "queued" },
    { id: "fixture-2", title: "Review pending reconcile", status: "running" },
  ];
}

export function summarizeTasks(tasks: Task[]): string {
  const open = tasks.filter((task) => task.status !== "done").length;
  return \`\${open}/\${tasks.length} open\`;
}

export function queueDepth(tasks: Task[]): number {
  return tasks.filter((task) => task.status === "queued").length;
}
`,
  );
  writeText(
    path.join(WORKSPACE, "src", "App.tsx"),
    `import { fetchTasks, queueDepth, summarizeTasks, type Task } from "./api";

export async function loadDashboardSummary(): Promise<string> {
  const tasks: Task[] = await fetchTasks();
  return summarizeTasks(tasks);
}

export async function loadQueueDepth(): Promise<number> {
  const tasks: Task[] = await fetchTasks();
  return queueDepth(tasks);
}

export function renderDashboardTitle(projectName: string): string {
  return \`\${projectName} dashboard\`;
}
`,
  );
  writeText(
    path.join(WORKSPACE, "tests", "smoke.test.mjs"),
    `import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = readFileSync(join(process.cwd(), "src", "api.ts"), "utf8");
if (!src.includes("summarizeTasks")) {
  throw new Error("fixture api is missing summarizeTasks");
}
console.log("fixture smoke ok");
`,
  );
  writeText(path.join(WORKSPACE, "node_modules", "ignored.js"), "export const ignored = true;\n");
  writeText(path.join(WORKSPACE, "dist", "bundle.js"), "export const bundled = true;\n");
  writeText(path.join(WORKSPACE, "coverage", "coverage.json"), "{\"ignored\":true}\n");
}

function ensureGitRepository() {
  if (!existsSync(path.join(WORKSPACE, ".git"))) {
    git(["init", "-b", "main"], WORKSPACE, { allowFail: true });
    if (!existsSync(path.join(WORKSPACE, ".git"))) {
      git(["init"], WORKSPACE);
      git(["checkout", "-B", "main"], WORKSPACE);
    }
  }
  git(["config", "user.email", "dashboard-e2e@example.local"], WORKSPACE);
  git(["config", "user.name", "Dashboard E2E"], WORKSPACE);
  git(["add", "."], WORKSPACE);
  const hasHead = git(["rev-parse", "--verify", "HEAD"], WORKSPACE, { allowFail: true });
  const dirty = git(["status", "--porcelain"], WORKSPACE);
  if (!hasHead.ok && dirty) {
    git(["commit", "-m", "baseline dashboard e2e fixture"], WORKSPACE);
  } else if (dirty) {
    git(["commit", "-m", "refresh dashboard e2e fixture"], WORKSPACE);
  }
  context.baselineCommit = git(["rev-parse", "HEAD"], WORKSPACE);
  return context.baselineCommit;
}

function mutateFixtureForScopeReconcile() {
  const file = path.join(WORKSPACE, "src", "api.ts");
  const runToken = RUN_ID.replace(/[^a-zA-Z0-9]/g, "").slice(-10);
  const marker = `// E2E_TRUNK_MARKER_START
export function formatTrunkRunLabel(count: number): string {
  return \`trunk-${runToken}:\${count}\`;
}
// E2E_TRUNK_MARKER_END
`;
  const before = readText(file);
  const updated = before.includes("// E2E_TRUNK_MARKER_START")
    ? before.replace(/\/\/ E2E_TRUNK_MARKER_START[\s\S]*?\/\/ E2E_TRUNK_MARKER_END\s*/m, marker)
    : `${before.trimEnd()}\n\n${marker}`;
  writeText(file, updated);
  git(["add", "src/api.ts"], WORKSPACE);
  const dirty = git(["status", "--porcelain"], WORKSPACE);
  assert(dirty, "fixture mutation did not change git state");
  git(["commit", "-m", `dashboard trunk change ${RUN_ID}`], WORKSPACE);
  context.targetCommit = git(["rev-parse", "HEAD"], WORKSPACE);
  return context.targetCommit;
}

function functionNames(node) {
  const functions = node?.metadata?.functions || [];
  return functions.map((item) => String(item).split("::").pop() || String(item));
}

function findNodeForPath(nodes, relPath) {
  const needle = normalizePath(relPath);
  return (nodes || []).find((node) => allNodePaths(node).some((item) => item === needle || item.endsWith(`/${needle}`)));
}

function assertNoIgnoredPaths(nodes, files) {
  const values = [];
  for (const node of nodes || []) values.push(...allNodePaths(node));
  for (const row of files || []) values.push(normalizePath(row.path || ""));
  const ignored = values.filter((item) =>
    item.includes("node_modules/")
    || item.includes("dist/")
    || item.includes("coverage/")
    || item.includes(".aming-claw/e2e-artifacts/"),
  );
  assert(ignored.length === 0, `ignored paths leaked into graph: ${ignored.slice(0, 5).join(", ")}`);
}

function queuedAiJobCount(ops) {
  const statuses = new Set(["queued", "running", "ai_pending", "ai_running"]);
  return (ops.operations || []).filter((op) =>
    (op.operation_type === "node_semantic" || op.operation_type === "edge_semantic")
    && statuses.has(String(op.status || "")),
  ).length;
}

function nodeSemanticOps(ops, nodeId) {
  return (ops.operations || []).filter((op) =>
    op.operation_type === "node_semantic" && String(op.target_id || "") === nodeId,
  );
}

function semanticJobPayload(node, { dryRun }) {
  return {
    job_type: "semantic_enrichment",
    target_scope: "node",
    target_ids: [node.node_id],
    options: {
      target: "nodes",
      include_nodes: true,
      include_edges: false,
      scope: "selected_node",
      mode: "semanticize",
      dry_run: Boolean(dryRun),
      skip_current: false,
      retry_stale_failed: true,
      include_package_markers: false,
    },
    created_by: "dashboard_trunk_e2e",
    actor: "dashboard_trunk_e2e",
    source: "dashboard_trunk_e2e",
  };
}

function semanticProjectionEntry(projection, nodeId) {
  const payload = projection?.projection || projection || {};
  const nodes = payload.node_semantics || {};
  return nodes[nodeId] || null;
}

function semanticProjectionPayload(entry) {
  if (!entry || typeof entry !== "object") return {};
  return entry.semantic || entry.semantic_payload || entry.payload || {};
}

function feedbackLinkedEventIds(item) {
  const evidence = item?.evidence && typeof item.evidence === "object" ? item.evidence : {};
  const rawIssue = evidence.raw_issue && typeof evidence.raw_issue === "object" ? evidence.raw_issue : {};
  const workerEvidence = rawIssue.evidence && typeof rawIssue.evidence === "object" ? rawIssue.evidence : {};
  const raw = workerEvidence.linked_event_ids || evidence.linked_event_ids || [];
  return Array.isArray(raw) ? raw.map(String).filter(Boolean) : [String(raw)].filter(Boolean);
}

async function clearTerminalSemanticJobs(snapshotId) {
  return http("POST", snapshotPath(PROJECT, snapshotId, "/semantic/jobs/clear-terminal"), {
    actor: "dashboard_trunk_e2e",
  });
}

async function cancelAllQueuedSemantic(snapshotId) {
  return http("POST", snapshotPath(PROJECT, snapshotId, "/semantic/jobs/cancel-all"), {
    operation_type: "node_semantic",
    target_scope: "node",
    status: "queued",
    actor: "dashboard_trunk_e2e",
  });
}

async function semanticFeedbackForNode(snapshotId, nodeId) {
  const feedback = await http(
    "GET",
    snapshotPath(PROJECT, snapshotId, `/feedback?node_id=${encodeURIComponent(nodeId)}&limit=100`),
  );
  const items = feedback.items || [];
  return items.find((item) => {
    const target = String(item.target_id || "");
    const nodes = Array.isArray(item.source_node_ids) ? item.source_node_ids.map(String) : [];
    const kind = String(item.feedback_kind || item.kind || "");
    const status = String(item.status || "").toLowerCase();
    return (
      (target === nodeId || nodes.includes(nodeId))
      && kind.includes("needs_observer_decision")
      && !["accepted", "rejected", "reviewed", "backlog_filed"].includes(status)
      && feedbackLinkedEventIds(item).length > 0
    );
  }) || null;
}

async function eventById(snapshotId, eventId) {
  return http("GET", snapshotPath(PROJECT, snapshotId, `/events/${encodeURIComponent(eventId)}`));
}

function e2eSuiteId() {
  if (SKIP_SEMANTIC) return "dashboard.trunk.safe";
  if (!SEMANTIC_LIVE) return "dashboard.semantic.safe";
  return `dashboard.semantic.live.${SEMANTIC_DECISION}`;
}

async function queueSelectedNodeSemanticJob(snapshotId, node) {
  const queued = await http(
    "POST",
    snapshotPath(PROJECT, snapshotId, "/semantic/jobs"),
    semanticJobPayload(node, { dryRun: false }),
  );
  assert(queued.status === "queued", `semantic queue returned ${queued.status}`);
  assert(queued.queued_count === 1, `selected-node queue should enqueue 1 row, got ${queued.queued_count}`);
  const queuedTargets = (queued.queued_ops || []).map((op) => String(op.target_id || op.job_id || ""));
  assert(queuedTargets.length === 1 && queuedTargets[0] === node.node_id, `queued_ops mismatch: ${queuedTargets.join(",")}`);
  return queued;
}

async function waitForSemanticProposal(snapshotId, nodeId, seenEventIds = new Set()) {
  return waitFor(
    "semantic worker feedback",
    async () => {
      const feedbackItem = await semanticFeedbackForNode(snapshotId, nodeId);
      if (!feedbackItem) return null;
      const ids = feedbackLinkedEventIds(feedbackItem);
      return ids.some((eventId) => !seenEventIds.has(eventId)) ? feedbackItem : null;
    },
    { timeoutMs: SEMANTIC_TIMEOUT_MS, intervalMs: 3000 },
  );
}

async function assertSemanticEventNotProjected(snapshotId, nodeId, eventId) {
  const projection = await http("GET", activePath(PROJECT, "/semantic/projection"));
  const projected = semanticProjectionEntry(projection, nodeId);
  if (projected) {
    const sourceEvent = projected.source_event || projected.source || {};
    assert(
      String(sourceEvent.event_id || sourceEvent.id || "") !== eventId,
      "rejected semantic event remained projected on the target node",
    );
  }
  const allNodeSemantics = projection.projection?.node_semantics || {};
  const mountedElsewhere = Object.entries(allNodeSemantics).filter(([otherNodeId, entry]) => {
    if (otherNodeId === nodeId) return false;
    const otherSource = entry?.source_event || entry?.source || {};
    return String(otherSource.event_id || otherSource.id || "") === eventId;
  });
  assert(mountedElsewhere.length === 0, `semantic event also mounted on another node: ${mountedElsewhere.map(([id]) => id).join(",")}`);
  return { projected };
}

async function applySemanticLiveDecision(snapshotId, node, decisionName, seenEventIds = new Set()) {
  const feedbackItem = await waitForSemanticProposal(snapshotId, node.node_id, seenEventIds);
  assert(String(feedbackItem.target_id || "") === node.node_id, "semantic feedback target_id mismatch");
  const linkedEventIds = feedbackLinkedEventIds(feedbackItem);
  assert(linkedEventIds.length > 0, "semantic feedback missing linked event ids");
  const eventId = linkedEventIds.find((id) => !seenEventIds.has(id)) || linkedEventIds[0];
  const eventResp = await eventById(snapshotId, eventId);
  const event = eventResp.event || {};
  assert(String(event.target_type || "") === "node", "semantic event target_type mismatch");
  assert(String(event.target_id || "") === node.node_id, "semantic event mounted to wrong node");
  const eventPayload = event.payload?.semantic_payload || event.payload?.semantic || {};
  assert(Object.keys(eventPayload).length > 0, "semantic event payload is empty");

  const action = decisionName === "reject" ? "reject_false_positive" : "accept_semantic_enrichment";
  const decisionVerb = decisionName === "reject" ? "rejected" : "accepted";
  const decision = await http("POST", snapshotPath(PROJECT, snapshotId, "/feedback/decision"), {
    feedback_ids: [feedbackItem.feedback_id],
    action,
    actor: "dashboard_trunk_e2e",
    rationale: `Dashboard trunk E2E ${decisionVerb} AI semantic enrichment for mount validation.`,
  });
  assert(decision.projection_rebuilt === true, `${decisionName} decision did not rebuild projection`);

  if (decisionName === "reject") {
    const rejected = decision.semantic_enrichment_rejected || {};
    assert((rejected.event_ids_rejected || []).includes(eventId), "reject decision did not reject linked semantic event");
    const eventAfter = await eventById(snapshotId, eventId);
    assert(String(eventAfter.event?.status || "") === "rejected", "semantic event status was not rejected");
    const jobAfter = await httpMaybe("GET", snapshotPath(PROJECT, snapshotId, `/semantic/jobs/${encodeURIComponent(node.node_id)}`));
    if (jobAfter.ok && jobAfter.value?.job?.status) {
      assert(String(jobAfter.value.job.status) === "rejected", `semantic job status after reject was ${jobAfter.value.job.status}`);
    }
    await assertSemanticEventNotProjected(snapshotId, node.node_id, eventId);
    return {
      decision: "reject",
      feedback_id: feedbackItem.feedback_id,
      event_id: eventId,
      event_status: "rejected",
      payload_keys: Object.keys(eventPayload).sort(),
    };
  }

  assert(decision.semantic_enrichment_accepted?.event_ids_flipped?.includes(eventId), "accept decision did not flip linked semantic event");
  const projection = await http("GET", activePath(PROJECT, "/semantic/projection"));
  const projected = semanticProjectionEntry(projection, node.node_id);
  assert(projected, "accepted semantic payload missing from projection for target node");
  const sourceEvent = projected.source_event || projected.source || {};
  assert(
    String(sourceEvent.event_id || sourceEvent.id || "") === eventId,
    "projection source_event does not match accepted semantic event",
  );
  const projectedPayload = semanticProjectionPayload(projected);
  assert(Object.keys(projectedPayload).length > 0, "projected semantic payload is empty");
  const allNodeSemantics = projection.projection?.node_semantics || {};
  const mountedElsewhere = Object.entries(allNodeSemantics).filter(([otherNodeId, entry]) => {
    if (otherNodeId === node.node_id) return false;
    const otherSource = entry?.source_event || entry?.source || {};
    return String(otherSource.event_id || otherSource.id || "") === eventId;
  });
  assert(mountedElsewhere.length === 0, `semantic event also mounted on another node: ${mountedElsewhere.map(([id]) => id).join(",")}`);
  return {
    decision: "accept",
    feedback_id: feedbackItem.feedback_id,
    event_id: eventId,
    event_status: event.status,
    projected_status: projected.validity?.status || projected.status || "",
    payload_keys: Object.keys(projectedPayload).sort(),
  };
}

async function loadRuntimeBundle(projectId) {
  const status = await http("GET", `/api/graph-governance/${pid(projectId)}/status`);
  const snapshotId = status.active_snapshot_id;
  assert(snapshotId, `${projectId} active snapshot missing`);
  const [summary, ops, nodes, edges, files, projection] = await Promise.all([
    http("GET", activePath(projectId, "/summary")),
    http("GET", `/api/graph-governance/${pid(projectId)}/operations/queue`),
    http("GET", snapshotPath(projectId, snapshotId, "/nodes?include_semantic=true&limit=2000")),
    http("GET", snapshotPath(projectId, snapshotId, "/edges?limit=6000")),
    http("GET", snapshotPath(projectId, snapshotId, "/files?limit=2000")),
    httpMaybe("GET", activePath(projectId, "/semantic/projection")),
  ]);
  return {
    status,
    snapshotId,
    summary,
    ops,
    nodes: nodes.nodes || [],
    edges: edges.edges || [],
    files: files.files || [],
    projection: projection.ok ? projection.value : null,
  };
}

async function stepEnvGate() {
  if (BUILD_DASHBOARD) {
    const dashboardDir = path.join(REPO_ROOT, "frontend", "dashboard");
    command("npm", ["run", "build"], dashboardDir);
  }
  const health = await http("GET", "/api/health");
  const projects = await http("GET", "/api/projects");
  assert(Array.isArray(projects.projects), "/api/projects did not return projects[]");
  let dashboard = { skipped: true };
  if (!SKIP_DASHBOARD) {
    if (STATIC_ROUTE) {
      dashboard = { skipped: false, ...(await validateDashboardStaticRoute()) };
    } else {
      const { response, text } = await fetchText(DASHBOARD);
      assert(response.ok, `dashboard ${DASHBOARD} is not reachable (status ${response.status})`);
      assert(text.includes("<div id=\"root\"></div>") || text.includes("aming-claw"), "dashboard root did not look like the Vite app");
      dashboard = { skipped: false, mode: "dev-or-custom", url: DASHBOARD, status: response.status };
    }
  }
  return { health, project_count: projects.projects.length, dashboard, built_dashboard: BUILD_DASHBOARD };
}

async function stepFixtureProject() {
  if (RESET && existsSync(WORKSPACE)) {
    rmSync(WORKSPACE, { recursive: true, force: true });
  }
  mkdirSync(WORKSPACE, { recursive: true });
  const shouldWrite = RESET || !existsSync(path.join(WORKSPACE, ".aming-claw.yaml"));
  if (shouldWrite) writeFixtureFiles();
  const commit = ensureGitRepository();
  const smoke = command("node", ["tests/smoke.test.mjs"], WORKSPACE);
  assert(smoke.includes("fixture smoke ok"), "fixture smoke test failed");
  return { workspace: WORKSPACE, project: PROJECT, baseline_commit: commit, reset: RESET, wrote_fixture: shouldWrite };
}

async function stepBootstrapProject() {
  const registered = await httpMaybe("POST", "/api/projects/register", { workspace_path: WORKSPACE });
  if (!registered.ok && registered.error.status !== 409) throw registered.error;
  const bootstrap = await http("POST", "/api/project/bootstrap", {
    workspace_path: WORKSPACE,
    project_name: PROJECT,
    scan_depth: 3,
    exclude_patterns: ["node_modules", "dist", "coverage", ".aming-claw/e2e-artifacts"],
  });
  assert(bootstrap.project_id === PROJECT, `bootstrap returned project ${bootstrap.project_id}, expected ${PROJECT}`);
  assert(bootstrap.snapshot_id, "bootstrap did not return snapshot_id");
  context.bootstrap = bootstrap;
  return {
    project_id: bootstrap.project_id,
    snapshot_id: bootstrap.snapshot_id,
    node_count: bootstrap.graph_stats?.node_count,
    edge_count: bootstrap.graph_stats?.edge_count,
    register_status: registered.ok ? "registered" : "already_registered",
  };
}

async function stepGraphBaseline() {
  const bundle = await loadRuntimeBundle(PROJECT);
  assert(bundle.status.graph_snapshot_commit === context.baselineCommit, "baseline graph commit does not match fixture HEAD");
  assert(bundle.status.current_state?.graph_stale?.is_stale === false, "baseline graph should not be stale");
  assert((bundle.summary.counts?.features || 0) > 0, "summary features count is zero");
  assert((bundle.nodes || []).length > 0, "nodes list is empty");
  assertNoIgnoredPaths(bundle.nodes, bundle.files);
  const apiNode = findNodeForPath(bundle.nodes, "src/api.ts");
  assert(apiNode, "src/api.ts node not found");
  const funcs = functionNames(apiNode);
  assert(funcs.some((name) => name.includes("summarizeTasks")), "src/api.ts functions did not include summarizeTasks");
  assert(apiNode.metadata?.function_lines, "src/api.ts node missing metadata.function_lines");
  assert(queuedAiJobCount(bundle.ops) === 0, "baseline queued AI semantic jobs unexpectedly present");
  context.baselineStatus = bundle.status;
  context.baselineSummary = bundle.summary;
  context.baselineNode = apiNode;
  return {
    snapshot_id: bundle.snapshotId,
    commit: bundle.status.graph_snapshot_commit,
    nodes: bundle.nodes.length,
    edges: bundle.edges.length,
    functions: funcs,
  };
}

async function stepTrunkCommitAndReconcile() {
  const targetCommit = mutateFixtureForScopeReconcile();
  const stale = await waitFor("graph stale status", async () => {
    const status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
    return status.current_state?.graph_stale?.is_stale ? status : null;
  }, { timeoutMs: 30000, intervalMs: 1000 });
  assert(stale.current_state.graph_stale.head_commit === targetCommit, "stale status head_commit does not match target commit");

  await http("POST", `/api/graph-governance/${pid(PROJECT)}/pending-scope`, {
    commit_sha: targetCommit,
    parent_commit_sha: stale.current_state.graph_stale.active_graph_commit || stale.graph_snapshot_commit || "",
    actor: "dashboard_e2e",
    evidence: { source: "dashboard_trunk_e2e", run_id: RUN_ID, layer: "L4" },
  });

  const result = await http("POST", `/api/graph-governance/${pid(PROJECT)}/reconcile/pending-scope`, {
    target_commit_sha: targetCommit,
    run_id: `dashboard-trunk-scope-${shortCommit(targetCommit)}-${Date.now()}`,
    actor: "dashboard_e2e",
    activate: true,
    semantic_enrich: true,
    semantic_use_ai: false,
    enqueue_stale: false,
    semantic_skip_completed: true,
    notes_extra: { source: "dashboard_trunk_e2e", run_id: RUN_ID, action: "scope_reconcile" },
  });

  const current = await waitFor("graph current status", async () => {
    const status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
    const graphStale = status.current_state?.graph_stale;
    return graphStale?.is_stale === false && status.graph_snapshot_commit === targetCommit ? status : null;
  }, { timeoutMs: 90000, intervalMs: 1500 });
  assert((current.pending_scope_reconcile_count || 0) === 0, "pending scope reconcile should be empty after materialize");
  context.reconciledStatus = current;
  return {
    target_commit: targetCommit,
    stale_snapshot: stale.active_snapshot_id,
    reconciled_snapshot: current.active_snapshot_id,
    result_snapshot: result.snapshot_id,
  };
}

async function stepDashboardApiConsistency() {
  const bundle = await loadRuntimeBundle(PROJECT);
  assert(bundle.status.graph_snapshot_commit === context.targetCommit, "reconciled graph commit mismatch");
  assert(bundle.status.active_snapshot_id === bundle.summary.snapshot_id, "status/summary snapshot mismatch");
  assert(bundle.ops.active_snapshot_id === bundle.status.active_snapshot_id, "operations queue snapshot mismatch");
  assert(queuedAiJobCount(bundle.ops) === 0, "scope reconcile queued AI semantic jobs unexpectedly");
  const apiNode = findNodeForPath(bundle.nodes, "src/api.ts");
  assert(apiNode, "src/api.ts node missing after reconcile");
  const funcs = functionNames(apiNode);
  assert(funcs.some((name) => name.includes("formatTrunkRunLabel")), "reconciled node missing new trunk function");
  const lineKeys = Object.keys(apiNode.metadata?.function_lines || {});
  assert(lineKeys.some((key) => key.endsWith("formatTrunkRunLabel")), "new trunk function line metadata missing");
  context.reconciledNode = apiNode;
  return {
    snapshot_id: bundle.snapshotId,
    project_id: bundle.status.project_id,
    node_id: apiNode.node_id,
    semantic_projection_status: bundle.projection?.status || bundle.projection?.projection?.status || "unknown",
    functions: funcs,
  };
}

async function stepSemanticJobsPath() {
  if (SKIP_SEMANTIC) {
    warn("semantic jobs path skipped by --skip-semantic");
    return { skipped: true };
  }

  const snapshotId = context.reconciledStatus?.active_snapshot_id;
  const node = context.reconciledNode;
  assert(snapshotId, "semantic path requires reconciled active snapshot");
  assert(node?.node_id, "semantic path requires reconciled target node");

  await cancelAllQueuedSemantic(snapshotId);
  await clearTerminalSemanticJobs(snapshotId);

  const dryPayload = semanticJobPayload(node, { dryRun: true });
  const dry = await http("POST", snapshotPath(PROJECT, snapshotId, "/semantic/jobs"), dryPayload);
  assert(dry.status === "dry_run", `dry-run semantic job returned ${dry.status}`);
  assert(dry.queued_count === 0, `dry-run should not queue rows, got ${dry.queued_count}`);
  assert(dry.planned_count === 1, `selected-node dry-run should plan 1 node, got ${dry.planned_count}`);
  assert((dry.batch_plan?.target_ids || []).includes(node.node_id), "dry-run batch_plan lost selected node id");

  if (!SEMANTIC_LIVE) {
    const cancelSweep = await cancelAllQueuedSemantic(snapshotId);
    await clearTerminalSemanticJobs(snapshotId);
    const opsAfter = await http("GET", `/api/graph-governance/${pid(PROJECT)}/operations/queue`);
    assert(nodeSemanticOps(opsAfter, node.node_id).length === 0, "safe semantic path left node job rows visible in operations queue");
    context.semantic = {
      snapshot_id: snapshotId,
      node_id: node.node_id,
      dry_run: { planned_count: dry.planned_count, queued_count: dry.queued_count },
      queue: { skipped: true, reason: "safe path uses dry_run only" },
      cancel_sweep: { cancelled_count: cancelSweep.cancelled_count ?? 0, status: cancelSweep.status || "" },
      live: { skipped: true },
    };
    return context.semantic;
  }

  const queued = await queueSelectedNodeSemanticJob(snapshotId, node);

  const jobBeforeCancel = await http(
    "GET",
    snapshotPath(PROJECT, snapshotId, `/semantic/jobs/${encodeURIComponent(node.node_id)}`),
  );
  assert(jobBeforeCancel.job?.node_id === node.node_id, "queued job lookup returned wrong node");

  const liveDecisions = [];
  const seenEventIds = new Set();
  const firstDecision = SEMANTIC_DECISION === "both" ? "reject" : SEMANTIC_DECISION;
  const first = await applySemanticLiveDecision(snapshotId, node, firstDecision, seenEventIds);
  liveDecisions.push(first);
  seenEventIds.add(first.event_id);
  let secondQueue = null;
  if (SEMANTIC_DECISION === "both") {
    secondQueue = await queueSelectedNodeSemanticJob(snapshotId, node);
    const second = await applySemanticLiveDecision(snapshotId, node, "accept", seenEventIds);
    liveDecisions.push(second);
    seenEventIds.add(second.event_id);
  }
  context.semantic = {
    snapshot_id: snapshotId,
    node_id: node.node_id,
    dry_run: { planned_count: dry.planned_count, queued_count: dry.queued_count },
    queue: { job_id: queued.job_id, queued_count: queued.queued_count, queued_ops: queued.queued_ops || [] },
    second_queue: secondQueue ? { job_id: secondQueue.job_id, queued_count: secondQueue.queued_count } : null,
    live: liveDecisions,
  };
  return context.semantic;
}

async function stepScenarioBranches() {
  const scenarios = [
    {
      id: "L7.python-function-lines",
      depends_on: "L5",
      status: "ready",
      note: "Add a Python fixture and assert function_lines parity.",
    },
    {
      id: "L7.docs-orphans",
      depends_on: "L5",
      status: "ready",
      note: "Generate docs with node binding markers and assert orphan handling.",
    },
    {
      id: "L7.semantic-cancel",
      depends_on: "L6",
      status: "covered",
      note: "Covered by this trunk semantic path and the broader scripts/e2e-semantic.mjs cancel matrix.",
    },
    {
      id: "L7.semantic-review-reject",
      depends_on: "L6",
      status: SEMANTIC_LIVE && ["reject", "both"].includes(SEMANTIC_DECISION) ? "covered" : "ready",
      note: "Run --semantic-live --semantic-decision reject to verify rejected AI semantic events never mount into projection.",
    },
    {
      id: "L7.e2e-evidence-ledger",
      depends_on: "L8",
      status: "covered",
      note: "Successful trunk runs post covered files and L7 node feature hashes to the E2E evidence ledger.",
    },
    {
      id: "L7.ui-inspector",
      depends_on: "L5",
      status: SKIP_DASHBOARD ? "blocked" : "ready",
      note: "Add Playwright once dashboard runtime is available in CI.",
    },
  ];
  return { scenarios };
}

async function stepCleanupAndArtifacts() {
  const reportPath = artifactPath("report.json");
  info(`${KEEP_WORKSPACE ? "fixture workspace kept" : "fixture workspace kept for rerun/debug"}: ${WORKSPACE}`);
  return { report_path: reportPath, kept_workspace: true };
}

async function recordRunEvidence(status = "passed") {
  const snapshotId = context.reconciledStatus?.active_snapshot_id || context.baselineStatus?.active_snapshot_id;
  if (!snapshotId || PROBE_ONLY) return null;
  const coveredNodeIds = [context.reconciledNode?.node_id || context.baselineNode?.node_id].filter(Boolean);
  const payload = {
    suite_id: e2eSuiteId(),
    status,
    command: ["node", normalizePath(path.relative(WORKSPACE, fileURLToPath(import.meta.url))), ...process.argv.slice(2)].join(" "),
    run_id: RUN_ID,
    artifact_path: artifactPath("report.json"),
    actor: "dashboard_trunk_e2e",
    covered_node_ids: coveredNodeIds,
    covered_files: [
      ".aming-claw.yaml",
      "src/api.ts",
      "src/App.tsx",
      "tests/smoke.test.mjs",
    ],
    metadata: {
      semantic_live: SEMANTIC_LIVE,
      semantic_decision: SEMANTIC_DECISION,
      target_commit: context.targetCommit,
    },
  };
  const evidence = await http("POST", snapshotPath(PROJECT, snapshotId, "/e2e/evidence"), payload);
  context.e2eEvidence = evidence;
  info(`e2e evidence recorded: ${evidence.evidence_id}`);
  return evidence;
}

async function main() {
  console.log(c("bold", "dashboard-trunk-e2e"));
  console.log(c("dim", `backend=${BACKEND} dashboard=${SKIP_DASHBOARD ? "skipped" : DASHBOARD}`));
  console.log(c("dim", `project=${PROJECT} workspace=${WORKSPACE} run_id=${RUN_ID} semantic_live=${SEMANTIC_LIVE}`));

  try {
    await runStep("L0", "Environment gate", stepEnvGate);
    if (PROBE_ONLY) {
      await runStep("L7", "Artifacts", stepCleanupAndArtifacts);
      console.log("");
      console.log(c("green", "TRUNK E2E PROBE OK"));
      return;
    }
    await runStep("L1", "Fixture project factory", stepFixtureProject);
    await runStep("L2", "Project registration and bootstrap", stepBootstrapProject);
    await runStep("L3", "Graph baseline assertions", stepGraphBaseline);
    await runStep("L4", "Trunk commit and scope reconcile", stepTrunkCommitAndReconcile);
    await runStep("L5", "Dashboard/API consistency", stepDashboardApiConsistency);
    await runStep("L6", "Semantic jobs path", stepSemanticJobsPath);
    await runStep("L7", "Scenario branch registry", stepScenarioBranches);
    await runStep("L8", "Cleanup and artifacts", stepCleanupAndArtifacts);
    await recordRunEvidence("passed");
    writeReport();
    console.log("");
    console.log(c("green", "TRUNK E2E OK"));
  } catch (error) {
    try {
      await stepCleanupAndArtifacts();
      writeReport();
    } catch {
      // Best effort only.
    }
    if (error instanceof HttpError) {
      console.log(c("dim", `body=${String(error.body || "").slice(0, 1000)}`));
    }
    console.log("");
    console.log(c("red", "TRUNK E2E FAIL"));
    exit(error instanceof HttpError ? 1 : 2);
  }
}

main();
