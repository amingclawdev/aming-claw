# AC-dev promotion handoff

Use this checklist before handing an accepted Aming Claw source commit to the
[stable external probe intake](stable-external-probe-intake.md).
It describes normal promotion criteria; publishing or completing this document
does not grant current promotion authority. Bypass, no-PASS, and WAIVED evidence
are not normal promotable PASS evidence.

## AC-dev source acceptance

Record public-safe evidence locators for every check against the same exact
`candidate_commit`. Missing, stale, mismatched, bypassed, or waived evidence means
stop before promotion; do not infer acceptance from a healthy service alone.

- [ ] Confirm `project_id=aming-claw` in AC-dev on port `40008`, with `WIP=1`
  for the source work. Do not use stable-world evidence as dev acceptance.
- [ ] Record the full immutable candidate commit, not a branch name or abbreviated
  hash. Confirm the candidate worktree is clean, including untracked files.
- [ ] Confirm the actually loaded dev runtime is current, non-stale, and running
  that exact candidate. Installed files or a matching checkout alone do not prove
  loaded runtime identity.
- [ ] Confirm the active full graph is for that exact candidate and pending
  reconcile is `0`. A stale graph, a candidate-only build, or a running reconcile
  is not the required active full graph evidence.
- [ ] Obtain role-distinct independent QA for the exact candidate. Worker checks,
  observer summaries, and another source row's QA do not replace that assessment.
- [ ] Confirm the source backlog row has a normal `FIXED` close with its required
  acceptance evidence. Audit closure, bypass, no-PASS, and `WAIVED` do not qualify.
- [ ] Have the responsible owner make the separate promotion decision. After
  promotion, verify the exact `promoted_owner_commit` equals the accepted
  `candidate_commit` before stable probe intake; do not silently substitute HEAD.

## Portable handoff identity

Use exactly these shared field names in both checklists. Prepare one handoff
record per isolated probe lane. Placeholder values are not acceptance evidence;
the stable intake supplies a fresh external identity and verifies the promoted
owner commit before starting that lane.

| Field | Value or meaning |
| --- | --- |
| `source_project_id` | `aming-claw` |
| `source_port` | `40008` |
| `candidate_commit` | `<full-clean-accepted-source-commit>` |
| `source_acceptance_refs` | `<public-safe-locators-for-source-row-QA-runtime-graph-and-promotion-evidence>` |
| `promoted_owner_commit` | `<full-verified-promoted-owner-commit>`; must equal `candidate_commit` |
| `target_port` | `40000` |
| `external_project_id` | `<fresh-Daily-Planner-project-id-for-this-lane>` |
| `probe_lane` | `Direct Main`, `MF Parallel single-row`, or `MF Batch multi-row` |

`source_acceptance_refs` contains public-safe locators only, not the referenced
objects. Evidence remains in its owning world. Do not copy any database, graph
snapshot, session, route, ContractRuntime, timeline, backlog, or credential bytes
between dev and stable. Do not include private paths, raw private state, tokens,
or authentication material in the handoff. A locator conveys no write authority.

## Stable intake and failure disposition

- [ ] Pass the identity record to the linked stable checklist for port `40000`;
  stable must verify the exact promoted owner commit before any external probe.
- [ ] Use fresh external Daily Planner project identities for three isolated
  probes: Direct Main, MF Parallel single-row, and MF Batch multi-row. Do not
  reuse dev project state or a different lane's identity or acceptance evidence.
- [ ] Distinguish one recorded retryable process correction from a true block.
  Record the correction and its actual retry outcome; do not silently loop or
  call the correction a PASS. A true block stops the affected lane and returns
  to AC-dev repair; it does not authorize a stable source hotfix or bypass.

## Normal acceptance of this documentation pair

The ordinary workflow uses one MF Parallel single-row contract with exactly two
disjoint bounded workers, one new Markdown file per worker, ordered durable
merge, one canonical exact-HEAD reconcile, role-distinct independent QA, and a
normal `FIXED` close. A document or offline content check is not proof that these
process criteria have been met; bypass and WAIVE are forbidden as success.

This is documentation-only dogfood. It changes no source, test mechanism,
runtime, database, graph policy, Contract, Rule, schema, registry, authority,
lane, service configuration, or stable project data. Promotion and probe
execution remain separate operations; this checklist performs neither.
