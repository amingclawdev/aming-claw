# Contract-Driven Governance + Event-Sourced Role-Scoped Runtime Context

Status: **DESIGN DRAFT for operator review** (2026-06-12). Foundation slice in progress. Backlogs: `AC-RUNTIME-CONTEXT-EVENT-SOURCED-ROLE-PROJECTION-20260612`, `AC-RUNTIME-CONTEXT-CONTROL-PLANE-FOUNDATION-20260613`.

2026-06-13 foundation slice: the existing runtime context service now exposes
an action/control-plane projection derived from lane plan, gate inputs, route
identity, content-address, and close-gate views. Permission-tree,
capability-subtree, and `granted_subtree_root_hash` hardening remain deferred to
the next layer.

This converges several open rows into one architecture: `AC-MF-PLAN-SERVICE`, `AC-WORKER-ATTESTATION-TRUST-ROOT`, `AC-WORKER-SELF-ATTESTATION` (FIXED), route-context friction, the permission-tree idea, and the `bypass_timeline_gate` / observer-unconstrained class of defects.

---

## 1. Problem & drift (with evidence)

Two long-running symptoms share one root:

- **Friction**: the observer hand-prepares a ~100 KB `launch_text` and pushes it; the worker echoes 13 fields back. Last lane: the worker took 6 startup attempts.
- **Weak trust root / multi-principal collapse**: the observer can file the worker's evidence indistinguishably from the worker; gates are black-list patches (surrogate, close-exit, `bypass_timeline_gate`, self-waiver) that we keep chasing.

**Root**: the observer is the one role that is *not* constrained by a contract. Timeline evidence (aming-claw governance.db, 2026-06-12):

| role | events | note |
|---|---|---|
| observer | 2658 | 5x the worker |
| worker | 522 | |
| qa | 153 | |
| judge | 54 | |

The observer is recorded authoring evidence that belongs to other roles: `implementation` (31), `mf_subagent.startup` (6), `mf_sub_worker_progress` (24), `implementation_patch` (6), `observer_implementation` (5), and acts in the `implementation` phase 81 times. That is the multi-principal collapse, in data.

---

## 2. Core model: contract-driven governance

Shift from **event-driven + black-list gate** ("append events, gate checks N required events exist") to **contract-driven + white-list gate** ("the observer instantiates a contract; each event fulfils one contract clause; the gate validates every event against the clause it claims to fulfil — is this the role/action that clause permits in this state?"). Only declared actions are legal; everything undeclared is denied by default.

Two contract layers (this resolves the recursion "who constrains the observer's contract?"):

- **Meta-contract** (system-owned, the observer CANNOT change): the roles, the lane state machine, and **each role's legal-action white-list per state** (incl. the observer's). Shipped as a config file (§5), versioned with the code.
- **Lane contract** (the observer instantiates): the concrete script for one lane — clauses = expected events, each bound to a role + state. MUST conform to the meta-contract. **Content-addressed + append-only** (so the observer cannot append an event then edit the contract to legalise it; each event references the contract revision hash it fulfilled).

The gate validates: (a) the lane contract conforms to the meta-contract; (b) each event conforms to the clause it fulfils; (c) the role that authored the event is the role that clause permits (verified via the access audit, §3).

**The observer becomes a governed principal.** Its actions are events fulfilling observer clauses, validated like everyone else's. Self-fix-and-close / bypass / on-behalf-filing / self-waiver are simply not in the observer's white-list → denied. The dozen separate gates we built become one mechanism: declare legal actions, validate each.

---

## 3. Runtime context: event-sourced, role-scoped state projection

Runtime context becomes the single, server-held projection that every agent reads through a role lens. State is **folded from the append-only event log, never hand-filled**.

Layers:

1. **Event log** (exists): append-only `task_timeline_events` = source of truth.
2. **State projection**: `fold(events)` → current lane state per the meta-contract state machine. Deterministic, recomputable, auditable. "What's missing" = clauses of the current/next state not yet fulfilled = the **plan view** (subsumes the plan service).
3. **Content-addressed store**: projection + contract nodes are Merkle nodes (content + hash); a revision = root hash. (`revision_id` becomes the content hash — currently a random `crev-` uuid; `_canonical_contract_hash` already exists.)
4. **Role-scoped access**: nodes carry a role-visibility tag; the projection served to a principal is filtered by role. At dispatch the **server** mints a worker-role-only nonce node the observer did not author and cannot read; the worker folds it into its submitted hash, so the observer cannot recompute the worker's hash.
5. **Access audit (the trust anchor)**: the central service logs every access `(principal, role_used, nodes_read, hash_submitted, time, lane)`. The trust root is not "the credential is secret" but "any access is logged" — an observer filing the worker's hash either never read the worker-only node as worker (no provenance) or read it under an anomalous role (flagged).
6. **Capability = subtree**: the granted subtree root hash IS the worker's read/edit/query boundary, unifying `fence_token` + `owned_files` (flat today) into one content-addressed capability; Merkle one-wayness gives down-only authority.

### Trust root — defense in depth (no single layer is absolute under shared process)

| layer | proves | mechanism |
|---|---|---|
| L1 content hash | read the real content | server recomputes from store |
| L2 role secret | read AS the worker role | observer lacks the nonce → cannot compute the hash |
| L3 access audit | this session read it, nobody anomalous did | server log (unforgeable by the AI) |
| L4 transcript / independent session | an independent subagent process existed | `AC-WORKER-SELF-ATTESTATION` (complementary) |

Implementers MUST treat the trust root as the *combination*; the prior attempt failed by making it a single text-match layer.

---

## 4. Meta-contract: role × state legal-action white-list (from the research)

States: `ALLOCATED → CONTRACT_READY → READ_RECEIPT → WORKER_STARTED → IMPLEMENTING → REVIEW_READY → QA_VERIFYING → {QA_PASSED | QA_FAILED} → MERGED → RECONCILED → CLOSED`, plus branches `BLOCKED` and `HOTFIX` (§6).

Legal actions are a white-list; **anything not listed is denied**. The FORBIDDEN column makes the historically-observed violations explicit.

### observer
| state | legal (white-list) | forbidden (hard red lines) |
|---|---|---|
| ALLOCATED..CONTRACT_READY | mint route token, build lane contract, allocate branch, prepare runtime text, dispatch worker | author worker evidence; skip contract |
| WORKER_STARTED..IMPLEMENTING | poll, relay worker progress **marked on_behalf** (never as author), record blocker | author `implementation`/`startup`/`worker_progress`/`patch`; edit owned files |
| REVIEW_READY..QA_VERIFYING | dispatch QA, relay QA result **marked on_behalf** | author the QA verdict; self-verify |
| QA_FAILED | record blocker, re-dispatch worker, escalate to judge | self-fix and close; downgrade severity; waive |
| QA_PASSED..MERGED | merge (no-ff), reconcile graph, redeploy | merge before QA pass; skip reconcile |
| CLOSED | close after all clauses fulfilled | close with unfulfilled/forbidden clauses; `bypass_timeline_gate`; self-waiver; self-clear a judge blocker |
| identity recovery | `route.identity.supersede` / `cleanup` under pinned canonical identity | mint a forked identity to launder evidence |

### worker (mf_sub)
| state | legal | forbidden |
|---|---|---|
| READ_RECEIPT | record own read receipt (precedes any counted evidence) | post-hoc receipt |
| WORKER_STARTED | record own startup with real session UUID + transcript + submitted subtree hash | surrogate/self-asserted identity; agent_id≠allocation_owner without server-registered host-adapter |
| IMPLEMENTING | graph_query as mf_sub (own fence), edit only owned subtree, commit with Chain trailers | edit outside granted subtree; merge/push; close; reconcile |
| REVIEW_READY | submit review_ready with test results | self-merge; self-QA |

Worker implementation evidence append is runtime-context-native. After a
successful finish gate, an `mf_sub` worker appends implementation evidence with:

```text
POST /api/graph-governance/{project_id}/runtime-contexts/{runtime_context_id}/implementation-evidence
```

The request uses the runtime-context session token in the body with
`runtime_context_id`, `parent_task_id`, `fence_token`, `session_token`, and
`target_project_root`. Workers should supply `changed_files`, `tests` (or
`test_results`), `finish_gate_event_ref`, and optional `summary`/`risk`.
The server derives `actor`, `task_id`, `worker_id`, `worker_slot_id`,
`route_identity`, and `payload.worker_role="mf_sub"` from the verified runtime
context. Workers must omit top-level `role`, `caller_role`, `actor_role`, and
`lane_role`; those fields are stripped before validation. A nested
`route_token_gate.caller_role="observer"` is audit metadata only and must not
override the runtime-context worker role.

### qa
| state | legal | forbidden |
|---|---|---|
| QA_VERIFYING | independent verification by a session distinct from observer AND worker; record verdict | be the same session as worker/observer; edit files |

### judge (optional)
| state | legal | forbidden |
|---|---|---|
| BLOCKED | accept/reject a resolution; clear a judge blocker | be self-cleared by the observer |

### system / service-router
Emits route/service events; not an AI principal; cannot be impersonated.

> This table is the heart of the model and is itself a config file (§5). The FORBIDDEN entries are exactly the defects we chased one by one (surrogate, close-exit, bypass, self-waiver, on-behalf authoring, identity fork) — now denied by construction.

---

## 5. Contract as a config file

Meta-contract (system-owned, code-versioned), sketch:

```yaml
# meta-contract.v1.yaml  (system-owned; lane contracts must conform)
schema_version: meta_contract.v1
roles: [observer, worker, qa, judge, system]
states: [ALLOCATED, CONTRACT_READY, READ_RECEIPT, WORKER_STARTED, IMPLEMENTING,
         REVIEW_READY, QA_VERIFYING, QA_PASSED, QA_FAILED, MERGED, RECONCILED, CLOSED,
         BLOCKED, HOTFIX]
transitions:
  - {from: ALLOCATED,   on: lane_contract_committed, to: CONTRACT_READY,  by: observer}
  - {from: WORKER_STARTED, on: implementation,        to: IMPLEMENTING,   by: worker}   # only worker may author
  - {from: QA_VERIFYING, on: independent_verification, to: QA_PASSED|QA_FAILED, by: qa}
  # ...
legal_actions:                      # white-list; unlisted == denied
  observer:
    IMPLEMENTING:   [poll, relay_progress_on_behalf, record_blocker]
    QA_FAILED:      [record_blocker, redispatch_worker, escalate_judge]
    CLOSED:         [close_after_all_clauses_fulfilled]
  worker:
    WORKER_STARTED: [record_own_startup]            # requires session_uuid + transcript + subtree_hash
    IMPLEMENTING:   [graph_query_mf_sub, edit_owned_subtree, commit_chain_trailers]
forbidden_always:                   # hard red lines, any state, any role-instance
  observer: [author_worker_evidence, self_fix_and_close, bypass_timeline_gate,
             self_waiver, self_clear_judge_blocker, surrogate_startup, fork_identity_to_launder]
evidence_binding:
  every_event_must:  [reference_contract_revision_hash, declare_fulfilled_clause, be_authored_by_clause_role]
```

Lane contract (observer instantiates per lane; content-addressed, append-only):

```yaml
# lane contract instance (conforms to meta-contract.v1)
schema_version: lane_contract.v1
conforms_to: meta_contract.v1@<hash>
lane: {project: aming-claw, backlog_id: AC-..., task_id: mfsub-...}
clauses:                            # = the expected events; each = role × state × action
  - {id: c1, state: CONTRACT_READY, role: observer, action: dispatch_worker, owned_subtree: <root_hash>}
  - {id: c2, state: WORKER_STARTED, role: worker,   action: record_own_startup}
  - {id: c3, state: IMPLEMENTING,   role: worker,   action: edit_owned_subtree, files: [...]}
  - {id: c4, state: QA_VERIFYING,   role: qa,       action: independent_verification}
  - {id: c5, state: CLOSED,         role: observer, action: close_after_all_clauses_fulfilled}
```

The gate, per event: resolve the fulfilled clause → check (role matches, action in the white-list for that state, contract-revision-hash current, authoring principal == clause role via access audit). Reject otherwise.

---

## 6. Hotfix contract (the logged escape hatch)

Early on the system will get stuck in ways the meta-contract did not anticipate. A **hotfix contract** gives the observer bounded extra freedom **only with a durable audit trail** — the opposite of the silent `bypass_timeline_gate` backdoor (which this design removes).

- A distinct meta-contract profile `HOTFIX` with a wider observer white-list, **entered explicitly** (a `hotfix.entered` event with a human-supplied reason), **time/scope-boxed**, and **fully logged**: every action taken under HOTFIX is tagged `under_hotfix=true` with the reason, and the projection surfaces a standing "this lane used hotfix" banner.
- Difference from a backdoor: it does NOT skip gates silently; it records a contract switch + every subsequent action under it. An auditor (or a later judge pass) sees exactly what extra latitude was used and why. The freedom is real but never invisible.
- Suggested guard: HOTFIX entry should still require something the AI cannot self-supply at scale (operator confirmation or a rate-limited / flagged-for-review entry) — but the MVP can accept "any entry, fully logged, surfaced for review", since the value early on is unblocking, and the audit makes misuse detectable after the fact. Decide the guard strength in review (open question §9).

This satisfies "give the observer freedom when fully stuck, as long as it leaves an audit trail."

---

## 7. Data flow (end to end)

1. Observer appends intent + commits a lane contract (conforms-to meta-contract); projection root advances; server mints the worker-role nonce node.
2. Observer dispatches: hands `(runtime_context_id, revision-root-hash, granted-subtree-path)` + worker credential — **not** a launch_text.
3. Worker fetches its subtree (incl. nonce) with its worker credential; server logs the access (principal=worker-session, role=worker).
4. Worker works within the subtree; submits ONE `subtree_hash` (incl. nonce) at startup/finish — not 13 echoed fields. The event references the contract clause + revision hash.
5. Gate validates: hash recomputes from store (L1) ∧ computed with the worker nonce (L2) ∧ access log shows this worker session read it as worker, no anomalous read (L3) ∧ (transcript corroborates, L4) ∧ the event fulfils a worker clause the meta-contract permits in this state.
6. Observer reads the role-scoped state snapshot → sees fulfilled vs missing clauses (plan view) → next legal action.

---

## 8. Evolution path (evolve the existing runtime context service, do not rebuild)

The current service already has: content hashes, append-only revisions with `previous_revision_hash`, a worker-fetch endpoint gated on `fence_token`, role-scoped views, `_canonical_contract_hash`.

1. `revision_id` := content hash (already computed; make it the key).
2. Structure the context payload into separately-hashable, role-tagged nodes.
3. `launch_text` → `(runtime_context_id, revision-root-hash, subtree-path)`; worker-fetch becomes primary.
4. `must_echo_fields` (13) → one `submitted_subtree_hash == server_recomputed`.
5. `fence_token` + `owned_files` → one `granted_subtree_root_hash` capability.
6. NEW: role-scoped projection filtering + per-subagent server-minted nonce + access log.
7. NEW: meta-contract config + lane-contract instances + `fold` reducer (state machine) + per-event clause validation.
8. NEW: HOTFIX profile; remove the silent `bypass_timeline_gate`.

Phase suggestion: (P0 prerequisites, independent of this mainline) land `bypass_timeline_gate` removal and the finish-gate attestation-injection fix first. (P1) meta-contract config + per-event clause validation over the *existing* event model (immediate white-list win for observer actions, no storage change). (P2) content-addressed projection + role nonce + access audit (the storage evolution). (P3) capability=subtree + drop launch_text/echo (the friction win).

---

## 8b. Decisions & refinements (operator review round 1, 2026-06-12)

**D1 — Hotfix: any-entry, fully logged, post-hoc audit.** Accepted as MVP (§6). Refinement: "logged" must be *queryable*, not just one timeline row — a standing "lane used hotfix" banner on the projection + a hotfix-usage audit view that a periodic / judge pass reviews. Otherwise the trail drowns in the timeline and the post-hoc audit never actually happens.

**D2 — Credential channel: trust the observer early; defer the worker-only nonce (L2).** Accepted (§3). **Important boundary (do not blur):** "trust the observer" simplifies IDENTITY verification (L2 nonce), it does NOT relax the BEHAVIORAL white-list. The observer is still bound by (a) the meta-contract red lines and (b) the lane contract it self-declared — every observer action is still validated against the white-list. Trust = "the observer acts honestly *as* its role"; still enforced = "its action types stay inside the declared/permitted set". Early trust is acceptable because the operator *is* the early observer, and L3 access-audit + L4 transcript still anchor provenance; add the L2 nonce later to harden. (If this boundary is blurred — "trusted" taken as "unconstrained" — we are back to the multi-principal collapse this whole design fixes.)

**D3 — Flow: template → fill → entry → role/path check → claim → fulfil.** Refines §7:
1. Observer picks a **contract template** (from the meta-contract's templates) and fills in this lane's content → writes the lane contract.
2. Observer hands the subagent the **contract entry (path)** — not a launch_text.
3. The **runtime context service validates the subagent's role + path** on every access (it is the single policy-enforcement point) and **opens the subagent's writable path** (its subtree; everything else read-only / invisible).
4. The subagent **claims the contract** — an explicit `contract.claimed` event ("I will fulfil this"). This is the binding anchor: the gate later checks that the fulfilling events come from the claimer, tying "who fulfilled" to the access audit (L3) + transcript (L4).
5. The subagent **fulfils** the contract by appending events that satisfy its clauses; the projection advances.
Open: is a claim exclusive (one claimer per contract / subtree)? — likely yes for a worker subtree; QA claims a separate verify clause.

**D4 — meta defines action TYPES + roles + red lines; observer COMBINES + self-constrains.** Refines §2/§4/§5:
- The meta-contract is the **vocabulary**: the legal action *types*, which role may perform each, and the `forbidden_always` floor. The observer **cannot invent actions** (only combine the meta vocabulary) and **cannot combine away the red lines**.
- The lane contract is the observer's **combination**, and it includes the observer's *own* clauses — the observer **declares in advance what it will do this lane and is then bound by its own declaration** (a commitment device), on top of the meta floor.
- Net: the observer is governed not by an externally imposed rule but by *its own advance declaration* + an *unremovable floor*. The writer of the contract is also a subject of it.

These hold §9 questions 1 & 2 as DECIDED; question 3 gains the "claim exclusivity" sub-point. Still open below: state-machine exactness, meta-contract change process, access-audit anomaly, consistency, migration.

## 9. Open questions for review

1. **Hotfix guard strength** (§6): accept "any entry, fully logged, surfaced for review" for MVP, or require operator confirmation / rate-limit from day one?
2. **Credential channel** (§3 L2): the worker credential passes through the host(=observer) at spawn. Accept "logged if the observer reads the worker nonce" (audit-anchored), or invest in an observer-untouchable channel?
3. **State machine exactness** (§4): confirm the state set + transition table; how to model parallel sub-lanes (worker + QA concurrently) — sub-states or per-lane slots?
4. **Meta-contract authority**: who may change the meta-contract config, and through what process (it is the root of "the observer is also governed")?
5. **Access-audit anomaly definition** (§3 L3) + binding to the worker session UUID.
6. **Consistency** (§3): snapshot isolation level; observer read-your-writes; projection rebuild cost.
7. **Migration** (§8): in-flight old-model lanes coexisting during rollout; no big-bang.
