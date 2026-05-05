import json

from agent.governance.reconcile_doc_index import (
    build_final_doc_index,
    render_markdown,
    write_final_doc_index,
)


def _graph(nodes, links=None):
    return {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "links": links or [],
    }


def test_final_doc_index_reports_coverage_and_keeps_index_docs_nonblocking(tmp_path):
    candidate = tmp_path / "graph.rebase.candidate.json"
    candidate.write_text(json.dumps({
        "deps_graph": _graph([
            {"id": "L1.1", "title": "Runtime", "primary": []},
            {
                "id": "L7.1",
                "title": "Approved Feature",
                "primary": ["agent/feature_a.py"],
                "secondary": ["docs/feature-a.md"],
                "test_coverage": {"test_files": ["agent/tests/test_feature_a.py"]},
            },
            {
                "id": "L7.2",
                "title": "Unapproved Feature",
                "primary": ["agent/feature_b.py"],
            },
        ]),
    }), encoding="utf-8")
    overlay = tmp_path / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "nodes": {
            "L7.10": {
                "node_id": "L7.10",
                "title": "Approved Feature",
                "primary": ["agent/feature_a.py"],
                "secondary": ["docs/feature-a-approved.md"],
            },
        },
    }), encoding="utf-8")
    inventory = [
        {"path": "README.md", "file_kind": "doc", "scan_status": "orphan"},
        {"path": "docs/README.md", "file_kind": "doc", "scan_status": "orphan"},
        {"path": "agent/orphan.py", "file_kind": "source", "scan_status": "orphan", "reason": "not clustered"},
        {"path": "agent/tests/test_orphan.py", "file_kind": "test", "scan_status": "orphan", "reason": "not attached"},
    ]

    report = build_final_doc_index(
        project_id="test-project",
        session_id="sess-1",
        candidate_graph_path=candidate,
        overlay_path=overlay,
        file_inventory_rows=inventory,
    )

    assert report["summary"]["candidate_leaf_count"] == 2
    assert report["summary"]["approved_leaf_count"] == 1
    assert report["summary"]["missing_source_leaf_count"] == 1
    assert report["summary"]["unresolved_file_count"] == 2
    assert report["summary"]["index_doc_count"] == 2
    assert report["summary"]["ready_for_signoff"] is False
    assert "candidate_source_leaf_missing_from_overlay" in report["summary"]["blocking_issues"]
    assert {item["path"] for item in report["inventory"]["index_docs"]} == {"README.md", "docs/README.md"}
    assert {item["path"] for item in report["inventory"]["unresolved_files"]} == {
        "agent/orphan.py",
        "agent/tests/test_orphan.py",
    }

    approved = next(f for f in report["features"] if f["candidate_node_id"] == "L7.1")
    assert approved["docs"] == ["docs/feature-a-approved.md", "docs/feature-a.md"]
    assert approved["tests"] == ["agent/tests/test_feature_a.py"]


def test_final_doc_index_writes_json_and_markdown_ready_report(tmp_path):
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "deps_graph": _graph([
            {
                "id": "L7.1",
                "title": "Feature",
                "primary": ["agent/feature.py"],
                "secondary": ["docs/feature.md"],
                "test": ["agent/tests/test_feature.py"],
            }
        ]),
    }), encoding="utf-8")
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({
        "nodes": {
            "L7.1": {
                "node_id": "L7.1",
                "title": "Feature",
                "primary": ["agent/feature.py"],
                "secondary": ["docs/feature.md"],
                "test": ["agent/tests/test_feature.py"],
            },
        },
    }), encoding="utf-8")

    report = write_final_doc_index(
        project_id="test-project",
        session_id="sess-ready",
        candidate_graph_path=candidate,
        overlay_path=overlay,
        output_dir=tmp_path,
        file_inventory_rows=[{"path": "README.md", "file_kind": "doc", "scan_status": "orphan"}],
    )

    assert report["summary"]["ready_for_signoff"] is True
    assert (tmp_path / "graph.rebase.doc-index.review.json").exists()
    assert (tmp_path / "graph.rebase.doc-index.review.md").exists()
    markdown = render_markdown(report)
    assert "ready_for_signoff: `True`" in markdown
    assert "`README.md`" in markdown
