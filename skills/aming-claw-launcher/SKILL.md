---
name: aming-claw-launcher
description: Use when a user wants to preview, start, or onboard onto Aming Claw — first-time setup, opening the dashboard, checking runtime status, or learning the basic CLI surface. Triggers on "preview aming-claw", "start aming-claw", "launcher", "open dashboard", "is governance running", "how do I run this", or any onboarding question. Defers to the main aming-claw skill for graph, backlog, manual-fix, semantic, or chain work.
---

# Aming Claw Launcher

Help the user start and verify Aming Claw locally. Never spawn governance silently — show the explicit command and let the user run it.

## Preview Flow

1. Write the local launcher artifact:

   ```text
   aming-claw launcher
   ```

   Writes `.aming-claw/aming-claw-launcher.html` with the dashboard link and start commands. Add `--open-browser` to open it in the default browser, or `--governance-url <url>` to target a non-default host. The launcher never starts services on its own.

2. Start governance in the foreground from a separate terminal/window
   (host-owned, no plugin-spawned workers):

   ```text
   aming-claw start
   ```

   This checks `GOVERNANCE_PORT` from `--port` (default `40000`) first. If
   Aming Claw governance is already healthy, it prints the dashboard URL and
   exits. If another process owns the port, it reports a conflict. Otherwise it
   runs `start_governance.main` as a long-running foreground service; do not run
   that path as a normal one-shot Codex tool call and wait for it to exit.
   ServiceManager is started independently (see project rules in `CLAUDE.md`);
   do not let the plugin session spawn executor workers.

3. Confirm health (CLI):

   ```text
   aming-claw status
   aming-claw plugin doctor --python <path-to-python-3.9-or-newer>
   ```

   Or, when MCP is available, prefer structured probes for a richer snapshot:

   - `runtime_status(project_id="<project_id>")` — governance + ServiceManager + version_check in one call.
   - `version_check` — HEAD vs CHAIN_VERSION + dirty files.
   - `graph_status` — active graph snapshot + stale check + semantic drift summary.
   - `health` — bare governance ping.

4. Check local AI runtime readiness for the selected project before promising
   AI Enrich or chain/executor work:

   - HTTP fallback: `GET /api/projects/{project_id}/ai-config`.
   - Inspect `tool_health.openai`, `tool_health.anthropic`,
     `project_config.ai.routing`, `semantic.use_ai_default`, and
     `model_catalog`.
   - `openai` maps to the local Codex CLI command `codex`; `CODEX_BIN` may
     override the path.
   - `anthropic` maps to the local Claude Code CLI command `claude`;
     `CLAUDE_BIN` may override the path.
   - A detected CLI only means the command and version probe worked. Treat
     authentication as unknown unless the user explicitly asks for a real check.
   - If the semantic provider/model is unset, report that AI Enrich is blocked
     until AI config is saved for the project.
   - If ServiceManager or executor is unavailable, report chain/executor as
     degraded even when local AI CLIs are detected.

   Suggested status copy:

   ```text
   Codex CLI: detected at <path>, version <version>, auth unknown.
   Claude CLI: detected at <path>, version <version>, auth unknown.
   Semantic route: <provider/model or unset>.
   AI Enrich: ready / blocked because <reason>.
   Chain executor: ready / degraded because <reason>.
   ```

5. Open the dashboard:

   ```text
   aming-claw open
   ```

   Default URL: `http://localhost:40000/dashboard`. The root path `/` is not
   the dashboard and may return `404` without meaning governance failed.
   Governance serves the dashboard from packaged static assets. No build is
   needed when `agent/governance/dashboard_dist/index.html` or
   `frontend/dashboard/dist/index.html` already exists. In a raw checkout with
   missing assets, run:

   ```text
   cd frontend/dashboard
   npm install
   npm run build
   ```

   If `/api/health` is OK but `/dashboard` returns `503`, report dashboard
   static assets as missing instead of reporting governance as down.

6. Plugin aftercare:

   `aming-claw start` only starts the governance service. It does not prove that
   the current Codex thread loaded the plugin. After installing or updating the
   plugin, tell the user to reload Codex or open a new Codex session, then
   verify that the Aming Claw skill and `mcp__aming_claw` tools are visible.
   A reload only addresses current-session hot loading. If `codex exec` reports
   `failed to load plugin`, `plugin is not installed`, or invalid marketplace
   paths, run `aming-claw plugin install` and `aming-claw plugin doctor` first;
   do not present reload as the primary fix.
   Treat ServiceManager/executor offline as degraded runtime, not as dashboard
   or governance failure.

## Current Workspace Registration

Starting governance or opening the dashboard does not register the current
workspace. If `GET /api/projects` is empty or does not include the active
workspace, ask before bootstrap unless the user explicitly requested
initialize/register/bootstrap.

Use governance on port `40000`, not the ServiceManager sidecar on `40101`:

```text
POST http://127.0.0.1:40000/api/project/bootstrap
```

For explicit bootstrap, infer the project id from the folder name and use common
excludes such as `node_modules`, `dist`, `build`, `.expo`, `.next`, and
`coverage`. Bootstrap builds a commit-bound graph; if the workspace is a dirty
git repo, ask the user to commit/stash first.

## MVP Graph Model

The Aming Claw repo itself can use `aming-claw://seed-graph-summary` as packaged
MVP navigation when no active `aming-claw` graph exists. That is not an install
failure. Target/user projects need a registered active graph before graph-backed
claims are available.

## CLI Surface (`agent/cli.py`)

| Command | Purpose |
| --- | --- |
| `aming-claw init` | Write `.aming-claw.yaml` in the current directory. |
| `aming-claw bootstrap --path <dir> --name <id>` | Register an external project under governance. |
| `aming-claw scan --path <dir> --project-id <id>` | Scan an external project into a `.aming-claw` candidate workspace. |
| `aming-claw start --port 40000 --workspace .` | Start governance in the foreground from a separate terminal/window. |
| `aming-claw status` | GET `/api/health` against the running governance service. |
| `aming-claw plugin doctor [--plugin-root <dir>] [--python <python3.9+>]` | Run read-only aftercare checks for plugin assets, generated marketplace, versioned Codex plugin cache, MCP config, Codex config hints, Python runtime, dashboard assets, AI CLI probes, and governance health. |
| `aming-claw open --governance-url <url>` | Open the dashboard in the default browser. |
| `aming-claw launcher [--open-browser] [--output path]` | Write the launcher HTML artifact. |
| `aming-claw plugin install <git-url>` | Clone/update a user-local plugin checkout, validate Codex/Claude manifests, optionally pip-install the runtime, install Codex cache/config, and print next steps. |
| `aming-claw run-executor` | Start an executor worker directly. Normally ServiceManager owns this — only use for explicit debugging. |

## Project-Local Plugin Contract

- MCP server config: `.mcp.json` at repo root, stdio entrypoint `python -m agent.mcp.server --project aming-claw --workers 0 --governance-url http://localhost:40000`. Plugin sessions keep `--workers 0`; ServiceManager owns executor lifecycle.
- Project rules: `CLAUDE.md` at repo root (graph-first discovery, backlog before mutation, MF SOP, dashboard E2E impact).
- This skill is auto-discovered through the Claude Code plugin manifest at `.claude-plugin/plugin.json`. It is namespaced as `/aming-claw:aming-claw-launcher`.

## Offline / Fresh Install

If governance is offline or this is a fresh install:

1. If the user asks to install from a Git URL, prefer the host-native plugin
   flow first:

   ```text
   Install the Aming Claw plugin from https://github.com/amingclawdev/aming-claw
   ```

   If the host cannot install Git plugins directly yet, ask the user to clone
   once and run:

   ```text
   git clone https://github.com/amingclawdev/aming-claw.git
   cd aming-claw
   pip install -e .
   ```

   Then ask the user to start governance in a separate terminal/window:

   ```text
   cd aming-claw
   python -m agent.cli start
   ```

   Then run:

   ```text
   python -m agent.cli plugin doctor --plugin-root . --python python
   ```

   If the CLI is already available, use:

   ```text
   aming-claw plugin install https://github.com/amingclawdev/aming-claw
   aming-claw plugin doctor
   ```

2. Read `aming-claw://seed-graph-summary` (packaged MVP structure) when the MCP resource is available — do not invent module locations.
3. Show the explicit startup flow rather than auto-running it inline:

   ```text
   aming-claw launcher
   aming-claw start
   ```

   Make clear that `aming-claw start` only exits immediately when governance is
   already healthy or the port is conflicting. When it starts governance, it is
   long-running and should stay open in its own terminal; the assistant should
   return to status checks instead of waiting for the command to exit.

4. After plugin install, tell the user to reload Codex/open a new session. The
   current thread may not hot-load newly installed skills or MCP tools.
   For Claude Code, plugin install loads skills only; it does not install the
   Python runtime, start governance, prove MCP visibility in the current
   session, or validate CLI auth. If the sandbox blocks a remote installer
   script, prefer an explicit `git clone` plus local marketplace install.
5. After the user starts services, re-run `runtime_status` and confirm `version_check.ok == true` before recommending any mutation.

## When to Hand Off

Use the main `aming-claw` skill (`skills/aming-claw/SKILL.md`) for:

- Graph queries, node lookups, semantic search, function indexes.
- Backlog mutations, manual-fix / observer-hotfix work, Chain trailers.
- Chain debugging, version-gate, semantic reconcile, drift analysis.
- Dashboard governance flows beyond the basic preview.

## What Not To Do

- Do not auto-start governance from a tool call. Always show `aming-claw start`
  as a separate-terminal command and wait for the user.
- Do not treat `aming-claw start` as plugin verification. Use
  `aming-claw plugin doctor` and a new Codex session visibility check.
- Do not bypass `aming-claw start` with `docker compose up` or raw `python -m agent.governance.server` unless the user is explicitly debugging.
- Do not modify `governance.db`, the version chain, or graph state from launcher flows — those go through the main `aming-claw` skill.
- Do not click HTML launcher buttons that would execute local shell commands. The launcher artifact is documentation, not a remote control.

## References

- Main governance skill: [SKILL.md](../aming-claw/SKILL.md).
- CLI source: [cli.py](../../agent/cli.py).
- Project rules: [CLAUDE.md](../../CLAUDE.md).
- Plugin packaging notes: [plugin-packaging.md](../aming-claw/references/plugin-packaging.md).
