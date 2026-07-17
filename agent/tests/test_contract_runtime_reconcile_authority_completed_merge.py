from types import SimpleNamespace

from agent.governance import server


def test_completed_merge_joins_task_scoped_current_full_reconcile(monkeypatch):
    conn = object()
    runtime_context_id = "mfrctx-completed-merge-reconcile"
    task_id = "completed-merge-reconcile-worker"
    parent_task_id = "AC-COMPLETED-MERGE-RECONCILE"
    backlog_id = parent_task_id
    merged_commit = "a" * 40
    context = SimpleNamespace(
        runtime_context_id=runtime_context_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        backlog_id=backlog_id,
        batch_id="",
        merge_queue_id="mq-completed-merge-reconcile",
    )
    record = {
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_execution_id": "cex-completed-merge-reconcile",
        "completed_lines": [
            {
                "line_id": "observer_dispatch_bounded_workers",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": parent_task_id,
            }
        ],
    }
    merge_event = {
        "id": 50,
        "backlog_id": backlog_id,
        "task_id": task_id,
        "runtime_context_id": runtime_context_id,
        "event_kind": "merge",
        "phase": "merge",
        "actor": "observer-principal",
        "status": "passed",
        "commit_sha": merged_commit,
        "created_at": "2026-07-17T01:00:00Z",
        "payload": {
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "merge_commit": merged_commit,
        },
    }
    reconcile_event = {
        "id": 51,
        "backlog_id": backlog_id,
        "task_id": task_id,
        "runtime_context_id": runtime_context_id,
        "event_kind": "reconcile",
        "phase": "reconcile",
        "actor": "observer-principal",
        "status": "passed",
        "commit_sha": merged_commit,
        "created_at": "2026-07-17T01:01:00Z",
        "payload": {
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "target_commit_sha": merged_commit,
            "reconcile_mode": "current_full",
        },
    }
    completed_merge = {
        "timeline_verified": True,
        "authority_verified": True,
        "authority_source": "contract_runtime_completed_lines+durable_merge_queue",
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": backlog_id,
        "merged_commit_sha": merged_commit,
        "qa_event_id": 0,
        "qa_event_created_at": "",
        "qa_contract_runtime_verified": True,
        "merge_event_id": 50,
        "merge_event_created_at": merge_event["created_at"],
        "merge_source_ref": "timeline:50",
    }
    captured = {}

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: [merge_event, reconcile_event],
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_authority",
        lambda *_args, **_kwargs: completed_merge,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_projection_post_worker_timeline_events",
        lambda *_args, timeline_events, **_kwargs: list(timeline_events),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_line_evidence_policy",
        lambda *_args, **_kwargs: {
            "allow_taskless_reconcile_only_for_explicit_shared_batch": True
        },
    )

    def current_full_authority(
        _conn, *, project_id, record, merge, reconcile
    ):
        captured.update(
            project_id=project_id,
            record=record,
            merge=merge,
            reconcile=reconcile,
        )
        return {
            "db_verified": True,
            "live_verified": True,
            "active_snapshot_verified": True,
            "graph_reconciled": True,
            "reconcile_source_ref": reconcile["reconcile_source_ref"],
            "reconcile_event_id": reconcile["reconcile_event_id"],
            "reconcile_event_created_at": reconcile[
                "reconcile_event_created_at"
            ],
            "reconcile_task_id": reconcile["reconcile_task_id"],
            "reconcile_runtime_context_id": reconcile[
                "reconcile_runtime_context_id"
            ],
        }

    monkeypatch.setattr(
        server,
        "_contract_runtime_current_full_reconcile_authority_from_merge",
        current_full_authority,
    )

    authority = server._contract_runtime_trusted_merge_projection(
        conn,
        project_id="aming-claw",
        record=record,
    )

    assert authority["timeline_verified"] is True
    assert authority["qa_contract_runtime_verified"] is True
    assert authority["merge_event_id"] == 50
    assert authority["reconcile_source_ref"] == "timeline:51"
    assert authority["reconcile_event_id"] == 51
    assert authority["graph_reconciled"] is True
    assert captured["reconcile"]["reconcile_task_id"] == task_id
    assert (
        captured["reconcile"]["reconcile_runtime_context_id"]
        == runtime_context_id
    )
    assert captured["reconcile"]["allow_taskless_reconcile"] is False
