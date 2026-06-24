from __future__ import annotations

import json

from agent.governance.contract_state_runtime import build_contract_state_projection


def _event(
    event_id: int,
    kind: str,
    *,
    status: str = "passed",
    payload=None,
    project_id: str = "",
):
    return {
        "id": event_id,
        "project_id": project_id,
        "backlog_id": "AC-CONTRACT-RUNTIME",
        "event_kind": kind,
        "phase": "contract",
        "status": status,
        "payload": payload or {},
        "verification": {},
        "artifact_refs": {},
    }


def test_projection_keeps_generated_demo_requirements_conditional():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-1",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "conditional_required_evidence": [
                {
                    "condition": {"target_kind": "generated_demo"},
                    "evidence": ["demo_target_identity_check"],
                }
            ],
        }
    }

    regular = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    demo = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "target_kind": "generated_demo",
        },
    )

    assert regular["required_evidence"] == ["graph_query_schema_trace"]
    assert regular["conditional_required_evidence"][0]["active"] is False
    assert "demo_target_identity_check" not in regular["missing_evidence"]
    assert demo["conditional_required_evidence"][0]["active"] is True
    assert "demo_target_identity_check" in demo["required_evidence"]
    assert demo["next_legal_action"]["id"] == "graph_query_schema_trace"

    trigger_demo = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "chain_trigger_json": json.dumps({"target_kind": "generated_demo"}),
        },
    )

    assert trigger_demo["conditional_required_evidence"][0]["active"] is True
    assert "demo_target_identity_check" in trigger_demo["required_evidence"]


def test_next_action_append_hints_are_meta_contract_appendable():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-hint",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert hint["event_kind"] == "contract_state_changed"
    assert hint["satisfies_by"] == "payload.requirement_id"
    assert hint["payload"]["requirement_id"] == "graph_query_schema_trace"
    assert hint["actor_role"] == "observer"
    assert hint["meta_contract_gate"]["allowed"] is True


def test_independent_verification_lane_hint_is_qa_owned_and_appendable():
    contract = {
        "contract": {
            "contract_id": "mf_workflow_runtime.v1",
            "contract_template_id": "mf_workflow_runtime.v1",
            "contract_revision_id": "rev-qa-lane",
            "state": "selected",
            "evidence_requirements": [
                {
                    "id": "independent_verification_lane",
                    "phase": "verification",
                    "kind": "qa",
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    action = projection["next_legal_action"]
    hint = action["timeline_append_hint"]
    assert action["id"] == "independent_verification_lane"
    assert hint["event_kind"] == "independent_verification"
    assert hint["actor_role"] == "qa"
    assert hint["meta_contract_gate"]["allowed"] is True
    assert hint["satisfies_by"] == "event_kind"
    assert hint["payload"]["requirement_id"] == "independent_verification_lane"
    assert hint["payload"]["requirement_ids"] == ["independent_verification_lane"]
    assert "qa_review" in hint["accepted_event_kinds"]
    prefill = hint["role_bound_prefill_policy"]
    assert prefill["execution_owner_role"] == "qa"
    assert prefill["observer_owned"] is False
    assert prefill["actor_must_supply_evidence"] is True
    assert "contract_execution_id" in prefill["observer_prefill_fields"]
    assert "verification" in prefill["actor_owned_execution_fields"]

    completed = build_contract_state_projection(
        [_event(12, "qa_review")],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert [item["id"] for item in completed["completed_evidence"]] == [
        "independent_verification_lane"
    ]
    assert completed["missing_evidence"] == []


def test_focused_verification_hint_uses_appendable_qa_event_kind():
    contract = {
        "contract": {
            "contract_id": "observer_hotfix_direct_mutation.v1",
            "contract_template_id": "observer_hotfix_direct_mutation.v1",
            "contract_execution_id": "cex-hotfix",
            "state": "selected",
            "evidence_requirements": [
                {
                    "id": "focused_verification",
                    "event_kind": "verification",
                    "accepted_event_kinds": ["verification", "qa_verification"],
                    "match_policy": "canonical_event_kind",
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert projection["next_legal_action"]["id"] == "focused_verification"
    assert hint["event_kind"] == "qa_verification"
    assert hint["actor_role"] == "qa"
    assert hint["meta_contract_gate"]["allowed"] is True
    assert "verification" in hint["accepted_event_kinds"]


def test_work_mode_transition_hint_carries_route_identity_and_precheck_ref():
    route_identity = {
        "route_id": "route-1",
        "route_context_hash": "sha256:ctx",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt",
        "visible_injection_manifest_hash": "sha256:visible",
        "route_token_ref": "rtok-1",
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-work-mode",
            "state": "selected",
            "required_evidence": [
                "route_context",
                "route_action_precheck",
                "observer_work_mode_transition",
            ],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(1, "route_context", payload={"route_context": route_identity}),
            _event(
                2,
                "route_action_precheck",
                payload={"route_action_precheck": route_identity},
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "observer-task-1",
        },
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert projection["next_legal_action"]["id"] == "observer_work_mode_transition"
    assert hint["event_kind"] == "observer_work_mode_transition"
    assert hint["route_identity"] == route_identity
    assert hint["payload"]["route_identity"] == route_identity
    assert hint["payload"]["task_id"] == "observer-task-1"
    assert hint["payload"]["route_action_precheck_ref"]["event_id"] == 2
    assert hint["meta_contract_gate"]["allowed"] is True


def test_work_mode_transition_hint_is_writable_before_route_action_precheck():
    route_identity = {
        "route_id": "route-1",
        "route_context_hash": "sha256:ctx",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt",
        "visible_injection_manifest_hash": "sha256:visible",
        "route_token_ref": "rtok-1",
    }
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-work-mode-before-precheck",
            "state": "selected",
            "required_evidence": [
                "route_context",
                "observer_work_mode_transition",
                "route_action_precheck",
            ],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                1,
                "route_context",
                payload={
                    "route_identity": route_identity,
                    **route_identity,
                },
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "observer-task-1",
        },
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert projection["next_legal_action"]["id"] == "observer_work_mode_transition"
    assert hint["event_kind"] == "observer_work_mode_transition"
    assert hint["route_identity"] == route_identity
    assert hint["payload"]["route_identity"] == route_identity
    assert hint["payload"]["task_id"] == "observer-task-1"
    assert "route_action_precheck_ref" not in hint["payload"]
    assert hint["meta_contract_gate"]["allowed"] is True


def test_projection_computes_completed_missing_and_next_action():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-2",
            "state": "selected",
            "required_evidence": ["route_context", "route_action_precheck"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                41,
                "route_context",
                payload={
                    "route_context": {
                        "route_id": "route-1",
                        "route_context_hash": "sha256:ctx",
                        "prompt_contract_id": "rprompt-1",
                        "prompt_contract_hash": "sha256:prompt",
                        "visible_injection_manifest_hash": "sha256:visible",
                    }
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert [item["id"] for item in projection["completed_evidence"]] == ["route_context"]
    assert projection["missing_evidence"] == ["route_action_precheck"]
    assert projection["blocked_evidence"] == []
    assert projection["ordered_next_steps"][0]["id"] == "route_action_precheck"
    assert projection["next_legal_action"]["id"] == "route_action_precheck"
    assert projection["next_legal_action"]["source"] == "contract_state"
    assert projection["next_legal_action"]["precedence"] == "active_contract_missing_step"
    assert projection["next_legal_action"]["contract_execution_id"] == projection[
        "active_contract_execution"
    ]["contract_execution_id"]
    assert projection["next_legal_action"]["contract_chain_id"] == projection[
        "contract_chain_id"
    ]


def test_close_gate_projection_events_complete_hotfix_contract_state():
    contract = {
        "contract": {
            "contract_id": "observer_hotfix_direct_mutation.v1",
            "contract_template_id": "observer_hotfix_direct_mutation.v1",
            "contract_revision_id": "rev-close-projection",
            "state": "selected",
            "required_evidence": [
                "route_context",
                "route_action_precheck",
                "implementation",
                "verification",
                "close_ready",
            ],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(1, "route_context", status="requested"),
            _event(
                2,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.prompt_alert_bundle",
                    "contract_evidence": [
                        {"requirement_id": "route_context_hash"},
                        {"requirement_id": "prompt_contract_hash"},
                        {"requirement_id": "visible_injection_manifest"},
                    ],
                },
            ),
            _event(3, "route_action_precheck", status="requested"),
            _event(
                4,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.action_precheck",
                    "contract_evidence": [
                        {"requirement_id": "route_context_hash"},
                        {"requirement_id": "prompt_contract_id"},
                        {"requirement_id": "prompt_contract_hash"},
                        {"requirement_id": "route_action_allowed"},
                    ],
                },
            ),
            _event(
                5,
                "hotfix_under_action",
                status="accepted",
                payload={
                    "implementation_close_evidence": {
                        "counts_as_implementation": True,
                        "changed_files": ["agent/governance/contract_state_runtime.py"],
                    }
                },
            ),
            _event(
                6,
                "independent_verification",
                status="passed",
                payload={"reviewer_role": "independent_qa"},
            ),
            _event(7, "close_ready", status="accepted"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    completed = {item["id"]: item for item in projection["completed_evidence"]}
    assert completed["route_context"]["event_id"] == 2
    assert completed["route_action_precheck"]["event_id"] == 4
    assert completed["implementation"]["event_id"] == 5
    assert completed["verification"]["event_id"] == 6
    assert completed["close_ready"]["event_id"] == 7
    assert projection["missing_evidence"] == []
    assert projection["contract_complete"] is True
    assert (
        projection["next_legal_action"]["id"]
        == "issue_backlog_close_route_token_then_backlog_close"
    )


def test_route_requirements_ignore_generic_requirement_id_batches():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-route-direct",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "route_context",
                    "accepted_event_kinds": ["route_context"],
                    "match_policy": "canonical_event_kind",
                },
                {
                    "id": "route_action_precheck",
                    "accepted_event_kinds": ["route_action_precheck"],
                    "match_policy": "canonical_event_kind",
                },
            ],
        }
    }

    generic = build_contract_state_projection(
        [
            _event(
                51,
                "contract_state_changed",
                payload={
                    "requirement_ids": [
                        "route_context",
                        "route_action_precheck",
                    ]
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    canonical = build_contract_state_projection(
        [
            _event(52, "route_context"),
            _event(53, "route_action_precheck"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert generic["completed_evidence"] == []
    assert generic["missing_evidence"] == [
        "route_context",
        "route_action_precheck",
    ]
    assert [item["id"] for item in canonical["completed_evidence"]] == [
        "route_context",
        "route_action_precheck",
    ]
    assert canonical["missing_evidence"] == []


def test_projection_keeps_no_contract_rows_without_chain_requirement():
    projection = build_contract_state_projection(
        [],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["legacy_no_contract"] is True
    assert projection["contract_chain_id"] == ""
    assert projection["contract_chain"] == []
    assert projection["successor_contract_candidates"] == []
    assert projection["selected_successor_contract"] == {}
    assert projection["next_legal_action"]["id"] == "select_or_enter_contract"
    assert projection["next_legal_action"]["precedence"] == "contract_first_pre_mutation"
    assert "observer_hotfix_direct_mutation.v1" in projection["next_legal_action"][
        "candidate_contract_templates"
    ]
    assert "merge" in projection["next_legal_action"]["supported_contract_roles"]
    assert projection["runtime_contract_hints"]["next_legal_operation"][
        "id"
    ] == "select_or_enter_contract"


def test_no_contract_hotfix_entry_prompts_hotfix_under_action():
    projection = build_contract_state_projection(
        [_event(41, "hotfix_entered")],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    action = projection["next_legal_action"]
    assert projection["legacy_no_contract"] is True
    assert action["id"] == "hotfix_post_action_summary"
    assert action["action"] == "record_hotfix_under_action"
    assert action["timeline_append_hint"]["event_kind"] == "hotfix_under_action"
    assert action["timeline_append_hint"]["payload"]["pre_reason_event_id"] == "41"
    implementation_close_evidence = action["timeline_append_hint"]["payload"][
        "implementation_close_evidence"
    ]
    assert implementation_close_evidence["counts_as_implementation"] is True
    assert implementation_close_evidence["changed_files"] == []
    assert implementation_close_evidence["verification_evidence_refs"] == []
    assert implementation_close_evidence["qa_lineage"]["required"] is True
    assert implementation_close_evidence["qa_lineage"]["required_gate"] == (
        "independent_qa_gate"
    )


def test_root_terminal_contract_with_close_ready_prompts_backlog_close_route_token():
    route_identity = {
        "route_id": "route-root",
        "route_context_hash": "sha256:ctx-root",
        "prompt_contract_id": "rprompt-root",
        "prompt_contract_hash": "sha256:prompt-root",
        "visible_injection_manifest_hash": "sha256:visible-root",
        "route_token_ref": "rtok-parent",
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-root-terminal",
            "contract_execution_id": "cex-root-terminal",
            "contract_revision_id": "rev-root-terminal",
            "state": "selected",
            "route_identity": route_identity,
            "required_evidence": ["route_context"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(21, "route_context", payload={"route_context": route_identity}),
            _event(
                22,
                "close_ready",
                status="accepted",
                payload={"contract_execution_id": "cex-root-terminal"},
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "task-root-terminal",
            "target_files": json.dumps(["agent/governance/contract_state_runtime.py"]),
        },
    )

    action = projection["next_legal_action"]
    assert action["id"] == "issue_backlog_close_route_token_then_backlog_close"
    assert action["action"] == "backlog_close"
    assert action["allowed_action"] == "backlog_close"
    assert action["close_ready_evidence_refs"][0]["ref"] == "timeline:22"
    hint = action["route_token_request_hint"]
    assert hint["allowed_actions"] == ["backlog_close"]
    assert hint["issue_route_token_request"]["allowed_actions"] == ["backlog_close"]
    assert hint["issue_route_token_request"]["evidence_refs"] == ["timeline:22"]
    assert hint["issue_route_token_request"]["parent_route_token_ref"] == "rtok-parent"


def test_projection_exposes_active_contract_execution_handle():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-3",
            "state": "selected",
            "required_evidence": ["route_context"],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    active = projection["active_contract_execution"]
    assert active["schema_version"] == "active_contract_execution.v1"
    assert active["project_id"] == "aming-claw"
    assert active["backlog_id"] == "AC-CONTRACT-RUNTIME"
    assert active["contract_id"] == "onboard_contract.v1"
    assert active["contract_template_id"] == "onboard_contract.v1"
    assert active["contract_revision_id"] == "rev-3"
    assert active["contract_execution_id"].startswith("cex-")
    assert active["contract_chain_id"].startswith("cchain-")
    assert projection["root_contract_execution"]["contract_execution_id"] == active[
        "contract_execution_id"
    ]
    assert projection["contract_chain"][0]["role"] == "root"
    executable = projection["executable_contract"]
    assert executable["active_contract_id"] == "onboard_contract.v1"
    assert executable["contract_execution"]["contract_execution_id"] == active[
        "contract_execution_id"
    ]
    assert executable["contract_execution"]["contract_chain_id"] == active[
        "contract_chain_id"
    ]
    assert executable["next_legal_operation"]["id"] == "route_context"
    assert executable["next_legal_operation"]["close_gate_is_navigation_source"] is False
    hints = projection["runtime_contract_hints"]
    assert hints["active_contract_execution_id"] == active["contract_execution_id"]
    assert hints["active_contract_chain_id"] == active["contract_chain_id"]
    assert hints["close_gate_policy"]["role"] == "final_verifier"
    assert hints["close_gate_policy"]["drives_next_legal_operation"] is False


def test_active_contract_binding_scopes_navigation_to_binding_watermark():
    onboard_template = {
        "template_id": "onboard_contract.v1",
        "required_evidence": [
            {
                "id": "route_context",
                "accepted_event_kinds": ["route_context"],
            },
            {
                "id": "route_action_precheck",
                "accepted_event_kinds": ["route_action_precheck"],
                "prerequisite_ids": ["route_context"],
            },
        ],
    }
    events = [
        _event(
            1,
            "mf_subagent_dispatch",
            payload={
                "task_id": "old-worker-a",
                "route_identity": {
                    "route_context_hash": "sha256:old",
                    "prompt_contract_id": "rprompt-old",
                    "route_token_ref": "rtok-old",
                },
            },
        ),
        _event(2, "implementation", payload={"task_id": "old-worker-b"}),
        _event(3, "verification", payload={"task_id": "old-worker-c"}),
        _event(
            10,
            "contract_binding",
            payload={
                "contract_binding": {
                    "contract_id": "onboard_contract.v1",
                    "contract_template_id": "onboard_contract.v1",
                    "contract_execution_id": "cex-fresh-onboard",
                    "contract_revision_id": "rev-fresh-onboard",
                    "state": "selected",
                },
                "route_identity": {
                    "route_id": "route-fresh",
                    "route_context_hash": "sha256:fresh",
                    "prompt_contract_id": "rprompt-fresh",
                    "prompt_contract_hash": "sha256:prompt-fresh",
                    "visible_injection_manifest_hash": "sha256:manifest-fresh",
                },
            },
        ),
    ]

    projection = build_contract_state_projection(
        events,
        contract={"schema_version": "legacy_bootstrap.v1"},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={"onboard_contract.v1": onboard_template},
    )

    assert projection["legacy_no_contract"] is False
    assert projection["contract_template_id"] == "onboard_contract.v1"
    assert projection["active_contract_execution"]["contract_execution_id"] == (
        "cex-fresh-onboard"
    )
    assert projection["completed_evidence"] == []
    assert projection["next_legal_action"]["id"] == "route_context"
    assert "cross_ref_lineage_bridge" not in {
        step["id"] for step in projection["ordered_next_steps"]
    }
    route_identity = projection["runtime_contract_hints"]["route_identity"]
    assert route_identity["route_context_hash"] == "sha256:fresh"
    assert route_identity["prompt_contract_id"] == "rprompt-fresh"
    assert "route_token_ref" not in route_identity


def test_runtime_hints_carry_role_bound_worker_prefill_boundary():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-prefill",
            "contract_execution_id": "cex-worker-prefill",
            "contract_chain_id": "cchain-worker-prefill",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "implementation",
                    "accepted_event_kinds": ["implementation"],
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "worker-task-1",
        },
    )

    action = projection["next_legal_action"]
    hint = action["timeline_append_hint"]
    prefill = hint["role_bound_prefill_policy"]
    assert action["contract_execution_id"] == "cex-worker-prefill"
    assert hint["actor_role"] == "mf_sub"
    assert prefill["execution_owner_role"] == "mf_sub"
    assert prefill["observer_owned"] is False
    assert prefill["actor_must_supply_evidence"] is True
    assert "route_identity" in prefill["observer_prefill_fields"]
    assert "changed_files" in prefill["actor_owned_execution_fields"]
    assert projection["runtime_contract_hints"]["next_legal_operation"][
        "execution_owner_role"
    ] == "mf_sub"


def test_mf_subagent_startup_next_action_hint_is_worker_owned():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-startup",
            "contract_execution_id": "cex-worker-startup",
            "contract_chain_id": "cchain-worker-startup",
            "state": "selected",
            "required_evidence": ["mf_subagent_startup"],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "worker-task-1",
        },
    )

    action = projection["next_legal_action"]
    hint = action["timeline_append_hint"]
    prefill = hint["role_bound_prefill_policy"]

    assert action["id"] == "mf_subagent_startup"
    assert hint["event_kind"] == "mf_subagent_startup"
    assert hint["satisfies_by"] == "event_kind"
    assert hint["actor_role"] == "mf_sub"
    assert hint["meta_contract_gate"]["allowed"] is True
    assert prefill["execution_owner_role"] == "mf_sub"
    assert prefill["observer_owned"] is False
    assert prefill["observer_prefill_allowed"] is True
    assert prefill["actor_must_supply_evidence"] is True
    assert "route_identity" in prefill["observer_prefill_fields"]
    assert "contract_execution_id" in prefill["observer_prefill_fields"]
    for field in (
        "status",
        "evidence_refs",
        "verification",
        "result",
        "changed_files",
        "tests",
        "graph_trace_ids",
    ):
        assert field not in prefill["observer_prefill_fields"]
        assert field in prefill["actor_owned_execution_fields"]


def test_mf_subagent_startup_missing_runtime_identity_does_not_complete_requirement():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-startup-invalid",
            "contract_execution_id": "cex-worker-startup-invalid",
            "contract_chain_id": "cchain-worker-startup-invalid",
            "state": "selected",
            "required_evidence": ["mf_subagent_startup"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                41,
                "mf_subagent_startup",
                status="ok",
                payload={
                    "contract_execution_id": "cex-worker-startup-invalid",
                    "actual_cwd": "/tmp/worker",
                    "actual_git_root": "/tmp/worker",
                    "known_missing_startup_fields": [
                        "runtime_context_id",
                        "fence_token",
                        "session_token_ref",
                    ],
                    "route_identity": {
                        "route_id": "route-startup",
                        "route_context_hash": "sha256:ctx",
                        "prompt_contract_id": "prompt-startup",
                        "prompt_contract_hash": "sha256:prompt",
                        "visible_injection_manifest_hash": "sha256:visible",
                    },
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["completed_evidence"] == []
    assert projection["missing_evidence"] == ["mf_subagent_startup"]
    assert projection["next_legal_action"]["id"] == "mf_subagent_startup"


def test_mf_subagent_startup_with_actual_identity_completes_requirement():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-startup-valid",
            "contract_execution_id": "cex-worker-startup-valid",
            "contract_chain_id": "cchain-worker-startup-valid",
            "state": "selected",
            "required_evidence": ["mf_subagent_startup"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                42,
                "mf_subagent_startup",
                status="passed",
                payload={
                    "contract_execution_id": "cex-worker-startup-valid",
                    "actual_cwd": "/tmp/worker",
                    "actual_git_root": "/tmp/worker",
                    "branch": "refs/heads/codex/worker",
                    "head_commit": "abc123",
                    "fence_token": "fence-123",
                    "close_satisfying": True,
                    "runtime_context_id": "mfrctx-worker",
                    "task_id": "worker-task",
                    "parent_task_id": "AC-CONTRACT-RUNTIME",
                    "worker_slot_id": "worker-a",
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["missing_evidence"] == []
    assert projection["completed_evidence"][0]["id"] == "mf_subagent_startup"
    assert projection["contract_complete"] is True


def test_projection_falls_back_to_event_project_id_for_active_execution():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-project-fallback",
            "state": "selected",
            "required_evidence": ["route_context"],
        }
    }

    projection = build_contract_state_projection(
        [_event(44, "route_context", project_id="aming-claw")],
        contract=contract,
        backlog_row={"bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["active_contract_execution"]["project_id"] == "aming-claw"


def test_non_onboard_contract_uses_same_projection_path():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-mf",
            "state": "selected",
            "required_evidence": [
                {"id": "implementation", "accepted_event_kinds": ["implementation"]},
                {"id": "verification", "accepted_event_kinds": ["verification"]},
            ],
        }
    }

    projection = build_contract_state_projection(
        [_event(50, "implementation", status="accepted")],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_id"] == "mf_parallel.v1"
    assert [item["id"] for item in projection["completed_evidence"]] == [
        "implementation"
    ]
    assert projection["missing_evidence"] == ["verification"]
    assert projection["next_legal_action"]["id"] == "verification"


def test_default_mf_parallel_requirements_still_drive_next_action_order():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-mf-defaults",
            "state": "bound",
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=[
            "close_ready",
            "implementation",
            "verification",
        ],
    )

    assert projection["requirements_explicit"] is False
    assert projection["missing_evidence"] == [
        "implementation",
        "verification",
        "close_ready",
    ]
    assert projection["next_legal_action"]["id"] == "implementation"
    assert projection["next_legal_action"]["contract_execution_id"] == projection[
        "active_contract_execution"
    ]["contract_execution_id"]
    assert projection["active_lane_contract"]["next_legal_action"]["id"] == (
        "implementation"
    )


def test_bound_root_default_requirements_drive_next_action_without_active_execution():
    projection = build_contract_state_projection(
        [
            _event(
                80,
                "contract_state_changed",
                payload={"contract_binding": {"state": "bound"}},
            )
        ],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=[
            "mf_subagent_startup",
            "implementation",
            "verification",
            "close_ready",
        ],
    )

    assert projection["state"] == "bound"
    assert projection["legacy_no_contract"] is False
    assert projection["active_contract_execution"] == {}
    assert projection["missing_evidence"] == [
        "mf_subagent_startup",
        "implementation",
        "verification",
        "close_ready",
    ]
    action = projection["next_legal_action"]
    assert action["id"] == "mf_subagent_startup"
    assert action["backlog_id"] == "AC-CONTRACT-RUNTIME"
    assert action["contract_execution_id"] == ""
    assert projection["runtime_contract_hints"]["next_legal_operation"]["id"] == (
        "mf_subagent_startup"
    )
    assert projection["executable_contract"]["next_legal_operation"]["id"] == (
        "mf_subagent_startup"
    )


def test_onboard_complete_exposes_successor_candidates_as_next_action():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-successor",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                    {"contract_template_id": "mf_parallel.v1"},
                ]
            },
        }
    }

    projection = build_contract_state_projection(
        [_event(60, "route_context")],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is True
    assert [item["contract_template_id"] for item in projection["successor_contract_candidates"]] == [
        "observer_hotfix_direct_mutation.v1",
        "mf_parallel.v1",
    ]
    assert projection["successor_next_legal_action"]["id"] == "select_successor_contract"
    assert projection["next_legal_action"]["id"] == "select_successor_contract"
    assert projection["next_legal_action"]["contract_chain_id"] == projection[
        "contract_chain_id"
    ]
    assert projection["next_legal_action"]["successor_contract_policy"][
        "selection_action"
    ] == "select_successor_contract"


def test_successor_binding_selects_latest_successor_with_distinct_execution_ids():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-successor",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-successor-binding",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [{"contract_template_id": "review_contract.v1"}]
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(70, "route_context"),
            _event(
                71,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-successor",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "first review",
                    }
                },
            ),
            _event(
                72,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-successor",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "second review",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    successors = [
        item for item in projection["contract_chain"] if item["role"] == "successor"
    ]
    assert len(successors) == 2
    assert successors[0]["successor_contract_execution_id"] != successors[1][
        "successor_contract_execution_id"
    ]
    assert projection["selected_successor_contract"]["handoff_reason"] == "second review"
    assert projection["successor_next_legal_action"]["contract_execution_id"] == (
        projection["selected_successor_contract"]["successor_contract_execution_id"]
    )


def test_successor_binding_requires_chain_and_parent_match():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-successor",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-successor-binding",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [{"contract_template_id": "review_contract.v1"}]
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(75, "route_context"),
            _event(
                76,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "missing parent and chain",
                    }
                },
            ),
            _event(
                77,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-other",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "wrong chain",
                    }
                },
            ),
            _event(
                78,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-successor",
                        "parent_contract_execution_id": "cex-other-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "wrong parent",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["selected_successor_contract"] == {}
    assert projection["successor_next_legal_action"]["id"] == "select_successor_contract"


def test_requirement_evidence_is_scoped_by_contract_execution_id():
    contract = {
        "contract": {
            "contract_id": "review_contract.v1",
            "contract_template_id": "review_contract.v1",
            "contract_execution_id": "cex-review-expected",
            "contract_revision_id": "rev-review",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "review_done",
                    "accepted_event_kinds": ["review_lane"],
                    "contract_execution_id": "cex-review-expected",
                }
            ],
        }
    }

    wrong = build_contract_state_projection(
        [
            _event(
                80,
                "review_lane",
                payload={"contract_execution_id": "cex-review-other"},
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    right = build_contract_state_projection(
        [
            _event(
                81,
                "review_lane",
                payload={"contract_execution_id": "cex-review-expected"},
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert wrong["completed_evidence"] == []
    assert wrong["missing_evidence"] == ["review_done"]
    assert [item["id"] for item in right["completed_evidence"]] == ["review_done"]
    assert right["missing_evidence"] == []


def test_selected_hotfix_successor_uses_template_requirements_before_next_successor():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-onboard-hotfix",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {
                "id": "hotfix_pre_reason",
                "event_kind": "hotfix_entered",
            },
            {
                "id": "hotfix_post_action_summary",
                "event_kind": "hotfix_under_action",
            },
        ],
        "successor_contract_policy": {
            "selection_action": "select_successor_contract",
            "candidates": [
                {"contract_template_id": "mf_parallel.v1"},
                {"contract_template_id": "qa_evidence_gate_review.v1"},
            ],
        },
    }

    projection = build_contract_state_projection(
        [
            _event(90, "route_context"),
            _event(
                91,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                        "handoff_reason": "workflow hotfix",
                    }
                },
            ),
            _event(
                92,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    state = projection["selected_successor_contract_state"]
    assert state["contract_execution_id"] == "cex-hotfix"
    assert state["completed_evidence"][0]["id"] == "hotfix_pre_reason"
    assert state["missing_evidence"] == ["hotfix_post_action_summary"]
    assert projection["next_legal_action"]["id"] == "hotfix_post_action_summary"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-hotfix"

    completed = build_contract_state_projection(
        [
            _event(90, "route_context"),
            _event(
                91,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                        "handoff_reason": "workflow hotfix",
                    }
                },
            ),
            _event(
                92,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                93,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    completed_state = completed["selected_successor_contract_state"]
    assert completed_state["contract_complete"] is True
    assert completed["next_legal_action"]["id"] == "select_successor_contract"
    assert completed["next_legal_action"]["contract_execution_id"] == "cex-hotfix"
    assert [
        item["contract_template_id"]
        for item in completed["next_legal_action"]["successor_contract_candidates"]
    ] == ["mf_parallel.v1", "qa_evidence_gate_review.v1"]


def test_successor_missing_step_does_not_inherit_selection_event_id():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-selection-pollution",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-selection-pollution",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(5920, "route_context"),
            _event(
                5921,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-selection-pollution",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    state = projection["selected_successor_contract_state"]
    assert state["missing_evidence"] == [
        "hotfix_pre_reason",
        "hotfix_post_action_summary",
    ]
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"
    for step in state["ordered_next_steps"]:
        assert "5921" not in step.get("accepted_event_ids", [])
    for step in projection["next_legal_action"]["ordered_missing_steps"]:
        assert "5921" not in step.get("accepted_event_ids", [])


def test_nested_successor_binding_after_hotfix_is_parent_scoped():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-hotfix-nested",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-onboard-hotfix",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }

    projection = build_contract_state_projection(
        [
            _event(100, "route_context"),
            _event(
                101,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-nested",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                102,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                103,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                104,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-nested",
                        "parent_contract_execution_id": "cex-other-parent",
                        "successor_contract_execution_id": "cex-wrong-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                105,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-nested",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    nested = projection["selected_successor_contract_state"][
        "selected_successor_contract_binding"
    ]
    assert nested["contract_execution_id"] == "cex-qa"
    assert projection["next_legal_action"]["id"] == "successor_contract_selected"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa"


def test_successor_requirements_accept_handoff_and_parent_execution_evidence():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-hotfix-lineage",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-onboard-hotfix",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
            {
                "id": "focused_verification",
                "event_kind": "verification",
                "match_policy": "canonical_event_kind",
            },
            {"id": "close_ready", "event_kind": "close_ready"},
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(150, "route_context"),
            _event(151, "hotfix_entered"),
            _event(
                152,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-lineage",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                        "handoff_event_id": "151",
                    }
                },
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "verification",
                payload={
                    "contract_execution_id": "cex-qa",
                    "parent_contract_execution_id": "cex-hotfix",
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    state = projection["selected_successor_contract_state"]
    assert [item["id"] for item in state["completed_evidence"]] == [
        "hotfix_pre_reason",
        "hotfix_post_action_summary",
        "focused_verification",
    ]
    assert state["missing_evidence"] == ["close_ready"]
    assert projection["next_legal_action"]["id"] == "close_ready"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-hotfix"


def test_custom_requirement_next_action_recommends_contract_state_changed_wrapper():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-custom-evidence",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "related_backlog_review",
                    "action": "record_related_backlog_review",
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    next_action = projection["next_legal_action"]
    hint = next_action["timeline_append_hint"]
    assert next_action["id"] == "related_backlog_review"
    assert hint["event_kind"] == "contract_state_changed"
    assert hint["satisfies_by"] == "payload.requirement_id"
    assert hint["payload"]["requirement_id"] == "related_backlog_review"

    completed = build_contract_state_projection(
        [
            _event(
                110,
                "contract_state_changed",
                payload={"requirement_id": "related_backlog_review"},
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert completed["contract_complete"] is True
    assert completed["missing_evidence"] == []


def test_top_level_successor_binding_does_not_rebind_root_contract():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-top-level-successor",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-top-level-successor",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"}
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(120, "route_context"),
            _event(
                121,
                "contract_binding",
                payload={
                    "contract_chain_id": "cchain-top-level-successor",
                    "parent_contract_execution_id": "cex-onboard-root",
                    "successor_contract_execution_id": "cex-hotfix",
                    "contract_id": "observer_hotfix_direct_mutation.v1",
                    "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    "handoff_reason": "workflow hotfix",
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    assert projection["contract_id"] == "onboard_contract.v1"
    assert projection["active_contract_execution"]["contract_execution_id"] == (
        "cex-onboard-root"
    )
    assert projection["selected_successor_contract"]["contract_id"] == (
        "observer_hotfix_direct_mutation.v1"
    )
    assert projection["selected_successor_contract_state"]["contract_execution_id"] == (
        "cex-hotfix"
    )
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"


def test_binding_execution_id_drives_successor_parent_scope_and_lane_runtime():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-bound-root-successor",
            "contract_revision_id": "rev-bound-root-successor",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"}
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(130, "route_context"),
            _event(
                131,
                "contract_binding",
                payload={
                    "contract_binding": {
                        "contract_chain_id": "cchain-bound-root-successor",
                        "contract_execution_id": "cex-onboard-bound",
                        "contract_id": "onboard_contract.v1",
                        "contract_template_id": "onboard_contract.v1",
                        "contract_revision_id": "rev-bound-root-successor",
                        "state": "selected",
                    },
                    "successor_contract": {
                        "contract_chain_id": "cchain-bound-root-successor",
                        "parent_contract_execution_id": "cex-onboard-bound",
                        "successor_contract_execution_id": "cex-hotfix-bound",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    },
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    assert projection["active_contract_execution"]["contract_execution_id"] == (
        "cex-onboard-bound"
    )
    assert projection["selected_successor_contract"]["contract_execution_id"] == (
        "cex-hotfix-bound"
    )
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-hotfix-bound"
    )
    assert projection["active_lane_contract"]["contract_execution_id"] == (
        "cex-hotfix-bound"
    )
    assert projection["contract_execution_index"]["cex-onboard-bound"]["role"] == "root"
    assert (
        projection["contract_execution_index"]["cex-hotfix-bound"][
            "parent_contract_execution_id"
        ]
        == "cex-onboard-bound"
    )


def test_qa_hotfix_qa_successor_path_exposes_nested_lane_next_action():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa",
            "contract_execution_id": "cex-qa-root",
            "contract_revision_id": "rev-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                140,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-root"},
            ),
            _event(
                141,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                142,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                143,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                144,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert [
        item["contract_execution_id"]
        for item in projection["contract_lane_executions"]
    ] == ["cex-qa-root", "cex-hotfix", "cex-qa-followup"]
    assert projection["active_lane_contract"]["role"] == "nested_successor"
    assert projection["active_lane_contract"]["contract_template_id"] == (
        "qa_evidence_gate_review.v1"
    )
    assert projection["next_legal_action"]["id"] == "qa_review"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-qa-followup"
    )
    assert projection["next_legal_action"]["ordered_missing_steps_source"] == (
        "nested_successor_contract_state"
    )
    assert projection["contract_execution_index"]["cex-hotfix"]["missing_evidence"] == []
    assert projection["contract_execution_index"]["cex-qa-followup"][
        "missing_evidence"
    ] == ["qa_review"]


def test_completed_hotfix_qa_successor_prompts_close_ready_not_hotfix_loop():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                {"contract_template_id": "audit_close_with_qa_acceptance.v1"},
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix-qa-complete",
            "contract_execution_id": "cex-onboard",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ],
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                170,
                "contract_state_changed",
                payload={"requirement_id": "graph_query_schema_trace"},
            ),
            _event(
                171,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-complete",
                        "parent_contract_execution_id": "cex-onboard",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                172,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                173,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                174,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-complete",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(175, "qa_review", payload={"contract_execution_id": "cex-qa"}),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert projection["active_lane_contract"]["contract_execution_id"] == "cex-qa"
    assert projection["active_lane_contract"]["missing_evidence"] == []
    assert projection["next_legal_action"]["id"] == "close_ready"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa"
    assert projection["next_legal_action"]["timeline_append_hint"][
        "terminal_semantics"
    ]["audit_close_candidate"] is True
    assert projection["next_legal_action"]["timeline_append_hint"][
        "terminal_semantics"
    ]["hotfix_candidate"] is True


def test_completed_hotfix_qa_successor_with_close_ready_prompts_backlog_close():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                {"contract_template_id": "audit_close_with_qa_acceptance.v1"},
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix-qa-close",
            "contract_execution_id": "cex-onboard",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ],
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                180,
                "contract_state_changed",
                payload={"requirement_id": "graph_query_schema_trace"},
            ),
            _event(
                181,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-close",
                        "parent_contract_execution_id": "cex-onboard",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                182,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                183,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                184,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-close",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(185, "qa_review", payload={"contract_execution_id": "cex-qa"}),
            _event(
                186,
                "close_ready",
                status="accepted",
                payload={"contract_execution_id": "cex-qa"},
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "task-hotfix-qa-close",
            "target_files": json.dumps(["agent/governance/contract_state_runtime.py"]),
        },
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    action = projection["next_legal_action"]
    assert projection["active_lane_contract"]["contract_execution_id"] == "cex-qa"
    assert action["id"] == "issue_backlog_close_route_token_then_backlog_close"
    assert action["action"] == "backlog_close"
    assert action["contract_execution_id"] == "cex-qa"
    assert action["route_token_request_hint"]["allowed_actions"] == ["backlog_close"]
    assert action["route_token_request_hint"]["evidence_refs"] == ["timeline:186"]
    lane_action = projection["active_lane_contract"]["next_legal_action"]
    assert lane_action["protected_action"] is True
    assert lane_action["allowed_action"] == "backlog_close"
    assert lane_action["route_token_request_hint"]["allowed_actions"] == [
        "backlog_close"
    ]
    assert lane_action["route_token_request_hint"]["evidence_refs"] == ["timeline:186"]
    assert lane_action["close_ready_evidence_refs"][0]["ref"] == "timeline:186"


def test_completed_hotfix_qa_followup_prompts_successor_selection():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                {"contract_template_id": "audit_close_with_qa_acceptance.v1"},
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix-qa-followup",
            "contract_execution_id": "cex-onboard",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ],
            },
        }
    }

    for event_id, gate_decision in ((175, "pass_with_followups"), (176, "block")):
        projection = build_contract_state_projection(
            [
                _event(
                    170,
                    "contract_state_changed",
                    payload={"requirement_id": "graph_query_schema_trace"},
                ),
                _event(
                    171,
                    "contract_binding",
                    payload={
                        "successor_contract": {
                            "contract_chain_id": (
                                "cchain-onboard-hotfix-qa-followup"
                            ),
                            "parent_contract_execution_id": "cex-onboard",
                            "successor_contract_execution_id": "cex-hotfix",
                            "contract_template_id": (
                                "observer_hotfix_direct_mutation.v1"
                            ),
                        }
                    },
                ),
                _event(
                    172,
                    "hotfix_entered",
                    payload={"successor_contract_execution_id": "cex-hotfix"},
                ),
                _event(
                    173,
                    "hotfix_under_action",
                    payload={"successor_contract_execution_id": "cex-hotfix"},
                ),
                _event(
                    174,
                    "contract_binding",
                    payload={
                        "successor_contract": {
                            "contract_chain_id": (
                                "cchain-onboard-hotfix-qa-followup"
                            ),
                            "parent_contract_execution_id": "cex-hotfix",
                            "successor_contract_execution_id": "cex-qa",
                            "contract_template_id": "qa_evidence_gate_review.v1",
                        }
                    },
                ),
                _event(
                    event_id,
                    "qa_review",
                    payload={
                        "contract_execution_id": "cex-qa",
                        "gate_decision": gate_decision,
                    },
                ),
            ],
            contract=contract,
            backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
            contract_templates={
                "observer_hotfix_direct_mutation.v1": hotfix_template,
                "qa_evidence_gate_review.v1": qa_template,
            },
        )

        assert projection["active_lane_contract"]["contract_execution_id"] == "cex-qa"
        assert projection["active_lane_contract"]["missing_evidence"] == []
        assert projection["next_legal_action"]["id"] == "select_successor_contract"
        assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa"
        assert "observer_hotfix_direct_mutation.v1" in [
            item["contract_template_id"]
            for item in projection["next_legal_action"][
                "successor_contract_candidates"
            ]
        ]


def test_qa_followup_hotfix_successor_exposes_deep_lane_next_action():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert [
        item["contract_execution_id"]
        for item in projection["contract_lane_executions"]
    ] == ["cex-qa-root", "cex-hotfix", "cex-qa-followup", "cex-hotfix-r2"]
    assert projection["active_lane_contract"]["role"] == "deep_successor"
    assert projection["active_lane_contract"]["contract_template_id"] == (
        "observer_hotfix_direct_mutation.v1"
    )
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-hotfix-r2"
    )
    assert projection["next_legal_action"]["ordered_missing_steps_source"] == (
        "deep_successor_contract_state"
    )


def test_completed_deep_successor_prompts_successor_contract_selection():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                157,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                158,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert projection["active_lane_contract"]["role"] == "deep_successor"
    assert projection["active_lane_contract"]["missing_evidence"] == []
    assert projection["next_legal_action"]["id"] == "select_successor_contract"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-hotfix-r2"
    )
    assert [
        item["contract_template_id"]
        for item in projection["next_legal_action"]["successor_contract_candidates"]
    ] == ["qa_evidence_gate_review.v1"]


def test_deep_successor_followup_qa_binding_exposes_next_lane():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                157,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                158,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                159,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-hotfix-r2",
                        "successor_contract_execution_id": "cex-qa-r2",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert [
        item["contract_execution_id"]
        for item in projection["contract_lane_executions"]
    ] == [
        "cex-qa-root",
        "cex-hotfix",
        "cex-qa-followup",
        "cex-hotfix-r2",
        "cex-qa-r2",
    ]
    assert projection["active_lane_contract"]["role"] == "successor_depth_4"
    assert projection["active_lane_contract"]["contract_template_id"] == (
        "qa_evidence_gate_review.v1"
    )
    assert projection["next_legal_action"]["id"] == "qa_review"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa-r2"
    assert projection["next_legal_action"]["ordered_missing_steps_source"] == (
        "successor_depth_4_contract_state.v1"
    )


def test_completed_deep_successor_followup_qa_prompts_close_ready():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa-complete",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                157,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                158,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                159,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-hotfix-r2",
                        "successor_contract_execution_id": "cex-qa-r2",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(160, "qa_review", payload={"contract_execution_id": "cex-qa-r2"}),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert projection["active_lane_contract"]["role"] == "successor_depth_4"
    assert projection["active_lane_contract"]["missing_evidence"] == []
    assert projection["active_lane_contract"]["next_legal_action"]["id"] == (
        "close_ready"
    )
    assert projection["next_legal_action"]["id"] == "close_ready"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa-r2"


def _strict_worker_identity_contract():
    return {
        "contract": {
            "contract_id": "strict_worker_contract.v1",
            "contract_template_id": "strict_worker_contract.v1",
            "contract_revision_id": "rev-strict-worker",
            "state": "selected",
            "evidence_requirements": [
                {
                    "id": "worker_implementation",
                    "event_kind": "implementation",
                    "expected_writer_role": "mf_sub",
                    "required_route_token_ref": "rtok-expected",
                    "required_runtime_context_id": "mfrctx-expected",
                    "required_task_id": "task-expected",
                    "required_parent_task_id": "parent-expected",
                    "required_worker_slot_id": "worker-slot-expected",
                    "required_fence_token_hash": "sha256:fence-expected",
                }
            ],
        }
    }


def _strict_worker_identity_payload(**overrides):
    payload = {
        "writer_role": "mf_sub",
        "meta_contract_gate": {"role": "mf_sub"},
        "route_token_ref": "rtok-expected",
        "runtime_context_id": "mfrctx-expected",
        "task_id": "task-expected",
        "parent_task_id": "parent-expected",
        "worker_slot_id": "worker-slot-expected",
        "fence_token_hash": "sha256:fence-expected",
    }
    payload.update(overrides)
    return payload


def test_strict_worker_identity_requirement_rejects_payload_self_declared_writer_role():
    payload = _strict_worker_identity_payload(worker_role="mf_sub")
    payload.pop("meta_contract_gate")

    projection = build_contract_state_projection(
        [_event(899, "implementation", payload=payload)],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is False
    assert projection["missing_evidence"] == ["worker_implementation"]
    assert projection["completed_evidence"] == []


def test_strict_worker_identity_requirement_rejects_conflicting_trusted_writer_role():
    projection = build_contract_state_projection(
        [
            _event(
                899,
                "implementation",
                payload=_strict_worker_identity_payload(
                    writer_role="mf_sub",
                    meta_contract_gate={"role": "observer"},
                ),
            )
        ],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is False
    assert projection["missing_evidence"] == ["worker_implementation"]
    assert projection["completed_evidence"] == []


def test_strict_worker_identity_requirement_accepts_matching_event():
    projection = build_contract_state_projection(
        [
            _event(
                900,
                "implementation",
                payload=_strict_worker_identity_payload(),
            )
        ],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is True
    assert projection["missing_evidence"] == []
    completed = projection["completed_evidence"][0]
    assert completed["id"] == "worker_implementation"
    assert completed["strict_identity"]["writer_role"] == "mf_sub"
    assert completed["strict_identity"]["route_token_ref"] == "rtok-expected"


def test_strict_worker_identity_requirement_rejects_wrong_identity_fields():
    wrong_overrides = {
        "writer_role": {"meta_contract_gate": {"role": "observer"}},
        "route_token_ref": {"route_token_ref": "rtok-wrong"},
        "runtime_context_id": {"runtime_context_id": "mfrctx-wrong"},
        "task_id": {"task_id": "task-wrong"},
        "parent_task_id": {"parent_task_id": "parent-wrong"},
        "worker_slot_id": {"worker_slot_id": "worker-slot-wrong"},
        "fence_token_hash": {"fence_token_hash": "sha256:fence-wrong"},
    }

    for field, overrides in wrong_overrides.items():
        projection = build_contract_state_projection(
            [
                _event(
                    901,
                    "implementation",
                    payload=_strict_worker_identity_payload(**overrides),
                )
            ],
            contract=_strict_worker_identity_contract(),
            backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        )

        assert projection["contract_complete"] is False, field
        assert projection["missing_evidence"] == ["worker_implementation"], field
        assert projection["completed_evidence"] == [], field


def test_strict_worker_identity_next_action_hint_exposes_expected_fields():
    projection = build_contract_state_projection(
        [],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert hint["event_kind"] == "implementation"
    assert hint["actor_role"] == "mf_sub"
    assert hint["strict_identity"]["match_policy"] == "all_declared_identity_fields"
    assert "meta_contract_gate.role" in hint["strict_identity"][
        "trusted_writer_role_sources"
    ]
    assert "payload.writer_role" not in hint["strict_identity"][
        "trusted_writer_role_sources"
    ]
    assert hint["expected_identity"] == {
        "writer_role": "mf_sub",
        "route_token_ref": "rtok-expected",
        "runtime_context_id": "mfrctx-expected",
        "task_id": "task-expected",
        "parent_task_id": "parent-expected",
        "worker_slot_id": "worker-slot-expected",
        "fence_token_hash": "sha256:fence-expected",
    }
    assert set(hint["required_payload_fields"]) == {
        "writer_role",
        "route_token_ref",
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "worker_slot_id",
        "fence_token_hash",
    }
    for field in hint["required_payload_fields"]:
        assert hint["payload"][field] == hint["expected_identity"][field]
