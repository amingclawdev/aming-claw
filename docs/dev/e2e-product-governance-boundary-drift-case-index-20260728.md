# Product-governance boundary drift E2E case index — 2026-07-28

This index records the three isolated zero-bypass cases that motivated
`AC-CONTRACT-RUNTIME-SCOPE-INSUFFICIENCY-HANDOFF-VERDICT-BOUNDARY-R1-20260728`.
They establish the pre-fix boundary; fresh post-fix E2E must repeat all three
before the recovery umbrella is updated.

| Case | Isolated project | Evidence | Required fixed behavior |
|---|---|---|---|
| Pre-implementation scope revision | `daily-planner-lite-20260728224416-2639d993` | `req-d06d49ebd401` failed with only an omitted/generated `fence_token` mismatch | An omitted fence preserves the persisted same-runtime fence. A worker can append the canonical scope request; observer may then submit one explicit clean same-runtime authority revision. |
| Post-implementation expansion | `daily-planner-lite-20260728224419-c3e57bf0` | `req-9d809a840b8b` correctly returned `runtime_context_scope_revision_after_implementation` | Keep the rejection and return an executable fresh/rework disposition; never widen in place. |
| QA NO-PASS and linked discovery | `daily-planner-lite-20260728224422-62bf9805` | QA graph `req-696477a42d1e`; QA NO-PASS `req-f0b66aeb5ca9`; observer verdict attempt rejected `req-79d912490e99`; linked row filed `req-fd56c04da089` | QA remains sole verdict author. Observer may route rework or file a bounded linked row without changing the source verdict or evidence. |

Related graph traces:

- `gqt-20260728-bb0b51538b`
- `gqt-20260728-720d35cc39`
- `gqt-20260728-163fa89c6d`

Fixed E2E acceptance:

1. Use three fresh isolated environments and zero bypass.
2. Prove the worker/QA request is bound to project, backlog, task, runtime or
   candidate identity and does not mutate authority.
3. Prove pre-implementation same-runtime revision and post-implementation
   fresh/rework routing.
4. Prove QA NO-PASS remains immutable while observer files the linked row.
5. Update umbrella
   `AC-RC-RECOVERY-ZERO-BYPASS-HAPPY-PATH-HOT-WINDOW-R1-20260723` only after
   all three fixed cases are jointly satisfiable.
