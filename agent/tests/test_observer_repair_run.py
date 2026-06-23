from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from agent.governance import observer_repair_run


def _rows():
    return [
        {
            "bug_id": "AC-TIMELINE-APPEND-ROUTE-WAIVER-SCHEMA-20260602",
            "title": "task_timeline_append protected gate advertises route_waiver recovery but MCP entrypoint does not consume it",
            "status": "OPEN",
            "priority": "P2",
            "details_md": "MCP schema does not expose route_waiver and route_token recovery path.",
            "chain_trigger_json": "{}",
        },
        {
            "bug_id": "AC-GRAPH-PENDING-SCOPE-QUEUE-TIMEOUT-20260602",
            "title": "graph_pending_scope_queue times out for external content-sys target commit",
            "status": "OPEN",
            "priority": "P2",
            "details_md": "pending scope queue timeout leaves graph stale after target commit.",
            "chain_trigger_json": "{}",
        },
        {
            "bug_id": "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
            "title": "Docker context fixture close blocked by route and timeline evidence",
            "status": "OPEN",
            "priority": "P1",
            "details_md": "missing implementation verification close_ready independent_verification and route_identity_mismatch.",
            "chain_trigger_json": json.dumps(
                {
                    "depends_on": [
                        "AC-TIMELINE-APPEND-ROUTE-WAIVER-SCHEMA-20260602",
                        "AC-GRAPH-PENDING-SCOPE-QUEUE-TIMEOUT-20260602",
                    ]
                }
            ),
        },
    ]


def _passing_timeline_precheck():
    return {
        "bug_id": "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
        "can_close": True,
        "timeline_gate": {
            "present_event_kinds": ["implementation", "verification", "close_ready"],
            "missing_event_kinds": [],
            "route_context_gate": {
                "present_requirement_ids": [
                    "route_context",
                    "route_action_precheck",
                    "bounded_implementation_worker_dispatch",
                    "mf_subagent_startup",
                    "independent_verification_lane",
                ],
                "missing_requirement_ids": [],
            },
        },
    }


def _single_route_row():
    return {
        "bug_id": "AC-EXTERNAL-ROUTE-PRECHECK-20260603",
        "title": "external route action precheck",
        "status": "OPEN",
        "priority": "P1",
        "details_md": "external route action precheck materialization",
        "chain_trigger_json": "{}",
    }


def _external_route_identity():
    return {
        "route_context_hash": "sha256:external-route-context",
        "prompt_contract_id": "rprompt-external-route",
        "prompt_contract_hash": "sha256:external-prompt-contract",
        "visible_injection_manifest_hash": "sha256:external-visible-manifest",
    }


def _command_route_identity():
    return {
        **_external_route_identity(),
        "route_id": "route-command-consumption-test",
    }


def test_repair_run_plan_is_deterministic_and_judge_independent():
    kwargs = {
        "project_id": "aming-claw",
        "root_backlog_ids": [row["bug_id"] for row in _rows()],
        "backlog_rows": _rows(),
        "blockers": ["route_token_required from protected backlog_upsert"],
        "actor": "observer-test",
    }

    first = observer_repair_run.build_repair_run_plan(**kwargs)
    second = observer_repair_run.build_repair_run_plan(**kwargs)

    assert first["repair_run_id"] == second["repair_run_id"]
    assert first["route_context"]["route_context_hash"] == second["route_context"]["route_context_hash"]
    assert first["runtime_independent_of_judgment_brain"] is True
    assert first["route_context"]["judgment_brain_required"] is False
    assert first["route_context"]["authorizes_protected_write"] is False
    assert first["protected_write_policy"]["diagnostic_events_count_as_close_evidence"] is False
    assert first["observer_step_monitor"]["schema_version"] == "observer_step_monitor.v1"


def test_repair_run_groups_lanes_and_orders_dependencies():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"] for row in _rows()],
        backlog_rows=_rows(),
    )

    lane_ids = [lane["lane_id"] for lane in plan["lane_dispatches"]]
    assert "runtime_schema" in lane_ids
    assert "graph_reconcile" in lane_ids
    assert "route_context" in lane_ids
    assert "independent_verification" in lane_ids
    assert "close_gate" in lane_ids

    edges = {
        (edge["from"], edge["to"], edge["reason"])
        for edge in plan["backlog_dependency_dag"]["edges"]
    }
    assert (
        "AC-TIMELINE-APPEND-ROUTE-WAIVER-SCHEMA-20260602",
        "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
        "declared_dependency",
    ) in edges
    assert (
        "AC-GRAPH-PENDING-SCOPE-QUEUE-TIMEOUT-20260602",
        "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
        "declared_dependency",
    ) in edges
    assert any(edge[2] == "schema_before_protected_write" for edge in edges)


def test_repair_run_step_monitor_passes_when_required_observer_evidence_is_present():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"] for row in _rows()],
        backlog_rows=_rows(),
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
        timeline_prechecks=[_passing_timeline_precheck()],
        route_context_seed={"graph_query_trace_ids": ["gqt-test-route-context"]},
    )

    monitor = plan["observer_step_monitor"]

    assert monitor["status"] == "passed"
    assert monitor["missing_steps"] == []
    assert monitor["missing_step_ids"] == []
    assert monitor["backlog_followup"]["required"] is False
    assert monitor["close_policy"]["may_close"] is True
    assert {step["step_id"] for step in monitor["present_steps"]} >= {
        "route_context",
        "route_action_precheck",
        "graph_first_discovery",
        "backlog_row",
        "bounded_implementation_worker_dispatch",
        "mf_subagent_startup",
        "independent_verification_lane",
        "implementation",
        "verification",
        "close_ready",
    }


def test_repair_run_step_monitor_surfaces_forgotten_steps_and_backlog_followup():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=["AC-MISSING-WORKFLOW-STEPS-20260602"],
        backlog_rows=[],
        blockers=["graph unavailable and missing independent verification"],
    )

    monitor = plan["observer_step_monitor"]
    missing = set(monitor["missing_step_ids"])

    assert monitor["status"] == "blocked"
    assert "graph_first_discovery" in missing
    assert "backlog_row" in missing
    assert "bounded_implementation_worker_dispatch" in missing
    assert "mf_subagent_startup" in missing
    assert "independent_verification_lane" in missing
    assert "implementation" in missing
    assert "verification" in missing
    assert "close_ready" in missing
    assert monitor["backlog_followup"]["required"] is True
    assert "create_or_update_missing_backlog_row" in monitor["backlog_followup"]["actions"]
    assert "upsert_or_update_backlog_before_mutation" in monitor["next_actions"]


def test_repair_run_step_monitor_keeps_independent_verification_separate():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"] for row in _rows()],
        backlog_rows=_rows(),
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        timeline_prechecks=[
            {
                "bug_id": "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
                "can_close": False,
                "timeline_gate": {
                    "present_event_kinds": ["implementation", "verification", "close_ready"],
                    "missing_event_kinds": [],
                    "route_context_gate": {
                        "present_requirement_ids": [
                            "route_context",
                            "route_action_precheck",
                            "bounded_implementation_worker_dispatch",
                            "mf_subagent_startup",
                        ],
                        "missing_requirement_ids": ["independent_verification_lane"],
                    },
                },
            }
        ],
        route_context_seed={"graph_query_trace_ids": ["gqt-test-route-context"]},
    )

    monitor = plan["observer_step_monitor"]

    assert "implementation" not in monitor["missing_step_ids"]
    assert "verification" not in monitor["missing_step_ids"]
    assert "close_ready" not in monitor["missing_step_ids"]
    assert "independent_verification_lane" in monitor["missing_step_ids"]
    assert "dispatch_independent_verification_lane" in monitor["next_actions"]


def test_repair_run_step_monitor_blocks_direct_observer_mutation_without_exception():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=["AC-DIRECT-OBSERVER-MUTATION-20260602"],
        backlog_rows=[
            {
                "bug_id": "AC-DIRECT-OBSERVER-MUTATION-20260602",
                "title": "direct observer edit request",
                "status": "OPEN",
                "priority": "P2",
                "details_md": "observer direct mutation requested",
            }
        ],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        route_context_seed={
            "graph_query_trace_ids": ["gqt-direct-mutation"],
            "observer_direct_mutation": True,
        },
    )

    monitor = plan["observer_step_monitor"]
    policy = monitor["direct_observer_mutation_policy"]

    assert policy["default_action"] == "dispatch_bounded_mf_sub"
    assert policy["direct_code_edits_allowed_by_default"] is False
    assert policy["status"] == "blocked_missing_exception"
    assert "timeline_evidence_before_mutation" in policy["required_exception_evidence"]
    assert "observer_direct_mutation_exception" in monitor["missing_step_ids"]
    assert (
        "run_and_record_observer_direct_mutation_exception_validator"
        in monitor["next_actions"]
    )


def test_repair_run_step_monitor_rejects_bare_direct_mutation_acceptance_flag():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=["AC-DIRECT-OBSERVER-MUTATION-20260602"],
        backlog_rows=[
            {
                "bug_id": "AC-DIRECT-OBSERVER-MUTATION-20260602",
                "title": "direct observer edit request",
                "status": "OPEN",
                "priority": "P2",
                "details_md": "observer direct mutation requested",
            }
        ],
        route_context_seed={
            "observer_direct_mutation": True,
            "role": "observer",
            "direct_mutation_exception": {
                "accepted": True,
                "allowed": True,
                "tiny_deterministic": True,
            },
        },
    )

    monitor = plan["observer_step_monitor"]
    policy = monitor["direct_observer_mutation_policy"]

    assert policy["status"] == "blocked_missing_exception"
    assert policy["accepted_exception"] is False
    assert "explicit reason" in policy["validation_error"]
    assert "observer_direct_mutation_exception" in monitor["missing_step_ids"]


def test_repair_run_classifies_gate_failures_into_next_legal_actions():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="content-sys",
        root_backlog_ids=[],
        blockers=[
            {
                "error": "route_token_required",
                "message": "route_token is required for protected governance action backlog_close",
            },
            "graph_pending_scope_queue timed out while graph stale",
            "route_identity_mismatch after stale hand-written route context",
        ],
    )

    assert "return_to_route_context_and_request_valid_route_token" in plan["next_legal_actions"]
    assert "replace_queue_wait_with_bounded_reconcile_fallback" in plan["next_legal_actions"]
    assert "supersede_or_reset_stale_route_identity_before_retry" in plan["next_legal_actions"]
    assert plan["checkpoints"][0]["checkpoint_id"] == "diagnosed"
    assert plan["checkpoints"][0]["status"] == "passed"


def test_repair_run_includes_service_generated_route_preview():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"] for row in _rows()],
        backlog_rows=_rows(),
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "status": "passed", "dirty": False, "dirty_files": []},
    )

    preview = plan["route_service_preview"]
    bundle = preview["prompt_bundle"]
    identity = preview["service_generated_route_identity"]
    prechecks = {item["precheck_id"]: item for item in preview["action_prechecks"]}
    prompt_source = preview["prompt_context_event"]["source_event"]
    dispatch_source = prechecks["observer_dispatch_bounded_worker"]["source_event"]
    bundle_json = json.dumps(bundle, sort_keys=True)

    assert preview["available"] is True
    assert preview["template_id"] == "mf_workflow_runtime.v1"
    assert preview["counts_as_close_evidence"] is False
    assert preview["authorizes_protected_write"] is False
    assert identity["route_context_hash"] == bundle["route_context_hash"]
    assert identity["prompt_contract_hash"] == bundle["prompt_contract_hash"]
    assert identity["prompt_contract_id"] == bundle["prompt_contract"]["prompt_contract_id"]
    assert identity["route_context_hash"].startswith("sha256:")
    assert identity["route_context_hash"] != plan["route_context"]["route_context_hash"]
    assert "raw_prompt" not in bundle_json
    assert prompt_source["event_type"] == "route.prompt_context.requested"
    assert prompt_source["event_kind"] == "route_context"
    assert prompt_source["payload"]["template_id"] == "mf_workflow_runtime.v1"
    assert prompt_source["status"] == "requested"
    assert prompt_source["verification"]["counts_as_close_evidence"] is False
    assert dispatch_source["event_type"] == "route.action.requested"
    assert dispatch_source["event_kind"] == "route_action_precheck"
    assert dispatch_source["payload"]["template_id"] == "mf_workflow_runtime.v1"
    assert dispatch_source["verification"]["counts_as_close_evidence"] is False
    assert prechecks["observer_dispatch_bounded_worker"]["result"]["decision"] == "allow"
    assert prechecks["implementation_worker_apply_patch"]["result"]["decision"] == "block"
    assert "bounded dispatch/startup evidence" in (
        prechecks["implementation_worker_apply_patch"]["route_action_gate"]["reason"]
    )

    materialization = observer_repair_run.build_route_service_materialization(plan)
    assert materialization["recordable"] is False
    assert [event["event_type"] for event in materialization["source_events"]] == [
        "route.prompt_context.requested",
        "route.action.requested",
    ]
    assert materialization["counts_as_close_evidence"] is False


def test_external_route_action_precheck_materializes_public_source_event():
    row = _single_route_row()
    identity = _external_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
    )

    assert plan["route_service_preview"]["available"] is True
    assert plan["route_service_preview"]["template_id"] == "mf_workflow_runtime.v1"

    materialization = observer_repair_run.build_route_service_materialization(
        plan,
        action_precheck_id="external-dispatch-precheck",
        external_route_identity=identity,
        action_precheck={
            **identity,
            "caller_role": "observer",
            "action": "dispatch_bounded_worker",
            "allowed": True,
            "private_provider_body": "do-not-leak-provider-body",
            "raw_prompt": "do-not-leak-raw-prompt",
            "hidden_context": "do-not-leak-hidden-context",
        },
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
        actor="observer-test",
    )

    assert materialization["ok"] is True
    assert materialization["recordable"] is True
    assert materialization["route_action_precheck"]["present"] is True
    assert materialization["route_action_precheck"]["valid"] is True
    assert materialization["route_action_precheck"]["source"] == "external"
    assert materialization["authorizes_protected_worker_dispatch_evidence"] is True
    assert materialization["authorizes_protected_write"] is False
    assert [event["event_type"] for event in materialization["source_events"]] == [
        "route.action.requested"
    ]

    source = materialization["source_events"][0]
    assert source["event_kind"] == "route_action_precheck"
    assert source["status"] == "allowed"
    assert source["payload"]["precheck_id"] == "external-dispatch-precheck"
    assert source["payload"]["route_context_hash"] == identity["route_context_hash"]
    assert source["verification"]["route_action_gate"]["allowed"] is True
    materialized_json = json.dumps(materialization, sort_keys=True)
    assert "do-not-leak-provider-body" not in materialized_json
    assert "do-not-leak-raw-prompt" not in materialized_json
    assert "do-not-leak-hidden-context" not in materialized_json


def test_external_route_action_precheck_preserves_contract_lineage():
    row = _single_route_row()
    identity = {
        **_external_route_identity(),
        "route_token_ref": "rtok-successor-lineage",
        "contract_execution_id": "cex-successor-route-precheck",
        "parent_contract_execution_id": "cex-onboard-root",
    }
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
    )

    materialization = observer_repair_run.build_route_service_materialization(
        plan,
        action_precheck_id="external-dispatch-precheck",
        external_route_identity=identity,
        action_precheck={
            **_external_route_identity(),
            "caller_role": "observer",
            "action": "dispatch_bounded_worker",
            "allowed": True,
        },
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
        actor="observer-test",
    )

    assert materialization["ok"] is True
    route_precheck = materialization["route_action_precheck"]
    assert route_precheck["route_identity"]["route_token_ref"] == "rtok-successor-lineage"
    assert (
        route_precheck["route_identity"]["contract_execution_id"]
        == "cex-successor-route-precheck"
    )
    source = materialization["source_events"][0]
    assert source["payload"]["route_token_ref"] == "rtok-successor-lineage"
    assert source["payload"]["route_action_gate"]["route_token_ref"] == "rtok-successor-lineage"
    assert source["payload"]["contract_execution_id"] == "cex-successor-route-precheck"
    assert source["verification"]["contract_execution_id"] == "cex-successor-route-precheck"
    assert source["artifact_refs"]["parent_contract_execution_id"] == "cex-onboard-root"


def test_command_route_identity_consumption_reports_supplied_identity():
    row = _single_route_row()
    identity = _command_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
    )

    materialization = observer_repair_run.build_route_service_materialization(
        plan,
        action_precheck_id="external-dispatch-precheck",
        external_route_identity=identity,
        action_precheck={
            **identity,
            "caller_role": "observer",
            "action": "dispatch_bounded_worker",
            "allowed": True,
        },
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
        actor="observer-test",
    )

    consumption = materialization["route_identity_consumption"]
    assert materialization["ok"] is True
    assert materialization["recordable"] is True
    assert consumption["consumed"] is True
    assert consumption["superseded"] is False
    assert consumption["consumed_route_identity"] == identity
    assert materialization["route_action_precheck"]["route_identity"] == identity
    assert "route_identity_supersession" not in materialization
    assert [event["event_kind"] for event in materialization["source_events"]] == [
        "route_action_precheck"
    ]


def test_command_route_identity_without_precheck_returns_supersession_lineage():
    row = _single_route_row()
    identity = _command_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
    )

    materialization = observer_repair_run.build_route_service_materialization(
        plan,
        external_route_identity=identity,
        action_precheck={},
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False, "dirty_files": []},
        actor="observer-test",
    )

    consumption = materialization["route_identity_consumption"]
    supersession = materialization["route_identity_supersession"]
    generated_identity = materialization["service_generated_route_identity"]

    assert materialization["ok"] is True
    assert materialization["recordable"] is True
    assert consumption["consumed"] is False
    assert consumption["superseded"] is True
    assert consumption["supplied_route_identity"] == identity
    assert consumption["generated_route_identity"] == generated_identity
    assert supersession["status"] == "superseded"
    assert supersession["superseded_route_identity"] == identity
    assert supersession["canonical_route_identity"] == generated_identity
    assert supersession["source_event"]["event_kind"] == "route_identity_supersede"
    assert supersession["source_event"]["status"] == "accepted"
    assert supersession["source_event"]["payload"]["service_router_suppress"] is True
    assert [event["event_kind"] for event in materialization["source_events"]] == [
        "route_context",
        "route_identity_supersede",
        "route_action_precheck",
    ]
    assert supersession["raw_prompt_excluded"] is True
    assert supersession["hidden_context_excluded"] is True


def test_external_route_action_precheck_rejects_mismatched_identity():
    row = _single_route_row()
    identity = _external_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
    )

    materialization = observer_repair_run.build_route_service_materialization(
        plan,
        action_precheck_id="external-dispatch-precheck",
        external_route_identity=identity,
        action_precheck={
            **identity,
            "route_context_hash": "sha256:wrong-route-context",
            "caller_role": "observer",
            "action": "dispatch_bounded_worker",
            "allowed": True,
        },
    )

    assert materialization["ok"] is False
    assert materialization["recordable"] is False
    assert materialization["missing_source_events"] == [
        "route.action_precheck:external-dispatch-precheck"
    ]
    assert materialization["route_action_precheck"]["present"] is False
    assert materialization["route_action_precheck"]["valid"] is False
    assert materialization["route_action_precheck"]["route_identity_mismatch_fields"] == [
        "route_context_hash"
    ]


def test_external_route_identity_with_empty_action_precheck_blocks():
    row = _single_route_row()
    identity = _external_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
    )

    materialization = observer_repair_run.build_route_service_materialization(
        plan,
        action_precheck_id="external-dispatch-precheck",
        external_route_identity=identity,
        action_precheck={},
    )

    assert materialization["ok"] is False
    assert materialization["recordable"] is False
    assert materialization["missing_source_events"] == [
        "route.action_precheck:external-dispatch-precheck"
    ]
    assert materialization["authorizes_protected_worker_dispatch_evidence"] is False
    assert materialization["route_action_precheck"]["present"] is False
    assert materialization["route_action_precheck"]["valid"] is False
    assert (
        materialization["route_action_precheck"]["reason"]
        == "external action precheck packet or source marker is required"
    )


def test_missing_external_route_action_precheck_source_event_still_blocks():
    row = _single_route_row()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
    )

    materialization = observer_repair_run.build_route_service_materialization(
        plan,
        action_precheck_id="external-dispatch-precheck",
    )

    assert materialization["ok"] is False
    assert materialization["recordable"] is False
    assert materialization["route_action_precheck"]["present"] is False
    assert materialization["missing_source_events"] == [
        "route.action_precheck:external-dispatch-precheck"
    ]


# ---------------------------------------------------------------------------
# AC-REPAIR-RUN-LIVE-IDENTITY-SUPERSEDE-GUARD-20260610
# Regression tests reproducing the #3384-3393 fork incident.
# ---------------------------------------------------------------------------


def _live_canonical_route_identity():
    """Simulates the canonical route identity that has accepted startup evidence."""
    return {
        "route_id": "route-live-canonical-3384",
        "route_context_hash": "sha256:live-canonical-route-context-3384",
        "prompt_contract_id": "rprompt-live-canonical-3384",
        "prompt_contract_hash": "sha256:live-canonical-prompt-contract-3384",
        "visible_injection_manifest_hash": "sha256:live-canonical-visible-manifest-3384",
    }


def _accepted_startup_event(route_identity: dict, event_id: int = 3384) -> dict:
    """Simulate an accepted mf_subagent_startup timeline event with the given identity."""
    return {
        "id": event_id,
        "event_kind": "mf_subagent_startup",
        "event_type": "mf_subagent.startup",
        "status": "passed",
        "payload": {
            "mf_subagent_startup_gate": {
                "route_context_hash": route_identity.get("route_context_hash", ""),
                "prompt_contract_id": route_identity.get("prompt_contract_id", ""),
                "route_id": route_identity.get("route_id", ""),
            },
        },
        "verification": {},
        "artifact_refs": {},
    }


def _accepted_verification_event(route_identity: dict, event_id: int = 3390) -> dict:
    """Simulate an accepted independent_verification timeline event."""
    return {
        "id": event_id,
        "event_kind": "independent_verification",
        "event_type": "mf.verification",
        "status": "accepted",
        "payload": {
            "route_context_hash": route_identity.get("route_context_hash", ""),
            "prompt_contract_id": route_identity.get("prompt_contract_id", ""),
        },
        "verification": {},
        "artifact_refs": {},
    }


def test_incident_3384_identity_only_record_true_errors_and_records_nothing():
    """#3384-3393: route_identity supplied + record=True + empty precheck must fail loudly.

    The buggy path would degrade to replay-mint and record route.identity.superseded
    against the live canonical identity.  The fix must return ok=False immediately,
    record nothing, and explain the two valid modes.
    """
    row = _single_route_row()
    live_identity = _live_canonical_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False},
    )

    # Reproduce incident: identity supplied, record=True, action_precheck empty
    result = observer_repair_run.build_route_service_materialization(
        plan,
        external_route_identity=live_identity,
        action_precheck={},  # empty — the missing packet that triggered the bug
        record=True,
    )

    # Must fail loudly — not ok, not recordable
    assert result["ok"] is False
    assert result["recordable"] is False
    assert result["record_blocked"] is True
    assert result["record_blocked_reason"] == "identity_only_without_action_precheck"
    assert (
        result["reason"] == "external action precheck packet or source marker is required"
    )
    # Must not contain any supersession event
    assert "route_identity_supersession" not in result
    assert result["source_events"] == []
    # Must list two valid next_legal_actions
    assert len(result["next_legal_actions"]) == 2
    assert any("action_precheck_packet" in a for a in result["next_legal_actions"])
    assert any("replay_mint" in a or "omitting" in a for a in result["next_legal_actions"])


def test_incident_3384_dry_run_behavior_unchanged():
    """dry-run (record=False) with identity-only still returns supersession lineage as before.

    The identity-only fail-loud guard only fires when record=True.
    Dry-run mode must remain unchanged so existing callers can preview the plan.
    """
    row = _single_route_row()
    live_identity = _live_canonical_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False},
    )

    # Dry-run (record=False, the default) must still work as before
    result = observer_repair_run.build_route_service_materialization(
        plan,
        external_route_identity=live_identity,
        action_precheck={},
        record=False,
    )

    # Dry-run result should NOT have record_blocked
    assert "record_blocked" not in result
    # Supersession lineage is still produced in dry-run for preview purposes
    assert "route_identity_supersession" in result
    assert result["route_identity_consumption"]["superseded"] is True


def test_incident_3384_pure_mint_no_identity_unchanged():
    """Fresh replay-mint (no claimed identity) must be unchanged by the guard."""
    row = _single_route_row()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False},
    )

    # No external identity — pure mint mode, record=True
    result = observer_repair_run.build_route_service_materialization(
        plan,
        external_route_identity=None,
        action_precheck=None,
        record=True,
    )

    # Pure mint is not blocked by the guard; it may or may not be recordable
    # depending on preview availability; the key assertion is no record_blocked
    assert "record_blocked" not in result


def test_guard_live_identity_supersession_refuses_without_force():
    """guard_live_identity_supersession blocks when protecting events exist and no force."""
    live_identity = _live_canonical_route_identity()
    startup_event = _accepted_startup_event(live_identity, event_id=3384)

    result = observer_repair_run.guard_live_identity_supersession(
        proposed_superseded_identity=live_identity,
        timeline_events=[startup_event],
    )

    assert result["ok"] is False
    assert result["protected"] is True
    assert 3384 in result["protecting_event_ids"]
    assert "force_supersede" in result["reason"]
    assert result["force_accepted"] is False


def test_guard_live_identity_supersession_allows_with_force_and_reason():
    """guard_live_identity_supersession allows when force_supersede=True and force_reason given."""
    live_identity = _live_canonical_route_identity()
    startup_event = _accepted_startup_event(live_identity, event_id=3384)

    result = observer_repair_run.guard_live_identity_supersession(
        proposed_superseded_identity=live_identity,
        timeline_events=[startup_event],
        force_supersede=True,
        force_reason="manual reverse-supersession #3415: recovery operator approval",
    )

    assert result["ok"] is True
    assert result["protected"] is True
    assert result["force_accepted"] is True
    assert 3384 in result["protecting_event_ids"]


def test_guard_live_identity_supersession_refuses_force_without_reason():
    """force_supersede=True without a non-empty force_reason is still refused."""
    live_identity = _live_canonical_route_identity()
    startup_event = _accepted_startup_event(live_identity, event_id=3384)

    result = observer_repair_run.guard_live_identity_supersession(
        proposed_superseded_identity=live_identity,
        timeline_events=[startup_event],
        force_supersede=True,
        force_reason="",  # empty — must be refused
    )

    assert result["ok"] is False
    assert result["force_accepted"] is False


def test_guard_live_identity_supersession_passes_no_protecting_events():
    """guard allows when no protecting events exist for the proposed identity."""
    live_identity = _live_canonical_route_identity()
    other_identity = {
        "route_id": "route-other-9999",
        "route_context_hash": "sha256:other-context",
        "prompt_contract_id": "rprompt-other-9999",
    }
    # Startup event is for a different identity — not a protector
    unrelated_event = _accepted_startup_event(other_identity, event_id=9999)

    result = observer_repair_run.guard_live_identity_supersession(
        proposed_superseded_identity=live_identity,
        timeline_events=[unrelated_event],
    )

    assert result["ok"] is True
    assert result["protected"] is False
    assert result["protecting_event_ids"] == []


def test_guard_live_identity_supersession_matches_verification_events():
    """guard detects independent_verification events as protectors too."""
    live_identity = _live_canonical_route_identity()
    verification_event = _accepted_verification_event(live_identity, event_id=3390)

    result = observer_repair_run.guard_live_identity_supersession(
        proposed_superseded_identity=live_identity,
        timeline_events=[verification_event],
    )

    assert result["ok"] is False
    assert result["protected"] is True
    assert 3390 in result["protecting_event_ids"]


def test_incident_3384_record_true_with_valid_precheck_still_works():
    """record=True with identity + valid precheck packet must not be blocked by the guard."""
    row = _single_route_row()
    live_identity = _live_canonical_route_identity()
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"]],
        backlog_rows=[row],
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "dirty": False},
    )

    # Valid precheck packet with all source markers
    result = observer_repair_run.build_route_service_materialization(
        plan,
        external_route_identity=live_identity,
        action_precheck={
            **live_identity,
            "caller_role": "observer",
            "action": "dispatch_bounded_worker",
            "allowed": True,
        },
        record=True,
    )

    # Must NOT be blocked by identity-only guard
    assert "record_blocked" not in result


# ---------------------------------------------------------------------------
# Server-level / runtime-path tests (AC-REPAIR-RUN-LIVE-IDENTITY-SUPERSEDE-GUARD-20260610)
# These tests exercise the HTTP server handler path to prove guard_live_identity_supersession
# is wired in and enforced end-to-end.
# ---------------------------------------------------------------------------


def _server_conn(tmp_dir: str):
    """Set up a temp project DB for server handler tests."""
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir
    os.makedirs(
        os.path.join(tmp_dir, "codex-tasks", "state", "governance", "proj"),
        exist_ok=True,
    )
    from agent.governance.db import get_connection

    return get_connection("proj")


def _server_ctx(body: dict | None = None, *, project_id: str = "proj"):
    """Build a minimal RequestContext for the route-evidence handler."""
    from agent.governance import server

    return server.RequestContext(
        None,
        "POST",
        {"project_id": project_id},
        {},
        body or {},
        "req-server-test",
        "",
        "",
    )


def _insert_backlog_row(conn, bug_id: str):
    """Insert a minimal MF-in-progress backlog row."""
    contract = {"template_id": "mf_parallel.v1", "contract_instance_id": bug_id}
    conn.execute(
        """INSERT INTO backlog_bugs
           (bug_id, title, status, priority, target_files, test_files,
            acceptance_criteria, chain_trigger_json, mf_type, bypass_policy_json,
            created_at, updated_at)
           VALUES (?, ?, 'MF_IN_PROGRESS', 'P0', ?, ?, ?, ?, 'chain_rescue', ?,
                   '2026-06-10T00:00:00Z', '2026-06-10T00:00:00Z')""",
        (
            bug_id,
            "Guard server path test",
            json.dumps(["agent/governance/observer_repair_run.py"]),
            json.dumps(["agent/tests/test_observer_repair_run.py"]),
            json.dumps(["server-level guard test"]),
            json.dumps(contract),
            json.dumps({"mf_type": "chain_rescue"}),
        ),
    )
    conn.commit()


def _insert_startup_timeline_event(conn, bug_id: str, route_identity: dict, event_id_hint: int = 9001):
    """Record a fake accepted mf_subagent_startup timeline event to act as a live-identity protector."""
    from agent.governance import task_timeline

    task_timeline.record_event(
        conn,
        project_id="proj",
        backlog_id=bug_id,
        event_type="mf_subagent.startup",
        phase="startup",
        event_kind="mf_subagent_startup",
        actor="observer-on-behalf-of:mf-sub-test",
        status="passed",
        payload={
            "on_behalf_of": "mf-sub-test",
            "mf_subagent_startup_gate": {
                "route_context_hash": route_identity.get("route_context_hash", ""),
                "prompt_contract_id": route_identity.get("prompt_contract_id", ""),
                "route_id": route_identity.get("route_id", ""),
            },
        },
        verification={},
        artifact_refs={},
        correlation_id=f"startup-test-{event_id_hint}",
    )
    conn.commit()


class TestServerPathGuardWiring(unittest.TestCase):
    """Server-level tests: guard_live_identity_supersession wired into recording path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _server_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_server_identity_only_record_true_returns_error_without_precheck(self):
        """AC1: identity-only record=true through HTTP path errors naming missing precheck."""
        from agent.governance import server

        bug_id = "BUG-SERVER-GUARD-IDENTITY-ONLY-20260610"
        _insert_backlog_row(self.conn, bug_id)

        live_identity = _live_canonical_route_identity()
        # Call handler with record=True, route_identity supplied, action_precheck empty
        with self.assertRaises(server.GovernanceError) as cm:
            server.handle_observer_repair_run_route_evidence(
                _server_ctx(
                    body={
                        "root_backlog_ids": [bug_id],
                        "actor": "observer-test",
                        "record": True,
                        "route_identity": live_identity,
                        "action_precheck": {},  # missing — triggers guard
                        "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                    }
                )
            )
        # The error must come from build_route_service_materialization (record_blocked)
        # which is re-raised as GovernanceError by the not-recordable path
        err = cm.exception
        self.assertIn(err.code, (
            "observer_repair_route_evidence_not_recordable",
            "observer_repair_run_supersession_refused",
        ), f"unexpected error code: {err.code}")

    def test_server_external_validated_mode_allowed(self):
        """AC1 counterpart: external-validated (precheck packet + identity) must succeed."""
        from agent.governance import server, task_timeline

        bug_id = "BUG-SERVER-GUARD-EXTERNAL-VALIDATED-20260610"
        _insert_backlog_row(self.conn, bug_id)

        identity = {
            "route_context_hash": "sha256:server-guard-external-validated-context",
            "prompt_contract_id": "rprompt-server-guard-validated",
            "prompt_contract_hash": "sha256:server-guard-validated-hash",
            "visible_injection_manifest_hash": "sha256:server-guard-validated-manifest",
        }

        # record=True with a valid external precheck — should NOT be blocked
        result = server.handle_observer_repair_run_route_evidence(
            _server_ctx(
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "record": True,
                    "action_precheck_id": "external-dispatch-precheck",
                    "route_identity": identity,
                    "action_precheck": {
                        **identity,
                        "caller_role": "observer",
                        "action": "dispatch_bounded_worker",
                        "allowed": True,
                    },
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                }
            )
        )
        self.assertTrue(result["recorded"], result)

    def test_server_route_evidence_merges_top_level_contract_lineage(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-SERVER-TOP-LEVEL-LINEAGE-20260623"
        _insert_backlog_row(self.conn, bug_id)
        identity = {
            "route_context_hash": "sha256:server-lineage-context",
            "prompt_contract_id": "rprompt-server-lineage",
            "prompt_contract_hash": "sha256:server-lineage-prompt",
            "visible_injection_manifest_hash": "sha256:server-lineage-manifest",
        }

        result = server.handle_observer_repair_run_route_evidence(
            _server_ctx(
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "record": True,
                    "action_precheck_id": "external-dispatch-precheck",
                    "route_identity": identity,
                    "route_token_ref": "rtok-server-top-level-lineage",
                    "contract_execution_id": "cex-server-top-level-lineage",
                    "parent_contract_execution_id": "cex-server-parent",
                    "action_precheck": {
                        **identity,
                        "caller_role": "observer",
                        "action": "dispatch_bounded_worker",
                        "allowed": True,
                    },
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                }
            )
        )

        self.assertTrue(result["recorded"], result)
        events = task_timeline.list_events(
            self.conn,
            "proj",
            backlog_id=bug_id,
            event_kind="route_action_precheck",
            limit=20,
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["payload"]["route_token_ref"], "rtok-server-top-level-lineage")
        self.assertEqual(
            event["payload"]["route_action_gate"]["route_token_ref"],
            "rtok-server-top-level-lineage",
        )
        self.assertEqual(
            event["payload"]["contract_execution_id"],
            "cex-server-top-level-lineage",
        )
        self.assertEqual(
            event["verification"]["parent_contract_execution_id"],
            "cex-server-parent",
        )

    def test_server_guard_invoked_refuses_supersession_of_live_identity(self):
        """AC2: supersession of identity with accepted startup lineage is refused without force."""
        from agent.governance import server

        bug_id = "BUG-SERVER-GUARD-SUPERSESSION-REFUSED-20260610"
        _insert_backlog_row(self.conn, bug_id)

        # Canonical live identity has an accepted startup event
        live_identity = {
            "route_id": "route-server-guard-live-9001",
            "route_context_hash": "sha256:server-guard-live-context-9001",
            "prompt_contract_id": "rprompt-server-guard-live-9001",
            "prompt_contract_hash": "sha256:server-guard-live-hash-9001",
            "visible_injection_manifest_hash": "sha256:server-guard-live-manifest-9001",
        }
        _insert_startup_timeline_event(self.conn, bug_id, live_identity)

        # Patch build_route_service_materialization to return a supersession materialization
        # (mimicking the dry-run supersession path that attempt-1 tests already cover)
        fake_supersession_materialization = {
            "ok": True,
            "recordable": True,
            "backlog_id": bug_id,
            "route_identity_supersession": {
                "supplied_route_identity": live_identity,
                "source_event": {
                    "event_type": "route.identity.superseded",
                    "event_kind": "route_identity_supersede",
                    "backlog_id": bug_id,
                    "payload": {},
                    "phase": "dispatch",
                    "status": "requested",
                    "source_event_id": "supersession-test",
                },
            },
            "source_events": [
                {
                    "event_type": "route.identity.superseded",
                    "event_kind": "route_identity_supersede",
                    "backlog_id": bug_id,
                    "payload": {},
                    "phase": "dispatch",
                    "status": "requested",
                    "source_event_id": "supersession-test",
                }
            ],
            "missing_source_events": [],
        }

        with mock.patch.object(
            observer_repair_run,
            "build_route_service_materialization",
            return_value=fake_supersession_materialization,
        ):
            with self.assertRaises(server.GovernanceError) as cm:
                server.handle_observer_repair_run_route_evidence(
                    _server_ctx(
                        body={
                            "root_backlog_ids": [bug_id],
                            "actor": "observer-test",
                            "record": True,
                            "route_identity": live_identity,
                            "action_precheck": {
                                **live_identity,
                                "caller_role": "observer",
                                "action": "dispatch_bounded_worker",
                                "allowed": True,
                            },
                            "version_check": {"ok": True, "dirty": False},
                        }
                    )
                )
            err = cm.exception
            self.assertEqual(
                err.code,
                "observer_repair_run_supersession_refused",
                f"guard must refuse supersession; got: {err.code}",
            )
            self.assertIn("protecting", err.message)

    def test_server_guard_invoked_allows_with_force_supersede_and_reason(self):
        """AC2: supersession with force_supersede=true + force_reason bypasses guard."""
        from agent.governance import server, task_timeline

        bug_id = "BUG-SERVER-GUARD-SUPERSESSION-FORCED-20260610"
        _insert_backlog_row(self.conn, bug_id)

        live_identity = {
            "route_id": "route-server-guard-forced-9002",
            "route_context_hash": "sha256:server-guard-forced-context-9002",
            "prompt_contract_id": "rprompt-server-guard-forced-9002",
            "prompt_contract_hash": "sha256:server-guard-forced-hash-9002",
            "visible_injection_manifest_hash": "sha256:server-guard-forced-manifest-9002",
        }
        _insert_startup_timeline_event(self.conn, bug_id, live_identity, event_id_hint=9002)

        fake_supersession_materialization = {
            "ok": True,
            "recordable": True,
            "backlog_id": bug_id,
            "route_identity_supersession": {
                "supplied_route_identity": live_identity,
                "source_event": {
                    "event_type": "route.identity.superseded",
                    "event_kind": "route_identity_supersede",
                    "backlog_id": bug_id,
                    "payload": {},
                    "phase": "dispatch",
                    "status": "requested",
                    "source_event_id": "supersession-forced-test",
                },
            },
            "source_events": [
                {
                    "event_type": "route.identity.superseded",
                    "event_kind": "route_identity_supersede",
                    "backlog_id": bug_id,
                    "payload": {},
                    "phase": "dispatch",
                    "status": "requested",
                    "source_event_id": "supersession-forced-test",
                }
            ],
            "missing_source_events": [],
        }

        # With force_supersede=True and a non-empty force_reason, guard must allow
        with mock.patch.object(
            observer_repair_run,
            "build_route_service_materialization",
            return_value=fake_supersession_materialization,
        ):
            result = server.handle_observer_repair_run_route_evidence(
                _server_ctx(
                    body={
                        "root_backlog_ids": [bug_id],
                        "actor": "observer-test",
                        "record": True,
                        "route_identity": live_identity,
                        "action_precheck": {
                            **live_identity,
                            "caller_role": "observer",
                            "action": "dispatch_bounded_worker",
                            "allowed": True,
                        },
                        "version_check": {"ok": True, "dirty": False},
                        "force_supersede": True,
                        "force_reason": "operator-approved recovery #9002",
                    }
                )
            )
        self.assertTrue(result["recorded"], result)
