# Plugin Packaging Notes

This repo is treated as the plugin root for the initial Aming Claw plugin package.

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

## Publish Caution

Before publishing or sharing the plugin outside the local machine, sanitize `.mcp.json` and environment variables. Local credentials and host paths are acceptable for a private dev checkout but not for a distributed plugin.
