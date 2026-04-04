# Observer Context

## Purpose

This file captures stable observer-side context for the current aming-claw roadmap so future observer runs do not need to reconstruct the same project facts from scratch.

## Current Focus

- The active roadmap focus is docs architecture cleanup and governance alignment.
- The intended sequence is:
  - Phase 1 docs migration and archive restructure
  - node binding / CODE_DOC_MAP alignment
  - governance host / MCP config alignment
- The observer should follow the chain to completion instead of stopping at intermediate queued, claimed, succeeded, or retry states.

## Governance Runtime Facts

- The live governance host service is expected at `http://localhost:40000`.
- Host health should be checked through `GET /api/health`.
- Task flow should be checked through:
  - `GET /api/task/{project_id}/list`
  - `POST /api/task/{project_id}/release`
  - `GET /api/version-check/{project_id}`
- `HEAD != CHAIN_VERSION` is a real live risk, but it is not automatically the same as a workflow execution outage.

## Known Config Drift

- `agent/governance/mcp_server.py` still defaults to `http://localhost:40006`.
- That `40006` default is stale relative to the current host-governance setup.
- Observer decisions should trust live host state on `40000`, not stale defaults in old bridge code.

## Known Workflow Lessons

- A waived node must be treated as passing gate evaluation.
- If a chain blocks because waived nodes are treated as invalid, classify it as a governance gate-semantics defect, not as a docs-content defect.
- If a test or workflow stage returns `Reached max turns (20)`, classify it as a workflow / executor blocker first, not as a product-code regression by default.
- Do not spam retries when the same executor-turn-limit failure repeats. Create or follow a focused unblock task.

## Observer Operating Rules

- Auto-release normal observer-held stages for the active chain when they are routine `pm`, `dev`, `test`, or `qa` transitions and no human approval is required.
- Be more conservative with `gatekeeper`, merge, or destructive follow-up actions.
- Prefer small, explicit unblock tasks when the workflow itself is defective.
- Do not treat transient task IDs or polling snapshots as long-term knowledge.

## What Belongs In Memory

Good memory candidates:

- stable governance endpoints
- recurring workflow failure patterns
- approved observer operating rules
- roadmap-level project priorities

Bad memory candidates:

- transient task IDs
- one-off queue snapshots
- temporary claimed / queued / observer_hold states
- raw polling logs
