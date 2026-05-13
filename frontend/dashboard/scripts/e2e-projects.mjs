#!/usr/bin/env node
// dashboard-projects-e2e
//
// Read-only by default. Verifies that the dashboard project console can work
// against an isolated example project without mutating the main aming-claw graph.
//
//   node scripts/e2e-projects.mjs
//   node scripts/e2e-projects.mjs --project dashboard-e2e-demo
//   node scripts/e2e-projects.mjs --apply   # bootstrap/build missing example graph

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { exit } from "node:process";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const DEFAULT_PROJECT = "dashboard-e2e-demo";
const DEFAULT_PARENT = "aming-claw";
const DEFAULT_WORKSPACE = path.join(REPO_ROOT, "examples", DEFAULT_PROJECT);

const FLAGS = parseFlags(process.argv.slice(2));
const BACKEND = FLAGS.backend || process.env.VITE_BACKEND_URL || "http://localhost:40000";
const PROJECT = FLAGS.project || process.env.VITE_PROJECT_ID || DEFAULT_PROJECT;
const PARENT_PROJECT = FLAGS.parent || DEFAULT_PARENT;
const WORKSPACE = path.resolve(FLAGS.workspace || DEFAULT_WORKSPACE);
const APPLY = FLAGS.apply === true;
const SKIP_PARENT = FLAGS["skip-parent-isolation"] === true;

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
const phase = (text) => console.log(`\n${c("cyan", "phase")} ${c("bold", text)}`);
const ok = (text) => console.log(`  ${c("green", "ok")} ${text}`);
const warn = (text) => console.log(`  ${c("yellow", "warn")} ${text}`);
const fail = (text) => console.log(`  ${c("red", "fail")} ${text}`);
const info = (text) => console.log(`  ${c("dim", text)}`);

function parseFlags(args) {
  const bool = new Set(["apply", "skip-parent-isolation"]);
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
  constructor(method, url, status, body, request) {
    super(`${method} ${url} -> ${status}`);
    this.method = method;
    this.url = url;
    this.status = status;
    this.body = body;
    this.request = request;
  }
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

function pid(projectId) {
  return encodeURIComponent(projectId);
}

function snapshotPath(projectId, snapshotId, suffix) {
  return `/api/graph-governance/${pid(projectId)}/snapshots/${encodeURIComponent(snapshotId)}${suffix}`;
}

function activePath(projectId, suffix) {
  return `/api/graph-governance/${pid(projectId)}/snapshots/active${suffix}`;
}

function shortCommit(commit) {
  if (!commit) return "-";
  return commit.length > 10 ? commit.slice(0, 7) : commit;
}

function allNodePaths(node) {
  return [
    ...(node.primary_files || []),
    ...(node.secondary_files || []),
    ...(node.test_files || []),
    ...(node.metadata?.config_files || []),
  ].map((item) => String(item).replaceAll("\\", "/"));
}

function relativeWorkspace() {
  return path.relative(REPO_ROOT, WORKSPACE).replaceAll("\\", "/");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function ensureProjectRegistered() {
  phase("project registry");
  const projects = await http("GET", "/api/projects");
  assert(Array.isArray(projects.projects), "/api/projects did not return projects[]");
  const project = projects.projects.find((row) => row.project_id === PROJECT);
  if (!project) {
    if (!APPLY) {
      throw new Error(
        `Project ${PROJECT} is not registered. Re-run with --apply to bootstrap ${WORKSPACE}.`,
      );
    }
    return bootstrapProject();
  }
  ok(`${PROJECT} registered`);
  info(`workspace=${project.workspace_path || "(empty)"} snapshot=${project.active_snapshot_id || "-"}`);
  return project;
}

async function bootstrapProject() {
  phase("bootstrap project (--apply)");
  assert(existsSync(WORKSPACE), `workspace does not exist: ${WORKSPACE}`);
  const result = await http("POST", "/api/project/bootstrap", {
    workspace_path: WORKSPACE,
    project_name: PROJECT,
    scan_depth: 3,
  });
  ok(`bootstrapped ${result.project_id || PROJECT} snapshot=${result.snapshot_id || "-"}`);
  return {
    project_id: result.project_id || PROJECT,
    workspace_path: WORKSPACE,
    active_snapshot_id: result.snapshot_id,
  };
}

async function verifyProjectConfig() {
  phase("project config");
  const [config, aiConfig, refs] = await Promise.all([
    http("GET", `/api/projects/${pid(PROJECT)}/config`),
    http("GET", `/api/projects/${pid(PROJECT)}/ai-config`),
    http("GET", `/api/projects/${pid(PROJECT)}/git-refs`),
  ]);
  assert(config.project_id === PROJECT, `config project_id mismatch: ${config.project_id}`);
  const language = String(config.language || "").toLowerCase();
  assert(
    language.includes("type") || language === "mixed",
    `expected typescript or mixed project config, got ${config.language || "(empty)"}`,
  );
  const excludes = [
    ...(config.graph?.exclude_paths || []),
    ...(config.graph?.ignore_globs || []),
    ...(config.graph?.effective_exclude_roots || []),
  ].join(" ");
  assert(excludes.includes("node_modules"), "project config should exclude node_modules");
  assert(aiConfig.project_id === PROJECT, "ai-config project_id mismatch");
  ok(`config loaded language=${config.language}`);
  ok(`ai semantic route=${aiConfig.semantic?.provider || "-"} / ${aiConfig.semantic?.model || "-"}`);
  ok(`git refs loaded repo=${Boolean(refs.is_git_repo)} ref=${refs.selected_ref || refs.current_branch || "-"}`);
  return { config, aiConfig, refs };
}

async function verifyGraphRuntime(project) {
  phase("graph runtime");
  let status = null;
  try {
    status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  } catch (error) {
    if (!APPLY) throw error;
    warn(`status missing (${error.message}); bootstrapping project again`);
    await bootstrapProject();
    status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  }
  if (!status.active_snapshot_id && APPLY) {
    await buildFullGraph();
    status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  }
  assert(status.active_snapshot_id, "active_snapshot_id is missing");
  const summary = await http("GET", activePath(PROJECT, "/summary"));
  const ops = await http("GET", `/api/graph-governance/${pid(PROJECT)}/operations/queue`);
  const nodes = await http("GET", snapshotPath(PROJECT, status.active_snapshot_id, "/nodes?include_semantic=true&limit=1000"));
  const edges = await http("GET", snapshotPath(PROJECT, status.active_snapshot_id, "/edges?limit=4000"));

  assert((summary.counts?.features || summary.health?.semantic_health?.feature_count || 0) > 0, "summary has no features");
  assert(Array.isArray(nodes.nodes) && nodes.nodes.length > 0, "nodes[] is empty");
  assert(Array.isArray(edges.edges), "edges[] missing");

  const rel = relativeWorkspace();
  const inspectable = nodes.nodes.find((node) => {
    const paths = allNodePaths(node);
    const isL7 = node.layer === "L7";
    const hasPrimary = (node.primary_files || []).length > 0;
    const hasFunctions = Number(node.metadata?.function_count || 0) > 0;
    const staysRelativeToExample =
      paths.every((item) => !item.startsWith("..")) &&
      paths.every((item) => !item.includes(`${rel}/`));
    return isL7 && hasPrimary && hasFunctions && staysRelativeToExample;
  });
  assert(inspectable, "no inspectable L7 node with functions found in example graph");

  const stale = status.current_state?.graph_stale;
  ok(`snapshot=${status.active_snapshot_id} commit=${shortCommit(status.graph_snapshot_commit)}`);
  ok(`counts nodes=${summary.counts?.nodes ?? nodes.count} features=${summary.counts?.features ?? "-"}`);
  ok(`inspectable node=${inspectable.node_id} ${inspectable.title}`);
  ok(`operations queue count=${ops.count}`);
  if (stale?.is_stale) {
    warn(`example graph stale: ${shortCommit(stale.active_graph_commit)} -> ${shortCommit(stale.head_commit)}`);
  } else {
    ok("example graph is current for its workspace");
  }
  return { status, summary, ops, nodes, edges, inspectable, project };
}

async function buildFullGraph() {
  phase("build graph (--apply)");
  const result = await http("POST", `/api/graph-governance/${pid(PROJECT)}/reconcile/full`, {
    run_id: `dashboard-projects-e2e-full-${Date.now()}`,
    actor: "dashboard_e2e",
    activate: true,
    semantic_enrich: true,
    semantic_use_ai: false,
    enqueue_stale: false,
    semantic_skip_completed: true,
    notes_extra: { source: "dashboard_projects_e2e", action: "build_graph" },
  });
  ok(`full reconcile snapshot=${result.snapshot_id || result.activation?.snapshot_id || "-"}`);
  return result;
}

async function verifyParentIsolation() {
  if (SKIP_PARENT) {
    warn("parent isolation skipped");
    return;
  }
  phase("parent graph isolation");
  const rootConfigPath = path.join(REPO_ROOT, ".aming-claw.yaml");
  assert(existsSync(rootConfigPath), `root .aming-claw.yaml missing: ${rootConfigPath}`);
  const rootConfig = readFileSync(rootConfigPath, "utf8");
  assert(rootConfig.includes("examples"), "root .aming-claw.yaml should exclude examples");
  ok("root config excludes examples");

  const parentStatus = await http("GET", `/api/graph-governance/${pid(PARENT_PROJECT)}/status`);
  assert(parentStatus.active_snapshot_id, `${PARENT_PROJECT} active snapshot missing`);
  const parentNodes = await http(
    "GET",
    snapshotPath(PARENT_PROJECT, parentStatus.active_snapshot_id, "/nodes?include_semantic=false&limit=3000"),
  );
  const rel = relativeWorkspace();
  const hits = (parentNodes.nodes || []).filter((node) =>
    allNodePaths(node).some((item) => item.includes(rel)),
  );
  assert(hits.length === 0, `${PARENT_PROJECT} graph contains ${hits.length} ${PROJECT} path(s)`);
  ok(`${PARENT_PROJECT} active graph has 0 ${PROJECT} nodes`);
}

async function verifyProjectSwitchContract(runtime) {
  phase("project switch contract");
  const parentStatus = await http("GET", `/api/graph-governance/${pid(PARENT_PROJECT)}/status`);
  assert(parentStatus.active_snapshot_id, `${PARENT_PROJECT} active snapshot missing`);
  assert(parentStatus.project_id === PARENT_PROJECT, "parent status project_id mismatch");
  assert(runtime.status.project_id === PROJECT, "target status project_id mismatch");
  assert(parentStatus.active_snapshot_id !== runtime.status.active_snapshot_id, "project snapshots should be distinct");
  ok(`${PARENT_PROJECT} snapshot=${parentStatus.active_snapshot_id}`);
  ok(`${PROJECT} snapshot=${runtime.status.active_snapshot_id}`);
}

function verifyProjectImportUiContract() {
  phase("project import UI contract");
  const viewSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectConsoleView.tsx"), "utf8");
  const apiSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/lib/api.ts"), "utf8");
  assert(viewSource.includes('data-testid="project-import-directory"'), "Projects page import directory button is missing");
  assert(viewSource.includes("handleChooseDirectory"), "Projects page does not wire a directory picker handler");
  assert(apiSource.includes("/api/local/choose-directory"), "dashboard API client missing directory picker endpoint");
  ok("Projects page exposes import directory picker contract");
}

function verifyEditorJumpWorkspaceContract() {
  phase("editor jump workspace contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const editorSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/lib/editor.ts"), "utf8");
  const fileLinkSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/FileLink.tsx"), "utf8");
  const inspectorSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/InspectorDrawer.tsx"), "utf8");
  assert(appSource.includes("activeWorkspaceRoot"), "App does not derive the active project workspace root");
  assert(appSource.includes("workspaceRoot={activeWorkspaceRoot}"), "App does not pass workspace root into the inspector");
  assert(editorSource.includes("rootOverride"), "editorUrl does not accept a workspace root override");
  assert(fileLinkSource.includes("workspaceRoot?: string"), "FileLink cannot receive an active project workspace root");
  assert(inspectorSource.includes("workspaceRoot={workspaceRoot}"), "Inspector does not propagate workspace root to file/function links");
  ok("editor jump resolves through active project workspace contract");
}

async function main() {
  console.log(c("bold", "dashboard-projects-e2e"));
  console.log(c("dim", `backend=${BACKEND} project=${PROJECT} workspace=${WORKSPACE} apply=${APPLY}`));

  try {
    await http("GET", "/api/health");
    verifyProjectImportUiContract();
    verifyEditorJumpWorkspaceContract();
    const project = await ensureProjectRegistered();
    await verifyProjectConfig();
    const runtime = await verifyGraphRuntime(project);
    await verifyParentIsolation();
    await verifyProjectSwitchContract(runtime);
    console.log("");
    console.log(c("green", "ACCEPTANCE OK"));
  } catch (error) {
    console.log("");
    fail(error.message);
    if (error instanceof HttpError) {
      console.log(c("dim", `body=${String(error.body || "").slice(0, 1000)}`));
      if (!APPLY) {
        console.log(c("yellow", "This script is read-only by default. Use --apply only for isolated example bootstrap/build."));
      }
    }
    console.log(c("red", "ACCEPTANCE FAIL"));
    exit(error instanceof HttpError ? 1 : 2);
  }
}

main();
