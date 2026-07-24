from __future__ import annotations

from agent.governance.contracts.runtime import (
    _active_failed_qa_line,
    _line_status_allows_contract_completion,
)


def _accepted_no_pass_line(*, reported_baseline_failed: int) -> dict:
    failures = [f"test_known_baseline_{index:02d}" for index in range(19)]
    base_commit = "a" * 40
    candidate_commit = "b" * 40
    common_results = {
        "status": "accepted",
        "candidate_new_failures": 0,
        "candidate_specific_issues": [],
        "no_pass_claim": True,
        "passed": False,
        "overall_release_pass": False,
        "overall_release_pass_claimed": False,
        "baseline": {"failed": reported_baseline_failed},
        "candidate": {"failed": len(failures), "passed": 741},
    }
    return {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "runtime_context_id": "mfrctx-no-pass-binding",
        "task_id": "worker-no-pass-binding",
        "parent_task_id": "parent-no-pass-binding",
        "authorization_source": "qa_session_token_ref",
        "observer_impersonation": False,
        "parent_materialization_authorized": False,
        "commit_sha": candidate_commit,
        "status": "accepted",
        "verdict": "accepted",
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_session_token_ref",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "parent_materialization_authorized": False,
            "completion_status_gate": {
                "schema_version": "contract_runtime.qa_completion_status_gate.v1",
                "source": "contract_runtime_line_write_normalization",
                "server_derived": True,
                "top_level_status_present": True,
                "top_level_status_passing": True,
                "normalized_status": "accepted",
                "nested_payload_decision_satisfies": False,
            },
            "authenticated_qa_binding": {
                "schema_version": "contract_runtime.authenticated_qa_binding.v1",
                "server_derived": True,
                "qa_principal": "qa:no-pass-binding",
                "qa_session_id": "ses-no-pass-binding",
                "independent_verification_session_matched": True,
            },
        },
        "payload": {
            "schema_version": "mf_parallel.qa_independent_verification.v1",
            "runtime_context_id": "mfrctx-no-pass-binding",
            "task_id": "worker-no-pass-binding",
            "parent_task_id": "parent-no-pass-binding",
            "acceptance_scope": "candidate_regression_and_acceptance_criteria",
            "verdict": "accepted",
            "full_suite_claim": "not_claimed",
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
            "test_results": dict(common_results),
        },
        "test_results": dict(common_results),
        "verification": {
            "status": "accepted",
            "verdict": "accepted",
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
        },
        "artifact_refs": {
            "external_no_pass_baseline_ledger": {
                "schema_version": (
                    "contract_runtime.external_no_pass_baseline_ledger.v2"
                ),
                "server_normalized": True,
                "base_commit_sha": base_commit,
                "candidate_commit_sha": candidate_commit,
                "base_failure_identities": failures,
                "candidate_failure_identities": failures,
                "base_reproduction": {
                    "reproduced": len(failures),
                    "total": len(failures),
                    "failure_identities": failures,
                },
                "candidate_suite_counts": {
                    "baseline_known_non_green": len(failures),
                    "failed": len(failures),
                    "passed": 741,
                },
                "candidate_new_failures": 0,
                "candidate_specific_issues": [],
                "no_pass_claim": True,
                "overall_release_pass_claimed": False,
                "refs": ["base:known", "candidate:known"],
            }
        },
    }


def test_exact_accepted_no_pass_line_allows_contract_completion():
    line = _accepted_no_pass_line(reported_baseline_failed=19)
    record = {
        "contract_execution_id": "cex-no-pass-binding",
        "completed_lines": [line],
    }

    assert _line_status_allows_contract_completion(
        line,
        source_record=record,
        source_line_index=0,
    )
    assert _active_failed_qa_line(
        [line],
        source_record=record,
    ) == (-1, {})


def test_accepted_no_pass_count_mismatch_is_active_failed_qa():
    line = _accepted_no_pass_line(reported_baseline_failed=20)
    record = {
        "contract_execution_id": "cex-no-pass-binding",
        "completed_lines": [line],
    }

    assert not _line_status_allows_contract_completion(
        line,
        source_record=record,
        source_line_index=0,
    )
    failed_index, failed_line = _active_failed_qa_line(
        [line],
        source_record=record,
    )
    assert failed_index == 0
    assert failed_line is line
