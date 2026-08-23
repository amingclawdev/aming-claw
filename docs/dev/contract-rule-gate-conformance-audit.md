# Happy-path Contract / Rule / Gate conformance freeze

Audit root: `AC-HAPPY-PATH-GATE-COHERENCE-FREEZE-P0-20260815`

Bounded successor row: `AC-HAPPY-PATH-GATE-COHERENCE-FREEZE-R2-20260823`

Audited code Position: `5de2f28e8e4cfead4b3451edf9aa0af21f8c6ac3` / `full-5de2f28e8e4c-direct-retry`

Machine-readable map: `docs/dev/contract-rule-gate-map.happy_path.v1.json`

## Outcome

The baseline is frozen without adding a Gate predicate or changing a runtime Rule.

- Direct Main `operator_supervised_direct_main v1 rev2` is the selected source-backed Contract. All eight Contract lines are mapped, including `observer_direct_implementation_exception`, and nine common Rules have lane-local validators. However, rev2 explicitly joins all ten common Rules, including `AC-COMMON-MERGE-ORDERED`; that source Rule requires worker fan-in, deterministic merge order, and append-only merge receipts, while the serial Direct chain exposes no such seam. The audit may not declare an explicitly joined Rule non-applicable or invent a Gate. Disposition: `CONTRACT_UPDATE` through a new source-backed Direct revision before claiming conformance.
- MF Parallel `mf_parallel.v2 rev10` is the selected source-backed Contract and joins the common Rule package explicitly. All fourteen Contract lines and all ten common Rules are mapped within this lane. Prefill, dispatch, read, startup, graph, implementation, commit, finish-time attestation, and finish Gate point to their own temporal validators instead of borrowing the worker-commit validator. Its nominal two-lane fan-out/fan-in, ordered merge, reconcile, independent QA, and terminal close chain remain coherent. Disposition: `NO_RULE_CHANGE`.
- The failed fresh Parallel attempt `cex-contract-dead-initial-join-recovery-e0469032273d2ba69477` stopped before worker read/startup/graph/implementation. It is a DC-042 Entrance/host realization failure: a one-shot process-local credential could not survive the tool-response boundary. It created no candidate custody, no merge authority, no PASS, and no bypass. Disposition: `TRANSPORT_ONLY`; it is not authority for a Contract or Gate change.
- MF Batch has an implemented/template-backed topology in `mf_batch_parallel.v1`, but no selected source-backed parent Contract definition explicitly joins the common Rule package. The merge-order, exact-graph, and close-integrity common Rules are therefore recorded only as unjoined candidates; they do not appear in authoritative batch `rule_refs`, and the server may not infer the missing join. The row-scoped MF Parallel child Contracts remain authoritative for their own rows. Disposition: `CONTRACT_UPDATE` before claiming a source-backed parent-Contract warranty.
- A fresh same-release three-lane warranty has not yet been produced at this baseline. Disposition: `INSUFFICIENT_EVIDENCE`, with execution order fixed to Direct Main → MF Parallel → MF Batch and parent `WIP=1`.

The predecessor freeze execution `cex-direct-main-5fbb6b111123caac6376` remains an immutable terminal no-PASS generation. The Direct timeline crash-window adapter was diagnosed and independently verified in bounded row `AC-CONTRACT-LINE-BYPASS-C6DCCA78048BF3AE` at `5de2f28e…`; it did not change Contract, Gate, bypass, waive, or retry-generation semantics. Direct retry policy therefore requires this fresh bounded R2 row instead of resuming or backfilling the predecessor.

This is a product audit and release-control artifact. It does not claim an independent Charting Loop conformance certificate or a combined-environment warranty.

## Authority boundary

The authority stack is deliberately small:

1. Position is an exact WorldRef plus admitted Facts.
2. Direction is the frozen objective and the complete applicable Rule closure projected at that Position.
3. Only the selected source-backed Contract revision and its explicit version/digest common-Rule join are Rules.
4. Gate validates Facts against those Rules. It cannot invent a Rule, silently repair a Contract, admit a Fact, synthesize evidence, or turn a blocker into Direction.
5. Guide projects the next legal Entrance or a typed refusal from the same Position and Direction. It is not another authority layer.
6. Backlog and timeline preserve work/evidence; neither can amend Rule semantics.

This implements the 08-09 three-lane discussion directly: ContractRuntime is the authority-bearing execution layer; Gate blocks only by validating a named Rule against admitted Facts. A new blocking predicate without a Contract/common-Rule join is an orphan predicate, not a reason to keep extending the mechanism.

## Method and case references

The normative method reference is frozen `charting-loop-method-v8`, `METHOD.md` digest `sha256:85b5a7a8…59446`, with scope datum digest `sha256:bd70498b…987af`. Its registered scholarly provenance is `zenodo-v1`. No Theory v2 source is registered in `theory/VERSIONS.json` at the audited Charting Loop state, so “Theory v2” is not treated as an unpinned authority in this audit.

The redirection is indexed against Drift Gym `cases/INDEX.md` digest `sha256:4b5e5e6e…66f5924`:

- DC-037: a repair must not become legislation by adding a new Gate predicate.
- DC-039: a fresh blocker/backlog row must not rewrite Direction; the freeze root and Direct → Parallel → Batch `WIP=1` sequence are reused.
- DC-042: Guide projection plus one-shot credential consumption was non-atomic at the host boundary. That is an Entrance/execution defect, not a Rule change.
- DC-043/DC-044: Rule conditions and witness time/order must remain granular; a boolean union is not a substitute for per-object and temporal evidence.

## P / D / E / X diagnosis

| Plane | Frozen reading | Consequence |
| --- | --- | --- |
| P — Position | Aming Claw `5de2f28…`, active full snapshot `full-5de2f28e8e4c-direct-retry`, zero pending scope reconcile | All audit claims bind this code WorldRef; historical demo worlds are compatibility witnesses only. |
| D — Direction | Freeze the Rule/Gate baseline under Direct Main, then obtain fresh Direct/Parallel/Batch receipts sequentially | A new blocker does not create a row or mechanism unless the existing Rule closure requires it. |
| E — Entrance | The fresh R2 Direct route and graph-first trace `gqt-20260823-024720bf7b` are valid | The four declared audit files may be changed; no other scope is implied. |
| X — execution | Parallel host continuation lost the process-local one-shot credential before any worker evidence | Preserve the failure as a DC-042 probe. Do not bypass, commit, merge, or modify the Contract/Gate from that probe. |

The allowed redirection outcomes remain exactly `NO_RULE_CHANGE`, `CONTRACT_UPDATE`, or `INSUFFICIENT_EVIDENCE`. `TRANSPORT_ONLY` is a defect-owner disposition within `NO_RULE_CHANGE`, not a fourth Rule outcome.

## Historical happy-path worlds

These are three independent historical worlds. They prove compatibility at their own WorldRefs; they are not one simultaneous release warranty and are not relabelled as current Contract certificates.

| Lane | Project / backlog | Exact commit and graph | Contract evidence at the time | Present use |
| --- | --- | --- | --- | --- |
| Direct Main | `daily-planner-v2-direct-253d7f62-20260808t200221z` / `DP-V2-DIRECT-253D7F62-R1-20260808` | `8a6ef43d…7210`, `full-8a6ef43-a390`, graph `bd0d18ed…2f0f5` | `onboard_route_guide service v1`; no current Direct rev2 certificate | Historical behavior oracle only |
| MF Parallel | `daily-planner-v2-parallel-r3-20260809t050248z` / `DP-V2-PARALLEL-R8-20260809` | `c18af8b3…6e70`, `full-c18af8b-2540`, graph `bdb607af…6405` | `mf_parallel.v2 rev9`, definition `d2902ffb…a9b7e` | Historical compatibility witness |
| MF Batch | `daily-planner-v2-batch-530a9121-20260809t043433z` / two child rows plus coordination row | `17042506…b72ee`, `full-1704250-9d45`, graph `0e3b2475…bc97` | parent route service plus row-scoped `mf_parallel.v2 rev9` children | Historical composite compatibility witness |

The smoke script replays these exact snapshots through their immutable snapshot summaries. It does not require a historical snapshot to remain the project's current active snapshot.

## Current Contract baseline

| Lane | Selected source | Explicit common-Rule join | Coherence result |
| --- | --- | --- | --- |
| Direct Main | `operator_supervised_direct_main.v1.rev2.json`, definition `ea169f8d…5ca2` | resolved, package digest `b811cb54…62603`, server inference false; 9 mapped and joined merge-ordered remains unvalidated | all 8 Contract lines mapped, but rev2 over-applies a fan-in Rule to a serial lane; new Direct revision required |
| MF Parallel | `mf_parallel.v2.rev10.json`, definition `66390c59…04d7a` | resolved, same package digest, server inference false; all 10 mapped lane-locally | all 14 Contract lines map to their temporal validators; coherent fan-out/fan-in, ordered merge, one reconcile, fresh final QA, close |
| MF Batch parent | `mf_batch_parallel.v1.json` template | no source-backed parent Contract join found; 3 common Rules retained as unjoined candidates only | implementation topology is auditable, but parent authority remains a `CONTRACT_UPDATE` gap |

The Batch gap does not invalidate row-scoped MF Parallel child custody. It limits the claim: current code can execute the composite topology, but the parent cannot receive a source-backed Contract warranty until its Rule closure is explicit.

## Gate conformance classes

Each mapped predicate records its validator symbol and implementation digest, admitted Fact selectors, expected/actual result, status, Guide projection, authority class, and disposition.

| Class | Test | Required response |
| --- | --- | --- |
| `missing_validator` | A Rule obligation has no executable validator at a required seam | `FIX`, `ROLLBACK`, or `CONTRACT_UPDATE`; never PASS |
| `under_validation` | Gate accepts less than the full Rule predicate | `FIX` or `ROLLBACK`; never PASS |
| `mis_validation` | Gate uses the wrong Fact, WorldRef, role, order, or authority owner | `FIX`, `ROLLBACK`, or `TRANSPORT_ONLY`; never PASS |
| `orphan_predicate` | Gate blocks on a predicate not joined to the Contract/common package | remove/rollback it, explicitly update the Contract, update only the Guide if it was projection text, or classify it transport-only; never silently promote it |

There are no accepted orphan predicates. The failed host-continuation condition is recorded as an orphan candidate only to make its non-authority explicit: DC-042, `TRANSPORT_ONLY`, no Rule change, no Gate change, and no bypass.

Closure is evaluated independently per lane. A common Rule mapped by Parallel cannot fill the Direct merge-ordered gap, and a template validator cannot fill the missing Batch parent join. Direct's explicitly joined-but-unvalidated Rule is recorded as `CONTRACT_UPDATE`, not as document-level non-applicability. The executable coherence test compares every Direct and Parallel Contract line with that lane's own mappings and pins the temporal validator for Parallel prefill, dispatch, finish-time attestation, and finish Gate, so neither a missing line nor a wrong-time validator can disappear behind an aggregate set union.

## Chain coherence and evidence continuity

The machine map contains the ordered global trace for every lane. Every downstream consume-set is supplied by the previous stage's produce-set, and each trace has a terminal `terminal_fixed` base case.

- Direct Main: route/scope → graph-first → `observer_direct_implementation_exception` → immutable candidate → QA graph → independent QA → current full reconcile → close.
- MF Parallel: `observer_prefill_child_contracts` → atomic two-lane dispatch → worker read/startup/graph/implementation/commit/finish-time attestation/finish Gate → all-lane ordered merge → one canonical reconcile → fresh integration QA → close.
- MF Batch: parent draft/review/preflight → one MF Parallel successor per row → durable integration epoch → ordered merge cursor → one shared final reconcile → protected child closes → atomic coordination close.

Custody, landing, and deployment are separate authorities:

- Candidate custody is the immutable Direct/worker commit plus lineage and scope evidence.
- Canonical landing is the exact Direct canonical commit or append-only ordered merge receipts.
- Runtime deployment is the loaded governance identity matching canonical HEAD.
- Graph authority is an active full snapshot at that exact HEAD.
- Independent QA is role-separated and bound to the exact candidate or reconciled canonical HEAD.

Moving HEAD, stale runtime/graph, partial graph, wrong role/WorldRef, fence escape, or presenting no-PASS/bypass as PASS invalidates the dependent claim.

## Bypass boundary

Bypass is not part of any nominal trace in this audit. Its only legitimate purpose is to keep an already-real AC candidate sedimenting while an append-only dependent block is recorded. It does not create QA PASS, a worker commit, merge authority, reconcile authority, deployment proof, or close authority.

That boundary also explains the recursion observed in the earlier Direct demo. Once a post-implementation Gate began validating graph/candidate linkage, a synthetic block could be carried through one diagnostic generation only if every downstream no-PASS event stayed bound to the same immutable commit and root generation. Treating each downstream refusal as a new block row would recursively create new mechanisms. The demo therefore proved continuity of a no-PASS diagnostic, not a happy-path PASS. Its terminal generation remains immutable; the fresh R2 execution is a Contract-required bounded successor, not a recursive blocker. The present Parallel probe has no candidate at all, so bypass is neither necessary nor legal.

## Dispositions

| Disposition | Use in this audit |
| --- | --- |
| `KEEP` | Direct rev2 and MF Parallel rev10 source-backed chains and validators that enforce their named Rules |
| `FIX` | Reserved for a proved missing/under/mis-validator; none authorized by the failed Parallel host probe |
| `ROLLBACK` | Required if an orphan predicate is found in Gate behavior |
| `CONTRACT_UPDATE` | Direct needs a new source-backed revision that corrects the serial lane's common-Rule applicability; Batch parent needs a source-backed definition and explicit common-Rule join before a parent Contract warranty |
| `GUIDE_UPDATE` | Only for an incorrect projection when the Contract Rule is already explicit; not authorized by this probe |
| `TRANSPORT_ONLY` | DC-042 host/credential continuation failure; no Rule or Gate change |

## Release sequence and exit criteria

Parent scheduling remains `WIP=1`:

1. Commit and independently verify the bounded R2 Direct Main freeze scope without reopening the predecessor no-PASS generation.
2. Under parent `WIP=1`, create and independently verify the minimal source-backed Direct successor revision that removes the unsupported serial merge-ordered applicability; do not add a Gate predicate.
3. Resolve the Batch parent authority gap through its own source-backed Contract update before a parent-Contract warranty, while preserving row-scoped MF Parallel child authority.
4. Deploy one exact owner release candidate and activate one exact full graph snapshot.
5. Obtain fresh Direct Main, MF Parallel, and MF Batch product receipts sequentially at that same exact release candidate.
6. If a combined-environment proof is desired, run it as a separate optional claim. Do not infer it from the three lane receipts.

The freeze root can close only when its declared tests pass, implementation and independent QA evidence bind the exact audit commit, the loaded runtime and active full graph match that commit, and the three fresh lane receipts are present. The three-lane parent can close only after its own remaining declared children/evidence are truthfully resolved. Historical PASS, bypass, waive, or a blocker row cannot substitute for any of those facts.
