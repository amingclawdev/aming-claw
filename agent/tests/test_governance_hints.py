from __future__ import annotations

from pathlib import Path

from agent.governance.governance_hints import apply_binding_hints_to_graph_nodes


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_governance_hint_attaches_unbound_doc_to_target_node(tmp_path):
    project = tmp_path / "project"
    _write(
        project / "docs" / "orphan.md",
        "<!-- governance-hint\n"
        '{"attach_to_node":{"target_module":"src.demo_app.service","role":"doc"}}'
        "\n-->\n# Service Notes\n",
    )
    nodes = [
        {
            "id": "L7.service",
            "title": "Demo Service",
            "primary": ["src/demo_app/service.py"],
            "secondary": [],
            "metadata": {"module": "src.demo_app.service"},
        }
    ]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert summary["applied_count"] == 1
    assert nodes[0]["secondary"] == ["docs/orphan.md"]
    assert nodes[0]["metadata"]["governance_hint_bindings"] == [
        {
            "path": "docs/orphan.md",
            "field": "secondary",
            "source_path": "docs/orphan.md",
        }
    ]


def test_governance_hint_does_not_rebind_existing_doc(tmp_path):
    project = tmp_path / "project"
    _write(
        project / "docs" / "bound.md",
        "<!-- governance-hint\n"
        '{"attach_to_node":{"target_node_id":"L7.target","role":"doc"}}'
        "\n-->\n# Bound Elsewhere\n",
    )
    nodes = [
        {"id": "L7.current", "title": "Current", "secondary": ["docs/bound.md"]},
        {"id": "L7.target", "title": "Target", "secondary": []},
    ]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert summary["applied_count"] == 0
    assert nodes[0]["secondary"] == ["docs/bound.md"]
    assert nodes[1]["secondary"] == []
    assert summary["skipped"][0]["reason"] == "already_bound"


def test_governance_hint_defers_index_doc_container_binding(tmp_path):
    project = tmp_path / "project"
    _write(
        project / "README.md",
        "<!-- governance-hint\n"
        '{"attach_to_node":{"target_node_id":"L7.target","role":"doc"}}'
        "\n-->\n# Project Index\n",
    )
    nodes = [{"id": "L7.target", "title": "Target", "secondary": []}]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert summary["applied_count"] == 0
    assert nodes[0]["secondary"] == []
    assert summary["skipped"][0]["reason"] == "index_doc_deferred"
