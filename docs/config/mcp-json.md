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
      "env": {
        "GOVERNANCE_URL": "http://localhost:40000",
        "PYTHONDONTWRITEBYTECODE": "1"
      }
    }
  }
}
```

## Notes

- The active MCP server entrypoint is `agent.mcp.server`. The older `agent.governance.mcp_server` is retained only for compatibility.
- The MCP server provides governance tools as MCP tool calls: task management, workflow impact, backlog filing, graph governance queries, operations queue checks, version checks, and optional executor control.
- Use `--workers 0` for normal editor/plugin sessions so opening MCP does not spawn duplicate queue consumers. Start workers only from an explicit executor-owned session.
- See [.aming-claw.yaml](aming-claw-yaml.md) for project-level configuration.
