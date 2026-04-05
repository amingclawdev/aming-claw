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
- **Example:** `["-m", "agent.governance.mcp_server"]`

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
      "args": ["-m", "agent.governance.mcp_server"],
      "env": {
        "PROJECT_ID": "my-project"
      }
    }
  }
}
```

## Notes

- The MCP server provides governance tools (task management, workflow queries, memory operations) as MCP tool calls.
- The `ServiceManager` auto-starts the executor worker when the MCP server initializes.
- See [.aming-claw.yaml](aming-claw-yaml.md) for project-level configuration.
