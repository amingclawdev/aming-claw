from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.governance.governance_hints import (
    GovernanceHintCASMismatch,
    apply_binding_hints_to_graph_nodes,
    audit_governance_hint_bindings,
    governance_hint_source_sha256,
    governance_hints_envelope_sha256,
    mutate_governance_hint_file,
    mutate_governance_hint_text,
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

    assert summary["hint_count"] == 2
    assert summary["effective_hint_count"] == 1
    assert summary["applied_count"] == 0
    assert summary["removed_count"] == 0
    assert nodes[0]["secondary"] == []


def test_json_governance_hints_parse_only_reserved_versioned_root():
    event = {
        "schema_version": "asset_binding_event.v1",
        "operation": "bind",
        "path": ".",
        "role": "config",
        "target_module": "agent.governance.contracts.registry",
    }
    valid = json.dumps({
        "schema_version": "contract_definition.v1",
        "governance_hints": {
            "schema_version": "governance_hints.v1",
            "asset_binding_events": [event],
        },
    })

    hints = parse_governance_hint_bindings(
        valid,
        source_path="agent/governance/contract_definitions/demo.json",
    )

    assert len(hints) == 1
    assert hints[0].path == "agent/governance/contract_definitions/demo.json"
    assert hints[0].field == "config"
    assert hints[0].target_module == "agent.governance.contracts.registry"
    assert parse_governance_hint_bindings(
        json.dumps({"asset_binding_events": [event]}),
        source_path="demo.json",
    ) == []
    assert parse_governance_hint_bindings(
        json.dumps({"governanceHints": {"asset_binding_events": [event]}}),
        source_path="demo.json",
    ) == []
    assert parse_governance_hint_bindings(
        json.dumps({"metadata": {"governance_hints": {"asset_binding_events": [event]}}}),
        source_path="demo.json",
    ) == []
    assert parse_governance_hint_bindings("{not-json", source_path="demo.json") == []


def test_json_governance_hint_mutation_is_deterministic_idempotent_and_cas_bound():
    source_path = "config/service.json"
    original = '{"business":{"governance_hints":{"mode":"business"}},"enabled":true}\n'
    event = {
        "path": source_path,
        "role": "config",
        "target_module": "agent.governance.contracts.registry",
    }

    first = mutate_governance_hint_text(
        original,
        source_path=source_path,
        action="attach",
        event=event,
        expected_source_sha256=governance_hint_source_sha256(original),
        expected_envelope_sha256=governance_hints_envelope_sha256({}),
    )
    payload = json.loads(first["text"])

    assert first["changed"] is True
    assert first["text"].endswith("\n")
    assert not first["text"].endswith("\n\n")
    assert payload["enabled"] is True
    assert payload["business"] == {"governance_hints": {"mode": "business"}}
    assert payload["governance_hints"]["asset_binding_events"][0]["path"] == "."

    second = mutate_governance_hint_text(
        first["text"],
        source_path=source_path,
        action="attach",
        event=event,
        expected_envelope_sha256=first["envelope_sha256_after"],
    )
    assert second["changed"] is False
    assert second["text"] == first["text"]

    with pytest.raises(GovernanceHintCASMismatch, match="expected_source_sha256"):
        mutate_governance_hint_text(
            first["text"],
            source_path=source_path,
            action="unbind",
            event=event,
            expected_source_sha256=governance_hint_source_sha256(original),
        )
    with pytest.raises(GovernanceHintCASMismatch, match="expected_envelope_sha256"):
        mutate_governance_hint_text(
            first["text"],
            source_path=source_path,
            action="unbind",
            event=event,
            expected_envelope_sha256=governance_hints_envelope_sha256({}),
        )


def test_json_governance_hint_order_is_explicit_last_event_wins(tmp_path):
    project = tmp_path / "project"
    source_path = "config/service.json"
    event = {
        "path": ".",
        "role": "config",
        "target_module": "service.config",
    }
    attached = mutate_governance_hint_text(
        '{"enabled":true}\n',
        source_path=source_path,
        action="attach",
        event=event,
    )
    unbound = mutate_governance_hint_text(
        attached["text"],
        source_path=source_path,
        action="unbind",
        event=event,
    )
    _write(project / source_path, unbound["text"])
    nodes = [{
        "id": "L7.config",
        "title": "Config",
        "config": [source_path],
        "metadata": {"module": "service.config"},
    }]

    summary = apply_binding_hints_to_graph_nodes(project, nodes)

    assert [hint.operation for hint in parse_governance_hint_bindings(
        unbound["text"], source_path=source_path
    )] == ["bind", "unbind"]
    assert summary["effective_hint_count"] == 1
    assert summary["removed_count"] == 1
    assert nodes[0]["config"] == []


def test_json_governance_hint_file_dry_run_uses_same_plan_without_writing(tmp_path):
    source = tmp_path / "config.json"
    original = '{"enabled":true}\n'
    source.write_text(original, encoding="utf-8")

    result = mutate_governance_hint_file(
        source,
        source_path="config.json",
        action="attach",
        event={
            "path": ".",
            "role": "config",
            "target_module": "service.registry",
        },
        dry_run=True,
    )

    assert result["changed"] is True
    assert result["written"] is False
    assert result["dry_run"] is True
    assert source.read_text(encoding="utf-8") == original


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
