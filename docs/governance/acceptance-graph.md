---
name: acceptance-graph
description: aming_claw project acceptance graph (Verification Topology) — Governance Service + Core Agent System
type: reference
version: v1.0
---

# aming_claw Acceptance Graph

## Status Legend

### verify_status
| Value | Meaning |
|----|------|
| verify:pass | E2E acceptance passed |
| verify:T2-pass | Unit + API tests passed |
| verify:fail | Acceptance failed |
| verify:pending | Pending verification |

## L0 — Infrastructure Layer (no dependencies)

```
L0.1  Python Runtime Environment  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[agent/requirements.txt]
      secondary:[runtime/python/]
      test:[]

L0.2  Shared Storage Directory Structure  [impl:done] [verify:T2-pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[agent/utils.py]
      secondary:[shared-volume/]
      test:[agent/tests/test_task_state.py]

L0.3  JSON/JSONL Persistence Utilities  [impl:done] [verify:T2-pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/utils.py]
      secondary:[]
      test:[agent/tests/test_task_state.py]

L0.4  Telegram API Wrapper  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/utils.py]
      secondary:[]
      test:[agent/tests/test_bot_commands.py]

L0.5  Internationalization Engine  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[agent/i18n.py, agent/locales/zh.json, agent/locales/en.json]
      secondary:[]
      test:[agent/tests/test_i18n.py]
```

## L1 — Service Layer (depends on L0)

```
L1.1  Configuration Management  [impl:done] [verify:pending] v1.0
      deps:[L0.2, L0.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/config.py]
      secondary:[]
      test:[agent/tests/test_config.py]

L1.2  Task State Machine  [impl:done] [verify:pending] v1.0
      deps:[L0.2, L0.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/task_state.py]
      secondary:[]
      test:[agent/tests/test_task_state.py]

L1.3  Git Checkpoint and Rollback  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/git_rollback.py]
      secondary:[]
      test:[agent/tests/test_git_rollback.py]

L1.4  Workspace Registry  [impl:done] [verify:pending] v1.0
      deps:[L0.2, L0.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/workspace_registry.py, agent/workspace.py]
      secondary:[]
      test:[agent/tests/test_workspace_queue.py]

L1.5  Workspace Task Queue  [impl:done] [verify:pending] v1.0
      deps:[L1.4, L1.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/workspace_queue.py]
      secondary:[]
      test:[agent/tests/test_workspace_queue.py]

L1.6  TOTP Two-Factor Authentication  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/auth.py]
      secondary:[]
      test:[]

L1.7  Model Registry  [impl:done] [verify:pending] v1.0
      deps:[L1.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/model_registry.py]
      secondary:[]
      test:[agent/tests/test_model_registry.py]
```

## L2 — Capability Layer (depends on L0+L1)

```
L2.1  AI Backend Integration (Claude/Codex/OpenAI)  [impl:done] [verify:pending] v1.0
      deps:[L1.1, L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py]
      secondary:[]
      test:[agent/tests/test_backends.py]

L2.2  Multi-Stage Pipeline  [impl:done] [verify:pending] v1.0
      deps:[L2.1, L1.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py, agent/pipeline_config.py]
      secondary:[]
      test:[agent/tests/test_role_pipeline.py, agent/tests/test_pipeline_config.py]

L2.3  Role Pipeline (PM/Dev/Test/QA)  [impl:done] [verify:pending] v1.0
      deps:[L2.2, L1.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py, agent/config.py]
      secondary:[]
      test:[agent/tests/test_role_pipeline.py]

L2.4  Noop Detection and Retry  [impl:done] [verify:pending] v1.0
      deps:[L2.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py]
      secondary:[]
      test:[agent/tests/test_backends.py]

L2.5  Task Acceptance Document Generation  [impl:done] [verify:pending] v1.0
      deps:[L1.2, L1.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/task_accept.py]
      secondary:[]
      test:[agent/tests/test_task_accept.py, agent/tests/test_acceptance_flow.py]

L2.6  Parallel Dispatcher  [impl:done] [verify:pending] v1.0
      deps:[L1.4, L1.5]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/parallel_dispatcher.py]
      secondary:[]
      test:[agent/tests/test_parallel_dispatcher.py]

L2.7  Service Manager  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/service_manager.py]
      secondary:[]
      test:[]
```

## L3 — Scenario Layer (depends on L0+L1+L2)

```
L3.1  Telegram Command Router  [impl:done] [verify:pending] v1.0
      deps:[L0.4, L1.1, L1.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/bot_commands.py]
      secondary:[]
      test:[agent/tests/test_bot_commands.py, agent/tests/test_interactive_commands.py]

L3.2  Interactive Menu System  [impl:done] [verify:pending] v1.0
      deps:[L3.1, L0.5]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[agent/interactive_menu.py]
      secondary:[]
      test:[agent/tests/test_interactive_menu.py]

L3.3  Task Create→Execute→Accept Full Chain  [impl:done] [verify:pending] v1.0
      deps:[L2.1, L2.5, L1.2, L1.3]
      gate_mode: explicit
      gates:[L2.1, L2.5]
      verify: L4
      test_coverage: partial
      primary:[agent/executor.py, agent/coordinator.py]
      secondary:[]
      test:[agent/tests/test_acceptance_flow.py]

L3.4  Screenshot Capability  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/executor.py]
      secondary:[executor-gateway/app/main.py]
      test:[agent/tests/test_screenshot_command_routing.py]

L3.5  Self-Update (mgr_reinit)  [impl:done] [verify:pending] v1.0
      deps:[L2.7, L1.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/service_manager.py, agent/bot_commands.py]
      secondary:[]
      test:[]
```

## L4 — Governance Service Layer (depends on L0)

```
L4.1  SQLite Database Layer  [impl:done] [verify:T2-pass] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/db.py]
      secondary:[]
      test:[agent/tests/test_governance_db.py]

L4.2  Explicit Enums and Error System  [impl:done] [verify:T2-pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[agent/governance/enums.py, agent/governance/errors.py]
      secondary:[]
      test:[agent/tests/test_governance_enums.py]

L4.3  Permission Matrix and State Machine  [impl:done] [verify:T2-pass] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/permissions.py]
      secondary:[]
      test:[agent/tests/test_governance_permissions.py]

L4.4  Structured Evidence Validation  [impl:done] [verify:pending] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/evidence.py, agent/governance/models.py]
      secondary:[]
      test:[agent/tests/test_governance_evidence.py]

L4.5  Gate Policy Engine  [impl:done] [verify:T2-pass] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/gate_policy.py]
      secondary:[]
      test:[agent/tests/test_governance_gate_policy.py]

L4.6  NetworkX DAG Graph Management  [impl:done] [verify:pending] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/graph.py]
      secondary:[]
      test:[agent/tests/test_governance_graph.py]

L4.7  Role Service (Principal+Session+Auth)  [impl:done] [verify:pending] v1.0
      deps:[L4.1, L4.2]
      gate_mode: explicit
      gates:[L4.1]
      verify: L2
      test_coverage: partial
      primary:[agent/governance/role_service.py]
      secondary:[agent/governance/redis_client.py]
      test:[agent/tests/test_governance_role.py]

L4.8  State Service (verify-update+release-gate+rollback)  [impl:done] [verify:pending] v1.0
      deps:[L4.1, L4.3, L4.4, L4.5, L4.6]
      gate_mode: explicit
      gates:[L4.3, L4.4, L4.5, L4.6]
      verify: L2
      test_coverage: partial
      primary:[agent/governance/state_service.py]
      secondary:[]
      test:[agent/tests/test_governance_state.py]

L4.9  Impact Analysis Engine  [impl:done] [verify:pending] v1.0
      deps:[L4.6, L4.8]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/impact_analyzer.py]
      secondary:[]
      test:[agent/tests/test_governance_impact.py]

L4.10  Audit Service  [impl:done] [verify:pending] v1.0
      deps:[L4.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/audit_service.py]
      secondary:[]
      test:[agent/tests/test_governance_audit.py]

L4.11  Project Service (init+isolation+bootstrap)  [impl:done] [verify:pending] v1.0
      deps:[L4.7, L4.8, L4.6]
      gate_mode: explicit
      gates:[L4.7]
      verify: L2
      test_coverage: partial
      primary:[agent/governance/project_service.py, agent/governance/session_persistence.py]
      secondary:[]
      test:[agent/tests/test_governance_session_persistence.py]

L4.12  Memory Service  [impl:done] [verify:pending] v1.0
      deps:[L4.1, L4.10]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/memory_service.py]
      secondary:[]
      test:[agent/tests/test_governance_memory.py]

L4.13  Event Bus  [impl:done] [verify:T2-pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/event_bus.py]
      secondary:[]
      test:[agent/tests/test_governance_event_bus.py]

L4.14  Idempotency Key Management  [impl:done] [verify:pending] v1.0
      deps:[L4.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/idempotency.py]
      secondary:[]
      test:[agent/tests/test_governance_idempotency.py]

L4.15  HTTP Service (routes+middleware)  [impl:done] [verify:pending] v1.0
      deps:[L4.7, L4.8, L4.11, L4.12, L4.10]
      gate_mode: explicit
      gates:[L4.7, L4.8, L4.11]
      verify: L4
      test_coverage: partial
      primary:[agent/governance/server.py]
      secondary:[]
      test:[agent/tests/test_governance_server.py]

L4.16  GovernanceClient SDK  [impl:done] [verify:pending] v1.0
      deps:[L4.15]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/client.py]
      secondary:[]
      test:[]

L4.17  Docker Deployment  [impl:done] [verify:pending] v1.0
      deps:[L4.15]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[Dockerfile.governance, docker-compose.governance.yml]
      secondary:[start_governance.py, init_project.py, Dockerfile.telegram-gateway, docker-compose.governance-dev.yml]
      test:[]
```

## L5 — v4 Foundation Layer (P0, depends on L4)

```
L5.1  Redis Streams Message Queue  [impl:done] [verify:pending] v4.0
      deps:[L4.17]
      gate_mode: explicit
      gates:[L4.17]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/telegram_gateway/chat_proxy.py]
      secondary:[docker-compose.governance.yml, agent/telegram_gateway/__init__.py]
      test:[]
      description: Gateway LPUSH/RPOP → XADD/XREADGROUP+ACK, no message loss

L5.2  Event Outbox Dual-Track Delivery  [impl:done] [verify:pending] v4.0
      deps:[L4.13, L4.1]
      gate_mode: explicit
      gates:[L4.13]
      verify: L4
      test_coverage: none
      primary:[agent/governance/event_bus.py, agent/governance/outbox.py]
      secondary:[agent/governance/db.py, agent/telegram_gateway/gov_event_listener.py]
      test:[]
      description: Events written to outbox table first (same transaction), background worker delivers asynchronously to Redis/dbservice

L5.3  Dual-Token Model (refresh+access)  [impl:pending] [verify:pending] v4.0
      deps:[L4.7]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: none
      primary:[agent/governance/role_service.py, agent/governance/server.py]
      secondary:[agent/governance/project_service.py]
      test:[]
      description: refresh_token(90d)+access_token(4h), supports revoke/rotate

L5.4  Agent Lifecycle API  [impl:pending] [verify:pending] v4.0
      deps:[L4.7, L4.1]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: none
      primary:[agent/governance/agent_lifecycle.py, agent/governance/server.py]
      secondary:[]
      test:[]
      description: register/heartbeat/deregister/orphans + lease mechanism
```

## L6 — v4 Consistency Layer (P1, depends on L5)

```
L6.1  Session Context (snapshot+log+version)  [impl:pending] [verify:pending] v4.0
      deps:[L5.1, L5.4]
      gate_mode: explicit
      gates:[L5.1, L5.4]
      verify: L4
      test_coverage: none
      primary:[agent/governance/session_context.py]
      secondary:[]
      test:[]
      description: Optimistic locking prevents overwrite, append log prevents data loss

L6.2  dbservice Docker Integration  [impl:done] [verify:pending] v4.0
      deps:[L4.17]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[dbservice/index.js, dbservice/Dockerfile, dbservice/package.json, docker-compose.governance.yml, nginx/nginx.conf]
      secondary:[dbservice/package-lock.json, dbservice/lib/knowledgeStore.js, dbservice/lib/memorySchema.js, dbservice/lib/memoryRelations.js, dbservice/lib/contextAssembly.js, dbservice/lib/bridgeLLM.js, dbservice/lib/transformersEmbedder.js]
      test:[dbservice/lib/knowledgeStore.test.js, dbservice/lib/memorySchema.test.js, dbservice/lib/memoryRelations.test.js, dbservice/lib/contextAssembly.test.js, dbservice/lib/phase8.test.js]
      description: dbservice containerization + dev-workflow domain pack + degradation strategy

L6.3  Message Worker (blocking consume+lease)  [impl:pending] [verify:pending] v4.0
      deps:[L5.1, L5.4, L6.1]
      gate_mode: explicit
      gates:[L5.1, L5.4, L6.1]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/message_worker.py]
      secondary:[]
      test:[]
      description: Blocking consume + lease mutex + cron fallback, three-level fault tolerance

L6.4  Observability (trace_id+structured logging)  [impl:pending] [verify:pending] v4.0
      deps:[L5.2]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/observability.py]
      secondary:[agent/telegram_gateway/gateway.py]
      test:[]
      description: trace_id links full chain, structured logging, key metrics monitoring
```

## L7 — Capability Enhancement Layer (P2, depends on L5+L6)

```
L7.1  Context Assembly Integration  [impl:done] [verify:pending] v4.0
      deps:[L6.1, L6.2]
      gate_mode: explicit
      gates:[L6.1, L6.2]
      verify: L4
      test_coverage: none
      primary:[agent/governance/server.py, dbservice/lib/contextAssembly.js]
      secondary:[]
      test:[]
      description: dbservice context assembly + dev-workflow task policies (telegram_handler/verify_node/code_review/release_check/dev_general)

L7.2  Stale Context Auto-Archive  [impl:done] [verify:pending] v4.0
      deps:[L6.1, L6.2]
      gate_mode: explicit
      gates:[L6.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/outbox.py, agent/governance/session_context.py]
      secondary:[]
      test:[]
      description: OutboxWorker checks for stale context (>24h) every 60s, automatically extracts decisions/pitfalls and archives to long-term memory

L7.3  Memory Dual-Write Proxy  [impl:done] [verify:pending] v4.0
      deps:[L4.12, L6.2]
      gate_mode: explicit
      gates:[L6.2]
      verify: L4
      test_coverage: none
      primary:[agent/governance/memory_service.py]
      secondary:[]
      test:[]
      description: memory_service.write_memory dual-writes JSON + dbservice /knowledge/upsert (best-effort)

L7.4  Task Registry  [impl:done] [verify:pending] v4.0
      deps:[L4.1, L5.4]
      gate_mode: explicit
      gates:[L4.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/task_registry.py, agent/governance/server.py, agent/governance/db.py]
      secondary:[]
      test:[]
      description: SQLite task table + create/claim/complete/list + retry + DB migration v1→v2

L7.5  Memory Migration + Domain Pack  [impl:done] [verify:pending] v4.0
      deps:[L6.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[dbservice/lib/contextAssembly.js, dbservice/lib/memorySchema.js]
      secondary:[]
      test:[]
      description: dev-workflow domain pack registration (architecture/pitfall/verify_decision/session_context/workaround/release_note/node_status/pattern) + Claude automatic memory migration to dbservice
```

## L8 — Workflow Feature Layer (P3, depends on L4+L5)

```
L8.1  import-graph State Sync  [impl:done] [verify:pending] v4.0
      deps:[L4.8, L4.15]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/governance/state_service.py]
      secondary:[]
      test:[]
      description: Parse [verify:pass/T2-pass] markers on import, sync to DB (non-pending values override existing pending status)

L8.2  Agent-Friendly Error Messages  [impl:done] [verify:pending] v4.0
      deps:[L4.15]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/server.py, agent/governance/models.py]
      secondary:[]
      test:[]
      description: verify-update missing fields/type errors return example JSON; evidence strings return correct format hints

L8.3  Release Profile  [impl:done] [verify:pending] v4.0
      deps:[L4.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/governance/state_service.py]
      secondary:[]
      test:[]
      description: Named profiles (full/hotfix/foundation/governance) + scope filtering + min_status policy

L8.4  Token Service (Dual-Token API)  [impl:done] [verify:pending] v4.0
      deps:[L5.3]
      gate_mode: explicit
      gates:[L5.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/token_service.py, agent/governance/server.py]
      secondary:[]
      test:[]
      description: POST /api/token/refresh|revoke|rotate endpoints

L8.5  Quickstart Documentation API  [impl:done] [verify:pending] v4.0
      deps:[L4.15]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/server.py]
      secondary:[]
      test:[]
      description: GET /api/docs/* returns overview/quickstart/endpoints/workflow_rules/memory_guide/telegram_integration
```

## L9 — Process Assurance Layer (Gate)

```
L9.1  Feature Coverage Check  [impl:pending] [verify:pending] v4.0
      deps:[L4.9, L4.15]
      gate_mode: explicit
      gates:[L4.9]
      verify: L4
      test_coverage: none
      primary:[agent/governance/coverage_check.py, agent/governance/server.py]
      secondary:[]
      test:[]
      description: At release-gate, check whether all files changed in git diff have corresponding acceptance nodes. Files without node coverage → warn/block release

L9.2  Node-Before-Code Gate  [impl:pending] [verify:pending] v4.0
      deps:[L9.1]
      gate_mode: explicit
      gates:[L9.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/server.py]
      secondary:[]
      test:[]
      description: At verify-update, check whether all changed_files in submitted evidence are covered by some node's primary/secondary

L9.3  Artifacts Constraint Check  [impl:pending] [verify:pending] v4.0
      deps:[L4.8, L4.15, L8.5]
      gate_mode: explicit
      gates:[L4.8, L8.5]
      verify: L4
      test_coverage: none
      primary:[agent/governance/artifacts.py, agent/governance/state_service.py, agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: coverage_check
      description: At node qa_pass, automatically check whether accompanying artifacts (api_docs/changelog/test) are complete. Missing artifacts → reject acceptance

L9.4  Node Creation Auto Doc Skeleton  [impl:pending] [verify:pending] v4.0
      deps:[L9.3, L4.13]
      gate_mode: explicit
      gates:[L9.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/doc_generator.py, agent/governance/event_bus.py]
      secondary:[agent/governance/server.py]
      test:[]
      artifacts:
        - type: api_docs
          section: coverage_check
      description: Listen to node.created events, scan @route endpoints in primary files, auto-generate api_docs skeleton. At qa_pass, require skeleton to be filled into complete documentation

L9.5  Gatekeeper Coverage Validation  [impl:done] [verify:pending] v4.0
      deps:[L9.1, L4.8]
      gate_mode: explicit
      gates:[L9.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/gatekeeper.py, agent/governance/state_service.py, agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: gatekeeper
      description: release-gate automatically checks whether the most recent coverage-check passed. Not run or pass=false → block release. Results stored in SQLite gatekeeper_checks table

L9.6  Artifacts Auto-Inference  [impl:done] [verify:pending] v4.0
      deps:[L9.3]
      gate_mode: explicit
      gates:[L9.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/artifacts.py]
      secondary:[agent/governance/server.py]
      test:[]
      artifacts:
        - type: api_docs
          section: coverage_check
      description: When a node has no artifacts declaration, auto-infer: primary has @route → require api_docs; has test declaration → require test_file

L9.7  Deploy Pre-Check Coverage-Check  [impl:pending] [verify:pending] v4.0
      deps:[L9.5, L4.17]
      gate_mode: explicit
      gates:[L9.5]
      verify: L4
      test_coverage: none
      primary:[deploy-governance.sh]
      secondary:[]
      test:[]
      description: Deploy script automatically runs coverage-check; deployment not allowed if it fails. Blocks the "change code then docker build directly to bypass workflow" loophole

L9.8  Memory Write Check  [impl:pending] [verify:pending] v4.0
      deps:[L7.3, L9.5]
      gate_mode: explicit
      gates:[L7.3]
      verify: L4
      test_coverage: none
      primary:[scripts/verify_loop.sh, agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: gatekeeper
      description: verify_loop checks whether the current change has new memory written to dbservice. If git diff shows code changes but dbservice has no recent new memory → warn to write memory

L9.9  Scheduled Task Management  [impl:done] [verify:pending] v4.0
      deps:[L6.3, L9.7]
      gate_mode: explicit
      gates:[L6.3]
      verify: L4
      verify_mode: manual
      test_coverage: none
      primary:[scripts/task-templates/telegram-handler.md]
      secondary:[docs/human-intervention-guide.md, docs/scheduled-task-design.md]
      test:[]
      description: Task prompt templates stored in project git tracking. Includes human intervention flow: dangerous operations notify human for confirmation, acceptance requires human to send test message

L9.10  Token Model Simplification  [impl:pending] [verify:pending] v5.0
      deps:[L5.3, L4.7]
      gate_mode: explicit
      gates:[L5.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/role_service.py, agent/governance/server.py]
      secondary:[agent/governance/token_service.py]
      test:[]
      artifacts:
        - type: api_docs
          section: token_model
      description: project_token never expires, replaces refresh+access dual-token. Deprecate /api/token/refresh and /api/token/rotate. Keep revoke + agent_token 24h TTL

L9.11  Gateway Token Proxy  [impl:pending] [verify:pending] v5.0
      deps:[L9.10, L5.1]
      gate_mode: explicit
      gates:[L9.10]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: Gateway holds project_token and proxies all API calls. CLI session only needs project_id without managing tokens itself

L9.12  audit_process — Audit Process Documentation  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: manual
      verify: L9
      test_coverage: none
      primary:[docs/governance/audit-process.md]
      secondary:[docs/dev/audit-process.md]
      test:[]
      description: Chain full-process audit procedure — end-to-end verification of task state transitions, gate checks, timeline, and merge idempotency
```

## L10 — Runtime Layer (v5 P0, depends on L7+L9)

```
L10.1  Task Registry Dual-Field State Machine  [impl:pending] [verify:pending] v5.0
      deps:[L7.4]
      gate_mode: explicit
      gates:[L7.4]
      verify: L4
      test_coverage: none
      primary:[agent/governance/task_registry.py, agent/governance/db.py]
      secondary:[]
      test:[]
      description: execution_status (queued/claimed/running/succeeded/failed/...) + notification_status (none/pending/sent) dual-field separation. DB migration v2→v3

L10.2  File Delivery Atomicity  [impl:pending] [verify:pending] v5.0
      deps:[L10.1, L4.17]
      gate_mode: explicit
      gates:[L10.1]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      description: DB first → file second (tmp+fsync+rename). Claim with fencing token. Startup recovery scans disk

L10.3  Executor Notification Persistence  [impl:pending] [verify:pending] v5.0
      deps:[L10.2]
      gate_mode: explicit
      gates:[L10.2]
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/backends.py]
      secondary:[]
      test:[]
      description: After execution, write execution_status=succeeded + notification_status=pending. Pub/Sub accelerates but is not depended upon

L10.4  Gateway Notification Re-delivery  [impl:pending] [verify:pending] v5.0
      deps:[L10.3]
      gate_mode: explicit
      gates:[L10.3]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: Gateway queries notification_status=pending tasks and sends notifications on each Telegram poll. Pub/Sub is acceleration channel

L10.5  Cancel/Retry/Timeout  [impl:pending] [verify:pending] v5.0
      deps:[L10.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/governance/task_registry.py, agent/executor.py]
      secondary:[agent/governance/server.py]
      test:[]
      description: cancel API + automatic re-queue on failed (attempt<max) + timeout detection (lease expiry)

L10.6  Progress Heartbeat  [impl:pending] [verify:pending] v5.0
      deps:[L10.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/governance/server.py]
      test:[]
      description: Executor periodically reports phase(planning/coding/testing/reviewing/finalizing) + percent + message

L10.7  PID Tracking + Orphan Disk Scan  [impl:pending] [verify:pending] v5.0
      deps:[L10.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Record worker_pid to Task Registry. On startup, scan processing/ + DB stale tasks, kill orphan processes, re-queue

L10.8  Runtime Projection API  [impl:pending] [verify:pending] v5.0
      deps:[L10.1, L10.3]
      gate_mode: explicit
      gates:[L10.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: task_registry
      description: GET /api/runtime/{pid} read-only Task Registry projection view (active/queued/pending_notify). Does not store its own state
```

## L11 — Interaction Experience Layer (v5 P1, depends on L10)

```
L11.1  Message Classifier (two-stage)  [impl:pending] [verify:pending] v5.0
      deps:[L10.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: First layer: rule-based fast intercept (commands/dangerous/queries); second layer: keyword fallback (followed by LLM)

L11.2  /menu Runtime Status  [impl:pending] [verify:pending] v5.0
      deps:[L10.8, L11.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: menu shows current project running task count, queue count, unread notifications. Project buttons show node pass rate

L11.3  Project Switch Context Save/Load  [impl:pending] [verify:pending] v5.0
      deps:[L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[agent/governance/session_context.py]
      test:[]
      description: /bind project switch automatically saves old project context and loads new project context

L11.4  Notification Attribution chat_id  [impl:pending] [verify:pending] v5.0
      deps:[L10.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[agent/governance/task_registry.py]
      test:[]
      description: Task completion notification sent back to the chat_id from when the task was created, not the currently bound project. Gateway poll queries notification_status=pending

L11.5  Gateway Token Proxy Integration  [impl:done] [verify:pending] v5.0
      deps:[L9.11]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: handle_message query-type messages use gov_api_for_chat to automatically use the bound project_token
```

## L12 — Executor Integration Layer (v5, depends on L10, L3)

```
L12.1  Executor Task Registry Integration  [impl:pending] [verify:pending] v5.0
      deps:[L10.1, L10.2, L3.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/governance/task_registry.py]
      test:[]
      description: pick_pending_task calls Task Registry claim (DB insert queued→claimed→running). On completion calls complete. Dual-field state execution_status + notification_status

L12.2  Executor Atomic Delivery  [impl:pending] [verify:pending] v5.0
      deps:[L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: Task file write: .tmp first then rename. Executor only scans official .json files. On startup scans processing/ to recover stale tasks

L12.3  Executor Redis Notification  [impl:pending] [verify:pending] v5.0
      deps:[L12.1, L5.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: After task completion, redis.publish task:completed. Also writes Task Registry succeeded + notification_status=pending

L12.4  Executor heartbeat + Progress Reporting  [impl:pending] [verify:pending] v5.0
      deps:[L12.1, L10.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: heartbeat thread periodically reports phase(planning/coding/testing) + percent. Written to Task Registry metadata

L12.5  Executor Startup Recovery  [impl:pending] [verify:pending] v5.0
      deps:[L12.1, L10.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: On startup, scan processing/ and DB for claimed/running tasks with expired leases. Kill orphan processes, re-queue or mark failed

L12.6  Tool Policy Strategy  [impl:pending] [verify:pending] v5.0
      deps:[L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: auto_allow/needs_approval/always_deny command policies. workspace allowlist restrictions. Dangerous operations require human confirmation
```

## L13 — Deployment Detection Layer (v5, depends on L9, L12)

```
L13.1  Pre-Deploy Detection Script  [impl:pending] [verify:pending] v5.0
      deps:[L9.5, L9.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[deploy-governance.sh]
      test:[]
      artifacts:
        - type: api_docs
          section: deployment
      description: Pre-deploy automatic checks: verify_loop all green, coverage-check pass, all new nodes qa_pass, documentation updated, memory written

L13.2  Staging Environment Auto-Validation  [impl:pending] [verify:pending] v5.0
      deps:[L13.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[docker-compose.governance.yml]
      test:[]
      description: Start staging container (40007), run health check + smoke test + API endpoint validation, allow switchover only after passing

L13.3  Dev/Prod Configuration Consistency Check  [impl:pending] [verify:pending] v5.0
      deps:[L13.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[]
      test:[]
      description: Compare dev/prod environment variables, port mappings, volume mounts for consistency. Detect missing/incorrect configuration

L13.4  Gateway Message Channel Validation  [impl:pending] [verify:pending] v5.0
      deps:[L13.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[]
      test:[]
      description: After deployment, automatically send test message to Telegram to verify Gateway channel is functional

L13.5  Deploy Integration into Workflow  [impl:pending] [verify:pending] v5.0
      deps:[L13.1, L13.2, L13.3, L13.4]
      gate_mode: explicit
      gates:[L13.1, L13.2, L13.3, L13.4]
      verify: L4
      test_coverage: none
      primary:[deploy-governance.sh]
      secondary:[]
      test:[]
      description: deploy-governance.sh calls pre-deploy-check.sh as a prerequisite step. Deployment blocked if checks fail

L13.6  End-to-End Task Execution Test  [impl:pending] [verify:pending] v5.0
      deps:[L13.1, L12.1, L12.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/e2e-task-test.sh]
      secondary:[scripts/pre-deploy-check.sh]
      test:[]
      artifacts:
        - type: api_docs
          section: deployment
      description: E2E test: Gateway writes task file→Executor consumes→result written back→notification. Verifies Docker volume binding, file cross-container visibility, executor claim+complete chain

L13.7  Volume Mount Consistency Check  [impl:pending] [verify:pending] v5.0
      deps:[L13.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[docker-compose.governance.yml]
      test:[]
      description: Check that Gateway's task-data volume is bind mounted to host shared-volume rather than a Docker volume. Prevents task files from being invisible across containers
```

## L14 — Coordinator Conversation Layer + Orphan Governance (v5.1, depends on L11, L12)

```
L14.1  Gateway Message Forwarding Refactor  [impl:pending] [verify:pending] v5.1
      deps:[L11.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: telegram_integration
      description: Remove message classifier direct task dispatch. All non-command messages forwarded to Coordinator for handling. Gateway only does send/receive, not decision-making

L14.2  Coordinator CLI Trigger  [impl:pending] [verify:pending] v5.1
      deps:[L14.1, L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, scripts/coordinator_session.py]
      secondary:[]
      test:[]
      description: Gateway receives non-command message → starts claude CLI session (with project context+memory) → processes message → replies → exits. Coordinator decides whether to dispatch task

L14.3  Coordinator Context Injection  [impl:pending] [verify:pending] v5.1
      deps:[L14.2, L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/coordinator_session.py]
      secondary:[agent/governance/session_context.py]
      test:[]
      description: Coordinator session on startup automatically loads: project context, governance state, dbservice memory, currently active tasks. Assembled into system prompt

L14.4  Executor Orphan Inspection  [impl:pending] [verify:pending] v5.1
      deps:[L12.5, L5.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Executor periodically (60s) queries /api/agent/orphans, finds orphan → checks PID → kills zombie process → re-queues task → POST /api/agent/cleanup

L14.5  Executor Lease Integration  [impl:pending] [verify:pending] v5.1
      deps:[L14.4, L5.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Executor registers lease on startup. Renews lease (with PID) during task execution heartbeat. Deregisters on completion/crash. Lease expiry → mark orphan

L14.6  Task Permission Isolation  [impl:pending] [verify:pending] v5.1
      deps:[L14.1, L14.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      description: Only Coordinator can create tasks. Gateway no longer directly creates task files. Executor verifies task source is coordinator role

L14.7  v5 Architecture Documentation Correction  [impl:pending] [verify:pending] v5.1
      deps:[L14.1, L14.2, L14.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[docs/architecture-v5-runtime.md]
      secondary:[]
      test:[]
      description: Correct the erroneous design in v5 docs where Gateway directly dispatches tasks. Clarify Coordinator's conversation+decision+orchestration role in the message flow

L14.8  Coordinator Host Machine Proxy  [impl:pending] [verify:pending] v5.1
      deps:[L14.2, L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: telegram_integration
      description: Gateway triggers host machine Executor to execute coordinator_chat tasks via task files. Executor distinguishes dev_task (write code) and coordinator_chat (conversation decision, stdout as reply). Solves the problem of Docker containers being unable to directly call host machine claude CLI
```

## L15 — Executor-Driven Architecture v6 P0 (depends on L12, L14)

```
L15.1  AILifecycleManager  [impl:pending] [verify:pending] v6.0
      deps:[L12.1, L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/ai_lifecycle.py]
      secondary:[]
      test:[]
      description: Unified AI process management. create_session(role,context,prompt)→start CLI→monitor PID→collect output→kill/cleanup. AI cannot self-start AI

L15.2  AI Output Parser  [impl:pending] [verify:pending] v6.0
      deps:[L15.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/ai_output_parser.py]
      secondary:[]
      test:[]
      description: Extract structured JSON from Claude stdout. schema_version validation. Supports mixed text+JSON in AI output

L15.3  Role Permission Matrix  [impl:pending] [verify:pending] v6.0
      deps:[]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/role_permissions.py]
      secondary:[]
      test:[]
      description: Hardcoded role permissions. coordinator:create_task/reply/archive. dev:modify_code/run_tests. tester:verify(testing/t2_pass). qa:verify(qa_pass)

L15.4  Acceptance Graph Constraint Validator  [impl:pending] [verify:pending] v6.0
      deps:[L9.1, L9.5]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/graph_validator.py]
      secondary:[]
      test:[]
      description: Executor fetches acceptance graph cache (with version CAS). Enforces: file coverage/dependency satisfaction/gate policy/role verification level/artifacts completeness/new file node creation

L15.5  Independent Evidence Collector  [impl:pending] [verify:pending] v6.0
      deps:[L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/evidence_collector.py]
      secondary:[]
      test:[]
      description: Executor independently collects factual evidence (git diff/pytest/file stat). Does not trust AI self-reported changed_files and test_results. Evidence split into decision (AI) and fact (code)

L15.6  Task State Machine  [impl:pending] [verify:pending] v6.0
      deps:[L10.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_state_machine.py]
      secondary:[]
      test:[]
      description: Explicit TaskStatus enum (created/queued/claimed/running/waiting_retry/waiting_human/blocked_by_dep/succeeded/failed_retryable/failed_terminal/eval_pending/eval_approved/eval_rejected/cancelled/archived). VALID_TRANSITIONS transition rules

L15.7  4-Layer Hierarchical Validator  [impl:pending] [verify:pending] v6.0
      deps:[L15.2, L15.3, L15.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/decision_validator.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: SchemaValidator→PolicyValidator→GraphValidator→ExecutionPreconditionValidator. Each layer independently returns {layer,passed,errors[]}. Includes error classification retry strategy (5 categories)

L15.8  Budgeted Context Assembler  [impl:pending] [verify:pending] v6.0
      deps:[L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/context_assembler.py]
      secondary:[]
      test:[]
      description: Assemble context by role budget (coordinator 8k/dev 4k/tester 3k/qa 3k). Layers: hard_context→conversation→memory→runtime. Truncate when over budget

L15.9  Task Orchestrator  [impl:pending] [verify:pending] v6.0
      deps:[L15.1, L15.7, L15.8, L15.5, L15.6]
      gate_mode: explicit
      gates:[L15.1, L15.7, L15.8]
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/executor.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: handle_user_message (assemble context→start Coordinator AI→validate decision→execute action→reply→update context). Code controls full flow, AI only outputs decision JSON
```

## L16 — v6 P1 Closed-Loop Chain (depends on L15)

```
L16.1  Dev Complete→Evidence Validation Integration  [impl:pending] [verify:pending] v6.0
      deps:[L15.5, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[agent/evidence_collector.py]
      test:[]
      description: After Executor dev_task completes, calls evidence_collector for independent collection (git diff/pytest). Compares AI self-report → records discrepancy → passes to eval

L16.2  Coordinator eval Auto-Trigger  [impl:pending] [verify:pending] v6.0
      deps:[L16.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[]
      test:[]
      description: dev_task succeeded → Executor code automatically creates coordinator_eval task → Coordinator evaluates dev result → decides next step → replies to user

L16.3  Error Classification Retry Integration  [impl:pending] [verify:pending] v6.0
      deps:[L15.7, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/task_state_machine.py]
      test:[]
      description: On validation failure, classify error (retryable_model/retryable_env/blocked_by_dep/non_retryable/needs_human) → retry, terminate, or escalate to human intervention per policy

L16.4  Conversation History Persistence  [impl:pending] [verify:pending] v6.0
      deps:[L15.8, L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/context_assembler.py]
      secondary:[agent/governance/session_context.py]
      test:[]
      description: Each message+reply written to session_context. New session loads the last 10 conversation turns on startup. Cross-message context continuity

L16.5  Memory Write Governance  [impl:pending] [verify:pending] v6.0
      deps:[L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/memory_write_guard.py]
      secondary:[agent/task_orchestrator.py]
      test:[]
      description: Pre-write checks: deduplication (similarity>0.85), credibility (>0.6), source (only qa_pass writes long-term decision), TTL (workaround 30 days). Prevents long-term memory pollution

L16.6  Auto-Archive Integration  [impl:pending] [verify:pending] v6.0
      deps:[L16.1, L16.5]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/memory_write_guard.py]
      test:[]
      description: Task complete → auto-archive: decisions written to long-term memory (after governance check), dev summaries written to pattern, stale context archived

L16.7  propose_node Validation Integration  [impl:pending] [verify:pending] v6.0
      deps:[L15.4, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/graph_validator.py]
      secondary:[]
      test:[]
      description: Coordinator outputs propose_node action → graph_validator validates (ID/uniqueness/dependencies/acyclicity/path safety) → on pass, calls governance API to create

L16.8  Task DB-ification  [impl:pending] [verify:pending] v6.0
      deps:[L15.6, L10.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[agent/governance/task_registry.py]
      test:[]
      artifacts:
        - type: api_docs
          section: task_registry
      description: task_orchestrator DB inserts (source of truth) before writing task file (secondary). Executor updates DB state on claim. Full lifecycle DB-driven
```

## L17 — v6 P2 Enhancements (depends on L15, L16)

```
L17.1  Execution Sandbox  [impl:pending] [verify:pending] v6.0
      deps:[L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/execution_sandbox.py]
      secondary:[agent/executor.py]
      test:[]
      description: Dev/Test commands run in isolated working directory. Command allowlist + parameter constraints. workspace overlay. High-risk commands require human confirmation

L17.2  Multi-Role Parallelism  [impl:pending] [verify:pending] v6.0
      deps:[L15.9, L15.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/ai_lifecycle.py]
      test:[]
      description: TaskOrchestrator supports running dev+tester AI sessions simultaneously. AILifecycleManager concurrent session management. Lease mutex protection

L17.3  Task Dependency Chain  [impl:pending] [verify:pending] v6.0
      deps:[L16.2, L15.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/task_state_machine.py]
      test:[]
      description: dev complete → automatically create tester task → tester complete → automatically create qa task. parent_task_id linkage. blocked_by_dep state management

L17.4  Human Approval Object  [impl:pending] [verify:pending] v6.0
      deps:[L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/approval_manager.py]
      secondary:[agent/task_orchestrator.py, agent/telegram_gateway/gateway.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: Sensitive operations create approval object (approval_id/action/risk/expires). Telegram button confirmation. approved_by/scope recorded

L17.5  Plan Layer  [impl:pending] [verify:pending] v6.0
      deps:[L15.9, L16.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[]
      test:[]
      description: Complex requests first generate a plan object (plan with multiple tasks attached). Plan executed in sequence after approval. Supports recovery/visualization/audit

L17.6  Observability trace+replay  [impl:pending] [verify:pending] v6.0
      deps:[L15.9, L15.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/observability.py]
      secondary:[agent/task_orchestrator.py, agent/ai_lifecycle.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: trace_id links full chain (message→coordinator→dev→eval→reply). Records raw prompt/context/AI output/validator decision/execution log. Supports replay debugging

L17.7  PM Role Integration  [impl:done] [verify:pending] v6.1
      deps:[L15.3, L15.8, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/role_permissions.py, agent/task_orchestrator.py, agent/context_assembler.py]
      secondary:[docs/architecture-v6-executor-driven.md]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: PM role: requirements analysis→PRD→node design. Permissions (generate_prd/design_nodes/propose_node). TaskOrchestrator auto-detects new feature request → starts PM session → PRD passed to Coordinator for orchestration
```

## L18 — Session Intervention Layer (v6.1, depends on L15, L17)

```
L18.1  Executor HTTP API Server  [impl:pending] [verify:pending] v6.1
      deps:[L15.9, L15.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[agent/executor.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor_api
      description: Executor embedded HTTP server (:40100) running in parallel with task loop. Provides monitoring/intervention/debug interfaces. Claude Code session operates directly via curl

L18.2  Monitoring Interface  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.1, L15.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[]
      test:[]
      description: GET /status (overall status) /sessions (AI process list) /tasks (task queue) /trace/{id} (chain details) /task/{id} (single task details+evidence+validator log)

L18.3  Intervention Interface  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.1, L15.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[agent/ai_lifecycle.py]
      test:[]
      description: POST /task/{id}/pause /task/{id}/cancel /task/{id}/retry /cleanup-orphans. Supports pause/cancel/retry tasks and cleanup zombie processes

L18.4  Direct Chat Interface  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[agent/task_orchestrator.py]
      test:[]
      description: POST /coordinator/chat (bypass Telegram to directly start Coordinator session). Supports synchronous wait for reply. Developer terminal debug entry point

L18.5  Debug Interface  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.7, L15.8, L17.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[]
      test:[]
      description: GET /validator/last-result /context/{pid} /ai-session/{id}/output. View validator decision details, context assembly result, raw AI output

L18.6  Integration Documentation  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L18.2, L18.3, L18.4, L18.5]
      gate_mode: explicit
      gates:[L18.2, L18.3, L18.4, L18.5]
      verify: L4
      test_coverage: none
      primary:[docs/api/executor-api.md]
      secondary:[agent/governance/server.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor_api
      description: Complete integration documentation: all endpoint descriptions, request/response examples, Claude Code session usage guide, common debugging commands
```

## L19 — Production Chain Completion (v6.2, depends on L15, L18)

```
L19.1  Context Persistence Fix  [impl:pending] [verify:pending] v6.2
      deps:[L15.8, L15.9, L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/task_orchestrator.py]
      secondary:[agent/context_assembler.py]
      test:[]
      description: process_coordinator_chat changed to call TaskOrchestrator.handle_user_message. Conversation history correctly saved (user msg+coordinator reply) to session_context. ContextAssembler loads last 10 turns injected into prompt

L19.2  Dev Branch Workflow  [impl:pending] [verify:pending] v6.2
      deps:[L15.9, L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[]
      test:[]
      description: Dev task creation automatically runs git checkout -b dev/task-{id}. Completed but not merged, awaiting human review. Coordinator eval reports branch name and diff. Merge triggered manually

L19.3  Telegram Markdown Escaping  [impl:pending] [verify:pending] v6.2
      deps:[L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      description: Escape MarkdownV2 special characters (_*[]()~`>#+-=|{}.!) before sending Telegram messages. Or switch to plain text mode to avoid formatting issues

L19.4  Translation Format Standardization  [impl:pending] [verify:pending] v6.2
      deps:[L18.1, L18.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: executor_api
      description: /coordinator/chat response adds structured fields: reply (for user)+actions_summary (operation summary)+status (success/failure/needs confirmation)+next_step (next step suggestion). Facilitates terminal role translation

L19.5  Review→Merge→Deploy Chain  [impl:pending] [verify:pending] v6.2
      deps:[L19.2, L13.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/merge-and-deploy.sh]
      secondary:[agent/executor_api.py]
      test:[]
      artifacts:
        - type: api_docs
          section: deployment
      description: POST /merge (merge dev branch to main after review approval) → pre-deploy-check → deploy. Complete review→merge→acceptance→deploy chain
```

## L20 — Dev Task v6 Chain Integration (v6.2, depends on L15, L16, L19)

```
L20.1  Dev task runs on v6 execution chain  [impl:pending] [verify:pending] v6.2
      deps:[L15.1, L15.5, L15.7, L19.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/ai_lifecycle.py, agent/evidence_collector.py, agent/decision_validator.py]
      test:[]
      description: dev_task no longer uses old process_claude; changed to AILifecycleManager starting dev session → structured output → DecisionValidator validation → EvidenceCollector independent collection → git branch workflow

L20.2  Dev Complete Auto-Triggers Coordinator eval  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L16.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/task_orchestrator.py]
      secondary:[]
      test:[]
      description: dev_task succeeded → Executor code automatically creates coordinator_eval task (with independently collected evidence). Coordinator evaluates result → decides next step → replies to user

L20.3  E2E Dev Chain Validation  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L20.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/e2e-dev-chain-test.sh]
      secondary:[]
      test:[]
      description: End-to-end test: Coordinator dispatches dev task → Dev changes code on branch → evidence collection → Coordinator eval → reply. Verifies complete v6 dev chain

L20.4  Dev task chat_id Injection  [impl:pending] [verify:pending] v6.2
      deps:[L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[]
      test:[]
      description: Coordinator automatically injects chat_id into task file when creating dev task. Executor process_dev_task_v6 no longer crashes with KeyError. Completion notification sent back to original chat

L20.5  Task Retry Limit (max_retry+dead_letter)  [impl:pending] [verify:pending] v6.2
      deps:[L15.6, L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/task_state_machine.py]
      test:[]
      description: Check attempt_count on task failure. Exceeds max_retry (default 3) → move to dead_letter directory → mark failed_terminal → no more retries. Prevents infinite loops

L20.6  Orphan Process Actual Cleanup  [impl:pending] [verify:pending] v6.2
      deps:[L14.4, L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Executor inspection checks tasks in processing/ → reads worker_pid → checks if process is alive → re-queues or marks failed for dead process tasks → cleans up stale files

L20.7  Notifications Changed to Gateway API  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: All Executor notifications changed to call Gateway API (POST /gateway/reply) instead of direct send_text. Unified notification channel. Gateway handles markdown escaping

L20.8  Dev→Tester→QA→Gatekeeper Auto-Trigger Chain  [impl:pending] [verify:pending] v6.2
      deps:[L16.2, L17.3, L20.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[agent/governance/gatekeeper.py]
      test:[]
      description: Dev complete → eval pass → Executor code creates test_task → Tester complete → creates qa_task → QA complete → triggers Gatekeeper check → notifies user for approval. Full chain code-driven, no reliance on AI

L20.9  AI Task Logging System  [impl:pending] [verify:pending] v6.2
      deps:[L15.1, L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/ai_lifecycle.py]
      secondary:[]
      test:[]
      description: Each task creates independent log directory shared-volume/codex-tasks/logs/task-xxx/. Records: prompt.txt (input), stdout.txt (AI output), evidence.json (evidence), validator.json (validation result), timeline.jsonl (timeline). Observer can tail in real time

L20.10  Dev task unified on v6 chain  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/task_orchestrator.py]
      test:[]
      description: All dev_task unified on process_dev_task_v6 rather than old process_claude. Fixes parallel dispatcher using old path. After completion calls handle_dev_complete to trigger auto chain

L20.11  PM Log Observability  [impl:pending] [verify:pending] v6.2
      deps:[L20.9, L17.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/ai_lifecycle.py]
      test:[]
      description: PM session writes logs to logs/ directory during execution. Records failure reason on failure. handle_user_message adds PM execution log for observer troubleshooting

L20.12  Chain Depth Limit  [impl:pending] [verify:pending] v6.2
      deps:[L20.2, L20.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/task_orchestrator.py]
      secondary:[]
      test:[]
      description: Prevents eval→dev infinite loops. Task file carries _chain_depth field. _trigger_coordinator_eval reads depth, >=3 stops and does not create new task. _write_task_file passes parent depth+1. _trigger_tester/_trigger_qa also inherit depth

L20.13  Memory Deletion Review  [impl:pending] [verify:pending] v6.2
      deps:[L15.3, L17.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/role_permissions.py, agent/task_orchestrator.py]
      secondary:[agent/memory_write_guard.py]
      test:[]
      description: Dev cannot directly delete memory, can only propose_memory_cleanup. Executor intercepts delete operations → creates approval → QA reviews before executing. amingclaw:arch and pitfall prefixes require human approval
```

## L22 — v7 Context Service (P0)

```
L22.1  ContextStore + Session State Machine  [impl:pending] [verify:pending] v7.0
      deps:[L15.1]
      gate_mode: auto
      verify: L4
      primary:[agent/context_store.py]
      secondary:[]
      test:[agent/tests/test_context_store.py]
      description: Session 8-state machine (CAS migration) + SQLite source of truth + Redis cache + idempotency key + PromptRenderer

L22.2  AILifecycleManager system-prompt-file  [impl:pending] [verify:pending] v7.0
      deps:[L22.1]
      gate_mode: auto
      verify: L4
      primary:[agent/ai_lifecycle.py]
      secondary:[]
      test:[]
      description: Switch to --system-prompt-file to pass context, no longer stuffing prompt via stdin

L22.3  Unified Task Entry /executor/task  [impl:pending] [verify:pending] v7.0
      deps:[L22.1, L22.2]
      gate_mode: auto
      verify: L4
      primary:[agent/executor_api.py, agent/task_orchestrator.py]
      secondary:[]
      test:[]
      description: POST /executor/task replaces /coordinator/chat, automatically registers observer, returns observer_token

L22.4  Context Budget Role Trimming  [impl:pending] [verify:pending] v7.0
      deps:[L22.1]
      gate_mode: auto
      verify: L4
      primary:[agent/context_assembler.py]
      secondary:[]
      test:[]
      description: Limit token budget + field priority trimming by role

L22.5  DecisionValidator Hard Rules  [impl:pending] [verify:pending] v7.0
      deps:[L22.1]
      gate_mode: auto
      verify: L4
      primary:[agent/decision_validator.py]
      secondary:[]
      test:[]
      description: dev_task must have target_files + session must have snapshot + evidence must be complete
```

## L24 — File Write Safety Layer (v7.2)

```
L24.1  AI Permission Removal (allowedTools read-only)  [impl:pending] [verify:pending] v7.2
      deps:[L22.2]
      gate_mode: auto
      verify: L4
      primary:[agent/ai_lifecycle.py]
      description: Remove dangerously-skip-permissions, change to allowedTools Read,Grep,Glob read-only

L24.2  Executor File Write API  [impl:pending] [verify:pending] v7.2
      deps:[L24.1]
      gate_mode: auto
      verify: L4
      primary:[agent/executor_api.py]
      description: /file/patch (with expected_old_hash)+/file/write+/file/mkdir, path validation realpath within worktree

L24.3  Controlled Command API  [impl:pending] [verify:pending] v7.2
      deps:[L24.2]
      gate_mode: auto
      verify: L4
      primary:[agent/executor_api.py]
      description: /test/run+/lint/run replace general /bash, allowlisted commands

L24.4  Task Bound to worktree  [impl:pending] [verify:pending] v7.2
      deps:[L24.1]
      gate_mode: auto
      verify: L4
      primary:[agent/task_orchestrator.py,agent/executor.py]
      description: Each task/session carries worktree_root+allowed_prefixes+base_commit
```

## Phase 1-7 — Implemented Feature Nodes (Implementation Phases)

The following nodes correspond to feature modules implemented in Phases 1-7 that previously had no acceptance graph nodes.

### L3 Supplemental Nodes

```
L3.2  Executor Lifecycle: monitor loop, PID lock, crash recovery, circuit breaker  [impl:done] [verify:pending] v4.0
      deps:[L3.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/service_manager.py, agent/executor_worker.py]
      secondary:[]
      test:[agent/tests/test_service_manager.py]
      verify_level: 2
      description: ServiceManager monitor loop (10s polling), PID lock prevents duplicate restarts, automatic crash recovery, circuit breaker (5 times/300s)
```

### L4 Supplemental Nodes

```
L4.18  Memory Backend: SQLite + FTS5, pluggable interface  [impl:done] [verify:pending] v4.0
      deps:[L4.1]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[agent/governance/memory_backend.py, agent/governance/memory_service.py]
      secondary:[agent/governance/db.py]
      test:[agent/tests/test_memory_backend.py]
      verify_level: 3
      description: MemoryBackend abstract interface + LocalBackend (SQLite+FTS5) + DockerBackend + CloudBackend pluggable implementations; /api/mem search endpoint

L4.19  ref_id Lifecycle: entity mapping, version chain, relation graph  [impl:done] [verify:pending] v4.0
      deps:[L4.18]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/memory_backend.py]
      secondary:[agent/governance/db.py, agent/governance/server.py]
      test:[agent/tests/test_memory_backend.py]
      verify_level: 2
      description: entity_id mapping, version chain tracking, search_and_aggregate aggregated query, relation graph (memory_relations table)

L4.20  Docker mem0 Backend: semantic search with FTS5 fallback  [impl:done] [verify:pending] v4.0
      deps:[L4.18]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/memory_backend.py]
      secondary:[]
      test:[agent/tests/test_verify_spec.py]
      verify_level: 2
      description: DockerBackend connects to dbservice semantic search; FTS5 fallback. Fixed 2026-03-29: endpoint /search→/knowledge/search, request fields project_id→scope+top_k→limit, response mapping via doc wrapper, write field body→content. Two-layer write (governance DB + dbservice vector index) and two-layer search (semantic first → FTS5 fallback) both verified working.

L4.21  Observer Mode — Task Hold/Release  [impl:done] [verify:pending] v4.1
      deps:[L4.1, L7.4]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/task_registry.py, agent/governance/db.py]
      secondary:[agent/governance/server.py]
      test:[agent/tests/test_observer_mode.py]
      verify_level: 2
      description: DB migration v9 (project_version.observer_mode column); task_registry adds hold_task/release_task/set_observer_mode/get_observer_mode; create_task is observer_mode-aware and automatically puts new tasks into observer_hold; claim_task automatically skips observer_hold tasks; REST endpoints /api/task/{pid}/hold|release + /api/project/{pid}/observer-mode

L4.22  Observer MCP Tools  [impl:done] [verify:pending] v4.1
      deps:[L4.21, L4.15]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/mcp/tools.py]
      secondary:[agent/governance/server.py]
      test:[agent/tests/test_observer_mode.py]
      verify_level: 2
      description: MCP tools: observer_mode (toggle), task_hold (pause), task_release (release); ToolDispatcher routes to REST API; Claude Code session can directly call three new tools to take over task flow

L4.23  Observer Prompt Rules  [impl:done] [verify:pending] v4.1
      deps:[L4.21, L4.22]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[docs/observer-rules.md]
      secondary:[docs/observer-feature-guide.md]
      test:[]
      verify_level: 2
      description: Observer operation specification document: coordinator takeover process, pause points at each stage, review rules, memory extraction steps; feature guide explains observer_mode design principles and usage scenarios

L4.24  Observer Instrumentation — Chain Flow Logging  [impl:done] [verify:pending] v4.1
      deps:[L4.21, L4.10]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/memory_service.py, agent/governance/task_registry.py, agent/governance/server.py]
      secondary:[agent/context_assembler.py, agent/governance/auto_chain.py]
      test:[]
      verify_level: 2
      description: Structured logging for observer flow observability. Covers: memory search (query+results), memory write (kind+content), task lifecycle (create/claim/complete with full context), conflict rule decisions, context assembly memory fetch (with exception surfacing), chain memory write success path. Ensures every step of the observer takeover flow has audit trail visibility.

L4.25  Coordinator Role Specification  [impl:done] [verify:pending] v4.2
      deps:[L4.23, L5.6]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[docs/coordinator-rules.md, agent/task_orchestrator.py, agent/role_permissions.py]
      secondary:[agent/executor_worker.py]
      test:[agent/tests/test_coordinator_decisions.py]
      verify_level: 2
      description: Coordinator role document defining decision boundaries (reply_only vs create_pm_task). Removes gateway pre-classification; all messages enter coordinator directly. Coordinator denied create_dev/test/qa_task — must go through PM. TASK_ROLE_MAP fixed: type=task maps to coordinator role. _needs_pm_analysis simplified to always-true except pure status queries. Coordinator has NO Bash/tool access (--max-turns 1, no allowedTools); memory/queue/context pre-injected by executor._build_prompt. _handle_coordinator_result supports v1 JSON format with gate validation. Raw AI output dumped to logs/coordinator-{task_id}.raw.txt for observability.

L4.26  PM Role Specification  [impl:done] [verify:pending] v4.2
      deps:[L4.25]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[docs/pm-rules.md]
      secondary:[]
      test:[]
      verify_level: 2
      description: PM role document defining PRD output requirements (target_files, acceptance_criteria, verification mandatory fields), gate_post_pm validation, memory context usage, prohibited actions.

L4.27  Memory Path Unification  [impl:done] [verify:pending] v4.2
      deps:[L4.20, L4.24]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/context_assembler.py, agent/task_orchestrator.py]
      secondary:[]
      test:[]
      verify_level: 2
      description: Memory search and write unified to dbservice-primary with local FTS5 fallback. context_assembler._fetch_memories tries dbservice /knowledge/search first, falls back to governance /api/mem/search. archive_memory action tries dbservice /knowledge/upsert first, falls back to governance /api/mem/write.

L4.28  Chain Robustness Fixes  [impl:done] [verify:pending] v4.2
      deps:[L4.21, L4.24]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/auto_chain.py, agent/governance/task_registry.py, agent/governance/db.py]
      secondary:[]
      test:[agent/tests/test_governance_db.py, agent/tests/test_checkpoint_gate.py]
      verify_level: 2
      description: Chain robustness fixes — (1) pitfall memory dedup in _write_chain_memory prevents identical entries; (2) failed task auto-retry respects observer_mode (goes to observer_hold instead of queued); (3) DB migration v10 adds session_context table for coordinator session-level logging; (4) B36 — unify retry-prompt/gate allowed scope via _compute_gate_static_allowed helper + _scan_dependent_tests for 1st-order import discovery, prevents dev ping-pong on unrelated-file block; (5) B8/G4/G6 checkpoint gate: docs/dev exempt from unrelated-file blocking, doc_impact auto-populated from graph, bidirectional code↔doc lookup.

L4.29  Coordinator Output Gate + Retry  [impl:done] [verify:pending] v4.2
      deps:[L4.25]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/executor_worker.py]
      secondary:[]
      test:[agent/tests/test_coordinator_decisions.py]
      verify_level: 2
      description: Gate validation for coordinator JSON output (G1-G7 rules: schema_version, reply, actions whitelist, prompt length). Invalid output triggers retry (max 2 retries with error feedback). 3 failures marks task failed. _validate_coordinator_output + retry loop in run_once.

L4.30  AI Keyword Extraction + Memory English Normalization  [impl:done] [verify:pending] v4.2
      deps:[L4.27, L4.25]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/llm_utils.py, agent/executor_worker.py, agent/governance/memory_service.py]
      secondary:[]
      test:[agent/tests/test_llm_utils.py]
      verify_level: 2
      description: New llm_utils module with extract_keywords (haiku, any language to English keywords) and translate_to_english (Chinese content normalization at write time). Replaces regex keyword extraction in executor._build_prompt. Memory writes auto-translate Chinese to English, preserving original in structured.original_content.

L4.31  Per-Role Model Selection  [impl:done] [verify:pending] v4.2
      deps:[L4.25]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/ai_lifecycle.py, agent/pipeline_config.py]
      secondary:[agent/pipeline_config.yaml.example]
      test:[]
      verify_level: 2
      description: Wire pipeline_config into ai_lifecycle.create_session. Each role uses configured provider+model. Supports coordinator/pm/dev/tester/qa/utility roles. Provides the common provider/model resolution layer used by both Anthropic/Claude and OpenAI/Codex execution paths. pipeline_config.yaml.example updated with all roles.

L4.32  E2E Coordinator Test Infrastructure  [impl:done] [verify:pending] v4.2
      deps:[L4.29, L4.30]
      verify_requires:[L4.33]
      gate_mode: auto
      verify: L3
      test_coverage: full
      primary:[agent/tests/test_e2e_coordinator.py]
      secondary:[agent/tests/conftest.py]
      test:[agent/tests/test_e2e_coordinator.py]
      verify_level: 3
      description: Isolated E2E test infrastructure using aming-claw-test project with dedicated domain pack. Tests S1 (create_pm_task), S5 (reply_only), S3 (duplicate detection), S2 (queue congestion). pytest marker @e2e separates from unit tests. Batched CLI calls for efficiency. verify_requires L4.33 (LLM Utils E2E must pass first).

L4.33  LLM Utils E2E — Keyword Extraction + Translation  [impl:done] [verify:pending] v4.2
      deps:[L4.30]
      verify_requires:[L4.36]
      gate_mode: auto
      verify: L3
      test_coverage: full
      primary:[agent/tests/test_e2e_coordinator.py]
      secondary:[agent/governance/llm_utils.py]
      test:[agent/tests/test_e2e_coordinator.py]
      verify_level: 3
      description: Real Claude CLI E2E tests for llm_utils. C1 (English keyword extraction), C2 (Chinese keyword extraction → English), C3 (Chinese→English translation), C4 (English skip — no CLI call). Validates keyword quality and translation accuracy with real model. Required by L4.32 (Coordinator E2E) via verify_requires.

L4.34  Two-Round Coordinator + query_memory  [impl:done] [verify:pending] v4.3
      deps:[L4.25, L4.29]
      verify_requires:[L4.33]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/executor_worker.py, agent/role_permissions.py]
      secondary:[docs/coordinator-rules.md]
      test:[agent/tests/test_coordinator_decisions.py, agent/tests/test_e2e_coordinator.py]
      verify_level: 2
      description: Two-round coordinator flow. Round 1: user prompt + conversation history + queue + context (no memories) → coordinator outputs query_memory/reply_only/create_pm_task. If query_memory: executor searches FTS5 with coordinator's queries. Round 2: same + memory results → final decision (no query_memory allowed). Gate validates round parameter. Replaces llm_utils keyword extraction in coordinator path.

L4.35  Conversation History (session_context)  [impl:done] [verify:pending] v4.3
      deps:[L4.28, L4.34]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/executor_worker.py]
      secondary:[agent/governance/server.py]
      test:[agent/tests/test_e2e_coordinator.py]
      verify_level: 2
      description: Coordinator conversation history via session_context table. Executor writes coordinator_turn entries (user_message, decision, reply_preview) after each decision. Reads last 10 entries and injects into coordinator prompt as Recent Conversation. Enables follow-up references and context continuity across turns.

L4.36  Dbservice E2E — Two-Layer Write + Search  [impl:done] [verify:pending] v4.3
      deps:[L4.20]
      gate_mode: auto
      verify: L3
      test_coverage: full
      primary:[agent/governance/memory_backend.py]
      secondary:[]
      test:[agent/tests/test_e2e_coordinator.py]
      verify_level: 3
      description: E2E tests for dbservice two-layer memory (Group E). E1: write→FTS5 search round-trip. E2: semantic search returns results. E3: write returns index_status=indexed. All tests use aming-claw-test project for isolation. Fixes: search endpoint /search→/knowledge/search, request fields scope+limit, response doc wrapper, write field body→content.

L4.37  PM Role Isolation + PRD Output  [impl:done] [verify:pending] v4.3
      deps:[L4.25, L4.31]
      verify_requires:[L4.32]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/executor_worker.py, agent/ai_lifecycle.py, agent/governance/auto_chain.py]
      secondary:[agent/role_permissions.py, agent/pipeline_config.py]
      test:[agent/tests/test_coordinator_decisions.py]
      verify_level: 2
      description: PM gets independent role (not mapped to coordinator). Has Read/Grep/Glob tools, --max-turns 3, 180s hang_timeout, opus model. Outputs PRD JSON with target_files, test_files, verification, acceptance_criteria, doc_impact, related_nodes, proposed_nodes (with test + test_strategy + verify_requires), skip_reasons. Gate uses explain-or-provide pattern for soft-mandatory fields. Coordinator forwards memories + context_update to PM metadata. Full PM memory write (prd_scope with all fields). Enhanced gate pitfall with previous output preview.

L4.38  Observer Task Cancel  [impl:done] [verify:pending] v4.3
      deps:[L4.21]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/task_registry.py, agent/governance/server.py, agent/mcp/tools.py]
      secondary:[]
      test:[agent/tests/test_coordinator_decisions.py]
      verify_level: 2
      description: New cancelled terminal status — no auto-chain, no retry. cancel_task function + POST /api/task/{pid}/cancel endpoint + task_cancel MCP tool. Observer can cleanly discard tasks without triggering downstream chain.

L4.39  PM E2E Tests  [impl:done] [verify:pending] v4.3
      deps:[L4.37]
      verify_requires:[L4.32]
      gate_mode: auto
      verify: L3
      test_coverage: full
      primary:[agent/tests/test_e2e_coordinator.py, agent/tests/test_coordinator_decisions.py]
      secondary:[]
      test:[agent/tests/test_e2e_coordinator.py, agent/tests/test_coordinator_decisions.py]
      verify_level: 3
      description: PM E2E batch (PA1-PA5: feature dev, bug fix, test-only, doc update, verification) validates PRD output per task type. Gate unit tests (PB1-PB5: all pass, missing mandatory, missing soft with/without skip_reasons). 404 total tests passing.

L4.40  Executor Subprocess Fix — subprocess.run + Input/Output Logging  [impl:done] [verify:pending] v4.3
      deps:[L4.37]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/ai_lifecycle.py, agent/service_manager.py]
      secondary:[agent/executor_worker.py]
      test:[]
      verify_level: 2
      description: Replaced Popen+watchdog with subprocess.run (fixes Windows pipe deadlock). ServiceManager stdout=DEVNULL (fixes buffer overflow crash). All log.info in ai_lifecycle replaced with file-based _al_log. Input/output files saved per session (input-{id}.txt, output-{id}.txt). ROLE_PROMPTS["pm"] simplified to role identity only (format in _build_prompt). pipeline_config.yaml created with per-role model config.

L4.41  Codex Provider Routing + MCP Registration  [impl:done] [verify:pending] v4.3
      deps:[L4.31, L4.40, L4.22]
      verify_requires:[L4.39]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/ai_lifecycle.py, agent/pipeline_config.yaml.example]
      secondary:[shared-volume/codex-tasks/state/pipeline_config.yaml, agent/tests/test_ai_lifecycle_provider_routing.py]
      test:[agent/tests/test_ai_lifecycle_provider_routing.py]
      verify_level: 2
      description: `ai_lifecycle` now routes by configured provider: `anthropic` uses Claude CLI, `openai` uses `codex exec`. Codex path composes system+task prompt into stdin, reads final output from `--output-last-message`, and preserves per-session input/output logs. Runtime pipeline config switched to OpenAI/Codex defaults (`gpt-5.4-mini` for coordinator/tester/qa/utility, `gpt-5.4-codex` for pm/dev). Observer host-side entry uses nginx `:40000/api/*`. Operational requirement: Codex CLI must have the `aming-claw` MCP server registered; repo `.mcp.json` alone is not sufficient.
```

### L5 Supplemental Nodes

```
L5.5  Conflict Rule Engine: duplicate, opposite-op, dependency, failure-pattern  [impl:done] [verify:pending] v4.0
      deps:[L4.1, L4.18]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[agent/governance/conflict_rules.py, agent/governance/server.py]
      secondary:[]
      test:[agent/tests/test_conflict_rules.py]
      verify_level: 3
      description: 5-rule engine (duplicate/conflict/queue_full/retry/new), task metadata enrichment, rule decision interface

L5.6  Coordinator Awareness: intent classifier, memory+queue+rule prompt injection  [impl:done] [verify:pending] v4.0
      deps:[L5.5, L5.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/executor_worker.py, agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[agent/tests/test_verify_spec.py]
      verify_level: 2
      description: Gateway intent classifier (greeting/query/dangerous/task/chat), Coordinator prompt injection with memory+queue+rule decision
```

### L6 Supplemental Nodes

```
L6.5  Spec Invariant Verification: 10 invariant tests  [impl:done] [verify:pending] v4.0
      deps:[L3.2, L4.18, L4.19, L5.5, L5.6, L4.20]
      gate_mode: auto
      verify: L3
      test_coverage: full
      primary:[agent/tests/test_verify_spec.py]
      secondary:[]
      test:[agent/tests/test_verify_spec.py]
      verify_level: 3
      description: 10 spec invariant tests covering all Phase 1-6 features; total test count 275
```
