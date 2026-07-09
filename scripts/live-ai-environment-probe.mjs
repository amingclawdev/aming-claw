#!/usr/bin/env node
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";

const DEFAULT_GOVERNANCE_URL = "http://127.0.0.1:40000";
const DEFAULT_PROJECT_ID = "aming-claw";
const DEFAULT_ROLE = "tester";
const DEFAULT_FETCH_TIMEOUT_MS = 5000;
const DEFAULT_VERSION_TIMEOUT_MS = 3000;
const DEFAULT_LIVE_TIMEOUT_MS = 15000;
const SECRET_ENV_KEYS = new Set([
  "ANTHROPIC_API_KEY",
  "CLAUDECODE",
  "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
  "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
  "CLAUDE_CODE_ENTRYPOINT",
  "CLAUDE_CODE_EXECPATH",
  "CLAUDE_CODE_OAUTH_TOKEN",
  "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
  "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
  "CLAUDE_CODE_SSE_PORT",
  "CODEX_API_KEY",
  "OPENAI_API_KEY",
]);

function nowIso() {
  return new Date().toISOString();
}

function compactTimestamp(iso) {
  return iso.replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function parseArgs(argv) {
  const options = {
    projectId: process.env.AMING_CLAW_PROJECT_ID || DEFAULT_PROJECT_ID,
    role: DEFAULT_ROLE,
    governanceUrl: process.env.AMING_CLAW_GOVERNANCE_URL || DEFAULT_GOVERNANCE_URL,
    allowLiveAi: false,
    timeoutMs: DEFAULT_LIVE_TIMEOUT_MS,
    fetchTimeoutMs: DEFAULT_FETCH_TIMEOUT_MS,
    versionTimeoutMs: DEFAULT_VERSION_TIMEOUT_MS,
    cwd: process.cwd(),
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) {
      if (options.projectId === DEFAULT_PROJECT_ID) options.projectId = arg;
      else if (options.role === DEFAULT_ROLE) options.role = arg;
      continue;
    }
    const [rawKey, inlineValue] = arg.slice(2).split(/=(.*)/s, 2);
    const key = rawKey.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
    const boolKeys = new Set(["allowLiveAi", "json"]);
    if (boolKeys.has(key)) {
      options[key] = inlineValue === undefined ? true : inlineValue !== "false";
      continue;
    }
    const value = inlineValue !== undefined ? inlineValue : argv[++i];
    if (!value || value.startsWith("--")) {
      throw new Error(`--${rawKey} requires a value`);
    }
    if (key === "projectId") options.projectId = value;
    else if (key === "role" || key === "route") options.role = value;
    else if (key === "backend" || key === "governanceUrl") options.governanceUrl = value.replace(/\/+$/, "");
    else if (key === "timeoutMs") options.timeoutMs = positiveInt(value, DEFAULT_LIVE_TIMEOUT_MS);
    else if (key === "fetchTimeoutMs") options.fetchTimeoutMs = positiveInt(value, DEFAULT_FETCH_TIMEOUT_MS);
    else if (key === "versionTimeoutMs") options.versionTimeoutMs = positiveInt(value, DEFAULT_VERSION_TIMEOUT_MS);
    else if (key === "cwd") options.cwd = resolve(value);
    else options[key] = value;
  }

  options.projectId = String(options.projectId || DEFAULT_PROJECT_ID).trim();
  options.role = normalizeRole(options.role || DEFAULT_ROLE);
  options.governanceUrl = String(options.governanceUrl || DEFAULT_GOVERNANCE_URL).replace(/\/+$/, "");
  return options;
}

function positiveInt(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function normalizeRole(value) {
  const role = String(value || "").trim().toLowerCase();
  if (role === "test") return "tester";
  return role || DEFAULT_ROLE;
}

function sanitizeText(value) {
  return String(value || "")
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]")
    .replace(/([?&](?:token|key|secret|password|api_key|session)[^=]*=)[^&\s]+/gi, "$1[REDACTED]")
    .replace(/\b(token|secret|password|api[_-]?key|session[_-]?token)(["':=\s]+)[^\s"',}]+/gi, "$1$2[REDACTED]")
    .replace(/\b(sk-[A-Za-z0-9_-]{12,})\b/g, "[REDACTED]")
    .replace(/\b([A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})\b/g, "[REDACTED]");
}

function sanitizeArg(value) {
  const text = sanitizeText(value);
  if (/^--?(token|secret|password|api[-_]?key|session[-_]?token)(=|$)/i.test(text)) {
    const [key] = text.split("=", 1);
    return `${key}=[REDACTED]`;
  }
  return text;
}

function sanitizeCommand(command) {
  return command.map((part) => sanitizeArg(String(part)));
}

function sanitizeUrl(url) {
  try {
    const parsed = new URL(url);
    parsed.search = "";
    return parsed.toString();
  } catch {
    return sanitizeText(url);
  }
}

function sha256(value) {
  return crypto.createHash("sha256").update(String(value || "")).digest("hex");
}

function check(id, status, details = {}) {
  return {
    id,
    status,
    ...details,
  };
}

function runIdFor(projectId, role, startedAt) {
  const seed = `${projectId}:${role}:${startedAt}:${process.pid}`;
  const suffix = crypto.createHash("sha1").update(seed).digest("hex").slice(0, 8);
  return `${compactTimestamp(startedAt)}-${projectId}-${role}-${suffix}`;
}

function baseReport(options) {
  const startedAt = nowIso();
  return {
    schema_version: 1,
    run_id: runIdFor(options.projectId, options.role, startedAt),
    project_id: options.projectId,
    role: options.role,
    expected: {
      provider: "",
      model: "",
      source: "",
    },
    observed: {
      provider: "",
      runtime: "",
      command: "",
      path: "",
      source: "",
      status: "",
      version: "",
      auth_status: "",
      error: "",
    },
    evidence: {
      expected_provider: "",
      expected_model: "",
      expected_role: options.role,
      actual_provider: "",
      actual_model: "",
      catalog_membership: false,
      cli_path: "",
      cli_version: "",
      tool_health: "",
      auth_status: "unknown",
      invocation_status: "not_requested",
      sanitized_evidence: true,
      allow_live_ai: Boolean(options.allowLiveAi),
    },
    checks: [],
    invocation: {
      schema_version: "ai_invocation_result.v1",
      request_schema_version: "ai_invocation_request.v1",
      allowed: Boolean(options.allowLiveAi),
      attempted: false,
      role: options.role,
      provider: "",
      model: "",
      backend_mode: "",
      cwd: options.cwd,
      adapter: "",
      status: "not_requested",
      command: [],
      timeout_ms: options.timeoutMs,
      duration_ms: 0,
      exit_code: null,
      signal: "",
      auth_status: "unknown",
      auth_mode: "cli_auth",
      output_policy: "hash_and_summary_only",
      provider_backed: false,
      calls_models: false,
      prompt_sha256: "",
      output_sha256: "",
      stdout_sha256: "",
      stderr_sha256: "",
      raw_output_stored: false,
      no_raw_prompt_output: true,
      evidence_refs: [`project:${options.projectId}`, `role:${options.role}`],
      error: "",
    },
    http: {
      url: sanitizeUrl(`${options.governanceUrl}/api/projects/${encodeURIComponent(options.projectId)}/ai-config`),
      status_code: null,
      ok: false,
      duration_ms: 0,
      response_keys: [],
      error: "",
    },
    options: {
      governance_url: options.governanceUrl,
      allow_live_ai: Boolean(options.allowLiveAi),
      timeout_ms: options.timeoutMs,
      fetch_timeout_ms: options.fetchTimeoutMs,
      version_timeout_ms: options.versionTimeoutMs,
    },
    started_at: startedAt,
    completed_at: "",
    duration_ms: 0,
    status: "failed",
  };
}

function finishReport(report, status) {
  report.status = status;
  report.completed_at = nowIso();
  report.duration_ms = new Date(report.completed_at).getTime() - new Date(report.started_at).getTime();
  return report;
}

async function fetchJson(url, timeoutMs) {
  const started = Date.now();
  const summary = {
    url: sanitizeUrl(url),
    status_code: null,
    ok: false,
    duration_ms: 0,
    response_keys: [],
    error: "",
  };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal });
    const text = await response.text();
    let json = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch {
      json = null;
    }
    summary.status_code = response.status;
    summary.ok = response.ok;
    summary.response_keys = json && typeof json === "object" ? Object.keys(json).slice(0, 40) : [];
    if (!response.ok) summary.error = sanitizeText(json?.error || text || response.statusText);
    if (response.ok && (!json || typeof json !== "object")) {
      summary.ok = false;
      summary.error = "AI config response was not a JSON object";
    }
    return { ok: Boolean(summary.ok), json, summary };
  } catch (error) {
    summary.error = sanitizeText(error.message || String(error));
    return { ok: false, json: null, summary };
  } finally {
    clearTimeout(timeout);
    summary.duration_ms = Date.now() - started;
  }
}

function resolveExpectedRoute(config, role) {
  const routing = plainObject(config?.project_config?.ai?.routing);
  const roleRoute = plainObject(routing[role]);
  if (roleRoute.provider || roleRoute.model) {
    return {
      provider: String(roleRoute.provider || "").trim(),
      model: String(roleRoute.model || "").trim(),
      source: `project_config.ai.routing.${role}`,
    };
  }

  if (role === "semantic") {
    const semanticRoute = plainObject(config?.semantic);
    if (semanticRoute.provider || semanticRoute.model) {
      return {
        provider: String(semanticRoute.provider || "").trim(),
        model: String(semanticRoute.model || "").trim(),
        source: "semantic",
      };
    }
  }

  const globalRoleRoute = plainObject(config?.role_routing?.[role]);
  if (globalRoleRoute.provider || globalRoleRoute.model) {
    return {
      provider: String(globalRoleRoute.provider || "").trim(),
      model: String(globalRoleRoute.model || "").trim(),
      source: globalRoleRoute.source ? `role_routing.${role}:${globalRoleRoute.source}` : `role_routing.${role}`,
    };
  }

  return {
    provider: "",
    model: "",
    source: "",
  };
}

function plainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value;
}

function evaluateConfig(config, report) {
  const expected = resolveExpectedRoute(config, report.role);
  report.expected = expected;

  const catalog = plainObject(config?.model_catalog);
  const providers = plainObject(catalog.providers);
  const models = plainObject(catalog.models);
  const providerModels = Array.isArray(models[expected.provider]) ? models[expected.provider] : [];
  const toolHealth = plainObject(config?.tool_health);
  const observed = plainObject(toolHealth[expected.provider]);
  report.observed = {
    provider: String(observed.provider || expected.provider || ""),
    runtime: String(observed.runtime || providers[expected.provider]?.runtime || ""),
    command: String(observed.command || providers[expected.provider]?.command || ""),
    path: String(observed.path || ""),
    source: String(observed.source || ""),
    status: String(observed.status || ""),
    version: sanitizeText(observed.version || ""),
    auth_status: String(observed.auth_status || "unknown"),
    error: sanitizeText(observed.error || ""),
  };

  if (!expected.provider || !expected.model) {
    report.checks.push(check("route_configured", "blocked", {
      expected: `${report.role} provider/model`,
      observed: expected.provider || expected.model ? `${expected.provider}/${expected.model}` : "",
      reason: `No provider/model route is configured for role ${report.role}.`,
    }));
  } else {
    report.checks.push(check("route_configured", "passed", {
      expected: `${report.role} provider/model`,
      observed: `${expected.provider}/${expected.model}`,
      source: expected.source,
    }));
  }

  if (!expected.provider) {
    report.checks.push(check("provider_catalog", "blocked", {
      expected: "configured provider",
      observed: "",
      reason: "Provider is missing, so catalog membership cannot be checked.",
    }));
  } else if (!providers[expected.provider]) {
    report.checks.push(check("provider_catalog", "blocked", {
      expected: expected.provider,
      observed: Object.keys(providers).sort(),
      reason: `Provider ${expected.provider} is absent from model_catalog.providers.`,
    }));
  } else {
    report.checks.push(check("provider_catalog", "passed", {
      expected: expected.provider,
      observed: expected.provider,
      runtime: providers[expected.provider]?.runtime || "",
    }));
  }

  if (!expected.model) {
    report.checks.push(check("model_catalog", "blocked", {
      expected: "configured model",
      observed: "",
      reason: "Model is missing, so model catalog membership cannot be checked.",
    }));
  } else if (!providerModels.includes(expected.model)) {
    report.checks.push(check("model_catalog", "blocked", {
      expected: expected.model,
      observed: providerModels,
      reason: `Model ${expected.model} is not listed for provider ${expected.provider}.`,
    }));
  } else {
    report.checks.push(check("model_catalog", "passed", {
      expected: expected.model,
      observed: expected.model,
    }));
  }

  if (!expected.provider) {
    report.checks.push(check("tool_health", "blocked", {
      expected: "provider tool",
      observed: "",
      reason: "Provider is missing, so tool health cannot be checked.",
    }));
  } else if (!observed || !Object.keys(observed).length) {
    report.checks.push(check("tool_health", "blocked", {
      expected: expected.provider,
      observed: "",
      reason: `No tool_health entry exists for provider ${expected.provider}.`,
    }));
  } else if (observed.status !== "detected") {
    report.checks.push(check("tool_health", "blocked", {
      expected: "detected",
      observed: String(observed.status || "unknown"),
      path: String(observed.path || ""),
      error: sanitizeText(observed.error || ""),
      reason: `${expected.provider} runtime is not detected.`,
    }));
  } else {
    report.checks.push(check("tool_health", "passed", {
      expected: "detected",
      observed: observed.status,
      path: String(observed.path || ""),
      version: sanitizeText(observed.version || ""),
      auth_status: String(observed.auth_status || "unknown"),
    }));
  }

  const adapter = adapterForProvider(expected.provider);
  if (!adapter) {
    report.checks.push(check("provider_adapter", "blocked", {
      expected: "openai or anthropic",
      observed: expected.provider,
      reason: `No live invocation adapter is implemented for provider ${expected.provider || "(missing)"}.`,
    }));
  } else {
    report.checks.push(check("provider_adapter", "passed", {
      expected: expected.provider,
      observed: adapter.name,
    }));
  }
}

function adapterForProvider(provider) {
  if (provider === "openai") return openaiCodexAdapter;
  if (provider === "anthropic") return anthropicClaudeAdapter;
  return null;
}

function providerEnv(_provider) {
  // Preserve the user's real AI runtime environment for the child process.
  // Secrets and raw provider output are never serialized into the report.
  return { ...process.env };
}

function spawnCapture(command, args, options) {
  const summary = {
    command: sanitizeCommand([command, ...args]),
    timeout_ms: options.timeoutMs,
    duration_ms: 0,
    exit_code: null,
    signal: "",
    stdout_sha256: "",
    stderr_sha256: "",
    output_sha256: "",
    status: "running",
    error: "",
  };
  const started = Date.now();
  return new Promise((resolvePromise) => {
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const child = spawn(command, args, {
      cwd: options.cwd || process.cwd(),
      stdio: ["pipe", "pipe", "pipe"],
      env: options.env || process.env,
    });
    const timeout = setTimeout(() => {
      timedOut = true;
      summary.status = "timed_out";
      child.kill("SIGTERM");
    }, options.timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
      if (stdout.length > 20000) stdout = stdout.slice(stdout.length - 20000);
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
      if (stderr.length > 20000) stderr = stderr.slice(stderr.length - 20000);
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      summary.status = "failed";
      summary.error = sanitizeText(error.message || String(error));
      summary.duration_ms = Date.now() - started;
      resolvePromise(summary);
    });
    child.on("close", (code, signal) => {
      clearTimeout(timeout);
      summary.exit_code = code;
      summary.signal = signal || "";
      if (!timedOut) summary.status = code === 0 ? "passed" : "failed";
      summary.duration_ms = Date.now() - started;
      summary.stdout_sha256 = `sha256:${sha256(stdout)}`;
      summary.stderr_sha256 = `sha256:${sha256(stderr)}`;
      summary.output_sha256 = summary.stdout_sha256;
      resolvePromise(summary);
    });
    if (options.input) child.stdin.end(options.input);
    else child.stdin.end();
  });
}

function versionProbe(command, timeoutMs) {
  const result = spawnSync(command, ["--version"], {
    encoding: "utf8",
    timeout: timeoutMs,
    stdio: "pipe",
  });
  return {
    ok: !result.error && result.status === 0,
    status: result.error ? "failed" : result.status === 0 ? "passed" : "failed",
    exit_code: result.status,
    error: sanitizeText(result.error?.message || ""),
    version: sanitizeText(`${result.stdout || result.stderr || ""}`.trim().split(/\r?\n/, 1)[0] || ""),
  };
}

const openaiCodexAdapter = {
  name: "codex-cli",
  provider: "openai",
  async invoke({ expected, observed, options }) {
    const prompt = 'Return exactly {"ok":true,"provider":"openai"} and do not inspect or modify files.';
    const tempDir = mkdtempSync(join(tmpdir(), "aming-claw-live-ai-probe-"));
    const outputPath = join(tempDir, "codex-last-message.txt");
    const command = observed.path || observed.command || "codex";
    const args = [
      "exec",
      "--skip-git-repo-check",
      "--ephemeral",
      "-C",
      options.cwd,
      "--sandbox",
      "read-only",
      "-o",
      outputPath,
    ];
    if (expected.model) args.push("--model", expected.model);
    args.push("-");
    try {
      const result = await spawnCapture(command, args, {
        cwd: options.cwd,
        env: providerEnv("openai"),
        input: prompt,
        timeoutMs: options.timeoutMs,
      });
      let lastMessage = "";
      try {
        lastMessage = readFileSync(outputPath, "utf8");
      } catch {
        lastMessage = "";
      }
      const outputSha256 = lastMessage
        ? `sha256:${sha256(lastMessage)}`
        : result.output_sha256;
      return {
        ...result,
        output_sha256: outputSha256,
        adapter: openaiCodexAdapter.name,
        provider: openaiCodexAdapter.provider,
        backend_mode: "codex_cli",
        auth_status: result.status === "passed" ? "live_ok" : "live_failed",
        prompt_sha256: `sha256:${sha256(prompt)}`,
      };
    } finally {
      rmSync(tempDir, { recursive: true, force: true });
    }
  },
};

const anthropicClaudeAdapter = {
  name: "claude-code-cli",
  provider: "anthropic",
  async invoke({ expected, observed, options }) {
    const prompt = 'Return exactly {"ok":true,"provider":"anthropic"} and do not inspect or modify files.';
    const command = observed.path || observed.command || "claude";
    const args = [
      "-p",
      "--output-format",
      "json",
      "--permission-mode",
      "dontAsk",
      "--tools",
      "",
      "--max-budget-usd",
      "0.10",
    ];
    if (expected.model) args.push("--model", expected.model);
    const result = await spawnCapture(command, args, {
      cwd: options.cwd,
      env: providerEnv("anthropic"),
      input: prompt,
      timeoutMs: options.timeoutMs,
    });
    return {
      ...result,
      adapter: anthropicClaudeAdapter.name,
      provider: anthropicClaudeAdapter.provider,
      backend_mode: "claude_cli",
      auth_status: result.status === "passed" ? "live_ok" : "live_failed",
      prompt_sha256: `sha256:${sha256(prompt)}`,
    };
  },
};

async function maybeInvoke(report, options) {
  const adapter = adapterForProvider(report.expected.provider);
  report.invocation.provider = report.expected.provider;
  report.invocation.model = report.expected.model;
  report.invocation.adapter = adapter?.name || "";
  report.invocation.backend_mode = report.expected.provider === "openai" ? "codex_cli" : "claude_cli";
  report.invocation.timeout_ms = options.timeoutMs;

  const blockingChecks = report.checks.filter((item) => item.status !== "passed");
  const blocksBeforeInvocation = blockingChecks.filter((item) => item.id !== "invocation_allowed");
  if (blocksBeforeInvocation.length) {
    report.invocation.status = "blocked";
    report.invocation.error = sanitizeText(
      `Skipping live invocation because prerequisite checks did not pass: ${blocksBeforeInvocation.map((item) => item.id).join(", ")}`,
    );
    return;
  }

  if (!options.allowLiveAi) {
    report.checks.push(check("invocation_allowed", "blocked", {
      expected: "--allow-live-ai",
      observed: false,
      reason: "Live provider invocation is explicit and was not requested.",
    }));
    report.invocation.status = "blocked";
    report.invocation.error = "Live provider invocation requires --allow-live-ai.";
    return;
  }

  report.checks.push(check("invocation_allowed", "passed", {
    expected: "--allow-live-ai",
    observed: true,
  }));
  report.invocation.attempted = true;
  const result = await adapter.invoke({
    expected: report.expected,
    observed: report.observed,
    options,
  });
  report.invocation = {
    ...report.invocation,
    attempted: true,
    provider: result.provider,
    adapter: result.adapter,
    role: options.role,
    model: report.expected.model,
    backend_mode: result.backend_mode,
    status: result.status,
    command: result.command,
    timeout_ms: result.timeout_ms,
    duration_ms: result.duration_ms,
    exit_code: result.exit_code,
    signal: result.signal,
    auth_status: result.auth_status,
    auth_mode: "cli_auth",
    output_policy: "hash_and_summary_only",
    provider_backed: true,
    calls_models: result.status === "passed",
    prompt_sha256: result.prompt_sha256,
    output_sha256: result.output_sha256,
    stdout_sha256: result.stdout_sha256,
    stderr_sha256: result.stderr_sha256,
    raw_output_stored: false,
    no_raw_prompt_output: true,
    error: result.error,
  };
  report.observed.auth_status = result.auth_status;
}

function finalStatus(report) {
  if (!report.http.ok) return "failed";
  if (report.invocation.attempted && report.invocation.status !== "passed") return "failed";
  if (report.checks.some((item) => item.status !== "passed")) return "blocked";
  if (report.invocation.status !== "passed") return "blocked";
  return "passed";
}

function updateEvidence(report) {
  const modelCatalog = report.checks.find((item) => item.id === "model_catalog");
  report.evidence = {
    expected_provider: report.expected.provider,
    expected_model: report.expected.model,
    expected_role: report.role,
    actual_provider: report.observed.provider,
    actual_model: report.expected.model,
    catalog_membership: modelCatalog?.status === "passed",
    cli_path: report.observed.path,
    cli_version: report.observed.version,
    tool_health: report.observed.status,
    auth_status: report.observed.auth_status || report.invocation.auth_status || "unknown",
    invocation_status: report.invocation.status,
    sanitized_evidence: true,
    allow_live_ai: Boolean(report.options.allow_live_ai),
  };
}

async function run() {
  const options = parseArgs(process.argv.slice(2));
  const report = baseReport(options);
  const url = `${options.governanceUrl}/api/projects/${encodeURIComponent(options.projectId)}/ai-config`;
  const fetched = await fetchJson(url, options.fetchTimeoutMs);
  report.http = fetched.summary;

  if (!fetched.ok) {
    report.checks.push(check("fetch_ai_config", "failed", {
      expected: "HTTP 2xx JSON object",
      observed: fetched.summary.status_code || 0,
      reason: fetched.summary.error || "Unable to fetch project AI config.",
    }));
    return finishReport(report, "failed");
  }

  report.checks.push(check("fetch_ai_config", "passed", {
    expected: "HTTP 2xx JSON object",
    observed: fetched.summary.status_code,
  }));
  evaluateConfig(fetched.json, report);

  const versionCommand = report.observed.path || report.observed.command;
  if (versionCommand) {
    const version = versionProbe(versionCommand, options.versionTimeoutMs);
    if (version.ok) {
      report.checks.push(check("local_version_probe", "passed", {
        expected: `${versionCommand} --version`,
        observed: version.version,
      }));
      report.observed.version = report.observed.version || version.version;
    } else {
      report.checks.push(check("local_version_probe", "blocked", {
        expected: `${versionCommand} --version`,
        observed: version.exit_code,
        error: version.error,
        reason: "Detected tool path did not pass a local version probe.",
      }));
    }
  }

  await maybeInvoke(report, options);
  updateEvidence(report);
  return finishReport(report, finalStatus(report));
}

run()
  .then((report) => {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    process.exitCode = report.status === "passed" ? 0 : report.status === "blocked" ? 2 : 1;
  })
  .catch((error) => {
    const options = (() => {
      try {
        return parseArgs(process.argv.slice(2));
      } catch {
        return {
          projectId: DEFAULT_PROJECT_ID,
          role: DEFAULT_ROLE,
          governanceUrl: DEFAULT_GOVERNANCE_URL,
          allowLiveAi: false,
          timeoutMs: DEFAULT_LIVE_TIMEOUT_MS,
          fetchTimeoutMs: DEFAULT_FETCH_TIMEOUT_MS,
          versionTimeoutMs: DEFAULT_VERSION_TIMEOUT_MS,
        };
      }
    })();
    const report = finishReport(baseReport(options), "failed");
    report.checks.push(check("script_error", "failed", {
      reason: sanitizeText(error.message || String(error)),
    }));
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    process.exitCode = 1;
  });
