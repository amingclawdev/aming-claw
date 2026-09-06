# Stable external probe intake

Use this checklist after the [AC-dev promotion handoff](ac-dev-promotion-handoff.md)
has completed normally. It describes normal acceptance criteria and grants no
current promotion or probe authority. Bypass, no-PASS, and WAIVED outcomes are
not normal promotable PASS evidence.

## Accept the promoted owner

- [ ] The source is project `aming-claw` on port `40008`. Its handoff records
  WIP=1, an exact clean candidate commit, a loaded current and non-stale runtime,
  an exact active full graph with zero pending reconcile, role-distinct QA, and
  a normal FIXED source row before promotion.
- [ ] Public acceptance references identify that candidate and its normal
  evidence. A prerequisite repair, another row's PASS, a bypass, no-PASS, or
  WAIVED disposition does not satisfy this handoff.
- [ ] Promotion evidence identifies the exact owner commit promoted to stable
  port `40000`. `promoted_owner_commit` equals `candidate_commit`, using the
  full immutable commit ID; a branch name, tag, short hash, or newer checkout
  is insufficient.
- [ ] The loaded stable owner is that exact promoted commit on port `40000`.
  Verify the running owner, not only the checkout on disk. Missing or mismatched
  promotion or loaded-commit evidence stops intake and returns the handoff to
  AC-dev.

## Portable handoff record

Use the same eight field names as the companion checklist. Keep one record per
external probe project; the source and promoted-owner fields remain identical
across the three records.

| Field | Required meaning |
| --- | --- |
| `source_project_id` | `aming-claw`, the owner project in AC-dev. |
| `source_port` | `40008`. |
| `candidate_commit` | Full immutable commit ID of the clean, accepted AC-dev candidate. |
| `source_acceptance_refs` | Public-safe locators or hashes for the source row, normal acceptance, QA, runtime, graph, and exact promotion evidence. |
| `promoted_owner_commit` | Full owner commit ID actually promoted to stable; exactly matches `candidate_commit`. |
| `target_port` | `40000`. |
| `external_project_id` | A fresh Daily Planner project identity dedicated to this probe. |
| `probe_lane` | Direct Main, MF Parallel single-row, or MF Batch multi-row. |

Portable template; replace placeholders with verified public-safe values:

```yaml
source_project_id: aming-claw
source_port: 40008
candidate_commit: "<full-candidate-commit>"
source_acceptance_refs:
  - "<public-source-acceptance-locator-or-hash>"
  - "<public-promotion-evidence-locator-or-hash>"
promoted_owner_commit: "<same-full-promoted-commit>"
target_port: 40000
external_project_id: "<fresh-daily-planner-project-id>"
probe_lane: "<one-of-the-three-probe-lanes>"
```

References locate evidence; they do not transfer authority or import the
referenced objects. Do not copy database, graph snapshot, session, route,
ContractRuntime, timeline, backlog, or credential bytes between dev and stable
worlds. Public IDs, status summaries, and hashes are the handoff boundary.
Create each external project's own governance state through its normal stable
entrypoint; do not clone a dev world's state or reuse a previous probe identity.

## Run three isolated Daily Planner probes

- [ ] Prepare three fresh external Daily Planner project identities, one for
  each lane below, distinct from `aming-claw` and from one another. Keep their
  workspaces, application data, backlog rows, and execution evidence isolated.
- [ ] Bind every probe to the same verified `promoted_owner_commit` on port
  `40000`. A change to the owner commit requires a new promotion handoff and
  fresh probe identities; do not combine results from different owner commits.
- [ ] Use each project's current normal workflow and record its actual result
  independently. Each probe must reach normal FIXED with its own required QA
  and acceptance evidence; bypass and WAIVE cannot count as probe success.

| Probe lane | Isolated exercise | Completion evidence |
| --- | --- | --- |
| Direct Main | One bounded Daily Planner change through the normal Direct Main workflow in its dedicated project. | Its own exact commit, required verification, and normal FIXED result. |
| MF Parallel single-row | One Daily Planner backlog row implemented by bounded workers with disjoint ownership. | Its own worker and integration evidence, required QA, and normal FIXED row. |
| MF Batch multi-row | Multiple Daily Planner backlog rows through the normal MF Batch workflow. | Its own per-row and batch integration evidence, required QA, and normal FIXED results for all included rows. |

All three results are required. A passing lane cannot substitute for a missing,
blocked, waived, or failed lane. Keep the public result summary bound to its
external project, probe lane, and exact promoted owner commit.

## One process correction or a true block

A retryable process correction is a known input or sequencing mistake for which
the current workflow explicitly offers a correction and the rejected attempt
is confirmed not to have written state. Record the original rejection, reason,
the one correction, and its actual retry result in the affected probe's own
evidence. Preserve the same project, lane, owner commit, and authorized scope.
The correction is not a PASS; only the subsequent normal result can satisfy
acceptance. Never replay an ambiguous write: establish its actual outcome first.

A repeated failure after that one correction, a missing applicable recovery,
unresolved write outcome, owner mismatch, failed required check, or need to
change owner source or governance behavior is a true block. Stop the affected
lane, preserve its existing evidence, and return a public-safe failure summary
and evidence locators to AC-dev for repair. Do not repair the owner on stable,
rotate identities to hide the failure, weaken a gate, or relabel the result PASS.

Keep any unaffected lane's result separately, but withhold complete intake
acceptance while a lane is blocked. After repair reaches normal FIXED, return
through the [AC-dev promotion handoff](ac-dev-promotion-handoff.md) and use a new
exact promotion and fresh isolated probes. This document does not perform that
repair, promotion, or probe run.
