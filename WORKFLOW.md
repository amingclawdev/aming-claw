# Aming-Claw Workflow Integration

> **To integrate your project, add `.aming-claw.yaml` to your repo root and run:**
> ```
> curl -X POST http://localhost:40000/api/projects/register \
>   -d '{"workspace_path": "/path/to/your/project"}'
> ```

---

## Config: `.aming-claw.yaml`

```yaml
version: 1
project:
  id: "kebab-case-id"           # MUST be kebab-case
  name: "Your Project"
  language: "javascript"        # javascript | python | go

testing:
  unit_command: "npm run test"
  e2e_command: ""

build:
  command: ""
  release_checks: []            # commands run before merge (exit 0 = pass)

deploy:
  strategy: "process"           # docker | electron | systemd | process | none
  service_rules:
    - patterns: ["server/**"]
      services: ["backend"]
    - patterns: ["client/**"]
      services: ["frontend"]
    - patterns: ["docs/**", "*.md"]
      services: []
  smoke_test:
    - {name: "backend", type: "http", url: "http://localhost:3000/health"}

governance:
  enabled: true
  test_tool_label: "jest"
```

## Pipeline

```
Message → Coordinator → PM → Dev → Gate → Tester → QA → Merge → Deploy → Smoke Test
```

| Stage | What runs | Config field |
|-------|----------|-------------|
| Tester | Your test suite | `testing.unit_command` |
| Release check | Your pre-deploy script | `build.release_checks` |
| Deploy | Restart affected services | `deploy.service_rules` + `deploy.commands` |
| Smoke test | Hit health endpoints | `deploy.smoke_test` |

## APIs

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/projects/register` | Register project (reads yaml from workspace) |
| GET | `/api/projects/{id}/config` | View resolved config |
| POST | `/api/projects/{id}/explain` | Dry-run: what services affected by file changes |
| POST | `/coordinator/chat` | Submit task: `{"message":"...", "project_id":"..."}` |
| GET | `/status` | Executor health check |

## Roles

| Role | Does | Cannot |
|------|------|--------|
| Coordinator | Dispatch messages to PM, create dev_task | Write code |
| PM | Analyze requirements, output PRD + `_verification` | Write code |
| Dev | Implement in git worktree | Skip tests |
| Gate | Check files changed, syntax valid (~10s) | Read project context |
| Tester | Run unit tests | Modify code |
| QA | Verify in real environment | Modify code |
| Observer | Monitor, `/takeover`, `/pause`, `/cancel` | — |

## Rules

- `project_id` must be kebab-case: `my-app` not `myApp`
- No shell metacharacters in commands: no `;` `&&` `|` backticks
- New projects must have `.aming-claw.yaml` — no silent defaults
- `service_rules` use union matching (file can trigger multiple services)
- Unmatched files = no deploy (safe default)

## Submit via Telegram

Send any message to the aming-claw bot. The coordinator routes it by `project_id`.

## Submit via CLI

```bash
curl -X POST http://localhost:40100/coordinator/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Fix the login bug", "project_id": "my-project"}'
```
