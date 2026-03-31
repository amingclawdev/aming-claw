# Workflow Autonomy Roadmap

## Goal

Evolve the current workflow from:
- `Observer participates in diagnosis and repair`

to:
- `Workflow self-diagnoses and self-improves by default`
- `Observer mainly monitors, approves high-risk actions, and handles rare exceptions`

This roadmap prioritizes automatic repair and automatic optimization first, then gradually reduces Observer intervention.

## Target End State

The desired operating model is:
- workflow runs full chain automatically:
  - `coordinator -> pm -> dev -> test -> qa -> gatekeeper -> merge -> deploy`
- failures are automatically classified
- workflow defects trigger automatic repair tasks
- repaired workflow changes are validated through replay and regression coverage
- Observer sees summaries, alerts, and approval requests instead of manually tracing logs

## Guiding Principles

1. automation before intervention
   - prefer automatic retry, automatic diagnosis, automatic repair, and automatic replay

2. contracts before autonomy
   - role behavior, memory behavior, graph mapping, and evidence format must be stable before autonomy is expanded

3. evidence before approval
   - QA, Gatekeeper, merge, and deploy should rely on structured evidence, not only free-form model judgment

4. Observer as governor, not executor
   - Observer should gradually move from fixing workflow details to supervising policy, risk, and exceptions

## Current Baseline

The current system already has:
- a working multi-stage chain
- isolated Dev worktrees
- QA and Gatekeeper stages
- isolated merge verification
- host-side deploy with smoke test
- version gate re-enabled
- partial audit and memory persistence

The main remaining gaps are:
- role-based memory rules are not fully fixed
- graph-to-test/doc/scenario mapping is incomplete
- requirement-to-evidence trace is incomplete
- workflow failure classification is still weak
- workflow self-improvement is still mostly driven by Observer

## Phase 1: Stabilize Contracts

### Objective

Make the workflow predictable enough that repeated runs behave consistently.

### Work Items

1. finalize role contracts
- create or complete rules for:
  - `dev`
  - `test`
  - `qa`
  - `gatekeeper`
- define for each role:
  - inputs
  - outputs
  - allowed tools
  - writable scope
  - verification duties
  - retry behavior

2. finalize memory contract
- define role-based memory read/write rules
- fix stable schema for:
  - `module_id`
  - `kind`
  - `content`
  - `structured`
  - `task_id`
  - `chain_stage`
  - `related_files`
  - `validation_status`
  - `supersedes`
- document which role may write which memory kinds
- enforce the same policy in code

3. finalize graph contract
- stabilize:
  - `file -> node`
  - `node -> tests`
  - `node -> docs`
  - `node -> acceptance scenarios`
- remove temporary node drift and ad-hoc fallback logic where possible

4. finalize evidence schema
- standardize structured evidence for:
  - `test_report`
  - `qa_review`
  - `gatekeeper_decision`
  - `merge_result`
  - `deploy_report`
  - `requirement_coverage`
  - `acceptance_trace`

### Exit Criteria

- the same task replay produces consistent stage behavior
- memory writes are schema-valid and role-valid
- graph lookups no longer require frequent manual bypasses

## Phase 2: Add Failure Classification

### Objective

Teach the workflow to understand why a run failed.

### Work Items

1. introduce failure classifier
- classify failures as:
  - task defect
  - prompt or contract defect
  - graph defect
  - gate defect
  - environment defect
  - provider or tool defect

2. introduce workflow issue extraction
- produce structured issue summaries with:
  - failing stage
  - root cause class
  - affected contracts
  - affected nodes
  - affected tools or provider
  - suggested repair direction

3. improve observer summaries
- Observer should receive:
  - chain summary
  - evidence summary
  - automatic retries attempted
  - root cause guess
  - whether manual action is still needed

### Exit Criteria

- Observer no longer needs to manually inspect raw logs for common failures
- common chain failures are automatically labeled into stable categories

## Phase 3: Add Automatic Workflow Repair

### Objective

Allow the workflow to repair its own governance defects, not only business tasks.

### Work Items

1. create workflow-improvement task type
- when failure classifier says the problem is in workflow itself:
  - create a structured workflow improvement task
  - run through the normal chain

2. standardize `predict -> verify -> diff -> iterate`
- every workflow repair must produce:
  - predicted expected output
  - actual observed output
  - mismatch analysis
  - repair hypothesis
  - verification result

3. add replay-based validation set
- maintain stable replay cases for:
  - coordinator routing
  - PM contract output
  - Dev context and worktree
  - Test contract and test report
  - QA contract
  - Gatekeeper PM alignment
  - merge isolation
  - deploy smoke
  - version gate

### Exit Criteria

- workflow defects are routinely repaired through workflow-generated tasks
- Observer no longer authors most repair tasks by hand

## Phase 4: Upgrade QA and Gatekeeper to Graph-Driven Acceptance

### Objective

Move from contract-only acceptance toward evidence-backed graph acceptance.

### Work Items

1. add requirement coverage trace
- for each requirement, capture:
  - changed files
  - related tests
  - node coverage
  - evidence source

2. add acceptance trace
- for each acceptance criterion, capture:
  - whether it is satisfied
  - by which evidence
  - with what confidence

3. upgrade QA
- QA should validate:
  - PM contract alignment
  - test evidence completeness
  - document impact completeness
  - scenario coverage where required

4. upgrade Gatekeeper
- Gatekeeper should require:
  - complete PM alignment
  - requirement coverage
  - acceptance trace
  - node state readiness
  - release preconditions

5. tighten release gates
- enforce:
  - node gate
  - doc gate
  - coverage gate
  - version gate
  - deploy success gate

### Exit Criteria

- `merge_pass` is backed by structured evidence, not only model judgment
- each relevant node can explain why it is ready

## Phase 5: Reach Observer-Mostly-Monitoring Mode

### Objective

Reduce Observer from active repair participant to governance supervisor.

### Default Observer Role

Observer should mostly:
- watch dashboards and summaries
- approve high-risk actions
- resolve rare ambiguous failures
- change policy when needed

Observer should no longer routinely:
- debug prompt routing
- identify broken metadata by hand
- create repair tasks for common workflow defects
- manually replay standard chain cases

### Allowed Observer Intervention Categories

1. policy changes
- graph policy
- memory policy
- gate policy
- approval policy

2. high-risk overrides
- release override
- destructive cancel or rollback
- force bypass of a critical gate

3. unresolved rare failures
- issues not covered by failure classifier
- infra failures not auto-recoverable
- conflicting evidence or governance ambiguity

### Exit Criteria

- Observer only handles exceptional or policy-level situations
- most workflow repairs are automatic
- chain summaries are readable without log archaeology

## Implementation Order

Recommended execution order:

1. role-based memory contract
2. graph-driven minimal verification mapping
3. evidence and trace schema
4. failure classifier
5. workflow-improvement task automation
6. graph-driven QA and Gatekeeper acceptance
7. Observer-mostly-monitoring mode

## Priority Breakdown

### P0

- role-based memory contract
- graph mapping stabilization
- evidence schema stabilization
- failure classification skeleton

### P1

- workflow improvement auto-task
- replay set and regression harness
- QA and Gatekeeper coverage trace

### P2

- Observer-only dashboard mode
- policy tuning and approval minimization
- deeper graph-driven release governance

## Required Deliverables

The roadmap should eventually produce:

1. documents
- `dev-rules.md`
- memory rules
- graph mapping spec
- acceptance trace spec
- observer operating model

2. runtime policies
- role-based memory policy
- role-based tool policy
- graph lookup policy
- gate policy

3. tests
- replay tests for each stage
- full-chain E2E
- gate behavior tests
- version-gate enforcement tests
- workflow self-repair regression tests

4. audit outputs
- chain summary
- release summary
- requirement coverage report
- acceptance trace report
- observer escalation summary

## Practical Short-Term Goal

The most realistic near-term milestone is:

- workflow repairs common defects automatically
- Observer mostly reviews summaries and only steps in when:
  - policy is unclear
  - a high-risk gate is hit
  - the automatic repair budget is exhausted

This is the correct transition stage before full Observer-mostly-monitoring mode.
