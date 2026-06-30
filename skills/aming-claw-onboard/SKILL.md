---
name: aming-claw-onboard
description: Single Aming Claw skill entrypoint. Use this first for Aming Claw governance, backlog, graph, runtime, worker, QA, dashboard, MCP, install/start/bootstrap, or dogfood work; prefer the MCP onboard_route_guide tool before choosing a role or successor contract.
---

# Aming Claw Onboard

This is the only active Aming Claw skill entrypoint.

Start here, then use the live MCP `onboard_route_guide` tool as the source of
truth. The HTTP endpoint is a fallback only when the host does not expose the
MCP tool. Archived skills are historical reference only and are not active
instructions.

## Entry

1. Check the live project state with MCP: `runtime_status`, `graph_status`, and
   `graph_operations_queue`.
2. Call MCP `onboard_route_guide` with `project_id` and any available
   `backlog_id` or `bug_id`, role/work-type hints, and route-token refs.
3. If MCP does not expose `onboard_route_guide`, fall back to
   `POST /api/projects/{project_id}/onboard-route-guide` with the same fields.
4. Confirm role: `observer`, `worker`, `mf_sub`, or `qa`.
5. Confirm work type, such as `capability_query`, `system_operation`,
   `continue_contract_chain`, `observer_hotfix`,
   `operator_supervised_direct_main`, `multi_backlog_parallel`,
   `parallel_worker`, or `qa_verification`.
6. Follow only the returned role/token guidance, `next_legal_action`, and index
   paths under `agent_onboard_guidance.onboard_route_guide`, including
   `interface_index`, `capability_index`, `system_operation_index`,
   `backlog_chain_binding`, `graph_first_policy`, source-hint status paths,
   and archive/resource paths.
7. Before source-only fallback, use source search, IDE symbols, language tooling,
   or project-native navigation to discover exact source symbol names. Then use
   the returned `graph_first_policy` to call `graph_query` with the correct
   `query_purpose`, starting with `function_index` and exact symbols, followed
   by callers/callees for matched symbols. Preserve graph trace ids in the
   contract/timeline payload. Use source-only evidence only when the graph misses,
   is unavailable, or source-hint status says docs/config/tests are not
   materialized.

## Guardrails

- Do not treat archived skill files as active instructions.
- Do not mutate governed files until a backlog row and route/contract evidence
  exist.
- For operator-supervised direct work, record the onboard/direct exception
  evidence before mutation and stay inside the approved target files.
- For worker or QA work, use the runtime context or QA session entry returned
  by the onboard guide.
- For QA evidence that is materialized by a parent observer, keep the QA owner,
  submitter, materialized-from, and authorization provenance fields returned by
  the runtime guide; do not collapse it into observer-authored QA.

## Archive

Legacy skill documents moved to `Archive/skills/`. Use
`Archive/skills/index.json` only as a provenance map from old skill ids to
archived paths and replacement onboard route guide paths.
