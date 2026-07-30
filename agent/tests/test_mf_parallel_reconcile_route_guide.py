import json
from pathlib import Path

from agent.governance.contract_state_runtime import (
    integration_epoch_resume_projection,
)
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


def test_registry_preserves_active_rev3_pin_when_later_revision_is_latest():
    registry = ContractDefinitionRegistry()
    pinned = registry.get("mf_parallel.v2", version="v2", revision="rev3")
    latest = registry.get("mf_parallel.v2", version="v2")

    assert pinned["definition_hash"] == (
        "sha256:31a20dd7897da4a76b482a6ba29d0341163416df84187c2b9b44fc5f35bb4a4e"
    )
    assert pinned["source_sha256"] == (
        "sha256:f0d9e997649e9ccd1bc46aaced8191b3506a9cd9b0d2f6588c0d52211b9dde22"
    )
    assert latest["revision"] == "rev7"
    assert latest["metadata"]["previous_revision"] == "mf_parallel.v2.rev6"
    assert latest["definition_hash"] != pinned["definition_hash"]


def test_reconcile_pending_epoch_projects_copy_safe_batch_task_input():
    projection = integration_epoch_resume_projection(
        {
            "project_id": "aming-claw",
            "status": "reconcile_pending",
            "batch_id": "batch-happy-path",
            "epoch_id": "epoch-happy-path",
            "coordination_backlog_id": "AC-BATCH-PARENT",
            "target_ref": "refs/heads/main",
            "current_head": "a" * 40,
            "merge_queue_id": "mq-happy-path",
            "active_task_id": "",
            "active_backlog_id": "",
        }
    )

    action = projection["next_legal_action"]
    assert action["id"] == "final_batch_reconcile"
    assert action["required_tool"] == "graph_current_full_reconcile"
    assert action["task_id"] == "batch-happy-path"
    assert action["backlog_id"] == "AC-BATCH-PARENT"
    assert action["action_input_copy_safe"] is True
    assert action["action_input"] == {
        "project_id": "aming-claw",
        "backlog_id": "AC-BATCH-PARENT",
        "task_id": "batch-happy-path",
        "target_commit_sha": "a" * 40,
        "activate": True,
        "require_clean": True,
        "semantic_use_ai": False,
        "semantic_enrich": False,
        "enqueue_stale": False,
        "notes_extra": {
            "integration_epoch_authority": {
                "batch_id": "batch-happy-path",
                "epoch_id": "epoch-happy-path",
                "merge_queue_id": "mq-happy-path",
            },
        },
    }


def test_non_reconcile_epoch_does_not_project_reconcile_action_input():
    projection = integration_epoch_resume_projection(
        {
            "status": "merge_in_doubt",
            "batch_id": "batch-happy-path",
            "epoch_id": "epoch-happy-path",
            "merge_queue_id": "mq-happy-path",
            "active_task_id": "task-row-2",
            "active_backlog_id": "AC-BATCH-ROW-2",
        }
    )

    action = projection["next_legal_action"]
    assert action["id"] == "resume_batch_merge"
    assert action["action_input"] == {}
    assert action["action_input_copy_safe"] is False
