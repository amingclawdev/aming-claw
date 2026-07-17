import json
from pathlib import Path

from agent.governance.contracts import ContractDefinitionRegistry


DEFINITION_PATH = (
    Path(__file__).resolve().parents[1]
    / "governance"
    / "contract_definitions"
    / "mf_parallel.v2.rev4.json"
)


def _definition() -> dict:
    return json.loads(DEFINITION_PATH.read_text())


def _observer_reconcile_line(definition: dict) -> dict:
    return next(
        line
        for stage in definition["rule_layer"]["stages"]
        for line in stage["lines"]
        if line["line_id"] == "observer_reconcile"
    )


def _reconcile_policy(definition: dict) -> dict:
    return definition["system_layer"]["graph_binding_policy"][
        "current_full_reconcile_evidence_policy"
    ]


def test_observer_reconcile_guide_uses_dispatched_runtime_task_before_submit():
    definition = _definition()
    policy = _reconcile_policy(definition)
    next_action = policy["observer_reconcile_next_action"]

    assert next_action["source_backed"] is True
    assert next_action["copy_safe"] is True
    assert next_action["route_scopes_interchangeable"] is False
    assert next_action["canonical_runtime_task"] == {
        "task_id_source": "dispatched_runtime_context.task_id",
        "runtime_context_id_source": (
            "dispatched_runtime_context.runtime_context_id"
        ),
        "target_project_root_source": (
            "dispatched_runtime_context.target_project_root"
        ),
        "explicit_task_id_mismatch_policy": "fail_closed",
    }

    reconcile, submit = next_action["sequence"]
    assert reconcile["action"] == "graph_current_full_reconcile"
    assert reconcile["route_kind"] == "reconcile_only"
    assert reconcile["route_task_id_source"] == (
        "dispatched_runtime_context.task_id"
    )
    assert submit == {
        "order": 2,
        "action": "contract_runtime_submit_line",
        "route_kind": "contract_runtime_submit",
        "route_task_id_source": "contract_execution_id",
        "line_id": "observer_reconcile",
        "requires": [
            "task_scoped_current_full_reconcile",
            "authoritative_snapshot_verified",
        ],
        "raw_route_token_required": False,
    }


def test_observer_reconcile_guide_is_copy_safe_and_snapshot_authoritative():
    definition = _definition()
    next_action = _reconcile_policy(definition)[
        "observer_reconcile_next_action"
    ]
    reconcile = next_action["sequence"][0]

    assert reconcile["required_copy_safe_fields"] == [
        "observer_session_id",
        "route_token_ref",
        "backlog_id",
        "contract_execution_id",
        "runtime_context_id",
        "task_id",
        "target_project_root",
    ]
    assert "raw_route_token" not in reconcile["required_copy_safe_fields"]
    assert reconcile["raw_route_token_required"] is False
    assert next_action["authoritative_snapshot"]["source"] == (
        "graph_snapshot_store.current_full_reconcile_state"
    )
    assert next_action["authoritative_snapshot"]["caller_claims_trusted"] is False
    assert next_action["authoritative_snapshot"]["required_checks"] == [
        "db_verified",
        "live_verified",
        "canonical_head_verified",
        "active_snapshot_verified",
        "provenance_scope_verified",
        "durable_order_verified",
    ]


def test_observer_reconcile_route_guidance_does_not_lower_existing_gate():
    definition = _definition()
    policy = _reconcile_policy(definition)
    line = _observer_reconcile_line(definition)
    inline = "\n".join(definition["instruction_layer"]["inline"])

    assert policy["current_full_reconcile_required"] is True
    assert policy["caller_authority_fields_trusted"] is False
    assert policy["authority_derivation"] == (
        "server_live_graph_snapshot_verification"
    )
    assert policy["explicit_task_id_mismatch_policy"] == "fail_closed"
    assert line["owner_role"] == "observer"
    assert line["allowed_writer_roles"] == ["observer"]
    assert line["requires"] == ["observer_merge"]
    assert "dispatched runtime_context.task_id" in line["description"]
    assert "route scopes are not interchangeable" in inline
    assert "Never copy a raw route token" in inline


def test_registry_materializes_reconcile_route_guidance_into_read_model():
    registered = ContractDefinitionRegistry().get(
        "mf_parallel.v2",
        version="v2",
        revision="rev4",
    )
    next_action = registered["system_layer"]["graph_binding_policy"][
        "current_full_reconcile_evidence_policy"
    ]["observer_reconcile_next_action"]
    reconcile_line = next(
        line
        for line in registered["read_model"]["rule_lines"]
        if line["line_id"] == "observer_reconcile"
    )

    assert next_action["sequence"][0]["route_task_id_source"] == (
        "dispatched_runtime_context.task_id"
    )
    assert next_action["sequence"][1]["route_task_id_source"] == (
        "contract_execution_id"
    )
    assert "task-scoped graph_current_full_reconcile" in reconcile_line[
        "description"
    ]


def test_registry_preserves_active_rev3_pin_when_rev4_becomes_latest():
    registry = ContractDefinitionRegistry()
    pinned = registry.get("mf_parallel.v2", version="v2", revision="rev3")
    latest = registry.get("mf_parallel.v2", version="v2")

    assert pinned["definition_hash"] == (
        "sha256:31a20dd7897da4a76b482a6ba29d0341163416df84187c2b9b44fc5f35bb4a4e"
    )
    assert pinned["source_sha256"] == (
        "sha256:f0d9e997649e9ccd1bc46aaced8191b3506a9cd9b0d2f6588c0d52211b9dde22"
    )
    assert latest["revision"] == "rev4"
    assert latest["metadata"]["previous_revision"] == "mf_parallel.v2.rev3"
    assert latest["definition_hash"] != pinned["definition_hash"]
