from __future__ import annotations

from pathlib import Path

from agent.governance import server


def _ctx(project_id: str):
    return server.RequestContext(
        None,
        "GET",
        {"project_id": project_id},
        {},
        {},
        "req-project-config-test",
        "",
        "",
    )


def _write_project_config(root: Path) -> None:
    (root / ".aming-claw.yaml").write_text(
        "\n".join([
            "project_id: dashboard-demo",
            "language: typescript",
            "testing:",
            "  unit_command: npm test",
            "governance:",
            "  enabled: true",
            "  test_tool_label: vitest",
            "  exclude_roots:",
            "    - examples",
            "graph:",
            "  exclude_paths:",
            "    - docs/dev",
            "  nested_projects:",
            "    mode: exclude",
            "    roots:",
            "      - examples/demo",
            "ai:",
            "  routing:",
            "    pm:",
            "      provider: openai",
            "      model: gpt-5.5",
            "    semantic:",
            "      provider: anthropic",
            "      model: claude-opus-4-7",
            "",
        ]),
        encoding="utf-8",
    )


def test_project_config_endpoint_exposes_governance_exclude_roots(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "dashboard-demo",
            "workspace_path": str(tmp_path),
            "status": "active",
        }],
    )

    payload = server.handle_project_config(_ctx("dashboard-demo"))

    assert payload["project_id"] == "dashboard-demo"
    assert payload["language"] == "typescript"
    assert payload["governance"]["exclude_roots"] == ["examples"]
    assert payload["graph"]["exclude_paths"] == ["docs/dev"]
    assert payload["graph"]["effective_exclude_roots"] == [
        "examples",
        "docs/dev",
        "examples/demo",
    ]
    assert payload["ai"]["routing"]["pm"] == {
        "provider": "openai",
        "model": "gpt-5.5",
    }


def test_project_config_endpoint_falls_back_to_repo_root_for_aming_claw(monkeypatch):
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "aming-claw",
            "workspace_path": "",
            "status": "active",
        }],
    )

    payload = server.handle_project_config(_ctx("aming-claw"))

    assert payload["project_id"] == "aming-claw"
    assert "examples" in payload["governance"]["exclude_roots"]


def test_project_ai_config_endpoint_returns_read_only_dashboard_contract(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "dashboard-demo",
            "workspace_path": str(tmp_path),
            "status": "active",
        }],
    )

    payload = server.handle_project_ai_config(_ctx("dashboard-demo"))

    assert payload["project_id"] == "dashboard-demo"
    assert payload["read_only"] is True
    assert "role_routing" in payload
    assert "semantic" in payload
    assert payload["semantic"]["analyzer_role"]
    assert payload["project_config"]["ai"]["routing"]["semantic"]["model"] == "claude-opus-4-7"


def test_projects_list_endpoint_returns_registered_projects(monkeypatch):
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "dashboard-demo",
            "workspace_path": "C:/demo",
            "status": "active",
        }],
    )

    payload = server.handle_projects_list(_ctx("aming-claw"))

    assert payload["ok"] is True
    assert payload["projects"][0]["project_id"] == "dashboard-demo"


def test_graph_stale_scope_operation_ignores_outside_workspace_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_graph_governance_project_root",
        lambda _project_id, _body: tmp_path,
    )
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "head-commit")
    monkeypatch.setattr(
        server,
        "_git_changed_paths_between",
        lambda _root, _base, _target, limit=None: [],
    )

    operation, summary = server._graph_stale_scope_operation(
        "dashboard-demo",
        status={"graph_snapshot_commit": "old-commit"},
        pending_rows=[],
    )

    assert operation is None
    assert summary["is_stale"] is False
    assert summary["head_commit"] == "head-commit"
    assert summary["changed_files"] == []
    assert summary["changed_file_count"] == 0
