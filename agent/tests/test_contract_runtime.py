from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from agent.governance import parallel_branch_runtime, server
from agent.governance.contracts.runtime import (
    ContractRuntime,
    WriteGateDecision,
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


def _synthetic_later_qa_pass(*, summary: str) -> dict:
    return {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "passed",
        "observer_impersonation": False,
        "payload": {
            "status": "passed",
            "summary": summary,
        },
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_role_session",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "completion_status_gate": {
                "schema_version": (
                    "contract_runtime.qa_completion_status_gate.v1"
                ),
                "source": "contract_runtime_line_write_normalization",
                "server_derived": True,
                "top_level_status_present": False,
                "top_level_status_passing": False,
                "normalized_status": "",
                "nested_payload_decision_satisfies": False,
            },
        },
    }


def test_synthetic_later_pass_clears_repair_without_satisfying_close():
    failed = {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "status": "failed",
        "payload": {"summary": "Independent QA failed the worker commit."},
    }
    passed = _synthetic_later_qa_pass(
        summary="Independent QA passed the retry."
    )
    record = {
        "contract_execution_id": "cex-synthetic-qa-pass",
        "completed_lines": [failed, passed],
    }

    assert not _line_status_allows_contract_completion(
        passed,
        source_record=record,
        source_line_index=1,
    )
    assert _active_failed_qa_line(
        record["completed_lines"],
        source_record=record,
    ) == (-1, {})


def test_synthetic_later_pass_with_failure_summary_keeps_repair_active():
    failed = {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "status": "failed",
        "payload": {"summary": "Independent QA failed the worker commit."},
    }
    contradictory = _synthetic_later_qa_pass(
        summary="Independent QA failed the worker commit again."
    )
    record = {
        "contract_execution_id": "cex-synthetic-qa-contradiction",
        "completed_lines": [failed, contradictory],
    }

    failed_index, failed_line = _active_failed_qa_line(
        record["completed_lines"],
        source_record=record,
    )
    assert failed_index == 1
    assert failed_line is contradictory


def test_failed_qa_rework_close_gate_prevalidates_without_generic_mutation(
    monkeypatch,
):
    """The facade's first gate must not append the canonical retry line.

    The runtime-context canonical helper owns the one permitted mutation.  If
    the generic timeline close gate writes first, the same request is observed
    as a duplicate revision by the canonical helper and can fail after a
    partial ContractRuntime advance.
    """

    record = {
        "contract_execution_id": "cex-failed-qa-atomic",
        "project_id": "project-failed-qa-atomic",
        "completed_lines": [],
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "worker_implementation",
                "line_id": "worker_implementation",
                "evidence_kind": "implementation",
            }
        },
    }
    submit_calls = []

    class _Store:
        def get(self, _contract_execution_id):
            return record

    class _Runtime:
        store = _Store()

        def current_guide(self, *_args, **_kwargs):
            return record["runtime_guide"]

        def submit_line_write(self, *_args, **_kwargs):
            submit_calls.append((_args, _kwargs))
            raise AssertionError(
                "generic close gate must not mutate a prevalidated failed-QA retry"
            )

    runtime_context = SimpleNamespace(
        runtime_context_id="mfrctx-failed-qa-atomic",
        task_id="worker-failed-qa-atomic",
    )
    monkeypatch.setattr(server, "_contract_runtime", lambda _conn: _Runtime())
    monkeypatch.setattr(
        server,
        "_contract_runtime_apply_mf_parallel_context_projection",
        lambda *_args, **_kwargs: (record, {}),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_close_line_for_event",
        lambda *_args, **_kwargs: {
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "evidence_kind": "implementation",
        },
    )
    monkeypatch.setattr(
        server,
        "_runtime_current_state_from_record",
        lambda _record: {
            "next_legal_action": record["runtime_guide"]["next_legal_action"]
        },
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_write_from_record",
        lambda *_args, **_kwargs: {
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "evidence_kind": "implementation",
        },
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_revise_failed_qa_implementation_lineage",
        lambda *_args, **_kwargs: {
            "status": "validated_submission",
            "canonical_payload": dict(_kwargs["payload"]),
        },
    )
    monkeypatch.setattr(
        parallel_branch_runtime,
        "get_branch_context_by_runtime_context_id",
        lambda *_args, **_kwargs: runtime_context,
    )

    gate = server._contract_runtime_close_gate(
        object(),
        project_id="project-failed-qa-atomic",
        body={
            "contract_execution_id": "cex-failed-qa-atomic",
            "runtime_context_id": runtime_context.runtime_context_id,
            "actor": "mf_sub",
        },
        event_kind="implementation",
        norm_payload={
            "runtime_context_id": runtime_context.runtime_context_id,
            "task_id": runtime_context.task_id,
        },
        trusted_actor_role="mf_sub",
    )

    assert gate["status"] == "validated_submission"
    assert gate["canonical_submit_required"] is True
    assert submit_calls == []


def test_failed_qa_revision_prepares_guide_before_single_cas_update():
    prior_commit = "a" * 40
    revised_commit = "b" * 40
    revision_event_ref = "timeline:failed-qa-rejoin"
    identity = {
        "runtime_context_id": "mfrctx-failed-qa-single-cas",
        "task_id": "worker-failed-qa-single-cas",
        "parent_task_id": "parent-failed-qa-single-cas",
        "worker_id": "worker-failed-qa-single-cas",
        "worker_slot_id": "worker-failed-qa-single-cas",
        "target_project_root": "/tmp/failed-qa-single-cas",
        "fence_token_hash": "sha256:" + ("c" * 64),
        "session_token_ref": "wstok-failed-qa-single-cas",
    }
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    prior_implementation = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "commit_sha": prior_commit,
        "changed_files": ["agent/governance/server.py"],
        "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        **identity,
        "payload": {
            **identity,
            "changed_files": ["agent/governance/server.py"],
            "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        },
    }
    record = {
        "contract_execution_id": "cex-failed-qa-single-cas",
        "contract_id": "mf_parallel.v2",
        "project_id": "project-failed-qa-single-cas",
        "backlog_id": "backlog-failed-qa-single-cas",
        "execution_state_revision": 7,
        "completed_lines": [failed_qa, prior_implementation],
    }

    class _SingleCasStore:
        def __init__(self):
            self.record = deepcopy(record)
            self.update_calls = 0

        def get(self, _contract_execution_id):
            return deepcopy(self.record)

        def update(
            self,
            _contract_execution_id,
            updated,
            *,
            expected_revision=None,
        ):
            self.update_calls += 1
            if self.update_calls > 1:
                raise AssertionError(
                    "failed-QA revision must use one prepared CAS update"
                )
            assert expected_revision == 7
            self.record = deepcopy(updated)
            return deepcopy(self.record)

    store = _SingleCasStore()
    runtime = object.__new__(ContractRuntime)
    runtime.store = store

    def _record_view(source, *, actor_role=None, completed_lines=None, **_kwargs):
        view = deepcopy(dict(source))
        view["completed_lines"] = deepcopy(list(completed_lines or []))
        view["execution_state"] = {
            "execution_state_revision": source["execution_state_revision"],
        }
        retry_commit_exists = any(
            line.get("line_id") == "worker_commit"
            for line in view["completed_lines"][1:]
            if isinstance(line, dict)
        )
        view["runtime_guide"] = {
            "next_legal_action": {
                "stage_id": (
                    "worker_attestation"
                    if retry_commit_exists
                    else "worker_commit"
                ),
                "line_id": (
                    "worker_finish_time_attestation"
                    if retry_commit_exists
                    else "worker_commit"
                ),
                "owner_role": "mf_sub",
                "evidence_kind": (
                    "record_finish_time_worker_attestation"
                    if retry_commit_exists
                    else "commit"
                ),
            }
        }
        view["precheck_decision"] = WriteGateDecision(ok=True).to_dict()
        return view

    runtime._record_view = _record_view
    marker = {
        "revision_event_ref": revision_event_ref,
        "session_token_ref_rotation": {},
    }
    authority = {
        "source": "runtime_context_clean_cumulative_git_revision",
        "server_derived": True,
        "actual_head_commit": revised_commit,
        "clean_worktree": True,
        "cumulative_changed_files": ["agent/governance/server.py"],
        "owned_files": ["agent/governance/server.py"],
        "revision_event_ref": revision_event_ref,
    }
    revision_write = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "commit_sha": revised_commit,
        "changed_files": ["agent/governance/server.py"],
        "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        **identity,
        "payload": {
            **identity,
            "changed_files": ["agent/governance/server.py"],
            "graph_trace_ids": ["gqt-failed-qa-single-cas"],
            "graph_trace_db_evidence": {
                "db_verified": True,
                "verified_trace_ids": ["gqt-failed-qa-single-cas"],
            },
            "failed_qa_revision_rejoin_marker": marker,
            "canonical_rework_lineage_revision_authority": authority,
        },
    }
    result = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )

    assert result["ok"] is True
    assert result["status"] == "revised"
    assert store.update_calls == 1
    assert store.record["execution_state_revision"] == 8
    assert store.record["runtime_guide"]["next_legal_action"]["line_id"] == (
        "worker_commit"
    )

    exact_replay = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )
    assert exact_replay["ok"] is True
    assert exact_replay["status"] == "already_completed"
    assert store.update_calls == 1

    store.record["completed_lines"].append(
        {
            "stage_id": "worker_commit",
            "line_id": "worker_commit",
            "actor_role": "mf_sub",
            "evidence_kind": "commit",
            "commit_sha": revised_commit,
            **identity,
            "payload": dict(identity),
        }
    )
    exact_after_commit = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )
    assert exact_after_commit["ok"] is True
    assert exact_after_commit["status"] == "already_completed"
    assert store.update_calls == 1

    later_commit = "d" * 40
    later_revision = deepcopy(revision_write)
    later_revision["commit_sha"] = later_commit
    later_revision["payload"][
        "canonical_rework_lineage_revision_authority"
    ]["actual_head_commit"] = later_commit
    rejected_later_revision = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        later_revision,
        actor_role="mf_sub",
    )
    assert rejected_later_revision["ok"] is False
    assert any(
        "closed after retry worker_commit" in error
        for error in rejected_later_revision["decision"]["errors"]
    )
    assert store.update_calls == 1


def test_failed_qa_fresh_dispatch_revision_is_append_only_single_cas_and_replay_safe():
    historical_dispatch = {
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "actor_role": "observer",
        "evidence_kind": "dispatch_bounded_worker",
        "runtime_context_id": "mfrctx-original",
        "task_id": "worker-original",
        "parent_task_id": "cex-failed-qa-fresh-dispatch",
        "worker_id": "worker-original",
        "worker_slot_id": "worker-original",
        "owned_files": ["agent/governance/server.py"],
        "payload": {
            "runtime_context_id": "mfrctx-original",
            "task_id": "worker-original",
            "parent_task_id": "cex-failed-qa-fresh-dispatch",
            "worker_role": "mf_sub",
        },
    }
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    record = {
        "contract_execution_id": "cex-failed-qa-fresh-dispatch",
        "contract_id": "mf_parallel.v2",
        "project_id": "project-failed-qa-fresh-dispatch",
        "backlog_id": "backlog-failed-qa-fresh-dispatch",
        "execution_state_revision": 11,
        "completed_lines": [historical_dispatch, failed_qa],
    }

    class _SingleCasStore:
        def __init__(self):
            self.record = deepcopy(record)
            self.update_calls = 0

        def get(self, _contract_execution_id):
            return deepcopy(self.record)

        def update(
            self,
            _contract_execution_id,
            updated,
            *,
            expected_revision=None,
        ):
            self.update_calls += 1
            assert expected_revision == self.record["execution_state_revision"]
            self.record = deepcopy(updated)
            return deepcopy(self.record)

    store = _SingleCasStore()
    runtime = object.__new__(ContractRuntime)
    runtime.store = store

    def _record_view(source, *, actor_role=None, completed_lines=None, **_kwargs):
        view = deepcopy(dict(source))
        view["completed_lines"] = deepcopy(list(completed_lines or []))
        view["runtime_guide"] = {
            "next_legal_action": {
                "stage_id": "worker_read",
                "line_id": "worker_read_runtime_guide",
                "owner_role": "mf_sub",
                "evidence_kind": "read_receipt",
            }
        }
        view["precheck_decision"] = WriteGateDecision(ok=True).to_dict()
        return view

    runtime._record_view = _record_view
    identity = {
        "runtime_context_id": "mfrctx-replacement",
        "task_id": "worker-replacement",
        "parent_task_id": "cex-failed-qa-fresh-dispatch",
        "worker_id": "worker-replacement",
        "worker_slot_id": "worker-replacement",
        "owned_files": [
            "agent/governance/server.py",
            "agent/tests/test_graph_governance_api.py",
        ],
        "route_token_ref": "rtok-replacement",
    }
    write = {
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "actor_role": "observer",
        "evidence_kind": "dispatch_bounded_worker",
        **identity,
        "payload": {
            **identity,
            "worker_role": "mf_sub",
            "failed_qa_rework_dispatch_revision_authority": {
                "source": "parallel_branch_allocate_failed_qa_rework",
                "server_derived": True,
                "contract_execution_id": record["contract_execution_id"],
                "failed_qa_completed_line_index": 1,
                "runtime_context_id": identity["runtime_context_id"],
                "task_id": identity["task_id"],
            },
        },
    }

    result = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )

    assert result["ok"] is True
    assert result["status"] == "revised"
    assert result["append_only_history_preserved"] is True
    assert store.update_calls == 1
    assert store.record["execution_state_revision"] == 12
    assert len(store.record["completed_lines"]) == 3
    assert store.record["completed_lines"][0] == historical_dispatch
    replacement = store.record["completed_lines"][-1]
    assert replacement["runtime_context_id"] == "mfrctx-replacement"
    assert replacement["payload"]["failed_qa_rework_dispatch_revision"][
        "timeline_projection_authoritative"
    ] is False

    exact_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )
    assert exact_replay["ok"] is True
    assert exact_replay["status"] == "already_completed"
    assert exact_replay["contract_runtime_line_mutated"] is False
    assert store.update_calls == 1
    assert len(store.record["completed_lines"]) == 3
    assert store.record["execution_state_revision"] == 12

    preserved_first_cycle = deepcopy(store.record["completed_lines"])
    later_failed_qa = deepcopy(failed_qa)
    later_failed_qa["payload"]["acceptance_failed"] = [
        "second_rework_generation_required"
    ]
    store.record["completed_lines"].extend(
        [
            {
                "stage_id": "worker_commit",
                "line_id": "worker_commit",
                "actor_role": "mf_sub",
                "evidence_kind": "worker_commit",
                "commit_sha": "a" * 40,
            },
            later_failed_qa,
        ]
    )
    store.record["execution_state_revision"] = 13

    stale_cycle_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )
    assert stale_cycle_replay["ok"] is False
    assert any(
        "active failed QA line" in error
        for error in stale_cycle_replay["decision"]["errors"]
    )
    assert store.update_calls == 1
    assert store.record["completed_lines"][:3] == preserved_first_cycle

    second_write = deepcopy(write)
    second_authority = second_write["payload"][
        "failed_qa_rework_dispatch_revision_authority"
    ]
    second_authority["failed_qa_completed_line_index"] = 4
    second_result = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert second_result["ok"] is True
    assert second_result["status"] == "revised"
    assert store.update_calls == 2
    assert store.record["completed_lines"][:3] == preserved_first_cycle
    second_exact_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert second_exact_replay["ok"] is True
    assert second_exact_replay["status"] == "already_completed"
    assert store.update_calls == 2

    duplicate_current_authority = deepcopy(
        store.record["completed_lines"][-1]
    )
    store.record["completed_lines"].append(duplicate_current_authority)
    multiple_current_authorities = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert multiple_current_authorities["ok"] is False
    assert any(
        "multiple current-cycle" in error
        for error in multiple_current_authorities["decision"]["errors"]
    )
    assert store.update_calls == 2
