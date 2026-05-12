# .aming-claw.yaml Schema Reference

`.aming-claw.yaml` is the project-level contract used by bootstrap, graph
reconcile, dashboard project management, and AI role routing. Keep it at the
workspace root.

## Required Fields

```yaml
project_id: my-project
language: python
```

- `project_id`: kebab-case governance project id.
- `language`: primary language hint. Mixed-language projects are still scanned
  by the language adapters.

## Testing

```yaml
testing:
  unit_command: "python -m pytest"
  e2e_command: "npm run e2e"
  allowed_commands:
    - executable: "python"
      args_prefixes: ["-m pytest", "-m unittest"]
```

`allowed_commands` is the command safety allowlist used by bootstrap and
project registration checks.

## Graph Governance

```yaml
governance:
  enabled: true
  test_tool_label: "pytest"
  exclude_roots:
    - "examples"

graph:
  exclude_paths:
    - "docs/dev"
    - ".worktrees"
  ignore_globs:
    - "**/node_modules/**"
    - "**/dist/**"
  nested_projects:
    mode: "exclude"
    roots:
      - "examples/dashboard-e2e-demo"
```

`governance.exclude_roots` is the legacy path-prefix list. `graph.exclude_paths`
is the v2 graph-scanner path-prefix list. They are merged into
`effective_exclude_roots` along with `graph.nested_projects.roots` when
`nested_projects.mode` is `exclude`.

Use `graph.exclude_paths` for generated artifacts, local worktrees, nested demo
projects, and docs/dev handoff scratch space that should not become governed L4
or L7 nodes in the parent project.

## AI Routing

```yaml
ai:
  routing:
    pm:
      provider: "openai"
      model: "gpt-5.5"
    dev:
      provider: "openai"
      model: "gpt-5.5"
    tester:
      provider: "openai"
      model: "gpt-5.4"
    qa:
      provider: "openai"
      model: "gpt-5.5"
    semantic:
      provider: "anthropic"
      model: "claude-opus-4-7"
```

Dashboard reads this block through `GET /api/projects/{project_id}/config` and
`GET /api/projects/{project_id}/ai-config`. Operators can update only this
block through `POST /api/projects/{project_id}/ai-config` with a `routing`
object; the backend writes it back to `.aming-claw.yaml` / `.aming-claw.json`
and leaves other config sections intact. Execution still applies the existing
runtime routing stack until role launchers consume the project-level routing
directly.

## Dashboard Branch / Ref Selection

```http
GET  /api/projects/{project_id}/git-refs
POST /api/projects/{project_id}/git-ref
```

The ref selector is dashboard metadata for graph operations. `POST /git-ref`
validates that the requested branch/ref resolves to a commit and persists it in
the project registry as `selected_ref`; it does not run `git checkout`.
Branch-aware graph history and semantic projection rules are separate schema
work.

## Complete Example

```yaml
version: 2
project_id: dashboard-demo
name: "Dashboard Demo"
language: typescript

testing:
  unit_command: "npm test"
  e2e_command: "npm run e2e"

governance:
  enabled: true
  test_tool_label: "vitest"

graph:
  exclude_paths:
    - "node_modules"
    - "dist"
  nested_projects:
    mode: "exclude"
    roots: []

ai:
  routing:
    pm: { provider: "openai", model: "gpt-5.5" }
    dev: { provider: "openai", model: "gpt-5.5" }
    semantic: { provider: "anthropic", model: "claude-opus-4-7" }
```
