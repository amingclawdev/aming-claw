from __future__ import annotations

from pathlib import Path

from agent.governance.governance_hints import (
    apply_binding_hints_to_graph_nodes,
    audit_governance_hint_bindings,
    parse_governance_hint_bindings,
    render_governance_hint_comment,
    rewrite_governance_hint_text,
)


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
            "operation": "bind",
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


def test_governance_hint_unbind_is_append_only_tombstone(tmp_path):
    project = tmp_path / "project"
    _write(
        project / "docs" / "bound.md",
        "<!-- governance-hint "
        '{"asset_binding_event":{"operation":"unbind","path":"docs/bound.md",'
        '"role":"doc","target_node_id":"L7.target","reason":"stale binding"}}'
        " -->\n# Bound Elsewhere\n",
    )
    nodes = [
        {"id": "L7.target", "title": "Target", "secondary": ["docs/bound.md"]},
    ]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert summary["removed_count"] == 1
    assert nodes[0]["secondary"] == []
    assert nodes[0]["metadata"]["governance_hint_bindings"] == [
        {
            "operation": "unbind",
            "path": "docs/bound.md",
            "field": "secondary",
            "source_path": "docs/bound.md",
        }
    ]


def test_governance_hint_replays_bind_then_unbind_deterministically(tmp_path):
    project = tmp_path / "project"
    _write(
        project / "docs" / "service.md",
        "<!-- governance-hint "
        '{"asset_binding_event":{"operation":"bind","path":"docs/service.md",'
        '"role":"doc","target_node_id":"L7.target"}}'
        " -->\n"
        "<!-- governance-hint "
        '{"asset_binding_event":{"operation":"unbind","path":"docs/service.md",'
        '"role":"doc","target_node_id":"L7.target","reason":"wrong node"}}'
        " -->\n# Service\n",
    )
    nodes = [{"id": "L7.target", "title": "Target", "secondary": []}]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert summary["applied_count"] == 1
    assert summary["removed_count"] == 1
    assert nodes[0]["secondary"] == []


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


def test_governance_hint_parses_rendered_line_comment():
    payload = {"attach_to_node": {"path": "tests/test_service.py", "role": "test", "target_node_id": "L7.1"}}
    comment = render_governance_hint_comment("tests/test_service.py", payload)

    hints = parse_governance_hint_bindings(comment, source_path="tests/test_service.py")

    assert comment.startswith("# governance-hint ")
    assert hints[0].path == "tests/test_service.py"
    assert hints[0].field == "test"
    assert hints[0].target_node_id == "L7.1"


def test_governance_hint_render_rejects_json_comment_write():
    assert render_governance_hint_comment("config/settings.json", {"attach_to_node": {}}) == ""


def test_governance_hint_prefers_stable_module_over_stale_node_id(tmp_path):
    project = tmp_path / "project"
    _write(
        project / "docs" / "service.md",
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/service.md","role":"doc",'
        '"target_node_id":"L7.old","target_module":"src.demo_app.service"}}'
        " -->\n# Service\n",
    )
    nodes = [
        {
            "id": "L7.old",
            "title": "Different Feature",
            "primary": ["src/demo_app/other.py"],
            "secondary": [],
            "metadata": {"module": "src.demo_app.other"},
        },
        {
            "id": "L7.new",
            "title": "Demo Service",
            "primary": ["src/demo_app/service.py"],
            "secondary": [],
            "metadata": {"module": "src.demo_app.service"},
        },
    ]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert summary["applied_count"] == 1
    assert nodes[0]["secondary"] == []
    assert nodes[1]["secondary"] == ["docs/service.md"]
    assert summary["applied"][0]["target_node_id"] == "L7.new"


def test_governance_hint_uses_exact_node_id_when_stable_module_is_duplicate(tmp_path):
    project = tmp_path / "project"
    _write(
        project / "docs" / "widget.md",
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/widget.md","role":"doc",'
        '"target_node_id":"L7.ts","target_module":"web.widget","target_title":"web.widget"}}'
        " -->\n# Widget\n",
    )
    nodes = [
        {
            "id": "L7.ts",
            "title": "web.widget",
            "primary": ["web/widget.ts"],
            "secondary": [],
            "metadata": {"module": "web.widget"},
        },
        {
            "id": "L7.js",
            "title": "web.widget",
            "primary": ["web/widget.js"],
            "secondary": [],
            "metadata": {"module": "web.widget"},
        },
    ]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert summary["applied_count"] == 1
    assert nodes[0]["secondary"] == ["docs/widget.md"]
    assert nodes[1]["secondary"] == []
    assert summary["applied"][0]["target_node_id"] == "L7.ts"


def test_governance_hint_audit_reports_node_id_only_and_target_conflict():
    hints = parse_governance_hint_bindings(
        "\n".join([
            "<!-- governance-hint "
            '{"attach_to_node":{"path":"docs/node-only.md","role":"doc","target_node_id":"L7.old"}}'
            " -->",
            "<!-- governance-hint "
            '{"attach_to_node":{"path":"docs/conflict.md","role":"doc",'
            '"target_node_id":"L7.old","target_module":"src.demo_app.service"}}'
            " -->",
        ]),
        source_path="docs/hints.md",
    )
    nodes = [
        {
            "id": "L7.old",
            "title": "Old Feature",
            "primary": ["src/demo_app/old.py"],
            "metadata": {"module": "src.demo_app.old"},
        },
        {
            "id": "L7.new",
            "title": "Demo Service",
            "primary": ["src/demo_app/service.py"],
            "metadata": {"module": "src.demo_app.service"},
        },
    ]

    audit = audit_governance_hint_bindings(hints, nodes)

    statuses = {row["path"]: row["status"] for row in audit["items"]}
    assert statuses == {
        "docs/node-only.md": "node_id_only",
        "docs/conflict.md": "target_conflict",
    }
    assert audit["needs_repair_count"] == 2


def test_governance_hint_audit_reports_ambiguous_title_only_stable_target():
    hints = parse_governance_hint_bindings(
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/mf.md","role":"doc",'
        '"target_node_id":"L3.13","target_title":"Workflow Orchestration"}}'
        " -->",
        source_path="docs/mf.md",
    )
    nodes = [
        {
            "id": "L3.1",
            "title": "Workflow Orchestration",
            "metadata": {"area_key": "agent", "subsystem_key": "workflow_orchestration"},
        },
        {
            "id": "L3.13",
            "title": "Workflow Orchestration",
            "metadata": {"area_key": "agent.governance", "subsystem_key": "workflow_orchestration"},
        },
    ]

    audit = audit_governance_hint_bindings(hints, nodes)

    assert audit["items"][0]["status"] == "target_ambiguous"
    assert audit["items"][0]["needs_repair"] is True
    assert audit["needs_repair_count"] == 1


def test_governance_hint_rewrite_stabilizes_node_id_only_hint():
    text = (
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/service.md","role":"doc","target_node_id":"L7.1"}}'
        " -->\n# Service\n"
    )
    nodes = [
        {
            "id": "L7.1",
            "title": "Demo Service",
            "primary": ["src/demo_app/service.py"],
            "metadata": {"module": "src.demo_app.service"},
        }
    ]

    result = rewrite_governance_hint_text(
        text,
        source_path="docs/service.md",
        nodes=nodes,
        action="stabilize",
    )

    assert result["changed"] is True
    assert result["repaired_count"] == 1
    assert '"target_module": "src.demo_app.service"' in result["text"]
    assert '"target_title": "Demo Service"' in result["text"]


def test_governance_hint_rewrite_stabilizes_ambiguous_title_with_composite_identity():
    text = (
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/mf.md","role":"doc",'
        '"target_node_id":"L3.13","target_title":"Workflow Orchestration"}}'
        " -->\n# MF\n"
    )
    nodes = [
        {
            "id": "L3.1",
            "title": "Workflow Orchestration",
            "metadata": {"area_key": "agent", "subsystem_key": "workflow_orchestration"},
        },
        {
            "id": "L3.13",
            "title": "Workflow Orchestration",
            "metadata": {"area_key": "agent.governance", "subsystem_key": "workflow_orchestration"},
        },
    ]

    result = rewrite_governance_hint_text(
        text,
        source_path="docs/mf.md",
        nodes=nodes,
        action="stabilize",
    )

    assert result["changed"] is True
    assert result["repaired_count"] == 1
    assert '"target_area_key": "agent.governance"' in result["text"]
    assert '"target_subsystem_key": "workflow_orchestration"' in result["text"]
    assert '"target_node_id": "L3.13"' in result["text"]


def test_governance_hint_rewrite_withdraws_matching_hint_block():
    text = (
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/service.md","role":"doc","target_node_id":"L7.1"}}'
        " -->\n\n# Service\n"
    )

    result = rewrite_governance_hint_text(
        text,
        source_path="docs/service.md",
        nodes=[],
        action="withdraw",
        path="docs/service.md",
        role="doc",
    )

    assert result["changed"] is True
    assert result["withdrawn_count"] == 1
    assert "governance-hint" not in result["text"]
    assert result["text"].startswith("# Service")
