# MF no-PASS QA authority

This row documents the close-satisfying shape for a bounded MF QA result that
is non-green only because the candidate reproduces the exact base failures.
It does not create, imply, or substitute for an overall release PASS.

The authenticated QA submission must bind the exact DB-verified QA graph
scope: `base_commit_sha`, `candidate_commit_sha`, and the candidate commit at
top level must agree. `candidate_new_failures` must be `0`; candidate-specific
issues and candidate-only failure identities must be empty. Base and candidate
non-green failure identity lists must be non-empty, duplicate-free, and
identical. Counts must equal those lists. Every overall-release PASS field must
be false, and the full-suite claim must be `not_claimed`.

The server persists the canonical evidence at
`artifact_refs.external_no_pass_baseline_ledger` with this shape:

```json
{
  "schema_version": "contract_runtime.external_no_pass_baseline_ledger.v2",
  "server_normalized": true,
  "base_commit_sha": "<exact full base commit>",
  "candidate_commit_sha": "<exact full candidate commit>",
  "base_failure_identities": ["<stable failure identity>"],
  "candidate_failure_identities": ["<same stable failure identity>"],
  "base_reproduction": {
    "reproduced": 1,
    "total": 1,
    "failure_identities": ["<same stable failure identity>"]
  },
  "candidate_suite_counts": {
    "baseline_known_non_green": 1,
    "failed": 1,
    "passed": 1
  },
  "candidate_new_failures": 0,
  "candidate_specific_issues": [],
  "no_pass_claim": true,
  "overall_release_pass_claimed": false,
  "refs": ["<DB-verified QA graph trace or durable test evidence ref>"]
}
```

Future submissions are normalized to this ledger only when authenticated
provenance, graph scope, commits, identities, counts, and no-PASS fields all
match; otherwise submission fails closed. An older immutable accepted line may
be read as equivalent only when its server-authenticated QA provenance and
DB-verified graph tuple prove the same commits, its base and candidate failure
node IDs are identical, candidate-new failures are zero, and it explicitly
claims no overall PASS. Compatibility recognition never rewrites history and
never synthesizes PASS.
