from __future__ import annotations

from agent.governance.contract_state_runtime import build_contract_state_projection


def _event(event_id: int, kind: str, *, status: str = "passed", payload=None):
    return {
        "id": event_id,
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
