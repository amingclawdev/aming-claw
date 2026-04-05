# .aming-claw.yaml Schema Reference

The `.aming-claw.yaml` file is the project-level configuration file that registers a workspace with the Aming Claw governance platform.

## Location

Place this file at the root of your project workspace.

## Schema

### `project_id` (required)

- **Type:** `string`
- **Description:** Unique identifier for the project within the governance platform.
- **Example:** `"my-project"`

### `workspace_path` (optional)

- **Type:** `string`
- **Description:** Absolute path to the project workspace. Defaults to the directory containing this file.
- **Example:** `"/home/user/projects/my-project"`

### `governance` (optional)

- **Type:** `object`
- **Description:** Governance-specific settings.

#### `governance.auto_chain` (optional)

- **Type:** `boolean`
- **Default:** `true`
- **Description:** Whether to enable automatic task chain progression (PM → Dev → Test → QA → Merge).

#### `governance.gate_policy` (optional)

- **Type:** `string`
- **Allowed values:** `"strict"`, `"permissive"`
- **Default:** `"strict"`
- **Description:** Controls how strictly gates enforce validation between pipeline stages.

### `memory` (optional)

- **Type:** `object`
- **Description:** Memory backend configuration.

#### `memory.backend` (optional)

- **Type:** `string`
- **Allowed values:** `"local"`, `"docker"`, `"cloud"`
- **Default:** `"local"`
- **Description:** Which memory backend to use for development memory storage.

### `roles` (optional)

- **Type:** `object`
- **Description:** Role-specific overrides. See [role-permissions.md](role-permissions.md) for the full role permission schema.

## Example

```yaml
project_id: my-project
governance:
  auto_chain: true
  gate_policy: strict
memory:
  backend: local
```
