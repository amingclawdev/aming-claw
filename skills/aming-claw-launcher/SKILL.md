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

2. Start governance in the foreground (host-owned, no plugin-spawned workers):

   ```text
   aming-claw start
   ```

   This runs `start_governance.main` with `GOVERNANCE_PORT` from `--port` (default `40000`). ServiceManager is started independently (see project rules in `CLAUDE.md`); do not let the plugin session spawn executor workers.

3. Confirm health (CLI):

   ```text
   aming-claw status
   ```

   Or, when MCP is available, prefer structured probes for a richer snapshot:

   - `runtime_status` — governance + ServiceManager + version_check in one call.
   - `version_check` — HEAD vs CHAIN_VERSION + dirty files.
   - `graph_status` — active graph snapshot + stale check + semantic drift summary.
   - `health` — bare governance ping.

4. Open the dashboard:

   ```text
   aming-claw open
   ```

   Default URL: `http://localhost:40000/dashboard`. The dashboard is served by governance from `frontend/dashboard/dist` and is the shared cockpit for user + AI + governance.

## CLI Surface (`agent/cli.py`)

| Command | Purpose |
| --- | --- |
| `aming-claw init` | Write `.aming-claw.yaml` in the current directory. |
| `aming-claw bootstrap --path <dir> --name <id>` | Register an external project under governance. |
| `aming-claw scan --path <dir> --project-id <id>` | Scan an external project into a `.aming-claw` candidate workspace. |
| `aming-claw start --port 40000 --workspace .` | Start governance in the foreground. |
| `aming-claw status` | GET `/api/health` against the running governance service. |
| `aming-claw open --governance-url <url>` | Open the dashboard in the default browser. |
| `aming-claw launcher [--open-browser] [--output path]` | Write the launcher HTML artifact. |
| `aming-claw plugin install <git-url>` | Clone/update a user-local plugin checkout, validate Codex/Claude manifests, optionally pip-install the runtime, and print next steps. |
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
   python -m agent.cli start
   ```

   If the CLI is already available, use:

   ```text
   aming-claw plugin install https://github.com/amingclawdev/aming-claw
   ```

2. Read `aming-claw://seed-graph-summary` (packaged MVP structure) when the MCP resource is available — do not invent module locations.
3. Show the explicit startup flow rather than auto-running:

   ```text
   aming-claw launcher
   aming-claw start
   ```

4. After the user starts services, re-run `runtime_status` and confirm `version_check.ok == true` before recommending any mutation.

## When to Hand Off

Use the main `aming-claw` skill (`skills/aming-claw/SKILL.md`) for:

- Graph queries, node lookups, semantic search, function indexes.
- Backlog mutations, manual-fix / observer-hotfix work, Chain trailers.
- Chain debugging, version-gate, semantic reconcile, drift analysis.
- Dashboard governance flows beyond the basic preview.

## What Not To Do

- Do not auto-start governance from a tool call. Always show `aming-claw start` and wait for the user.
- Do not bypass `aming-claw start` with `docker compose up` or raw `python -m agent.governance.server` unless the user is explicitly debugging.
- Do not modify `governance.db`, the version chain, or graph state from launcher flows — those go through the main `aming-claw` skill.
- Do not click HTML launcher buttons that would execute local shell commands. The launcher artifact is documentation, not a remote control.

## References

- Main governance skill: [SKILL.md](../aming-claw/SKILL.md).
- CLI source: [cli.py](../../agent/cli.py).
- Project rules: [CLAUDE.md](../../CLAUDE.md).
- Plugin packaging notes: [plugin-packaging.md](../aming-claw/references/plugin-packaging.md).
