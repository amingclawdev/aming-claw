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
  --ai-prompt-mode required
```

By default the runner mounts the current checkout into the container and uses
`file:///plugin-source` as the install source. To test the public README path
against GitHub instead:

```bash
PLUGIN_REPO_URL=https://github.com/amingclawdev/aming-claw \
docker/hn-install-audit/run-install-audit.sh --host both
```

## Lanes

- `aming-claw-install-audit-codex`: installs Codex CLI in the image, uses a
  fresh `$CODEX_HOME`, and mounts `~/.codex` read-only when present.
- `aming-claw-install-audit-claude`: installs Claude Code CLI in the image,
  uses a fresh `$HOME`, and mounts `~/.claude` plus `~/.claude.json` read-only
  when present.

Each lane has two phases:

1. Feed the README/launcher one-click install prompt to the CLI and verify
   install, skill discovery, MCP tool visibility, and required resource reads.
2. Feed the HN demo prompt and verify the three-fear demo evidence path.

The container also runs deterministic code checks so the final report cannot
claim pass solely because the model said it passed.

The runner attempts every requested lane, then exits non-zero if any requested
lane failed. This keeps the harness useful for CI while still collecting both
Codex and Claude reports from the same run when possible.

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

## Security

- Tokens are never baked into images.
- Token-looking values are rejected by `validate-report.mjs`.
- Reports may mention auth files only as redacted evidence labels.
- If host auth is absent or unusable, the lane is `FAIL` or `SKIPPED`, not
  `PASS`.

Interactive OAuth/device-code login is intentionally out of scope for this
first pass. File a follow-up if Mode B auth reuse is insufficient.
