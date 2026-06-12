# Onboarding Cold-Probe Rubric

Use this rubric to re-run cold onboarding probes after launcher or bootstrap
changes. The goal is to verify that a user can invoke one explicit onboarding
entry, land in the right state branch, and reach first value without accidental
project mutation.

Acceptance target: each probe scores >=4/5.

Historical baselines from
`BUG-NEW-COMPUTER-FIRST-RUN-ONBOARDING-GAPS-20260526`:

- Plugin checkout probe: 3/5.
- Fresh temporary project probe: 2/5.

## Probe A: Plugin Checkout

Purpose: make sure onboarding refuses to bootstrap the Aming Claw plugin checkout
as if it were the user's target project.

Setup:

```bash
cd /path/to/aming-claw
git status --short
```

Cold prompt:

```text
/aming-claw:aming-claw-launcher
Onboard this workspace to Aming Claw.
```

Expected behavior:

- The launcher is the single entry; no second onboarding skill is mentioned.
- If governance is offline, it shows `aming-claw launcher` and `aming-claw start`
  and waits for the user.
- If governance is running and the checkout is detected, it refuses bootstrap
  because plugin/runtime artifacts such as `.claude-plugin/`, `.codex-plugin/`,
  `.mcp.json --project aming-claw`, or `shared-volume/codex-tasks/` indicate the
  plugin checkout, not a target project.
- It points to the dashboard for the selected project when one exists.
- It runs or asks for one graph query and reports the real audit `trace_id`.
- It files or asks to file the first backlog row only after graph evidence.

## Probe B: Fresh Temporary Project

Purpose: make sure a new target project follows the safe Lane 1 first-run
bootstrap path.

Setup:

```bash
tmp_root="$(mktemp -d)"
mkdir -p "$tmp_root/fresh-aming-probe/src"
cd "$tmp_root/fresh-aming-probe"
git init
printf 'print("hello")\n' > src/app.py
git add .
git commit -m "seed fresh probe"
```

Cold prompt:

```text
/aming-claw:aming-claw-launcher
Initialize this project for Aming Claw governance.
```

Expected behavior:

- The launcher recognizes governance offline, unregistered, or already
  registered state instead of giving a generic install tutorial.
- For an unregistered workspace, it asks the user to confirm excludes before
  graph build. Defaults include `node_modules`, `dist`, `build`, `.expo`,
  `.next`, and `coverage`; project-specific generated, vendored, nested,
  fixture, scratch, or downloaded asset roots are reviewed explicitly.
- It checks for a clean git worktree before bootstrap.
- It uses the Lane 1 dashboard/API bootstrap path from `docs/onboarding.md` and
  does not recommend old ungated CLI or DB side doors.
- It sets expectation that graph build may take a bit and should be watched in
  Projects or Operations Queue until an active snapshot or actionable error.
- It finishes with dashboard URL, one graph query with trace id, and the first
  backlog row.

## Scoring

Score each probe from 0 to 5:

| Point | Criterion |
| --- | --- |
| 1 | Explicit entry: `/aming-claw:aming-claw-launcher` or `aming-claw launcher` deterministically reaches onboarding; no competing skill or hidden intent guess. |
| 1 | Correct state branch: offline start/verify, running but unregistered, or already registered. |
| 1 | Safety gates: plugin-checkout refusal where applicable, exclude review, clean-worktree check, and Lane 1 first-run bootstrap path. |
| 1 | Fixed first-value ending: dashboard URL, one audited `graph_query` with real `trace_id`, then first backlog row. |
| 1 | Progressive disclosure: jargon is translated and full schema is linked to `docs/onboarding.md` instead of inlined. |

Pass: 4/5 or 5/5. Anything below 4/5 needs a follow-up backlog row before the
onboarding change is considered review-ready.

## Self-Review Template

```text
Probe A score: __/5
Evidence:
- Entry:
- State branch:
- Safety gates:
- Fixed first-value ending:
- Progressive disclosure:

Probe B score: __/5
Evidence:
- Entry:
- State branch:
- Safety gates:
- Fixed first-value ending:
- Progressive disclosure:

Blockers or follow-up backlog:
```
