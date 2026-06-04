"""Tests for observer-owned agent task contract handoff records."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance.mf_subagent_contract import (
    AGENT_TASK_CONTRACT_SCHEMA_VERSION,
    canonical_contract_hash,
    build_observer_owned_agent_task_contract,
    verification_route_policy_from_contract,
)
from agent.governance.parallel_branch_runtime import (
    BranchTaskRuntimeContext,
    append_branch_contract_revision,
    get_latest_branch_contract_revision,
)


def _context(**overrides: object) -> BranchTaskRuntimeContext:
    fields = {
        "project_id": "aming-claw",
        "task_id": "task-contract-worker",
        "root_task_id": "task-contract-root",
        "backlog_id": "AC-OBSERVER-CONTRACT",
        "branch_ref": "refs/heads/codex/task-contract-worker",
        "status": "running",
        "worker_id": "worker-contract",
        "lease_id": "lease-contract",
        "lease_expires_at": "2026-06-04T05:30:00Z",
        "fence_token": "fence-contract",
        "worktree_path": "/repo/.worktrees/task-contract-worker",
        "base_commit": "base123",
        "head_commit": "head123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-contract",
    }
    fields.update(overrides)
    return BranchTaskRuntimeContext(**fields)


def test_observer_owned_contract_records_handoff_identity_and_canonical_hash() -> None:
    contract = build_observer_owned_agent_task_contract(
        _context(),
        requester="dashboard",
        observer_owner="observer-session-1",
        executor_lane="mf_sub",
        verifier_lane="qa",
        route_identity={
            "route_id": "route-contract",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-contract",
            "visible_injection_manifest_hash": "sha256:visible",
        },
        contract_revision_id="crev-1",
        previous_revision_hash="sha256:previous",
        actor_role="observer",
        actor_session_id="observer-session-1",
        timestamp="2026-06-04T04:00:00Z",
        read_receipt_hash="sha256:read",
        gate_receipt_hash="sha256:gate",
        required_evidence=["implementation", "verification"],
        target_files=["agent/governance/mf_subagent_contract.py"],
        target_fences=["fence-contract"],
        visible_injection_manifest={"manifest_hash": "sha256:visible"},
    )

    assert contract["schema_version"] == AGENT_TASK_CONTRACT_SCHEMA_VERSION
    assert contract["source_of_truth"] == "Contract/Revision/Event"
    assert contract["requester"] == "dashboard"
    assert contract["observer_owner"] == "observer-session-1"
    assert contract["executor_lane"] == "mf_sub"
    assert contract["verifier_lane"] == "qa"
    assert contract["route_identity"]["route_context_hash"] == "sha256:route"
    assert contract["visible_injection_manifest"]["manifest_hash"] == "sha256:visible"
    assert contract["required_evidence"] == ["implementation", "verification"]
    assert contract["target_files"] == ["agent/governance/mf_subagent_contract.py"]
    assert contract["target_fences"] == ["fence-contract"]
    assert contract["lease"]["deadline"] == "2026-06-04T05:30:00Z"
    receipt = contract["step_receipt_contract"]
    assert receipt["previous_revision_hash"] == "sha256:previous"
    assert receipt["actor_role"] == "observer"
    assert receipt["actor_session_id"] == "observer-session-1"
    assert receipt["read_receipt_hash"] == "sha256:read"
    assert receipt["gate_receipt_hash"] == "sha256:gate"
    assert set(receipt["required_fields"]).issuperset(
        {
            "canonical_visible_contract_text_hash",
            "previous_revision_hash",
            "actor_role",
            "actor_session_id",
            "timestamp",
            "read_receipt_hash",
            "gate_receipt_hash",
        }
    )
    material = dict(contract)
    material.pop("canonical_visible_contract_text_hash")
    assert contract["canonical_visible_contract_text_hash"] == canonical_contract_hash(
        material
    )


def test_verification_route_policy_blocks_real_ai_without_explicit_precheck() -> None:
    blocked = verification_route_policy_from_contract(
        {
            "verification_route_policy": {
                "deterministic_gates": ["contract_hash"],
                "local_tests": ["pytest -q"],
                "docker": {"allowed": False},
                "real_ai_provider_calls": {"providers": ["provider-a"]},
            }
        }
    )

    assert blocked["deterministic_gates"] == ["contract_hash"]
    assert blocked["local_tests"]["commands"] == ["pytest -q"]
    assert blocked["docker"]["requires_explicit_route"] is True
    assert blocked["real_ai_provider_calls"]["allowed"] is False
    assert blocked["real_ai_provider_calls"]["blocked_by_default"] is True
    assert blocked["post_verification_impact_actions"]["requires_observer"] is True
    assert blocked["policy_separation"]["local_tests_are_not_provider_calls"] is True

    authorized = verification_route_policy_from_contract(
        {
            "verification_route_policy": {
                "real_ai_provider_calls": {"authorized": True, "providers": ["provider-a"]}
            },
            "precheck": {"authorization_refs": ["precheck-1"]},
        }
    )

    assert authorized["real_ai_provider_calls"]["allowed"] is True
    assert authorized["real_ai_provider_calls"]["blocked_by_default"] is False
    assert authorized["real_ai_provider_calls"]["authorization_refs"] == ["precheck-1"]


def test_branch_contract_revision_receipt_chains_previous_revision_hash() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    context = _context()

    first = append_branch_contract_revision(
        conn,
        context,
        revision_id="crev-1",
        payload={
            "summary": "initial",
            "read_receipt_hash": "sha256:read-1",
            "actor_session_id": "observer-session-1",
        },
        route_gate={
            "caller_role": "observer",
            "caller_session_id": "observer-session-1",
            "route_token_hash": "sha256:gate-1",
        },
        now_iso="2026-06-04T04:00:00Z",
    )

    first_receipt = first.payload["revision_receipt"]
    assert first.payload["source_of_truth"] == "Contract/Revision/Event"
    assert first_receipt["previous_revision_hash"] == ""
    assert first_receipt["read_receipt_hash"] == "sha256:read-1"
    assert first_receipt["gate_receipt_hash"] == "sha256:gate-1"
    assert first_receipt["actor_role"] == "observer"
    assert first_receipt["actor_session_id"] == "observer-session-1"

    second = append_branch_contract_revision(
        conn,
        context,
        revision_id="crev-2",
        payload={"summary": "updated", "read_receipt_hash": "sha256:read-2"},
        route_gate={"caller_role": "observer", "route_token_hash": "sha256:gate-2"},
        now_iso="2026-06-04T04:05:00Z",
    )

    second_receipt = second.payload["revision_receipt"]
    assert second_receipt["previous_revision_hash"] == first_receipt[
        "canonical_visible_contract_text_hash"
    ]
    assert second_receipt["read_receipt_hash"] == "sha256:read-2"
    assert second_receipt["gate_receipt_hash"] == "sha256:gate-2"
    assert get_latest_branch_contract_revision(
        conn,
        context.project_id,
        second.runtime_context_id,
    ).revision_id == "crev-2"
