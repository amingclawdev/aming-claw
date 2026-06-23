# HN Install Audit Docker Harness

This harness verifies the real one-click install path in fresh container HOME
directories. It exists because a host Codex or Claude Code session may already
have the same plugin installed, which makes host-only install tests unreliable.

The first implementation uses **Mode B: host auth reuse**. Existing auth files
are mounted read-only at runtime. They are not copied into Docker images and
the report must label the lane as `AUTH_REUSED_FROM_HOST`.

## Run

```bash
docker/hn-install-audit/run-install-audit.sh --host both
```

Useful options:

```bash
docker/hn-install-audit/run-install-audit.sh \
  --host codex \
  --run-id local-install-smoke \
  --changed-files "docker/hn-install-audit/run-install-audit.sh,agent/mcp/events.py" \
  --ai-prompt-mode required
```

For live Codex/Claude prompt diagnosis, keep a named container so the same
authenticated runtime can be inspected or restarted without rebuilding the
image and auth mount every time:

```bash
RUN_ID=codex-live-debug-$(date -u +%Y%m%dT%H%M%SZ)
PLUGIN_REPO_URL=https://github.com/amingclawdev/aming-claw.git \
docker/hn-install-audit/run-install-audit.sh \
  --host codex \
  --run-id "$RUN_ID" \
  --ref codex/runtime-startup-next-move \
  --ai-prompt-mode required \
  --prompt-timeout-ms 300000 \
  --keep-container \
  --container-name "aming-claw-codex-live-$RUN_ID"
```

Rerun the preserved container during diagnosis:

```bash
docker/hn-install-audit/run-install-audit.sh \
  --host codex \
  --run-id "$RUN_ID" \
  --reuse-container \
  --container-name "aming-claw-codex-live-$RUN_ID"
```

Use `--replace-container` when the named container should be discarded and
created again. The JSON report includes `docker_debug.container_name`,
`docker_debug.prompt_timeout_ms`, and a `docker_debug.reuse_command` hint when
the container name is known.

When `--source-mode mounted-worktree` is used, the harness copies only
audit-relevant source inputs from the mounted checkout. It intentionally
excludes local/runtime-heavy directories such as `.git`, `.codex`, `.claude`,
`.aming-claw`, `shared-volume`, `reports`, `node_modules`, `dist`, `build`,
and `coverage`. This keeps Docker proof from inheriting host caches or private
runtime state while still allowing a local branch to be dogfooded before it is
pushed.

For Claude diagnosis where OAuth/device login must happen inside the container
home, keep the named Claude container as the auth store. Do not run
`--replace-container`, `docker rm`, Docker prune, or an unnamed `--rm` lane
after login unless you intentionally want to discard that Claude auth state.
Reuse the same container name for the prompt rerun and keep logs/report files as
evidence. Mark this lane explicitly:

```bash
docker/hn-install-audit/run-install-audit.sh \
  --host claude \
  --run-id claude-container-auth \
  --auth-mode CONTAINER_PERSISTED_LOGIN \
  --keep-container \
  --container-name aming-claw-claude-container-auth
```

On first login, Claude Code may print an auto-update warning such as
`no write permission to npm prefix`. Treat that as environment diagnostic
evidence, not as proof of failed auth; verify auth with a real `claude -p`
prompt and keep the warning in the audit notes when it affects repeatability.

Kept containers are a debugging shortcut only. Final public proof should run
from the pushed git ref in a fresh container, without `--keep-container` or
`--reuse-container`:

```bash
PLUGIN_REPO_URL=https://github.com/amingclawdev/aming-claw.git \
docker/hn-install-audit/run-install-audit.sh \
  --host codex \
  --ref codex/runtime-startup-next-move \
  --ai-prompt-mode required
```

When testing a Claude login captured in a dedicated container-auth home, pass it
explicitly instead of overriding `HOME`:

```bash
docker/hn-install-audit/run-install-audit.sh \
  --host claude \
  --run-id claude-auth-smoke \
  --claude-auth-home ~/.aming-claw/docker-auth/claude-home \
  --ai-prompt-mode required
```

By default the runner mounts the current checkout into the container and uses
`file:///plugin-source` as the install source. To test the public README path
against GitHub instead:

```bash
PLUGIN_REPO_URL=https://github.com/amingclawdev/aming-claw \
docker/hn-install-audit/run-install-audit.sh --host both
```

Do not treat a host-installed Codex or Claude plugin cache as container proof.
Those cache/config files may legitimately contain host-local absolute paths
such as `/Users/...` or `/home/...`. A Docker validation lane must reinstall or
refresh the plugin runtime from the container-visible source/ref; copied host
caches are debug-only evidence and should be filed as friction if they affect
MCP startup.

## Lanes

- `aming-claw-install-audit-codex`: installs Codex CLI in the image, uses a
  fresh `$CODEX_HOME`, and mounts `<codex-auth-home>/.codex` read-only when
  present.
- `aming-claw-install-audit-claude`: installs Claude Code CLI in the image,
  uses a fresh `$HOME`, and mounts `<claude-auth-home>/.claude` plus
  `<claude-auth-home>/.claude.json` read-only when present.

Each lane has two phases:

1. Feed the README/launcher one-click install prompt to the CLI and verify
   install, skill discovery, MCP tool visibility, and required resource reads.
2. Feed the HN challenge prompt and verify the multi-agent challenge evidence
   path.

The container also runs deterministic code checks so the final report cannot
claim pass solely because the model said it passed.

The same harness is also the reusable AI feature fixture surface. After it
starts an isolated governance service against the cloned container workspace,
it emits:

- `ai_fixture_readiness`: deterministic host/plugin/MCP/dashboard readiness.
- `feature_smoke_results`: feature-specific contract smokes with sanitized
  evidence.

The first feature smoke is `observer_command_pending`. It registers an observer
session, subscribes to the governance event stream, enqueues an observer
command, verifies the reminder-only callback payload, claims the durable command
with the session token, and completes it. The JSON evidence records session and
command ids plus hashes/statuses, but never records `session_token` or host auth
token values.

`DOCKER_LIVE_OBSERVER_ROUTE=1` enables the provider-backed observer route proof
used by `docker_live_ai_observer_route_demo`. That lane asks the AI CLI inside
the container to acknowledge the route alert, follow ordered observer steps,
show the final drift prompt, and write `live_observer_route_result`. The report
keeps prompt, stdout, stderr, and compact evidence hashes plus typed fields such
as `provider_backed`, `route_alert_ack`, `ordered_step_count`, and
`raw_output_stored: false`; it must not persist raw prompt output. The runner
forwards `DOCKER_LIVE_OBSERVER_ROUTE` and the optional
`LIVE_OBSERVER_ROUTE_REPORT_PATH` into the container so the route request is
visible to the audited harness, not only to the host wrapper.

Future AI feature smokes should be added to
`docker/hn-install-audit/common/install-audit.mjs` via the reusable feature
smoke runner, then validated in `docker/hn-install-audit/validate-report.mjs`.
Avoid standalone one-off scripts unless the harness cannot provide the required
isolated governance workspace.

## State Manager Contract

Each install audit report includes a `state_manager` section with schema
`docker_ai_e2e_state_manager.v1`. It records sanitized before/after lane state,
provider config, impact planning, dependency decisions, command evidence, and
feature-smoke evidence.

The first executable lane is still the install audit. The shared state manager
also defines update, new-feature, and external-project lanes so later Docker AI
E2E suites can reuse the same state/report semantics:

- install: reuse read-only host auth while reinstalling plugin/runtime state;
- update: upgrade from a previous known-good baseline to the target commit;
- new-feature: run feature smokes only after the container is current;
- external-project: bootstrap/reconcile governed target projects through a
  provider adapter.

`--changed-files` accepts a newline or comma separated file list. The runner
passes it to the impact planner so the report can explain why lanes were
selected, skipped, blocked, reused, or reserved for a later feature smoke.

The runner attempts every requested lane, then exits non-zero if any requested
lane failed. This keeps the harness useful for CI while still collecting both
Codex and Claude reports from the same run when possible.

Even with `--no-build`, the runner bind-mounts the current
`install-audit.mjs`, `state-manager.mjs`, and `validate-report.mjs` into the
container. Reused images provide the provider CLI and OS dependencies; the
audited harness contract still comes from the checkout under test.

## Artifacts

Reports are written under `docs/hn-demo/audits/install-<run-id>/` by default:

- `codex-install-audit-<run-id>.json`
- `claude-install-audit-<run-id>.json`
- `<host>-hn-demo-<run-id>.md/json` from the HN demo run

Run report validation manually:

```bash
node docker/hn-install-audit/validate-report.mjs \
  docs/hn-demo/audits/install-<run-id>/codex-install-audit-<run-id>.json
```

For the route-focused Docker live-AI proof, require the live observer evidence
even when broader install-audit blockers are present:

```bash
node docker/hn-install-audit/validate-report.mjs \
  --require-live-observer-route \
  docs/hn-demo/audits/install-<run-id>/codex-install-audit-<run-id>.json
```

Run state-manager unit checks without Docker:

```bash
node docker/hn-install-audit/validate-report.mjs --self-test
```

## Security

- Tokens are never baked into images.
- Token-looking values are rejected by `validate-report.mjs`.
- `state_manager` command evidence is sanitized before it is written.
- Reports may mention auth files only as redacted evidence labels.
- If host auth is absent, unusable, or Claude reports `Not logged in`, the lane
  is `FAIL`, `SKIPPED`, or `LOGIN_REQUIRED`, not `PASS`.

Interactive OAuth/device-code login is intentionally kept outside the automated
run. Log in once into a dedicated auth home, then pass that directory with
`--claude-auth-home` for repeatable release checks. If that auth cannot be
mounted on the host, use a named kept Claude container and treat the container
home as debug-only auth state until the validation is complete; set
`--auth-mode CONTAINER_PERSISTED_LOGIN` so the report and validator do not
expect mounted host auth files.
