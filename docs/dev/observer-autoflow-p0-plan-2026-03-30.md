# Observer Auto-Flow P0 Plan (2026-03-30)

## Purpose

Record the recommended next iteration mode after the host-governed workflow baseline was stabilized.

This plan assumes:

- Observer should not directly patch product code during the next round
- auto-flow should be allowed to repair and improve itself first
- Observer should intervene only when the workflow cannot safely recover through its own governed chain

## Current Baseline

The live baseline is already materially better than earlier rounds:

- full chain has run:
  - `coordinator -> pm -> dev -> test -> qa -> gatekeeper -> merge -> deploy`
- deploy smoke previously reached `all_pass = true`
- host-side governance and executor are now the active operating mode
- Observer recovery APIs exist for graph runtime restoration

The most important remaining gaps are still:

1. missing role spec documents for:
   - `dev`
   - `test`
   - `qa`
   - `gatekeeper`
2. role-based memory contract is not yet fully formalized in docs + code
3. Dev-stage node gating needs to be tightened again after recovery rounds
4. QA / Gatekeeper are still contract-driven more than trace-driven

## Decision

For the next iteration, Observer should act as:

- task decomposer
- queue governor
- hold / release reviewer
- evidence reviewer
- escalation point only when auto-flow stalls or risks policy drift

Observer should not act as:

- primary code author
- direct fixer for normal workflow defects
- silent bypass around version gate, node gate, or role policy

## Recommended P0 Execution Order

### Phase 1. Documentation Contract Round

Goal:

- let the workflow itself produce the missing single-source role docs before deeper code-policy tightening

Governed task set:

1. create a PM task for role-spec completion
2. let Dev produce the missing docs:
   - `docs/dev-rules.md`
   - `docs/test-rules.md`
   - `docs/qa-rules.md`
   - `docs/gatekeeper-rules.md`
3. let Test / QA / Gatekeeper review doc completeness against existing runtime behavior

Expected output:

- role docs that explicitly define:
  - inputs
  - outputs
  - allowed tools
  - writable scope
  - verification duties
  - retry behavior

Observer involvement:

- review PM scope before release from hold
- reject only if the task tries to mix role-spec work with unrelated runtime changes

### Phase 2. Memory Contract Round

Goal:

- let the workflow formalize role-based memory behavior before Observer tightens policy in code

Governed task set:

1. create a PM task focused only on memory contract formalization
2. require the chain to define:
   - stable fields:
     - `module_id`
     - `kind`
     - `content`
     - `structured`
     - `task_id`
     - `chain_stage`
     - `related_files`
     - `validation_status`
     - `supersedes`
   - which role may write which memory kinds
   - which role may only read or propose cleanup
3. let the workflow propose both:
   - documentation changes
   - minimal enforcement changes

Expected output:

- a docs-first contract
- a minimal code enforcement layer that matches the docs
- tests proving the enforcement does not break current chain writes

Observer involvement:

- reject if the task jumps straight to heavy refactor without first pinning the contract
- reject if the proposal silently broadens write rights for high-risk roles

### Phase 3. Node-Gate Re-tightening Round

Goal:

- restore blocking behavior only after the previous two contracts are clearer

Governed task set:

1. create a scoped task to assess current relaxed gate points
2. tighten Dev-stage node gate from log-only back toward blocking
3. run one governed smoke after the change

Expected output:

- explicit identification of each relaxed gate
- decision for each gate:
  - restore now
  - keep temporary
  - defer with reason

Observer involvement:

- review any change that can block the full chain
- require a replay or smoke before approving full tighten-up

### Phase 4. QA / Gatekeeper Trace Upgrade Round

Goal:

- move acceptance from mostly contract-driven review toward requirement-evidence trace

Governed task set:

1. first add minimal `requirement_coverage`
2. then add minimal `acceptance_trace`
3. only after those artifacts exist, tighten QA and Gatekeeper prompts / checks

Observer involvement:

- do not approve a one-shot “make QA graph-driven” patch
- require staged delivery with intermediate artifacts

## Observer Operating Rules For This Round

Observer should approve auto-flow continuation when:

- the task is narrow and contract-aligned
- the change stays inside the declared phase
- tests or replay evidence are included
- no critical gate is silently bypassed

Observer should hold or reject when:

- the task mixes multiple P0 themes in one patch
- the change weakens version gate or node gate without explicit decision record
- the proposal replaces governed flow with manual operator shortcuts
- the workflow proposes broad policy changes without matching docs

Observer should directly intervene only when:

1. queue progression is broken and auto-flow cannot continue
2. governance policy state is corrupted and recovery APIs are insufficient
3. a high-risk release or destructive action needs manual decision
4. repeated self-repair attempts converge on the wrong policy direction

## Recommended Task Prompts

### Prompt A. Role Spec Round

`Read docs/dev/session-handoff-2026-03-30-late.md and docs/dev/workflow-gap-assessment-2026-03-30.md. Complete the missing role specifications for dev/test/qa/gatekeeper as single-source docs under docs/. Keep behavior aligned with the current host-governed workflow baseline, do not expand scope into unrelated runtime changes, and include inputs/outputs/tools/writable-scope/verification/retry rules.`

### Prompt B. Memory Contract Round

`Read docs/dev/session-handoff-2026-03-30-late.md, docs/dev/workflow-autonomy-roadmap.md, and the current memory-related governance code. Formalize the role-based memory contract in docs first, then propose or implement only the minimal enforcement needed to match that contract without breaking the current chain. Include tests for allowed and denied writes by role.`

### Prompt C. Node Gate Round

`Assess which Dev-stage node gates are still relaxed from recovery mode, document the remaining relaxations, and restore the minimum safe subset back toward blocking mode. Keep version gate enabled, avoid unrelated changes, and prove the result with a governed smoke or replay.`

## Review Checklist For User Decision

Choose the next round based on the risk you want to take:

1. lowest risk:
   - run Phase 1 only
   - produce role docs through auto-flow
2. balanced:
   - run Phase 1, then Phase 2
   - formalize docs first, then minimal memory enforcement
3. higher risk:
   - after Phase 1 and 2 complete, allow Phase 3 gate re-tightening

Recommended choice:

- option 2

Reason:

- it preserves the Observer-first discipline
- it gives auto-flow a fair chance to improve itself
- it creates clearer contracts before re-tightening blocking governance

## Success Criteria For The Next Observer-Led Round

This round should be considered successful if all of the following are true:

1. Observer does not directly patch runtime code
2. auto-flow completes at least one P0 contract-improvement round
3. the resulting artifacts are written into docs and verified by the chain
4. any code-policy tightening is justified by a written contract first
5. no safety gate is weakened to “make progress”
