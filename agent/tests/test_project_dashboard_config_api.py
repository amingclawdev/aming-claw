from __future__ import annotations

from pathlib import Path

from agent.governance import server


def _ctx(project_id: str, method: str = "GET", body: dict | None = None):
    return server.RequestContext(
        None,
        method,
        {"project_id": project_id},
        {},
        body or {},
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
            "  e2e:",
            "    auto_run: false",
            "    suites:",
            "      dashboard.semantic.safe:",
            "        label: Dashboard semantic safe path",
            "        command: node scripts/e2e-trunk.mjs --skip-dashboard",
            "        live_ai: false",
            "        mutates_db: true",
            "        trigger:",
            "          paths:",
            "            - src/**",
            "          tags:",
            "            - dashboard",
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
    assert payload["testing"]["e2e"]["suites"]["dashboard.semantic.safe"]["command"].startswith("node scripts/")
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


def test_project_e2e_config_endpoint_exposes_suite_registry(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    monkeypatch.setattr(
        server,
        "_graph_governance_project_root",
        lambda _project_id, _body: tmp_path,
    )

    payload = server.handle_project_e2e_config(_ctx("dashboard-demo"))

    assert payload["ok"] is True
    assert payload["project_id"] == "dashboard-demo"
    suites = payload["e2e"]["suites"]
    assert suites["dashboard.semantic.safe"]["trigger"]["paths"] == ["src/**"]
    assert suites["dashboard.semantic.safe"]["live_ai"] is False


def test_project_ai_config_endpoint_returns_writable_dashboard_contract(tmp_path, monkeypatch):
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
    assert payload["read_only"] is False
    assert payload["write_supported"] is True
    assert "role_routing" in payload
    assert "semantic" in payload
    assert payload["semantic"]["analyzer_role"]
    assert payload["project_config"]["ai"]["routing"]["semantic"]["model"] == "claude-opus-4-7"
    assert "dashboard.semantic.safe" in payload["project_config"]["testing"]["e2e"]["suites"]


def test_project_ai_config_endpoint_updates_project_routing(tmp_path, monkeypatch):
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

    payload = server.handle_project_ai_config_update(_ctx(
        "dashboard-demo",
        method="POST",
        body={
            "routing": {
                "pm": {"provider": "openai", "model": "gpt-5.5"},
                "dev": {"provider": "openai", "model": "gpt-5.4-mini"},
                "semantic": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
            },
            "actor": "dashboard-test",
        },
    ))

    assert payload["ok"] is True
    assert payload["updated"] is True
    assert payload["project_config"]["ai"]["routing"]["dev"]["model"] == "gpt-5.4-mini"
    assert payload["project_config"]["ai"]["routing"]["semantic"]["model"] == "claude-sonnet-4-5"


def test_project_git_ref_endpoints_return_and_persist_selected_ref(tmp_path, monkeypatch):
    projects = {
        "dashboard-demo": {
            "project_id": "dashboard-demo",
            "workspace_path": str(tmp_path),
            "status": "active",
        }
    }

    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _pid, _body: tmp_path)
    monkeypatch.setattr(
        server,
        "_git_refs_for_root",
        lambda _root: {
            "head_commit": "abc123",
            "current_branch": "main",
            "branches": ["feature/dashboard", "main"],
            "tags": [],
            "is_git_repo": True,
        },
    )
    monkeypatch.setattr(server, "_git_ref_exists", lambda _root, ref: ref in {"main", "feature/dashboard"})
    monkeypatch.setattr(server.project_service, "get_project", lambda pid: projects.get(pid))

    def _update(pid, updates):
        projects[pid].update(updates)
        return projects[pid]

    monkeypatch.setattr(server.project_service, "update_project_metadata", _update)

    initial = server.handle_project_git_refs(_ctx("dashboard-demo"))
    assert initial["selected_ref"] == "main"

    updated = server.handle_project_git_ref_select(_ctx(
        "dashboard-demo",
        method="POST",
        body={"selected_ref": "feature/dashboard", "actor": "dashboard-test"},
    ))
    assert updated["ok"] is True
    assert updated["selected_ref"] == "feature/dashboard"
    assert projects["dashboard-demo"]["selected_ref_updated_by"] == "dashboard-test"


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
