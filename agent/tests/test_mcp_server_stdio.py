from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from agent.governance import mcp_server as governance_mcp_server
from agent.mcp import server as plugin_mcp_server
from agent.mcp.server import AmingClawMCP
from agent.mcp.schema_contract import MCP_TOOL_SCHEMA_VERSION
from agent.mcp.tools import TOOLS as runtime_mcp_tools
from agent.mcp.tools import ToolDispatcher


ROOT = Path(__file__).resolve().parents[2]


def _run_mcp_probe(
    messages: list[dict],
    *,
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> tuple[list[dict], str, int]:
    args = [
        sys.executable,
        "-m",
        "agent.mcp.server",
        "--project",
        "aming-claw",
        "--workers",
        "0",
        "--governance-url",
        "http://127.0.0.1:9",
    ]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env={**os.environ, **(extra_env or {})},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    for message in messages:
        proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.close()
    stdout = proc.stdout.read()
    stderr = proc.stderr.read() if proc.stderr else ""
    returncode = proc.wait(timeout=10)
    responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return responses, stderr, returncode


def test_mcp_stdio_initialize_and_health_survive_missing_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "health", "arguments": {}},
        },
    ])

    assert returncode == 0
    assert stderr == ""
    assert responses[0]["result"]["serverInfo"]["name"] == "aming-claw"
    assert "resources" in responses[0]["result"]["capabilities"]
    text = responses[1]["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert "error" in payload


def test_mcp_stdio_serializes_qa_onboard_compact_selected_role_under_limit(
    tmp_path,
    monkeypatch,
):
    compact_payload = {
        "schema_version": "onboard_route_guide.service_response.v1",
        "projection_version": "qa-selected-role-compact.v1",
        "ok": True,
        "project_id": "aming-claw",
        "backlog_id": "AC-QA-COMPACT-MCP",
        "selected_role": "qa",
        "selected_work_type": "qa_verification",
        "compact_selected_role": True,
        "mcp_text_content_serialized_char_limit": 64000,
        "agent_onboard_guidance": {
            "selected_role_guidance": {
                "current_line_id": "qa_graph_context",
                "ordered_steps": [
                    {"id": "qa_session_register"},
                    {"id": "graph_query_schema"},
                    {"id": "read_compact_contract_runtime"},
                    {"id": "submit_qa_graph_context"},
                    {"id": "reread_after_qa_graph_context"},
                ],
            },
            "canonical_dispatch_identity": {
                "project_id": "aming-claw",
                "backlog_id": "AC-QA-COMPACT-MCP",
                "original_worker_task_id": "worker-qa-compact",
                "assigned_worktree": "/tmp/qa-compact-worker",
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-qa-compact",
                "prompt_contract_hash": "sha256:prompt",
                "visible_injection_manifest_hash": "sha256:manifest",
            },
        },
        "next_legal_action": {
            "line_id": "qa_graph_context",
            "action": "record_graph_trace",
        },
        "raw_route_token_required": False,
        "raw_route_token_exposed": False,
    }

    class CompactDispatcher:
        def dispatch(self, name, arguments):
            assert name == "onboard_route_guide"
            assert arguments == {
                "project_id": "aming-claw",
                "backlog_id": "AC-QA-COMPACT-MCP",
                "role": "qa",
                "work_type": "qa_verification",
            }
            return compact_payload

    mcp = AmingClawMCP(
        project_id="aming-claw",
        governance_url="http://127.0.0.1:9",
        workspace=str(tmp_path),
        redis_url="redis://127.0.0.1:9/0",
    )
    mcp.dispatcher = CompactDispatcher()
    messages = []
    monkeypatch.setattr(plugin_mcp_server, "_write", messages.append)

    mcp._handle(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "onboard_route_guide",
                    "arguments": {
                        "project_id": "aming-claw",
                        "backlog_id": "AC-QA-COMPACT-MCP",
                        "role": "qa",
                        "work_type": "qa_verification",
                    },
                },
            }
        )
    )

    assert len(messages) == 1
    content_text = messages[0]["result"]["content"][0]["text"]
    assert json.loads(content_text) == compact_payload
    assert len(content_text) <= 64000


def test_worker_mcp_hides_and_rejects_host_only_auth_tools():
    raw_session = "worker-session-must-not-echo"
    raw_fence = "worker-fence-must-not-echo"
    responses, stderr, returncode = _run_mcp_probe(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "runtime_context_session_token_initial_join",
                    "arguments": {
                        "project_id": "aming-claw",
                        "runtime_context_id": "mfrctx-worker-filter",
                        "task_id": "worker-filter-task",
                        "reason": "must be rejected before HTTP",
                    },
                },
            },
        ],
        extra_env={
            "AMING_WORKER_SESSION_TOKEN": raw_session,
            "AMING_WORKER_FENCE_TOKEN": raw_fence,
        },
    )

    assert returncode == 0
    tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert not {
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_reissue",
        "runtime_context_session_token_rejoin",
    } & tool_names
    assert responses[2]["error"]["message"] == (
        "host-only authentication tool is unavailable in worker MCP"
    )
    serialized = json.dumps(responses, sort_keys=True) + stderr
    assert raw_session not in serialized
    assert raw_fence not in serialized


@pytest.mark.parametrize(
    "present_env_key",
    ["AMING_WORKER_SESSION_TOKEN", "AMING_WORKER_FENCE_TOKEN"],
)
def test_worker_mcp_filters_host_tools_when_auth_env_key_is_empty(present_env_key):
    responses, stderr, returncode = _run_mcp_probe(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "runtime_context_session_token_initial_join",
                    "arguments": {
                        "project_id": "aming-claw",
                        "runtime_context_id": "mfrctx-worker-filter",
                        "task_id": "worker-filter-task",
                        "reason": "must be rejected before HTTP",
                    },
                },
            },
        ],
        extra_env={present_env_key: ""},
    )

    assert returncode == 0
    tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert "runtime_context_session_token_initial_join" not in tool_names
    assert responses[2]["error"]["message"] == (
        "host-only authentication tool is unavailable in worker MCP"
    )
    assert stderr == ""


def test_standalone_worker_mcp_filters_and_dispatch_rejects_host_only_tools(
    monkeypatch,
):
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", "standalone-session")
    monkeypatch.setenv("AMING_WORKER_FENCE_TOKEN", "standalone-fence")

    tool_names = {
        tool["name"] for tool in governance_mcp_server._tools_for_current_process()
    }
    assert "runtime_context_session_token_initial_join" not in tool_names
    with pytest.raises(
        ValueError,
        match="host-only authentication tool is unavailable in worker MCP",
    ):
        governance_mcp_server._dispatch_tool(
            "runtime_context_session_token_initial_join",
            {
                "project_id": "aming-claw",
                "runtime_context_id": "mfrctx-worker-filter",
                "task_id": "worker-filter-task",
                "reason": "must be rejected before HTTP",
            },
        )


def test_parallel_branch_merge_queue_apply_schemas_expose_branch_ref():
    for tools in (governance_mcp_server.TOOLS, runtime_mcp_tools):
        tool = next(
            candidate
            for candidate in tools
            if candidate["name"] == "parallel_branch_merge_queue_apply"
        )
        properties = tool["inputSchema"]["properties"]
        assert "branch_ref" in properties


def test_mcp_stdio_tools_list_does_not_require_redis_or_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = responses[0]["result"]["tools"]
    schema_meta = responses[0]["result"]["_meta"]["aming_claw_tool_schema"]
    assert MCP_TOOL_SCHEMA_VERSION == "2026-07-17.1"
    assert schema_meta["loaded_client_tool_schema_version"] == (
        MCP_TOOL_SCHEMA_VERSION
    )
    assert schema_meta["server_tool_schema_version"] == ""
    assert schema_meta["client_schema_fresh"] is None
    assert schema_meta["server_version_observable"] is False
    assert schema_meta["status"] == "process_local_loaded_schema"
    assert schema_meta["freshness_signal"]["mcp_tool"] == "runtime_status"
    names = {tool["name"] for tool in tools}
    assert {"health", "manager_health", "graph_query", "backlog_upsert"}.issubset(names)
    assert {
        "observer_hotfix_enter",
        "mf_parallel_enter",
        "mf_batch_parallel_enter",
        "runtime_context_implementation_evidence",
        "runtime_context_worker_commit",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_reissue",
    }.issubset(names)
    tool_by_name = {tool["name"]: tool for tool in tools}
    assert "contract_execution_id" in tool_by_name["qa_session_register"][
        "inputSchema"
    ]["properties"]
    assert "contract_execution_id" in tool_by_name["qa_session_register"][
        "inputSchema"
    ]["required"]
    assert "qa_session_token_ref" in tool_by_name["graph_query"]["inputSchema"][
        "properties"
    ]
    assert "qa_session_token_ref" in tool_by_name["task_timeline_append"][
        "inputSchema"
    ]["properties"]
    for tool_name in (
        "contract_runtime_current",
        "contract_runtime_guide",
        "contract_runtime_precheck_line",
        "contract_runtime_submit_line",
    ):
        properties = tool_by_name[tool_name]["inputSchema"]["properties"]
        assert "backlog_id" in properties
        assert "qa_session_token_ref" in properties


def test_runtime_context_finish_time_worker_attestation_schema_requires_harness_type():
    finish_attestation = next(
        tool
        for tool in governance_mcp_server.TOOLS
        if tool["name"] == "runtime_context_finish_time_worker_attestation"
    )
    schema = finish_attestation["inputSchema"]
    assert {"project_id", "runtime_context_id", "harness_type"}.issubset(
        schema["required"]
    )
    harness_type = schema["properties"]["harness_type"]
    assert "Required for runtime_context_finish_time_worker_attestation" in (
        harness_type["description"]
    )
    assert "copy_safe_body" in harness_type["description"]


def test_runtime_context_worker_commit_schemas_expose_optional_lineage_authority():
    for tools in (governance_mcp_server.TOOLS, runtime_mcp_tools):
        worker_commit = next(
            tool for tool in tools if tool["name"] == "runtime_context_worker_commit"
        )
        schema = worker_commit["inputSchema"]
        properties = schema["properties"]
        assert properties["implementation_event_ref"] == {"type": "string"}
        assert properties["implementation_lineage_ref"] == {"type": "string"}
        assert properties["worker_implementation_lineage"] == {"type": "object"}
        assert "implementation_event_ref" not in schema["required"]


def test_observer_hotfix_enter_schemas_require_backlog_scope():
    for tools in (governance_mcp_server.TOOLS, runtime_mcp_tools):
        hotfix_enter = next(
            tool for tool in tools if tool["name"] == "observer_hotfix_enter"
        )
        schema = hotfix_enter["inputSchema"]
        assert {"project_id", "reason"}.issubset(schema["required"])
        assert {
            "backlog_id",
            "bug_id",
            "actor_role",
            "observer_session_id",
            "observer_route_token_ref",
        }.issubset(schema["properties"])
        assert {"required": ["backlog_id"]} in schema["anyOf"]
        assert {"required": ["bug_id"]} in schema["anyOf"]


def test_mf_parallel_enter_schemas_require_backlog_scope_and_worker_fence():
    for tools in (governance_mcp_server.TOOLS, runtime_mcp_tools):
        mf_parallel_enter = next(
            tool for tool in tools if tool["name"] == "mf_parallel_enter"
        )
        schema = mf_parallel_enter["inputSchema"]
        assert {"project_id", "reason"}.issubset(schema["required"])
        assert {
            "backlog_id",
            "bug_id",
            "actor_role",
            "worker_fence",
            "owned_files",
            "target_files",
            "contract_execution_id",
            "onboard_service_waiver",
        }.issubset(schema["properties"])
        assert {"required": ["backlog_id"]} in schema["anyOf"]
        assert {"required": ["bug_id"]} in schema["anyOf"]


def test_mf_batch_parallel_enter_schemas_require_batch_scope():
    for tools in (governance_mcp_server.TOOLS, runtime_mcp_tools):
        mf_batch_parallel_enter = next(
            tool for tool in tools if tool["name"] == "mf_batch_parallel_enter"
        )
        schema = mf_batch_parallel_enter["inputSchema"]
        assert {"project_id", "backlog_ids", "reason"}.issubset(schema["required"])
        assert {
            "backlog_id",
            "bug_id",
            "backlog_ids",
            "actor_role",
            "observer_session_id",
            "observer_route_token_ref",
            "onboard_service_waiver",
            "target_head_commit",
            "target_ref",
            "snapshot_id",
            "graph_snapshot_id",
            "preflight_mode",
            "merge_mode",
            "merge_queue_id",
            "metadata",
        }.issubset(schema["properties"])
        assert {"required": ["backlog_id"]} in schema["anyOf"]
        assert {"required": ["bug_id"]} in schema["anyOf"]


def test_mcp_runtime_context_write_tools_dispatch_to_canonical_facades(monkeypatch):
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True, "path": path}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    common = {
        "project_id": "demo",
        "runtime_context_id": "mfrctx-demo",
        "session_token": "worker-session",
        "fence_token": "fence-demo",
    }
    assert governance_mcp_server._dispatch_tool(
        "runtime_context_implementation_evidence",
        {**common, "changed_files": ["src/app.js"]},
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "runtime_context_worker_commit",
        {
            **common,
            "contract_execution_id": "cex-demo",
            "worker_commit_sha": "a" * 40,
        },
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "runtime_context_finish_time_worker_attestation",
        {**common, "graph_trace_ids": ["gqt-demo"]},
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "runtime_context_finish_gate",
        {**common, "checkpoint_id": "ckpt-demo"},
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "runtime_context_session_token_initial_join",
        {
            "project_id": "demo",
            "runtime_context_id": "mfrctx-demo",
            "task_id": "worker-demo",
            "reason": "host adapter needs first worker auth env",
            "ttl_seconds": 1200,
        },
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "runtime_context_session_token_reissue",
        {**common, "task_id": "worker-demo", "parent_task_id": "AC-DEMO"},
    )["ok"] is True

    assert "implementation_event_ref" not in calls[1][2]
    assert calls == [
        (
            "POST",
            "/api/graph-governance/demo/runtime-contexts/mfrctx-demo/implementation-evidence",
            {
                "runtime_context_id": "mfrctx-demo",
                "session_token": "worker-session",
                "fence_token": "fence-demo",
                "changed_files": ["src/app.js"],
            },
        ),
        (
            "POST",
            "/api/graph-governance/demo/runtime-contexts/mfrctx-demo/worker-commit",
            {
                "runtime_context_id": "mfrctx-demo",
                "session_token": "worker-session",
                "fence_token": "fence-demo",
                "contract_execution_id": "cex-demo",
                "worker_commit_sha": "a" * 40,
            },
        ),
        (
            "POST",
            "/api/graph-governance/demo/runtime-contexts/mfrctx-demo/finish-time-worker-attestation",
            {
                "runtime_context_id": "mfrctx-demo",
                "session_token": "worker-session",
                "fence_token": "fence-demo",
                "graph_trace_ids": ["gqt-demo"],
            },
        ),
        (
            "POST",
            "/api/graph-governance/demo/runtime-contexts/mfrctx-demo/finish-gate",
            {
                "runtime_context_id": "mfrctx-demo",
                "session_token": "worker-session",
                "fence_token": "fence-demo",
                "checkpoint_id": "ckpt-demo",
            },
        ),
        (
            "POST",
            "/api/graph-governance/demo/runtime-contexts/mfrctx-demo/session-token/initial-join",
            {
                "runtime_context_id": "mfrctx-demo",
                "task_id": "worker-demo",
                "reason": "host adapter needs first worker auth env",
                "ttl_seconds": 1200,
            },
        ),
        (
            "POST",
            "/api/graph-governance/demo/runtime-contexts/mfrctx-demo/session-token/reissue",
            {
                "runtime_context_id": "mfrctx-demo",
                "session_token": "worker-session",
                "fence_token": "fence-demo",
                "task_id": "worker-demo",
                "parent_task_id": "AC-DEMO",
            },
        ),
    ]


def test_governance_mcp_hotfix_enter_dispatches_to_runtime_facade(monkeypatch):
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True, "successor_contract_execution_id": "cex-hotfix"}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    result = governance_mcp_server._dispatch_tool(
        "observer_hotfix_enter",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-HOTFIX",
            "task_id": "hotfix-task",
            "reason": "Human approved runtime repair.",
            "actor_role": "observer",
            "route_token_ref": "rtok-hotfix",
            "observer_session_id": "obs-session-hotfix",
            "observer_route_token_ref": "rtok-observer-hotfix",
        },
    )

    assert result["successor_contract_execution_id"] == "cex-hotfix"
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/hotfix/enter",
            {
                "backlog_id": "AC-HOTFIX",
                "task_id": "hotfix-task",
                "reason": "Human approved runtime repair.",
                "actor_role": "observer",
                "route_token_ref": "rtok-hotfix",
                "observer_session_id": "obs-session-hotfix",
                "observer_route_token_ref": "rtok-observer-hotfix",
            },
        )
    ]


def test_governance_mcp_mf_parallel_enter_dispatches_to_runtime_facade(monkeypatch):
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True, "successor_contract_execution_id": "cex-mf-parallel"}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    result = governance_mcp_server._dispatch_tool(
        "mf_parallel_enter",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-PARALLEL",
            "task_id": "parallel-task",
            "reason": "Human approved parallel repair.",
            "actor_role": "observer",
            "route_token_ref": "rtok-parallel",
            "onboard_service_waiver": True,
            "worker_fence": {"fence_token": "fence-parallel"},
            "owned_files": ["agent/governance/server.py"],
        },
    )

    assert result["successor_contract_execution_id"] == "cex-mf-parallel"
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/mf-parallel/enter",
            {
                "backlog_id": "AC-PARALLEL",
                "task_id": "parallel-task",
                "reason": "Human approved parallel repair.",
                "actor_role": "observer",
                "route_token_ref": "rtok-parallel",
                "onboard_service_waiver": True,
                "worker_fence": {"fence_token": "fence-parallel"},
                "owned_files": ["agent/governance/server.py"],
            },
        )
    ]


def test_governance_mcp_mf_batch_parallel_enter_dispatches_to_runtime_facade(
    monkeypatch,
):
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True, "batch_id": "batch-1"}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    result = governance_mcp_server._dispatch_tool(
        "mf_batch_parallel_enter",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-BATCH",
            "backlog_ids": ["AC-ONE", "AC-TWO"],
            "task_id": "batch-task",
            "reason": "Human approved batch repair.",
            "actor_role": "observer",
            "route_token_ref": "rtok-batch",
            "observer_session_id": "obs-batch",
            "onboard_service_waiver": True,
            "target_head_commit": "abc123",
            "target_ref": "refs/heads/main",
            "preflight_mode": "parallel",
            "merge_queue_id": "mq-batch",
            "metadata": {"source": "test"},
        },
    )

    assert result["batch_id"] == "batch-1"
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/mf-batch-parallel/enter",
            {
                "backlog_id": "AC-BATCH",
                "backlog_ids": ["AC-ONE", "AC-TWO"],
                "task_id": "batch-task",
                "reason": "Human approved batch repair.",
                "actor_role": "observer",
                "route_token_ref": "rtok-batch",
                "observer_session_id": "obs-batch",
                "onboard_service_waiver": True,
                "target_head_commit": "abc123",
                "target_ref": "refs/heads/main",
                "preflight_mode": "parallel",
                "merge_queue_id": "mq-batch",
                "metadata": {"source": "test"},
            },
        )
    ]


def test_governance_mcp_mf_timeline_precheck_forwards_close_commit(monkeypatch):
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True, "can_close": False}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    result = governance_mcp_server._dispatch_tool(
        "mf_timeline_precheck",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-COMMIT",
            "view": "repair",
            "include_events": True,
            "limit": 25,
            "commit_sha": "abc123",
        },
    )

    assert result == {"ok": True, "can_close": False}
    assert calls == [
        (
            "GET",
            "/api/backlog/aming-claw/BUG-COMMIT/timeline-gate?"
            "include_events=true&view=repair&limit=25&commit_sha=abc123",
            None,
        )
    ]


def test_tool_dispatcher_hotfix_enter_posts_runtime_facade():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "successor_contract_execution_id": "cex-hotfix"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "observer_hotfix_enter",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-HOTFIX",
            "task_id": "hotfix-task",
            "reason": "Human approved runtime repair.",
            "actor_role": "observer",
            "route_token_ref": "rtok-hotfix",
            "observer_session_id": "obs-session-hotfix",
            "observer_route_token_ref": "rtok-observer-hotfix",
        },
    )

    assert result["successor_contract_execution_id"] == "cex-hotfix"
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/hotfix/enter",
            {
                "backlog_id": "AC-HOTFIX",
                "task_id": "hotfix-task",
                "reason": "Human approved runtime repair.",
                "actor_role": "observer",
                "route_token_ref": "rtok-hotfix",
                "observer_session_id": "obs-session-hotfix",
                "observer_route_token_ref": "rtok-observer-hotfix",
            },
        )
    ]


def test_tool_dispatcher_mf_parallel_enter_posts_runtime_facade():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "successor_contract_execution_id": "cex-mf-parallel"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "mf_parallel_enter",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-PARALLEL",
            "task_id": "parallel-task",
            "reason": "Human approved parallel repair.",
            "actor_role": "observer",
            "route_token_ref": "rtok-parallel",
            "onboard_service_waiver": True,
            "worker_fence": {"fence_token": "fence-parallel"},
            "owned_files": ["agent/governance/server.py"],
        },
    )

    assert result["successor_contract_execution_id"] == "cex-mf-parallel"
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/mf-parallel/enter",
            {
                "backlog_id": "AC-PARALLEL",
                "task_id": "parallel-task",
                "reason": "Human approved parallel repair.",
                "actor_role": "observer",
                "route_token_ref": "rtok-parallel",
                "onboard_service_waiver": True,
                "worker_fence": {"fence_token": "fence-parallel"},
                "owned_files": ["agent/governance/server.py"],
            },
        )
    ]


def test_tool_dispatcher_mf_batch_parallel_enter_posts_runtime_facade():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "batch_id": "batch-1"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "mf_batch_parallel_enter",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-BATCH",
            "backlog_ids": ["AC-ONE", "AC-TWO"],
            "task_id": "batch-task",
            "reason": "Human approved batch repair.",
            "actor_role": "observer",
            "route_token_ref": "rtok-batch",
            "observer_session_id": "obs-batch",
            "onboard_service_waiver": True,
            "target_head_commit": "abc123",
            "target_ref": "refs/heads/main",
            "preflight_mode": "parallel",
            "merge_queue_id": "mq-batch",
            "metadata": {"source": "test"},
        },
    )

    assert result["batch_id"] == "batch-1"
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/mf-batch-parallel/enter",
            {
                "backlog_id": "AC-BATCH",
                "backlog_ids": ["AC-ONE", "AC-TWO"],
                "task_id": "batch-task",
                "reason": "Human approved batch repair.",
                "actor_role": "observer",
                "route_token_ref": "rtok-batch",
                "observer_session_id": "obs-batch",
                "onboard_service_waiver": True,
                "target_head_commit": "abc123",
                "target_ref": "refs/heads/main",
                "preflight_mode": "parallel",
                "merge_queue_id": "mq-batch",
                "metadata": {"source": "test"},
            },
        )
    ]


def test_mcp_stdio_backlog_close_schema_exposes_route_gate_fields():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = responses[0]["result"]["tools"]
    backlog_close = next(tool for tool in tools if tool["name"] == "backlog_close")
    properties = backlog_close["inputSchema"]["properties"]
    assert "contract_execution_id" in properties
    assert "route_token" in properties
    assert "route_token_ref" in properties
    assert "route_waiver" in properties
    assert "route_token_waiver" in properties
    runtime_backlog_close = next(
        tool for tool in runtime_mcp_tools if tool["name"] == "backlog_close"
    )
    assert (
        "contract_execution_id"
        in runtime_backlog_close["inputSchema"]["properties"]
    )


def test_mcp_stdio_mf_timeline_precheck_schema_exposes_close_commit_aliases():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = responses[0]["result"]["tools"]
    precheck = next(tool for tool in tools if tool["name"] == "mf_timeline_precheck")
    properties = precheck["inputSchema"]["properties"]
    assert {
        "close_commit",
        "commit",
        "commit_sha",
        "target_head_commit",
        "head_commit",
    }.issubset(properties)


def test_mcp_stdio_observer_route_context_issue_schema_is_listed():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = responses[0]["result"]["tools"]
    route_issue = next(tool for tool in tools if tool["name"] == "observer_route_context_issue")
    assert "runtime_context.current_values.merge_queue_id" in route_issue[
        "description"
    ]
    properties = route_issue["inputSchema"]["properties"]
    assert {
        "caller_role",
        "backlog_id",
        "task_id",
        "target_files",
        "allowed_actions",
        "evidence_refs",
        "close_commit",
    }.issubset(properties)


def test_mcp_stdio_public_safe_batch_close_blocker_schema_guidance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = responses[0]["result"]["tools"]
    reconcile = next(
        tool for tool in tools if tool["name"] == "graph_current_full_reconcile"
    )
    assert "route_proof_diagnostics" in reconcile["description"]
    assert "raw route tokens" in reconcile["description"]

    queue_status = next(
        tool for tool in tools if tool["name"] == "parallel_branch_merge_queue_status"
    )
    properties = queue_status["inputSchema"]["properties"]
    assert "runtime_context.current_values.merge_queue_id" in properties["flow"][
        "description"
    ]
    assert "freshly issued route-token response" in properties["merge_queue_id"][
        "description"
    ]


def test_mcp_stdio_backlog_audit_archive_schema_exposes_evidence_shape():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = responses[0]["result"]["tools"]
    audit_archive = next(tool for tool in tools if tool["name"] == "backlog_audit_archive")
    properties = audit_archive["inputSchema"]["properties"]
    assert {
        "commit",
        "reason",
        "timeline_precheck",
        "failure_audit",
        "qa_acceptance",
        "audit_close_gate",
        "verification",
        "graph_snapshot",
    }.issubset(properties)
    assert "route_token" in properties
    assert "route_token_ref" in properties
    assert "route_waiver" in properties
    assert "route_token_waiver" in properties
    assert audit_archive["inputSchema"]["required"] == [
        "project_id",
        "bug_id",
        "commit",
        "reason",
    ]

    legacy_archive = next(
        tool for tool in governance_mcp_server.TOOLS if tool["name"] == "backlog_audit_archive"
    )
    legacy_properties = legacy_archive["inputSchema"]["properties"]
    assert {"failure_audit", "qa_acceptance", "audit_close_gate"}.issubset(
        legacy_properties
    )


def test_mcp_stdio_protected_write_schemas_expose_route_gate_fields():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    for name in ("backlog_upsert", "task_timeline_append"):
        properties = tools[name]["inputSchema"]["properties"]
        assert "route_token" in properties
        assert "route_token_ref" in properties
        assert "route_waiver" in properties
        assert "route_token_waiver" in properties


def test_mcp_stdio_parallel_branch_startup_schema_exposes_read_receipt_bridge_fields():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    properties = tools["parallel_branch_startup"]["inputSchema"]["properties"]
    assert {
        "actual_host_worker_id",
        "worker_session_id",
        "worker_transcript_ref",
        "worker_transcript_path",
        "harness_type",
        "filer_principal",
        "route_token_ref",
        "read_receipt_hash",
        "read_receipt_event_id",
    }.issubset(properties)


def test_mcp_stdio_initial_join_schema_exposes_actual_host_identity_fields():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    properties = tools["runtime_context_session_token_initial_join"]["inputSchema"][
        "properties"
    ]
    assert {
        "agent_id",
        "actual_host_worker_id",
        "host_worker_id",
        "worker_session_id",
        "host_startup_id",
        "host_session_id",
    }.issubset(properties)


def test_mcp_stdio_reissue_schema_requires_worker_auth_proof():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    schema = tools["runtime_context_session_token_reissue"]["inputSchema"]
    assert {
        "project_id",
        "runtime_context_id",
        "task_id",
        "fence_token",
        "session_token",
    }.issubset(schema["required"])
    assert {
        "parent_task_id",
        "target_project_root",
        "ttl_seconds",
    }.issubset(schema["properties"])


def test_mcp_stdio_parallel_branch_allocate_schema_exposes_dispatch_ready_fields():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    properties = tools["parallel_branch_allocate"]["inputSchema"]["properties"]
    assert {
        "observer_command_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
        "owned_files",
        "target_files",
        "route_identity",
        "canonical_route_identity",
        "parent_route_identity",
        "parent_route_lineage",
        "child_route_lineage",
        "route_lineage",
        "route_token_ref",
    }.issubset(properties)
    for key in (
        "observer_command_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
    ):
        assert properties[key]["type"] == "string"
    assert properties["owned_files"]["type"] == "array"
    assert properties["owned_files"]["items"]["type"] == "string"
    assert properties["target_files"]["type"] == "array"
    assert properties["target_files"]["items"]["type"] == "string"
    assert properties["route_identity"]["type"] == "object"
    assert properties["canonical_route_identity"]["type"] == "object"
    assert properties["parent_route_identity"]["type"] == "object"
    assert "runtime_context.current_values.merge_queue_id" in properties[
        "merge_queue_id"
    ]["description"]
    assert properties["route_token_ref"]["type"] == "string"
    assert (
        properties["route_token_ref"]["description"]
        == "Opaque server-registered route token reference accepted by protected HTTP facades."
    )


def test_mcp_stdio_parallel_branch_allocate_schema_fields_forwarded():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "path": path}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    route_identity = {
        "route_id": "route-worker",
        "route_context_hash": "sha256:route-worker",
        "prompt_contract_id": "rprompt-worker",
        "prompt_contract_hash": "sha256:prompt-worker",
        "route_token_ref": "rtok-worker",
        "visible_injection_manifest_hash": "sha256:visible-worker",
    }
    parent_route_identity = {
        "route_id": "route-parent",
        "route_context_hash": "sha256:route-parent",
        "prompt_contract_id": "rprompt-parent",
        "prompt_contract_hash": "sha256:prompt-parent",
        "route_token_ref": "rtok-parent",
        "visible_injection_manifest_hash": "sha256:visible-parent",
    }

    result = dispatcher.dispatch(
        "parallel_branch_allocate",
        {
            "project_id": "aming-claw",
            "task_id": "mf-sub-allocate",
            "parent_task_id": "AC-ALLOCATE",
            "backlog_id": "AC-ALLOCATE",
            "observer_command_id": "cmd-allocate",
            "route_id": route_identity["route_id"],
            "route_context_hash": route_identity["route_context_hash"],
            "prompt_contract_id": route_identity["prompt_contract_id"],
            "prompt_contract_hash": route_identity["prompt_contract_hash"],
            "visible_injection_manifest_hash": route_identity[
                "visible_injection_manifest_hash"
            ],
            "route_token_ref": route_identity["route_token_ref"],
            "route_identity": route_identity,
            "canonical_route_identity": route_identity,
            "parent_route_identity": parent_route_identity,
            "owned_files": ["agent/governance/mcp_server.py"],
            "target_files": ["agent/tests/test_mcp_server_stdio.py"],
            "workspace_root": "/repo/.worktrees",
            "worktree_path": "/repo/.worktrees/mf-sub-allocate",
            "worker_id": "worker-allocate",
            "fence_token": "fence-allocate",
            "base_commit": "base",
            "target_head_commit": "target",
            "merge_queue_id": "mq-allocate",
        },
    )

    assert result["ok"] is True
    assert calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/parallel-branches/allocate",
            {
                "task_id": "mf-sub-allocate",
                "parent_task_id": "AC-ALLOCATE",
                "backlog_id": "AC-ALLOCATE",
                "observer_command_id": "cmd-allocate",
                "route_id": route_identity["route_id"],
                "route_context_hash": route_identity["route_context_hash"],
                "prompt_contract_id": route_identity["prompt_contract_id"],
                "prompt_contract_hash": route_identity["prompt_contract_hash"],
                "visible_injection_manifest_hash": route_identity[
                    "visible_injection_manifest_hash"
                ],
                "route_token_ref": route_identity["route_token_ref"],
                "route_identity": route_identity,
                "canonical_route_identity": route_identity,
                "parent_route_identity": parent_route_identity,
                "owned_files": ["agent/governance/mcp_server.py"],
                "target_files": ["agent/tests/test_mcp_server_stdio.py"],
                "workspace_root": "/repo/.worktrees",
                "worktree_path": "/repo/.worktrees/mf-sub-allocate",
                "worker_id": "worker-allocate",
                "fence_token": "fence-allocate",
                "base_commit": "base",
                "target_head_commit": "target",
                "merge_queue_id": "mq-allocate",
            },
        )
    ]


def test_governance_mcp_parallel_branch_allocate_schema_and_dispatch(monkeypatch):
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True, "path": path}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    tool = next(
        item for item in governance_mcp_server.TOOLS
        if item["name"] == "parallel_branch_allocate"
    )
    properties = tool["inputSchema"]["properties"]
    assert {
        "observer_command_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
        "owned_files",
        "target_files",
        "route_identity",
        "canonical_route_identity",
        "parent_route_identity",
    }.issubset(properties)

    result = governance_mcp_server._dispatch_tool(
        "parallel_branch_allocate",
        {
            "project_id": "aming-claw",
            "task_id": "mf-sub-allocate",
            "observer_command_id": "cmd-allocate",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-allocate",
            "owned_files": ["agent/governance/mcp_server.py"],
            "target_files": ["agent/tests/test_mcp_server_stdio.py"],
        },
    )

    assert result["ok"] is True
    assert calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/parallel-branches/allocate",
            {
                "task_id": "mf-sub-allocate",
                "observer_command_id": "cmd-allocate",
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-allocate",
                "owned_files": ["agent/governance/mcp_server.py"],
                "target_files": ["agent/tests/test_mcp_server_stdio.py"],
            },
        )
    ]


def test_mcp_stdio_observer_repair_run_plan_schema_is_read_only_entrypoint():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    schema = tools["observer_repair_run_plan"]["inputSchema"]
    properties = schema["properties"]
    assert schema["required"] == ["project_id"]
    assert "root_backlog_ids" in properties
    assert "blockers" in properties
    assert "include_timeline_precheck" in properties
    assert "version_check" in properties
    assert "route_token" not in properties
    assert "route_waiver" not in properties

    route_schema = tools["observer_repair_run_route_evidence"]["inputSchema"]
    route_properties = route_schema["properties"]
    assert route_schema["required"] == ["project_id"]
    assert "root_backlog_ids" in route_properties
    assert "record" in route_properties
    assert "action_precheck_id" in route_properties
    assert "version_check" in route_properties
    assert "route_token" not in route_properties
    assert "route_waiver" not in route_properties


def test_governance_mcp_runtime_context_current_tool_is_read_only(monkeypatch):
    tool_names = {tool["name"] for tool in governance_mcp_server.TOOLS}
    assert "runtime_context_current" in tool_names
    schema = next(
        tool for tool in governance_mcp_server.TOOLS if tool["name"] == "runtime_context_current"
    )["inputSchema"]
    assert schema["required"] == ["project_id", "runtime_context_id"]
    assert "fence_token" in schema["properties"]
    assert "parent_task_id" in schema["properties"]
    assert "session_token" in schema["properties"]
    assert "target_project_root" in schema["properties"]

    calls = []

    def fake_http(method: str, path: str, body: dict | None = None):
        calls.append((method, path, body))
        return {"ok": True, "view": "worker_view"}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    result = governance_mcp_server._dispatch_tool(
        "runtime_context_current",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-test",
            "fence_token": "fence-test",
            "parent_task_id": "AC-PARENT",
            "view": "all",
            "graph_trace_id": "gqt-test",
        },
    )

    assert result == {"ok": True, "view": "worker_view"}
    assert calls == [
        (
            "GET",
            "/api/graph-governance/aming-claw/runtime-contexts/"
            "mfrctx-test/current-state?"
            "fence_token=fence-test&parent_task_id=AC-PARENT&view=all&graph_trace_id=gqt-test",
            None,
        )
    ]


def test_governance_mcp_runtime_context_worker_guide_tool_is_read_only(monkeypatch):
    tool = next(
        tool for tool in governance_mcp_server.TOOLS if tool["name"] == "runtime_context_worker_guide"
    )
    schema = tool["inputSchema"]
    assert schema["required"] == ["project_id", "runtime_context_id"]
    assert "read/write guide" in tool["description"]
    assert "fence_token" in schema["properties"]
    assert "parent_task_id" in schema["properties"]
    assert "view" in schema["properties"]
    assert "graph_trace_id" in schema["properties"]
    assert "session_token" in schema["properties"]
    assert "target_project_root" in schema["properties"]
    assert "route_token" not in schema["properties"]
    assert "route_waiver" not in schema["properties"]

    calls = []

    def fake_http(method: str, path: str, body: dict | None = None):
        calls.append((method, path, body))
        return {"ok": True, "guide": {"intent": "read_write"}}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    result = governance_mcp_server._dispatch_tool(
        "runtime_context_worker_guide",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-test",
            "fence_token": "fence-test",
            "parent_task_id": "AC-PARENT",
            "view": "worker_view",
            "graph_trace_id": "gqt-test",
            "session_token": "session-test",
            "target_project_root": "/repo/fixture",
        },
    )

    assert result == {"ok": True, "guide": {"intent": "read_write"}}
    assert calls == [
        (
            "GET",
            "/api/graph-governance/aming-claw/runtime-contexts/"
            "mfrctx-test/worker-guide?"
            "fence_token=fence-test&parent_task_id=AC-PARENT&view=worker_view&"
            "graph_trace_id=gqt-test&session_token=session-test&"
            "target_project_root=%2Frepo%2Ffixture",
            None,
        )
    ]


def test_worker_auth_env_is_added_only_at_runtime_mcp_http_boundary(monkeypatch):
    session_token = "worker-session-secret"
    fence_token = "worker-fence-secret"
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", session_token)
    monkeypatch.setenv("AMING_WORKER_FENCE_TOKEN", fence_token)
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "trace_id": "gqt-worker-env"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    arguments = {
        "project_id": "aming-claw",
        "tool": "function_index",
        "args": {"query": "probe_executable"},
        "query_source": "mf_subagent",
        "query_purpose": "subagent_context_build",
        "worker_role": "mf_sub",
        "runtime_context_id": "mfrctx-worker-env",
    }

    result = dispatcher.dispatch("graph_query", arguments)

    assert result == {"ok": True, "trace_id": "gqt-worker-env"}
    assert "session_token" not in arguments
    assert "fence_token" not in arguments
    assert calls[0][2]["session_token"] == session_token
    assert calls[0][2]["fence_token"] == fence_token
    assert session_token not in json.dumps(result)
    assert fence_token not in json.dumps(result)


def test_worker_auth_env_reaches_standalone_worker_guide_without_mutating_args(
    monkeypatch,
):
    session_token = "standalone-worker-session-secret"
    fence_token = "standalone-worker-fence-secret"
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", session_token)
    monkeypatch.setenv("AMING_WORKER_FENCE_TOKEN", fence_token)
    calls = []

    def fake_http(method: str, path: str, body: dict | None = None):
        calls.append((method, path, body))
        return {"ok": True, "guide": {"next_legal_action": "read"}}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    arguments = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-worker-env",
        "parent_task_id": "cex-parent",
        "target_project_root": "/repo/worker",
    }

    result = governance_mcp_server._dispatch_tool(
        "runtime_context_worker_guide",
        arguments,
    )

    assert "session_token" not in arguments
    assert "fence_token" not in arguments
    assert urllib.parse.quote(session_token, safe="") in calls[0][1]
    assert urllib.parse.quote(fence_token, safe="") in calls[0][1]
    assert session_token not in json.dumps(result)
    assert fence_token not in json.dumps(result)


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_path"),
    [
        (
            "runtime_context_read_receipt",
            {
                "project_id": "aming-claw",
                "runtime_context_id": "mfrctx-worker-env",
                "task_id": "worker-env-task",
            },
            "/runtime-contexts/mfrctx-worker-env/read-receipts",
        ),
        (
            "parallel_branch_startup",
            {
                "project_id": "aming-claw",
                "task_id": "worker-env-task",
                "worker_role": "mf_sub",
            },
            "/parallel-branches/startup",
        ),
    ],
)
def test_worker_auth_env_is_added_to_worker_write_facades(
    monkeypatch,
    tool_name,
    arguments,
    expected_path,
):
    session_token = "write-worker-session-secret"
    fence_token = "write-worker-fence-secret"
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", session_token)
    monkeypatch.setenv("AMING_WORKER_FENCE_TOKEN", fence_token)
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "status": "accepted"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(tool_name, arguments)

    assert result == {"ok": True, "status": "accepted"}
    assert "session_token" not in arguments
    assert "fence_token" not in arguments
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert expected_path in calls[0][1]
    assert calls[0][2]["session_token"] == session_token
    assert calls[0][2]["fence_token"] == fence_token


def test_worker_auth_env_conflict_fails_closed_without_echo(monkeypatch):
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", "authoritative-session")
    monkeypatch.setenv("AMING_WORKER_FENCE_TOKEN", "authoritative-fence")

    with pytest.raises(ValueError) as caught:
        governance_mcp_server._dispatch_tool(
            "runtime_context_worker_guide",
            {
                "project_id": "aming-claw",
                "runtime_context_id": "mfrctx-worker-env",
                "session_token": "conflicting-session",
            },
        )

    assert str(caught.value) == "worker tool auth conflicts with the host environment"
    assert "authoritative" not in str(caught.value)
    assert "conflicting" not in str(caught.value)


def test_mcp_dispatcher_runtime_context_initial_join_posts_canonical_facade():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "status": "session_token_initial_join_issued"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "runtime_context_session_token_initial_join",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-join",
            "task_id": "worker-join",
            "parent_task_id": "AC-JOIN",
            "target_project_root": "/repo/fixture",
            "route_context_hash": "sha256:route-join",
            "prompt_contract_id": "prompt-join",
            "reason": "host adapter needs first worker auth env",
            "ttl_seconds": 1200,
        },
    )

    assert result == {"ok": True, "status": "session_token_initial_join_issued"}
    assert calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/runtime-contexts/"
            "mfrctx-join/session-token/initial-join",
            {
                "runtime_context_id": "mfrctx-join",
                "task_id": "worker-join",
                "parent_task_id": "AC-JOIN",
                "target_project_root": "/repo/fixture",
                "route_context_hash": "sha256:route-join",
                "prompt_contract_id": "prompt-join",
                "reason": "host adapter needs first worker auth env",
                "ttl_seconds": 1200,
            },
        )
    ]


def test_mcp_dispatcher_runtime_context_reissue_posts_canonical_facade():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "status": "session_token_reissued"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "runtime_context_session_token_reissue",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-reissue",
            "task_id": "worker-reissue",
            "parent_task_id": "AC-REISSUE",
            "target_project_root": "/repo/fixture",
            "session_token": "old-session-token",
            "fence_token": "fence-reissue",
            "ttl_seconds": 1200,
        },
    )

    assert result == {"ok": True, "status": "session_token_reissued"}
    assert calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/runtime-contexts/"
            "mfrctx-reissue/session-token/reissue",
            {
                "runtime_context_id": "mfrctx-reissue",
                "task_id": "worker-reissue",
                "parent_task_id": "AC-REISSUE",
                "target_project_root": "/repo/fixture",
                "session_token": "old-session-token",
                "fence_token": "fence-reissue",
                "ttl_seconds": 1200,
            },
        )
    ]


def test_mcp_backlog_close_forwards_route_gate_payloads():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    route_token = {
        "route_context_hash": "sha256:test-route",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "backlog_close",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:event-1"],
    }
    route_waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": "sha256:test-route-waiver",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "backlog_close",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "reason": "Unit test supplies explicit route waiver evidence.",
        "timeline_evidence": {"event_id": "event-2"},
    }

    result = dispatcher.dispatch(
        "backlog_close",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-ROUTE",
            "commit": "abc123",
            "actor": "observer",
            "contract_execution_id": "cex-close-authority-test",
            "route_token": route_token,
            "route_token_ref": "rtok-close-test",
            "route_waiver": route_waiver,
        },
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "POST",
            "/api/backlog/aming-claw/BUG-ROUTE/close",
            {
                "commit": "abc123",
                "actor": "observer",
                "contract_execution_id": "cex-close-authority-test",
                "route_token": route_token,
                "route_token_ref": "rtok-close-test",
                "route_waiver": route_waiver,
            },
        )
    ]


def test_mcp_mf_timeline_precheck_forwards_close_commit_aliases():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "mf_timeline_precheck",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-COMMIT",
            "view": "repair",
            "include_events": True,
            "limit": 25,
            "close_commit": "abc123",
        },
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "GET",
            "/api/backlog/aming-claw/BUG-COMMIT/timeline-gate?"
            "view=repair&include_events=true&limit=25&close_commit=abc123",
            None,
        )
    ]


def test_mcp_observer_route_context_issue_forwards_token_request():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "route_token_ref": "rtok-test"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "observer_route_context_issue",
        {
            "project_id": "aming-claw",
            "caller_role": "observer",
            "backlog_id": "BUG-ROUTE",
            "task_id": "BUG-ROUTE",
            "target_files": ["src/app.py"],
            "allowed_actions": ["backlog_close"],
            "evidence_refs": ["timeline:1"],
            "close_commit": "abc123",
        },
    )

    assert result == {"ok": True, "route_token_ref": "rtok-test"}
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer/route-context/issue",
            {
                "caller_role": "observer",
                "backlog_id": "BUG-ROUTE",
                "task_id": "BUG-ROUTE",
                "target_files": ["src/app.py"],
                "allowed_actions": ["backlog_close"],
                "evidence_refs": ["timeline:1"],
                "close_commit": "abc123",
            },
        )
    ]


def test_mcp_backlog_audit_archive_forwards_payload():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    route_waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": "sha256:test-route-waiver",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "backlog_audit_archive",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ARCHIVE"},
        "reason": "Unit test supplies explicit route waiver evidence.",
        "timeline_evidence": {"event_id": "event-archive"},
    }
    failure_audit = {
        "what_happened": "Implementation landed before real close evidence was recorded.",
        "non_reconstructable_evidence_reason": "Startup and close_ready cannot be backfilled.",
    }
    qa_acceptance = {
        "passed": True,
        "reviewer": "qa-reviewer-1",
        "reviewer_role": "qa",
        "tests": ["pytest"],
        "artifacts": ["artifact://pytest"],
    }
    audit_close_gate = {
        "allowed": True,
        "passed": True,
        "normal_close_gate": {"can_close": False},
    }

    result = dispatcher.dispatch(
        "backlog_audit_archive",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-ARCHIVE",
            "commit": "abc123",
            "reason": "Historical close evidence cannot be reconstructed.",
            "timeline_precheck": {"can_close": False},
            "failure_audit": failure_audit,
            "qa_acceptance": qa_acceptance,
            "audit_close_gate": audit_close_gate,
            "verification": {"tests": ["pytest"]},
            "route_waiver": route_waiver,
        },
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "POST",
            "/api/backlog/aming-claw/BUG-ARCHIVE/audit-archive",
            {
                "commit": "abc123",
                "reason": "Historical close evidence cannot be reconstructed.",
                "timeline_precheck": {"can_close": False},
                "failure_audit": failure_audit,
                "qa_acceptance": qa_acceptance,
                "audit_close_gate": audit_close_gate,
                "verification": {"tests": ["pytest"]},
                "route_waiver": route_waiver,
            },
        )
    ]


def test_mcp_protected_write_dispatch_forwards_route_gate_payloads():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    route_token = {
        "route_context_hash": "sha256:test-route",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "backlog_upsert",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:event-1"],
    }
    route_waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": "sha256:test-route-waiver",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "task_timeline_append",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "reason": "Unit test supplies explicit route waiver evidence.",
        "timeline_evidence": {"event_id": "event-2"},
    }

    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-ROUTE",
            "status": "FIXED",
            "route_token": route_token,
            "route_token_ref": "rtok-upsert-test",
        },
    )
    dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-ROUTE",
            "event_type": "mf.verification",
            "event_kind": "verification",
            "route_token_ref": "rtok-timeline-test",
            "route_waiver": route_waiver,
        },
    )

    assert calls == [
        (
            "POST",
            "/api/backlog/aming-claw/BUG-ROUTE",
            {
                "status": "FIXED",
                "route_token": route_token,
                "route_token_ref": "rtok-upsert-test",
            },
        ),
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {
                "backlog_id": "BUG-ROUTE",
                "event_type": "mf.verification",
                "event_kind": "verification",
                "route_token_ref": "rtok-timeline-test",
                "route_waiver": route_waiver,
            },
        ),
    ]


def test_mcp_protected_write_dispatch_preserves_structured_gate_failure():
    structured_failure = {
        "error": "route_token_required",
        "message": "route_token is required for protected governance action",
        "details": {
            "fault_domain": "caller_missing_route_evidence",
            "expected_behavior": True,
            "do_not_file_system_bug": True,
            "is_system_bug": False,
            "next_valid_actions": ["return_to_route_context_and_request_a_valid_route_token"],
            "system_bug_preconditions": ["valid route token was supplied and still rejected"],
        },
    }

    def fake_api(method: str, path: str, data: dict | None = None):
        return structured_failure

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-ROUTE",
            "event_type": "mf.verification",
            "event_kind": "verification",
        },
    )

    assert result["error"] == "route_token_required"
    assert result["details"]["fault_domain"] == "caller_missing_route_evidence"
    assert result["details"]["expected_behavior"] is True
    assert result["details"]["is_system_bug"] is False


def test_mcp_parallel_branch_startup_forwards_read_receipt_bridge_fields():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "status": "startup_recorded"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "parallel_branch_startup",
        {
            "project_id": "aming-claw",
            "task_id": "AC-STARTUP",
            "actual_host_worker_id": "host-session-2963",
            "worker_session_id": "host-session-2963",
            "worker_transcript_ref": "multi_agent:host-session-2963",
            "worker_transcript_path": "/tmp/host-session-2963.jsonl",
            "harness_type": "codex",
            "filer_principal": "host-session-2963",
            "route_token_ref": "timeline_event:2966;service_event:2967",
            "read_receipt_hash": "sha256:read-receipt",
            "read_receipt_event_id": "2963",
        },
    )

    assert result == {"ok": True, "status": "startup_recorded"}
    assert calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/parallel-branches/startup",
            {
                "task_id": "AC-STARTUP",
                "actual_host_worker_id": "host-session-2963",
                "worker_session_id": "host-session-2963",
                "worker_transcript_ref": "multi_agent:host-session-2963",
                "worker_transcript_path": "/tmp/host-session-2963.jsonl",
                "harness_type": "codex",
                "filer_principal": "host-session-2963",
                "route_token_ref": "timeline_event:2966;service_event:2967",
                "read_receipt_hash": "sha256:read-receipt",
                "read_receipt_event_id": "2963",
            },
        )
    ]


def test_mcp_contract_add_tools_expose_thin_guided_facade_only():
    tool_by_name = {tool["name"]: tool for tool in governance_mcp_server.TOOLS}

    assert {
        "onboard_contract_start",
        "onboard_contract_current",
        "onboard_contract_submit_line",
        "contract_add_start",
        "contract_add_current",
        "contract_add_submit_line",
        "contract_update_start",
        "contract_update_current",
        "contract_update_submit_line",
        "contract_runtime_current",
        "contract_runtime_guide",
        "contract_runtime_precheck_line",
        "contract_runtime_submit_line",
    }.issubset(tool_by_name)
    assert "contract_add_create" not in tool_by_name
    assert "contract_update_create" not in tool_by_name
    assert "contract_definition_create" not in tool_by_name
    assert "contract_definition_update" not in tool_by_name
    assert "contract_execution_id" not in tool_by_name["onboard_contract_start"][
        "inputSchema"
    ]["properties"]
    assert tool_by_name["onboard_contract_submit_line"]["inputSchema"]["required"] == [
        "project_id",
        "contract_execution_id",
    ]
    assert tool_by_name["contract_add_submit_line"]["inputSchema"]["required"] == [
        "project_id",
        "contract_execution_id",
    ]
    assert tool_by_name["contract_update_submit_line"]["inputSchema"]["required"] == [
        "project_id",
        "contract_execution_id",
    ]
    assert tool_by_name["contract_runtime_submit_line"]["inputSchema"]["required"] == [
        "project_id",
        "contract_execution_id",
    ]
    assert tool_by_name["contract_runtime_precheck_line"]["inputSchema"]["required"] == [
        "project_id",
        "contract_execution_id",
    ]
    assert "execution_state_revision" in tool_by_name["contract_runtime_submit_line"][
        "inputSchema"
    ]["properties"]
    assert set(tool_by_name["contract_runtime_submit_line"]["inputSchema"]["properties"]) >= {
        "backlog_id",
        "timeout_seconds",
        "definition_hash",
        "instruction_bundle_hash",
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "worker_role",
        "session_token_ref",
        "fence_token",
        "target_project_root",
        "qa_session_token",
        "evidence_owner_actor",
        "evidence_owner_role",
        "submitter_session",
        "submitter_principal",
        "materialized_from",
        "authorization_source",
        "qa_session_token_ref",
        "qa_evidence_provenance",
    }
    assert (
        tool_by_name["contract_runtime_precheck_line"]["inputSchema"]["properties"]
        == tool_by_name["contract_runtime_submit_line"]["inputSchema"]["properties"]
    )
    for tool_name in (
        "onboard_contract_start",
        "onboard_contract_current",
        "onboard_contract_submit_line",
        "contract_add_start",
        "contract_add_current",
        "contract_add_submit_line",
        "contract_update_start",
        "contract_update_current",
        "contract_update_submit_line",
        "contract_runtime_current",
        "contract_runtime_guide",
        "contract_runtime_precheck_line",
        "contract_runtime_submit_line",
    ):
        properties = tool_by_name[tool_name]["inputSchema"]["properties"]
        assert "observer_session_id" in properties
        assert "observer_route_token_ref" in properties


def test_governance_mcp_contract_runtime_qa_token_is_header_only(monkeypatch):
    calls = []

    def fake_http(
        method: str,
        path: str,
        data: dict | None = None,
        *,
        gov_token=None,
        timeout_seconds=None,
    ):
        calls.append((method, path, data, gov_token))
        return {"ok": True}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    tool_by_name = {
        tool["name"]: tool for tool in governance_mcp_server.TOOLS
    }
    assert "qa_session_token" in tool_by_name["task_timeline_append"][
        "inputSchema"
    ]["properties"]
    assert "qa_session_token_ref" in tool_by_name["task_timeline_append"][
        "inputSchema"
    ]["properties"]

    assert governance_mcp_server._dispatch_tool(
        "contract_runtime_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-direct-fix",
            "qa_session_token": "gov-qa-token",
        },
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "onboard_contract_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "qa_session_token": "gov-qa-token",
            "stage_id": "qa",
            "line_id": "qa_independent_verification",
            "evidence_kind": "independent_verification",
            "materialized_from": "qa_packet:qapkt-onboard",
            "submitter_principal": "observer:parent",
        },
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "contract_runtime_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-direct-fix",
            "qa_session_token": "gov-qa-token",
            "stage_id": "qa",
            "line_id": "qa_independent_verification",
            "evidence_kind": "independent_verification",
            "materialized_from": "qa_packet:qapkt-curie",
            "submitter_principal": "observer:parent",
        },
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "qa_session_token": "gov-qa-token",
            "backlog_id": "AC-QA",
            "task_id": "qa-task",
            "event_type": "qa.independent_verification",
            "event_kind": "independent_verification",
            "phase": "verification",
            "actor": "qa:curie",
            "status": "passed",
            "commit_sha": "a" * 40,
            "payload": {"graph_trace_ids": ["gqt-qa"]},
        },
    )["ok"] is True

    assert calls[0] == (
        "GET",
        "/api/projects/aming-claw/contract-runtime/cex-direct-fix/current-state?response_view=cli_current",
        None,
        "gov-qa-token",
    )
    assert calls[1][0:2] == (
        "POST",
        "/api/projects/aming-claw/onboard-contract/cex-onboard/line-writes",
    )
    assert calls[1][2]["materialized_from"] == "qa_packet:qapkt-onboard"
    assert "qa_session_token" not in calls[1][2]
    assert calls[1][3] == "gov-qa-token"
    assert calls[2][0:2] == (
        "POST",
        "/api/projects/aming-claw/contract-runtime/cex-direct-fix/line-writes",
    )
    assert calls[2][2]["materialized_from"] == "qa_packet:qapkt-curie"
    assert calls[2][2]["submitter_principal"] == "observer:parent"
    assert "qa_session_token" not in calls[2][2]
    assert calls[2][3] == "gov-qa-token"
    assert calls[3] == (
        "POST",
        "/api/task/aming-claw/timeline",
        {
            "backlog_id": "AC-QA",
            "task_id": "qa-task",
            "event_type": "qa.independent_verification",
            "event_kind": "independent_verification",
            "phase": "verification",
            "actor": "qa:curie",
            "status": "passed",
            "commit_sha": "a" * 40,
            "payload": {"graph_trace_ids": ["gqt-qa"]},
        },
        "gov-qa-token",
    )


def test_governance_mcp_contract_runtime_timeout_policy_is_bounded_and_configurable(
    monkeypatch,
):
    env_key = "AMING_CONTRACT_RUNTIME_MCP_TIMEOUT_SECONDS"
    monkeypatch.delenv(env_key, raising=False)

    assert governance_mcp_server._contract_runtime_mcp_timeout_seconds({}) == 120
    assert (
        governance_mcp_server._contract_runtime_mcp_timeout_seconds(
            {"timeout_seconds": 1}
        )
        == 10
    )
    assert (
        governance_mcp_server._contract_runtime_mcp_timeout_seconds(
            {"timeout_seconds": 60 * 60 + 1}
        )
        == 60 * 60
    )

    monkeypatch.setenv(env_key, "75")
    assert governance_mcp_server._contract_runtime_mcp_timeout_seconds({}) == 75
    assert (
        governance_mcp_server._contract_runtime_mcp_timeout_seconds(
            {"timeout_seconds": 45}
        )
        == 45
    )

    tool_by_name = {
        tool["name"]: tool for tool in governance_mcp_server.TOOLS
    }
    timeout_schema = tool_by_name["contract_runtime_submit_line"]["inputSchema"][
        "properties"
    ]["timeout_seconds"]
    assert timeout_schema["default"] == 120
    assert timeout_schema["minimum"] == 10
    assert timeout_schema["maximum"] == 60 * 60
    assert (
        tool_by_name["contract_runtime_precheck_line"]["inputSchema"]["properties"]
        ["timeout_seconds"]
        == timeout_schema
    )


def test_governance_mcp_contract_runtime_timeout_is_transport_only_with_disposition(
    monkeypatch,
):
    qa_token = "gov-qa-contract-runtime-timeout-secret"
    calls = []

    def fake_http(
        method: str,
        path: str,
        data: dict | None = None,
        *,
        gov_token=None,
        timeout_seconds=None,
    ):
        calls.append((method, path, data, gov_token, timeout_seconds))
        return {"ok": False, "error": "request_timeout", "message": "timed out"}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    common_args = {
        "project_id": "aming-claw",
        "contract_execution_id": "cex-timeout",
        "execution_state_revision": 10,
        "stage_id": "qa",
        "line_id": "qa_graph_context",
        "evidence_kind": "qa_graph_context",
        "qa_session_token": qa_token,
    }

    precheck = governance_mcp_server._dispatch_tool(
        "contract_runtime_precheck_line",
        {**common_args, "timeout_seconds": 45},
    )
    submit = governance_mcp_server._dispatch_tool(
        "contract_runtime_submit_line",
        {**common_args, "timeout_seconds": 75},
    )

    assert precheck["effective_timeout_seconds"] == 45
    assert precheck["transport_disposition"] == "ambiguous"
    assert precheck["write_disposition"] == "not_written"
    assert precheck["retry_disposition"] == "safe_to_retry_precheck"
    assert submit["effective_timeout_seconds"] == 75
    assert submit["transport_disposition"] == "ambiguous"
    assert submit["write_disposition"] == "ambiguous"
    assert (
        submit["retry_disposition"]
        == "poll_authoritative_current_state_before_retry"
    )
    assert submit["retry_guidance"]["automatic_retry"] is False
    assert submit["retry_guidance"]["exact_once_intent"] is True
    assert qa_token not in json.dumps(precheck)
    assert qa_token not in json.dumps(submit)

    assert [call[4] for call in calls] == [45, 75]
    for _, _, body, header_token, _ in calls:
        assert body is not None
        assert "timeout_seconds" not in body
        assert "qa_session_token" not in body
        assert header_token == qa_token


def test_governance_mcp_contract_runtime_retry_after_unchanged_poll_writes_once(
    monkeypatch,
):
    qa_token = "gov-qa-contract-runtime-exact-once-secret"
    state = {"revision": 10}
    submit_attempts = []
    successful_writes = []
    current_polls = []

    def fake_http(
        method: str,
        path: str,
        data: dict | None = None,
        *,
        gov_token=None,
        timeout_seconds=None,
    ):
        if "/current-state?" in path:
            current_polls.append((path, gov_token))
            return {
                "ok": True,
                "execution_state_revision": state["revision"],
                "completed_lines": [
                    {"line_id": "qa_graph_context"}
                    for _ in successful_writes
                ],
            }
        if path.endswith("/line-writes"):
            submit_attempts.append((data, gov_token, timeout_seconds))
            if len(submit_attempts) == 1:
                return {
                    "ok": False,
                    "error": "request_timeout",
                    "message": "timed out",
                }
            successful_writes.append(dict(data or {}))
            state["revision"] += 1
            return {
                "ok": True,
                "execution_state_revision": state["revision"],
                "completed_line": {"line_id": "qa_graph_context"},
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    submit_args = {
        "project_id": "aming-claw",
        "contract_execution_id": "cex-exact-once",
        "execution_state_revision": 10,
        "stage_id": "qa",
        "line_id": "qa_graph_context",
        "evidence_kind": "qa_graph_context",
        "qa_session_token": qa_token,
        "timeout_seconds": 33,
    }

    timed_out = governance_mcp_server._dispatch_tool(
        "contract_runtime_submit_line",
        submit_args,
    )
    assert timed_out["write_disposition"] == "ambiguous"
    assert len(submit_attempts) == 1
    assert successful_writes == []

    current = governance_mcp_server._dispatch_tool(
        "contract_runtime_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-exact-once",
            "qa_session_token": qa_token,
        },
    )
    assert current["execution_state_revision"] == 10
    assert current["completed_lines"] == []

    retried = governance_mcp_server._dispatch_tool(
        "contract_runtime_submit_line",
        submit_args,
    )
    assert retried["ok"] is True
    assert retried["execution_state_revision"] == 11
    assert len(submit_attempts) == 2
    assert len(successful_writes) == 1
    assert len(current_polls) == 1
    assert submit_attempts[0][0] == submit_attempts[1][0]
    assert all(attempt[1] == qa_token for attempt in submit_attempts)
    assert all(attempt[2] == 33 for attempt in submit_attempts)
    assert all("timeout_seconds" not in attempt[0] for attempt in submit_attempts)
    assert qa_token not in json.dumps(timed_out)


def test_managed_mcp_task_timeline_append_resolves_qa_ref_header_only_and_fails_closed():
    raw_token = "gov-qa-managed-timeline-secret"
    commit_sha = "a" * 40
    api_calls = []
    auth_calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        api_calls.append((method, path, data))
        if path == "/api/role/assign":
            return {
                "session_id": "ses-qa-managed-timeline",
                "principal_id": "qa:managed-timeline",
                "role": "qa",
                "scope": [],
                "token": raw_token,
                "expires_at": "2099-07-17T12:00:00Z",
            }
        return {"ok": True}

    def fake_api_with_role_token(
        method: str,
        path: str,
        data: dict | None = None,
        *,
        role_token: str,
    ):
        auth_calls.append((method, path, data, role_token))
        return {"ok": True}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    dispatcher._api_with_role_token = fake_api_with_role_token

    registered = dispatcher.dispatch(
        "qa_session_register",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-QA-MANAGED-TIMELINE",
            "task_id": "qa-task",
            "commit_sha": commit_sha,
            "contract_execution_id": "cex-qa-managed-timeline",
            "principal_id": "qa:managed-timeline",
        },
    )
    token_ref = registered["qa_session_token_ref"]
    timeline_args = {
        "project_id": "aming-claw",
        "backlog_id": "AC-QA-MANAGED-TIMELINE",
        "task_id": "qa-task",
        "commit_sha": commit_sha,
        "event_type": "qa.independent_verification",
        "event_kind": "independent_verification",
        "phase": "verification",
        "actor": "qa:managed-timeline",
        "status": "passed",
        "payload": {"graph_trace_ids": ["gqt-qa-managed-timeline"]},
        "qa_session_token_ref": token_ref,
    }

    assert dispatcher.dispatch("task_timeline_append", timeline_args) == {
        "ok": True
    }
    assert auth_calls == [
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {
                key: value
                for key, value in timeline_args.items()
                if key not in {"project_id", "qa_session_token_ref"}
            },
            raw_token,
        )
    ]
    forwarded_body = auth_calls[0][2]
    assert "qa_session_token_ref" not in forwarded_body
    assert "qa_session_token" not in forwarded_body
    assert raw_token not in json.dumps(forwarded_body, sort_keys=True)

    unknown = dispatcher.dispatch(
        "task_timeline_append",
        {**timeline_args, "qa_session_token_ref": "qa-session-ref-unknown"},
    )
    assert unknown["error"] == "qa_session_token_ref_unknown"

    missing_scope = dict(timeline_args)
    missing_scope.pop("task_id")
    missing = dispatcher.dispatch("task_timeline_append", missing_scope)
    assert missing["error"] == "qa_session_token_ref_scope_mismatch"
    assert "task_id" in missing["mismatched_fields"]

    cross_scope = dispatcher.dispatch(
        "task_timeline_append",
        {**timeline_args, "backlog_id": "AC-QA-OTHER"},
    )
    assert cross_scope["error"] == "qa_session_token_ref_scope_mismatch"
    assert "backlog_id" in cross_scope["mismatched_fields"]

    ambiguous = dispatcher.dispatch(
        "task_timeline_append",
        {**timeline_args, "qa_session_token": "other-raw-token"},
    )
    assert ambiguous["error"] == "qa_session_auth_ambiguous"
    assert len(auth_calls) == 1
    assert api_calls == [
        (
            "POST",
            "/api/role/assign",
            {
                "project_id": "aming-claw",
                "principal_id": "qa:managed-timeline",
                "role": "qa",
                "backlog_id": "AC-QA-MANAGED-TIMELINE",
                "task_id": "qa-task",
                "commit_sha": commit_sha,
            },
        )
    ]


def test_governance_mcp_task_timeline_append_rejects_managed_qa_ref_without_registry(
    monkeypatch,
):
    calls = []

    def fake_http(method: str, path: str, data: dict | None = None, *, gov_token=None):
        calls.append((method, path, data, gov_token))
        return {"ok": True}

    monkeypatch.setattr(
        governance_mcp_server,
        "_http_with_optional_gov_token",
        fake_http,
    )
    result = governance_mcp_server._dispatch_tool(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-QA-MANAGED-TIMELINE",
            "task_id": "qa-task",
            "commit_sha": "a" * 40,
            "event_type": "qa.independent_verification",
            "event_kind": "independent_verification",
            "qa_session_token_ref": "qa-session-ref-managed-elsewhere",
        },
    )

    assert result["error"] == "qa_session_token_ref_unavailable"
    assert calls == []


def test_mcp_contract_add_dispatches_to_guided_http_facade(monkeypatch):
    calls = []

    def fake_api(
        method: str,
        path: str,
        data: dict | None = None,
        **_kwargs,
    ):
        calls.append((method, path, data))
        return {"ok": True, "path": path}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_api)

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    assert governance_mcp_server._dispatch_tool(
        "onboard_contract_start",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-ONBOARD",
            "contract_execution_id": "cex-must-not-forward",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "onboard_contract_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )["ok"] is True
    assert governance_mcp_server._dispatch_tool(
        "onboard_contract_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "stage_id": "graph_context",
            "line_id": "graph_query_schema_trace",
            "evidence_kind": "graph_query_schema_trace",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_add_start",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-CONTRACT-ADD",
            "observer_session_id": "obs-contract-add",
            "observer_route_token_ref": "rtok-contract-add",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_add_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-contract-add",
            "observer_session_id": "obs-contract-add",
            "observer_route_token_ref": "rtok-contract-add",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_add_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-contract-add",
            "stage_id": "worker_precheck",
            "line_id": "worker_draft_precheck",
            "evidence_kind": "contract_draft_precheck",
            "observer_session_id": "obs-contract-add",
            "observer_route_token_ref": "rtok-contract-add",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_update_start",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-CONTRACT-UPDATE",
            "observer_session_id": "obs-contract-update",
            "observer_route_token_ref": "rtok-contract-update",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_update_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-contract-update",
            "observer_session_id": "obs-contract-update",
            "observer_route_token_ref": "rtok-contract-update",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_update_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-contract-update",
            "stage_id": "worker_previous_source",
            "line_id": "worker_previous_source_proof",
            "evidence_kind": "contract_previous_source_proof",
            "observer_session_id": "obs-contract-update",
            "observer_route_token_ref": "rtok-contract-update",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_runtime_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_runtime_guide",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_runtime_precheck_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "execution_state_revision": 1,
            "stage_id": "graph_context",
            "line_id": "graph_query_schema_trace",
            "evidence_kind": "graph_query_schema_trace",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
            "runtime_context_id": "rctx-worker",
            "task_id": "worker-task",
            "parent_task_id": "observer-task",
            "worker_role": "mf_sub",
            "session_token_ref": "sref-worker",
            "fence_token": "fence-worker",
            "target_project_root": "/tmp/worker",
        },
    )["ok"] is True
    assert dispatcher.dispatch(
        "contract_runtime_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "execution_state_revision": 1,
            "stage_id": "graph_context",
            "line_id": "graph_query_schema_trace",
            "evidence_kind": "graph_query_schema_trace",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
            "runtime_context_id": "rctx-worker",
            "task_id": "worker-task",
            "parent_task_id": "observer-task",
            "worker_role": "mf_sub",
            "session_token_ref": "sref-worker",
            "fence_token": "fence-worker",
            "target_project_root": "/tmp/worker",
        },
    )["ok"] is True

    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/onboard-contract/start",
            {
                "backlog_id": "AC-ONBOARD",
                "observer_session_id": "obs-onboard",
                "observer_route_token_ref": "rtok-onboard",
            },
        ),
        (
            "GET",
            "/api/projects/aming-claw/onboard-contract/cex-onboard/current-state?observer_session_id=obs-onboard&observer_route_token_ref=rtok-onboard",
            None,
        ),
        (
            "POST",
            "/api/projects/aming-claw/onboard-contract/cex-onboard/line-writes",
            {
                "stage_id": "graph_context",
                "line_id": "graph_query_schema_trace",
                "evidence_kind": "graph_query_schema_trace",
                "observer_session_id": "obs-onboard",
                "observer_route_token_ref": "rtok-onboard",
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-add/start",
            {
                "backlog_id": "AC-CONTRACT-ADD",
                "observer_session_id": "obs-contract-add",
                "observer_route_token_ref": "rtok-contract-add",
            },
        ),
        (
            "GET",
            "/api/projects/aming-claw/contract-add/cex-contract-add/current-state?observer_session_id=obs-contract-add&observer_route_token_ref=rtok-contract-add",
            None,
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-add/cex-contract-add/line-writes",
            {
                "stage_id": "worker_precheck",
                "line_id": "worker_draft_precheck",
                "evidence_kind": "contract_draft_precheck",
                "observer_session_id": "obs-contract-add",
                "observer_route_token_ref": "rtok-contract-add",
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-update/start",
            {
                "backlog_id": "AC-CONTRACT-UPDATE",
                "observer_session_id": "obs-contract-update",
                "observer_route_token_ref": "rtok-contract-update",
            },
        ),
        (
            "GET",
            "/api/projects/aming-claw/contract-update/cex-contract-update/current-state?observer_session_id=obs-contract-update&observer_route_token_ref=rtok-contract-update",
            None,
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-update/cex-contract-update/line-writes",
            {
                "stage_id": "worker_previous_source",
                "line_id": "worker_previous_source_proof",
                "evidence_kind": "contract_previous_source_proof",
                "observer_session_id": "obs-contract-update",
                "observer_route_token_ref": "rtok-contract-update",
            },
        ),
        (
            "GET",
            "/api/projects/aming-claw/contract-runtime/cex-onboard/current-state?response_view=cli_current&observer_session_id=obs-onboard&observer_route_token_ref=rtok-onboard",
            None,
        ),
        (
            "GET",
            "/api/projects/aming-claw/contract-runtime/cex-onboard/guide?response_view=cli_guide&observer_session_id=obs-onboard&observer_route_token_ref=rtok-onboard",
            None,
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-runtime/cex-onboard/line-writes/precheck",
            {
                "execution_state_revision": 1,
                "stage_id": "graph_context",
                "line_id": "graph_query_schema_trace",
                "evidence_kind": "graph_query_schema_trace",
                "observer_session_id": "obs-onboard",
                "observer_route_token_ref": "rtok-onboard",
                "runtime_context_id": "rctx-worker",
                "task_id": "worker-task",
                "parent_task_id": "observer-task",
                "worker_role": "mf_sub",
                "session_token_ref": "sref-worker",
                "fence_token": "fence-worker",
                "target_project_root": "/tmp/worker",
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-runtime/cex-onboard/line-writes",
            {
                "execution_state_revision": 1,
                "stage_id": "graph_context",
                "line_id": "graph_query_schema_trace",
                "evidence_kind": "graph_query_schema_trace",
                "observer_session_id": "obs-onboard",
                "observer_route_token_ref": "rtok-onboard",
                "runtime_context_id": "rctx-worker",
                "task_id": "worker-task",
                "parent_task_id": "observer-task",
                "worker_role": "mf_sub",
                "session_token_ref": "sref-worker",
                "fence_token": "fence-worker",
                "target_project_root": "/tmp/worker",
            },
        ),
    ]


def test_mcp_observer_repair_run_plan_dispatches_to_read_only_endpoint():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "repair_run_id": "repair-test"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "observer_repair_run_plan",
        {
            "project_id": "aming-claw",
            "root_backlog_ids": ["AC-ROUTE-FLOW-SESSION-GUIDANCE-20260602"],
            "blockers": ["route_token_required"],
            "include_timeline_precheck": True,
            "actor": "observer-test",
        },
    )

    assert result == {"ok": True, "repair_run_id": "repair-test"}
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer-repair-run/plan",
            {
                "root_backlog_ids": ["AC-ROUTE-FLOW-SESSION-GUIDANCE-20260602"],
                "blockers": ["route_token_required"],
                "include_timeline_precheck": True,
                "actor": "observer-test",
            },
        )
    ]


def test_mcp_observer_repair_run_route_evidence_dispatches_to_endpoint():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "mode": "dry_run"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

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

    assert result == {"ok": True, "mode": "dry_run"}
    assert calls == [
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


def test_mcp_stdio_resources_expose_skill_and_context_without_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/templates/list",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "aming-claw://skill"},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "aming-claw://current-context"},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": "aming-claw://seed-graph-summary"},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": {"uri": "aming-claw://self-graph-bundle-manifest"},
        },
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "resources/read",
            "params": {"uri": "aming-claw://graph-first"},
        },
    ])

    assert returncode == 0
    assert stderr == ""
    resources = {r["uri"]: r for r in responses[0]["result"]["resources"]}
    assert "aming-claw://skill" in resources
    assert resources["aming-claw://skill"]["name"] == "Aming Claw Onboard Skill"
    assert "aming-claw://current-context" in resources
    assert "aming-claw://seed-graph-summary" in resources
    assert "aming-claw://self-graph-bundle-manifest" in resources
    assert "aming-claw://self-graph-bundle/manifest" in resources
    assert "aming-claw://self-graph-bundle/graph-structure" in resources
    assert "aming-claw://self-graph-bundle/semantic-projection" in resources
    templates = responses[1]["result"]["resourceTemplates"]
    assert templates[0]["uriTemplate"] == "aming-claw://project/{project_id}/context"
    skill_text = responses[2]["result"]["contents"][0]["text"]
    assert "# Aming Claw Onboard" in skill_text
    assert "only active Aming Claw skill entrypoint" in skill_text
    assert "onboard_route_guide" in skill_text
    context_text = responses[3]["result"]["contents"][0]["text"]
    assert "project_id: `aming-claw`" in context_text
    assert "dashboard_url:" in context_text
    assert "health: `unavailable`" in context_text
    assert "backlog: `unavailable`" in context_text
    assert "## Primary Next Actions" in context_text
    assert "Start Services" in context_text
    assert "aming-claw start" in context_text
    assert "Call `graph_query` with `tool=query_schema`" in context_text
    seed = json.loads(responses[4]["result"]["contents"][0]["text"])
    assert seed["project_id"] == "aming-claw"
    assert "onboard_route_guide" in " ".join(seed["recommended_first_actions"])
    assert "graph-native" in " ".join(seed["recommended_first_actions"]).lower()
    mcp_surface = next(s for s in seed["core_surfaces"] if s["name"] == "mcp-plugin")
    assert ".codex-plugin/plugin.json" in mcp_surface["paths"]
    assert ".claude-plugin/plugin.json" in mcp_surface["paths"]
    assert "skills/aming-claw-onboard/SKILL.md" in mcp_surface["paths"]
    assert "skills/aming-claw/SKILL.md" not in mcp_surface["paths"]
    manifest = json.loads(responses[5]["result"]["contents"][0]["text"])
    assert manifest["bundle_major"] == 1
    assert manifest["consumer_contract"]["incompatible_major_action"] == "emit_plugin_update_reminder"
    assert manifest["resource_uris"]["graph_structure"] == "aming-claw://self-graph-bundle/graph-structure"
    graph_first_text = responses[6]["result"]["contents"][0]["text"]
    assert "Graph-First Playbook" in graph_first_text
    assert "graph_query" in graph_first_text
    assert manifest["resource_uris"]["semantic_projection"] == "aming-claw://self-graph-bundle/semantic-projection"


def test_mcp_current_context_prefers_workspace_project_config(tmp_path: Path):
    workspace = tmp_path / "external-project"
    workspace.mkdir()
    (workspace / ".aming-claw.yaml").write_text("project_id: instructor\n", encoding="utf-8")

    responses, stderr, returncode = _run_mcp_probe(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": "aming-claw://current-context"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "aming-claw://project/dashboard-e2e-demo/context"},
            },
        ],
        extra_args=["--workspace", str(workspace)],
    )

    assert returncode == 0
    assert stderr == ""
    current_text = responses[0]["result"]["contents"][0]["text"]
    assert "default_project_id: `aming-claw`" in current_text
    assert "workspace_project_id: `instructor`" in current_text
    assert "dashboard_project_id: `-`" in current_text
    assert "active_project_id: `instructor`" in current_text
    assert "context_source: `workspace_config`" in current_text
    assert "dashboard?project_id=instructor" in current_text

    project_text = responses[1]["result"]["contents"][0]["text"]
    assert "default_project_id: `aming-claw`" in project_text
    assert "workspace_project_id: `instructor`" in project_text
    assert "dashboard_project_id: `dashboard-e2e-demo`" in project_text
    assert "active_project_id: `dashboard-e2e-demo`" in project_text
    assert "context_source: `resource_uri`" in project_text


def _server_with_context_payloads(
    tmp_path: Path,
    *,
    graph: dict,
    project_id: str = "instructor",
    projects: list[dict] | None = None,
    backlog_count: int = 2,
    health: dict | None = None,
) -> AmingClawMCP:
    workspace = tmp_path / project_id
    workspace.mkdir()
    if projects is None:
        projects = [
            {
                "project_id": project_id,
                "workspace_path": str(workspace),
                "active_snapshot_id": graph.get("active_snapshot_id"),
            }
        ]
    server = AmingClawMCP(
        project_id="aming-claw",
        governance_url="http://governance.test",
        manager_url="http://manager.test",
        workspace=str(workspace),
        redis_url="redis://unused",
    )

    def fake_request(method: str, url: str, data: dict | None = None, timeout: int = 15) -> dict:
        if url.endswith("/api/projects"):
            return {"projects": projects}
        if url.endswith("/api/health"):
            return health or {"status": "ok", "version": "test-version"}
        if "/api/version-check/" in url:
            return {"head": "abcdef123456", "dirty": False, "runtime_match": True}
        if "/api/graph-governance/" in url and url.endswith("/status"):
            return graph
        if "/api/graph-governance/" in url and url.endswith("/operations/queue"):
            return {"count": 3}
        if "/api/backlog/" in url:
            return {"count": backlog_count, "bugs": [{} for _ in range(backlog_count)]}
        return {"error": f"unexpected url {url}"}

    server._request_json = fake_request  # type: ignore[method-assign]
    return server


def _primary_action_lines(context_text: str) -> list[str]:
    return [line for line in context_text.splitlines() if re.match(r"^\d+\. \*\*", line)]


def test_mcp_current_context_online_current_graph_shows_minimal_actions(tmp_path: Path):
    server = _server_with_context_payloads(
        tmp_path,
        graph={
            "active_snapshot_id": "full-abcdef-1234",
            "pending_scope_reconcile_count": 0,
            "current_state": {"graph_stale": {"is_stale": False}},
        },
        backlog_count=4,
    )

    text = server._current_context_text("instructor")

    assert "project_id: `instructor`" in text
    assert "dashboard?project_id=instructor&view=graph" in text
    assert "graph: snapshot `full-abcdef-1234` stale `False` pending_scope `0`" in text
    assert "operations_queue: count `3`" in text
    assert "backlog: open `4`" in text
    assert "selected_project_note: active project `instructor` differs from default `aming-claw`" in text
    actions = _primary_action_lines(text)
    assert len(actions) == 3
    assert "Check Current Project Status" in actions[0]
    assert "Find PR Opportunities" in actions[1]
    assert "Explain Graph Concepts" in actions[2]


def test_mcp_current_context_online_stale_graph_prioritizes_update(tmp_path: Path):
    server = _server_with_context_payloads(
        tmp_path,
        graph={
            "active_snapshot_id": "scope-abcdef-1234",
            "pending_scope_reconcile_count": 1,
            "current_state": {"graph_stale": {"is_stale": True}},
        },
    )

    text = server._current_context_text("instructor")

    actions = _primary_action_lines(text)
    assert len(actions) == 3
    assert "Update Graph" in actions[0]
    assert "Check Current Project Status" in actions[1]
    assert "Find PR Opportunities" in actions[2]


def test_mcp_current_context_online_missing_graph_opens_projects(tmp_path: Path):
    server = _server_with_context_payloads(
        tmp_path,
        graph={
            "active_snapshot_id": "",
            "pending_scope_reconcile_count": 0,
            "current_state": {},
        },
        projects=[{"project_id": "instructor", "workspace_path": str(tmp_path / "instructor"), "active_snapshot_id": ""}],
    )

    text = server._current_context_text("instructor")

    assert "dashboard?project_id=instructor&view=projects" in text
    actions = _primary_action_lines(text)
    assert len(actions) == 3
    assert "Initialize Project" in actions[0]
    assert "Check Current Project Status" in actions[1]
    assert "Explain Graph Concepts" in actions[2]
