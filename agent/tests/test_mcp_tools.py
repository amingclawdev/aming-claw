from __future__ import annotations

from agent.mcp.tools import TOOLS, ToolDispatcher


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        return {"ok": True, "method": method, "path": path, "data": data}


def _dispatcher(recorder: _Recorder) -> ToolDispatcher:
    return ToolDispatcher(api_fn=recorder.api, worker_pool=None, service_mgr=None)


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
