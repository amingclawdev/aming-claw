# .mcp.json Schema Reference

The `.mcp.json` file configures the MCP (Model Context Protocol) server integration for the Aming Claw governance platform.

## Location

Place this file at the root of your project workspace, alongside `.aming-claw.yaml`.

## Schema

### `mcpServers` (required)

- **Type:** `object`
- **Description:** Map of MCP server names to their configurations.

#### Server Configuration

Each server entry contains:

##### `command` (required)

- **Type:** `string`
- **Description:** The command to launch the MCP server process.
- **Example:** `"python"`

##### `args` (required)

- **Type:** `array` of `string`
- **Description:** Arguments passed to the command.
- **Example:** `["-m", "agent.mcp.server", "--project", "aming-claw", "--workers", "0"]`

##### `env` (optional)

- **Type:** `object`
- **Description:** Environment variables to set for the MCP server process.

##### `env_vars` (optional)

- **Type:** `array` of environment variable names
- **Description:** Names of variables that Codex may copy from its local parent environment into the MCP stdio process. Aming Claw uses this for `AMING_WORKER_SESSION_TOKEN` and `AMING_WORKER_FENCE_TOKEN`; values must never be stored in `.mcp.json`, generated TOML, logs, or fixtures.

##### `cwd` (optional)

- **Type:** `string`
- **Description:** Working directory for the MCP server process. Defaults to the project root.

## Example

```json
{
  "mcpServers": {
    "aming-claw": {
      "command": "python",
      "args": [
        "-m",
        "agent.mcp.server",
        "--project",
        "aming-claw",
        "--workers",
        "0",
        "--governance-url",
        "http://localhost:40000"
      ],
      "env_vars": [
        "AMING_WORKER_SESSION_TOKEN",
        "AMING_WORKER_FENCE_TOKEN"
      ],
      "env": {
        "GOVERNANCE_URL": "http://localhost:40000",
        "MANAGER_URL": "http://127.0.0.1:40101",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1"
      }
    }
  }
}
```

## Notes

- The active MCP server entrypoint is `agent.mcp.server`. The older `agent.governance.mcp_server` is retained only for compatibility.
- `env` contains fixed non-secret process settings. `env_vars` is a names-only allowlist: Codex reads current values from its own local environment when it starts the MCP child. A normal host/observer session with neither variable set remains unauthenticated as a worker.
- The MCP server provides governance tools as MCP tool calls: task management, workflow impact, backlog filing, graph governance queries, operations queue checks, version checks, manager-sidecar host operations, and optional MCP-local executor control.
- Use `--workers 0` for normal editor/plugin sessions so opening MCP does not spawn duplicate queue consumers. Start workers only from an explicit executor-owned session.
- Redis event notifications are disabled by default for editor/plugin sessions to keep stdio startup quiet and resilient. Set `MCP_ENABLE_EVENTS=1` or pass `--enable-events` only for sessions that need push notifications.
- Host-ops tools such as `manager_health`, `manager_start`, `governance_redeploy`, `executor_respawn`, and `runtime_status` are a facade over ServiceManager/manager_http_server. They do not make MCP the owner of long-lived services.
- `manager_start` currently bootstraps the Windows PowerShell host script. Linux/macOS bootstrap scripts are tracked separately in `OPT-BACKLOG-HOST-OPS-CROSS-PLATFORM-SCRIPTS`.
- See [.aming-claw.yaml](aming-claw-yaml.md) for project-level configuration.

## Launch Roots And Bridge Configs

The source-controlled plugin `.mcp.json` must stay relocatable: keep `command`
relative or PATH-based and keep `cwd` as `"."`. Do not commit a user-machine
absolute Python path or checkout path into this file.

Some hosts resolve `.mcp.json` relative to the workspace root they opened. If a
session opened from a parent directory can see `aming-claw://current-context`
while a session opened from the plugin checkout cannot, inspect which config the
host loaded. A parent/workspace `.mcp.json` may intentionally act as a
host-local bridge by using absolute `command` and `cwd` values that point back
to the plugin checkout. That bridge can be valid for one machine, but it should
not replace the relocatable repo config.

Run:

```bash
aming-claw plugin doctor
```

The doctor reports the repo-local config, installed plugin cache, and whether a
parent bridge points back to the plugin root. The final readiness check is still
inside the new Codex/Claude session: list MCP resources and confirm
`aming-claw://current-context` is present.
