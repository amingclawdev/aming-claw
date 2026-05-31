"""Tests for backlog-backed parallel agent contract gating."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE backlog_bugs (
            bug_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'OPEN',
            chain_trigger_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    return conn


def _accepted_contract() -> dict:
    return {
        "row_type": "contract",
        "contract_type": "parallel_agent_contract",
        "contract_status": "ACCEPTED",
        "parent_bug_id": "FEATURE-AI-SUMMARY-20260523",
        "parallel_contract": {
            "schema_version": "parallel_agent_contract.v1",
            "participants": [
                {
                    "agent_id": "backend",
                    "role": "backend",
                    "write_scope": [
                        "agent/governance/server.py",
                        "agent/governance/reconcile_semantic_summary.py",
                        "agent/tests/test_parallel_agent_contract_gate.py",
                    ],
                },
                {
                    "agent_id": "frontend",
                    "role": "frontend",
                    "write_scope": [
                        "frontend/dashboard/src/lib/api.ts",
                        "frontend/dashboard/src/views/**",
                    ],
                },
            ],
            "shared_interfaces": [
                {
                    "name": "semantic_summary_job",
                    "owner": "backend",
                    "consumers": ["frontend"],
                    "schema": {"job_type": "semantic_summary"},
                }
            ],
            "forbidden_cross_writes": True,
            "integration_gate": {
                "owner": "observer",
                "required_checks": [
                    "pytest -q agent/tests/test_parallel_agent_contract_gate.py"
                ],
            },
        },
    }


def _observer_test_scenario_policy() -> dict:
    return {
        "mode": "observer_configured",
        "decision": "new_scenario_required",
        "allowed_decisions": [
            "none",
            "reuse_existing",
            "new_scenario_required",
        ],
        "reason": "contract validation policy needs focused coverage",
        "required_evidence_ids": [
            "observer_test_strategy",
            "focused_tests",
            "contract_gate_tests",
            "docs_policy_update",
            "e2e_deferred_followup",
        ],
        "e2e_decision": "e2e_deferred",
        "followup_backlog_id": "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524",
    }


def _insert_contract(
    conn: sqlite3.Connection,
    *,
    bug_id: str = "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
    status: str = "OPEN",
    chain_trigger_json: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, status, chain_trigger_json) VALUES (?, ?, ?)",
        (bug_id, status, json.dumps(chain_trigger_json or _accepted_contract())),
    )
    conn.commit()


def test_parallel_contract_accepts_backend_task_within_owned_scope() -> None:
    from governance.parallel_agent_contract import validate_parallel_agent_task_gate

    conn = _conn()
    _insert_contract(conn)

    evidence = validate_parallel_agent_task_gate(
        conn,
        "aming-claw",
        "dev",
        {
            "bug_id": "FEATURE-AI-SUMMARY-20260523",
            "parallel_contract_id": "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
            "parallel_agent_id": "backend",
            "target_files": ["agent/governance/server.py"],
        },
    )

    assert evidence["contract_id"] == "CONTRACT-PARALLEL-AI-SUMMARY-20260523"
    assert evidence["agent_id"] == "backend"
    assert evidence["contract_status"] == "ACCEPTED"


def test_parallel_contract_validates_observer_configured_test_scenario_policy() -> None:
    from governance.parallel_agent_contract import validate_parallel_agent_task_gate

    conn = _conn()
    contract = _accepted_contract()
    contract["parallel_contract"]["test_scenario_policy"] = _observer_test_scenario_policy()
    _insert_contract(conn, chain_trigger_json=contract)

    evidence = validate_parallel_agent_task_gate(
        conn,
        "aming-claw",
        "dev",
        {
            "bug_id": "FEATURE-AI-SUMMARY-20260523",
            "parallel_contract_id": "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
            "parallel_agent_id": "backend",
            "target_files": ["agent/governance/server.py"],
        },
    )

    policy = evidence["test_scenario_policy"]
    assert policy["mode"] == "observer_configured"
    assert policy["decision"] == "new_scenario_required"
    assert policy["required_evidence_ids"] == [
        "observer_test_strategy",
        "focused_tests",
        "contract_gate_tests",
        "docs_policy_update",
        "e2e_deferred_followup",
    ]
    assert policy["e2e_decision"] == "e2e_deferred"
    assert policy["followup_backlog_id"] == (
        "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524"
    )


def test_parallel_contract_rejects_deferred_e2e_without_followup_backlog_id() -> None:
    from governance.parallel_agent_contract import (
        ParallelAgentContractError,
        validate_parallel_agent_task_gate,
    )

    conn = _conn()
    contract = _accepted_contract()
    policy = _observer_test_scenario_policy()
    policy["followup_backlog_id"] = ""
    contract["parallel_contract"]["test_scenario_policy"] = policy
    _insert_contract(conn, chain_trigger_json=contract)

    with pytest.raises(ParallelAgentContractError, match="followup_backlog_id"):
        validate_parallel_agent_task_gate(
            conn,
            "aming-claw",
            "dev",
            {
                "bug_id": "FEATURE-AI-SUMMARY-20260523",
                "parallel_contract_id": "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
                "parallel_agent_id": "backend",
                "target_files": ["agent/governance/server.py"],
            },
        )


def test_parallel_contract_rejects_missing_contract_id_when_parallel_agent_is_declared() -> None:
    from governance.parallel_agent_contract import (
        ParallelAgentContractError,
        validate_parallel_agent_task_gate,
    )

    conn = _conn()

    with pytest.raises(ParallelAgentContractError, match="parallel_contract_id is required"):
        validate_parallel_agent_task_gate(
            conn,
            "aming-claw",
            "dev",
            {
                "parallel_agent_id": "backend",
                "target_files": ["agent/governance/server.py"],
            },
        )


def test_parallel_contract_rejects_pending_contract_status() -> None:
    from governance.parallel_agent_contract import (
        ParallelAgentContractError,
        validate_parallel_agent_task_gate,
    )

    conn = _conn()
    pending = _accepted_contract()
    pending["contract_status"] = "PENDING_REVIEW"
    _insert_contract(conn, chain_trigger_json=pending)

    with pytest.raises(ParallelAgentContractError, match="must be ACCEPTED"):
        validate_parallel_agent_task_gate(
            conn,
            "aming-claw",
            "dev",
            {
                "parallel_contract_id": "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
                "parallel_agent_id": "backend",
                "target_files": ["agent/governance/server.py"],
            },
        )


def test_parallel_contract_rejects_overlapping_write_scopes() -> None:
    from governance.parallel_agent_contract import (
        ParallelAgentContractError,
        validate_parallel_agent_task_gate,
    )

    conn = _conn()
    contract = _accepted_contract()
    contract["parallel_contract"]["participants"][1]["write_scope"].append(
        "agent/governance/**"
    )
    _insert_contract(conn, chain_trigger_json=contract)

    with pytest.raises(ParallelAgentContractError, match="overlapping write scopes"):
        validate_parallel_agent_task_gate(
            conn,
            "aming-claw",
            "dev",
            {
                "parallel_contract_id": "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
                "parallel_agent_id": "backend",
                "target_files": ["agent/governance/server.py"],
            },
        )


def test_parallel_contract_rejects_target_file_outside_agent_scope() -> None:
    from governance.parallel_agent_contract import (
        ParallelAgentContractError,
        validate_parallel_agent_task_gate,
    )

    conn = _conn()
    _insert_contract(conn)

    with pytest.raises(ParallelAgentContractError, match="outside participant write_scope"):
        validate_parallel_agent_task_gate(
            conn,
            "aming-claw",
            "dev",
            {
                "parallel_contract_id": "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
                "parallel_agent_id": "backend",
                "target_files": ["frontend/dashboard/src/lib/api.ts"],
            },
        )


def test_parallel_contract_rejects_missing_shared_interface_or_integration_gate() -> None:
    from governance.parallel_agent_contract import (
        ParallelAgentContractError,
        validate_parallel_agent_task_gate,
    )

    conn = _conn()
    contract = _accepted_contract()
    contract["parallel_contract"]["shared_interfaces"] = []
    contract["parallel_contract"]["integration_gate"]["required_checks"] = []
    _insert_contract(conn, chain_trigger_json=contract)

    with pytest.raises(ParallelAgentContractError, match="shared_interfaces"):
        validate_parallel_agent_task_gate(
            conn,
            "aming-claw",
            "dev",
            {
                "parallel_contract_id": "CONTRACT-PARALLEL-AI-SUMMARY-20260523",
                "parallel_agent_id": "backend",
                "target_files": ["agent/governance/server.py"],
            },
        )


def test_task_create_persists_parallel_contract_gate_evidence(tmp_path, monkeypatch) -> None:
    from governance import task_registry
    from governance.db import get_connection
    from governance.server import handle_task_create

    project_id = f"parallel-contract-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setenv("OPT_BACKLOG_ENFORCE", "strict")

    conn = get_connection(project_id)
    now = "2026-05-23T00:00:00Z"
    feature_bug_id = "FEATURE-AI-SUMMARY-20260523"
    contract_bug_id = "CONTRACT-PARALLEL-AI-SUMMARY-20260523"
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, status, chain_trigger_json, created_at, updated_at) "
        "VALUES (?, 'OPEN', '{}', ?, ?)",
        (feature_bug_id, now, now),
    )
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, status, chain_trigger_json, created_at, updated_at) "
        "VALUES (?, 'OPEN', ?, ?, ?)",
        (contract_bug_id, json.dumps(_accepted_contract()), now, now),
    )
    parent = task_registry.create_task(
        conn,
        project_id,
        prompt="parent task",
        task_type="task",
        created_by="pytest",
        metadata={"bug_id": feature_bug_id},
    )
    conn.close()

    class _Ctx:
        token = None
        body = {
            "type": "dev",
            "prompt": "Implement backend side of AI Summary",
            "metadata": {
                "bug_id": feature_bug_id,
                "parent_task_id": parent["task_id"],
                "parallel_contract_id": contract_bug_id,
                "parallel_agent_id": "backend",
                "target_files": ["agent/governance/server.py"],
            },
            "route_token": {
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "caller_role": "observer",
                "allowed_action": "task_create",
                "project_id": project_id,
                "backlog_id": feature_bug_id,
                "expires_at": "2999-01-01T00:00:00Z",
                "evidence_refs": ["timeline:route-context"],
            },
        }

        def get_project_id(self) -> str:
            return project_id

    result = handle_task_create(_Ctx())
    assert result["type"] == "dev"

    conn = get_connection(project_id)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id = ?",
            (result["task_id"],),
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
    finally:
        conn.close()

    evidence = metadata["parallel_contract_evidence"]
    assert evidence["contract_id"] == contract_bug_id
    assert evidence["agent_id"] == "backend"
    assert evidence["target_files"] == ["agent/governance/server.py"]


def test_server_source_contains_parallel_contract_gate_hook() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "validate_parallel_agent_task_gate" in content
    assert "parallel_contract_gate" in content
