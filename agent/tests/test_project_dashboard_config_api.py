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
