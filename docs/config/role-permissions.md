# Role Permissions Schema Reference

This document defines the permission model for each role in the Aming Claw governance platform.

## Permission Matrix

| Permission | Observer | Coordinator | PM | Dev | Tester | QA | Gatekeeper |
|-----------|----------|-------------|-----|-----|--------|-----|------------|
| `task.create` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `task.claim` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `task.complete` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `node.verify` | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `node.baseline` | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `node.waive` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `gate.override` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| `memory.write` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `memory.read` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

## Role Schema

### `role_id` (required)

- **Type:** `string`
- **Allowed values:** `"observer"`, `"coordinator"`, `"pm"`, `"dev"`, `"tester"`, `"qa"`, `"gatekeeper"`
- **Description:** Identifies the role assigned to an agent session.

### `permissions` (derived)

- **Type:** `array` of `string`
- **Description:** List of permission strings granted to this role. Derived from the permission matrix above.

### `tool_access` (derived)

- **Type:** `array` of `string`
- **Description:** MCP tools available to this role.

## Tool Access by Role

| Role | Tools |
|------|-------|
| **PM** | Read (code + docs), propose_node |
| **Dev** | Read, Write, Edit, Bash |
| **Tester** | Read, Bash (pytest only) |
| **QA** | Read, Bash (review commands) |
| **Coordinator** | All dispatch APIs |
| **Observer** | All APIs (read + claim/complete) |
| **Gatekeeper** | Read, gate.override |

## YAML Migration Plan

Role permissions are currently defined in `agent/role_permissions.py`. The migration plan:

1. **Phase A (current):** Permissions hardcoded in Python (`ROLE_PERMISSIONS` dict in `agent/role_permissions.py`)
2. **Phase B (planned):** Extract to `.aming-claw.yaml` under `roles:` key, allowing per-project overrides
3. **Phase C (future):** Full YAML-driven role configuration with custom role definitions

### Phase B Target Schema

```yaml
roles:
  dev:
    tools: [Read, Write, Edit, Bash]
    permissions: [task.complete, memory.write, memory.read]
  tester:
    tools: [Read, Bash]
    permissions: [task.complete, node.verify, memory.write, memory.read]
```

See [.aming-claw.yaml](aming-claw-yaml.md) for the project configuration file format.
