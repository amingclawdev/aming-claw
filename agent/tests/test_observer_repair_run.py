from __future__ import annotations

import json

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
