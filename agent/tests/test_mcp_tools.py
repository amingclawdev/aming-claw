from __future__ import annotations

from types import SimpleNamespace

from agent.mcp import tools as mcp_tools
from agent.mcp.tools import TOOLS, ToolDispatcher


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        return {"ok": True, "method": method, "path": path, "data": data}


class _RuntimeGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "abc1234"}
        if path == "/api/version-check/aming-claw":
            return {
                "ok": True,
                "head": "abc1234",
                "chain_version": "abc1234",
                "dirty": False,
                "runtime_match": True,
                "gov_runtime_version": "abc1234",
                "sm_runtime_version": "abc1234",
            }
        return {"ok": True, "method": method, "path": path, "data": data}


def _dispatcher(recorder: _Recorder, manager: _Recorder | None = None) -> ToolDispatcher:
    return ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=manager.api if manager else None,
        workspace=".",
    )


def test_active_mcp_exposes_backlog_and_graph_governance_tools():
    names = _tool_names()

    assert {
        "backlog_list",
        "backlog_get",
        "backlog_upsert",
        "backlog_close",
        "graph_status",
        "graph_operations_queue",
        "graph_query",
        "graph_pending_scope_queue",
        "manager_health",
        "manager_start",
        "governance_redeploy",
        "executor_respawn",
        "runtime_status",
    }.issubset(names)


def test_mcp_backlog_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "aming-claw",
            "bug_id": "OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY",
            "title": "Tool parity",
            "force_admit": True,
        },
    )
    dispatcher.dispatch(
        "backlog_close",
        {
            "project_id": "aming-claw",
            "bug_id": "OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY",
            "commit": "abc1234",
        },
    )

    assert recorder.calls[0] == (
        "POST",
        "/api/backlog/aming-claw/OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY",
        {"title": "Tool parity", "force_admit": True},
    )
    assert recorder.calls[1] == (
        "POST",
        "/api/backlog/aming-claw/OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY/close",
        {"commit": "abc1234"},
    )


def test_mcp_graph_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "graph_operations_queue",
        {
            "project_id": "aming-claw",
            "require_current_semantic": True,
        },
    )
    dispatcher.dispatch(
        "graph_query",
        {
            "project_id": "aming-claw",
            "tool": "search_semantic",
            "args": {"query": "mcp", "limit": 5},
        },
    )
    dispatcher.dispatch(
        "graph_pending_scope_queue",
        {
            "project_id": "aming-claw",
            "commit_sha": "head",
            "parent_commit_sha": "old",
            "evidence": {"source": "test"},
        },
    )

    assert recorder.calls[0] == (
        "GET",
        "/api/graph-governance/aming-claw/operations/queue?require_current_semantic=true",
        None,
    )
    assert recorder.calls[1] == (
        "POST",
        "/api/graph-governance/aming-claw/query",
        {
            "tool": "search_semantic",
            "args": {"query": "mcp", "limit": 5},
            "actor": "mcp",
            "query_source": "observer",
            "query_purpose": "prompt_context_build",
        },
    )
    assert recorder.calls[2] == (
        "POST",
        "/api/graph-governance/aming-claw/pending-scope",
        {"commit_sha": "head", "parent_commit_sha": "old", "evidence": {"source": "test"}},
    )


def test_mcp_pending_scope_queue_can_force_requeue_suspect_materialization():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "graph_pending_scope_queue",
        {
            "project_id": "aming-claw",
            "commit_sha": "head",
            "status": "queued",
            "force_requeue": True,
            "evidence": {"source": "suspect_snapshot"},
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/pending-scope",
            {
                "commit_sha": "head",
                "status": "queued",
                "force_requeue": True,
                "evidence": {"source": "suspect_snapshot"},
            },
        )
    ]


def test_mcp_host_ops_tools_route_to_manager_sidecar():
    governance = _Recorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    dispatcher.dispatch("manager_health", {})
    dispatcher.dispatch(
        "governance_redeploy",
        {
            "project_id": "aming-claw",
            "chain_version": "abc1234",
            "sync_version": False,
        },
    )
    dispatcher.dispatch(
        "executor_respawn",
        {
            "project_id": "aming-claw",
            "chain_version": "abc1234",
        },
    )

    assert manager.calls == [
        ("GET", "/api/manager/health", None),
        ("POST", "/api/manager/redeploy/governance", {"chain_version": "abc1234"}),
        ("POST", "/api/manager/respawn-executor", {"chain_version": "abc1234"}),
    ]
    assert governance.calls == []


def test_mcp_runtime_status_aggregates_governance_and_manager():
    governance = _RuntimeGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["governance"]["status"] == "ok"
    assert status["manager"]["ok"] is True
    assert status["version_check"]["runtime_match"] is True
    assert governance.calls == [
        ("GET", "/api/health", None),
        ("GET", "/api/version-check/aming-claw", None),
    ]
    assert manager.calls == [("GET", "/api/manager/health", None)]


def test_mcp_manager_start_refuses_takeover_from_mcp():
    governance = _Recorder()
    dispatcher = _dispatcher(governance, _Recorder())

    result = dispatcher.dispatch("manager_start", {"takeover": True})

    assert result["ok"] is False
    assert result["error"] == "takeover_not_supported_from_mcp"


def test_mcp_manager_start_uses_posix_script_on_macos(monkeypatch):
    governance = _Recorder()
    manager = _Recorder()

    def manager_api(method: str, path: str, data: dict | None = None) -> dict:
        manager.calls.append((method, path, data))
        return {"ok": len(manager.calls) > 1}

    dispatcher = ToolDispatcher(
        api_fn=governance.api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=manager_api,
        workspace="/repo",
    )
    calls = []

    monkeypatch.setattr(mcp_tools.sys, "platform", "darwin")
    monkeypatch.setattr(mcp_tools.os.path, "exists", lambda path: path == "/repo/scripts/start-manager.sh")

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="Manager healthy.", stderr="")

    monkeypatch.setattr(mcp_tools.subprocess, "run", fake_run)

    result = dispatcher.dispatch("manager_start", {"health_wait_seconds": 7})

    assert result["ok"] is True
    assert result["script"] == "start-manager.sh"
    assert result["platform"] == "darwin"
    assert calls[0][0] == [
        "bash",
        "/repo/scripts/start-manager.sh",
        "--health-wait-seconds",
        "7",
    ]
    assert manager.calls == [
        ("GET", "/api/manager/health", None),
        ("GET", "/api/manager/health", None),
    ]
