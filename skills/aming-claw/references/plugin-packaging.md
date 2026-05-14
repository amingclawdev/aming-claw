# Plugin Packaging Notes

This repo is treated as the plugin root for the initial Aming Claw plugin package.

## MVP Status

- Codex local plugin shape is present: `.codex-plugin/plugin.json`,
  `skills/aming-claw/`, and `.mcp.json`.
- Claude Code local plugin shape is present: `.claude-plugin/plugin.json`
  and `.claude-plugin/marketplace.json` at the repo root; `skills/` and
  `.mcp.json` are auto-discovered. Skills are namespaced
  `/aming-claw:aming-claw` and `/aming-claw:aming-claw-launcher`.
- MCP runs through the stdio module entrypoint:
  `python -m agent.mcp.server --project aming-claw --workers 0`.
- Governance and ServiceManager stay host-owned. Plugin MCP sessions should
  control/query them, not spawn duplicate executor workers.
- The dashboard is served by governance at `/dashboard`. The root path `/` is
  not the dashboard and may return `404`.
- Dashboard static assets are required. No build is needed if
  `agent/governance/dashboard_dist/index.html` or
  `frontend/dashboard/dist/index.html` exists. Raw checkouts missing both should
  run `npm --prefix frontend/dashboard install` and
  `npm --prefix frontend/dashboard run build` before static smoke testing.
- Pip install works as a Python package entrypoint. The release build must run
  `npm --prefix frontend/dashboard run build` first; that command syncs the
  dashboard into `agent/governance/dashboard_dist` so the wheel can serve
  `/dashboard` without a target-machine npm build.
- A plugin launcher is explicit: `aming-claw launcher` writes a local HTML
  entry artifact with status/start guidance and a dashboard link. It does not
  auto-start governance or ServiceManager.
- Git URL bootstrap is explicit: `aming-claw plugin install <repo-url>` and
  `python scripts/install_from_git.py <repo-url>` clone/update a user-local
  checkout, validate Codex/Claude plugin assets, optionally pip-install the
  runtime, and print next steps. They do not silently install credentials or
  mutate global editor settings.
- Installing plugin assets, installing the Python package, starting governance,
  serving the dashboard, loading MCP tools in the current Codex/Claude session,
  and ServiceManager/executor health are separate states. After plugin install
  or update, open a new editor session before expecting new skills/MCP tools.

## Layout

- `.codex-plugin/plugin.json`: Codex local plugin manifest (explicit
  `skills` and `mcpServers` pointers).
- `.agents/plugins/marketplace.json`: repo-local Codex marketplace entry that
  installs this root plugin by default for local/plugin sessions. Keep
  `source.path` as `"./."`; plain `"./"` normalizes to an empty local plugin
  source path in current Codex CLI builds.
- `.claude-plugin/plugin.json`: Claude Code plugin manifest. `skills/` and
  `.mcp.json` are auto-discovered from the plugin root, so the manifest only
  declares `name` + `description` + metadata.
- `.claude-plugin/marketplace.json`: repo-local Claude Code marketplace entry
  with one plugin (`source: "."`), so `/plugin marketplace add <repo>` then
  `/plugin install aming-claw@aming-claw-local` works without an external
  registry.
- `skills/aming-claw/`: main governance skill loaded for graph, backlog, MF,
  semantic, and chain work.
- `skills/aming-claw-launcher/`: onboarding skill loaded for preview, start,
  status, and dashboard flows.
- `.mcp.json`: active MCP server config using `agent.mcp.server`.
- `agent/plugin_installer.py` + `scripts/install_from_git.py`: Git URL plugin
  installer used by the CLI and first-run fallback flows.

The Codex manifest points to:

```json
{
  "skills": "./skills/",
  "mcpServers": "./.mcp.json"
}
```

The Claude Code manifest relies on auto-discovery — no explicit pointers
needed for skills or MCP. Sample shape:

```json
{
  "name": "aming-claw",
  "version": "0.1.0",
  "description": "Graph-first governance workflow guard ...",
  "author": { "name": "Aming Claw" },
  "keywords": ["governance", "graph", "mcp"]
}
```

## MCP Config

The active MCP server entrypoint is:

```text
python -m agent.mcp.server --project aming-claw --workers 0 --governance-url http://localhost:40000
```

Keep `--workers 0` for normal editor/plugin sessions. ServiceManager owns executor lifecycle.
Redis event forwarding is off by default for local plugin sessions; use
`MCP_ENABLE_EVENTS=1` or `--enable-events` only when push notifications are
needed.

`.mcp.json` must remain relocatable. Use `"cwd": "."` and avoid absolute
developer-machine paths such as `C:\Users\...`; package tests enforce this.

Claude Code reads project-scoped MCP servers from `.mcp.json` (auto-discovered
from the plugin root when packaged as a plugin); Codex local plugin packaging
reads the plugin manifest's `mcpServers` pointer. Keeping the same stdio
entrypoint at `.mcp.json` lets both surfaces reuse one MCP contract.

## Compatibility Checks

Run these before publishing a local plugin bundle or pip package:

```text
python -m pytest agent/tests/test_package_install.py agent/tests/test_mcp_server_stdio.py agent/tests/test_dashboard_static_route.py -q
python -m pytest agent/tests/test_plugin_installer.py agent/tests/test_cli.py -q
npm --prefix frontend/dashboard run build
python scripts/build_package.py --skip-dashboard-build
node frontend/dashboard/scripts/e2e-trunk.mjs --probe --static-route --dashboard http://localhost:40000/dashboard
```

Directory picker smoke: `/api/local/choose-directory` should prefer `tkinter`
and then use PowerShell on Windows, `osascript` on macOS, and `zenity`/`kdialog`
on Linux. Manual path entry remains the documented fallback when no GUI picker
is available.

## Packaging Gap Matrix

| Surface | Current | Gap Before Public Release |
| --- | --- | --- |
| Pip package | `pyproject.toml` exposes `aming-claw`, `aming-governance`, and `aming-governance-host`; dashboard assets are synced into `agent/governance/dashboard_dist` before wheel build. | Run clean wheel install smoke on each release target. |
| Codex local plugin | `.codex-plugin/plugin.json` points at skills and `.mcp.json`; `.agents/plugins/marketplace.json` points at the repo root plugin and marks it installed by default for local sessions. `aming-claw plugin install <git-url>` prepares a user-local checkout for hosts that need an explicit local plugin root. Tests ensure paths exist and `.mcp.json` is relocatable. | Sanitize env and host URLs before publishing outside trusted local/team installs. Host-native "paste Git URL to install" support still depends on the editor/plugin host. |
| Claude Code local plugin | `.claude-plugin/plugin.json` at repo root + project-level `CLAUDE.md` + `.mcp.json`; `.claude-plugin/marketplace.json` makes the repo a local marketplace. Skills auto-discovered as `/aming-claw:aming-claw` (governance) and `/aming-claw:aming-claw-launcher` (onboarding). Install via `/plugin marketplace add <path-or-git-url>` then `/plugin install aming-claw@aming-claw-local`. | Sanitize env/host URLs before publishing outside trusted local/team installs. Global Claude Code settings remain out of scope. |
| Cross-platform desktop | Windows, macOS, and Linux directory picker fallbacks are implemented with manual entry fallback. | Add real-machine smoke evidence for macOS and Linux/WSL before public release. |

## Publish Caution

Before publishing or sharing the plugin outside the local machine, sanitize
`.mcp.json` and environment variables. Never commit local credentials into MCP
env blocks; provide them through the host environment instead.

Sources checked 2026-05-13: Claude Code settings/MCP/plugin scopes
(`https://code.claude.com/docs/en/settings`,
`https://code.claude.com/docs/en/mcp`) and OpenAI Docs MCP/Codex docs
(`https://platform.openai.com/docs/docs-mcp`,
`https://platform.openai.com/docs/codex`).
