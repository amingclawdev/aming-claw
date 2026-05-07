import json

from agent.governance.chain_graph_context import (
    build_reconcile_graph_preflight,
    get_graph_doc_associations,
    get_related_nodes,
    resolve_context,
)


def _graph(nodes, links=None):
    return {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "links": links or [],
    }


def test_reconcile_context_uses_candidate_and_overlay_docs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "foo.md").write_text("# Foo\n", encoding="utf-8")
    (tmp_path / "docs" / "foo-approved.md").write_text("# Foo approved\n", encoding="utf-8")

    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "deps_graph": _graph([
            {"id": "L3.1", "title": "Subsystem", "primary": []},
            {
                "id": "L7.1",
                "title": "Foo",
                "primary": ["agent/foo.py"],
                "secondary": ["docs/foo.md"],
                "test_coverage": {"test_files": ["agent/tests/test_foo.py"]},
            },
        ], [{"source": "L3.1", "target": "L7.1", "relation": "contains"}]),
    }), encoding="utf-8")

    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({
        "nodes": {
            "L7.10": {
                "node_id": "L7.10",
                "primary": ["agent/foo.py"],
                "secondary": ["docs/foo-approved.md"],
            },
        },
    }), encoding="utf-8")

    metadata = {
        "operation_type": "reconcile-cluster",
        "target_files": ["agent/foo.py"],
        "cluster_payload": {"candidate_graph_path": str(candidate)},
        "overlay_path": str(overlay),
    }

    docs = get_graph_doc_associations(
        "test-project", ["agent/foo.py"],
        metadata=metadata, workspace_root=tmp_path,
    )
    assert docs == ["docs/foo-approved.md", "docs/foo.md"]
    assert get_related_nodes("test-project", ["agent/foo.py"], metadata=metadata) == ["L7.1"]

    preflight = build_reconcile_graph_preflight(
        "test-project", metadata,
        proposed_nodes=[{"primary": ["agent/foo.py"]}],
    )
    assert preflight["mode"] == "reconcile_session"
    assert preflight["target_node_ids"] == ["L7.1"]
    assert preflight["related_docs"] == ["docs/foo-approved.md", "docs/foo.md"]
    assert preflight["related_tests"] == ["agent/tests/test_foo.py"]
    assert preflight["coverage"][0]["doc_status"] == "covered"
    assert preflight["coverage"][0]["test_status"] == "covered"


def test_reconcile_context_ignores_none_sentinel_test_values(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "foo.md").write_text("# Foo\n", encoding="utf-8")
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "deps_graph": _graph([{
            "id": "L7.1",
            "title": "Foo",
            "primary": ["agent/foo.py"],
            "secondary": ["docs/foo.md"],
            "test": "none",
        }]),
    }), encoding="utf-8")
    metadata = {
        "operation_type": "reconcile-cluster",
        "target_files": ["agent/foo.py"],
        "cluster_payload": {"candidate_graph_path": str(candidate)},
    }

    preflight = build_reconcile_graph_preflight(
        "test-project", metadata,
        proposed_nodes=[{"primary": ["agent/foo.py"]}],
    )

    assert preflight["related_tests"] == []
    assert preflight["coverage"][0]["test_status"] == "missing"


def test_active_context_prefers_graph_snapshot_store(tmp_path, monkeypatch):
    from agent.governance.db import get_connection
    from agent.governance.graph_snapshot_store import (
        activate_graph_snapshot,
        create_graph_snapshot,
    )

    project_id = "dual-read-project"
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "foo.md").write_text("# Foo\n", encoding="utf-8")

    conn = get_connection(project_id)
    try:
        snapshot = create_graph_snapshot(
            conn,
            project_id,
            snapshot_id="imported-abc1234-dual",
            commit_sha="abc1234",
            snapshot_kind="imported",
            graph_json={
                "deps_graph": _graph([
                    {
                        "id": "L7.1",
                        "title": "Foo",
                        "primary": ["agent/foo.py"],
                        "secondary": ["docs/foo.md"],
                    }
                ])
            },
        )
        activate_graph_snapshot(conn, project_id, snapshot["snapshot_id"])
        conn.commit()
    finally:
        conn.close()

    context = resolve_context(project_id)
    assert context.mode == "active_snapshot"
    assert context.source == "graph_snapshot_store"
    assert context.snapshot_id == "imported-abc1234-dual"
    assert context.commit_sha == "abc1234"

    assert get_related_nodes(project_id, ["agent/foo.py"]) == ["L7.1"]
    docs = get_graph_doc_associations(
        project_id,
        ["agent/foo.py"],
        workspace_root=tmp_path,
    )
    assert docs == ["docs/foo.md"]


def test_active_context_falls_back_to_legacy_graph_json(tmp_path, monkeypatch):
    project_id = "legacy-project"
    shared = tmp_path / "shared"
    graph_dir = shared / "codex-tasks" / "state" / "governance" / project_id
    graph_dir.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "bar.md").write_text("# Bar\n", encoding="utf-8")
    (graph_dir / "graph.json").write_text(json.dumps({
        "deps_graph": _graph([
            {
                "id": "L7.2",
                "title": "Bar",
                "primary": ["agent/bar.py"],
                "secondary": ["docs/bar.md"],
            }
        ])
    }), encoding="utf-8")
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(shared))
    monkeypatch.setattr(
        "agent.governance.chain_graph_context._active_snapshot_context",
        lambda _project_id: None,
    )

    context = resolve_context(project_id)
    assert context.mode == "active_legacy"
    assert context.source == "legacy_graph_json"
    assert get_related_nodes(project_id, ["agent/bar.py"]) == ["L7.2"]
    docs = get_graph_doc_associations(
        project_id,
        ["agent/bar.py"],
        workspace_root=tmp_path,
    )
    assert docs == ["docs/bar.md"]
