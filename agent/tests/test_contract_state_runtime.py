from __future__ import annotations

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
    assert projection["next_legal_action"] is None


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
