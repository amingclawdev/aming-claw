from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

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
    assert result["health_picture"]["semantic_coverage_ratio"] == 0
    assert result["health_picture"]["doc_coverage_ratio"] == 1
    assert result["health_picture"]["test_coverage_ratio"] == 1
    assert result["graph_query_trace"]["trace"]["status"] == "complete"
    assert Path(result["report_path"]).exists()

    snapshot = store.get_graph_snapshot(conn, PID, snapshot_id)
    notes = json.loads(snapshot["notes"])
    assert notes["global_semantic_review"]["latest_full_status"] == "reviewed"


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
        actor="observer",
        run_id="full-picture-ai",
    )

    assert calls[0]["stage"] == "reconcile_global_semantic_review"
    assert calls[0]["payload"]["mode"] == "full_global_semantic_review"
    assert result["global_ai_review"]["requested"] is True
    assert result["global_ai_review"]["open_issue_count"] == 1


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
