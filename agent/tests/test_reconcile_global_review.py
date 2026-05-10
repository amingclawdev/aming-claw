from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import reconcile_global_review
from agent.governance import server
from agent.governance.db import _ensure_schema


PID = "global-review-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(path_params: dict, *, method: str = "GET", query: dict | None = None, body: dict | None = None):
    return server.RequestContext(
        None,
        method,
        path_params,
        query or {},
        body or {},
        "req-test",
        "",
        "",
    )


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    yield c
    c.close()


@pytest.fixture()
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "feature.py").write_text(
        "def feature_entry():\n    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "agent" / "test_feature.py").write_text(
        "def test_feature_entry():\n    assert True\n",
        encoding="utf-8",
    )
    return root


def _graph(path: str = "agent/feature.py", node_id: str = "L7.1") -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L3.1",
                    "layer": "L3",
                    "title": "Feature Runtime",
                    "kind": "subsystem",
                    "metadata": {},
                },
                {
                    "id": node_id,
                    "layer": "L7",
                    "title": "Feature Entry",
                    "kind": "service_runtime",
                    "primary": [path],
                    "secondary": ["docs/feature.md"],
                    "test": ["agent/test_feature.py"],
                    "metadata": {"hierarchy_parent": "L3.1", "subsystem": "runtime"},
                },
            ],
            "edges": [
                {
                    "source": "L3.1",
                    "target": node_id,
                    "edge_type": "contains",
                    "direction": "hierarchy",
                    "evidence": {"source": "test"},
                }
            ],
        }
    }


def _create_scope_pair(conn: sqlite3.Connection, *, changed_path: str = "agent/feature.py") -> tuple[str, str]:
    base = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-base-global",
        commit_sha="base",
        snapshot_kind="scope",
        graph_json=_graph(changed_path),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        base["snapshot_id"],
        nodes=_graph(changed_path)["deps_graph"]["nodes"],
        edges=_graph(changed_path)["deps_graph"]["edges"],
    )
    notes = {
        "pending_scope_reconcile": {
            "active_snapshot_id": base["snapshot_id"],
            "scope_file_delta": {
                "changed_files": [changed_path],
                "impacted_files": [changed_path, "agent/test_feature.py"],
            },
        }
    }
    current = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-current-global",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=_graph(changed_path),
        notes=json.dumps(notes),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        current["snapshot_id"],
        nodes=_graph(changed_path)["deps_graph"]["nodes"],
        edges=_graph(changed_path)["deps_graph"]["edges"],
    )
    conn.commit()
    return base["snapshot_id"], current["snapshot_id"]


def test_incremental_global_review_queues_semantics_and_blocks_without_ai(conn, project):
    base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    result = reconcile_global_review.run_incremental_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        semantic_use_ai=False,
        actor="observer",
        run_id="scope-incremental-no-ai",
    )

    assert result["ok"] is True
    assert result["base_snapshot_id"] == base_snapshot_id
    assert result["changed_node_ids"] == ["L7.1"]
    assert result["blocked"] is True
    assert result["status"] == "blocked_semantic_pending"
    assert result["pending_node_ids"] == ["L7.1"]
    assert result["graph_query_trace"]["trace"]["status"] == "complete"

    row = conn.execute(
        """
        SELECT status FROM graph_semantic_jobs
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (PID, snapshot_id),
    ).fetchone()
    assert row["status"] == "ai_pending"

    snapshot = store.get_graph_snapshot(conn, PID, snapshot_id)
    notes = json.loads(snapshot["notes"])
    assert notes["global_semantic_review"]["latest_incremental_status"] == "blocked_semantic_pending"
    events = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["semantic_global_review_generated"],
    )
    assert len(events) == 1
    assert events[0]["status"] == graph_events.EVENT_STATUS_OBSERVED
    assert events[0]["payload"]["review_kind"] == "incremental"


def test_full_global_review_builds_health_picture_without_semantic_enrichment(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    result = reconcile_global_review.run_full_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        actor="observer",
        run_id="full-picture",
    )

    assert result["ok"] is True
    assert result["status"] == "reviewed"
    assert result["health_picture"]["feature_count"] == 1
    assert result["health_picture"]["semantic_complete_count"] == 0
    assert result["health_picture"]["semantic_pending_count"] == 1
    assert result["health_picture"]["semantic_coverage_ratio"] == 0
    assert result["health_picture"]["governance_observability_score"] == 0
    assert result["health_picture"]["doc_coverage_ratio"] == 1
    assert result["health_picture"]["test_coverage_ratio"] == 1
    assert result["health_picture"]["project_health_score"] == 100
    assert result["health_picture"]["average_health_score"] == 100
    assert result["health_picture"]["file_hygiene"]["available"] is False
    assert result["health_picture"]["file_hygiene_score"] == 100
    assert result["health_picture"]["low_health_count"] == 0
    assert result["graph_query_trace"]["trace"]["status"] == "complete"
    assert Path(result["report_path"]).exists()

    snapshot = store.get_graph_snapshot(conn, PID, snapshot_id)
    notes = json.loads(snapshot["notes"])
    assert notes["global_semantic_review"]["latest_full_status"] == "reviewed"
    events = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["semantic_global_review_generated"],
    )
    assert len(events) == 1
    assert events[0]["status"] == graph_events.EVENT_STATUS_OBSERVED
    assert events[0]["payload"]["review_kind"] == "full"


def test_full_global_review_uses_compact_trace_context_for_large_metadata(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)
    row = conn.execute(
        """
        SELECT metadata_json FROM graph_nodes_index
        WHERE project_id=? AND snapshot_id=? AND node_id='L3.1'
        """,
        (PID, snapshot_id),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    metadata["giant_unpromptable_blob"] = "x" * 30_000
    metadata["functions"] = [{"name": f"helper_{i}", "line": i} for i in range(1000)]
    conn.execute(
        """
        UPDATE graph_nodes_index
        SET metadata_json=?
        WHERE project_id=? AND snapshot_id=? AND node_id='L3.1'
        """,
        (json.dumps(metadata), PID, snapshot_id),
    )
    conn.commit()

    result = reconcile_global_review.run_full_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        actor="observer",
        run_id="full-picture-compact-trace",
        query_budget={"max_queries": 20, "max_result_nodes": 200, "max_result_chars": 20_000},
    )

    assert result["graph_query_trace"]["trace"]["status"] == "complete"
    subsystem_result = result["graph_query_trace"]["subsystems"]["subsystems"][0]
    assert "giant_unpromptable_blob" not in subsystem_result["metadata"]
    assert subsystem_result["metadata"]["function_count"] == 1000


def test_full_global_review_scores_project_health_from_existing_signals(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)
    reconcile_global_review.semantic._ensure_semantic_state_schema(conn)
    row = conn.execute(
        """
        SELECT metadata_json FROM graph_nodes_index
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (PID, snapshot_id),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    metadata["function_count"] = 35
    conn.execute(
        """
        UPDATE graph_nodes_index
        SET metadata_json=?
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (json.dumps(metadata), PID, snapshot_id),
    )
    semantic_json = {
        "status": "ai_complete",
        "doc_status": "weak",
        "test_status": "over_broad_relation",
        "config_status": "n/a",
        "quality_flags": ["review_required"],
        "open_issues": [
            {"reason": "merge_suggestions", "summary": "Possible duplicate capability."},
            {"reason": "split_suggestions", "summary": "Feature surface is too broad."},
            {"type": "test_add", "summary": "Add missing focused regression test."},
        ],
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:test', '{}', ?, 0, 0, 'now')
        """,
        (PID, snapshot_id, json.dumps(semantic_json)),
    )
    conn.commit()

    result = reconcile_global_review.run_full_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        actor="observer",
        run_id="full-picture-health-v3",
    )

    health = result["health_picture"]
    assert health["score_version"] == "project_health_v4_existing_data_plus_file_hygiene"
    assert health["semantic_coverage_ratio"] == 1
    assert health["governance_observability_score"] == 100
    assert health["artifact_binding_score"] == 90
    assert health["raw_project_health_score"] == 67
    assert health["project_health_score"] == 67
    assert health["project_health_issue_counts"]["high_function_count"] == 1
    assert health["project_health_issue_counts"]["duplicate_or_overlap_issue_reported"] == 1
    assert health["project_health_issue_counts"]["broad_responsibility_issue_reported"] == 1
    assert health["project_health_issue_counts"]["test_gap_issue_reported"] == 1
    assert health["low_health_nodes"][0]["issues"] == [
        "broad_responsibility_issue",
        "doc_status_weak",
        "duplicate_or_overlap_issue",
        "high_function_count",
        "review_required",
        "test_gap_issue",
        "test_status_over_broad",
    ]


def test_full_global_review_includes_file_hygiene_from_existing_inventory(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)
    snapshot = store.get_graph_snapshot(conn, PID, snapshot_id)
    notes = json.loads(snapshot["notes"])
    notes["run_id"] = "inventory-health-run"
    conn.execute(
        "UPDATE graph_snapshots SET notes=? WHERE project_id=? AND snapshot_id=?",
        (json.dumps(notes), PID, snapshot_id),
    )
    rows = [
        ("agent/orphan.py", "source", "orphan", "unmapped", "pending", 100),
        ("docs/orphan.md", "doc", "orphan", "unmapped", "pending", 200),
        ("scripts/check.ps1", "script", "pending_decision", "pending_decision", "pending", 300),
        ("docs/dev/scratch/ai-output.json", "generated", "ignored", "ignored", "ignore", 10 * 1024 * 1024),
    ]
    for path, kind, scan, graph, decision, size in rows:
        conn.execute(
            """
            INSERT INTO reconcile_file_inventory
              (project_id, run_id, path, file_kind, scan_status, graph_status,
               decision, size_bytes, updated_at)
            VALUES (?, 'inventory-health-run', ?, ?, ?, ?, ?, ?, 'now')
            """,
            (PID, path, kind, scan, graph, decision, size),
        )
    conn.commit()

    result = reconcile_global_review.run_full_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        actor="observer",
        run_id="full-picture-file-hygiene",
    )

    hygiene = result["health_picture"]["file_hygiene"]
    assert hygiene["available"] is True
    assert hygiene["run_id"] == "inventory-health-run"
    assert hygiene["orphan_count"] == 2
    assert hygiene["pending_decision_count"] == 1
    assert hygiene["cleanup_candidate_count"] == 1
    assert hygiene["cleanup_candidate_mb"] == 10
    assert hygiene["review_required_count"] == 3
    assert hygiene["review_required_sample"][0]["suggested_dashboard_actions"] == [
        "attach_to_node",
        "create_node",
        "delete_candidate",
        "waive",
    ]
    assert result["health_picture"]["file_hygiene_score"] == pytest.approx(92.22)
    assert result["health_picture"]["raw_project_health_score"] == 100
    assert result["health_picture"]["project_health_global_penalties"]["file_hygiene"] == pytest.approx(1.9)
    assert result["health_picture"]["project_health_score"] == pytest.approx(98.1)


def test_full_global_review_can_call_ai_once(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    calls: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        calls.append({"stage": stage, "payload": payload})
        return {
            "global_summary": "The graph picture is usable but semantic coverage is incomplete.",
            "open_issues": [
                {
                    "kind": "status_observation",
                    "message": "Semantic coverage is incomplete.",
                }
            ],
        }

    result = reconcile_global_review.run_full_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        global_review_use_ai=True,
        global_review_ai_call=fake_ai,
        classify_feedback=True,
        actor="observer",
        run_id="full-picture-ai",
    )

    assert calls[0]["stage"] == "reconcile_global_semantic_review"
    assert calls[0]["payload"]["mode"] == "full_global_semantic_review"
    assert result["global_ai_review"]["requested"] is True
    assert result["global_ai_review"]["open_issue_count"] == 1
    assert result["feedback_classification"]["count"] == 1
    assert result["feedback_classification"]["items"][0]["source_round"] == "global-full:full-picture-ai"


def test_incremental_global_review_requires_changed_semantics_when_global_state_exists(
    conn,
    project,
    monkeypatch,
):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    def fake_enrichment(*_args, **_kwargs):
        return {
            "ok": True,
            "summary": {
                "ai_complete_count": 0,
                "ai_selected_count": 1,
                "semantic_run_status": "ai_partial",
                "semantic_graph_state": {"hit_count": 44},
            },
        }

    monkeypatch.setattr(
        reconcile_global_review.semantic,
        "run_semantic_enrichment",
        fake_enrichment,
    )

    result = reconcile_global_review.run_incremental_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        semantic_use_ai=False,
        actor="observer",
        run_id="scope-incremental-global-state-only",
    )

    assert result["status"] == "blocked_semantic_pending"
    assert result["blocked"] is True
    assert result["pending_node_ids"] == ["L7.1"]
    assert result["review_gate"]["reason"] == "changed_semantic_pending"
    assert result["review_gate"]["semantic_summary_gate"]["allowed"] is True


def test_incremental_global_review_classifies_changed_ai_semantic_issues(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    def fake_ai(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_semantic_feature"
        return {
            "feature_name": "Feature Entry Runtime",
            "semantic_summary": "Owns the feature entry runtime.",
            "intent": "Provide the runtime entry point.",
            "domain_label": "runtime",
            "dependency_patch_suggestions": [
                {
                    "type": "add_typed_relation",
                    "target": "L3.1",
                    "reason": "Add a typed relation from the feature entry to the runtime subsystem.",
                }
            ],
        }

    result = reconcile_global_review.run_incremental_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        semantic_use_ai=True,
        semantic_ai_call=fake_ai,
        actor="observer",
        run_id="scope-incremental-ai",
    )

    assert result["blocked"] is False
    assert result["complete_node_ids"] == ["L7.1"]
    assert result["feedback_classification"]["count"] == 1
    item = result["feedback_classification"]["items"][0]
    assert item["feedback_kind"] == reconcile_feedback.KIND_GRAPH_CORRECTION
    assert item["source_node_ids"] == ["L7.1"]


def test_incremental_global_review_api(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    result = server.handle_graph_governance_snapshot_incremental_global_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot_id},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "semantic_mode": "manual",
                "use_ai": False,
                "run_id": "scope-incremental-api",
            },
        )
    )

    assert result["ok"] is True
    assert result["blocked"] is True
    assert result["graph_query_trace"]["trace_id"]


def test_full_global_review_api(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    result = server.handle_graph_governance_snapshot_full_global_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot_id},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "run_id": "full-picture-api",
            },
        )
    )

    assert result["ok"] is True
    assert result["health_picture"]["feature_count"] == 1
    assert result["graph_query_trace"]["trace_id"]


def test_incremental_global_review_stops_queries_after_budget_exceeded(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)

    result = reconcile_global_review.run_incremental_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        semantic_use_ai=False,
        actor="observer",
        run_id="scope-incremental-budget",
        query_budget={"max_queries": 1, "max_result_nodes": 1000, "max_result_chars": 100000},
    )

    assert result["ok"] is True
    assert result["graph_query_trace"]["trace"]["status"] == "budget_exceeded"
    assert any(query.get("skipped") for query in result["graph_query_trace"]["queries"])


def test_incremental_global_review_shortens_long_run_id_paths(conn, project):
    _base_snapshot_id, snapshot_id = _create_scope_pair(conn)
    long_run_id = "post-scope-incremental-review-" + ("very-long-segment-" * 12)

    result = reconcile_global_review.run_incremental_global_review(
        conn,
        PID,
        snapshot_id,
        project,
        semantic_use_ai=False,
        actor="observer",
        run_id=long_run_id,
    )

    assert result["run_id"] == long_run_id
    assert len(Path(result["report_path"]).name) < 100
    assert Path(result["report_path"]).name.endswith(".json")
