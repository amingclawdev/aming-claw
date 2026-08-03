from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import contract_state_runtime
from agent.governance.contracts import ContractDefinitionRegistry, ContractRuntime
from agent.governance.contracts.hash import file_sha256, stable_sha256
from agent.governance.contracts.runtime import (
    _audited_bypass_recovery_fallback,
    _audited_bypass_terminal_disposition,
    _contract_completion_satisfying_lines,
    _line_status_allows_contract_completion,
    _project_current_contract_state,
    _project_record_state,
    _worker_implementation_lineage,
    ContractRuntimeError,
    read_backlog_contract_chain_current,
    rebuild_backlog_contract_chain_projection,
    SQLiteContractExecutionStore,
    StalePinnedContractExecutionError,
    upsert_contract_chain_successor_binding,
)
from agent.governance.contracts.write_gate import validate_contract_write


def test_acceptance_file_fence_closure_requires_stable_structured_authority():
    gate = contract_state_runtime.acceptance_file_fence_closure_gate(
        [
            "free text is descriptive, not authority",
            {
                "id": "AC-VALID",
                "required_scope": {"kind": "unresolved"},
            },
            {
                "id": "AC-VALID",
                "required_scope": {
                    "kind": "files",
                    "files": ["agent/governance/server.py"],
                },
            },
        ],
        ["agent/governance/server.py"],
    )

    assert gate["accepted"] is False
    assert gate["errors"] == [
        "acceptance_criteria_require_stable_ids",
        "acceptance_criterion_ids_must_be_unique",
        "acceptance_criteria_require_structured_required_scope",
        "acceptance_required_scope_unresolved",
    ]
    assert gate["duplicate_criterion_ids"] == ["AC-VALID"]
    assert gate["unresolved_criterion_ids"] == ["AC-VALID"]
    assert gate["copy_safe_observer_remediation"]["owner_role"] == "observer"
    assert gate["free_text_authority_allowed"] is False
    assert stable_sha256(gate) == stable_sha256(
        contract_state_runtime.acceptance_file_fence_closure_gate(
            [
                "free text is descriptive, not authority",
                {
                    "id": "AC-VALID",
                    "required_scope": {"kind": "unresolved"},
                },
                {
                    "id": "AC-VALID",
                    "required_scope": {
                        "kind": "files",
                        "files": ["agent/governance/server.py"],
                    },
                },
            ],
            ["agent/governance/server.py"],
        )
    )


def test_acceptance_file_fence_closure_reports_exact_missing_files():
    gate = contract_state_runtime.acceptance_file_fence_closure_gate(
        [
            {
                "id": "AC-SERVER",
                "required_scope": {
                    "kind": "files",
                    "files": [
                        "agent/governance/server.py",
                        "agent/tests/test_graph_governance_api.py",
                    ],
                },
            },
            {
                "id": "AC-NODE",
                "required_scope": {
                    "kind": "nodes",
                    "node_ids": ["governance.contract_mint"],
                },
            },
            {
                "id": "AC-E2E",
                "required_scope": {
                    "kind": "verification_only_external_dependency",
                    "dependency_id": "browser:e2e",
                },
            },
        ],
        ["agent/governance/server.py"],
    )

    assert gate["accepted"] is False
    assert gate["criterion_ids"] == ["AC-SERVER", "AC-NODE", "AC-E2E"]
    assert gate["missing_required_files"] == [
        "agent/tests/test_graph_governance_api.py"
    ]
    assert gate["required_node_union"] == ["governance.contract_mint"]
    assert gate["verification_only_external_dependencies"] == ["browser:e2e"]


def test_acceptance_file_fence_closure_accepts_closed_scope_and_empty_set():
    criteria = [
        {
            "id": "AC-FILES",
            "required_scope": {
                "kind": "files_and_nodes",
                "files": ["agent/governance/server.py"],
                "node_ids": ["governance.server"],
            },
        }
    ]

    closed = contract_state_runtime.acceptance_file_fence_closure_gate(
        criteria,
        ["agent/governance/server.py"],
    )
    empty = contract_state_runtime.acceptance_file_fence_closure_gate([], [])

    assert closed["accepted"] is True
    assert closed["required_file_union"] == ["agent/governance/server.py"]
    assert empty["accepted"] is True
    assert empty["criterion_count"] == 0


def test_acceptance_file_fence_closure_forbids_worker_or_qa_widening():
    authority = [
        {
            "id": "AC-ONE",
            "required_scope": {
                "kind": "files",
                "files": ["agent/governance/server.py"],
            },
        }
    ]
    widened = [
        *authority,
        {
            "id": "AC-TWO",
            "required_scope": {
                "kind": "files",
                "files": ["agent/governance/auto_chain.py"],
            },
        },
    ]

    gate = contract_state_runtime.acceptance_file_fence_closure_gate(
        authority,
        ["agent/governance/server.py", "agent/governance/auto_chain.py"],
        actor_role="qa",
        reported_acceptance_criteria=widened,
        implementation_started=True,
    )

    assert gate["accepted"] is False
    assert gate["errors"] == [
        "worker_or_qa_acceptance_scope_widening_forbidden"
    ]
    assert gate["copy_safe_observer_remediation"]["action"] == (
        "create_fresh_or_rework_contract_with_revised_file_fence"
    )
    assert gate["copy_safe_observer_remediation"][
        "worker_or_qa_scope_widening_allowed"
    ] is False


def test_meta_and_mf_parallel_templates_require_acceptance_file_fence_closure():
    template_root = Path(__file__).resolve().parents[1] / "governance" / "contract_templates"
    meta = json.loads((template_root / "meta_contract.v1.json").read_text())
    mf_parallel = json.loads((template_root / "mf_parallel.v2.json").read_text())

    for policy in (
        meta["acceptance_file_fence_closure_policy"],
        mf_parallel["acceptance_file_fence_closure_policy"],
    ):
        assert policy["criterion_authority_required_fields"] == [
            "id",
            "required_scope",
        ]
        assert policy["required_scope_kinds"] == [
            "files",
            "nodes",
            "files_and_nodes",
            "verification_only_external_dependency",
            "unresolved",
        ]
        assert policy["required_file_union_must_be_contained_by"] == [
            "target_files",
            "owned_files",
        ]
        assert policy["unresolved_scope_policy"] == "fail_closed"
        assert policy["worker_or_qa_scope_widening_allowed"] is False
        assert policy["post_implementation_revision_policy"] == (
            "fresh_or_rework_contract"
        )
        assert policy["bypass_allowed"] is False


def test_failed_qa_rejoin_marker_resets_only_prior_revision_proof_lines():
    runtime_context_id = "mfrctx-failed-qa-route-rebind"
    task_id = "worker-failed-qa-route-rebind"
    context_keys = {
        ("runtime_context_id", runtime_context_id),
        ("task_id", task_id),
        ("line_instance_id", f"runtime_context:{runtime_context_id}"),
        ("line_instance_id", f"task:{task_id}"),
    }
    common = {
        "actor_role": "mf_sub",
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "line_instance_id": f"runtime_context:{runtime_context_id}",
        "status": "passed",
    }
    old_implementation = {
        **common,
        "line_id": "worker_implementation",
        "payload": {"revision": "old", "runtime_context_id": runtime_context_id},
    }
    fresh_implementation = {
        **common,
        "line_id": "worker_implementation",
        "payload": {
            "revision": "fresh",
            "runtime_context_id": runtime_context_id,
            "failed_qa_revision_rejoin_marker": {
                "revision_event_ref": "timeline:200",
            },
        },
    }
    old_qa = {
        **common,
        "line_id": "qa_independent_verification",
        "payload": {"revision": "old", "runtime_context_id": runtime_context_id},
    }
    fresh_qa = {
        **common,
        "line_id": "qa_independent_verification",
        "payload": {"revision": "fresh", "runtime_context_id": runtime_context_id},
    }
    lines = [
        {
            **common,
            "line_id": "worker_read_runtime_guide",
            "payload": {"runtime_context_id": runtime_context_id},
        },
        old_implementation,
        {
            **common,
            "line_id": "worker_commit",
            "payload": {"revision": "old", "runtime_context_id": runtime_context_id},
        },
        old_qa,
        fresh_implementation,
        fresh_qa,
    ]

    satisfying = _contract_completion_satisfying_lines(
        lines,
        failed_qa_rejoin_contexts=context_keys,
        failed_qa_rejoin_markers=[
            {"source_ref": "timeline:200", "context_keys": context_keys}
        ],
    )

    assert lines[0] in satisfying
    assert old_implementation not in satisfying
    assert lines[2] not in satisfying
    assert old_qa not in satisfying
    assert fresh_implementation in satisfying
    assert fresh_qa in satisfying


@pytest.mark.parametrize(
    ("line_order", "qa_shape", "expected_next"),
    [
        ("semantic", "authenticated_observations", "observer_merge"),
        ("qa_before_worker", "authenticated_observations", "observer_merge"),
        ("semantic", "authenticated_failure", "worker_implementation"),
        ("semantic", "unauthenticated_observations", "worker_implementation"),
        ("semantic", "authenticated_sibling_failure", "worker_implementation"),
    ],
)
def test_projected_authenticated_qa_pass_keeps_baseline_observations_audit_only(
    tmp_path,
    line_order,
    qa_shape,
    expected_next,
):
    line_contracts = [
        (
            "orchestration",
            "observer_prefill_child_contracts",
            "observer",
            "contract_binding",
        ),
        (
            "dispatch",
            "observer_dispatch_bounded_workers",
            "observer",
            "dispatch_bounded_worker",
        ),
        ("worker_read", "worker_read_runtime_guide", "mf_sub", "read_receipt"),
        ("worker_startup", "worker_startup", "mf_sub", "mf_subagent_startup"),
        ("worker_context", "worker_graph_context", "mf_sub", "graph_trace"),
        (
            "worker_implementation",
            "worker_implementation",
            "mf_sub",
            "implementation",
        ),
        ("worker_commit", "worker_commit", "mf_sub", "worker_commit"),
        (
            "worker_attestation",
            "worker_finish_time_attestation",
            "mf_sub",
            "record_finish_time_worker_attestation",
        ),
        (
            "worker_finish",
            "worker_finish_gate",
            "mf_sub",
            "mf_subagent_finish_gate",
        ),
        ("qa_graph_context", "qa_graph_context", "qa", "graph_trace"),
        (
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
        ),
        ("observer_integration", "observer_merge", "observer", "merge"),
    ]
    stages = []
    prior_line_id = ""
    for stage_id, line_id, owner_role, evidence_kind in line_contracts:
        line = {
            "line_id": line_id,
            "owner_role": owner_role,
            "allowed_writer_roles": [owner_role],
            "evidence_kind": evidence_kind,
        }
        if prior_line_id:
            line["requires"] = [prior_line_id]
        stages.append({"stage_id": stage_id, "lines": [line]})
        prior_line_id = line_id
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v2",
        stages=stages,
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    execution_id = f"cex-projected-qa-observation-{line_order}-{qa_shape}"
    record = runtime.start_execution(
        "mf_parallel.v2",
        project_id="aming-claw",
        backlog_id="AC-PROJECTED-QA-PASS-BASELINE-OBSERVATIONS",
        contract_execution_id=execution_id,
        actor_role="observer",
        version="v1",
        revision="rev1",
    )
    runtime_context_id = "mfrctx-projected-qa-observation"
    task_id = "worker-projected-qa-observation"
    parent_task_id = execution_id
    common = {
        "line_instance_id": f"runtime_context:{runtime_context_id}",
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
    }
    lines = [
        {
            "stage_id": "orchestration",
            "line_id": "observer_prefill_child_contracts",
            "actor_role": "observer",
            "evidence_kind": "contract_binding",
            "status": "accepted",
        },
        {
            **common,
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "actor_role": "observer",
            "evidence_kind": "dispatch_bounded_worker",
        },
        {
            **common,
            "stage_id": "worker_read",
            "line_id": "worker_read_runtime_guide",
            "actor_role": "mf_sub",
            "evidence_kind": "read_receipt",
        },
        {
            **common,
            "stage_id": "worker_startup",
            "line_id": "worker_startup",
            "actor_role": "mf_sub",
            "evidence_kind": "mf_subagent_startup",
            "status": "passed",
        },
        {
            **common,
            "stage_id": "worker_context",
            "line_id": "worker_graph_context",
            "actor_role": "mf_sub",
            "evidence_kind": "graph_trace",
            "payload": {"graph_trace_evidence": {"db_verified": True}},
        },
        {
            **common,
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "actor_role": "mf_sub",
            "evidence_kind": "implementation",
            "status": "accepted",
        },
        {
            **common,
            "stage_id": "worker_commit",
            "line_id": "worker_commit",
            "actor_role": "mf_sub",
            "evidence_kind": "worker_commit",
        },
        {
            **common,
            "stage_id": "worker_attestation",
            "line_id": "worker_finish_time_attestation",
            "actor_role": "mf_sub",
            "evidence_kind": "record_finish_time_worker_attestation",
        },
        {
            **common,
            "stage_id": "worker_finish",
            "line_id": "worker_finish_gate",
            "actor_role": "mf_sub",
            "evidence_kind": "mf_subagent_finish_gate",
        },
        {
            **common,
            "stage_id": "qa_graph_context",
            "line_id": "qa_graph_context",
            "actor_role": "qa",
            "evidence_kind": "graph_trace",
            "payload": {"graph_trace_evidence": {"db_verified": True}},
        },
    ]
    qa_line = {
        **common,
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "passed",
        "verdict": "pass",
        "authorization_source": "qa_session_token_ref",
        "observer_impersonation": False,
        "parent_materialization_authorized": False,
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_session_token_ref",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "parent_materialization_authorized": False,
            "authenticated_qa_binding": {
                "schema_version": (
                    "contract_runtime.authenticated_qa_binding.v1"
                ),
                "server_derived": True,
                "qa_principal": "qa:projected-observation",
                "qa_session_id": "ses-projected-observation",
                "independent_verification_session_matched": True,
            },
            "completion_status_gate": {
                "schema_version": (
                    "contract_runtime.qa_completion_status_gate.v1"
                ),
                "server_derived": True,
                "top_level_status_present": True,
                "top_level_status_passing": True,
                "normalized_status": "passed",
                "nested_payload_decision_satisfies": False,
            },
        },
        "payload": {
            "status": "passed",
            "verdict": "pass",
            "focused_tests": [
                {"status": "passed", "failed": 0},
                {"status": "baseline_observation", "failed": 42},
            ],
        },
        "tests": [
            {"status": "passed", "failed": 0},
            {"status": "baseline_observation", "failed": 1},
        ],
        "test_results": {
            "status": "passed",
            "tests": [
                {"status": "passed", "failed": 0},
                {"status": "baseline_observation", "failed": 1},
            ],
        },
        "verification": {"status": "passed", "verdict": "pass"},
    }
    if qa_shape == "authenticated_failure":
        qa_line["status"] = "failed"
        qa_line["verdict"] = "fail"
        qa_line["qa_evidence_provenance"]["completion_status_gate"].update(
            {
                "top_level_status_passing": False,
                "normalized_status": "failed",
            }
        )
    elif qa_shape == "unauthenticated_observations":
        qa_line["qa_evidence_provenance"].pop("authenticated_qa_binding")
    elif qa_shape == "authenticated_sibling_failure":
        qa_line["test_results"]["failed"] = 1
    lines.append(qa_line)
    if line_order == "qa_before_worker":
        lines = [*lines[:2], *lines[-2:], *lines[2:-2]]

    persisted = dict(record)
    persisted["completed_lines"] = lines
    persisted["execution_state_revision"] = 12
    runtime.store.update(
        execution_id,
        persisted,
        expected_revision=int(record["execution_state_revision"]),
    )
    projection = {
        "schema_version": (
            "contract_runtime.mf_parallel_runtime_context_projection.v1"
        ),
        "source": "runtime_context_worker_evidence",
        "projected_completed_lines": lines,
        "failed_qa_revision_rejoin_contexts": [
            {
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": parent_task_id,
            }
        ],
    }

    views = [
        runtime.projected_record(
            execution_id,
            actor_role="observer",
            completed_lines=lines,
            projection=projection,
        )
        for _ in range(10)
    ]
    assert {
        view["runtime_guide"]["next_legal_action"]["line_id"]
        for view in views
    } == {expected_next}
    assert len(
        {
            view["runtime_guide"]["runtime_guide_hash"]
            for view in views
        }
    ) == 1

    guide = views[0]["runtime_guide"]
    assert guide["writer_role_safe_copy_payload"]["copy_payload"][
        "line_id"
    ] == expected_next
    submit_guidance = guide["post_projection_submit_line_guidance"]
    assert submit_guidance["projected_line_ids"] == [
        line["line_id"] for line in lines
    ]
    assert submit_guidance["duplicate_submit_line_required"] is False
    if expected_next == "observer_merge":
        assert _line_status_allows_contract_completion(qa_line) is True
        assert guide.get("failed_qa_rework") is None
        merge_precheck = runtime.precheck_line_write(
            execution_id,
            dict(
                guide["writer_role_safe_copy_payload"]["copy_payload"]
            ),
            actor_role="observer",
            projected_completed_lines=lines,
            projection=projection,
        )
        assert merge_precheck["ok"] is True
        assert merge_precheck["completed_lines_count"] == len(lines)
    else:
        assert _line_status_allows_contract_completion(qa_line) is False
        assert guide["failed_qa_rework"]["status"] == (
            "blocked_by_failed_independent_qa"
        )


def test_builtin_contract_templates_bind_bounded_qa_base_diff_context():
    template_root = Path(__file__).resolve().parents[1] / "governance" / "contract_templates"
    mf_parallel = json.loads((template_root / "mf_parallel.v2.json").read_text())
    direct_fix = json.loads((template_root / "direct_fix.v1.json").read_text())

    policy = mf_parallel["qa_graph_context_policy"]
    assert policy["accepted_graph_basis"] == [
        "exact_candidate_snapshot",
        "canonical_base_plus_candidate_diff",
    ]
    assert policy["candidate_diff_derivation"] == "server_only"
    assert policy["default_graph_basis"] == "canonical_base_plus_candidate_diff"
    assert policy["graph_basis_decision_required"] is True
    assert policy["graph_basis_decision_source"] == (
        "server_bounded_qa_graph_basis"
    )
    assert policy["canonical_head_policy"] == "base_or_candidate"
    assert policy["overlay_failure_policy"] == "fail_closed"
    assert policy["one_hop_dependency_failure_policy"] == "fail_closed"
    assert policy["exact_candidate_escalation_stage"] == (
        "server_persisted_overlay_failure_classification"
    )
    assert policy["exact_candidate_acceptance_stage"] == (
        "graph_query_trace_persisted_basis_decision"
    )
    assert policy["full_candidate_snapshot_required"] is False
    assert policy["exact_candidate_query_root_clean_required"] is True
    assert policy["assigned_target_project_root_required"] is True
    assert policy["post_merge_graph_policy"] == {
        "authority": (
            "contract_definition.system_layer.graph_binding_policy."
            "current_full_reconcile_evidence_policy"
        ),
        "reconcile_mode": "current_full",
        "required_after": "observer_merge",
        "required_before": "observer_close_ready",
        "required_proof": [
            "canonical_head_verified",
            "active_snapshot_verified",
            "merged_head_commit_equals_reconciled_commit",
            "qa_id_and_timestamp_before_merge_id_and_timestamp",
            "merge_id_and_timestamp_before_reconcile_id_and_timestamp",
            "reconcile_task_scope_matches_or_is_explicitly_shared_taskless",
        ],
    }

    qa_checkpoint = next(
        checkpoint
        for checkpoint in direct_fix["runtime_contract_hints"][
            "graph_query_checkpoints"
        ]
        if checkpoint["id"] == "qa_graph_context"
    )
    required_fields = qa_checkpoint["packet"]["required_fields"]
    for field in (
        "graph_trace_evidence.graph_basis",
        "graph_trace_evidence.graph_basis_decision",
        "graph_trace_evidence.graph_basis_decision_hash",
        "graph_trace_evidence.canonical_base_snapshot_id",
        "graph_trace_evidence.base_commit_sha",
        "graph_trace_evidence.candidate_commit_sha",
        "graph_trace_evidence.changed_files",
        "graph_trace_evidence.candidate_diff_hash",
        "graph_trace_evidence.changed_files_source",
        "graph_trace_evidence.candidate_overlay_hash",
        "graph_trace_evidence.root_identity_hash",
        "graph_trace_evidence.query_root_identity_hash",
        "graph_trace_evidence.canonical_project_identity_hash",
        "graph_trace_evidence.repository_identity_hash",
    ):
        assert field in required_fields
    assert qa_checkpoint["packet"]["graph_basis_policy"][
        "candidate_diff_derivation"
    ] == "server_only"
    assert qa_checkpoint["packet"]["graph_basis_policy"][
        "default_graph_basis"
    ] == "canonical_base_plus_candidate_diff"
    assert qa_checkpoint["packet"]["graph_basis_policy"][
        "graph_basis_decision_required"
    ] is True
    assert qa_checkpoint["packet"]["graph_basis_policy"][
        "exact_candidate_query_root_clean_required"
    ] is True


def test_mf_parallel_rev7_adds_scope_sideband_without_linear_line():
    governance_root = Path(__file__).resolve().parents[1] / "governance"
    template = json.loads(
        (governance_root / "contract_templates" / "mf_parallel.v2.json").read_text()
    )
    definition = ContractDefinitionRegistry(
        governance_root / "contract_definitions"
    ).get("mf_parallel.v2", version="v2", revision="rev7")

    assert template["runtime_contract_hints"][
        "contract_definition_revision"
    ] == "rev7"
    policy = definition["system_layer"]["scope_insufficiency_sideband_policy"]
    assert policy["linear_contract_line"] is False
    assert policy["worker_facade"] == (
        "runtime_context_scope_insufficiency_request"
    )
    assert policy["request_grants_authority"] is False
    assert policy["request_mutates_owned_files"] is False
    assert policy["qa_verdict_policy"]["sole_author_role"] == "qa"
    line_ids = {
        line["line_id"]
        for stage in definition["rule_layer"]["stages"]
        for line in stage["lines"]
    }
    assert "scope_insufficiency_request" not in line_ids

    machine_contract = (
        contract_state_runtime.cli_agent_qa_onboard_guidance_contract()
    )
    boundary = machine_contract["line_contracts"][
        "qa_independent_verification"
    ]["scope_insufficiency_boundary"]
    assert boundary["linear_contract_line"] is False
    assert boundary["qa_is_sole_verdict_author"] is True
    assert boundary["observer_may_author_qa_verdict"] is False


def test_projected_record_cannot_override_canonical_worker_commit_lineage(
    tmp_path,
):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v2",
        stages=[
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                    }
                ],
            },
            {
                "stage_id": "worker_commit",
                "lines": [
                    {
                        "line_id": "worker_commit",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "worker_commit",
                        "requires": ["worker_implementation"],
                    }
                ],
            },
        ],
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    record = runtime.start_execution(
        "mf_parallel.v2",
        project_id="aming-claw",
        backlog_id="AC-PROJECTED-WORKER-COMMIT-LINEAGE",
        contract_execution_id="cex-projected-worker-commit-lineage",
        actor_role="observer",
    )
    identity = {
        "runtime_context_id": "mfrctx-projected-worker-commit",
        "task_id": "task-projected-worker-commit",
        "parent_task_id": "cex-projected-worker-commit-lineage",
        "worker_id": "worker-projected-worker-commit",
        "worker_slot_id": "worker-projected-worker-commit",
        "worker_session_id": "session-projected-worker-commit",
        "actor_session_principal": "session-projected-worker-commit",
        "filer_principal": "session-projected-worker-commit",
        "implementation_event_ref": "timeline:implementation",
        "target_project_root": str(tmp_path),
        "session_token_ref": "wstok-projected-worker-commit",
        "fence_token_hash": "sha256:fence",
        "worker_role": "mf_sub",
        "evidence_owner_role": "mf_sub",
        "owned_files": ["agent/governance/contracts/runtime.py"],
        "changed_files": ["agent/governance/contracts/runtime.py"],
        "commit_diff_files": ["agent/governance/contracts/runtime.py"],
        "graph_trace_ids": ["gqt-projected-worker-commit"],
        "db_verified": True,
        "clean_worktree": True,
        "dirty_files": [],
        "commit_sha": "a" * 40,
        "head_commit": "a" * 40,
        "immutable_head_commit": "a" * 40,
        "validated_head_commit": "a" * 40,
        "observer_impersonation": False,
    }
    projected_line = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "runtime_context_id": identity["runtime_context_id"],
        "task_id": identity["task_id"],
        "payload": {
            "runtime_context_id": identity["runtime_context_id"],
            "task_id": identity["task_id"],
            "worker_id": identity["worker_id"],
            "worker_slot_id": identity["worker_slot_id"],
            "changed_files": identity["changed_files"],
            "graph_trace_ids": identity["graph_trace_ids"],
        },
    }

    projected = runtime.projected_record(
        record["contract_execution_id"],
        actor_role="mf_sub",
        completed_lines=[projected_line],
    )
    write = {
        "line_id": "worker_commit",
        "actor_role": "mf_sub",
        "commit_sha": identity["commit_sha"],
        "payload": identity,
    }

    projected_precheck = runtime.precheck_line_write(
        record["contract_execution_id"],
        write,
        actor_role="mf_sub",
        projected_completed_lines=[projected_line],
    )
    assert projected["completed_lines"] == [projected_line]
    assert runtime.store.get(record["contract_execution_id"])["completed_lines"] == []
    assert projected_precheck["ok"] is False
    assert any(
        "requires matching worker_implementation lineage" in error
        for error in projected_precheck["decision"]["errors"]
    )

    canonical_write = {
        **_write_from(
            runtime.store.get(record["contract_execution_id"]),
            actor_role="mf_sub",
            stage_id="worker_implementation",
            line_id="worker_implementation",
            evidence_kind="implementation",
        ),
        "runtime_context_id": identity["runtime_context_id"],
        "task_id": identity["task_id"],
        "payload": projected_line["payload"],
    }
    implementation_result = runtime.submit_line_write(
        record["contract_execution_id"],
        canonical_write,
        actor_role="mf_sub",
    )
    assert implementation_result["ok"] is True
    canonical_record = runtime.store.get(record["contract_execution_id"])
    canonical_implementation = canonical_record["completed_lines"][-1]
    identity["implementation_lineage_ref"] = _worker_implementation_lineage(
        canonical_record,
        canonical_implementation,
    )["implementation_lineage_ref"]
    canonical_commit_write = {
        **_write_from(
            canonical_record,
            actor_role="mf_sub",
            stage_id="worker_commit",
            line_id="worker_commit",
            evidence_kind="worker_commit",
        ),
        "commit_sha": identity["commit_sha"],
        "payload": identity,
    }

    canonical_precheck = runtime.precheck_line_write(
        record["contract_execution_id"],
        canonical_commit_write,
        actor_role="mf_sub",
    )
    assert canonical_precheck["ok"] is True


def test_precommit_worker_implementation_correction_is_append_only_and_bounded(
    tmp_path,
):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v2",
        stages=[
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                    }
                ],
            },
            {
                "stage_id": "worker_commit",
                "lines": [
                    {
                        "line_id": "worker_commit",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "worker_commit",
                        "requires": ["worker_implementation"],
                    }
                ],
            },
        ],
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    execution = runtime.start_execution(
        "mf_parallel.v2",
        project_id="aming-claw",
        backlog_id="AC-PRECOMMIT-CORRECTION",
        contract_execution_id="cex-precommit-correction",
        actor_role="observer",
    )
    runtime.current_guide(
        execution["contract_execution_id"],
        actor_role="mf_sub",
    )
    changed_files = [
        "agent/governance/dashboard_dist/assets/index-BGxFKhRa.css",
        "agent/governance/dashboard_dist/assets/index-BttN1OBl.js",
        "agent/governance/dashboard_dist/assets/index-CxdqAXM1.js",
        "agent/governance/dashboard_dist/assets/index-DTkNP_Kn.css",
        "agent/governance/dashboard_dist/index.html",
        "frontend/dashboard/package.json",
    ]
    owned_files = [
        "agent/governance/dashboard_dist/",
        "frontend/dashboard/package.json",
    ]
    identity = {
        "runtime_context_id": "mfrctx-precommit-correction",
        "task_id": "task-precommit-correction",
        "parent_task_id": "AC-PRECOMMIT-CORRECTION",
        "worker_id": "worker-precommit-correction",
        "worker_slot_id": "worker-precommit-correction",
        "target_project_root": str(tmp_path),
        "session_token_ref": "wstok-precommit-correction",
        "fence_token_hash": "sha256:precommit-fence",
    }
    initial_record = runtime.store.get(execution["contract_execution_id"])
    initial_write = {
        **_write_from(
            initial_record,
            actor_role="mf_sub",
            stage_id="worker_implementation",
            line_id="worker_implementation",
            evidence_kind="implementation",
        ),
        **identity,
        "commit_sha": "a" * 40,
        "changed_files": [changed_files[0]],
        "graph_trace_ids": ["gqt-precommit-initial"],
        "payload": {
            **identity,
            "changed_files": [changed_files[0]],
            "graph_trace_ids": ["gqt-precommit-initial"],
        },
    }
    initial_result = runtime.submit_line_write(
        execution["contract_execution_id"],
        initial_write,
        actor_role="mf_sub",
    )
    assert initial_result["ok"], json.dumps(
        initial_result["decision"],
        indent=2,
    )
    before_correction = runtime.store.get(execution["contract_execution_id"])
    immutable_initial = json.loads(
        json.dumps(before_correction["completed_lines"][0])
    )
    initial_lineage = _worker_implementation_lineage(
        before_correction,
        before_correction["completed_lines"][0],
    )

    corrected_head = "b" * 40
    correction_payload = {
        **identity,
        "changed_files": changed_files,
        "graph_trace_ids": ["gqt-precommit-corrected"],
        "precommit_implementation_correction_intent": {
            "schema_version": (
                "runtime_context.precommit_implementation_correction_intent.v1"
            ),
            "action": "revise_precommit_worker_implementation",
            "contract_execution_id": execution["contract_execution_id"],
            "runtime_context_id": identity["runtime_context_id"],
            "task_id": identity["task_id"],
            "prior_implementation_lineage_ref": initial_lineage[
                "implementation_lineage_ref"
            ],
            "verified_by_server": True,
            "caller_authority_fields_trusted": False,
        },
        "graph_trace_db_evidence": {
            "db_verified": True,
            "verified_trace_ids": ["gqt-precommit-corrected"],
        },
        "canonical_precommit_lineage_revision_authority": {
            "schema_version": (
                "runtime_context.clean_cumulative_git_precommit_correction_authority.v1"
            ),
            "source": (
                "runtime_context_clean_cumulative_git_precommit_correction"
            ),
            "server_derived": True,
            "actual_head_commit": corrected_head,
            "diff_base_commit": "0" * 40,
            "clean_worktree": True,
            "cumulative_changed_files": changed_files,
            "owned_files": owned_files,
            "graph_trace_ids": ["gqt-precommit-corrected"],
            "correction_intent_verified": True,
            "correction_intent_schema_version": (
                "runtime_context.precommit_implementation_correction_intent.v1"
            ),
            "correction_intent_action": (
                "revise_precommit_worker_implementation"
            ),
            "prior_implementation_lineage_ref": initial_lineage[
                "implementation_lineage_ref"
            ],
            "caller_authority_fields_trusted": False,
        },
    }
    correction_write = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "commit_sha": corrected_head,
        "changed_files": changed_files,
        "graph_trace_ids": ["gqt-precommit-corrected"],
        "payload": correction_payload,
    }
    corrected = runtime.revise_precommit_worker_implementation(
        execution["contract_execution_id"],
        correction_write,
        actor_role="mf_sub",
    )
    assert corrected["ok"] is True
    assert corrected["status"] == "revised"
    assert corrected["supersedes_implementation_lineage_ref"] == (
        initial_lineage["implementation_lineage_ref"]
    )

    corrected_record = runtime.store.get(execution["contract_execution_id"])
    implementations = [
        line
        for line in corrected_record["completed_lines"]
        if line.get("line_id") == "worker_implementation"
    ]
    assert len(implementations) == 2
    assert implementations[0] == immutable_initial
    assert implementations[-1]["commit_sha"] == corrected_head
    assert implementations[-1]["changed_files"] == changed_files
    correction_audit = implementations[-1]["payload"][
        "canonical_precommit_lineage_revision"
    ]
    assert correction_audit["source"] == (
        "server_verified_precommit_correction"
    )
    assert correction_audit["append_only_history_preserved"] is True
    assert correction_audit["single_correction_boundary"] is True
    assert correction_audit["supersedes_implementation_lineage_ref"] == (
        initial_lineage["implementation_lineage_ref"]
    )
    latest_lineage = _worker_implementation_lineage(
        corrected_record,
        implementations[-1],
    )
    assert latest_lineage["changed_files"] == changed_files
    assert latest_lineage["graph_trace_ids"] == [
        "gqt-precommit-corrected"
    ]

    corrected_revision = corrected_record["execution_state_revision"]
    idempotent = runtime.revise_precommit_worker_implementation(
        execution["contract_execution_id"],
        correction_write,
        actor_role="mf_sub",
    )
    assert idempotent["ok"] is True
    assert idempotent["status"] == "already_completed"
    assert runtime.store.get(execution["contract_execution_id"])[
        "execution_state_revision"
    ] == corrected_revision

    wrong_idempotent_lineage = {
        **correction_write,
        "payload": {
            **correction_payload,
            "precommit_implementation_correction_intent": {
                **correction_payload[
                    "precommit_implementation_correction_intent"
                ],
                "prior_implementation_lineage_ref": (
                    "contract-runtime:worker-implementation:sha256:"
                    + "f" * 64
                ),
            },
            "canonical_precommit_lineage_revision_authority": {
                **correction_payload[
                    "canonical_precommit_lineage_revision_authority"
                ],
                "prior_implementation_lineage_ref": (
                    "contract-runtime:worker-implementation:sha256:"
                    + "f" * 64
                ),
            },
        },
    }
    rejected_idempotent = runtime.revise_precommit_worker_implementation(
        execution["contract_execution_id"],
        wrong_idempotent_lineage,
        actor_role="mf_sub",
    )
    assert rejected_idempotent["ok"] is False
    assert any(
        "intent must bind the prior implementation lineage" in error
        for error in rejected_idempotent["decision"]["errors"]
    )
    assert runtime.store.get(execution["contract_execution_id"])[
        "execution_state_revision"
    ] == corrected_revision

    negative_cases = {
        "different_head": {
            **correction_write,
            "commit_sha": "c" * 40,
            "payload": {
                **correction_payload,
                "commit_sha": "c" * 40,
                "canonical_precommit_lineage_revision_authority": {
                    **correction_payload[
                        "canonical_precommit_lineage_revision_authority"
                    ],
                    "actual_head_commit": "c" * 40,
                },
            },
        },
        "different_worker": {
            **correction_write,
            "worker_id": "different-worker",
            "payload": {
                **correction_payload,
                "worker_id": "different-worker",
            },
        },
        "different_session": {
            **correction_write,
            "session_token_ref": "wstok-different-session",
            "payload": {
                **correction_payload,
                "session_token_ref": "wstok-different-session",
            },
        },
        "different_fence": {
            **correction_write,
            "fence_token_hash": "sha256:different-fence",
            "payload": {
                **correction_payload,
                "fence_token_hash": "sha256:different-fence",
            },
        },
        "caller_graph_authority": {
            **correction_write,
            "payload": {
                **correction_payload,
                "graph_trace_db_evidence": {
                    "db_verified": False,
                    "verified_trace_ids": ["gqt-precommit-corrected"],
                },
            },
        },
    }
    for case_name, candidate in negative_cases.items():
        rejected = runtime.revise_precommit_worker_implementation(
            execution["contract_execution_id"],
            candidate,
            actor_role="mf_sub",
        )
        assert rejected["ok"] is False, case_name
        assert runtime.store.get(execution["contract_execution_id"])[
            "execution_state_revision"
        ] == corrected_revision, case_name

    after_correction = runtime.store.get(execution["contract_execution_id"])
    worker_commit = {
        "stage_id": "worker_commit",
        "line_id": "worker_commit",
        "actor_role": "mf_sub",
        "evidence_kind": "worker_commit",
        **identity,
        "implementation_lineage_ref": latest_lineage[
            "implementation_lineage_ref"
        ],
        "commit_sha": corrected_head,
        "payload": {
            **identity,
            "implementation_lineage_ref": latest_lineage[
                "implementation_lineage_ref"
            ],
            "commit_sha": corrected_head,
        },
    }
    after_correction["completed_lines"].append(worker_commit)
    expected_revision = int(after_correction["execution_state_revision"])
    after_correction["execution_state_revision"] = expected_revision + 1
    runtime.store.update(
        execution["contract_execution_id"],
        after_correction,
        expected_revision=expected_revision,
    )
    runtime.current_guide(
        execution["contract_execution_id"],
        actor_role="mf_sub",
    )
    after_commit_revision = runtime.store.get(
        execution["contract_execution_id"]
    )["execution_state_revision"]
    closed = runtime.revise_precommit_worker_implementation(
        execution["contract_execution_id"],
        correction_write,
        actor_role="mf_sub",
    )
    assert closed["ok"] is False
    assert any(
        "closed after worker_commit" in error
        for error in closed["decision"]["errors"]
    )
    assert runtime.store.get(execution["contract_execution_id"])[
        "execution_state_revision"
    ] == after_commit_revision


def _write_minimal_contract(tmp_path, *, status: str = "active"):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    guide = prompts / "observer.md"
    guide.write_text("Follow the compiled runtime guide.\n", encoding="utf-8")
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": "observer_onboard",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "onboard",
        "status": status,
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "bootstrap",
                    "lines": [
                        {
                            "line_id": "read_context",
                            "owner_role": "observer",
                            "allowed_writer_roles": ["observer"],
                            "evidence_kind": "contract_state_changed",
                        }
                    ],
                },
                {
                    "stage_id": "qa",
                    "lines": [
                        {
                            "line_id": "qa_verdict",
                            "owner_role": "qa",
                            "allowed_writer_roles": ["qa"],
                            "evidence_kind": "qa_verification",
                        }
                    ],
                },
            ]
        },
        "instruction_layer": {
            "inline": ["Runtime guide is authoritative."],
            "refs": [
                {
                    "id": "observer_prompt",
                    "path": "prompts/observer.md",
                    "sha256": file_sha256(guide),
                    "visible_to_roles": ["observer"],
                    "stage_ids": ["bootstrap"],
                }
            ],
        },
    }
    path = tmp_path / "observer_onboard.v1.rev1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_contract_definition(
    tmp_path,
    *,
    contract_id: str,
    stages: list[dict],
    system_layer: dict | None = None,
):
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": contract_id,
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": contract_id,
        "status": "active",
        "rule_layer": {"stages": stages},
        "instruction_layer": {
            "inline": ["Runtime guide is authoritative."],
            "refs": [],
        },
    }
    if system_layer is not None:
        payload["system_layer"] = system_layer
    path = tmp_path / f"{contract_id}.v1.rev1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_chain_projection_contracts(tmp_path):
    _write_minimal_contract(tmp_path)
    _write_contract_definition(
        tmp_path,
        contract_id="direct_fix",
        stages=[
            {
                "stage_id": "worker_graph_context",
                "lines": [
                    {
                        "line_id": "direct_fix_worker_graph_context",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "graph_trace",
                    }
                ],
            },
            {
                "stage_id": "candidate_repair",
                "lines": [
                    {
                        "line_id": "direct_fix_candidate_repair",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "direct_fix_repair_evidence",
                        "requires": ["direct_fix_worker_graph_context"],
                    }
                ],
            },
            {
                "stage_id": "qa_graph_context",
                "lines": [
                    {
                        "line_id": "direct_fix_qa_graph_context",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "graph_trace",
                        "requires": ["direct_fix_candidate_repair"],
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                        "requires": ["direct_fix_qa_graph_context"],
                    }
                ],
            },
            {
                "stage_id": "return_to_parent",
                "lines": [
                    {
                        "line_id": "direct_fix_return_to_parent",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "direct_fix_return_to_parent",
                        "requires": ["qa_independent_verification"],
                    }
                ],
            },
        ],
    )
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v1",
        stages=[
            {
                "stage_id": "observer_prefill",
                "lines": [
                    {
                        "line_id": "observer_prefill_child_contracts",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "mf_parallel_prefill",
                    }
                ],
            }
        ],
    )


def _write_from(record, *, actor_role, stage_id, line_id, evidence_kind=None):
    state = record["execution_state"]
    guide = record["runtime_guide"]
    next_action = guide.get("next_legal_action") or {}
    return {
        "project_id": record["project_id"],
        "backlog_id": record["backlog_id"],
        "contract_execution_id": record["contract_execution_id"],
        "definition_hash": record["definition_hash"],
        "instruction_bundle_hash": record["instruction_bundle_hash"],
        "execution_state_revision": state["execution_state_revision"],
        "runtime_guide_hash": guide["runtime_guide_hash"],
        "stage_id": stage_id,
        "line_id": line_id,
        "actor_role": actor_role,
        "evidence_kind": evidence_kind or next_action.get("evidence_kind") or "",
    }


def _start_completed_chain_projection_root(
    runtime: ContractRuntime,
    *,
    backlog_id: str,
    contract_execution_id: str,
) -> dict:
    root = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id=contract_execution_id,
        actor_role="observer",
    )
    completed = runtime.submit_line_write(
        contract_execution_id,
        _write_from(
            root,
            actor_role="observer",
            stage_id="observer_prefill",
            line_id="observer_prefill_child_contracts",
        ),
        actor_role="observer",
    )
    assert completed["ok"] is True
    return completed["record"]


def _direct_fix_graph_payload(
    *,
    actor_role: str,
    trace_id: str,
    include_bounded_qa: bool = True,
    project_id: str = "aming-claw",
    backlog_id: str = "AC-CONTRACT-RUNTIME",
    task_id: str = "direct-fix-worker-test",
):
    query_source = {
        "observer": "observer",
        "mf_sub": "mf_subagent",
        "qa": "qa",
    }[actor_role]
    query_purpose = {
        "observer": "observer_scope_build",
        "mf_sub": "subagent_context_build",
        "qa": "independent_verification",
    }[actor_role]
    evidence = {
        "schema_version": "direct_fix_graph_trace_db_evidence.v1",
        "source": "graph_query_traces",
        "db_verified": True,
        "trace_ids": [trace_id],
        "verified_trace_ids": [trace_id],
        "missing_trace_ids": [],
        "identity_mismatches": [],
        "query_source": query_source,
        "query_purpose": query_purpose,
        "target_project_root": "/tmp/aming-claw-test",
    }
    if actor_role == "mf_sub":
        evidence.update(
            {
                "worker_role": "mf_sub",
                "runtime_context_id": "mfrctx-test",
                "task_id": "direct-fix-worker-test",
                "parent_task_id": "cex-direct-fix-parent-test",
            }
        )
    if actor_role == "qa" and include_bounded_qa:
        commit_sha = "a" * 40
        evidence.update(
            {
                "qa_session_id": "ses-qa-contract-runtime",
                "qa_principal": "qa-contract-runtime",
                "project_id": project_id,
                "backlog_id": backlog_id,
                "task_id": task_id,
                "graph_basis": "exact_candidate_snapshot",
                "canonical_base_snapshot_id": "full-contract-runtime-qa",
                "base_commit_sha": commit_sha,
                "candidate_commit_sha": commit_sha,
                "changed_files": [],
                "candidate_diff_hash": (
                    "sha256:" + hashlib.sha256(b"").hexdigest()
                ),
                "changed_files_source": "server_exact_candidate_snapshot",
            }
        )
    return {
        "schema_version": "direct_fix_graph_context.v1",
        "graph_trace_ids": [trace_id],
        "graph_trace_evidence": evidence,
    }


def _start_repaired_direct_fix(runtime, *, backlog_id: str):
    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id=f"cex-root-{backlog_id}",
        actor_role="observer",
        route_token_ref=f"rtok-root-{backlog_id}",
    )
    direct_fix, generation, repair_ref = _start_repaired_direct_fix_child(
        runtime,
        root,
        backlog_id=backlog_id,
        contract_execution_id=f"cex-direct-{backlog_id}",
    )
    return root, direct_fix, generation, repair_ref


def _start_repaired_direct_fix_child(
    runtime,
    root,
    *,
    backlog_id: str,
    contract_execution_id: str,
):
    direct_fix = runtime.start_execution(
        "direct_fix",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id=contract_execution_id,
        actor_role="observer",
        route_token_ref=f"rtok-{contract_execution_id}",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )
    runtime.current_guide(direct_fix["contract_execution_id"], actor_role="mf_sub")
    direct_fix = runtime.store.get(direct_fix["contract_execution_id"])
    worker_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                direct_fix,
                actor_role="mf_sub",
                stage_id="worker_graph_context",
                line_id="direct_fix_worker_graph_context",
                evidence_kind="graph_trace",
            ),
            "runtime_context_id": "mfrctx-test",
            "task_id": "direct-fix-worker-test",
            "parent_task_id": "cex-direct-fix-parent-test",
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id=f"gqt-worker-{contract_execution_id}",
            ),
        },
        actor_role="mf_sub",
    )
    assert worker_graph["ok"] is True
    direct_fix = worker_graph["record"]
    repaired = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            direct_fix,
            actor_role="mf_sub",
            stage_id="candidate_repair",
            line_id="direct_fix_candidate_repair",
            evidence_kind="direct_fix_repair_evidence",
        ),
        actor_role="mf_sub",
    )
    assert repaired["ok"] is True
    record = repaired["record"]
    repair_line_index = len(record["completed_lines"]) - 1
    repair_ref = (
        f"contract_runtime:{record['contract_execution_id']}:"
        f"completed_lines:{repair_line_index}"
    )
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    record = runtime.store.get(record["contract_execution_id"])
    qa_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="direct_fix_qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="qa",
                trace_id=f"gqt-qa-{contract_execution_id}",
            ),
        },
        actor_role="qa",
    )
    assert qa_graph["ok"] is True
    record = qa_graph["record"]
    return record, int(record["execution_state_revision"]), repair_ref


def _direct_fix_qa_write(record, *, generation: int, repair_ref: str):
    write = _write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
        evidence_kind="independent_verification",
    )
    write["status"] = "passed"
    write["payload"] = {
        "status": "pass",
        "direct_fix_contract_execution_id": record["contract_execution_id"],
        "projection_generation": generation,
        "source_refs": [repair_ref],
    }
    write["verification"] = {
        "verdict": "PASS",
        "independent": True,
        "child_contract_execution_id": record["contract_execution_id"],
        "projection_generation": generation,
        "source_ref": repair_ref,
    }
    return write


def _return_direct_fix_to_parent(runtime, record, *, generation: int, repair_ref: str):
    qa = runtime.submit_line_write(
        record["contract_execution_id"],
        _direct_fix_qa_write(
            record,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True
    runtime.current_guide(record["contract_execution_id"], actor_role="observer")
    qa_record = runtime.store.get(record["contract_execution_id"])
    returned = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            qa_record,
            actor_role="observer",
            stage_id="return_to_parent",
            line_id="direct_fix_return_to_parent",
            evidence_kind="direct_fix_return_to_parent",
        ),
        actor_role="observer",
    )
    assert returned["ok"] is True
    return returned["record"]


def test_direct_fix_graph_context_gates_repair_and_qa(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
        contract_execution_id="cex-root-graph-gate",
        actor_role="observer",
        route_token_ref="rtok-root-graph-gate",
    )
    direct_fix = runtime.start_execution(
        "direct_fix",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
        contract_execution_id="cex-direct-graph-gate",
        actor_role="observer",
        route_token_ref="rtok-direct-graph-gate",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    guide = runtime.current_guide(direct_fix["contract_execution_id"], actor_role="mf_sub")
    assert guide["next_legal_action"]["line_id"] == "direct_fix_worker_graph_context"

    missing_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            runtime.store.get(direct_fix["contract_execution_id"]),
            actor_role="mf_sub",
            stage_id="worker_graph_context",
            line_id="direct_fix_worker_graph_context",
            evidence_kind="graph_trace",
        ),
        actor_role="mf_sub",
    )
    assert missing_graph["ok"] is False
    assert "direct_fix_worker_graph_context requires non-empty graph_trace_ids" in (
        missing_graph["decision"]["errors"]
    )

    record = runtime.store.get(direct_fix["contract_execution_id"])
    worker_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="mf_sub",
                stage_id="worker_graph_context",
                line_id="direct_fix_worker_graph_context",
                evidence_kind="graph_trace",
            ),
            "runtime_context_id": "mfrctx-test",
            "task_id": "direct-fix-worker-test",
            "parent_task_id": "cex-direct-fix-parent-test",
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-worker-graph-gate",
            ),
        },
        actor_role="mf_sub",
    )
    assert worker_graph["ok"] is True
    assert (
        worker_graph["record"]["runtime_guide"]["next_legal_action"]["line_id"]
        == "direct_fix_candidate_repair"
    )

    repaired = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            worker_graph["record"],
            actor_role="mf_sub",
            stage_id="candidate_repair",
            line_id="direct_fix_candidate_repair",
            evidence_kind="direct_fix_repair_evidence",
        ),
        actor_role="mf_sub",
    )
    assert repaired["ok"] is True
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
    )
    assert current["readiness_state"] == "direct_fix_complete_awaiting_independent_qa_graph"
    assert current["next_legal_action"]["line_id"] == "direct_fix_qa_graph_context"

    record = runtime.store.get(direct_fix["contract_execution_id"])
    bad_qa_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="direct_fix_qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-wrong-role-for-qa",
            ),
        },
        actor_role="qa",
    )
    assert bad_qa_graph["ok"] is False
    assert any("query_source must be one of ['qa']" in error for error in bad_qa_graph["decision"]["errors"])

    record = runtime.store.get(direct_fix["contract_execution_id"])
    qa_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="direct_fix_qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="qa",
                trace_id="gqt-qa-graph-gate",
            ),
        },
        actor_role="qa",
    )
    assert qa_graph["ok"] is True
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
    )
    assert current["next_legal_action"]["line_id"] == "qa_independent_verification"


def test_mf_parallel_v2_graph_context_gates_worker_and_qa(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel_graph_gate",
        stages=[
            {
                "stage_id": "worker_context",
                "lines": [
                    {
                        "line_id": "worker_graph_context",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "graph_trace",
                    }
                ],
            },
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                        "requires": ["worker_graph_context"],
                    }
                ],
            },
            {
                "stage_id": "qa_graph_context",
                "lines": [
                    {
                        "line_id": "qa_graph_context",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "graph_trace",
                        "requires": ["worker_implementation"],
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                        "requires": ["qa_graph_context"],
                    }
                ],
            },
        ],
        system_layer={
            "graph_binding_policy": {
                "bounded_qa_review_policy": {
                    "schema_version": "contract_bounded_qa_review_policy.v1",
                    "enabled": True,
                    "line_ids": ["qa_graph_context"],
                    "authority_object_path": "payload.graph_trace_evidence",
                    "authority_source": "graph_query_traces",
                    "lookup_key_fields": [
                        "graph_trace_ids",
                        "graph_query_trace_ids",
                    ],
                    "query_sources": ["qa"],
                    "query_purposes": ["independent_verification"],
                    "accepted_graph_basis": [
                        "exact_candidate_snapshot",
                        "canonical_base_plus_candidate_diff",
                    ],
                    "base_diff_required_hash_fields": [
                        "candidate_overlay_hash",
                        "root_identity_hash",
                        "query_root_identity_hash",
                        "canonical_project_identity_hash",
                        "repository_identity_hash",
                    ],
                    "required_identity_fields": [
                        "project_id",
                        "backlog_id",
                        "task_id",
                        "qa_session_id",
                        "qa_principal",
                        "target_project_root",
                    ],
                }
            }
        },
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "mf_parallel_graph_gate",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-V2-GRAPH-GATE",
        contract_execution_id="cex-mf-parallel-v2-graph-gate",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-v2-graph-gate",
    )
    assert record["runtime_guide"]["next_legal_action"]["line_id"] == "worker_graph_context"
    runtime.current_guide(record["contract_execution_id"], actor_role="mf_sub")
    record = runtime.store.get(record["contract_execution_id"])

    missing_worker_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            record,
            actor_role="mf_sub",
            stage_id="worker_context",
            line_id="worker_graph_context",
            evidence_kind="graph_trace",
        ),
        actor_role="mf_sub",
    )
    assert missing_worker_graph["ok"] is False
    assert "worker_graph_context requires non-empty graph_trace_ids" in (
        missing_worker_graph["decision"]["errors"]
    )

    worker_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                runtime.store.get(record["contract_execution_id"]),
                actor_role="mf_sub",
                stage_id="worker_context",
                line_id="worker_graph_context",
                evidence_kind="graph_trace",
            ),
            "runtime_context_id": "mfrctx-mf-parallel-v2-test",
            "task_id": "mf-parallel-v2-worker",
            "parent_task_id": "cex-mf-parallel-v2-parent",
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-mf-parallel-v2-worker",
            ),
        },
        actor_role="mf_sub",
    )
    assert worker_graph["ok"] is True

    implementation = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            worker_graph["record"],
            actor_role="mf_sub",
            stage_id="worker_implementation",
            line_id="worker_implementation",
            evidence_kind="implementation",
        ),
        actor_role="mf_sub",
    )
    assert implementation["ok"] is True
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    bad_qa_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-mf-parallel-v2-wrong-source",
            ),
        },
        actor_role="qa",
    )
    assert bad_qa_graph["ok"] is False
    assert any(
        "qa_graph_context query_source must be one of ['qa']" in error
        for error in bad_qa_graph["decision"]["errors"]
    )
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    tupleless_qa_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="qa",
                trace_id="gqt-mf-parallel-v2-qa",
                include_bounded_qa=False,
                backlog_id="AC-MF-PARALLEL-V2-GRAPH-GATE",
                task_id="mf-parallel-v2-worker",
            ),
        },
        actor_role="qa",
    )
    assert tupleless_qa_graph["ok"] is False
    assert any(
        "qa_graph_context requires a supported graph_basis" in error
        for error in tupleless_qa_graph["decision"]["errors"]
    )
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    forged_exact_source_payload = _direct_fix_graph_payload(
        actor_role="qa",
        trace_id="gqt-mf-parallel-v2-qa-forged-exact-source",
        backlog_id="AC-MF-PARALLEL-V2-GRAPH-GATE",
        task_id="mf-parallel-v2-worker",
    )
    forged_exact_source_payload["graph_trace_evidence"][
        "changed_files_source"
    ] = "server_forged_exact_candidate_snapshot"
    forged_exact_source = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": forged_exact_source_payload,
        },
        actor_role="qa",
    )
    assert forged_exact_source["ok"] is False
    assert any(
        "requires changed_files_source=server_exact_candidate_snapshot" in error
        for error in forged_exact_source["decision"]["errors"]
    )
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    required_empty_comparison_payload = _direct_fix_graph_payload(
        actor_role="qa",
        trace_id="gqt-mf-parallel-v2-qa-required-empty-comparison",
        backlog_id="AC-MF-PARALLEL-V2-GRAPH-GATE",
        task_id="mf-parallel-v2-worker",
    )
    required_empty_comparison_payload["graph_trace_evidence"][
        "comparison_authority_required"
    ] = True
    required_empty_comparison = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": required_empty_comparison_payload,
        },
        actor_role="qa",
    )
    assert required_empty_comparison["ok"] is False
    assert any(
        "comparison_authority_required=true" in error
        for error in required_empty_comparison["decision"]["errors"]
    )
    required_empty_adapter = required_empty_comparison["decision"][
        "imported_legacy_checks"
    ][0]
    assert {
        mismatch["field"]
        for mismatch in required_empty_adapter["identity_mismatches"]
    } == {
        "comparison_base_commit_sha",
        "comparison_base_commit_source",
        "changed_files_source",
    }
    assert all(
        set(mismatch) >= {"field", "expected", "actual"}
        for mismatch in required_empty_adapter["identity_mismatches"]
    )
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    comparison_qa_payload = _direct_fix_graph_payload(
        actor_role="qa",
        trace_id="gqt-mf-parallel-v2-qa-comparison",
        backlog_id="AC-MF-PARALLEL-V2-GRAPH-GATE",
        task_id="mf-parallel-v2-worker",
    )
    comparison_authority = comparison_qa_payload["graph_trace_evidence"]
    comparison_authority.update(
        {
            "changed_files": ["agent/governance/contracts/write_gate.py"],
            "candidate_diff_hash": (
                "sha256:" + hashlib.sha256(b"canonical-comparison-diff").hexdigest()
            ),
            "changed_files_source": (
                "server_runtime_context_base_to_exact_candidate_diff"
            ),
            "comparison_base_commit_sha": "b" * 40,
            "comparison_base_commit_source": (
                "ContractRuntime.completed_lines.worker_commit+"
                "parallel_branch_runtime_context.base_commit"
            ),
        }
    )

    incomplete_comparison_payload = json.loads(
        json.dumps(comparison_qa_payload)
    )
    incomplete_comparison_payload["graph_trace_evidence"].pop(
        "comparison_base_commit_source"
    )
    incomplete_comparison = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": incomplete_comparison_payload,
        },
        actor_role="qa",
    )
    assert incomplete_comparison["ok"] is False
    assert any(
        "requires comparison_base_commit_source=" in error
        for error in incomplete_comparison["decision"]["errors"]
    )
    assert incomplete_comparison["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ] == [
        {
            "field": "comparison_base_commit_source",
            "expected": [
                (
                    "ContractRuntime.completed_lines.observer_merge+"
                    "parallel_branch_merge_queue_items.target_head_before_merge"
                ),
                (
                    "ContractRuntime.completed_lines.worker_commit+"
                    "parallel_branch_runtime_context.base_commit"
                ),
            ],
            "actual": "",
        }
    ]
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    same_commit_comparison_payload = json.loads(
        json.dumps(comparison_qa_payload)
    )
    same_commit_authority = same_commit_comparison_payload[
        "graph_trace_evidence"
    ]
    same_commit_authority["comparison_base_commit_sha"] = (
        same_commit_authority["candidate_commit_sha"]
    )
    same_commit_comparison = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": same_commit_comparison_payload,
        },
        actor_role="qa",
    )
    assert same_commit_comparison["ok"] is False
    assert any(
        "requires distinct comparison base and candidate commits" in error
        for error in same_commit_comparison["decision"]["errors"]
    )
    same_commit_mismatch = same_commit_comparison["decision"][
        "imported_legacy_checks"
    ][0]["identity_mismatches"]
    assert same_commit_mismatch == [
        {
            "field": "comparison_base_commit_sha",
            "expected": (
                "full git object id distinct from candidate_commit_sha"
            ),
            "actual": same_commit_authority["candidate_commit_sha"],
        }
    ]
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    forged_source_comparison_payload = json.loads(
        json.dumps(comparison_qa_payload)
    )
    forged_source_comparison_payload["graph_trace_evidence"][
        "changed_files_source"
    ] = "server_forged_comparison_diff"
    forged_source_comparison = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": forged_source_comparison_payload,
        },
        actor_role="qa",
    )
    assert forged_source_comparison["ok"] is False
    assert any(
        "requires changed_files_source="
        "server_runtime_context_base_to_exact_candidate_diff" in error
        for error in forged_source_comparison["decision"]["errors"]
    )
    assert forged_source_comparison["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ] == [
        {
            "field": "changed_files_source",
            "expected": (
                "server_runtime_context_base_to_exact_candidate_diff"
            ),
            "actual": "server_forged_comparison_diff",
        }
    ]
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    qa_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": comparison_qa_payload,
        },
        actor_role="qa",
    )
    assert qa_graph["ok"] is True
    assert (
        qa_graph["record"]["runtime_guide"]["next_legal_action"]["line_id"]
        == "qa_independent_verification"
    )


def test_direct_fix_same_revision_graph_gate_migration_is_explicit(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="direct_fix",
        stages=[
            {
                "stage_id": "candidate_repair",
                "lines": [
                    {
                        "line_id": "direct_fix_candidate_repair",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "direct_fix_repair_evidence",
                    }
                ],
            }
        ],
    )
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "direct_fix",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-MIGRATION",
        contract_execution_id="cex-direct-legacy-active",
        actor_role="observer",
        route_token_ref="rtok-legacy-active",
    )
    assert record["runtime_guide"]["next_legal_action"]["line_id"] == "direct_fix_candidate_repair"

    _write_chain_projection_contracts(tmp_path)

    with pytest.raises(StalePinnedContractExecutionError):
        runtime.current_guide(record["contract_execution_id"], actor_role="mf_sub")


def _append_parent_successor_ack(runtime, *, parent_id: str, child_id: str):
    parent = runtime.store.get(parent_id)
    completed_lines = list(parent.get("completed_lines") or [])
    completed_lines.append(
        {
            "stage_id": "successor_return",
            "line_id": "resume_parent_after_successor_return",
            "actor_role": "observer",
            "evidence_kind": "successor_return_acknowledgement",
            "payload": {
                "schema_version": "successor_return_acknowledgement.v1",
                "parent_contract_execution_id": parent_id,
                "successor_contract_execution_id": child_id,
                "successor_contract_id": "direct_fix",
            },
        }
    )
    parent["completed_lines"] = completed_lines
    revision = int(parent.get("execution_state_revision") or 0) + 1
    parent["execution_state_revision"] = revision
    execution_state = parent.get("execution_state")
    if isinstance(execution_state, dict):
        execution_state["execution_state_revision"] = revision
    return runtime.store.update(parent_id, parent)


def test_minimal_contract_runtime_drives_next_action_and_role_gate(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)

    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        contract_execution_id="cex-min-path",
        actor_role="observer",
        route_token_ref="rtok-min-path",
    )

    guide = record["runtime_guide"]
    assert guide["next_legal_action"] == {
        "stage_id": "bootstrap",
        "line_id": "read_context",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "evidence_kind": "contract_state_changed",
        "required": True,
    }
    assert guide["instructions"]["inline"] == ["Runtime guide is authoritative."]
    assert guide["instructions"]["refs"][0]["content"] == "Follow the compiled runtime guide.\n"

    result = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            record,
            actor_role="observer",
            stage_id="bootstrap",
            line_id="read_context",
        ),
    )
    assert result["ok"] is True
    next_record = result["record"]
    assert next_record["execution_state"]["execution_state_revision"] == 2
    assert next_record["runtime_guide"]["next_legal_action"]["stage_id"] == "qa"
    assert next_record["runtime_guide"]["next_legal_action"]["owner_role"] == "qa"

    rejected = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            next_record,
            actor_role="observer",
            stage_id="qa",
            line_id="qa_verdict",
        ),
    )
    assert rejected["ok"] is False
    assert "cannot write line" in rejected["decision"]["errors"][0]

    runtime.current_guide("cex-min-path", actor_role="qa")
    qa_record = runtime.store.get("cex-min-path")
    accepted_qa = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            qa_record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_verdict",
        ),
    )
    assert accepted_qa["ok"] is True
    assert accepted_qa["record"]["runtime_guide"]["next_legal_action"] is None


def test_minimal_runtime_rejects_stale_runtime_guide_hash(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )
    write = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
    )
    write["runtime_guide_hash"] = "sha256:" + "0" * 64

    result = runtime.submit_line_write(record["contract_execution_id"], write)

    assert result["ok"] is False
    assert "runtime_guide_hash mismatch" in result["decision"]["errors"]


def test_write_gate_accepts_exact_server_copy_safe_writer_hash_from_reader_guide(
    tmp_path,
):
    _write_minimal_contract(tmp_path)
    registry = ContractDefinitionRegistry(tmp_path)
    runtime = ContractRuntime(registry, instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH-CROSS-ROLE-HASH",
        contract_execution_id="cex-min-path-cross-role-hash",
        actor_role="observer",
    )
    first = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            record,
            actor_role="observer",
            stage_id="bootstrap",
            line_id="read_context",
        ),
    )
    assert first["ok"] is True
    reader_record = first["record"]
    reader_guide = reader_record["runtime_guide"]
    safe_copy = reader_guide["writer_role_safe_copy_payload"]["copy_payload"]
    assert reader_guide["runtime_guide_hash"] != safe_copy[
        "runtime_guide_hash"
    ]

    definition = registry.get(
        reader_record["contract_id"],
        version=reader_record["version"],
        revision=reader_record["revision"],
    )
    accepted = validate_contract_write(
        definition,
        reader_record["execution_state"],
        safe_copy,
        runtime_guide=reader_guide,
    )
    assert accepted.ok is True

    stale = dict(safe_copy)
    stale["runtime_guide_hash"] = "sha256:" + "0" * 64
    rejected = validate_contract_write(
        definition,
        reader_record["execution_state"],
        stale,
        runtime_guide=reader_guide,
    )
    assert rejected.ok is False
    detailed = next(
        error
        for error in rejected.errors
        if error.startswith("runtime_guide_hash mismatch:")
    )
    assert safe_copy["runtime_guide_hash"] in detailed
    assert reader_guide["runtime_guide_hash"] in detailed
    assert (
        f"({stale['runtime_guide_hash']})" in detailed
        or repr(stale["runtime_guide_hash"]) in detailed
    )

    wrong_line = dict(safe_copy)
    wrong_line["line_id"] = "different_line"
    cross_line = validate_contract_write(
        definition,
        reader_record["execution_state"],
        wrong_line,
        runtime_guide=reader_guide,
    )
    assert cross_line.ok is False
    assert any(
        error.startswith("runtime_guide_hash mismatch:")
        for error in cross_line.errors
    )


def test_runtime_bypass_current_line_is_audited_idempotent_and_stale_safe(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="bypass_min_path",
        stages=[
            {
                "stage_id": "bootstrap",
                "lines": [
                    {
                        "line_id": "read_context",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "read_receipt",
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_verdict",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "qa_verification",
                        "requires": ["read_context"],
                    }
                ],
            },
        ],
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "bypass_min_path",
        project_id="aming-claw",
        backlog_id="AC-BYPASS-MIN-PATH",
        actor_role="observer",
    )
    request = {
        "bypass_identity": "bypass:cex:1:read_context",
        "stage_id": "bootstrap",
        "line_id": "read_context",
        "execution_state_revision": record["execution_state_revision"],
        "runtime_guide_hash": record["runtime_guide"]["runtime_guide_hash"],
        "diagnostic_backlog_id": "AC-BYPASS-DIAGNOSTIC",
        "classification": "process_contract_conflict",
        "reason": "guide cannot produce the required receipt",
        "decision": "operator chose audited continuation",
        "evidence_refs": ["backlog:AC-BYPASS-DIAGNOSTIC", "timeline:42"],
    }

    result = runtime.bypass_current_line(
        record["contract_execution_id"],
        request,
        actor_role="observer",
    )

    assert result["ok"] is True
    assert result["idempotent"] is False
    assert result["decision"] == {
        "ok": True,
        "errors": [],
        "disposition": "proceeded_with_exception",
        "no_pass_claim": True,
    }
    assert result["written_line"]["status"] == "waived"
    assert result["written_line"]["evidence_kind"] == "contract_line_bypass"
    assert result["written_line"]["no_pass_claim"] is True
    assert result["written_line"]["payload"]["diagnostic_backlog_id"] == (
        "AC-BYPASS-DIAGNOSTIC"
    )
    assert result["record"]["execution_state_revision"] == 2
    assert result["record"]["runtime_guide"]["next_legal_action"]["line_id"] == (
        "qa_verdict"
    )
    assert "terminal_disposition" not in result["record"]["runtime_guide"]
    assert "bypass_recovery_fallback" not in result["record"]["runtime_guide"]

    merged_commit = "a" * 40
    durable_merge_authority = {
        "schema_version": "contract_runtime.observer_merge_durable_authority.v1",
        "server_derived": True,
        "db_verified": True,
        "merge_gate_passed": True,
        "merge_event_ref": "timeline:42",
        "merge_commit": merged_commit,
        "qa_contract_runtime_verified": True,
        "qa_acceptance_ref": "contract-runtime-acceptance:qa:1",
    }
    current_full_reconcile_authority = {
        "schema_version": (
            "graph_snapshot_store.current_full_reconcile_state.v1"
        ),
        "source": "graph_snapshot_store.current_full_reconcile_state",
        "server_derived": True,
        "db_verified": True,
        "live_verified": True,
        "canonical_head_verified": True,
        "active_snapshot_verified": True,
        "active_snapshot_matches_canonical_head": True,
        "graph_reconciled": True,
        "provenance_verified": True,
        "provenance_scope_verified": True,
        "durable_order_verified": True,
        "reconcile_snapshot_verified": True,
        "contract_execution_scope_verified": True,
        "task_scope_verified": True,
        "runtime_context_scope_verified": True,
        "parent_task_scope_verified": True,
        "merge_queue_scope_verified": True,
        "current_full_reconcile": True,
        "strategy": "current_full_reconcile",
        "active_snapshot_status": "active",
        "active_snapshot_id": "full-aaaaaaaa-current",
        "active_snapshot_commit": merged_commit,
        "canonical_head_commit": merged_commit,
        "current_canonical_commit_sha": merged_commit,
        "reconciled_commit_sha": merged_commit,
        "reconcile_provenance_target_commit": merged_commit,
        "merge_source_ref": "timeline:42",
        "merged_commit_sha": merged_commit,
        "reconcile_source_ref": "timeline:43",
        "project_id": record["project_id"],
        "backlog_id": record["backlog_id"],
        "contract_execution_id": record["contract_execution_id"],
    }
    current_full_reconcile_authority["authority_hash"] = stable_sha256(
        current_full_reconcile_authority
    )
    reconcile_authority = {
        "schema_version": "contract_runtime.observer_reconcile_record_authority.v1",
        "server_derived": True,
        "record_verified": True,
        "merge_projection_verified": True,
        "dispatch_lineage_verified": True,
        "reconcile_event_recorded": True,
        "merge_source_ref": "timeline:42",
        "merged_commit_sha": merged_commit,
        "reconcile_source_ref": "timeline:43",
        "current_full_reconcile_activation_verified": True,
        "terminal_current_full_reconcile_authority": (
            current_full_reconcile_authority
        ),
    }
    reconcile_authority["authority_hash"] = stable_sha256(reconcile_authority)
    terminal_lines = [
        result["written_line"],
        {
            "line_id": "qa_independent_verification",
            "actor_role": "qa",
            "evidence_kind": "independent_verification",
            "status": "passed",
        },
        {
            "line_id": "observer_merge",
            "actor_role": "observer",
            "evidence_kind": "merge",
            "status": "accepted",
            "payload": {
                "durable_merge_authority": durable_merge_authority,
            },
        },
        {
            "line_id": "observer_reconcile",
            "actor_role": "observer",
            "evidence_kind": "reconcile",
            "status": "accepted",
            "payload": {"reconcile_authority": reconcile_authority},
        },
    ]
    terminal_record = {
        "project_id": record["project_id"],
        "backlog_id": record["backlog_id"],
        "contract_execution_id": record["contract_execution_id"],
        "contract_id": record["contract_id"],
        "completed_lines": terminal_lines,
    }
    receipt_only_lines = json.loads(json.dumps(terminal_lines))
    receipt_only_authority = receipt_only_lines[-1]["payload"][
        "reconcile_authority"
    ]
    receipt_only_authority.pop(
        "terminal_current_full_reconcile_authority"
    )
    receipt_only_authority["current_full_reconcile_activation_verified"] = False
    receipt_only_authority.pop("authority_hash")
    receipt_only_authority["authority_hash"] = stable_sha256(
        receipt_only_authority
    )
    assert _audited_bypass_terminal_disposition(
        {**terminal_record, "completed_lines": receipt_only_lines}
    ) == {}
    terminal = _audited_bypass_terminal_disposition(terminal_record)
    assert terminal["row_status"] == "WAIVED"
    assert terminal["readiness_state"] == "completed_with_exception"
    assert terminal["scheduler_eligible"] is False
    assert terminal["current_eligible"] is False
    assert terminal["close_eligible"] is False
    assert terminal["resume_eligible"] is False
    assert terminal["source_backlog_mutated"] is False
    assert terminal["terminal_barrier"]["bypass_line_index"] == 0
    assert terminal["terminal_barrier"]["reconcile_source_ref"] == "timeline:43"
    assert terminal["terminal_barrier"]["active_snapshot_id"] == (
        "full-aaaaaaaa-current"
    )
    assert terminal["terminal_barrier"]["active_snapshot_commit"] == merged_commit
    assert terminal["terminal_barrier"]["canonical_head_commit"] == merged_commit
    assert terminal["terminal_barrier"][
        "reconcile_provenance_target_commit"
    ] == merged_commit
    fallback = terminal["bypass_recovery_fallback"]
    assert fallback["schema_version"] == (
        "contract_runtime.bypass_recovery_fallback.v1"
    )
    assert fallback["authority"] == "advisory_navigation"
    assert fallback["navigation_model"] == "single_unified_fallback"
    assert fallback["per_gate_checklist_mapping"] is False
    assert fallback["advisory_only"] is True
    assert fallback["authorizes_write"] is False
    assert fallback["authorizes_pass"] is False
    assert fallback["satisfies_gate"] is False
    assert fallback["source_terminal_disposition"] == (
        "WAIVED/completed_with_exception"
    )
    assert fallback["source_scheduler_eligible"] is False
    assert fallback["source_resume_eligible"] is False
    assert fallback["source_status_may_become_fixed"] is False
    assert fallback["generation_disposition"] == "discarded"
    assert fallback["current_generation_must_not_resume"] is True
    assert fallback["downstream_missing_evidence_repair_forbidden"] is True
    assert fallback["demo_validation_bypass_allowed"] is False
    assert fallback["zero_bypass_happy_path_unchanged"] is True
    assert fallback["repair_target"] == {
        "status": "known",
        "backlog_id": "AC-BYPASS-DIAGNOSTIC",
        "source": "canonical_contract_line_bypass_payload",
        "observer_confirmation_required": False,
    }
    assert fallback["required_sequence"] == [
        "independent_root_repair",
        "independent_qa",
        "ordered_batch_merge",
        "current_head_full_reconcile",
        "fresh_generation_from_scenario_1",
    ]
    assert fallback["durable_evidence"][
        "current_head_full_reconcile_ref"
    ] == "timeline:43"
    for forbidden in (
        "resume_original_contract",
        "return_to_parent",
        "parent_to_resume",
        "retry_source_backlog_close_after_repair",
        "retry_historical_source_backlog_close",
        "repair_downstream_missing_evidence",
        "mark_bypass_source_fixed",
    ):
        assert forbidden in fallback["forbidden_actions"]
    accepted_without_explicit_status = json.loads(json.dumps(terminal_lines))
    accepted_without_explicit_status[-2].pop("status")
    accepted_without_explicit_status[-1].pop("status")
    statusless_terminal = _audited_bypass_terminal_disposition(
        {"completed_lines": accepted_without_explicit_status}
    )
    assert statusless_terminal["row_status"] == "WAIVED"
    assert statusless_terminal["readiness_state"] == "completed_with_exception"
    assert statusless_terminal["terminal_barrier"]["reconcile_line_index"] == 3
    explicit_failed_merge = json.loads(
        json.dumps(accepted_without_explicit_status)
    )
    explicit_failed_merge[-2]["status"] = "failed"
    assert _audited_bypass_terminal_disposition(
        {"completed_lines": explicit_failed_merge}
    ) == {}
    explicit_failed_reconcile = json.loads(
        json.dumps(accepted_without_explicit_status)
    )
    explicit_failed_reconcile[-1]["status"] = "failed"
    assert _audited_bypass_terminal_disposition(
        {"completed_lines": explicit_failed_reconcile}
    ) == {}
    projected_terminal = _project_record_state(
        {
            "backlog_id": record["backlog_id"],
            "contract_execution_id": record["contract_execution_id"],
            "contract_id": record["contract_id"],
            "execution_state_revision": 4,
            "completed_lines": terminal_lines,
            "runtime_guide": {"next_legal_action": None},
        }
    )
    assert "parent_to_resume_contract_execution_id" not in projected_terminal
    assert "return_to_parent" not in projected_terminal
    assert projected_terminal["next_legal_action"] == {}
    assert projected_terminal["bypass_recovery_fallback"] == fallback
    from agent.governance import server

    runtime_resume = server._onboard_runtime_resume_from_current_projection(
        {
            "schema_version": "backlog_contract_chain_current.v1",
            "project_id": record["project_id"],
            "backlog_id": record["backlog_id"],
            "contract_chain_id": "cchain-bypass-min-path",
            "root_contract_execution_id": record[
                "contract_execution_id"
            ],
            "projection_source": "backlog_contract_chain_current",
            **projected_terminal,
        }
    )
    assert runtime_resume["terminal"] is True
    assert runtime_resume["next_legal_action"] == {}
    assert runtime_resume["bypass_recovery_fallback"] == fallback

    server_terminal = server._runtime_current_state_from_record(
        {
            "contract_execution_id": record["contract_execution_id"],
            "contract_id": "bypass_min_path",
            "execution_state_revision": 4,
            "runtime_guide": {
                "next_legal_action": None,
                "readiness_state": "completed_with_exception",
                "terminal_disposition": terminal,
            },
        }
    )
    assert server_terminal["readiness_state"] == "completed_with_exception"
    assert server_terminal["row_status"] == "WAIVED"
    assert server_terminal["source_row_status"] == "WAIVED"
    assert server_terminal["disposition"] == "completed_with_exception"
    assert server_terminal["terminal"] is True
    assert server_terminal["scheduler_eligible"] is False
    assert server_terminal["current_eligible"] is False
    assert server_terminal["close_eligible"] is False
    assert server_terminal["resume_eligible"] is False
    assert server_terminal["next_legal_action"] == {}
    assert server_terminal["terminal_disposition"] == terminal
    assert server_terminal["bypass_recovery_fallback"] == fallback
    forged_lines = json.loads(json.dumps(terminal_lines))
    forged_lines[-1]["payload"]["reconcile_authority"]["record_verified"] = False
    assert _audited_bypass_terminal_disposition(
        {"completed_lines": forged_lines}
    ) == {}
    noncanonical_bypass_lines = json.loads(json.dumps(terminal_lines))
    noncanonical_bypass_lines[0]["payload"]["schema_version"] = (
        "contract_line_bypass.forged"
    )
    assert _audited_bypass_terminal_disposition(
        {"completed_lines": noncanonical_bypass_lines}
    ) == {}

    candidate_commit = "3ee34c35d611c7bde658b1faf7e8d41fb31526fd"
    qa_acceptance_ref = (
        "contract-runtime:cex-mf-parallel-ff19447376e89875a7f1:"
        "completed_lines:10"
    )
    live_qa_line = {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "passed",
        "commit_sha": candidate_commit,
    }
    live_merge_authority = {
        **durable_merge_authority,
        "branch_head": candidate_commit,
        "qa_completed_line_index": 0,
        "qa_acceptance_ref": qa_acceptance_ref,
    }
    live_merge_line = {
        "line_id": "observer_merge",
        "actor_role": "observer",
        "evidence_kind": "merge",
        "status": "accepted",
        "payload": {
            "durable_merge_authority": live_merge_authority,
        },
    }

    def bind_bypass_request_hash(line):
        payload = line["payload"]
        payload["request_hash"] = stable_sha256(
            {
                "bypass_identity": payload["bypass_identity"],
                "line_id": line["line_id"],
                "stage_id": line["stage_id"],
                "execution_state_revision": payload[
                    "execution_state_revision"
                ],
                "diagnostic_backlog_id": payload[
                    "diagnostic_backlog_id"
                ],
                "classification": payload["classification"],
                "reason": payload["reason"],
                "decision": payload["decision"],
                "actor_role": line["actor_role"],
                "evidence_refs": payload["evidence_refs"],
                "continuation_authority": {},
            }
        )

    graph_context_bypass = json.loads(json.dumps(result["written_line"]))
    graph_context_bypass["stage_id"] = "independent_qa"
    graph_context_bypass["line_id"] = "qa_graph_context"
    graph_context_bypass["payload"].update(
        {
            "bypass_identity": (
                "bypass:cex-mf-parallel-ff19447376e89875a7f1:"
                "revision-10:independent_qa:qa_graph_context"
            ),
            "blocked_owner_role": "qa",
            "blocked_evidence_kind": "graph_trace",
            "execution_state_revision": 10,
        }
    )
    bind_bypass_request_hash(graph_context_bypass)
    graph_context_round_authority = {
        "schema_version": (
            "contract_runtime.audit_only_no_pass_bypass_round_authority.v1"
        ),
        "server_derived": True,
        "db_verified": True,
        "no_pass_claim": True,
        "authoritative_pass_synthesized": False,
        "source_shape": (
            "qa_graph_context_bypass_then_independent_verification"
        ),
        "bypass_line_id": "qa_graph_context",
        "bypass_completed_line_index": 0,
        "qa_independent_verification_completed_line_index": 1,
        "candidate_commit_sha": candidate_commit,
    }
    graph_context_round_authority["authority_hash"] = stable_sha256(
        graph_context_round_authority
    )
    graph_context_merge_authority = {
        **durable_merge_authority,
        "branch_head": candidate_commit,
        "qa_completed_line_index": 1,
        "qa_contract_runtime_verified": False,
        "qa_acceptance_ref": graph_context_round_authority["authority_hash"],
        "qa_audit_only_no_pass_authority": graph_context_round_authority,
    }
    graph_context_merge_line = {
        "line_id": "observer_merge",
        "actor_role": "observer",
        "evidence_kind": "merge",
        "payload": {
            "durable_merge_authority": graph_context_merge_authority,
        },
    }
    graph_context_reconcile_line = json.loads(json.dumps(terminal_lines[-1]))
    graph_context_reconcile_line.pop("status")
    graph_context_terminal_lines = [
        graph_context_bypass,
        live_qa_line,
        graph_context_merge_line,
        graph_context_reconcile_line,
    ]
    graph_context_terminal = _audited_bypass_terminal_disposition(
        {"completed_lines": graph_context_terminal_lines}
    )
    assert graph_context_terminal["status"] == "WAIVED"
    assert graph_context_terminal["readiness_state"] == (
        "completed_with_exception"
    )
    assert graph_context_terminal["terminal_barrier"][
        "reconcile_line_index"
    ] == 3

    graph_context_wrong_round = json.loads(
        json.dumps(graph_context_terminal_lines)
    )
    wrong_round_authority = graph_context_wrong_round[2]["payload"][
        "durable_merge_authority"
    ]["qa_audit_only_no_pass_authority"]
    wrong_round_authority["bypass_completed_line_index"] = 99
    wrong_round_authority["authority_hash"] = stable_sha256(
        {
            key: value
            for key, value in wrong_round_authority.items()
            if key != "authority_hash"
        }
    )
    graph_context_wrong_round[2]["payload"]["durable_merge_authority"][
        "qa_acceptance_ref"
    ] = wrong_round_authority["authority_hash"]
    assert _audited_bypass_terminal_disposition(
        {"completed_lines": graph_context_wrong_round}
    ) == {}

    late_merge_bypass = json.loads(json.dumps(result["written_line"]))
    late_merge_bypass["stage_id"] = "observer_integration"
    late_merge_bypass["line_id"] = "observer_merge"
    late_merge_bypass["payload"].update(
        {
            "bypass_identity": (
                "bypass:cex-mf-parallel-ff19447376e89875a7f1:"
                "revision-11:observer_integration:observer_merge"
            ),
            "source_backlog_id": (
                "AC-CONTRACT-RUNTIME-BYPASS-TERMINAL-"
                "NO-SOURCE-RESUME-R1-20260723"
            ),
            "blocked_owner_role": "observer",
            "blocked_evidence_kind": "merge",
            "execution_state_revision": 11,
        }
    )
    bind_bypass_request_hash(late_merge_bypass)
    terminal_after_late_merge_bypass = _audited_bypass_terminal_disposition(
        {
            "completed_lines": [
                live_qa_line,
                late_merge_bypass,
                live_merge_line,
                terminal_lines[-1],
            ]
        }
    )
    assert terminal_after_late_merge_bypass["terminal"] is True
    assert terminal_after_late_merge_bypass["terminal_barrier"] == {
        "bypass_line_index": 1,
        "qa_line_index": 0,
        "merge_line_index": 2,
        "reconcile_line_index": 3,
        "reconcile_source_ref": "timeline:43",
        "active_snapshot_id": "full-aaaaaaaa-current",
        "active_snapshot_commit": merged_commit,
        "canonical_head_commit": merged_commit,
        "reconcile_provenance_target_commit": merged_commit,
        "ordering_policy": "stage_aware_forward_only",
    }
    assert terminal_after_late_merge_bypass[
        "bypass_recovery_fallback"
    ]["required_sequence"] == fallback["required_sequence"]

    late_reconcile_bypass = json.loads(json.dumps(result["written_line"]))
    late_reconcile_bypass["stage_id"] = "observer_integration"
    late_reconcile_bypass["line_id"] = "observer_reconcile"
    late_reconcile_bypass["payload"].update(
        {
            "bypass_identity": (
                "bypass:cex-mf-parallel-ff19447376e89875a7f1:"
                "revision-12:observer_integration:observer_reconcile"
            ),
            "source_backlog_id": (
                "AC-CONTRACT-RUNTIME-BYPASS-TERMINAL-"
                "NO-SOURCE-RESUME-R1-20260723"
            ),
            "blocked_owner_role": "observer",
            "blocked_evidence_kind": "reconcile",
            "execution_state_revision": 12,
        }
    )
    bind_bypass_request_hash(late_reconcile_bypass)
    terminal_after_late_reconcile_bypass = (
        _audited_bypass_terminal_disposition(
            {
                "completed_lines": [
                    live_qa_line,
                    live_merge_line,
                    late_reconcile_bypass,
                    terminal_lines[-1],
                ]
            }
        )
    )
    assert terminal_after_late_reconcile_bypass["terminal"] is True
    assert terminal_after_late_reconcile_bypass["terminal_barrier"] == {
        "bypass_line_index": 2,
        "qa_line_index": 0,
        "merge_line_index": 1,
        "reconcile_line_index": 3,
        "reconcile_source_ref": "timeline:43",
        "active_snapshot_id": "full-aaaaaaaa-current",
        "active_snapshot_commit": merged_commit,
        "canonical_head_commit": merged_commit,
        "reconcile_provenance_target_commit": merged_commit,
        "ordering_policy": "stage_aware_forward_only",
    }
    assert terminal_after_late_reconcile_bypass[
        "bypass_recovery_fallback"
    ]["required_sequence"] == fallback["required_sequence"]
    assert _audited_bypass_terminal_disposition(
        {
            "completed_lines": [
                live_qa_line,
                live_merge_line,
                late_reconcile_bypass,
                live_merge_line,
                terminal_lines[-1],
            ]
        }
    ) == {}

    late_close_ready_bypass = json.loads(json.dumps(result["written_line"]))
    late_close_ready_bypass["stage_id"] = "observer_integration"
    late_close_ready_bypass["line_id"] = "observer_close_ready"
    late_close_ready_bypass["payload"].update(
        {
            "bypass_identity": (
                "bypass:cex-mf-parallel-ff19447376e89875a7f1:"
                "revision-13:observer_integration:observer_close_ready"
            ),
            "source_backlog_id": (
                "AC-CONTRACT-RUNTIME-BYPASS-TERMINAL-"
                "NO-SOURCE-RESUME-R1-20260723"
            ),
            "blocked_owner_role": "observer",
            "blocked_evidence_kind": "close_ready",
            "execution_state_revision": 13,
        }
    )
    bind_bypass_request_hash(late_close_ready_bypass)
    assert _audited_bypass_terminal_disposition(
        {
            "completed_lines": [
                live_qa_line,
                live_merge_line,
                late_close_ready_bypass,
            ]
        }
    ) == {}
    terminal_after_late_close_ready_bypass = (
        _audited_bypass_terminal_disposition(
            {
                "completed_lines": [
                    live_qa_line,
                    live_merge_line,
                    terminal_lines[-1],
                    late_close_ready_bypass,
                ]
            }
        )
    )
    assert terminal_after_late_close_ready_bypass["terminal_barrier"] == {
        "bypass_line_index": 3,
        "qa_line_index": 0,
        "merge_line_index": 1,
        "reconcile_line_index": 2,
        "reconcile_source_ref": "timeline:43",
        "active_snapshot_id": "full-aaaaaaaa-current",
        "active_snapshot_commit": merged_commit,
        "canonical_head_commit": merged_commit,
        "reconcile_provenance_target_commit": merged_commit,
        "ordering_policy": "stage_aware_forward_only",
    }
    assert terminal_after_late_close_ready_bypass[
        "bypass_recovery_fallback"
    ]["required_sequence"] == fallback["required_sequence"]
    terminal_after_multiple_late_bypasses = (
        _audited_bypass_terminal_disposition(
            {
                "completed_lines": [
                    live_qa_line,
                    late_merge_bypass,
                    live_merge_line,
                    late_reconcile_bypass,
                    terminal_lines[-1],
                    late_close_ready_bypass,
                ]
            }
        )
    )
    assert terminal_after_multiple_late_bypasses["terminal_barrier"] == {
        "bypass_line_index": 5,
        "qa_line_index": 0,
        "merge_line_index": 2,
        "reconcile_line_index": 4,
        "reconcile_source_ref": "timeline:43",
        "active_snapshot_id": "full-aaaaaaaa-current",
        "active_snapshot_commit": merged_commit,
        "canonical_head_commit": merged_commit,
        "reconcile_provenance_target_commit": merged_commit,
        "ordering_policy": "stage_aware_forward_only",
    }

    cross_candidate_merge = json.loads(json.dumps(live_merge_line))
    cross_candidate_merge["payload"]["durable_merge_authority"][
        "branch_head"
    ] = "c" * 40
    assert _audited_bypass_terminal_disposition(
        {
            "completed_lines": [
                live_qa_line,
                late_merge_bypass,
                cross_candidate_merge,
                terminal_lines[-1],
            ]
        }
    ) == {}
    stale_round_merge = json.loads(json.dumps(live_merge_line))
    stale_round_merge["payload"]["durable_merge_authority"][
        "qa_completed_line_index"
    ] = 99
    assert _audited_bypass_terminal_disposition(
        {
            "completed_lines": [
                live_qa_line,
                late_merge_bypass,
                stale_round_merge,
                terminal_lines[-1],
            ]
        }
    ) == {}

    retry = runtime.bypass_current_line(
        record["contract_execution_id"], request, actor_role="observer"
    )
    assert retry["ok"] is True
    assert retry["idempotent"] is True
    assert retry["record"]["execution_state_revision"] == 2
    assert len(retry["record"]["completed_lines"]) == 1

    conflict = runtime.bypass_current_line(
        record["contract_execution_id"],
        {**request, "reason": "changed after the identity was consumed"},
        actor_role="observer",
    )
    assert conflict["ok"] is False
    assert conflict["decision"]["errors"] == ["bypass_identity_conflict"]

    stale = runtime.bypass_current_line(
        record["contract_execution_id"],
        {**request, "bypass_identity": "bypass:cex:stale"},
        actor_role="observer",
    )
    assert stale["ok"] is False
    assert stale["decision"]["errors"] == ["execution_state_revision mismatch"]


@pytest.mark.parametrize(
    "contract_id",
    [
        "direct_main.v1",
        "mf_parallel.v2",
        "mf_batch_parallel.v1",
        "mf_batch_parallel.restart.v1",
    ],
)
def test_terminal_bypass_recovery_fallback_is_lane_neutral_and_fail_closed(
    contract_id,
):
    terminal = {
        "schema_version": "contract_runtime.audited_bypass_terminal.v1",
        "status": "WAIVED",
        "readiness_state": "completed_with_exception",
        "terminal": True,
        "scheduler_eligible": False,
        "resume_eligible": False,
        "no_pass_claim": True,
        "diagnostic_backlog_id": "",
        "terminal_barrier": {
            "bypass_line_index": 1,
            "qa_line_index": 2,
            "merge_line_index": 3,
            "reconcile_line_index": 4,
            "reconcile_source_ref": "timeline:44",
        },
    }
    fallback = _audited_bypass_recovery_fallback(
        {
            "backlog_id": "AC-BYPASS-SOURCE",
            "contract_execution_id": "cex-bypass-source",
            "contract_id": contract_id,
        },
        terminal,
    )

    assert fallback["status"] == "unknown"
    assert fallback["source_contract_id"] == contract_id
    assert fallback["repair_target"] == {
        "status": "unknown",
        "backlog_id": "",
        "source": "durable_server_evidence_insufficient",
        "observer_confirmation_required": True,
    }
    assert fallback["observer_confirmation_required"] is True
    assert fallback["required_sequence"] == [
        "independent_root_repair",
        "independent_qa",
        "ordered_batch_merge",
        "current_head_full_reconcile",
        "fresh_generation_from_scenario_1",
    ]
    invalid_barrier = json.loads(json.dumps(terminal))
    invalid_barrier["terminal_barrier"]["reconcile_source_ref"] = ""
    assert _audited_bypass_recovery_fallback(
        {"contract_id": contract_id},
        invalid_barrier,
    ) == {}
    historical_terminal = json.loads(json.dumps(terminal))
    historical_terminal["schema_version"] = (
        "contract_runtime.historical_audited_bypass_supersession.v1"
    )
    assert _audited_bypass_recovery_fallback(
        {"contract_id": contract_id},
        historical_terminal,
    ) == {}


@pytest.mark.parametrize(
    (
        "diagnostic_backlog_id",
        "source_backlog_id",
        "contract_execution_id",
        "stage_id",
        "bypass_identity_stage_id",
        "line_id",
        "execution_state_revision",
    ),
    [
        (
            "AC-CONTRACT-LINE-BYPASS-E0A8A5698E8A27D6",
            (
                "AC-ACTIVITY-PLAYBACK-PER-RESOURCE-CACHE-"
                "SINGLE-FLIGHT-R2-20260722"
            ),
            "cex-mf-parallel-05dca57d0f222edea88d",
            "observer_integration",
            "observer_integration",
            "observer_close_ready",
            29,
        ),
        (
            "AC-CONTRACT-LINE-BYPASS-0FAA24E164C04608",
            "AC-CONTRACT-LINE-BYPASS-E0A8A5698E8A27D6",
            "cex-mf-parallel-950f36cb45ff712a44d6",
            "observer_integration",
            "observer_integration",
            "observer_reconcile",
            13,
        ),
        (
            "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820",
            "AC-CONTRACT-LINE-BYPASS-E0A8A5698E8A27D6",
            "cex-mf-parallel-950f36cb45ff712a44d6",
            "observer_integration",
            "observer_integration",
            "observer_close_ready",
            14,
        ),
        (
            "AC-CONTRACT-LINE-BYPASS-F711E000035BF613",
            "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820",
            "cex-mf-parallel-1e5fbd813bda945d383d",
            "observer_integration",
            "observer_merge",
            "observer_merge",
            18,
        ),
        (
            "AC-CONTRACT-LINE-BYPASS-3C0FD2ED41F3AC87",
            "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820",
            "cex-mf-parallel-1e5fbd813bda945d383d",
            "qa_graph_context",
            "qa_graph_context",
            "qa_graph_context",
            10,
        ),
        (
            "AC-CONTRACT-LINE-BYPASS-7D856ECDAB165BE2",
            "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820",
            "cex-mf-parallel-1e5fbd813bda945d383d",
            "observer_integration",
            "observer_integration",
            "observer_reconcile",
            19,
        ),
    ],
)
def test_historical_operator_supersession_is_exact_and_no_pass(
    diagnostic_backlog_id,
    source_backlog_id,
    contract_execution_id,
    stage_id,
    bypass_identity_stage_id,
    line_id,
    execution_state_revision,
):
    bypass_identity = (
        f"bypass:{contract_execution_id}:revision-{execution_state_revision}:"
        f"{bypass_identity_stage_id}:{line_id}"
    )
    completed_lines = [
        {
            "actor_role": "observer",
            "stage_id": stage_id,
            "line_id": line_id,
            "evidence_kind": "contract_line_bypass",
            "status": "waived",
            "no_pass_claim": True,
            "payload": {
                "schema_version": "contract_line_bypass.v1",
                "source_backlog_id": source_backlog_id,
                "diagnostic_backlog_id": diagnostic_backlog_id,
                "bypass_identity": bypass_identity,
                "execution_state_revision": execution_state_revision,
                "disposition": "proceeded_with_exception",
                "no_pass_claim": True,
            },
        }
    ]
    immutable_evidence = json.loads(json.dumps(completed_lines))
    record = {
        "backlog_id": source_backlog_id,
        "contract_execution_id": contract_execution_id,
        "contract_id": "mf_parallel.v2",
        "execution_state_revision": execution_state_revision,
        "completed_lines": completed_lines,
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": stage_id,
                "line_id": line_id,
            }
        },
    }

    terminal = _audited_bypass_terminal_disposition(record)

    assert completed_lines == immutable_evidence
    assert terminal["schema_version"] == (
        "contract_runtime.historical_audited_bypass_supersession.v1"
    )
    assert terminal["status"] == "WAIVED"
    assert terminal["readiness_state"] == "completed_with_exception"
    assert terminal["terminal_basis"] == "historical_operator_supersession"
    assert terminal["no_pass_claim"] is True
    for field in (
        "scheduler_eligible",
        "current_eligible",
        "close_eligible",
        "resume_eligible",
    ):
        assert terminal[field] is False
    provenance = terminal["historical_operator_supersession"]
    assert provenance["operator_authorized"] is True
    assert provenance["immutable_source_evidence"] is True
    assert provenance["source_evidence_mutated"] is False
    assert provenance["current_generation_barrier_satisfied"] is False
    assert provenance["authoritative_pass_synthesized"] is False
    assert provenance["qa_pass_claimed"] is False
    assert provenance["merge_pass_claimed"] is False
    assert provenance["reconcile_pass_claimed"] is False
    assert provenance["matched_bindings"] == [
        {
            "diagnostic_backlog_id": diagnostic_backlog_id,
            "source_backlog_id": source_backlog_id,
            "source_contract_execution_id": contract_execution_id,
            "stage_id": stage_id,
            "line_id": line_id,
            "completed_line_index": 0,
            "bypass_identity": bypass_identity,
        }
    ]
    projected = _project_record_state(record)
    assert projected["current_contract_execution_id"] == ""
    assert projected["readiness_state"] == "completed_with_exception"
    assert projected["next_legal_action"] == {}
    assert projected["terminal_disposition"] == terminal


def test_historical_operator_supersession_rejects_unrelated_execution():
    source_backlog_id = (
        "AC-META-CONTRACT-PRINCIPAL-QA-TOKEN-FALSE-ROLE-R1-20260722"
    )
    contract_execution_id = "cex-mf-parallel-e08d916b06ecab610295"
    line = {
        "actor_role": "observer",
        "stage_id": "observer_integration",
        "line_id": "observer_reconcile",
        "evidence_kind": "contract_line_bypass",
        "status": "waived",
        "no_pass_claim": True,
        "payload": {
            "schema_version": "contract_line_bypass.v1",
            "source_backlog_id": source_backlog_id,
            "diagnostic_backlog_id": "AC-CONTRACT-LINE-BYPASS-1DBCE496EE79FCC7",
            "bypass_identity": (
                "bypass:cex-mf-parallel-e08d916b06ecab610295:"
                "revision-13:observer_integration:observer_reconcile"
            ),
            "execution_state_revision": 13,
            "disposition": "proceeded_with_exception",
            "no_pass_claim": True,
        },
    }
    record = {
        "backlog_id": source_backlog_id,
        "contract_execution_id": contract_execution_id,
        "completed_lines": [line],
    }

    assert _audited_bypass_terminal_disposition(record) == {}
    line["payload"]["diagnostic_backlog_id"] = (
        "AC-CONTRACT-LINE-BYPASS-7D856ECDAB165BE2"
    )
    assert _audited_bypass_terminal_disposition(record) == {}


def test_runtime_current_guide_exposes_strict_line_bypass_row_binding(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="bypass_guide_min_path",
        stages=[
            {
                "stage_id": "bootstrap",
                "lines": [
                    {
                        "line_id": "read_context",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "read_receipt",
                    }
                ],
            }
        ],
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "bypass_guide_min_path",
        project_id="aming-claw",
        backlog_id="AC-BYPASS-GUIDE-MIN-PATH",
        actor_role="observer",
        contract_execution_id="cex-bypass-guide-min-path",
    )

    guide = runtime.current_guide(
        record["contract_execution_id"],
        actor_role="observer",
    )

    bypass = guide["line_bypass_guidance"]
    assert bypass["status"] == "available_for_current_line"
    assert bypass["endpoint"] == (
        "/api/projects/aming-claw/contract-runtime/"
        "cex-bypass-guide-min-path/line-bypasses"
    )
    assert bypass["current_line_binding"] == {
        "source_backlog_id": "AC-BYPASS-GUIDE-MIN-PATH",
        "contract_execution_id": "cex-bypass-guide-min-path",
        "line_id": "read_context",
        "stage_id": "bootstrap",
        "line_instance_id": "",
        "execution_state_revision": 1,
        "runtime_guide_hash": guide["runtime_guide_hash"],
    }
    identity = bypass["bypass_identity"]["recommended_value"]
    assert identity == (
        "bypass:cex-bypass-guide-min-path:revision-1:"
        "bootstrap:read_context"
    )
    expected_diagnostic_id = (
        "AC-CONTRACT-LINE-BYPASS-"
        + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16].upper()
    )
    create = bypass["diagnostic_row_binding"]["create_new"]
    assert create["deterministic_id_example"] == expected_diagnostic_id
    assert "diagnostic_backlog_id" not in bypass["create_new_copy_safe_body"]
    reuse = bypass["diagnostic_row_binding"]["reuse_existing_open"]
    assert reuse["required_status"] == "OPEN"
    assert reuse["required_exact_binding"] == {
        "source_backlog_id": "AC-BYPASS-GUIDE-MIN-PATH",
        "contract_execution_id": "cex-bypass-guide-min-path",
        "line_id": "read_context",
    }
    generic = bypass["diagnostic_row_binding"]["generic_root_cause_row"]
    assert generic["allowed_as"] == "evidence_ref_only"
    assert generic["allowed_as_diagnostic_backlog_id"] is False
    assert bypass["invariants"]["no_pass_claim"] is True
    assert bypass["invariants"]["source_generation_terminal_after_barrier"] is True
    assert bypass["invariants"]["pre_barrier_forward_integration_required"] is True
    assert bypass["invariants"]["post_barrier_scheduler_eligible"] is False
    assert bypass["invariants"]["post_barrier_current_eligible"] is False
    assert bypass["invariants"]["post_barrier_close_eligible"] is False
    assert bypass["invariants"]["post_barrier_resume_eligible"] is False
    assert bypass["invariants"]["source_backlog_mutated_by_bypass"] is False
    assert bypass["invariants"]["repair_requires_separate_backlog_row"] is True
    assert bypass["invariants"]["strict_line_validation_unchanged"] is True
    assert bypass["invariants"]["bypass_acceptance_logic_unchanged"] is True


def test_runtime_write_gate_rejects_negative_cases(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )

    stale_revision = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
    )
    stale_revision["execution_state_revision"] = 0
    assert runtime.submit_line_write(
        record["contract_execution_id"], stale_revision
    )["decision"]["errors"] == ["execution_state_revision mismatch"]

    wrong_next_action = _write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_verdict",
        evidence_kind="qa_verification",
    )
    wrong_next_errors = runtime.submit_line_write(
        record["contract_execution_id"], wrong_next_action
    )["decision"]["errors"]
    assert any("write does not match next legal action" in item for item in wrong_next_errors)

    wrong_role = _write_from(
        record,
        actor_role="qa",
        stage_id="bootstrap",
        line_id="read_context",
    )
    wrong_role_errors = runtime.submit_line_write(
        record["contract_execution_id"], wrong_role
    )["decision"]["errors"]
    assert any("cannot write line" in item for item in wrong_role_errors)

    wrong_evidence_kind = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
        evidence_kind="qa_verification",
    )
    assert "evidence_kind mismatch" in runtime.submit_line_write(
        record["contract_execution_id"], wrong_evidence_kind
    )["decision"]["errors"]

    forged_role = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
    )
    forged_result = runtime.submit_line_write(
        record["contract_execution_id"],
        forged_role,
        actor_role="mf_sub",
    )
    assert forged_result["ok"] is False
    assert any("cannot write line" in item for item in forged_result["decision"]["errors"])


def test_sqlite_contract_execution_store_persists_rows_and_cas(tmp_path):
    _write_minimal_contract(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = SQLiteContractExecutionStore(conn)
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=store,
    )

    created = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        contract_execution_id="cex-sqlite-min-path",
        actor_role="observer",
        route_token_ref="rtok-sqlite",
    )
    row = conn.execute(
        "SELECT contract_execution_id, execution_state_revision, record_json "
        "FROM contract_runtime_executions WHERE contract_execution_id = ?",
        ("cex-sqlite-min-path",),
    ).fetchone()
    assert dict(row)["contract_execution_id"] == "cex-sqlite-min-path"
    assert row["execution_state_revision"] == 1
    assert json.loads(row["record_json"])["route_token_ref"] == "rtok-sqlite"

    read_back = SQLiteContractExecutionStore(conn).get("cex-sqlite-min-path")
    assert read_back["runtime_guide"]["next_legal_action"]["line_id"] == "read_context"

    write_result = runtime.submit_line_write(
        "cex-sqlite-min-path",
        _write_from(
            created,
            actor_role="observer",
            stage_id="bootstrap",
            line_id="read_context",
        ),
    )
    assert write_result["ok"] is True
    assert write_result["record"]["execution_state_revision"] == 2

    stale = dict(write_result["record"])
    stale["execution_state_revision"] = 3
    with pytest.raises(ContractRuntimeError, match="stale execution_state_revision"):
        store.update("cex-sqlite-min-path", stale, expected_revision=1)


def test_contract_chain_mapping_schema_idempotent_and_rebuilds_projection(tmp_path):
    _write_minimal_contract(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = SQLiteContractExecutionStore(conn)
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=store,
    )

    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
        contract_execution_id="cex-map-root",
        actor_role="observer",
        route_token_ref="rtok-map-root",
    )
    child = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
        contract_execution_id="cex-map-child",
        actor_role="observer",
        route_token_ref="rtok-map-child",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    first = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
    )
    assert first["contract_chain_id"] == root["contract_chain_id"]
    assert first["root_contract_execution_id"] == root["contract_execution_id"]
    assert first["active_child_contract_execution_id"] == child["contract_execution_id"]
    assert first["current_contract_execution_id"] == child["contract_execution_id"]
    assert first["projection_watermark"] >= 2
    first_hash = first["projection_hash"]
    first_watermark = first["projection_watermark"]

    conn.execute("DELETE FROM backlog_contract_chain_current")
    conn.execute("DELETE FROM contract_chain_edges")
    conn.execute("DELETE FROM backlog_contract_chain_bindings")
    rebuilt = rebuild_backlog_contract_chain_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
    )
    rebuilt_again = rebuild_backlog_contract_chain_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
    )
    assert rebuilt["current_contract_execution_id"] == child["contract_execution_id"]
    assert rebuilt["projection_hash"] == first_hash
    assert rebuilt["projection_watermark"] > first_watermark
    assert rebuilt_again["projection_hash"] == rebuilt["projection_hash"]
    binding_count = conn.execute(
        "SELECT COUNT(*) FROM backlog_contract_chain_bindings"
    ).fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM contract_chain_edges").fetchone()[0]
    upsert_contract_chain_successor_binding(
        conn,
        parent_record=root,
        child_record=child,
        edge_kind="test_child",
        binding_kind="successor_current",
    )
    upsert_contract_chain_successor_binding(
        conn,
        parent_record=root,
        child_record=child,
        edge_kind="test_child",
        binding_kind="successor_current",
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM backlog_contract_chain_bindings"
    ).fetchone()[0] == binding_count
    assert conn.execute("SELECT COUNT(*) FROM contract_chain_edges").fetchone()[0] == (
        edge_count + 1
    )


def test_completed_recovery_supersedes_only_named_stale_predecessor(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    backlog_id = "AC-CHAIN-COMPLETED-RECOVERY-CURRENT"
    root = _start_completed_chain_projection_root(
        runtime,
        backlog_id=backlog_id,
        contract_execution_id="cex-a-recovery-root",
    )
    stale = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id="cex-b-stale-predecessor",
        actor_role="observer",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )
    recovery = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id="cex-z-completed-recovery",
        actor_role="observer",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
        metadata={
            "recovery_policy": "start_new_execution",
            "stale_contract_execution_id": stale["contract_execution_id"],
        },
    )
    completed_recovery = runtime.submit_line_write(
        recovery["contract_execution_id"],
        _write_from(
            recovery,
            actor_role="observer",
            stage_id="observer_prefill",
            line_id="observer_prefill_child_contracts",
        ),
        actor_role="observer",
    )
    assert completed_recovery["ok"] is True

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )

    assert current["current_contract_execution_id"] == recovery[
        "contract_execution_id"
    ]
    assert current["readiness_state"] == "contract_complete"
    assert current["next_legal_action"] == {}
    assert set(current["active_chain"]["execution_ids"]) == {
        root["contract_execution_id"],
        stale["contract_execution_id"],
        recovery["contract_execution_id"],
    }
    assert any(
        ref.startswith(
            f"contract_runtime:{stale['contract_execution_id']}:revision:"
        )
        for ref in current["source_refs"]
    )
    assert runtime.store.get(stale["contract_execution_id"])["runtime_guide"][
        "next_legal_action"
    ] is not None

    unrelated = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id="cex-zz-unrelated-incomplete",
        actor_role="observer",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )
    current_with_unrelated = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )
    assert current_with_unrelated["current_contract_execution_id"] == unrelated[
        "contract_execution_id"
    ]
    assert current_with_unrelated["readiness_state"] == "contract_active"


@pytest.mark.parametrize(
    "recovery_lineage_shape",
    ("frozen_target", "legacy_live_source"),
)
def test_completed_recovery_terminalizes_history_and_requires_fresh_generation(
    recovery_lineage_shape,
):
    project_id = "aming-claw"
    backlog_id = (
        "AC-CONTRACT-RUNTIME-OBSERVER-MERGE-QA-BASELINE-COMMIT-IDENTITY-"
        "R1-20260726"
    )
    root_id = "onboard-service-60d12cacdde638db2df5"
    parent_id = "cex-mf-parallel-2f6f70dc03fb507fdf52"
    child_id = "cex-repair-567f422fe80f9072"
    chain_id = "cchain-60d12cacdde638db2df5"
    candidate_commit = "439710a9a9ae47f09ec2be0e3f1dac38308b477b"
    merged_commit = "488700771df207eb346229a9fe61dde1876f634f"
    source_guide_hash = (
        "sha256:736098d20d764c20f317e9e790659ea0bd2db2475a762567a759c78449732de1"
    )
    target = {
        "schema_version": "contract_runtime.same_row_recovery_target.v1",
        "source_of_authority": (
            "stale_contract_runtime.runtime_guide.next_legal_action"
        ),
        "source_contract_execution_id": parent_id,
        "source_execution_state_revision": 12,
        "source_runtime_guide_hash": source_guide_hash,
        "stage_id": "observer_integration",
        "line_id": "observer_merge",
        "action": "record_merge",
        "evidence_kind": "merge",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "historical_parent_immutable": True,
        "authoritative_pass_synthesized": False,
    }
    target["target_hash"] = stable_sha256(target)
    parent = {
        "schema_version": "contract_runtime_execution_record.v1",
        "project_id": project_id,
        "backlog_id": backlog_id,
        "contract_execution_id": parent_id,
        "parent_contract_execution_id": root_id,
        "root_contract_execution_id": root_id,
        "contract_chain_id": chain_id,
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev5",
        "execution_state_revision": 12,
        "completed_lines": [
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "passed",
                "commit_sha": candidate_commit,
            }
        ],
        "runtime_guide": {
            "runtime_guide_hash": source_guide_hash,
            "next_legal_action": {
                "stage_id": "observer_integration",
                "line_id": "observer_merge",
                "evidence_kind": "merge",
                "owner_role": "observer",
                "allowed_writer_roles": ["observer"],
            },
        },
    }
    qa_provenance = {
        "schema_version": "qa_evidence_provenance.v1",
        "server_derived": True,
        "evidence_owner_role": "qa",
        "observer_impersonation": False,
        "authenticated_qa_binding": {
            "schema_version": "contract_runtime.authenticated_qa_binding.v1",
            "server_derived": True,
            "qa_principal": "qa:merge-scope-qa-r3-20260726",
            "qa_session_id": "ses-1785106230998-979251",
            "independent_verification_session_matched": True,
        },
        "completion_status_gate": {
            "schema_version": "contract_runtime.qa_completion_status_gate.v1",
            "server_derived": True,
            "normalized_status": "passed",
            "top_level_status_present": True,
            "top_level_status_passing": True,
        },
    }
    durable_merge = {
        "schema_version": "contract_runtime.observer_merge_durable_authority.v1",
        "source": "parallel_branch_merge_queue+task_timeline_merge",
        "server_derived": True,
        "db_verified": True,
        "project_id": project_id,
        "backlog_id": backlog_id,
        "runtime_context_id": "mfrctx-3c4480963474f205",
        "task_id": "observer-merge-authority-repair-worker-3",
        "parent_task_id": child_id,
        "branch_head": candidate_commit,
        "merge_commit": merged_commit,
        "target_head_after_merge": merged_commit,
        "merge_gate_passed": True,
        "merge_queue_id": "mq-f1a3902f300f821cb131",
        "queue_item_id": (
            "mq-f1a3902f300f821cb131:"
            "observer-merge-authority-repair-worker-3"
        ),
        "queue_item_status": "merged",
        "qa_completed_line_index": 0,
        "qa_acceptance_ref": f"contract_runtime:{child_id}:revision:12",
        "qa_contract_runtime_verified": True,
        "no_pass_claim": False,
        "overall_release_pass_claimed": False,
        "authoritative_pass_synthesized": False,
        "close_satisfying": True,
        "timeline_event_refs": ["timeline:18299"],
        "merge_event_ref": "timeline:18299",
        "merge_event_id": 18299,
        "merge_event_created_at": "2026-07-26T23:11:33Z",
    }
    current_full = {
        "schema_version": "graph_snapshot_store.current_full_reconcile_state.v1",
        "source": "graph_snapshot_store.current_full_reconcile_state",
        "server_derived": True,
        "db_verified": True,
        "live_verified": True,
        "canonical_head_verified": True,
        "active_snapshot_verified": True,
        "active_snapshot_matches_canonical_head": True,
        "graph_reconciled": True,
        "provenance_verified": True,
        "provenance_scope_verified": True,
        "durable_order_verified": True,
        "reconcile_snapshot_verified": True,
        "contract_execution_scope_verified": True,
        "task_scope_verified": True,
        "runtime_context_scope_verified": True,
        "parent_task_scope_verified": True,
        "merge_queue_scope_verified": True,
        "current_full_reconcile": True,
        "strategy": "current_full_reconcile",
        "active_snapshot_status": "active",
        "active_snapshot_id": (
            "full-4887007-observer-merge-external-tuple-postmerge"
        ),
        "active_snapshot_commit": merged_commit,
        "canonical_head_commit": merged_commit,
        "current_canonical_commit_sha": merged_commit,
        "reconciled_commit_sha": merged_commit,
        "reconcile_provenance_target_commit": merged_commit,
        "merge_source_ref": "timeline:18299",
        "merged_commit_sha": merged_commit,
        "reconcile_source_ref": "timeline:18300",
        "project_id": project_id,
        "backlog_id": backlog_id,
        "contract_execution_id": child_id,
    }
    current_full["authority_hash"] = stable_sha256(current_full)
    reconcile = {
        "schema_version": "contract_runtime.observer_reconcile_record_authority.v1",
        "server_derived": True,
        "record_verified": True,
        "merge_projection_verified": True,
        "dispatch_lineage_verified": True,
        "reconcile_event_recorded": True,
        "merge_source_ref": "timeline:18299",
        "merged_commit_sha": merged_commit,
        "reconcile_source_ref": "timeline:18300",
        "current_full_reconcile_activation_verified": True,
        "terminal_current_full_reconcile_authority": current_full,
    }
    reconcile["authority_hash"] = stable_sha256(reconcile)
    recovery_metadata = {
        "facade": "contract_runtime_recovery",
        "recovery_policy": "start_new_execution",
        "stale_contract_execution_id": parent_id,
        "recovery_contract_execution_id": child_id,
        "historical_evidence_replayed": False,
        "authoritative_pass_synthesized": False,
        "current_repair_target": target,
    }
    recovery_backlog_lineage = {
        "recovery_policy": "start_new_execution",
        "stale_contract_execution_id": parent_id,
        "recovery_contract_execution_id": child_id,
        "current_repair_target": target,
    }
    if recovery_lineage_shape == "legacy_live_source":
        recovery_metadata = {
            "facade": "mf_parallel",
            "source_contract_execution_id": parent_id,
            "source_failed_qa_event_ref": "timeline:18278",
            "repair_run_id": "repair-567f422fe80f9072",
            "immutable_checkpoint_commit": (
                "9e07577946d46b4bd9c1285227b8dc09b8f11fe3"
            ),
            "immutable_checkpoint_reconcile_ref": "timeline:18277",
            "historical_evidence_replayed": False,
            "authoritative_pass_synthesized": False,
        }
        recovery_backlog_lineage = {}
        durable_merge["parent_task_id"] = "repair-567f422fe80f9072"
    child = {
        **parent,
        "contract_execution_id": child_id,
        "execution_state_revision": 15,
        "completed_lines": [
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "passed",
                "commit_sha": candidate_commit,
                "qa_evidence_provenance": qa_provenance,
                "payload": {"status": "passed", "verdict": "passed"},
            },
            {
                "stage_id": "observer_integration",
                "line_id": "observer_merge",
                "actor_role": "observer",
                "evidence_kind": "merge",
                "status": "passed",
                "commit_sha": merged_commit,
                "payload": {"durable_merge_authority": durable_merge},
            },
            {
                "stage_id": "observer_integration",
                "line_id": "observer_reconcile",
                "actor_role": "observer",
                "evidence_kind": "reconcile",
                "status": "passed",
                "commit_sha": merged_commit,
                "payload": {"reconcile_authority": reconcile},
            },
            {
                "stage_id": "observer_integration",
                "line_id": "observer_close_ready",
                "actor_role": "observer",
                "evidence_kind": "close_ready",
                "status": "passed",
                "commit_sha": merged_commit,
            },
        ],
        "runtime_guide": {
            "runtime_guide_hash": (
                "sha256:d4f992200764092cba3ad6a4d4d5a2dd1dcbca836b39ad44712e4d72ea9cf96b"
            ),
            "next_legal_action": None,
        },
        "metadata": recovery_metadata,
        "backlog_lineage": recovery_backlog_lineage,
    }
    immutable_parent_lines = json.loads(json.dumps(parent["completed_lines"]))

    projected = _project_current_contract_state(
        [parent, child],
        root_record=parent,
        row_times={parent_id: "2026-07-27T00:00:00Z", child_id: "2026-07-27T00:02:00Z"},
    )

    assert projected["current_contract_execution_id"] == child_id
    assert projected["active_child_contract_execution_id"] == ""
    assert projected["readiness_state"] == "contract_complete"
    assert projected["next_legal_action"] == {}
    assert projected["scheduler_eligible"] is False
    assert projected["resume_eligible"] is False
    barrier = projected["completed_repair_fresh_generation_barrier"]
    assert barrier["status"] == "repair_complete_fresh_generation_required"
    assert barrier["repair_child_contract_execution_id"] == child_id
    assert barrier["historical_source_contract_execution_id"] == parent_id
    assert barrier["reconcile_source_ref"] == "timeline:18300"
    assert barrier["historical_parent_mutated"] is False
    assert barrier["historical_missing_evidence_backfilled"] is False
    assert barrier["historical_source_scheduler_eligible"] is False
    assert barrier["historical_source_resume_eligible"] is False
    assert barrier["authoritative_pass_synthesized"] is False
    assert barrier["fresh_generation_required"] is True
    assert barrier["fresh_generation_start"] == "scenario_1"
    assert barrier["diagnostic_fixed_eligible"] is False
    assert barrier["advisory_only"] is True
    assert barrier["authorizes_write"] is False
    assert barrier["satisfies_gate"] is False
    assert barrier["required_sequence"] == [
        "fresh_generation_from_scenario_1"
    ]
    assert {
        "resume_original_contract",
        "return_to_parent",
        "parent_to_resume",
        "retry_source_backlog_close_after_repair",
    }.issubset(set(barrier["forbidden_actions"]))
    assert barrier["target_source"] == (
        "frozen_recovery_target"
        if recovery_lineage_shape == "frozen_target"
        else "legacy_persisted_parent_guide"
    )
    assert parent["completed_lines"] == immutable_parent_lines
    assert not any(
        line.get("line_id") == "qa_graph_context"
        for line in parent["completed_lines"]
    )

    invalid_children = []
    failed_qa = json.loads(json.dumps(child))
    failed_qa["completed_lines"][0]["status"] = "failed"
    invalid_children.append(failed_qa)
    wrong_chain = json.loads(json.dumps(child))
    wrong_chain["contract_chain_id"] = "cchain-unrelated"
    invalid_children.append(wrong_chain)
    missing_merge_proof = json.loads(json.dumps(child))
    missing_merge_proof["completed_lines"][1]["payload"][
        "durable_merge_authority"
    ]["db_verified"] = False
    invalid_children.append(missing_merge_proof)
    stale_reconcile = json.loads(json.dumps(child))
    stale_reconcile["completed_lines"][2]["payload"]["reconcile_authority"][
        "terminal_current_full_reconcile_authority"
    ]["active_snapshot_commit"] = "d" * 40
    invalid_children.append(stale_reconcile)
    for invalid_child in invalid_children:
        invalid_projection = _project_current_contract_state(
            [parent, invalid_child],
            root_record=parent,
            row_times={
                parent_id: "2026-07-27T00:00:00Z",
                child_id: "2026-07-27T00:02:00Z",
            },
        )
        assert not invalid_projection.get(
            "completed_repair_fresh_generation_barrier"
        )

    drifted_parent = json.loads(json.dumps(parent))
    drifted_parent["runtime_guide"]["next_legal_action"]["line_id"] = (
        "observer_reconcile"
    )
    drift_projection = _project_current_contract_state(
        [drifted_parent, child],
        root_record=drifted_parent,
        row_times={
            parent_id: "2026-07-27T00:00:00Z",
            child_id: "2026-07-27T00:02:00Z",
        },
    )
    assert not drift_projection.get(
        "completed_repair_fresh_generation_barrier"
    )

    unrelated_incomplete = {
        **parent,
        "contract_execution_id": "cex-unrelated-incomplete",
        "completed_lines": [],
        "runtime_guide": {
            "runtime_guide_hash": "sha256:unrelated-incomplete",
            "next_legal_action": {
                "stage_id": "worker",
                "line_id": "worker_implementation",
            },
        },
    }
    unrelated_projection = _project_current_contract_state(
        [parent, child, unrelated_incomplete],
        root_record=parent,
        row_times={
            parent_id: "2026-07-27T00:00:00Z",
            child_id: "2026-07-27T00:02:00Z",
            "cex-unrelated-incomplete": "2026-07-27T00:03:00Z",
        },
    )
    assert not unrelated_projection.get(
        "completed_repair_fresh_generation_barrier"
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = SQLiteContractExecutionStore(conn)
    root = {
        **parent,
        "contract_execution_id": root_id,
        "parent_contract_execution_id": "",
        "root_contract_execution_id": root_id,
        "contract_id": "onboard_route_guide",
        "version": "v1",
        "revision": "rev1",
        "execution_state_revision": 3,
        "completed_lines": [],
        "runtime_guide": {
            "runtime_guide_hash": "sha256:completed-onboard-root",
            "next_legal_action": None,
        },
        "metadata": {},
        "backlog_lineage": {},
    }
    store.create(root)
    store.create(parent)
    store.create(child)
    rebuilt = rebuild_backlog_contract_chain_projection(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    rebuilt_again = rebuild_backlog_contract_chain_projection(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    assert rebuilt["current_contract_execution_id"] == child_id
    assert rebuilt["readiness_state"] == "contract_complete"
    assert rebuilt["next_legal_action"] == {}
    assert rebuilt["scheduler_eligible"] is False
    assert rebuilt["resume_eligible"] is False
    assert rebuilt_again["projection_hash"] == rebuilt["projection_hash"]
    assert rebuilt_again["completed_repair_fresh_generation_barrier"][
        "barrier_hash"
    ] == rebuilt["completed_repair_fresh_generation_barrier"][
        "barrier_hash"
    ]

    active_chain_json = conn.execute(
        """
        SELECT active_chain_json
          FROM backlog_contract_chain_current
         WHERE project_id = ? AND backlog_id = ?
        """,
        (project_id, backlog_id),
    ).fetchone()[0]
    legacy_active_chain = json.loads(active_chain_json)
    legacy_active_chain.pop(
        "completed_repair_fresh_generation_barrier",
        None,
    )
    conn.execute(
        """
        UPDATE backlog_contract_chain_current
           SET current_contract_execution_id = ?,
               current_contract_id = ?,
               active_child_contract_execution_id = ?,
               readiness_state = ?,
               active_chain_json = ?,
               next_legal_action_json = ?
         WHERE project_id = ? AND backlog_id = ?
        """,
        (
            parent_id,
            "mf_parallel.v2",
            parent_id,
            "contract_active",
            json.dumps(legacy_active_chain, sort_keys=True, separators=(",", ":")),
            json.dumps(
                {
                    "id": "observer_merge",
                    "stage_id": "observer_integration",
                    "line_id": "observer_merge",
                    "source": "backlog_contract_chain_current",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            project_id,
            backlog_id,
        ),
    )
    migrated = read_backlog_contract_chain_current(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    assert migrated["current_contract_execution_id"] == child_id
    assert migrated["readiness_state"] == "contract_complete"
    assert migrated["next_legal_action"] == {}
    assert migrated["completed_repair_fresh_generation_barrier"][
        "fresh_generation_required"
    ] is True

    child_active_chain = json.loads(
        conn.execute(
            """
            SELECT active_chain_json
              FROM backlog_contract_chain_current
             WHERE project_id = ? AND backlog_id = ?
            """,
            (project_id, backlog_id),
        ).fetchone()[0]
    )
    child_active_chain.pop(
        "completed_repair_fresh_generation_barrier",
        None,
    )
    conn.execute(
        """
        UPDATE backlog_contract_chain_current
           SET active_chain_json = ?
         WHERE project_id = ? AND backlog_id = ?
        """,
        (
            json.dumps(
                child_active_chain,
                sort_keys=True,
                separators=(",", ":"),
            ),
            project_id,
            backlog_id,
        ),
    )
    migrated_child_without_barrier = read_backlog_contract_chain_current(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    assert migrated_child_without_barrier["current_contract_execution_id"] == child_id
    assert migrated_child_without_barrier["readiness_state"] == "contract_complete"
    assert migrated_child_without_barrier["next_legal_action"] == {}
    assert migrated_child_without_barrier[
        "completed_repair_fresh_generation_barrier"
    ]["fresh_generation_required"] is True

    migrated_active_chain = json.loads(
        conn.execute(
            """
            SELECT active_chain_json
              FROM backlog_contract_chain_current
             WHERE project_id = ? AND backlog_id = ?
            """,
            (project_id, backlog_id),
        ).fetchone()[0]
    )
    migrated_active_chain.pop(
        "completed_repair_fresh_generation_barrier",
        None,
    )
    conn.execute(
        """
        UPDATE backlog_contract_chain_current
           SET current_contract_execution_id = ?,
               current_contract_id = ?,
               readiness_state = ?,
               active_chain_json = ?,
               next_legal_action_json = ?
         WHERE project_id = ? AND backlog_id = ?
        """,
        (
            parent_id,
            "mf_parallel.v2",
            "same_row_recovery_target_ready",
            json.dumps(
                migrated_active_chain,
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    "line_id": "observer_merge",
                    "source": (
                        "backlog_contract_chain_current.same_row_recovery_cursor"
                    ),
                    "same_row_recovery_cursor": {"legacy": True},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            project_id,
            backlog_id,
        ),
    )
    migrated_cursor = read_backlog_contract_chain_current(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    assert migrated_cursor["current_contract_execution_id"] == child_id
    assert migrated_cursor["readiness_state"] == "contract_complete"
    assert migrated_cursor["next_legal_action"] == {}
    assert migrated_cursor["completed_repair_fresh_generation_barrier"][
        "fresh_generation_required"
    ] is True

    false_positive_active_chain = json.loads(
        conn.execute(
            """
            SELECT active_chain_json
              FROM backlog_contract_chain_current
             WHERE project_id = ? AND backlog_id = ?
            """,
            (project_id, backlog_id),
        ).fetchone()[0]
    )
    false_positive_active_chain.pop(
        "completed_repair_fresh_generation_barrier",
        None,
    )
    conn.execute(
        """
        UPDATE backlog_contract_chain_current
           SET current_contract_execution_id = ?,
               current_contract_id = ?,
               active_child_contract_execution_id = ?,
               readiness_state = ?,
               active_chain_json = ?,
               next_legal_action_json = ?
         WHERE project_id = ? AND backlog_id = ?
        """,
        (
            parent_id,
            "mf_parallel.v2",
            parent_id,
            "contract_complete",
            json.dumps(
                false_positive_active_chain,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "{}",
            project_id,
            backlog_id,
        ),
    )
    unrelated_contract_complete = read_backlog_contract_chain_current(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    assert unrelated_contract_complete["current_contract_execution_id"] == parent_id
    assert not unrelated_contract_complete.get(
        "completed_repair_fresh_generation_barrier"
    )

    conn.execute(
        """
        UPDATE backlog_contract_chain_current
           SET active_child_contract_execution_id = ?,
               readiness_state = ?,
               next_legal_action_json = ?
         WHERE project_id = ? AND backlog_id = ?
        """,
        (
            "",
            "contract_active",
            json.dumps(
                {
                    "id": "observer_merge",
                    "stage_id": "observer_integration",
                    "line_id": "observer_merge",
                    "source": "backlog_contract_chain_current",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            project_id,
            backlog_id,
        ),
    )
    source_without_active_child = read_backlog_contract_chain_current(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    assert source_without_active_child["current_contract_execution_id"] == parent_id
    assert source_without_active_child["active_child_contract_execution_id"] == ""
    assert not source_without_active_child.get(
        "completed_repair_fresh_generation_barrier"
    )


def test_incomplete_recovery_remains_current_and_fail_closed(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    backlog_id = "AC-CHAIN-INCOMPLETE-RECOVERY-CURRENT"
    root = _start_completed_chain_projection_root(
        runtime,
        backlog_id=backlog_id,
        contract_execution_id="cex-a-incomplete-recovery-root",
    )
    stale = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id="cex-b-incomplete-stale",
        actor_role="observer",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )
    recovery = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id="cex-z-incomplete-recovery",
        actor_role="observer",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
        backlog_lineage={
            "recovery_policy": "start_new_execution",
            "stale_contract_execution_id": stale["contract_execution_id"],
        },
    )

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )

    assert current["current_contract_execution_id"] == recovery[
        "contract_execution_id"
    ]
    assert current["readiness_state"] == "contract_active"
    assert current["next_legal_action"]["line_id"] == (
        "observer_prefill_child_contracts"
    )
    assert set(current["active_chain"]["execution_ids"]) == {
        root["contract_execution_id"],
        stale["contract_execution_id"],
        recovery["contract_execution_id"],
    }


def test_direct_fix_qa_without_explicit_binding_counts_after_repair(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, _, _ = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-GENERIC",
    )

    generic_qa_write = _write_from(
        direct_fix,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
        evidence_kind="independent_verification",
    )
    generic_qa_write["status"] = "passed"
    generic_qa_write["payload"] = {
        "status": "pass",
        "qa_summary": "generic QA line without child/projection/source refs",
    }
    generic_qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        generic_qa_write,
        actor_role="qa",
    )
    assert generic_qa["ok"] is True

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-QA-GENERIC",
    )
    assert current["readiness_state"] == "return_to_parent_after_direct_fix_qa"
    assert current["current_contract_execution_id"] == direct_fix["contract_execution_id"]
    assert current["next_legal_action"]["id"] == "return_to_parent_after_direct_fix_qa"


def test_direct_fix_qa_with_child_generation_and_source_refs_is_counted(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-EXPLICIT",
    )

    qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _direct_fix_qa_write(
            direct_fix,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-QA-EXPLICIT",
    )
    assert current["readiness_state"] == "return_to_parent_after_direct_fix_qa"
    assert current["current_contract_execution_id"] == direct_fix["contract_execution_id"]
    assert current["next_legal_action"]["id"] == "return_to_parent_after_direct_fix_qa"
    assert current["next_legal_action"]["qa_evidence_ref"] == (
        f"contract_runtime:{direct_fix['contract_execution_id']}:completed_lines:3"
    )


def test_direct_fix_projection_does_not_resume_parent_when_return_precedes_qa(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-RETURN-BEFORE-QA",
    )

    record = runtime.store.get(direct_fix["contract_execution_id"])
    return_line = _write_from(
        record,
        actor_role="observer",
        stage_id="return_to_parent",
        line_id="direct_fix_return_to_parent",
        evidence_kind="direct_fix_return_to_parent",
    )
    qa_line = _direct_fix_qa_write(
        record,
        generation=generation,
        repair_ref=repair_ref,
    )
    mutated = dict(record)
    mutated["completed_lines"] = list(record["completed_lines"]) + [
        return_line,
        qa_line,
    ]
    runtime.store.update(mutated["contract_execution_id"], mutated)

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-RETURN-BEFORE-QA",
        rebuild_if_missing=True,
    )
    assert current["readiness_state"] == "return_to_parent_after_direct_fix_qa"
    assert current["current_contract_execution_id"] == direct_fix["contract_execution_id"]
    assert current["next_legal_action"]["id"] == "return_to_parent_after_direct_fix_qa"


def test_later_mf_parallel_successor_becomes_current_after_direct_fix_return(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    root, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
    )
    qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _direct_fix_qa_write(
            direct_fix,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True
    runtime.current_guide(direct_fix["contract_execution_id"], actor_role="observer")
    qa_record = runtime.store.get(direct_fix["contract_execution_id"])
    returned = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            qa_record,
            actor_role="observer",
            stage_id="return_to_parent",
            line_id="direct_fix_return_to_parent",
            evidence_kind="direct_fix_return_to_parent",
        ),
        actor_role="observer",
    )
    assert returned["ok"] is True
    current_after_return = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
    )
    assert current_after_return["readiness_state"] == (
        "parent_resume_required_after_direct_fix_qa"
    )
    parent_next = current_after_return["next_legal_action"]
    assert parent_next["id"] == "resume_parent_after_successor_return"
    assert parent_next["stage_id"] == "successor_return"
    assert parent_next["line_id"] == "resume_parent_after_successor_return"
    assert parent_next["evidence_kind"] == "successor_return_acknowledgement"
    assert parent_next["owner_role"] == "observer"

    mf_parallel = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
        contract_execution_id="zz-mf-parallel-after-direct-fix",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-after-direct-fix",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
    )
    assert current["current_contract_execution_id"] == (
        mf_parallel["contract_execution_id"]
    )
    assert current["active_child_contract_execution_id"] == (
        mf_parallel["contract_execution_id"]
    )
    assert current["current_contract_id"] == "mf_parallel.v1"
    assert current["readiness_state"] == "contract_active"


def test_parent_resume_cursor_advances_across_returned_direct_fix_children(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    backlog_id = "AC-DIRECT-FIX-MULTI-RETURN-CURSOR"
    root, first_child, first_generation, first_repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id=backlog_id,
    )
    first_child = _return_direct_fix_to_parent(
        runtime,
        first_child,
        generation=first_generation,
        repair_ref=first_repair_ref,
    )
    second_child, second_generation, second_repair_ref = _start_repaired_direct_fix_child(
        runtime,
        root,
        backlog_id=backlog_id,
        contract_execution_id=f"cex-direct-b-{backlog_id}",
    )
    second_child = _return_direct_fix_to_parent(
        runtime,
        second_child,
        generation=second_generation,
        repair_ref=second_repair_ref,
    )

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )
    assert current["readiness_state"] == (
        "parent_resume_required_after_direct_fix_qa"
    )
    assert current["next_legal_action"]["successor_contract_execution_id"] == (
        first_child["contract_execution_id"]
    )

    _append_parent_successor_ack(
        runtime,
        parent_id=root["contract_execution_id"],
        child_id=first_child["contract_execution_id"],
    )
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )
    assert current["readiness_state"] == (
        "parent_resume_required_after_direct_fix_qa"
    )
    assert current["next_legal_action"]["successor_contract_execution_id"] == (
        second_child["contract_execution_id"]
    )

    _append_parent_successor_ack(
        runtime,
        parent_id=root["contract_execution_id"],
        child_id=second_child["contract_execution_id"],
    )
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )
    assert current["current_contract_execution_id"] == root["contract_execution_id"]
    assert current["parent_to_resume_contract_execution_id"] == ""
    assert current["readiness_state"] == "contract_active"
    assert current["next_legal_action"]["line_id"] == "read_context"


def test_server_chain_current_refreshes_stale_next_action_after_finish(tmp_path):
    from agent.governance import server

    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )

    qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _direct_fix_qa_write(
            direct_fix,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True
    stale_after_qa = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )
    assert stale_after_qa["next_legal_action"]["line_id"] == (
        "direct_fix_return_to_parent"
    )

    runtime.current_guide(direct_fix["contract_execution_id"], actor_role="observer")
    record = runtime.store.get(direct_fix["contract_execution_id"])
    returned = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            record,
            actor_role="observer",
            stage_id="return_to_parent",
            line_id="direct_fix_return_to_parent",
            evidence_kind="direct_fix_return_to_parent",
        ),
        actor_role="observer",
    )
    assert returned["ok"] is True
    assert returned["record"]["runtime_guide"]["next_legal_action"] is None

    conn.execute(
        """
        UPDATE backlog_contract_chain_current
        SET next_legal_action_json = ?
        WHERE project_id = ? AND backlog_id = ?
        """,
        (
            json.dumps(
                stale_after_qa["next_legal_action"],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "aming-claw",
            "AC-DIRECT-FIX-STALE-AFTER-FINISH",
        ),
    )
    stale_read = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )
    assert stale_read["next_legal_action"]["line_id"] == "direct_fix_return_to_parent"

    refreshed = server._contract_chain_current_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )

    assert refreshed["current_contract_execution_id"].startswith("cex-root-")
    assert refreshed["next_legal_action"]["line_id"] == "read_context"
    assert refreshed["next_legal_action"]["line_id"] != "direct_fix_return_to_parent"
    assert refreshed["contract_runtime_current_state"]["next_legal_action"][
        "line_id"
    ] == "read_context"
    freshness = refreshed["projection_freshness"]
    assert freshness["status"] == "refreshed_from_contract_runtime_current"
    assert freshness["runtime_context_projection_applied"] is False
    assert freshness["stale_next_legal_action"]["line_id"] == (
        "direct_fix_return_to_parent"
    )


def test_server_chain_current_projects_mf_parallel_runtime_context_completion_read_only(
    tmp_path,
    monkeypatch,
):
    from agent.governance import server

    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE",
        contract_execution_id="cex-mf-parallel-runtime-context-projected-complete",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-runtime-context-projected-complete",
    )
    stored_before = runtime.store.get(record["contract_execution_id"])
    assert stored_before.get("completed_lines") == []
    durable = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE",
    )
    assert durable["readiness_state"] == "contract_active"
    assert durable["next_legal_action"]["line_id"] == "observer_prefill_child_contracts"

    projected_line = {
        "stage_id": "observer_prefill",
        "line_id": "observer_prefill_child_contracts",
        "actor_role": "observer",
        "evidence_kind": "mf_parallel_prefill",
        "line_instance_id": "runtime_context:mfrctx-projected-complete",
        "payload": {
            "source": "runtime_context_worker_evidence",
            "projection_persists_completed_line": False,
            "observer_authored_worker_backfill": False,
        },
    }
    projection = {
        "schema_version": "contract_runtime.mf_parallel_runtime_context_projection.v1",
        "source": "runtime_context_worker_evidence",
        "projected_completed_lines": [projected_line],
        "projected_line_count": 1,
        "persistence": {
            "mutates_contract_runtime_completed_lines": False,
            "observer_authored_worker_backfill": False,
        },
    }

    def projected_runtime_current(_conn, *, project_id, record, actor_role):
        assert project_id == "aming-claw"
        assert actor_role == "observer"
        projected = dict(record)
        projected["execution_state_revision"] = 2
        projected["execution_state"] = {
            "schema_version": "contract_execution_state.v1",
            "contract_execution_id": record["contract_execution_id"],
            "execution_state_revision": 2,
            "execution_state_hash": "sha256:projected-runtime-context-state",
            "completed_lines": [projected_line],
        }
        projected["runtime_guide"] = {
            "schema_version": "contract_runtime_guide.v1",
            "next_legal_action": None,
            "runtime_guide_hash": "sha256:projected-runtime-context-guide",
            "execution": {
                "project_id": "aming-claw",
                "backlog_id": (
                    "AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE"
                ),
                "contract_execution_id": record["contract_execution_id"],
                "execution_state_revision": 2,
                "execution_state_hash": "sha256:projected-runtime-context-state",
                "route_token_ref": (
                    "rtok-mf-parallel-runtime-context-projected-complete"
                ),
            },
        }
        return projected, projection

    monkeypatch.setattr(
        server,
        "_contract_runtime_apply_mf_parallel_context_projection",
        projected_runtime_current,
    )

    refreshed = server._contract_chain_current_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE",
    )

    assert refreshed["readiness_state"] == "contract_complete"
    assert refreshed["next_legal_action"] == {}
    assert refreshed["contract_runtime_current_state"]["readiness_state"] == (
        "contract_complete"
    )
    assert refreshed["contract_runtime_current_state"]["next_legal_action"] == {}
    freshness = refreshed["projection_freshness"]
    assert freshness["status"] == "refreshed_from_contract_runtime_current"
    assert freshness["runtime_context_projection_applied"] is True
    assert freshness["readiness_state_changed"] is True
    assert freshness["stale_readiness_state"] == "contract_active"
    assert freshness["refreshed_readiness_state"] == "contract_complete"
    assert freshness["runtime_context_projection"]["persistence"] == {
        "mutates_contract_runtime_completed_lines": False,
        "observer_authored_worker_backfill": False,
    }

    stored_after = runtime.store.get(record["contract_execution_id"])
    assert stored_after.get("completed_lines") == []


def test_mf_parallel_failed_qa_summary_resets_completion_path(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v1",
        stages=[
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                        "requires": ["worker_implementation"],
                    }
                ],
            },
            {
                "stage_id": "observer_integration",
                "lines": [
                    {
                        "line_id": "observer_merge",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "merge",
                        "requires": ["qa_independent_verification"],
                    }
                ],
            },
        ],
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-FAILED-QA-SUMMARY",
        contract_execution_id="cex-mf-parallel-failed-qa-summary",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-failed-qa-summary",
    )

    runtime.current_guide(record["contract_execution_id"], actor_role="mf_sub")
    worker_record = runtime.store.get(record["contract_execution_id"])
    worker = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            worker_record,
            actor_role="mf_sub",
            stage_id="worker_implementation",
            line_id="worker_implementation",
            evidence_kind="implementation",
        ),
        actor_role="mf_sub",
    )
    assert worker["ok"] is True
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])
    qa_write = _write_from(
        qa_record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
        evidence_kind="independent_verification",
    )
    qa_write["payload"] = {
        "summary": (
            "Independent QA failed the worker commit because legacy route-token "
            "refs cannot renew."
        )
    }

    qa = runtime.submit_line_write(
        record["contract_execution_id"],
        qa_write,
        actor_role="qa",
    )
    assert qa["ok"] is True

    guide = runtime.current_guide(record["contract_execution_id"], actor_role="observer")
    assert guide["next_legal_action"]["line_id"] == "worker_implementation"
    assert guide["next_legal_action"]["owner_role"] == "mf_sub"


def test_mf_parallel_failed_qa_retry_reuses_same_context_setup_lines(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v1",
        stages=[
            {
                "stage_id": "worker_read",
                "lines": [
                    {
                        "line_id": "worker_read_runtime_guide",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "read_receipt",
                    }
                ],
            },
            {
                "stage_id": "worker_startup",
                "lines": [
                    {
                        "line_id": "worker_startup",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "mf_subagent_startup",
                    }
                ],
            },
            {
                "stage_id": "worker_context",
                "lines": [
                    {
                        "line_id": "worker_graph_context",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "graph_trace",
                    }
                ],
            },
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                    }
                ],
            },
            {
                "stage_id": "worker_finish",
                "lines": [
                    {
                        "line_id": "worker_finish_gate",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "mf_subagent_finish_gate",
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                    }
                ],
            },
            {
                "stage_id": "observer_integration",
                "lines": [
                    {
                        "line_id": "observer_close_ready",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "close_ready",
                    }
                ],
            },
        ],
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-FAILED-QA-RETRY-CONTEXT",
        contract_execution_id="cex-mf-parallel-failed-qa-retry-context",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-failed-qa-retry-context",
    )
    context_id = "mfrctx-same-worker"
    instance_id = f"runtime_context:{context_id}"

    def line(stage, line_id, role, evidence, *, payload=None):
        payload = dict(payload or {})
        payload.setdefault("runtime_context_id", context_id)
        return {
            "stage_id": stage,
            "line_id": line_id,
            "actor_role": role,
            "evidence_kind": evidence,
            "line_instance_id": instance_id,
            "runtime_context_id": context_id,
            "payload": payload,
        }

    completed_lines = [
        line(
            "worker_read",
            "worker_read_runtime_guide",
            "mf_sub",
            "read_receipt",
            payload={"schema_version": "contract_context_read_receipt.v1"},
        ),
        line("worker_startup", "worker_startup", "mf_sub", "mf_subagent_startup"),
        line("worker_context", "worker_graph_context", "mf_sub", "graph_trace"),
        line("worker_implementation", "worker_implementation", "mf_sub", "implementation"),
        line(
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
            payload={"summary": "Independent QA failed the worker commit."},
        ),
        line(
            "worker_implementation",
            "worker_implementation",
            "mf_sub",
            "implementation",
            payload={
                "schema_version": "mf_sub.worker_implementation.v1",
                "summary": "Retry implementation fixed the QA blocker.",
            },
        ),
        line(
            "worker_finish",
            "worker_finish_gate",
            "mf_sub",
            "mf_subagent_finish_gate",
            payload={"schema_version": "mf_sub.finish_gate.v1"},
        ),
        line(
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
            payload={
                "schema_version": "qa_independent_verification.retry.v1",
                "qa_result": "pass",
                "pass": True,
            },
        ),
        {
            "stage_id": "observer_integration",
            "line_id": "observer_close_ready",
            "actor_role": "observer",
            "evidence_kind": "close_ready",
            "payload": {"schema_version": "observer_close_ready.retry.v1"},
        },
    ]

    projected = runtime.projected_record(
        record["contract_execution_id"],
        actor_role="observer",
        completed_lines=completed_lines,
    )
    assert projected["runtime_guide"]["next_legal_action"] is None


def test_direct_fix_qa_evidence_preserves_owner_submitter_provenance(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-PROVENANCE",
    )
    write = _direct_fix_qa_write(
        direct_fix,
        generation=generation,
        repair_ref=repair_ref,
    )
    write.update(
        {
            "actor_session_principal": "qa:curie",
            "evidence_owner_actor": "qa:curie",
            "evidence_owner_session_ref": "qstok-curie",
            "submitter_session": "obs-parent-materializer",
            "submitter_principal": "observer:parent",
            "materialized_from": "qa_packet:qapkt-curie",
            "authorization_source": "qa_session_token_ref",
            "qa_session_token_ref": "qstok-curie",
        }
    )

    result = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        write,
        actor_role="qa",
    )

    assert result["ok"] is True
    line = result["record"]["completed_lines"][-1]
    assert line["actor_role"] == "qa"
    assert line["evidence_owner_role"] == "qa"
    assert line["evidence_owner_actor"] == "qa:curie"
    assert line["submitter_session"] == "obs-parent-materializer"
    assert line["submitter_principal"] == "observer:parent"
    assert line["materialized_from"] == "qa_packet:qapkt-curie"
    assert line["authorization_source"] == "qa_session_token_ref"
    assert line["observer_impersonation"] is False
    assert line["qa_evidence_provenance"]["evidence_owner_actor"] == "qa:curie"
    assert line["qa_evidence_provenance"]["submitter_principal"] == "observer:parent"


def test_direct_fix_qa_materialization_defaults_do_not_make_observer_owner(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-MATERIALIZER-NOT-OWNER",
    )
    write = _direct_fix_qa_write(
        direct_fix,
        generation=generation,
        repair_ref=repair_ref,
    )
    write.update(
        {
            "submitter_session": "obs-parent-materializer",
            "submitter_principal": "observer:parent",
            "materialized_from": "qa_packet:qapkt-unowned",
        }
    )

    result = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        write,
        actor_role="qa",
    )

    assert result["ok"] is True
    line = result["record"]["completed_lines"][-1]
    assert line["actor_role"] == "qa"
    assert line["evidence_owner_role"] == "qa"
    assert line["evidence_owner_actor"] == "qa"
    assert line["submitter_principal"] == "observer:parent"
    assert line["qa_evidence_provenance"]["evidence_owner_actor"] == "qa"
    assert line["qa_evidence_provenance"]["submitter_principal"] == "observer:parent"


def test_upsert_contract_chain_successor_binding_rejects_lineage_mismatch(tmp_path):
    _write_minimal_contract(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-LINEAGE-MISMATCH",
        contract_execution_id="cex-lineage-root",
        actor_role="observer",
    )
    child = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-LINEAGE-MISMATCH",
        contract_execution_id="cex-lineage-child",
        actor_role="observer",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    cases = [
        ("parent_contract_execution_id", "cex-other-parent", "parent_contract"),
        ("root_contract_execution_id", "cex-other-root", "root_contract"),
        ("contract_chain_id", "cchain-other", "contract_chain"),
    ]
    for field, value, message in cases:
        bad_child = dict(child)
        bad_child[field] = value
        with pytest.raises(ContractRuntimeError, match=message):
            upsert_contract_chain_successor_binding(
                conn,
                parent_record=root,
                child_record=bad_child,
            )


def test_deprecated_definition_replays_but_cannot_start_new_execution(tmp_path):
    _write_minimal_contract(tmp_path)
    registry = ContractDefinitionRegistry(tmp_path)
    runtime = ContractRuntime(registry, instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )

    registry.deprecate_definition(
        "observer_onboard",
        version="v1",
        revision="rev1",
        reason="test deprecation",
    )

    with pytest.raises(ContractRuntimeError, match="cannot start new executions"):
        runtime.start_execution(
            "observer_onboard",
            project_id="aming-claw",
            backlog_id="AC-MIN-PATH-2",
            actor_role="observer",
        )

    replay_guide = runtime.current_guide(record["contract_execution_id"])
    assert replay_guide["contract"]["contract_id"] == "observer_onboard"


def test_runtime_raises_structured_stale_pinned_execution_error(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )
    record["definition_hash"] = "sha256:stale-pinned-definition"
    runtime.store.update(record["contract_execution_id"], record)

    with pytest.raises(StalePinnedContractExecutionError) as exc:
        runtime.current_guide(record["contract_execution_id"], actor_role="observer")

    error = exc.value.to_dict()
    assert error["field"] == "definition_hash"
    assert error["contract_execution_id"] == record["contract_execution_id"]
    assert error["pinned_definition_hash"] == "sha256:stale-pinned-definition"
    assert error["current_definition_hash"].startswith("sha256:")
