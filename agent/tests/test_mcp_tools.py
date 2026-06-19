from __future__ import annotations

from types import SimpleNamespace

from agent.mcp import tools as mcp_tools
from agent.mcp.tools import TOOLS, ToolDispatcher


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


def _tool_properties(name: str) -> dict:
    tool = next(tool for tool in TOOLS if tool.get("name") == name)
    return tool["inputSchema"]["properties"]


def test_mf_timeline_precheck_schema_exposes_repair_view():
    view = _tool_properties("mf_timeline_precheck")["view"]
    assert "repair" in view["enum"]


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


class _RuntimeMismatchGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "new1234"}
        if path == "/api/version-check/aming-claw":
            return {
                "ok": False,
                "head": "new1234",
                "chain_version": "old1234",
                "dirty": False,
                "runtime_match": False,
                "gov_runtime_version": "new1234",
                "sm_runtime_version": "old1234",
                "message": "HEAD (new1234) != CHAIN_VERSION (old1234)",
            }
        return {"ok": True, "method": method, "path": path, "data": data}


class _AdvancedRuntimeMismatchGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "new1234"}
        if path == "/api/version-check/aming-claw":
            return {
                "ok": True,
                "head": "new1234",
                "chain_version": "new1234",
                "dirty": False,
                "runtime_match": False,
                "gov_runtime_version": "new1234",
                "sm_runtime_version": "old1234",
                "message": "ServiceManager runtime is behind",
            }
        return {"ok": True, "method": method, "path": path, "data": data}


class _OfflineGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        return {"ok": False, "error": "<urlopen error timed out>"}


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
        "task_timeline_append",
        "task_timeline_list",
        "mf_timeline_precheck",
        "observer_repair_run_plan",
        "observer_repair_run_route_evidence",
        "backlog_export",
        "backlog_import",
        "graph_status",
        "graph_operations_queue",
        "stale_artifact_cleanup",
        "stale_artifact_cleanup_apply",
        "graph_query",
        "runtime_context_current",
        "runtime_context_worker_guide",
        "parallel_branch_allocate",
        "parallel_branch_startup",
        "parallel_branch_checkpoint",
        "parallel_branch_finish_gate",
        "graph_pending_scope_queue",
        "manager_health",
        "manager_start",
        "governance_redeploy",
        "executor_respawn",
        "runtime_status",
        "observer_session_register",
        "observer_session_heartbeat",
        "observer_session_close",
        "observer_session_revoke",
        "observer_command_list",
        "observer_command_enqueue",
        "observer_command_next",
        "observer_command_claim",
        "observer_command_takeover",
        "observer_command_complete",
        "observer_command_fail",
        "observer_runtime_text_prepare",
    }.issubset(names)


def test_active_runtime_context_tools_are_read_only_and_route_to_current_service():
    tools = {str(tool["name"]): tool for tool in TOOLS}
    current_schema = tools["runtime_context_current"]["inputSchema"]
    guide_tool = tools["runtime_context_worker_guide"]
    guide_schema = guide_tool["inputSchema"]
    expected_fields = {
        "project_id",
        "runtime_context_id",
        "fence_token",
        "parent_task_id",
        "view",
        "graph_trace_id",
        "session_token",
        "target_project_root",
    }

    assert current_schema["required"] == ["project_id", "runtime_context_id"]
    assert guide_schema["required"] == ["project_id", "runtime_context_id"]
    assert expected_fields.issubset(current_schema["properties"])
    assert expected_fields.issubset(guide_schema["properties"])
    assert "read/write guide" in guide_tool["description"]
    assert "route_token" not in current_schema["properties"]
    assert "route_waiver" not in current_schema["properties"]
    assert "route_token" not in guide_schema["properties"]
    assert "route_waiver" not in guide_schema["properties"]

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    args = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-test",
        "fence_token": "fence-test",
        "parent_task_id": "AC-PARENT",
        "view": "worker_view",
        "graph_trace_id": "gqt-test",
        "session_token": "session-test",
        "target_project_root": "/repo/fixture",
    }

    dispatcher.dispatch("runtime_context_current", args)
    dispatcher.dispatch("runtime_context_worker_guide", args)

    query = (
        "fence_token=fence-test&parent_task_id=AC-PARENT&view=worker_view&"
        "graph_trace_id=gqt-test&session_token=session-test&"
        "target_project_root=%2Frepo%2Ffixture"
    )
    assert recorder.calls == [
        (
            "GET",
            "/api/graph-governance/aming-claw/runtime-contexts/"
            f"mfrctx-test/current-state?{query}",
            None,
        ),
        (
            "GET",
            "/api/graph-governance/aming-claw/runtime-contexts/"
            f"mfrctx-test/worker-guide?{query}",
            None,
        ),
    ]


def test_mcp_observer_command_list_advertises_consumer_recovery_diagnostics():
    tool = next(tool for tool in TOOLS if tool.get("name") == "observer_command_list")

    assert "observer-consumer recovery diagnostics" in tool["description"]


def test_mcp_observer_repair_route_evidence_exposes_command_identity_inputs():
    props = _tool_properties("observer_repair_run_route_evidence")

    assert {
        "route_identity",
        "external_route_identity",
        "claimed_route_identity",
        "command_route_identity",
        "observer_command_route_identity",
        "action_precheck",
        "external_action_precheck",
        "action_precheck_packet",
    }.issubset(props)


def test_mcp_observer_repair_run_route_evidence_routes_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    result = dispatcher.dispatch(
        "observer_repair_run_route_evidence",
        {
            "project_id": "aming-claw",
            "root_backlog_ids": ["AC-ROUTE-FLOW-SESSION-GUIDANCE-20260602"],
            "record": False,
            "actor": "observer-test",
            "action_precheck_id": "external-dispatch-precheck",
            "route_identity": {
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-route",
                "visible_injection_manifest_hash": "sha256:visible",
            },
            "action_precheck": {
                "action": "dispatch_bounded_worker",
                "caller_role": "observer",
                "allowed": True,
            },
        },
    )

    assert result["path"] == "/api/projects/aming-claw/observer-repair-run/route-evidence"
    assert recorder.calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer-repair-run/route-evidence",
            {
                "root_backlog_ids": ["AC-ROUTE-FLOW-SESSION-GUIDANCE-20260602"],
                "record": False,
                "actor": "observer-test",
                "action_precheck_id": "external-dispatch-precheck",
                "route_identity": {
                    "route_context_hash": "sha256:route",
                    "prompt_contract_id": "rprompt-route",
                    "visible_injection_manifest_hash": "sha256:visible",
                },
                "action_precheck": {
                    "action": "dispatch_bounded_worker",
                    "caller_role": "observer",
                    "allowed": True,
                },
            },
        )
    ]


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
    dispatcher.dispatch(
        "backlog_export",
        {
            "project_id": "aming-claw",
            "status": "OPEN",
            "bug_ids": ["BUG-1", "BUG-2"],
        },
    )
    dispatcher.dispatch(
        "backlog_import",
        {
            "project_id": "aming-claw",
            "payload": {"schema": "aming-claw.backlog.export", "rows": []},
            "on_conflict": "skip",
            "dry_run": True,
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
    assert recorder.calls[2] == (
        "GET",
        "/api/backlog/aming-claw/portable/export?status=OPEN&bug_id=BUG-1%2CBUG-2",
        None,
    )
    assert recorder.calls[3] == (
        "POST",
        "/api/backlog/aming-claw/portable/import",
        {
            "payload": {"schema": "aming-claw.backlog.export", "rows": []},
            "on_conflict": "skip",
            "dry_run": True,
        },
    )


def test_mcp_protected_mutations_forward_route_token_or_waiver():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    route_token = {
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "caller_role": "observer",
        "allowed_action": "task_create",
        "project_id": "aming-claw",
        "backlog_id": "BUG-1",
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:route-context"],
    }
    route_waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "allowed_action": "backlog_close",
        "project_id": "aming-claw",
        "backlog_id": "BUG-1",
        "reason": "Operator approved a bounded manual-fix route gate waiver.",
        "timeline_evidence": {"event_id": 42},
    }

    dispatcher.dispatch(
        "task_create",
        {
            "project_id": "aming-claw",
            "prompt": "Implement scoped work.",
            "type": "dev",
            "metadata": {"bug_id": "BUG-1"},
            "route_token": route_token,
        },
    )
    dispatcher.dispatch(
        "task_complete",
        {
            "project_id": "aming-claw",
            "task_id": "task-1",
            "status": "succeeded",
            "result": {"changed_files": ["agent/mcp/tools.py"]},
            "route_token": {**route_token, "allowed_action": "task_complete", "task_id": "task-1"},
        },
    )
    dispatcher.dispatch(
        "backlog_close",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-1",
            "commit": "abc1234",
            "route_waiver": route_waiver,
        },
    )

    assert recorder.calls[0][2]["route_token"] == route_token
    assert recorder.calls[1][2]["route_token"]["allowed_action"] == "task_complete"
    assert recorder.calls[2] == (
        "POST",
        "/api/backlog/aming-claw/BUG-1/close",
        {"commit": "abc1234", "route_waiver": route_waiver},
    )


def test_mcp_protected_write_schemas_expose_route_gate_payloads():
    for name in ("backlog_upsert", "backlog_close", "backlog_audit_archive", "task_timeline_append"):
        properties = _tool_properties(name)

        assert properties["route_token"]["type"] == "object"
        assert properties["route_token_ref"]["type"] == "string"
        assert properties["route_waiver"]["type"] == "object"
        assert properties["route_token_waiver"]["type"] == "object"


def test_mcp_protected_backlog_and_timeline_dispatch_forward_route_gate_payloads():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    route_token = {
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "caller_role": "observer",
        "allowed_action": "backlog_upsert",
        "project_id": "aming-claw",
        "backlog_id": "BUG-1",
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:route-context"],
    }
    route_waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "allowed_action": "task_timeline_append",
        "project_id": "aming-claw",
        "backlog_id": "BUG-1",
        "reason": "Operator approved a bounded manual-fix route gate waiver.",
        "timeline_evidence": {"event_id": 42},
    }
    route_token_ref = "rtok-protected-write"
    backlog_upsert_waiver = {**route_waiver, "allowed_action": "backlog_upsert"}
    timeline_token = {**route_token, "allowed_action": "task_timeline_append"}
    backlog_close_token = {**route_token, "allowed_action": "backlog_close"}
    backlog_close_waiver = {**route_waiver, "allowed_action": "backlog_close"}

    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-1",
            "status": "FIXED",
            "route_token": route_token,
            "route_token_ref": route_token_ref,
            "route_waiver": backlog_upsert_waiver,
        },
    )
    dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-1",
            "event_type": "mf.verification",
            "event_kind": "verification",
            "route_token": timeline_token,
            "route_token_ref": route_token_ref,
            "route_waiver": route_waiver,
        },
    )
    dispatcher.dispatch(
        "backlog_close",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-1",
            "commit": "abc1234",
            "route_token": backlog_close_token,
            "route_token_ref": route_token_ref,
            "route_waiver": backlog_close_waiver,
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/backlog/aming-claw/BUG-1",
            {
                "status": "FIXED",
                "route_token": route_token,
                "route_token_ref": route_token_ref,
                "route_waiver": backlog_upsert_waiver,
            },
        ),
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {
                "backlog_id": "BUG-1",
                "event_type": "mf.verification",
                "event_kind": "verification",
                "route_token": timeline_token,
                "route_token_ref": route_token_ref,
                "route_waiver": route_waiver,
            },
        ),
        (
            "POST",
            "/api/backlog/aming-claw/BUG-1/close",
            {
                "commit": "abc1234",
                "route_token": backlog_close_token,
                "route_token_ref": route_token_ref,
                "route_waiver": backlog_close_waiver,
            },
        ),
    ]


def test_mcp_protected_dispatch_does_not_synthesize_route_gate_payloads_when_absent():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-1",
            "status": "FIXED",
        },
    )
    dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-1",
            "event_type": "mf.verification",
            "event_kind": "verification",
        },
    )
    dispatcher.dispatch(
        "backlog_close",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-1",
            "commit": "abc1234",
        },
    )

    for _method, _path, body in recorder.calls:
        assert body is not None
        assert "route_token" not in body
        assert "route_waiver" not in body
        assert "route_token_waiver" not in body


def test_mcp_backlog_list_defaults_to_compact_open_page():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch("backlog_list", {"project_id": "aming-claw"})

    assert recorder.calls == [
        (
            "GET",
            "/api/backlog/aming-claw?view=compact&limit=50&offset=0&status=OPEN",
            None,
        )
    ]


def test_mcp_backlog_list_supports_search_and_closed_page():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "backlog_list",
        {
            "project_id": "aming-claw",
            "q": "portable import",
            "limit": 500,
            "offset": 3,
            "include_closed": True,
            "view": "full",
        },
    )

    assert recorder.calls == [
        (
            "GET",
            "/api/backlog/aming-claw?view=full&limit=100&offset=3&q=portable+import&include_closed=true",
            None,
        )
    ]


def test_mcp_timeline_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-1",
            "event_type": "mf.implementation",
            "event_kind": "implementation",
            "status": "accepted",
            "payload": {"changed_files": ["agent/mcp/tools.py"]},
        },
    )
    dispatcher.dispatch(
        "task_timeline_list",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-1",
            "event_kind": "implementation",
            "limit": 25,
        },
    )
    dispatcher.dispatch(
        "mf_timeline_precheck",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-1",
            "view": "repair",
            "include_events": True,
            "limit": 25,
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {
                "backlog_id": "BUG-1",
                "event_type": "mf.implementation",
                "event_kind": "implementation",
                "status": "accepted",
                "payload": {"changed_files": ["agent/mcp/tools.py"]},
            },
        ),
        (
            "GET",
            "/api/task/aming-claw/timeline?backlog_id=BUG-1&event_kind=implementation&limit=25",
            None,
        ),
        (
            "GET",
            "/api/backlog/aming-claw/BUG-1/timeline-gate?view=repair&include_events=true&limit=25",
            None,
        ),
    ]


def test_mcp_observer_command_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "observer_session_register",
        {
            "project_id": "aming-claw",
            "observer_kind": "codex",
            "session_label": "local",
            "pid": 123,
            "cwd": "/repo",
            "capabilities": {"actions": ["*"], "command_types": ["*"]},
        },
    )
    dispatcher.dispatch(
        "observer_session_heartbeat",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )
    dispatcher.dispatch(
        "observer_command_enqueue",
        {
            "project_id": "aming-claw",
            "command_type": "analyze_requirements",
            "payload": {"raw_id": "raw-1"},
            "created_by": "dashboard",
        },
    )
    dispatcher.dispatch(
        "observer_command_list",
        {
            "project_id": "aming-claw",
            "status": "queued,claimed",
            "limit": 2000,
        },
    )
    dispatcher.dispatch(
        "observer_command_next",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )
    dispatcher.dispatch(
        "observer_command_claim",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
            "command_id": "cmd-1",
        },
    )
    dispatcher.dispatch(
        "observer_command_takeover",
        {
            "project_id": "aming-claw",
            "session_id": "obs-fallback",
            "session_token": "fallback-tok",
            "command_id": "cmd-stale",
            "reason": "fallback observer resolves stale claimed command",
        },
    )
    dispatcher.dispatch(
        "observer_command_complete",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
            "command_id": "cmd-1",
            "result": {"ok": True},
        },
    )
    dispatcher.dispatch(
        "observer_command_fail",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
            "command_id": "cmd-2",
            "error": "blocked",
        },
    )
    dispatcher.dispatch(
        "observer_session_close",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )
    dispatcher.dispatch(
        "observer_session_revoke",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/register",
            {
                "observer_kind": "codex",
                "session_label": "local",
                "pid": 123,
                "cwd": "/repo",
                "capabilities": {"actions": ["*"], "command_types": ["*"]},
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/obs-1/heartbeat",
            {"session_token": "tok"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands",
            {
                "command_type": "analyze_requirements",
                "payload": {"raw_id": "raw-1"},
                "created_by": "dashboard",
            },
        ),
        (
            "GET",
            "/api/projects/aming-claw/observer-commands?status=queued%2Cclaimed&limit=1000",
            None,
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/next",
            {"session_id": "obs-1", "session_token": "tok"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/claim",
            {"session_id": "obs-1", "session_token": "tok", "command_id": "cmd-1"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-stale/takeover",
            {
                "session_id": "obs-fallback",
                "session_token": "fallback-tok",
                "reason": "fallback observer resolves stale claimed command",
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-1/complete",
            {"session_id": "obs-1", "session_token": "tok", "result": {"ok": True}},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-2/fail",
            {"session_id": "obs-1", "session_token": "tok", "error": "blocked"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/obs-1/close",
            {"session_token": "tok"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/obs-1/revoke",
            {"session_token": "tok"},
        ),
    ]


def test_mcp_observer_runtime_text_prepare_routes_to_governance_endpoint(tmp_path):
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    main = tmp_path / "main"
    main.mkdir()

    result = dispatcher.dispatch(
        "observer_runtime_text_prepare",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-RUNTIME-TEXT",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-runtime",
            "prompt_contract_hash": "sha256:prompt",
            "route_id": "route-runtime",
            "visible_injection_manifest_hash": "sha256:visible",
            "parent_route_identity": {
                "route_id": "event.route_prompt_context.preview",
                "route_context_hash": "sha256:parent-route",
                "prompt_contract_id": "rprompt-parent",
                "prompt_contract_hash": "sha256:parent-prompt",
                "route_token_ref": "rtok-parent",
                "visible_injection_manifest_hash": "sha256:visible",
            },
            "main_worktree": str(main),
            "workspace_root": str(tmp_path / "workers"),
            "owned_files": ["agent/observer_runtime.py"],
            "observer_command_id": "cmd-runtime-text",
            "task_id": "AC-RUNTIME-TEXT-impl-1",
            "parent_task_id": "AC-RUNTIME-TEXT",
            "merge_queue_id": "mq-runtime-text",
            "fence_token": "fence-runtime-text",
            "branch_runtime_registration_ref": (
                "/api/graph-governance/aming-claw/parallel-branches/allocate"
            ),
            "graph_trace_ids": ["gqt-runtime-text"],
            "base_commit": "base123",
            "target_head_commit": "target123",
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer/runtime-text/prepare",
            {
                "backlog_id": "AC-RUNTIME-TEXT",
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-runtime",
                "prompt_contract_hash": "sha256:prompt",
                "route_id": "route-runtime",
                "visible_injection_manifest_hash": "sha256:visible",
                "parent_route_identity": {
                    "route_id": "event.route_prompt_context.preview",
                    "route_context_hash": "sha256:parent-route",
                    "prompt_contract_id": "rprompt-parent",
                    "prompt_contract_hash": "sha256:parent-prompt",
                    "route_token_ref": "rtok-parent",
                    "visible_injection_manifest_hash": "sha256:visible",
                },
                "main_worktree": str(main),
                "workspace_root": str(tmp_path / "workers"),
                "owned_files": ["agent/observer_runtime.py"],
                "observer_command_id": "cmd-runtime-text",
                "task_id": "AC-RUNTIME-TEXT-impl-1",
                "parent_task_id": "AC-RUNTIME-TEXT",
                "merge_queue_id": "mq-runtime-text",
                "fence_token": "fence-runtime-text",
                "branch_runtime_registration_ref": (
                    "/api/graph-governance/aming-claw/parallel-branches/allocate"
                ),
                "graph_trace_ids": ["gqt-runtime-text"],
                "base_commit": "base123",
                "target_head_commit": "target123",
            },
        )
    ]
    assert result["ok"] is True
    assert result["path"] == "/api/projects/aming-claw/observer/runtime-text/prepare"


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
        "stale_artifact_cleanup",
        {
            "project_id": "aming-claw",
            "repo_root": "/repo",
            "include_unowned": False,
        },
    )
    dispatcher.dispatch(
        "stale_artifact_cleanup_apply",
        {
            "project_id": "aming-claw",
            "repo_root": "/repo",
            "candidate_ids": ["batch_worktree:abc"],
            "actor": "observer",
            "reason": "terminal cleanup",
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
        "GET",
        "/api/graph-governance/aming-claw/stale-artifact-cleanup?repo_root=%2Frepo&include_unowned=false",
        None,
    )
    assert recorder.calls[3] == (
        "POST",
        "/api/graph-governance/aming-claw/stale-artifact-cleanup/apply",
        {
            "repo_root": "/repo",
            "candidate_ids": ["batch_worktree:abc"],
            "actor": "observer",
            "reason": "terminal cleanup",
        },
    )
    assert recorder.calls[4] == (
        "POST",
        "/api/graph-governance/aming-claw/pending-scope",
        {"commit_sha": "head", "parent_commit_sha": "old", "evidence": {"source": "test"}},
    )


def test_mcp_graph_query_schema_exposes_mf_sub_runtime_identity_fields():
    properties = _tool_properties("graph_query")

    for key in (
        "runtime_context_id",
        "target_project_root",
        "project_root",
        "repo_root",
        "task_id",
        "parent_task_id",
        "worker_role",
        "fence_token",
        "session_token",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
        "route_identity",
    ):
        assert key in properties


def test_mcp_parallel_branch_tool_schemas_expose_bounded_identity_fields():
    allocate = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_allocate")
    startup = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_startup")
    checkpoint = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_checkpoint")
    finish_gate = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_finish_gate")
    runtime_text = next(
        tool for tool in TOOLS if tool.get("name") == "observer_runtime_text_prepare"
    )

    allocate_props = allocate["inputSchema"]["properties"]
    startup_props = startup["inputSchema"]["properties"]
    checkpoint_props = checkpoint["inputSchema"]["properties"]
    finish_props = finish_gate["inputSchema"]["properties"]
    runtime_text_props = runtime_text["inputSchema"]["properties"]
    assert runtime_text["inputSchema"]["required"] == [
        "project_id",
        "backlog_id",
        "observer_command_id",
        "route_context_hash",
        "prompt_contract_id",
    ]

    assert allocate["inputSchema"]["required"] == ["project_id", "task_id"]
    for key in (
        "workspace_root",
        "repo_root_path",
        "backlog_id",
        "parent_task_id",
        "root_task_id",
        "stage_task_id",
        "agent_id",
        "worker_id",
        "branch_prefix",
        "worktree_root",
        "ref_name",
        "target_branch",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "fence_token",
        "create_worktree",
    ):
        assert key in allocate_props

    assert startup["inputSchema"]["required"] == ["project_id", "task_id"]
    for key in (
        "parent_task_id",
        "worker_role",
        "worker_id",
        "agent_id",
        "actual_host_worker_id",
        "worker_session_id",
        "worker_transcript_ref",
        "worker_transcript_path",
        "harness_type",
        "filer_principal",
        "session_token",
        "session_token_surrogate",
        "fence_token",
        "runtime_context_id",
        "observer_command_id",
        "actual_cwd",
        "actual_git_root",
        "branch",
        "head_commit",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "owned_files",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
    ):
        assert key in startup_props

    assert checkpoint["inputSchema"]["required"] == [
        "project_id",
        "task_id",
        "checkpoint_id",
        "fence_token",
    ]
    for key in ("head_commit", "refresh_head", "refresh_head_from_worktree", "replay_source"):
        assert key in checkpoint_props

    assert finish_gate["inputSchema"]["required"] == ["project_id", "task_id"]
    for key in (
        "fence_token",
        "checkpoint_id",
        "status",
        "changed_files",
        "test_results",
        "graph_trace_evidence",
        "route_lineage",
        "parent_route_lineage",
    ):
        assert key in finish_props

    for props in (allocate_props, checkpoint_props, finish_props):
        assert props["route_token"]["type"] == "object"
        assert props["route_waiver"]["type"] == "object"
        assert props["route_token_waiver"]["type"] == "object"
    assert runtime_text_props["observer_command_id"]["type"] == "string"
    assert runtime_text_props["parent_route_identity"]["type"] == "object"
    assert "canonical parent route identity" in runtime_text_props[
        "parent_route_identity"
    ]["description"]


def test_mcp_parallel_branch_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    route_token = {
        "route_context_hash": "sha256:route",
        "prompt_contract_id": "rprompt-1",
        "allowed_action": "parallel_branch_allocate",
    }

    dispatcher.dispatch(
        "parallel_branch_allocate",
        {
            "project_id": "aming-claw",
            "task_id": "mf-sub-1",
            "workspace_root": "/repo",
            "repo_root_path": "/repo",
            "backlog_id": "BUG-1",
            "parent_task_id": "observer-1",
            "root_task_id": "observer-1",
            "stage_task_id": "mf-sub-1",
            "agent_id": "codex",
            "worker_id": "worker-1",
            "branch_prefix": "mf",
            "worktree_root": ".worktrees",
            "ref_name": "main",
            "target_branch": "main",
            "base_commit": "base",
            "target_head_commit": "target",
            "merge_queue_id": "mq-1",
            "fence_token": "fence-1",
            "create_worktree": False,
            "route_token": route_token,
        },
    )
    dispatcher.dispatch(
        "parallel_branch_startup",
        {
            "project_id": "aming-claw",
            "task_id": "mf-sub-1",
            "parent_task_id": "observer-1",
            "worker_role": "mf_sub",
            "worker_id": "worker-1",
            "agent_id": "agent-1",
            "actual_host_worker_id": "host-worker-1",
            "worker_session_id": "host-worker-1",
            "worker_transcript_ref": "multi_agent:host-worker-1",
            "worker_transcript_path": "/repo/transcripts/host-worker-1.jsonl",
            "harness_type": "codex",
            "filer_principal": "host-worker-1",
            "session_token_surrogate": "host-session:worker-1",
            "fence_token": "fence-1",
            "actual_cwd": "/repo/.worktrees/mf-sub-1",
            "actual_git_root": "/repo/.worktrees/mf-sub-1",
            "branch": "refs/heads/mf/mf-sub-1",
            "head_commit": "head",
            "base_commit": "base",
            "target_head_commit": "target",
            "merge_queue_id": "mq-1",
            "owned_files": ["agent/mcp/tools.py"],
            "route_id": "route-1",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-1",
            "prompt_contract_hash": "sha256:prompt",
            "route_token_ref": "rtok-1",
            "visible_injection_manifest_hash": "sha256:visible",
        },
    )
    dispatcher.dispatch(
        "parallel_branch_checkpoint",
        {
            "project_id": "aming-claw",
            "task_id": "mf-sub-1",
            "checkpoint_id": "ckpt-1",
            "fence_token": "fence-1",
            "head_commit": "head",
            "refresh_head": False,
            "replay_source": "checkpoint",
        },
    )
    dispatcher.dispatch(
        "parallel_branch_finish_gate",
        {
            "project_id": "aming-claw",
            "task_id": "mf-sub-1",
            "parent_task_id": "observer-1",
            "worker_role": "mf_sub",
            "fence_token": "fence-1",
            "checkpoint_id": "ckpt-2",
            "status": "review_ready",
            "changed_files": ["agent/mcp/tools.py"],
            "test_results": {"status": "passed", "passed": True},
            "graph_trace_evidence": {
                "query_source": "mf_subagent",
                "trace_ids": ["gqt-1"],
            },
            "route_lineage": {"schema_version": "mf_subagent_route_lineage.v1"},
            "blockers": [],
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/parallel-branches/allocate",
            {
                "task_id": "mf-sub-1",
                "workspace_root": "/repo",
                "repo_root_path": "/repo",
                "backlog_id": "BUG-1",
                "parent_task_id": "observer-1",
                "root_task_id": "observer-1",
                "stage_task_id": "mf-sub-1",
                "agent_id": "codex",
                "worker_id": "worker-1",
                "branch_prefix": "mf",
                "worktree_root": ".worktrees",
                "ref_name": "main",
                "target_branch": "main",
                "base_commit": "base",
                "target_head_commit": "target",
                "merge_queue_id": "mq-1",
                "fence_token": "fence-1",
                "create_worktree": False,
                "route_token": route_token,
            },
        ),
        (
            "POST",
            "/api/graph-governance/aming-claw/parallel-branches/startup",
            {
                "task_id": "mf-sub-1",
                "parent_task_id": "observer-1",
                "worker_role": "mf_sub",
                "worker_id": "worker-1",
                "agent_id": "agent-1",
                "actual_host_worker_id": "host-worker-1",
                "worker_session_id": "host-worker-1",
                "worker_transcript_ref": "multi_agent:host-worker-1",
                "worker_transcript_path": "/repo/transcripts/host-worker-1.jsonl",
                "harness_type": "codex",
                "filer_principal": "host-worker-1",
                "session_token_surrogate": "host-session:worker-1",
                "fence_token": "fence-1",
                "actual_cwd": "/repo/.worktrees/mf-sub-1",
                "actual_git_root": "/repo/.worktrees/mf-sub-1",
                "branch": "refs/heads/mf/mf-sub-1",
                "head_commit": "head",
                "base_commit": "base",
                "target_head_commit": "target",
                "merge_queue_id": "mq-1",
                "owned_files": ["agent/mcp/tools.py"],
                "route_id": "route-1",
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt",
                "route_token_ref": "rtok-1",
                "visible_injection_manifest_hash": "sha256:visible",
            },
        ),
        (
            "POST",
            "/api/graph-governance/aming-claw/parallel-branches/checkpoint",
            {
                "task_id": "mf-sub-1",
                "checkpoint_id": "ckpt-1",
                "fence_token": "fence-1",
                "head_commit": "head",
                "refresh_head": False,
                "replay_source": "checkpoint",
            },
        ),
        (
            "POST",
            "/api/graph-governance/aming-claw/parallel-branches/finish-gate",
            {
                "task_id": "mf-sub-1",
                "parent_task_id": "observer-1",
                "worker_role": "mf_sub",
                "fence_token": "fence-1",
                "checkpoint_id": "ckpt-2",
                "status": "review_ready",
                "changed_files": ["agent/mcp/tools.py"],
                "test_results": {"status": "passed", "passed": True},
                "graph_trace_evidence": {
                    "query_source": "mf_subagent",
                    "trace_ids": ["gqt-1"],
                },
                "route_lineage": {"schema_version": "mf_subagent_route_lineage.v1"},
                "blockers": [],
            },
        ),
    ]


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
    assert status["strict_ok"] is True
    assert status["severity"] == "ok"
    assert status["usable"] is True
    assert status["capabilities"]["graph_queries"] is True
    assert status["capabilities"]["core_runtime"] is True
    assert status["capabilities"]["advanced_chain_ops"] is True
    assert status["governance"]["status"] == "ok"
    assert status["manager"]["ok"] is True
    assert status["version_check"]["runtime_match"] is True
    assert status["target_project_version"]["head"] == "abc1234"
    assert status["governance_runtime"]["runtime_match"] is True
    assert governance.calls == [
        ("GET", "/api/health", None),
        ("GET", "/api/version-check/aming-claw", None),
    ]
    assert manager.calls == [("GET", "/api/manager/health", None)]


def test_mcp_runtime_status_runtime_mismatch_is_advanced_ops_only():
    governance = _RuntimeMismatchGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["strict_ok"] is False
    assert status["severity"] == "warning"
    assert status["usable"] is True
    assert status["capabilities"]["graph_queries"] is True
    assert status["capabilities"]["backlog"] is True
    assert status["capabilities"]["core_runtime"] is False
    assert status["capabilities"]["advanced_chain_ops"] is False
    assert status["capabilities"]["executor"] is False
    assert "version metadata needs attention" in status["summary"]
    assert "advanced_chain_ops_redeploy_or_restart" in status["recommended_actions"]


def test_mcp_runtime_status_service_manager_mismatch_keeps_core_ok():
    governance = _AdvancedRuntimeMismatchGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["strict_ok"] is True
    assert status["severity"] == "ok"
    assert status["capabilities"]["core_runtime"] is True
    assert status["capabilities"]["advanced_chain_ops"] is False
    assert status["capabilities"]["executor"] is False
    assert "Governance core is healthy" in status["summary"]
    assert "advanced_chain_ops_redeploy_or_restart" in status["recommended_actions"]


def test_mcp_runtime_status_governance_offline_reports_loaded_mcp():
    governance = _OfflineGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is False
    assert status["severity"] == "blocking"
    assert status["governance"]["governance_online"] is False
    assert status["governance"]["mcp_loaded"] is True
    assert status["version_check"]["governance_online"] is False
    assert "start_governance" in status["recommended_actions"]
    assert "MCP server is loaded" in status["governance"]["message"]


def test_mcp_version_check_preserves_governance_and_workspace_heads(monkeypatch):
    governance = _Recorder()

    def api(method: str, path: str, data: dict | None = None) -> dict:
        governance.calls.append((method, path, data))
        if path == "/api/version-check/aming-claw":
            return {
                "ok": False,
                "head": "target-old",
                "target_head": "target-old",
                "target_project_root": ".",
                "governance_synced_head": "gov-old",
                "chain_version": "chain-old",
                "dirty": False,
                "message": "HEAD (target-old) != CHAIN_VERSION (chain-old)",
            }
        return {"ok": True}

    def fake_check_output(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return b"workspace-new\n"
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return b""
        if cmd[:3] == ["git", "log", "--oneline"]:
            return b"workspace-new commit\n"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(mcp_tools.subprocess, "check_output", fake_check_output)
    dispatcher = ToolDispatcher(api_fn=api, worker_pool=None, service_mgr=None, workspace=".")

    result = dispatcher.dispatch("version_check", {"project_id": "aming-claw"})

    assert result["head"] == "target-old"
    assert result["target_head"] == "target-old"
    assert result["mcp_workspace_head"] == "workspace-new"
    assert result["mcp_workspace_probe"]["head"] == "workspace-new"
    assert result["governance_synced_head"] == "gov-old"
    assert "MCP workspace HEAD (workspace-new) != CHAIN_VERSION (chain-old)" in result["message"]
    assert "governance synced HEAD (gov-old) differs from MCP workspace HEAD (workspace-new)" in result["message"]


def test_mcp_version_check_governance_offline_preserves_workspace_head(monkeypatch):
    governance = _OfflineGovRecorder()

    def fake_check_output(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return b"workspace-new\n"
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return b""
        return b""

    monkeypatch.setattr(mcp_tools.subprocess, "check_output", fake_check_output)
    dispatcher = ToolDispatcher(api_fn=governance.api, worker_pool=None, service_mgr=None, workspace=".")

    result = dispatcher.dispatch("version_check", {"project_id": "aming-claw"})

    assert result["ok"] is False
    assert result["governance_online"] is False
    assert result["mcp_loaded"] is True
    assert result["recommended_action"] == "start_governance"
    assert result["mcp_workspace_head"] == "workspace-new"
    assert "head" not in result
    assert "MCP server is loaded" in result["message"]


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
