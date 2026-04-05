# Aming Claw — Onboarding Guide

This guide covers project setup and first-task creation for both self-governance
(the aming-claw project itself) and external project onboarding.

## 1. Project Setup

### 1.1 Configuration File

Every project managed by aming-claw needs a `.aming-claw.yaml` configuration file
at the repository root. This file defines the project identity and governance settings.

```yaml
# .aming-claw.yaml
project_id: my-project
governance_url: http://localhost:40000
roles:
  - coordinator
  - pm
  - dev
  - tester
  - qa
  - gatekeeper
  - observer
```

### 1.2 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOVERNANCE_URL` | `http://localhost:40000` | Governance API endpoint |
| `EXECUTOR_POLL_INTERVAL` | `10` | Seconds between task polls |
| `MAX_CONCURRENT_WORKERS` | `2` | Parallel executor workers (1-5) |
| `ROLE_CONFIG_DIR` | `config/roles` | Custom role config directory |

### 1.3 Role Configuration (YAML)

Role configs live in `config/roles/default/{role}.yaml`. Each file defines:
- `version` — Config schema version
- `role` — Role name
- `max_turns` — Maximum Claude CLI turns
- `tools` — Allowed Claude tools
- `permissions` — `can`/`cannot` action lists
- `prompt_template` — System prompt for the role

To customize roles per-project, create overrides in `config/roles/{project_id}/{role}.yaml`.
Override files are merged on top of defaults at startup.

## 2. Acceptance Graph Definition

The acceptance graph defines verification nodes that track project quality gates.

### 2.1 Graph Structure

Nodes are organized in layers:
- **L1** — Core infrastructure (must pass before L2)
- **L2** — Feature modules
- **L3** — Integration / E2E

Each node has a status lifecycle: `pending` → `testing` → `t2_pass` → `qa_pass`

### 2.2 Defining Nodes

Use the governance API to register nodes:

```bash
curl -X POST http://localhost:40000/api/wf/my-project/node \
  -H "Content-Type: application/json" \
  -d '{"node_id": "L1.1", "description": "Core module tests", "depends_on": []}'
```

Or define nodes in the PM PRD output — the auto-chain will register them.

## 3. API Import

### 3.1 Python API

```python
from agent.governance.client import GovernanceClient

client = GovernanceClient(project_id="my-project")
tasks = client.list_tasks(status="queued")
```

### 3.2 REST API

```bash
# Health check
curl http://localhost:40000/api/health

# List tasks
curl http://localhost:40000/api/task/my-project/list

# Workflow summary
curl http://localhost:40000/api/wf/my-project/summary
```

## 4. Creating Your First PM Task

The auto-chain flow starts with a PM task:

```bash
curl -X POST http://localhost:40000/api/task/my-project/create \
  -H "Content-Type: application/json" \
  -d '{
    "type": "pm",
    "prompt": "Analyze requirements for feature X and produce a PRD with target files, acceptance criteria, and test plan.",
    "metadata": {}
  }'
```

The executor picks up the PM task, runs Claude with the PM role prompt,
and the auto-chain creates subsequent dev → test → qa → gatekeeper → merge tasks.

## 5. Verification

### 5.1 Check Task Status

```bash
curl http://localhost:40000/api/task/my-project/list?status=succeeded
```

### 5.2 Check Version Gate

```bash
curl http://localhost:40000/api/version-check/my-project
```

### 5.3 Run Tests Locally

```bash
pytest agent/tests/ -v
```

## 6. Self-Governance Setup (aming-claw)

The aming-claw project governs itself. The `.aming-claw.yaml` config at the repo root
defines `project_id: aming-claw`.

Key differences from external project onboarding:
- The governance server runs on the host (port 40000), not in Docker
- MCP server auto-starts the executor via ServiceManager
- Role configs are in `config/roles/default/`
- All code changes go through the auto-chain: PM → Dev → Test → QA → Gatekeeper → Merge

## 7. External Project Onboarding

To onboard an external project:

1. **Create `.aming-claw.yaml`** in the external project's repo root
2. **Start governance server** pointing to the external project
3. **Import the acceptance graph** — define L1/L2/L3 nodes for the project
4. **Create initial PM task** — the auto-chain handles the rest
5. **Configure role overrides** (optional) — place custom YAML in `config/roles/{project_id}/`

### 7.1 External Project Config Example

```yaml
# .aming-claw.yaml (in external project root)
project_id: my-external-project
governance_url: http://localhost:40000
```

### 7.2 Custom Role Overrides

Create project-specific role configs:

```bash
mkdir -p config/roles/my-external-project
# Override PM max_turns for this project
cat > config/roles/my-external-project/pm.yaml <<EOF
max_turns: 30
tools:
  - Read
  - Grep
EOF
```

Override files are merged on top of defaults — only specify fields you want to change.
