import json

from agent.governance.chain_graph_context import (
    build_reconcile_graph_preflight,
    get_graph_doc_associations,
    get_related_nodes,
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
