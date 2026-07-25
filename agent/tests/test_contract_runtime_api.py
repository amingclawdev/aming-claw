from __future__ import annotations

import copy
from types import SimpleNamespace

from agent.governance import parallel_branch_runtime
from agent.governance import graph_snapshot_store
from agent.governance import server
from agent.governance.contracts.write_gate import (
    _validate_worker_receipt_hash_evidence,
)


def test_contract_write_gate_rejects_nested_worker_receipt_placeholders():
    errors: list[str] = []

    _validate_worker_receipt_hash_evidence(
        errors,
        {
            "evidence_kind": "read_receipt",
            "payload": {
                "read_receipt_hash": "sha256:valid-top-level",
                "contract_context_read_receipt": {
                    "schema_version": "contract_context_read_receipt.v1",
                    "event_kind": "contract_context_read_receipt",
                    "receipt_hash": "<worker-computed-read-receipt-hash>",
                },
            },
        },
    )

    assert errors == [
        (
            "payload.contract_context_read_receipt.receipt_hash requires a "
            "worker-computed non-placeholder sha256: value"
        )
    ]


def _audit_only_merge_line(*, status: str | None = None):
    candidate_commit = "a" * 40
    merged_commit = "b" * 40
    diagnostic_id = "AC-AUDIT-ONLY-DIAGNOSTIC"
    audit_authority = {
        "schema_version": (
            "contract_runtime.audit_only_no_pass_bypass_round_authority.v1"
        ),
        "server_derived": True,
        "db_verified": True,
        "qa_contract_runtime_verified": False,
        "canonical_contract_acceptance_recorded": False,
        "authoritative_pass_synthesized": False,
        "close_satisfying": False,
        "no_pass_claim": True,
        "overall_release_pass_claimed": False,
        "candidate_commit_sha": candidate_commit,
        "diagnostic_backlog_id": diagnostic_id,
        "diagnostic_status": "OPEN",
        "bypass_completed_line_index": 2,
        "qa_event_ref": "timeline:4",
    }
    durable = {
        "schema_version": (
            "contract_runtime.observer_merge_durable_authority.v1"
        ),
        "source": (
            "parallel_branch_merge_queue+task_timeline_merge+"
            "audit_only_no_pass_bypass_round"
        ),
        "server_derived": True,
        "db_verified": True,
        "project_id": "contract-runtime-api-test",
        "backlog_id": "AC-AUDIT-ONLY",
        "runtime_context_id": "mfrctx-audit-only",
        "task_id": "worker-audit-only",
        "parent_task_id": "cex-audit-only",
        "branch_head": candidate_commit,
        "merge_commit": merged_commit,
        "target_head_after_merge": merged_commit,
        "merge_gate_passed": True,
        "merge_queue_id": "mq-audit-only",
        "queue_item_id": "mqitem-audit-only",
        "queue_item_status": "merged",
        "qa_contract_runtime_verified": False,
        "qa_audit_only_no_pass_authority": audit_authority,
        "timeline_event_refs": ["timeline:5"],
        "merge_event_ref": "timeline:5",
    }
    line = {
        "stage_id": "observer_integration",
        "line_id": "observer_merge",
        "actor_role": "observer",
        "evidence_kind": "merge",
        "commit_sha": merged_commit,
        "payload": {
            "schema_version": "observer_merge.no_pass_exception.v1",
            "disposition": "audit_only_no_pass_bypass_recovery",
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
            "authoritative_pass_synthesized": False,
            "close_satisfying": False,
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "independent_qa_status": (
                "accepted_in_scope_no_pass_system_blocked"
            ),
            "system_diagnostics": [
                {"backlog_id": diagnostic_id, "status": "OPEN"}
            ],
            "system_diagnostics_open": [diagnostic_id],
            "qa_audit_only_no_pass_authority": audit_authority,
            "durable_merge_authority": durable,
        },
    }
    if status is not None:
        line["status"] = status
    return line, audit_authority


def test_audit_only_merge_missing_status_requires_trusted_recovery_mode():
    line, _audit = _audit_only_merge_line()

    assert not server._contract_runtime_server_derived_observer_merge_no_pass_line(
        line
    )
    assert server._contract_runtime_server_derived_observer_merge_no_pass_line(
        line,
        allow_missing_top_level_status=True,
    )

    for rejected_status in ("waived", "bypassed", "failed"):
        rejected = copy.deepcopy(line)
        rejected["status"] = rejected_status
        assert not (
            server._contract_runtime_server_derived_observer_merge_no_pass_line(
                rejected,
                allow_missing_top_level_status=True,
            )
        )

    forged = copy.deepcopy(line)
    forged["payload"]["durable_merge_authority"]["server_derived"] = False
    assert not server._contract_runtime_server_derived_observer_merge_no_pass_line(
        forged,
        allow_missing_top_level_status=True,
    )


def test_completed_merge_recovers_missing_status_only_after_canonical_acceptance(
    monkeypatch,
):
    project_id = "contract-runtime-api-test"
    line, audit_authority = _audit_only_merge_line()
    candidate_commit = "a" * 40
    merged_commit = "b" * 40
    qa_graph = {
        "stage_id": "qa",
        "line_id": "qa_graph_context",
        "actor_role": "qa",
        "evidence_kind": "graph_trace",
        "status": "accepted",
        "commit_sha": candidate_commit,
        "payload": {
            "graph_trace_evidence": {
                "db_verified": True,
                "verified_trace_ids": ["gqt-audit-only"],
                "candidate_commit_sha": candidate_commit,
            }
        },
    }
    bypass = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "contract_line_bypass",
        "status": "bypassed",
    }
    record = {
        "project_id": project_id,
        "backlog_id": "AC-AUDIT-ONLY",
        "contract_execution_id": "cex-audit-only",
        "contract_id": "mf_parallel.v2",
        "completed_lines": [
            {
                "stage_id": "dispatch",
                "line_id": "observer_dispatch_bounded_workers",
                "actor_role": "observer",
                "evidence_kind": "dispatch_bounded_worker",
            },
            qa_graph,
            bypass,
            line,
        ],
    }
    context = SimpleNamespace(
        runtime_context_id="mfrctx-audit-only",
        task_id="worker-audit-only",
        parent_task_id="cex-audit-only",
        backlog_id="AC-AUDIT-ONLY",
    )
    timeline_events = [
        {
            "id": 5,
            "event_kind": "live_merge",
            "phase": "merge",
            "commit_sha": merged_commit,
            "created_at": "2026-07-22T16:00:00Z",
            "payload": {},
        }
    ]
    durable_item = SimpleNamespace(
        task_id=context.task_id,
        status="merged",
        merge_commit=merged_commit,
        target_head_after_merge=merged_commit,
    )
    monkeypatch.setattr(
        parallel_branch_runtime,
        "get_merge_queue_item",
        lambda *_args, **_kwargs: durable_item,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_observer_merge_completed_round",
        lambda *_args, **_kwargs: {
            "qa_contract_runtime_verified": False,
            "qa_graph_completed_line_index": 1,
            "qa_audit_only_no_pass_authority": audit_authority,
        },
    )
    acceptance_calls = []

    def accepted(*_args, **kwargs):
        acceptance_calls.append(kwargs)
        return {"db_verified": True}

    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_line_acceptance",
        accepted,
    )

    authority = server._contract_runtime_completed_merge_authority(
        object(),
        project_id=project_id,
        record=record,
        context=context,
        timeline_events=timeline_events,
    )

    assert authority["authority_verified"] is True
    assert authority["no_pass_claim"] is True
    assert authority["overall_release_pass_claimed"] is False
    assert acceptance_calls == [
        {
            "project_id": project_id,
            "record": record,
            "completed_line_index": 3,
            "expected_line": line,
            "allow_missing_observer_merge_status": True,
        }
    ]

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: timeline_events,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        lambda *_args, merge, **_kwargs: dict(merge),
    )
    trusted_projection = server._contract_runtime_trusted_merge_projection(
        object(),
        project_id=project_id,
        record=record,
    )
    assert trusted_projection["authority_verified"] is True
    assert trusted_projection["no_pass_claim"] is True
    assert trusted_projection["overall_release_pass_claimed"] is False

    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_line_acceptance",
        lambda *_args, **_kwargs: {},
    )
    assert server._contract_runtime_completed_merge_authority(
        object(),
        project_id=project_id,
        record=record,
        context=context,
        timeline_events=timeline_events,
    ) == {}

    forged = copy.deepcopy(record)
    forged["completed_lines"][3]["payload"]["durable_merge_authority"][
        "db_verified"
    ] = False
    assert server._contract_runtime_completed_merge_authority(
        object(),
        project_id=project_id,
        record=forged,
        context=context,
        timeline_events=timeline_events,
    ) == {}


def test_trusted_merge_uses_one_coherent_worker_identity_after_close_ready(
    monkeypatch,
):
    project_id = "contract-runtime-api-test"
    contract_execution_id = "cex-f711"
    runtime_context_id = "mfrctx-f711"
    task_id = "worker-f711"
    context = SimpleNamespace(
        runtime_context_id=runtime_context_id,
        task_id=task_id,
        parent_task_id=contract_execution_id,
        backlog_id="AC-F711",
    )
    record = {
        "project_id": project_id,
        "backlog_id": context.backlog_id,
        "contract_execution_id": contract_execution_id,
        "runtime_guide": {
            # A contract-scoped close action is intentionally incomplete as a
            # worker identity and must not seed fields from completed lines.
            "next_legal_action": {
                "line_id": "observer_close_ready",
                "task_id": contract_execution_id,
            }
        },
        "completed_lines": [
            {
                "line_id": "observer_dispatch_bounded_workers",
                "payload": {"worker_count": 1},
            },
            {
                "line_id": "observer_merge",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": contract_execution_id,
            },
            {
                "line_id": "observer_reconcile",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "parent_task_id": contract_execution_id,
                },
            },
            {
                "line_id": "observer_close_ready",
                "task_id": contract_execution_id,
                "payload": {"status": "passed"},
            },
        ],
    }

    identity = server._contract_runtime_server_line_identity(record)
    assert identity == {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": contract_execution_id,
        "identity_status": "resolved",
        "identity_source_line_id": "observer_merge",
    }

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_authority",
        lambda *_args, **_kwargs: {
            "timeline_verified": True,
            "authority_verified": True,
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "parent_task_id": contract_execution_id,
            "merged_commit_sha": "b" * 40,
        },
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        lambda *_args, merge, **_kwargs: dict(merge),
    )

    projection = server._contract_runtime_trusted_merge_projection(
        object(),
        project_id=project_id,
        record=record,
    )
    assert projection["timeline_verified"] is True
    assert projection["authority_verified"] is True
    assert projection["runtime_context_id"] == runtime_context_id
    assert projection["task_id"] == task_id


def test_server_line_identity_rejects_ambiguous_single_line_scope(
    monkeypatch,
):
    record = {
        "project_id": "contract-runtime-api-test",
        "backlog_id": "AC-AMBIGUOUS-IDENTITY",
        "contract_execution_id": "cex-ambiguous",
        "completed_lines": [
            {
                "line_id": "observer_dispatch_bounded_workers",
                "payload": {"worker_count": 2},
            },
            {
                "line_id": "observer_merge",
                "payload": {
                    "primary": {
                        "runtime_context_id": "mfrctx-primary",
                        "task_id": "worker-primary",
                        "parent_task_id": "cex-ambiguous",
                    },
                    "conflicting": {
                        "runtime_context_id": "mfrctx-conflict",
                        "task_id": "worker-conflict",
                        "parent_task_id": "cex-ambiguous",
                    },
                },
            },
        ],
    }

    identity = server._contract_runtime_server_line_identity(record)
    assert identity["identity_status"] == "ambiguous"
    assert identity["runtime_context_id"] == ""
    assert identity["task_id"] == ""
    assert identity["identity_source_line_id"] == "observer_merge"

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [],
    )
    projection = server._contract_runtime_trusted_merge_projection(
        object(),
        project_id=record["project_id"],
        record=record,
    )
    assert projection["timeline_verified"] is False
    assert projection["identity_mismatches"] == [
        {
            "field": "server_line_identity",
            "expected": "one coherent runtime_context_id/task_id tuple",
            "actual": "ambiguous",
            "source_line_id": "observer_merge",
        }
    ]


def test_current_full_authority_uses_current_canonical_reconcile_target(
    monkeypatch,
):
    historical_merge_commit = "a" * 40
    current_canonical_commit = "b" * 40
    observed = {}

    def current_full_state(
        _conn,
        _project_id,
        merged_commit_sha,
        **kwargs,
    ):
        observed["merged_commit_sha"] = merged_commit_sha
        observed.update(kwargs)
        return {
            "db_verified": True,
            "active_snapshot_commit": current_canonical_commit,
            "active_snapshot_status": "active",
            "active_snapshot_verified": True,
            "reconcile_snapshot_verified": True,
            "current_canonical_commit_sha": current_canonical_commit,
            "reconciled_commit_sha": current_canonical_commit,
        }

    monkeypatch.setattr(
        graph_snapshot_store,
        "current_full_reconcile_state",
        current_full_state,
    )
    monkeypatch.setattr(
        server.project_service,
        "resolve_project_root",
        lambda *_args, **_kwargs: "/canonical/project",
    )
    monkeypatch.setattr(
        server,
        "_git_head_commit",
        lambda _root: current_canonical_commit,
    )
    monkeypatch.setattr(
        server,
        "_git_commit_is_ancestor",
        lambda _root, ancestor, descendant: (
            ancestor == historical_merge_commit
            and descendant == current_canonical_commit
        ),
    )
    merge = {
        "timeline_verified": True,
        "merged_commit_sha": historical_merge_commit,
        "runtime_context_id": "mfrctx-current-canonical",
        "task_id": "worker-current-canonical",
        "parent_task_id": "cex-current-canonical",
        "merge_source_ref": "timeline:11",
        "merge_event_id": 11,
        "merge_event_created_at": "2026-07-22T10:01:00Z",
    }
    record = {
        "project_id": "contract-runtime-api-test",
        "backlog_id": "AC-CURRENT-CANONICAL",
        "contract_execution_id": "cex-current-canonical",
    }

    authority = server._contract_runtime_current_full_reconcile_authority_from_merge(
        object(),
        project_id="contract-runtime-api-test",
        record=record,
        merge=merge,
        reconcile={
            "reconcile_event_id": 12,
            "reconcile_event_created_at": "2026-07-22T10:02:00Z",
            "reconcile_task_id": "worker-current-canonical",
            "reconcile_runtime_context_id": "mfrctx-current-canonical",
        },
    )

    assert observed["merged_commit_sha"] == historical_merge_commit
    assert observed["current_canonical_commit_sha"] == current_canonical_commit
    assert authority["merged_commit_sha"] == historical_merge_commit
    assert authority["reconciled_commit_sha"] == current_canonical_commit
    assert authority["canonical_head_equals_merged_commit"] is False
    assert authority["reconciled_commit_is_ancestor_of_canonical_head"] is True
    assert authority["active_snapshot_matches_canonical_head"] is True
    assert authority["active_snapshot_verified"] is True
    assert authority["graph_reconciled"] is True


def test_current_full_authority_keeps_completed_reconcile_after_later_head(
    monkeypatch,
):
    historical_merge_commit = "a" * 40
    reconciled_commit = "b" * 40
    current_canonical_commit = "c" * 40
    ancestry_calls = []

    def current_full_state(
        _conn,
        _project_id,
        merged_commit_sha,
        **kwargs,
    ):
        assert merged_commit_sha == historical_merge_commit
        assert (
            kwargs["current_canonical_commit_sha"] == current_canonical_commit
        )
        return {
            "db_verified": True,
            "active_snapshot_commit": current_canonical_commit,
            "active_snapshot_status": "active",
            "active_snapshot_verified": True,
            "reconcile_snapshot_verified": True,
            "current_canonical_commit_sha": current_canonical_commit,
            "reconciled_commit_sha": reconciled_commit,
        }

    monkeypatch.setattr(
        graph_snapshot_store,
        "current_full_reconcile_state",
        current_full_state,
    )
    monkeypatch.setattr(
        server.project_service,
        "resolve_project_root",
        lambda *_args, **_kwargs: "/canonical/project",
    )
    monkeypatch.setattr(
        server,
        "_git_head_commit",
        lambda _root: current_canonical_commit,
    )

    def is_ancestor(_root, ancestor, descendant):
        ancestry_calls.append((ancestor, descendant))
        return (
            ancestor == reconciled_commit
            and descendant == current_canonical_commit
        )

    monkeypatch.setattr(server, "_git_commit_is_ancestor", is_ancestor)
    authority = (
        server._contract_runtime_current_full_reconcile_authority_from_merge(
            object(),
            project_id="contract-runtime-api-test",
            record={
                "backlog_id": "AC-COMPLETED-RECONCILE",
                "contract_execution_id": "cex-completed-reconcile",
            },
            merge={
                "timeline_verified": True,
                "merged_commit_sha": historical_merge_commit,
                "runtime_context_id": "mfrctx-completed-reconcile",
                "task_id": "worker-completed-reconcile",
                "parent_task_id": "cex-completed-reconcile",
                "merge_event_id": 11,
                "merge_event_created_at": "2026-07-22T10:01:00Z",
            },
            reconcile={
                "reconcile_event_id": 12,
                "reconcile_event_created_at": "2026-07-22T10:02:00Z",
                "reconcile_task_id": "worker-completed-reconcile",
                "reconcile_runtime_context_id": "mfrctx-completed-reconcile",
            },
        )
    )

    assert ancestry_calls == [(reconciled_commit, current_canonical_commit)]
    assert authority["merged_commit_sha"] == historical_merge_commit
    assert authority["reconciled_commit_sha"] == reconciled_commit
    assert authority["canonical_head_commit"] == current_canonical_commit
    assert authority["canonical_head_equals_merged_commit"] is False
    assert authority["canonical_head_equals_reconciled_commit"] is False
    assert authority["reconciled_commit_is_ancestor_of_canonical_head"] is True
    assert authority["graph_reconciled"] is True
