---
name: aming-claw-onboard
description: Single Aming Claw entrypoint. Use this first for Aming Claw governance, backlog, graph, runtime, worker, QA, dashboard, MCP, install/start/bootstrap, or dogfood work; route through the onboard route guide service before choosing a role or successor contract.
---

# Aming Claw Onboard

This is the only active Aming Claw skill entrypoint.

Start here, then use the live onboard route guide service as the source of
truth. Archived skills are historical reference only and are not active
instructions.

## Entry

1. Check the live project state with MCP: `runtime_status`, `graph_status`, and
   `graph_operations_queue`.
2. Call the onboard route guide service:
   `POST /api/projects/{project_id}/onboard-route-guide`.
3. Confirm role: `observer`, `worker`, or `qa`.
4. Confirm work type, such as `capability_query`, `system_operation`,
   `continue_contract_chain`, `observer_hotfix`,
   `operator_supervised_direct_main`, `multi_backlog_parallel`,
   `parallel_worker`, or `qa_verification`.
5. Follow only the returned `agent_onboard_guidance`,
   `interface_index`, `backlog_chain_binding`, and `next_legal_action`.

## Guardrails

- Do not treat archived skill files as active instructions.
- Do not mutate governed files until a backlog row and route/contract evidence
  exist.
- For operator-supervised direct work, record the onboard/direct exception
  evidence before mutation and stay inside the approved target files.
- For worker or QA work, use the runtime context or QA session entry returned
  by the onboard guide.

## Archive

Legacy skill documents moved to `Archive/skills/`. Use
`Archive/skills/index.json` only as a provenance map from old skill ids to
archived paths and replacement onboard route guide paths.
