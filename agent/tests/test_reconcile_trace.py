from agent.governance.reconcile_trace import ReconcileTrace


def test_trace_fallback_reason_summary_ignores_fallback_count_keys(tmp_path):
    trace = ReconcileTrace(
        project_id="aming-claw",
        run_id="run-reconcile-trace-test",
        snapshot_id="snapshot-test",
        trace_dir=tmp_path / "trace",
    )

    trace.step(
        "scope_delta",
        output_payload={
            "strategy": "full_rebuild_fallback",
            "fallback_reason": "source_function_identity_changed",
            "filetree_fallback_node_count": 3,
            "phase_trace": {
                "steps": [
                    {
                        "name": "ast_candidate_scanning",
                        "elapsed_ms": 42,
                        "metrics": {
                            "filetree_fallback_node_count": 3,
                            "candidate_count": 11,
                        },
                    }
                ]
            },
        },
    )

    summary = trace.finalize()
    observability = summary["observability"]

    assert observability["fallback_reasons"] == {"source_function_identity_changed": 1}
    assert "3" not in observability["fallback_reasons"]


def test_trace_fallback_reasons_list_is_preserved(tmp_path):
    trace = ReconcileTrace(
        project_id="aming-claw",
        run_id="run-reconcile-trace-list-test",
        snapshot_id="snapshot-test",
        trace_dir=tmp_path / "trace",
    )

    trace.step(
        "scope_delta",
        output_payload={
            "fallback_reasons": [
                "source_function_identity_changed",
                "inventory_status_change_requires_full_rebuild",
            ],
        },
    )

    summary = trace.finalize()

    assert summary["observability"]["fallback_reasons"] == {
        "inventory_status_change_requires_full_rebuild": 1,
        "source_function_identity_changed": 1,
    }
