from unittest import mock


def _capture_persisted_events(monkeypatch):
    from agent.governance import chain_context

    store = chain_context.get_store()
    captured = []

    def capture(root_task_id, task_id, event_type, payload, project_id):
        captured.append(
            {
                "root_task_id": root_task_id,
                "task_id": task_id,
                "event_type": event_type,
                "payload": payload,
                "project_id": project_id,
            }
        )

    monkeypatch.setattr(store, "_persist_event", capture)
    return captured


def _delta(count=5):
    return {
        "creates": [
            {
                "node_id": f"L7.{idx}",
                "parent_layer": "L7",
                "title": f"Candidate {idx}",
                "primary": [f"agent/candidate_{idx}.py"],
                "deps": [],
            }
            for idx in range(1, count + 1)
        ],
        "updates": [],
        "links": [],
    }


def test_reconcile_v2_passthrough_skips_auto_inferrer(monkeypatch):
    from agent.governance import auto_chain

    events = _capture_persisted_events(monkeypatch)
    graph_delta = _delta(5)
    result = {"graph_delta": graph_delta, "changed_files": ["agent/reconcile.py"]}
    metadata = {"chain_id": "pm-root"}

    with mock.patch("agent.governance.auto_chain._infer_graph_delta") as infer:
        auto_chain._emit_or_infer_graph_delta(
            "aming-claw",
            "task-reconcile-dev",
            result,
            metadata,
            task_type="reconcile_v2",
        )

    infer.assert_not_called()
    proposed = [e for e in events if e["event_type"] == "graph.delta.proposed"]
    assert len(proposed) == 1
    assert proposed[0]["payload"]["source"] == "reconcile-derived"
    assert proposed[0]["payload"]["graph_delta"] == graph_delta


def test_reconcile_task_without_underscore_also_bypasses(monkeypatch):
    from agent.governance import auto_chain

    events = _capture_persisted_events(monkeypatch)
    graph_delta = _delta(1)

    with mock.patch("agent.governance.auto_chain._infer_graph_delta") as infer:
        auto_chain._emit_or_infer_graph_delta(
            "aming-claw",
            "task-reconcile",
            {"graph_delta": graph_delta},
            {"chain_id": "pm-root"},
            task_type="reconcile",
        )

    infer.assert_not_called()
    proposed = [e for e in events if e["event_type"] == "graph.delta.proposed"]
    assert proposed[0]["payload"]["source"] == "reconcile-derived"
    assert proposed[0]["payload"]["graph_delta"] == graph_delta


def test_regular_dev_task_still_runs_auto_inferrer(monkeypatch):
    from agent.governance import auto_chain

    events = _capture_persisted_events(monkeypatch)

    mock_conn = mock.MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None
    inferred = _delta(1)

    def fake_infer(pm_nodes, changed_files, dev_delta, dev_result, prd_declarations=None):
        assert changed_files == ["agent/new_module.py"]
        return inferred, [{"rule": "J"}], ["src_module_binding"], "auto-inferred"

    with mock.patch("agent.governance.db.get_connection", return_value=mock_conn), \
            mock.patch("agent.governance.auto_chain._infer_graph_delta", side_effect=fake_infer) as infer:
        auto_chain._emit_or_infer_graph_delta(
            "aming-claw",
            "task-dev",
            {"changed_files": ["agent/new_module.py"]},
            {"chain_id": "pm-root"},
            task_type="dev",
        )

    assert infer.call_count == 1
    proposed = [e for e in events if e["event_type"] == "graph.delta.proposed"]
    assert proposed[0]["payload"]["source"] == "auto-inferred"
    assert proposed[0]["payload"]["graph_delta"] == inferred
