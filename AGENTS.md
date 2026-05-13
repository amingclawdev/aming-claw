# Aming Claw Codex Setup

Use this project-level Codex setup when working in this repository or an
external project governed by Aming Claw.

## Runtime Model

- Codex does not auto-start Aming Claw services.
- Start governance explicitly with `aming-claw start` or `aming-governance-host`.
- Keep `.mcp.json` project-local and relocatable; do not put credentials or
  absolute user-machine paths in it.
- Use MCP tools for graph, backlog, runtime, and ServiceManager checks before
  editing governance or dashboard code.

## Local Startup

```bash
aming-claw launcher
aming-claw start
```

Then open:

```text
http://localhost:40000/dashboard
```

## Codex Contract

1. Load the project `.mcp.json`.
2. Call `runtime_status`, `graph_status`, and `graph_operations_queue` before
   implementation work.
3. File or update a backlog row before mutating code, docs, config, dashboard
   assets, or runtime state.
4. For manual fixes, follow `skills/aming-claw/references/mf-sop.md`.
5. For dashboard or graph behavior, evaluate E2E impact and run or file the
   relevant E2E evidence.

Global Codex settings are intentionally out of scope for v1. Keep this
project-level setup transparent and reversible.
