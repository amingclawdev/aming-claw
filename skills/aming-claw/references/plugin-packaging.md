# Plugin Packaging Notes

This repo is treated as the plugin root for the initial Aming Claw plugin package.

## MVP Status

- Codex local plugin shape is present: `.codex-plugin/plugin.json`,
  `skills/aming-claw/`, and `.mcp.json`.
- MCP runs through the stdio module entrypoint:
  `python -m agent.mcp.server --project aming-claw --workers 0`.
- Governance and ServiceManager stay host-owned. Plugin MCP sessions should
  control/query them, not spawn duplicate executor workers.
- The dashboard is served by governance at `/dashboard` from
  `frontend/dashboard/dist`; build it before static smoke testing.
- Pip install works as a Python package entrypoint, but a one-command packaged
  desktop plugin still needs a release wrapper that builds/includes dashboard
  assets and rewrites host-specific config safely.

## Layout

- `.codex-plugin/plugin.json`: plugin manifest.
- `skills/aming-claw/`: session-loadable skill and references.
- `.mcp.json`: active MCP server config using `agent.mcp.server`.

The manifest points to:

```json
{
  "skills": "./skills/",
  "mcpServers": "./.mcp.json"
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

Claude Code reads project-scoped MCP servers from `.mcp.json`; Codex local
plugin packaging reads the plugin manifest's `mcpServers` pointer. Keeping the
same stdio entrypoint lets both surfaces reuse one MCP contract.

## Compatibility Checks

Run these before publishing a local plugin bundle or pip package:

```text
python -m pytest agent/tests/test_package_install.py agent/tests/test_mcp_server_stdio.py agent/tests/test_dashboard_static_route.py -q
npm --prefix frontend/dashboard run build
node frontend/dashboard/scripts/e2e-trunk.mjs --probe --static-route --dashboard http://localhost:40000/dashboard
```

Windows-specific smoke: `/api/local/choose-directory` must use the PowerShell
folder picker fallback when `tkinter` is unavailable. macOS/Linux packaging
should add native picker fallbacks before claiming full GUI parity; until then,
manual path entry remains the documented fallback.

## Packaging Gap Matrix

| Surface | Current | Gap Before Public Release |
| --- | --- | --- |
| Pip package | `pyproject.toml` exposes `aming-claw`, `aming-governance`, and `aming-governance-host` scripts. | Decide whether wheels include built dashboard assets, or require `npm --prefix frontend/dashboard run build` post-install. Add a release command that verifies static assets exist. |
| Codex local plugin | `.codex-plugin/plugin.json` points at skills and `.mcp.json`; tests ensure paths exist and `.mcp.json` is relocatable. | Add marketplace metadata only when distributing outside this repo; sanitize env and host URLs. |
| Claude Code project plugin/settings | The same `.mcp.json` stdio server and `skills/aming-claw` instructions can be mirrored into Claude project settings. | Add a checked-in `.claude/settings.json` or install guide only after deciding team-level permission policy. Keep `.claude/settings.local.json` ignored. |
| Cross-platform desktop | Windows PowerShell folder picker fallback exists. | Add macOS `osascript` and Linux `zenity`/portal fallbacks if native directory picking becomes a v1 requirement. |

## Publish Caution

Before publishing or sharing the plugin outside the local machine, sanitize
`.mcp.json` and environment variables. Never commit local credentials into MCP
env blocks; provide them through the host environment instead.

Sources checked 2026-05-13: Claude Code settings/MCP/plugin scopes
(`https://code.claude.com/docs/en/settings`,
`https://code.claude.com/docs/en/mcp`) and OpenAI Docs MCP/Codex docs
(`https://platform.openai.com/docs/docs-mcp`,
`https://platform.openai.com/docs/codex`).
