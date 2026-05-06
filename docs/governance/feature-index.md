# Governance Feature Index

This index is generated from the latest reconcile doc-index review. It is
the repo-level entry point for feature nodes, owned code, linked docs,
linked tests, and remaining doc/test debt.

## Source

- project_id: `aming-claw`
- session_id: `7128a3a2bec24deaaa0bbf60399dadc3`
- source_review: `shared-volume/codex-tasks/state/governance/aming-claw/graph.rebase.doc-index.review.json`

## Summary

| Metric | Value |
|---|---:|
| Candidate feature leaves | `171` |
| Approved feature leaves | `171` |
| Missing source leaves | `0` |
| Missing docs | `107` |
| Missing tests | `26` |
| Unresolved files | `23` |
| Index docs tracked | `6` |

## Blocking Issues

- `approved_feature_missing_doc`
- `approved_feature_missing_test`
- `file_inventory_unresolved`

## Feature Nodes

| Node | Feature | Code | Docs | Tests | Debt |
|---|---|---|---|---|---|
| `L7.1` | agent | `agent/__init__.py` | `docs/coordinator-rules.md`<br>`docs/governance/reconcile-workflow.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auto_backlog_bridge.py`<br>+39 more | none |
| `L7.2` | agent._patch_locales | `agent/_patch_locales.py` | missing | missing | doc, test |
| `L7.3` | agent.ai_lifecycle | `agent/ai_lifecycle.py` | `docs/coordinator-rules.md`<br>`docs/governance/prd-full.md` | `agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auth_reclaim_e2e.py`<br>`agent/tests/test_auth_token_env_strip.py`<br>+8 more | none |
| `L7.4` | agent.ai_output_parser | `agent/ai_output_parser.py` | missing | `agent/tests/test_task_orchestrator_autochain.py` | doc |
| `L7.5` | agent.backends | `agent/backends.py` | `docs/api/governance-api.md` | missing | test |
| `L7.6` | agent.cli | `agent/cli.py` | `docs/governance/acceptance-graph.md` | `agent/tests/test_cli.py`<br>`agent/tests/test_graph_delta_auto_infer.py`<br>`agent/tests/test_phase_z_v2_project_profile.py`<br>+1 more | none |
| `L7.7` | agent.config | `agent/config.py` | missing | `agent/tests/test_config_validation.py`<br>`agent/tests/test_aming_config.py`<br>`agent/tests/test_checkpoint_gate.py`<br>+2 more | doc |
| `L7.8` | agent.context_assembler | `agent/context_assembler.py` | missing | `agent/tests/test_context_assembler.py`<br>`agent/tests/test_task_orchestrator_autochain.py` | doc |
| `L7.9` | agent.context_store | `agent/context_store.py` | missing | `agent/tests/test_context_store.py` | doc |
| `L7.10` | agent.decision_validator | `agent/decision_validator.py` | missing | `agent/tests/test_coordinator_decisions.py`<br>`agent/tests/test_task_orchestrator_autochain.py` | doc |
| `L7.11` | agent.deploy_chain | `agent/deploy_chain.py` | `docs/deployment.md`<br>`docs/governance/plan-graph-driven-doc.md`<br>`docs/governance/prd-full.md` | `agent/tests/test_deploy_chain.py`<br>`agent/tests/test_deploy_chain_port_fix.py`<br>`agent/tests/test_deploy_event_driven.py`<br>+6 more | none |
| `L7.12` | agent.evidence_collector | `agent/evidence_collector.py` | missing | `agent/tests/test_task_orchestrator_autochain.py` | doc |
| `L7.13` | agent.execution_sandbox | `agent/execution_sandbox.py` | missing | missing | doc, test |
| `L7.14` | agent.executor | `agent/executor.py` | `docs/api/executor-api.md` | `agent/tests/test_executor_auth_smoke.py`<br>`agent/tests/test_executor_complete_retry.py`<br>`agent/tests/test_executor_output_parsing.py`<br>+58 more | none |
| `L7.15` | agent.executor_api | `agent/executor_api.py` | `docs/api/executor-api.md` | missing | test |
| `L7.16` | agent.executor_worker | `agent/executor_worker.py` | `docs/api/executor-api.md`<br>`docs/architecture.md`<br>`docs/coordinator-rules.md`<br>+4 more | `agent/tests/test_executor_worker_claim_response.py`<br>`agent/tests/test_executor_worker_claim_stability.py`<br>`agent/tests/test_executor_worker_merge.py`<br>+34 more | none |
| `L7.17` | agent.governance | `agent/governance/__init__.py` | `docs/coordinator-rules.md`<br>`docs/governance/reconcile-workflow.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auto_backlog_bridge.py`<br>+39 more | none |
| `L7.18` | agent.governance.agent_lifecycle | `agent/governance/agent_lifecycle.py` | missing | missing | doc, test |
| `L7.19` | agent.governance.ai_cluster_processor | `agent/governance/ai_cluster_processor.py` | missing | `agent/tests/test_ai_cluster_processor.py`<br>`agent/tests/test_auto_backlog_bridge_cluster.py`<br>`agent/tests/test_llm_cache.py`<br>+1 more | doc |
| `L7.20` | agent.governance.artifacts | `agent/governance/artifacts.py` | missing | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_doc_policy.py`<br>`agent/tests/test_phase_z.py`<br>+3 more | doc |
| `L7.21` | agent.governance.audit_service | `agent/governance/audit_service.py` | missing | `agent/tests/test_auto_chain_dedup.py`<br>`agent/tests/test_backlog_close_commit_verify.py`<br>`agent/tests/test_backlog_mf_predeclare.py`<br>+11 more | doc |
| `L7.22` | agent.governance.auto_backlog_bridge | `agent/governance/auto_backlog_bridge.py` | missing | `agent/tests/test_auto_backlog_bridge.py`<br>`agent/tests/test_auto_backlog_bridge_cluster.py`<br>`agent/tests/test_cr5_gatekeeper_overlay.py` | doc |
| `L7.23` | agent.governance.auto_chain | `agent/governance/auto_chain.py` | `docs/api/governance-api.md`<br>`docs/architecture.md`<br>`docs/coordinator-rules.md`<br>+8 more | `agent/tests/test_auto_chain_backlog_stage.py`<br>`agent/tests/test_auto_chain_bug_id_carry.py`<br>`agent/tests/test_auto_chain_dedup.py`<br>+77 more | none |
| `L7.24` | agent.governance.backlog_db | `agent/governance/backlog_db.py` | `docs/api/executor-api.md` | `agent/tests/test_backlog_db.py`<br>`agent/tests/test_backlog_required_docs.py` | none |
| `L7.25` | agent.governance.backlog_runtime | `agent/governance/backlog_runtime.py` | missing | `agent/tests/test_backlog_runtime.py`<br>`agent/tests/test_auto_chain_backlog_stage.py`<br>`agent/tests/test_reconcile_batch_memory.py`<br>+1 more | doc |
| `L7.26` | agent.governance.backlog_triage | `agent/governance/backlog_triage.py` | missing | `agent/tests/test_backlog_triage_gate.py` | doc |
| `L7.27` | agent.governance.baseline_gc | `agent/governance/baseline_gc.py` | missing | `agent/tests/test_baseline_gc.py`<br>`agent/tests/test_baseline_slice.py` | doc |
| `L7.28` | agent.governance.baseline_service | `agent/governance/baseline_service.py` | missing | `agent/tests/test_baseline_service.py`<br>`agent/tests/test_baseline_async_failure.py`<br>`agent/tests/test_baseline_slice.py`<br>+5 more | doc |
| `L7.29` | agent.governance.chain_context | `agent/governance/chain_context.py` | `docs/coordinator-rules.md`<br>`docs/dev/manual-fix-current-2026-04-24-002.md`<br>`docs/governance/plan-graph-driven-doc.md`<br>+1 more | `agent/tests/test_chain_context.py`<br>`agent/tests/test_chain_context_archive_on_merge.py`<br>`agent/tests/test_chain_context_bugid.py`<br>+20 more | none |
| `L7.30` | agent.governance.chain_graph_context | `agent/governance/chain_graph_context.py` | missing | `agent/tests/test_chain_graph_context.py` | doc |
| `L7.31` | agent.governance.chain_trailer | `agent/governance/chain_trailer.py` | `docs/api/executor-api.md` | `agent/tests/test_chain_trailer.py`<br>`agent/tests/test_chain_trailer_runtime_version.py`<br>`agent/tests/test_chain_trailer_strip_slice.py`<br>+8 more | none |
| `L7.32` | agent.governance.client | `agent/governance/client.py` | missing | `agent/tests/test_auto_chain_dedup.py`<br>`agent/tests/test_graph_delta_auto_infer.py` | doc |
| `L7.33` | agent.governance.conflict_rules | `agent/governance/conflict_rules.py` | `docs/architecture.md`<br>`docs/governance/conflict-rules.md`<br>`docs/governance/plan-graph-driven-doc.md` | `agent/tests/test_conflict_rules.py`<br>`agent/tests/test_coordinator_decisions.py`<br>`agent/tests/test_phase_b.py`<br>+2 more | none |
| `L7.34` | agent.governance.coverage_check | `agent/governance/coverage_check.py` | missing | missing | doc, test |
| `L7.35` | agent.governance.cron_reconcile | `agent/governance/cron_reconcile.py` | missing | `agent/tests/test_cron_reconcile.py` | doc |
| `L7.36` | agent.governance.db | `agent/governance/db.py` | `docs/api/executor-api.md`<br>`docs/api/governance-api.md`<br>`docs/governance/plan-graph-driven-doc.md`<br>+2 more | `agent/tests/test_db_migrations.py`<br>`agent/tests/test_auto_chain_reconcile_bypass.py`<br>`agent/tests/test_backlog_db.py`<br>+22 more | none |
| `L7.37` | agent.governance.doc_generator | `agent/governance/doc_generator.py` | missing | `agent/tests/test_governance_doc_generator.py`<br>`agent/tests/test_pm_proposed_to_dev_delta_bridge.py`<br>`agent/tests/test_version_check_runtime_fields.py`<br>+1 more | doc |
| `L7.38` | agent.governance.doc_policy | `agent/governance/doc_policy.py` | missing | `agent/tests/test_doc_policy.py` | doc |
| `L7.39` | agent.governance.drift_detector | `agent/governance/drift_detector.py` | missing | `agent/tests/test_drift_detector.py` | doc |
| `L7.40` | agent.governance.enums | `agent/governance/enums.py` | missing | `agent/tests/test_dev_contract_round4.py`<br>`agent/tests/test_executor_stall.py` | doc |
| `L7.41` | agent.governance.errors | `agent/governance/errors.py` | missing | `agent/tests/test_backlog_close_commit_verify.py`<br>`agent/tests/test_backlog_mf_predeclare.py`<br>`agent/tests/test_baseline_slice.py`<br>+1 more | doc |
| `L7.42` | agent.governance.event_bus | `agent/governance/event_bus.py` | `docs/governance/prd-full.md` | `agent/tests/test_governance_event_bus.py` | none |
| `L7.43` | agent.governance.evidence | `agent/governance/evidence.py` | missing | `agent/tests/test_auto_chain_related_nodes.py`<br>`agent/tests/test_baseline_slice.py`<br>`agent/tests/test_bootstrap.py`<br>+30 more | doc |
| `L7.44` | agent.governance.failure_classifier | `agent/governance/failure_classifier.py` | `docs/coordinator-rules.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_replay_validation.py`<br>`agent/tests/test_version_check_runtime_fields.py`<br>+2 more | none |
| `L7.45` | agent.governance.gate_policy | `agent/governance/gate_policy.py` | missing | `agent/tests/test_governance_gate_policy.py`<br>`agent/tests/test_graph_routing.py`<br>`agent/tests/test_version_check_runtime_fields.py`<br>+1 more | doc |
| `L7.46` | agent.governance.gatekeeper | `agent/governance/gatekeeper.py` | missing | `agent/tests/test_gatekeeper_graph_commit.py`<br>`agent/tests/test_gatekeeper_graph_commit_blocking.py`<br>`agent/tests/fixtures/replay_data.py`<br>+21 more | doc |
| `L7.47` | agent.governance.graph | `agent/governance/graph.py` | `docs/governance/plan-graph-driven-doc.md` | `agent/tests/test_graph_delta_auto_infer.py`<br>`agent/tests/test_graph_delta_emission_smoke.py`<br>`agent/tests/test_graph_delta_events.py`<br>+15 more | none |
| `L7.48` | agent.governance.graph_generator | `agent/governance/graph_generator.py` | `docs/governance/acceptance-graph.md`<br>`docs/governance/plan-graph-driven-doc.md` | `agent/tests/test_graph_generator.py`<br>`agent/tests/test_bootstrap.py`<br>`agent/tests/test_doc_governance.py`<br>+6 more | none |
| `L7.49` | agent.governance.idempotency | `agent/governance/idempotency.py` | missing | `agent/tests/test_backfill_observer_hotfix_trail.py`<br>`agent/tests/test_backlog_db.py`<br>`agent/tests/test_chain_context_bugid.py`<br>+8 more | doc |
| `L7.50` | agent.governance.impact_analyzer | `agent/governance/impact_analyzer.py` | missing | `agent/tests/test_bootstrap.py`<br>`agent/tests/test_code_doc_map_coverage.py`<br>`agent/tests/test_dev_contract_round4.py`<br>+5 more | doc |
| `L7.51` | agent.governance.language_adapters | `agent/governance/language_adapters/__init__.py` | `docs/coordinator-rules.md`<br>`docs/governance/reconcile-workflow.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auto_backlog_bridge.py`<br>+39 more | none |
| `L7.52` | agent.governance.language_adapters.base | `agent/governance/language_adapters/base.py` | `docs/governance/reconcile-workflow.md` | `agent/tests/test_baseline_async_failure.py`<br>`agent/tests/test_baseline_gc.py`<br>`agent/tests/test_baseline_service.py`<br>+4 more | none |
| `L7.53` | agent.governance.language_adapters.filetree_adapter | `agent/governance/language_adapters/filetree_adapter.py` | `docs/governance/reconcile-workflow.md` | `agent/tests/test_cluster_grouper.py`<br>`agent/tests/test_qa_graph_delta_review.py` | none |
| `L7.54` | agent.governance.language_adapters.python_adapter | `agent/governance/language_adapters/python_adapter.py` | `docs/governance/reconcile-workflow.md` | `agent/tests/test_qa_graph_delta_review.py` | none |
| `L7.55` | agent.governance.llm_cache | `agent/governance/llm_cache.py` | missing | `agent/tests/test_llm_cache.py`<br>`agent/tests/test_ai_cluster_processor.py`<br>`agent/tests/test_phase_z_ai_cluster_wiring.py`<br>+1 more | doc |
| `L7.56` | agent.governance.llm_cache_local | `agent/governance/llm_cache_local.py` | missing | `agent/tests/test_ai_cluster_processor.py` | doc |
| `L7.57` | agent.governance.llm_utils | `agent/governance/llm_utils.py` | missing | `agent/tests/test_llm_utils.py`<br>`agent/tests/test_coordinator_decisions.py`<br>`agent/tests/test_e2e_coordinator.py` | doc |
| `L7.58` | agent.governance.mcp_server | `agent/governance/mcp_server.py` | `docs/architecture.md` | `agent/tests/test_backlog_db.py` | none |
| `L7.59` | agent.governance.memory_backend | `agent/governance/memory_backend.py` | `docs/architecture.md`<br>`docs/governance/memory.md`<br>`docs/governance/prd-full.md` | `agent/tests/test_memory_backend.py`<br>`agent/tests/test_config_validation.py`<br>`agent/tests/test_coordinator_decisions.py`<br>+6 more | none |
| `L7.60` | agent.governance.memory_service | `agent/governance/memory_service.py` | `docs/governance/prd-full.md` | `agent/tests/test_coordinator_decisions.py`<br>`agent/tests/test_memory_backend.py`<br>`agent/tests/test_memory_injection_downstream.py`<br>+4 more | none |
| `L7.61` | agent.governance.models | `agent/governance/models.py` | missing | `agent/tests/test_conflict_rules.py`<br>`agent/tests/test_phase_z_v2_pr2.py` | doc |
| `L7.62` | agent.governance.observability | `agent/governance/observability.py` | missing | missing | doc, test |
| `L7.63` | agent.governance.outbox | `agent/governance/outbox.py` | `docs/governance/acceptance-graph.md` | missing | test |
| `L7.64` | agent.governance.output_schemas | `agent/governance/output_schemas/__init__.py` | `docs/coordinator-rules.md`<br>`docs/governance/reconcile-workflow.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auto_backlog_bridge.py`<br>+39 more | none |
| `L7.65` | agent.governance.output_schemas.dev_result_schema | `agent/governance/output_schemas/dev_result_schema.py` | missing | missing | doc, test |
| `L7.66` | agent.governance.output_schemas.error_codes | `agent/governance/output_schemas/error_codes.py` | `docs/coordinator-rules.md` | `agent/tests/test_dev_result_validator.py`<br>`agent/tests/test_pm_declarations_compliance.py`<br>`agent/tests/test_pm_result_validator.py` | none |
| `L7.67` | agent.governance.output_schemas.pm_result_schema | `agent/governance/output_schemas/pm_result_schema.py` | `docs/coordinator-rules.md`<br>`docs/roles/pm.md` | `agent/tests/test_pm_result_validator.py` | none |
| `L7.68` | agent.governance.permissions | `agent/governance/permissions.py` | `docs/config/role-permissions.md`<br>`docs/coordinator-rules.md`<br>`docs/governance/acceptance-graph.md` | `agent/tests/test_auth_token_env_strip.py`<br>`agent/tests/test_checkpoint_gate.py`<br>`agent/tests/test_coordinator_decisions.py`<br>+8 more | none |
| `L7.69` | agent.governance.preflight | `agent/governance/preflight.py` | `docs/governance/acceptance-graph.md`<br>`docs/governance/auto-chain.md`<br>`docs/governance/plan-graph-driven-doc.md` | `agent/tests/test_preflight.py`<br>`agent/tests/test_auto_backlog_bridge_cluster.py`<br>`agent/tests/test_bootstrap.py`<br>+12 more | none |
| `L7.70` | agent.governance.project_profile | `agent/governance/project_profile.py` | `docs/governance/acceptance-graph.md` | `agent/tests/test_project_profile.py`<br>`agent/tests/test_reconcile_cluster_contract_quality.py` | none |
| `L7.71` | agent.governance.project_service | `agent/governance/project_service.py` | `docs/governance/acceptance-graph.md` | `agent/tests/test_autochain_new_file_binding.py`<br>`agent/tests/test_bootstrap.py`<br>`agent/tests/test_governance_session_persistence.py`<br>+9 more | none |
| `L7.72` | agent.governance.reconcile | `agent/governance/reconcile.py` | `docs/architecture.md`<br>`docs/governance/acceptance-graph.md`<br>`docs/governance/manual-fix-sop.md`<br>+1 more | `agent/tests/test_reconcile.py`<br>`agent/tests/test_reconcile_batch_memory.py`<br>`agent/tests/test_reconcile_batch_memory_api.py`<br>+73 more | none |
| `L7.73` | agent.governance.reconcile_batch_memory | `agent/governance/reconcile_batch_memory.py` | `docs/governance/acceptance-graph.md` | `agent/tests/test_reconcile_batch_memory.py`<br>`agent/tests/test_reconcile_batch_memory_api.py`<br>`agent/tests/test_phase_z_v2_architecture_relations.py`<br>+1 more | none |
| `L7.74` | agent.governance.reconcile_config | `agent/governance/reconcile_config.py` | missing | `agent/tests/test_cluster_grouper.py` | doc |
| `L7.75` | agent.governance.reconcile_deferred_queue | `agent/governance/reconcile_deferred_queue.py` | `docs/governance/acceptance-graph.md` | `agent/tests/test_reconcile_deferred_queue.py`<br>`agent/tests/test_auto_backlog_bridge_cluster.py`<br>`agent/tests/test_reconcile_session.py`<br>+1 more | none |
| `L7.76` | agent.governance.reconcile_doc_index | `agent/governance/reconcile_doc_index.py` | missing | `agent/tests/test_reconcile_doc_index.py`<br>`agent/tests/test_reconcile_cluster_contract_quality.py`<br>`agent/tests/test_reconcile_cluster_pm_prompt.py` | doc |
| `L7.77` | agent.governance.reconcile_file_inventory | `agent/governance/reconcile_file_inventory.py` | missing | `agent/tests/test_reconcile_file_inventory.py`<br>`agent/tests/test_reconcile_session.py` | doc |
| `L7.78` | agent.governance.reconcile_phases | `agent/governance/reconcile_phases/__init__.py` | `docs/coordinator-rules.md`<br>`docs/governance/reconcile-workflow.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auto_backlog_bridge.py`<br>+39 more | none |
| `L7.79` | agent.governance.reconcile_phases.aggregator | `agent/governance/reconcile_phases/aggregator.py` | `docs/governance/acceptance-graph.md` | `agent/tests/test_phase_z_v2_pr2.py`<br>`agent/tests/test_reconcile_v2_aggregator.py` | none |
| `L7.80` | agent.governance.reconcile_phases.cluster_grouper | `agent/governance/reconcile_phases/cluster_grouper.py` | `docs/governance/reconcile-workflow.md` | `agent/tests/test_cluster_grouper.py`<br>`agent/tests/test_language_adapters.py`<br>`agent/tests/test_phase_z_ai_cluster_wiring.py`<br>+1 more | none |
| `L7.81` | agent.governance.reconcile_phases.context | `agent/governance/reconcile_phases/context.py` | `docs/coordinator-rules.md`<br>`docs/dev/manual-fix-current-2026-04-24-002.md`<br>`docs/governance/acceptance-graph.md`<br>+2 more | `agent/tests/test_context_assembler.py`<br>`agent/tests/test_context_store.py`<br>`agent/tests/test_phase_a_parity.py`<br>+5 more | none |
| `L7.82` | agent.governance.reconcile_phases.orchestrator | `agent/governance/reconcile_phases/orchestrator.py` | missing | `agent/tests/test_coordinator_decisions.py`<br>`agent/tests/test_phase_f.py`<br>`agent/tests/test_phase_g.py`<br>+10 more | doc |
| `L7.83` | agent.governance.reconcile_phases.phase_a | `agent/governance/reconcile_phases/phase_a.py` | missing | `agent/tests/test_phase_a_parity.py`<br>`agent/tests/test_reconcile_scope_phase_filter.py` | doc |
| `L7.84` | agent.governance.reconcile_phases.phase_b | `agent/governance/reconcile_phases/phase_b.py` | missing | `agent/tests/test_phase_b.py` | doc |
| `L7.85` | agent.governance.reconcile_phases.phase_c | `agent/governance/reconcile_phases/phase_c.py` | missing | `agent/tests/test_phase_c.py` | doc |
| `L7.86` | agent.governance.reconcile_phases.phase_d | `agent/governance/reconcile_phases/phase_d.py` | missing | `agent/tests/test_phase_d.py` | doc |
| `L7.87` | agent.governance.reconcile_phases.phase_e | `agent/governance/reconcile_phases/phase_e.py` | missing | `agent/tests/test_phase_e.py`<br>`agent/tests/test_phase_b.py` | doc |
| `L7.88` | agent.governance.reconcile_phases.phase_f | `agent/governance/reconcile_phases/phase_f.py` | missing | `agent/tests/test_phase_f.py` | doc |
| `L7.89` | agent.governance.reconcile_phases.phase_g | `agent/governance/reconcile_phases/phase_g.py` | missing | `agent/tests/test_phase_g.py` | doc |
| `L7.90` | agent.governance.reconcile_phases.phase_h | `agent/governance/reconcile_phases/phase_h.py` | missing | `agent/tests/test_phase_h.py` | doc |
| `L7.91` | agent.governance.reconcile_phases.phase_k | `agent/governance/reconcile_phases/phase_k.py` | missing | `agent/tests/test_phase_k.py`<br>`agent/tests/test_phase_k_autospawn.py`<br>`agent/tests/test_phase_k_confidence_threshold.py`<br>+2 more | doc |
| `L7.92` | agent.governance.reconcile_phases.phase_z | `agent/governance/reconcile_phases/phase_z.py` | missing | `agent/tests/test_phase_z.py`<br>`agent/tests/test_phase_z_ai_cluster_wiring.py`<br>`agent/tests/test_phase_z_cluster_groups_smoke.py`<br>+13 more | doc |
| `L7.93` | agent.governance.reconcile_phases.phase_z_llm | `agent/governance/reconcile_phases/phase_z_llm.py` | missing | `agent/tests/test_phase_z.py` | doc |
| `L7.94` | agent.governance.reconcile_phases.phase_z_v2 | `agent/governance/reconcile_phases/phase_z_v2.py` | missing | `agent/tests/test_phase_z_v2_architecture_relations.py`<br>`agent/tests/test_phase_z_v2_calibrate_script.py`<br>`agent/tests/test_phase_z_v2_feature_clusters.py`<br>+10 more | doc |
| `L7.95` | agent.governance.reconcile_phases.scope | `agent/governance/reconcile_phases/scope.py` | `docs/api/executor-api.md` | `agent/tests/test_reconcile_commit_sweep.py`<br>`agent/tests/test_reconcile_scope_cli.py`<br>`agent/tests/test_reconcile_scope_phase_filter.py`<br>+1 more | none |
| `L7.96` | agent.governance.reconcile_session | `agent/governance/reconcile_session.py` | `docs/governance/acceptance-graph.md` | `agent/tests/test_reconcile_session.py`<br>`agent/tests/test_reconcile_session_integration.py`<br>`agent/tests/test_auto_backlog_bridge_cluster.py`<br>+4 more | none |
| `L7.97` | agent.governance.reconcile_task | `agent/governance/reconcile_task.py` | missing | `agent/tests/test_reconcile_task_type.py`<br>`agent/tests/test_auto_chain_reconcile_bypass.py`<br>`agent/tests/test_baseline_service.py`<br>+5 more | doc |
| `L7.98` | agent.governance.redeploy_handler | `agent/governance/redeploy_handler.py` | missing | `agent/tests/test_governance_redeploy_endpoint.py`<br>`agent/tests/test_redeploy_contract.py`<br>`agent/tests/test_run_deploy_executor_stateless.py` | doc |
| `L7.99` | agent.governance.redis_client | `agent/governance/redis_client.py` | missing | `agent/tests/test_auto_chain_dedup.py`<br>`agent/tests/test_governance_role.py`<br>`agent/tests/test_governance_session_persistence.py`<br>+3 more | doc |
| `L7.100` | agent.governance.role_config | `agent/governance/role_config.py` | missing | `agent/tests/test_role_config.py`<br>`agent/tests/test_checkpoint_gate.py`<br>`agent/tests/test_pipeline_config_gatekeeper_round1.py` | doc |
| `L7.101` | agent.governance.role_service | `agent/governance/role_service.py` | `docs/governance/manual-fix-sop.md` | `agent/tests/test_governance_role.py`<br>`agent/tests/test_governance_session_persistence.py`<br>`agent/tests/test_observer_node_governance_round1.py`<br>+2 more | none |
| `L7.102` | agent.governance.server | `agent/governance/server.py` | `docs/api/governance-api.md`<br>`docs/architecture.md`<br>`docs/coordinator-rules.md`<br>+5 more | `agent/tests/test_backlog_close_commit_verify.py`<br>`agent/tests/test_backlog_mf_predeclare.py`<br>`agent/tests/test_backlog_mf_runtime_e2e.py`<br>+35 more | none |
| `L7.103` | agent.governance.session_context | `agent/governance/session_context.py` | missing | `agent/tests/test_e2e_coordinator.py`<br>`agent/tests/test_version_check_runtime_fields.py`<br>`agent/tests/test_version_check_trailer_priority.py` | doc |
| `L7.104` | agent.governance.session_persistence | `agent/governance/session_persistence.py` | missing | `agent/tests/test_version_check_runtime_fields.py`<br>`agent/tests/test_version_check_trailer_priority.py` | doc |
| `L7.105` | agent.governance.state_service | `agent/governance/state_service.py` | missing | `agent/tests/test_auto_chain_related_nodes.py`<br>`agent/tests/test_auto_chain_verify_update_session_id.py`<br>`agent/tests/test_get_server_version.py`<br>+5 more | doc |
| `L7.106` | agent.governance.symbol_cluster_processor | `agent/governance/symbol_cluster_processor.py` | missing | `agent/tests/test_ai_cluster_processor.py` | doc |
| `L7.107` | agent.governance.symbol_disappearance_review | `agent/governance/symbol_disappearance_review.py` | `docs/governance/reconcile-workflow.md` | `agent/tests/test_symbol_disappearance_review.py` | none |
| `L7.108` | agent.governance.symbol_layer_scorer | `agent/governance/symbol_layer_scorer.py` | missing | `agent/tests/test_symbol_layer_scorer.py`<br>`agent/tests/test_phase_z_v2_pr2.py` | doc |
| `L7.109` | agent.governance.symbol_node_aggregator | `agent/governance/symbol_node_aggregator.py` | missing | `agent/tests/test_phase_z_v2_pr2.py` | doc |
| `L7.110` | agent.governance.symbol_swap | `agent/governance/symbol_swap.py` | `docs/governance/reconcile-workflow.md` | `agent/tests/test_phase_z_v2_pr3.py`<br>`agent/tests/test_symbol_atomic_swap.py`<br>`agent/tests/test_symbol_disappearance_review.py` | none |
| `L7.111` | agent.governance.task_registry | `agent/governance/task_registry.py` | `docs/dev/manual-fix-current-2026-04-24-002.md`<br>`docs/governance/plan-graph-driven-doc.md`<br>`docs/governance/prd-full.md` | `agent/tests/test_task_registry.py`<br>`agent/tests/test_task_registry_escalate.py`<br>`agent/tests/test_auto_backlog_bridge_cluster.py`<br>+15 more | none |
| `L7.112` | agent.governance.token_service | `agent/governance/token_service.py` | missing | missing | doc, test |
| `L7.113` | agent.graph_validator | `agent/graph_validator.py` | missing | `agent/tests/test_task_orchestrator_autochain.py` | doc |
| `L7.114` | agent.i18n | `agent/i18n.py` | missing | missing | doc, test |
| `L7.115` | agent.manager_http_server | `agent/manager_http_server.py` | missing | `agent/tests/test_manager_http_server.py`<br>`agent/tests/test_manager_http_server_spawn.py`<br>`agent/tests/test_manager_health_runtime.py`<br>+7 more | doc |
| `L7.116` | agent.mcp | `agent/mcp/__init__.py` | `docs/coordinator-rules.md`<br>`docs/governance/reconcile-workflow.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auto_backlog_bridge.py`<br>+39 more | none |
| `L7.117` | agent.mcp.__main__ | `agent/mcp/__main__.py` | missing | `agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auth_failure_classifier.py`<br>`agent/tests/test_auth_reclaim_e2e.py`<br>+88 more | doc |
| `L7.118` | agent.mcp.events | `agent/mcp/events.py` | missing | missing | doc, test |
| `L7.119` | agent.mcp.executor | `agent/mcp/executor.py` | `docs/api/executor-api.md` | `agent/tests/test_executor_auth_smoke.py`<br>`agent/tests/test_executor_complete_retry.py`<br>`agent/tests/test_executor_output_parsing.py`<br>+58 more | none |
| `L7.120` | agent.mcp.server | `agent/mcp/server.py` | `docs/api/governance-api.md`<br>`docs/architecture.md`<br>`docs/coordinator-rules.md`<br>+5 more | `agent/tests/test_backlog_mf_runtime_e2e.py`<br>`agent/tests/test_backlog_provenance_paths.py`<br>`agent/tests/test_backlog_required_docs.py`<br>+29 more | none |
| `L7.121` | agent.mcp.tools | `agent/mcp/tools.py` | `docs/governance/prd-full.md` | missing | test |
| `L7.122` | agent.memory_write_guard | `agent/memory_write_guard.py` | missing | missing | doc, test |
| `L7.123` | agent.notification_gateway | `agent/notification_gateway.py` | missing | `agent/tests/test_notification_gateway.py` | doc |
| `L7.124` | agent.observability | `agent/observability.py` | missing | missing | doc, test |
| `L7.125` | agent.pipeline_config | `agent/pipeline_config.py` | missing | `agent/tests/test_pipeline_config_gatekeeper_round1.py` | doc |
| `L7.126` | agent.project_config | `agent/project_config.py` | missing | `agent/tests/test_bootstrap.py` | doc |
| `L7.127` | agent.role_permissions | `agent/role_permissions.py` | `docs/config/role-permissions.md`<br>`docs/coordinator-rules.md` | `agent/tests/test_auth_token_env_strip.py`<br>`agent/tests/test_checkpoint_gate.py`<br>`agent/tests/test_coordinator_decisions.py`<br>+7 more | none |
| `L7.128` | agent.service_manager | `agent/service_manager.py` | `docs/architecture.md`<br>`docs/deployment.md`<br>`docs/governance/plan-graph-driven-doc.md`<br>+2 more | `agent/tests/test_service_manager.py`<br>`agent/tests/test_service_manager_spawn.py`<br>`agent/tests/test_auth_token_env_strip.py`<br>+11 more | none |
| `L7.129` | agent.task_orchestrator | `agent/task_orchestrator.py` | missing | `agent/tests/test_task_orchestrator_autochain.py`<br>`agent/tests/test_coordinator_decisions.py` | doc |
| `L7.130` | agent.task_state_machine | `agent/task_state_machine.py` | missing | missing | doc, test |
| `L7.131` | agent.telegram_gateway | `agent/telegram_gateway/__init__.py` | `docs/coordinator-rules.md`<br>`docs/governance/reconcile-workflow.md` | `agent/tests/fixtures/replay_data.py`<br>`agent/tests/test_ai_lifecycle_provider_routing.py`<br>`agent/tests/test_auto_backlog_bridge.py`<br>+39 more | none |
| `L7.132` | agent.telegram_gateway.chat_proxy | `agent/telegram_gateway/chat_proxy.py` | missing | `agent/tests/test_reconcile_cluster_noop_checkpoint.py` | doc |
| `L7.133` | agent.telegram_gateway.gateway | `agent/telegram_gateway/gateway.py` | `docs/governance/design-spec-full.md` | `agent/tests/test_gate_contradiction.py`<br>`agent/tests/test_reconcile_cluster_noop_checkpoint.py`<br>`agent/tests/test_verify_spec.py` | none |
| `L7.134` | agent.telegram_gateway.gov_event_listener | `agent/telegram_gateway/gov_event_listener.py` | missing | missing | doc, test |
| `L7.135` | agent.telegram_gateway.message_worker | `agent/telegram_gateway/message_worker.py` | missing | missing | doc, test |
| `L7.136` | agent.utils | `agent/utils.py` | `docs/governance/design-spec-full.md`<br>`docs/governance/prd-full.md` | `agent/tests/test_utils.py`<br>`agent/tests/test_bootstrap.py`<br>`agent/tests/test_governance_doc_generator.py`<br>+7 more | none |
| `L7.137` | agent.workspace_queue | `agent/workspace_queue.py` | missing | missing | doc, test |
| `L7.138` | aming_claw | `aming_claw.py` | missing | `agent/tests/test_backlog_mf_runtime_e2e.py`<br>`agent/tests/test_doc_restructuring_phase3.py`<br>`agent/tests/test_package_install.py`<br>+2 more | doc |
| `L7.139` | dbservice.index | `dbservice/index.js` | `docs/governance/prd-full.md` | `agent/tests/test_graph_generator.py`<br>`agent/tests/test_phase_z_v2_architecture_relations.py`<br>`agent/tests/test_project_profile.py`<br>+1 more | none |
| `L7.140` | dbservice.lib.bridgeLLM | `dbservice/lib/bridgeLLM.js` | missing | missing | doc, test |
| `L7.141` | dbservice.lib.contextAssembly | `dbservice/lib/contextAssembly.js` | missing | `agent/tests/test_phase_z_v2_pr3.py`<br>`dbservice/lib/contextAssembly.test.js` | doc |
| `L7.142` | dbservice.lib.knowledgeStore | `dbservice/lib/knowledgeStore.js` | missing | `dbservice/lib/contextAssembly.test.js`<br>`dbservice/lib/knowledgeStore.test.js`<br>`dbservice/lib/memoryRelations.test.js`<br>+1 more | doc |
| `L7.143` | dbservice.lib.memoryRelations | `dbservice/lib/memoryRelations.js` | `docs/governance/design-spec-full.md` | `dbservice/lib/memoryRelations.test.js` | none |
| `L7.144` | dbservice.lib.memorySchema | `dbservice/lib/memorySchema.js` | missing | `dbservice/lib/contextAssembly.test.js`<br>`dbservice/lib/knowledgeStore.test.js`<br>`dbservice/lib/memorySchema.test.js`<br>+1 more | doc |
| `L7.145` | dbservice.lib.transformersEmbedder | `dbservice/lib/transformersEmbedder.js` | missing | missing | doc, test |
| `L7.146` | executor-gateway.app.main | `executor-gateway/app/main.py` | missing | `agent/tests/test_bootstrap.py`<br>`agent/tests/test_dirty_ignore_filter.py`<br>`agent/tests/test_graph_generator.py`<br>+1 more | doc |
| `L7.147` | executor-gateway.executors.code_change | `executor-gateway/executors/code_change.py` | missing | `agent/tests/test_task_create_backlog_gate.py`<br>`executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.148` | executor-gateway.executors.plan_task | `executor-gateway/executors/plan_task.py` | missing | `executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.149` | executor-gateway.executors.run_tests | `executor-gateway/executors/run_tests.py` | missing | `agent/tests/test_coordinator_decisions.py`<br>`executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.150` | executor-gateway.executors.take_screenshot | `executor-gateway/executors/take_screenshot.py` | missing | `executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.151` | gateway.app.main | `gateway/app/main.py` | missing | `agent/tests/test_bootstrap.py`<br>`agent/tests/test_dirty_ignore_filter.py`<br>`agent/tests/test_graph_generator.py`<br>+1 more | doc |
| `L7.152` | gateway.executors.code_change | `gateway/executors/code_change.py` | missing | `agent/tests/test_task_create_backlog_gate.py`<br>`executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.153` | gateway.executors.plan_task | `gateway/executors/plan_task.py` | missing | `executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.154` | gateway.executors.run_tests | `gateway/executors/run_tests.py` | missing | `agent/tests/test_coordinator_decisions.py`<br>`executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.155` | gateway.executors.take_screenshot | `gateway/executors/take_screenshot.py` | missing | `executor-gateway/tests/test_executors_contract.py` | doc |
| `L7.156` | init_project | `init_project.py` | `docs/api/governance-api.md`<br>`docs/deployment.md` | `agent/tests/test_bootstrap.py`<br>`agent/tests/test_governance_session_persistence.py`<br>`agent/tests/test_observer_node_governance_round1.py` | none |
| `L7.157` | scripts._fix-backends | `scripts/_fix-backends.py` | missing | missing | doc, test |
| `L7.158` | scripts._verify-syntax | `scripts/_verify-syntax.py` | missing | missing | doc, test |
| `L7.159` | scripts.apply_graph | `scripts/apply_graph.py` | missing | `agent/tests/test_qa_graph_delta_review.py` | doc |
| `L7.160` | scripts.backfill-observer-hotfix-trail | `scripts/backfill-observer-hotfix-trail.py` | missing | `agent/tests/test_backfill_observer_hotfix_trail.py` | doc |
| `L7.161` | scripts.etl-backlog-md-to-db | `scripts/etl-backlog-md-to-db.py` | `docs/roles/observer.md` | `agent/tests/test_backlog_db.py` | none |
| `L7.162` | scripts.observer-watch-chain | `scripts/observer-watch-chain.py` | missing | missing | doc, test |
| `L7.163` | scripts.phase-z-v2 | `scripts/phase-z-v2.py` | `docs/governance/reconcile-workflow.md` | `agent/tests/test_cr5_gatekeeper_overlay.py`<br>`agent/tests/test_phase_z_v2_architecture_relations.py`<br>`agent/tests/test_phase_z_v2_calibrate_script.py`<br>+7 more | none |
| `L7.164` | scripts.phase-z-v2-calibrate | `scripts/phase-z-v2-calibrate.py` | missing | `agent/tests/test_phase_z_v2_calibrate_script.py` | doc |
| `L7.165` | scripts.phase-z-v2-enrich | `scripts/phase-z-v2-enrich.py` | missing | missing | doc, test |
| `L7.166` | scripts.rebuild_graph | `scripts/rebuild_graph.py` | `docs/governance/plan-graph-driven-doc.md` | missing | test |
| `L7.167` | scripts.reconcile-dropped-nodes | `scripts/reconcile-dropped-nodes.py` | missing | `agent/tests/test_reconcile_dropped_nodes.py` | doc |
| `L7.168` | scripts.reconcile-scoped | `scripts/reconcile-scoped.py` | missing | `agent/tests/test_baseline_slice.py`<br>`agent/tests/test_qa_graph_delta_review.py`<br>`agent/tests/test_reconcile_scope_cli.py` | doc |
| `L7.169` | scripts.validate_stage_output | `scripts/validate_stage_output.py` | `docs/coordinator-rules.md`<br>`docs/roles/dev.md` | `agent/tests/test_dev_result_validator.py`<br>`agent/tests/test_qa_graph_delta_review.py` | none |
| `L7.170` | setup | `setup.py` | missing | `agent/tests/test_auth_failure_classifier.py`<br>`agent/tests/test_auto_chain_prd_declarations_threading.py`<br>`agent/tests/test_autochain_new_file_binding.py`<br>+48 more | doc |
| `L7.171` | start_governance | `start_governance.py` | missing | `agent/tests/test_config_validation.py`<br>`agent/tests/test_gov_stderr_persistence.py`<br>`agent/tests/test_governance_host_migration_round1.py`<br>+4 more | doc |

## Unresolved Files

| Path | Kind | Status | Reason |
|---|---|---|---|
| `agent/ACCEPTANCE_WORKFLOW.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/config/aming-claw-yaml.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/config/mcp-json.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/docs-dev-reposition-impl-prompt.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-deploy-event-driven-dual-redeploy.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-graph-bootstrap-from-commits.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-phase-z-v2-symbol-topology.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-qa-commit-sweep-integration.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-reconcile-cluster-driven-standard-chain.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-reconcile-commit-sweep.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-reconcile-comprehensive-2026-04-25.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-reconcile-phase-a-exclude-worktrees.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-reconcile-scoped-2026-04-25.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-reconcile-task-sweep.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-stage-output-preflight-validator.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/dev/proposal-version-gate-as-commit-trailer.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/governance/audit-process.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/governance/implementation-process.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/governance/pm-stage.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/roles/coordinator.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/roles/gatekeeper.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/roles/qa.md` | `doc` | `orphan` | doc file not attached to any feature cluster |
| `docs/roles/tester.md` | `doc` | `orphan` | doc file not attached to any feature cluster |

## Index Docs

| Path | Status | Graph Referenced |
|---|---|---:|
| `README.md` | `orphan` | `False` |
| `WORKFLOW.md` | `orphan` | `False` |
| `docs/api/README.md` | `orphan` | `False` |
| `docs/config/README.md` | `orphan` | `False` |
| `docs/governance/README.md` | `orphan` | `False` |
| `docs/roles/README.md` | `orphan` | `False` |
