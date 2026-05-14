# Manual-Fix Checklist

Canonical source: `docs/governance/manual-fix-sop.md`. This file is only the short session checklist.

## Before Editing

1. Confirm chain/MF route is justified. Routine feature work should use the normal chain when possible.
2. Ensure a backlog row exists with target files, acceptance criteria, and details.
3. Predeclare/start the MF row with an MF id.
4. Capture baselines:
   - `git status`;
   - `version_check`;
   - `preflight_check`;
   - `graph_status`;
   - `graph_operations_queue`;
   - `wf_impact` for target files.
5. Run graph-first discovery and list reused nodes/modules in the working notes or final summary.
6. For new features or user-visible behavior changes, record the E2E impact decision:
   - run or add/update the relevant E2E and record evidence;
   - for dashboard/graph/bootstrap/file-hygiene paths, update the repo-owned fixture artifact first, materialize it into an isolated temp project, then run the E2E against that generated project;
   - for orphan file flows, put the orphan doc/test/config file in the fixture artifact, let the E2E write the governance hint, commit the fixture change, run Update graph, and assert the binding;
   - file a follow-up backlog row when live-AI, DB-mutating, slow, or human-approval E2E is deferred;
   - write `e2e_not_applicable` with a reason for docs-only or non-runtime changes.

## Commit

Stage explicit files only. Use Chain trailers:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <backlog-id>
```

Use `[observer-hotfix]` or `manual fix:` in the subject when this is a true MF bypass.

## After Commit

1. Restart/redeploy changed runtime services when needed.
2. Run `version_check`; require `ok=true`, `dirty=false`, and runtime matching HEAD for runtime changes.
3. Check graph status. If HEAD is ahead of the active graph, run direct Update graph/scope reconcile before telling a dashboard user the graph is current. Explicit pending-scope queueing is legacy/debug only.
4. Rebuild or refresh semantic projection when dashboard semantic state changed.
5. Confirm the E2E impact decision is current, deferred with a backlog row, or explicitly not applicable.
6. Close the backlog row with the commit hash and verification evidence.
