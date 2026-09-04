from __future__ import annotations

import hashlib
import io
import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent.governance import mcp_server as governance_mcp_server
from agent.mcp import server as plugin_mcp_server
from agent.mcp import tools as mcp_tools
from agent.mcp.schema_contract import (
    MCP_TOOL_SCHEMA_VERSION,
    mcp_tool_schema_fingerprint,
    mcp_tool_schema_compatibility,
)
from agent.mcp.tools import TOOLS, ToolDispatcher


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


def _tool_properties(name: str) -> dict:
    tool = next(tool for tool in TOOLS if tool.get("name") == name)
    return tool["inputSchema"]["properties"]


def _nested_public_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *(_nested_public_keys(child) for child in value.values())
        )
    if isinstance(value, list):
        return set().union(*(_nested_public_keys(child) for child in value))
    return set()


def _managed_allocation_auth_fixture():
    session_sentinel = "SENTINEL_MANAGED_ALLOCATION_SESSION"
    fence_sentinel = "SENTINEL_MANAGED_ALLOCATION_FENCE"
    nested_sentinel = "SENTINEL_MANAGED_ALLOCATION_NESTED"
    route = {
        "route_id": "route-managed-allocation",
        "route_context_hash": "sha256:" + ("1" * 64),
        "prompt_contract_id": "rprompt-managed-allocation",
        "prompt_contract_hash": "sha256:" + ("2" * 64),
        "route_token_ref": "rtok-managed-allocation",
        "visible_injection_manifest_hash": "sha256:" + ("3" * 64),
    }
    identity = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-managed-allocation",
        "task_id": "worker-managed-allocation",
        "parent_task_id": "cex-managed-allocation",
        "contract_execution_id": "cex-managed-allocation",
        "target_project_root": "/tmp/managed-allocation",
        "worker_id": "worker-managed-allocation",
        "worker_slot_id": "worker-managed-allocation",
        "agent_id": "worker-managed-allocation",
        "allocation_owner": "worker-managed-allocation",
        "actual_host_worker_id": "worker-managed-allocation",
        "worker_session_id": "desktop-managed-allocation",
        "host_startup_id": "desktop-managed-allocation",
        "host_session_id": "desktop-managed-allocation",
        "session_token_ref": "wstok-managed-allocation",
        **route,
    }
    allocation_args = {
        key: value
        for key, value in identity.items()
        if key not in {"runtime_context_id", "session_token_ref"}
    }
    allocation_args.update(
        {
            "backlog_id": "AC-MANAGED-ALLOCATION-AUTH",
            "workspace_root": "/tmp",
            "worktree_path": identity["target_project_root"],
            "fence_token": fence_sentinel,
            "base_commit": "a" * 40,
            "target_head_commit": "a" * 40,
            "merge_queue_id": "mq-managed-allocation",
            "owned_files": ["agent/tests/test_mcp_tools.py"],
            "create_worktree": False,
        }
    )
    allocation_response = {
        "ok": True,
        "project_id": identity["project_id"],
        "context": {
            **identity,
            "fence_token_present": True,
            "fence_token_hash": "sha256:" + ("6" * 64),
            "fence_token_redacted": True,
            "worker_credentials": {
                "session_token": nested_sentinel,
                "fence_token": nested_sentinel,
                "host_envelope": {
                    "env": {
                        "AMING_WORKER_SESSION_TOKEN": nested_sentinel,
                        "AMING_WORKER_FENCE_TOKEN": nested_sentinel,
                    }
                },
            },
        },
        "branch_runtime_evidence": {
            **identity,
            "route_identity": route,
            "worker_credentials": {
                "session_token": nested_sentinel,
                "fence_token": nested_sentinel,
            },
        },
        "same_owner_worker_session": {
            "issued": True,
            "delivery": "worker_host_envelope",
            "session_token": session_sentinel,
            "session_token_ref": identity["session_token_ref"],
            "session_token_hash": "sha256:" + ("4" * 64),
            "scope": {
                "project_id": identity["project_id"],
                "runtime_context_id": identity["runtime_context_id"],
                "task_id": identity["task_id"],
                "worker_slot_id": identity["worker_slot_id"],
            },
        },
        "diagnostics": {
            "raw_session_token": nested_sentinel,
            "raw_fence_token": nested_sentinel,
            "token_hash": "sha256:" + ("5" * 64),
            "padding": "x" * 96_000,
        },
    }
    return {
        "session_sentinel": session_sentinel,
        "fence_sentinel": fence_sentinel,
        "nested_sentinel": nested_sentinel,
        "route": route,
        "identity": identity,
        "allocation_args": allocation_args,
        "allocation_response": allocation_response,
    }


def _assert_managed_allocation_auth_is_public_safe(result, fixture):
    serialized = json.dumps(result, sort_keys=True)
    for sentinel in (
        fixture["session_sentinel"],
        fixture["fence_sentinel"],
        fixture["nested_sentinel"],
    ):
        assert sentinel not in serialized
    assert result["auth_loaded"] is True
    assert result["session_token_ref"] == fixture["identity"][
        "session_token_ref"
    ]
    assert result["managed_host_envelope_ref"]
    assert result["managed_host_envelope"] == {
        **result["managed_host_envelope"],
        "status": "staged",
        "process_local": True,
        "managed_host_envelope_ref": result["managed_host_envelope_ref"],
        "session_token_ref": fixture["identity"]["session_token_ref"],
        "runtime_context_id": fixture["identity"]["runtime_context_id"],
        "task_id": fixture["identity"]["task_id"],
    }
    for flag in (
        "raw_worker_auth_exposed",
        "raw_session_token_exposed",
        "raw_fence_token_exposed",
    ):
        assert result[flag] is False
        assert result["managed_host_envelope"][flag] is False
    public_keys = _nested_public_keys(result)
    for forbidden in (
        "same_owner_worker_session",
        "host_envelope",
        "worker_credentials",
        "session_token_hash",
        "fence_token_hash",
        "token_hash",
    ):
        assert forbidden not in public_keys


def test_managed_parallel_allocate_recursively_scrubs_and_stages_opaque_auth():
    fixture = _managed_allocation_auth_fixture()
    calls = []

    def api(method, path, data=None):
        calls.append((method, path, data))
        return fixture["allocation_response"]

    dispatcher = ToolDispatcher(api, worker_pool=None)
    result = dispatcher.dispatch(
        "parallel_branch_allocate",
        fixture["allocation_args"],
    )

    _assert_managed_allocation_auth_is_public_safe(result, fixture)
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/parallel-branches/allocate")
    assert dispatcher._host_envelope_continuity.pending_count() == 1


def test_managed_parallel_allocate_missing_auth_reports_post_write_truth():
    fixture = _managed_allocation_auth_fixture()
    response = dict(fixture["allocation_response"])
    response["same_owner_worker_session"] = {
        key: value
        for key, value in response["same_owner_worker_session"].items()
        if key != "session_token"
    }
    dispatcher = ToolDispatcher(
        lambda *_args, **_kwargs: response,
        worker_pool=None,
    )

    result = dispatcher.dispatch(
        "parallel_branch_allocate",
        fixture["allocation_args"],
    )

    assert result["error"] == "managed_allocation_auth_missing"
    assert result["http_request_performed"] is True
    assert result["writes_performed"] is True
    assert result["zero_write_rejection"] is False
    serialized = json.dumps(result, sort_keys=True)
    for sentinel in (
        fixture["fence_sentinel"],
        fixture["nested_sentinel"],
    ):
        assert sentinel not in serialized


def test_managed_allocation_initial_join_rotation_and_continuation_fail_closed():
    fixture = _managed_allocation_auth_fixture()
    identity = fixture["identity"]
    route = fixture["route"]
    rotated_session = "SENTINEL_MANAGED_ROTATED_SESSION"
    rotated_fence = "SENTINEL_MANAGED_ROTATED_FENCE"
    rotated_ref = "wstok-managed-allocation-rotated"
    calls = []
    rotation_complete = False

    def api(method, path, data=None):
        nonlocal rotation_complete
        calls.append((method, path, dict(data or {})))
        if path.endswith("/parallel-branches/allocate"):
            return fixture["allocation_response"]
        if path.endswith("/session-token/initial-join"):
            assert data["session_token"] == fixture["session_sentinel"]
            assert data["fence_token"] == fixture["fence_sentinel"]
            assert "managed_host_envelope_ref" not in data
            rotated_identity = {**identity, "session_token_ref": rotated_ref}
            rotation_complete = True
            return {
                "ok": True,
                "status": "session_token_issued",
                "delivery": "worker_host_envelope",
                **rotated_identity,
                "route_identity": route,
                "session_token": rotated_session,
                "fence_token": rotated_fence,
                "host_envelope": {
                    **rotated_identity,
                    "route_identity": route,
                    "env": {
                        "AMING_WORKER_SESSION_TOKEN": rotated_session,
                        "AMING_WORKER_FENCE_TOKEN": rotated_fence,
                    },
                },
            }
        if method == "GET" and "/worker-guide" in path:
            query = __import__(
                "urllib.parse", fromlist=["parse_qs", "urlparse"]
            ).parse_qs(
                __import__(
                    "urllib.parse", fromlist=["urlparse"]
                ).urlparse(path).query
            )
            assert query["session_token"] == [
                rotated_session
                if rotation_complete
                else fixture["session_sentinel"]
            ]
            assert query["fence_token"] == [
                rotated_fence
                if rotation_complete
                else fixture["fence_sentinel"]
            ]
            return {
                "ok": True,
                "status": "worker_guide_ready",
                "schema_version": (
                    "runtime_context.worker_guide_compact_response.v1"
                ),
                "response_view": "compact",
                "project_id": identity["project_id"],
                "runtime_context_id": identity["runtime_context_id"],
                "task_id": identity["task_id"],
                "next_legal_action": (
                    "record_read_receipt"
                    if rotation_complete
                    else "request_runtime_context_initial_join_host_envelope"
                ),
                "canonical_executable_action": {
                    "mcp_tool": (
                        "runtime_context_read_receipt"
                        if rotation_complete
                        else "runtime_context_session_token_initial_join"
                    ),
                    "copy_safe_body": {
                        **identity,
                        "session_token_ref": (
                            rotated_ref
                            if rotation_complete
                            else identity["session_token_ref"]
                        ),
                    },
                },
                "recursive_diagnostics": {
                    "session_token": fixture["nested_sentinel"],
                    "fence_token": fixture["nested_sentinel"],
                    "padding": "x" * 96_000,
                },
            }
        assert data["session_token"] == rotated_session
        assert data["fence_token"] == rotated_fence
        if path.endswith("/parallel-branches/startup"):
            return {"ok": True, "status": "startup_recorded"}
        return {"ok": True, "status": "accepted"}

    dispatcher = ToolDispatcher(api, worker_pool=None)
    allocated = dispatcher.dispatch(
        "parallel_branch_allocate",
        fixture["allocation_args"],
    )
    allocation_envelope_ref = allocated["managed_host_envelope_ref"]
    pre_join_guide = dispatcher.dispatch(
        "runtime_context_worker_guide",
        {
            **identity,
            "managed_host_envelope_ref": allocation_envelope_ref,
        },
    )
    assert pre_join_guide["next_legal_action"] == (
        "request_runtime_context_initial_join_host_envelope"
    )
    assert fixture["nested_sentinel"] not in json.dumps(
        pre_join_guide,
        sort_keys=True,
    )
    assert len(json.dumps(pre_join_guide).encode()) < 64 * 1024
    join_args = {
        **identity,
        "managed_host_envelope_ref": allocation_envelope_ref,
        "reason": "rotate allocation auth before worker continuation",
    }
    joined = dispatcher.dispatch(
        "runtime_context_session_token_initial_join",
        join_args,
    )

    assert joined["auth_loaded"] is True
    assert joined["session_token_ref"] == rotated_ref
    assert joined["managed_host_envelope_ref"] != allocation_envelope_ref
    assert joined["allocation_auth_revoked"] is True
    assert joined["previous_managed_host_envelope_revoked"] is True
    joined_serialized = json.dumps(joined, sort_keys=True)
    assert rotated_session not in joined_serialized
    assert rotated_fence not in joined_serialized

    post_join_guide = dispatcher.dispatch(
        "runtime_context_worker_guide",
        {
            **identity,
            "session_token_ref": rotated_ref,
            "managed_host_envelope_ref": joined[
                "managed_host_envelope_ref"
            ],
        },
    )
    assert post_join_guide["next_legal_action"] == "record_read_receipt"
    assert fixture["nested_sentinel"] not in json.dumps(
        post_join_guide,
        sort_keys=True,
    )

    call_count = len(calls)
    for rejected_args in (
        {**identity, "managed_host_envelope_ref": allocation_envelope_ref},
        {
            **identity,
            "task_id": "worker-cross-allocation",
            "managed_host_envelope_ref": joined["managed_host_envelope_ref"],
        },
    ):
        rejected = dispatcher.dispatch(
            "runtime_context_read_receipt",
            rejected_args,
        )
        assert rejected["error"] == (
            "managed_host_envelope_ref_stale_or_scope_mismatch"
        )
        assert rejected["http_request_performed"] is False
        assert rejected["writes_performed"] is False
        assert rejected["zero_write_rejection"] is True
        assert len(calls) == call_count

    current = {
        **identity,
        "session_token_ref": rotated_ref,
        "managed_host_envelope_ref": joined["managed_host_envelope_ref"],
    }
    receipt = dispatcher.dispatch("runtime_context_read_receipt", current)
    assert receipt == {"ok": True, "status": "accepted"}
    startup = dispatcher.dispatch(
        "parallel_branch_startup",
        {**current, "worker_role": "mf_sub"},
    )
    assert startup["managed_host_envelope_consumed"] is True

    call_count = len(calls)
    replay = dispatcher.dispatch("runtime_context_read_receipt", current)
    assert replay["error"] == (
        "managed_host_envelope_ref_stale_or_scope_mismatch"
    )
    assert replay["http_request_performed"] is False
    assert replay["writes_performed"] is False
    assert replay["zero_write_rejection"] is True
    assert len(calls) == call_count


def test_managed_allocation_ref_is_required_and_process_local():
    fixture = _managed_allocation_auth_fixture()
    calls = []

    def api(method, path, data=None):
        calls.append((method, path, data))
        return fixture["allocation_response"]

    dispatcher = ToolDispatcher(api, worker_pool=None)
    allocated = dispatcher.dispatch(
        "parallel_branch_allocate",
        fixture["allocation_args"],
    )
    identity = fixture["identity"]
    call_count = len(calls)
    missing = dispatcher.dispatch(
        "runtime_context_session_token_initial_join",
        identity,
    )
    assert missing["error"] == "managed_host_envelope_ref_required"
    assert missing["http_request_performed"] is False
    assert missing["writes_performed"] is False
    assert missing["zero_write_rejection"] is True
    assert len(calls) == call_count

    fresh_process_calls = []
    fresh_process = ToolDispatcher(
        lambda *args, **kwargs: fresh_process_calls.append((args, kwargs)),
        worker_pool=None,
    )
    lost = fresh_process.dispatch(
        "runtime_context_session_token_initial_join",
        {
            **identity,
            "managed_host_envelope_ref": allocated[
                "managed_host_envelope_ref"
            ],
        },
    )
    assert lost["error"] == "managed_host_envelope_not_loaded"
    assert lost["http_request_performed"] is False
    assert lost["writes_performed"] is False
    assert lost["zero_write_rejection"] is True
    assert fresh_process_calls == []


def test_managed_mcp_host_envelope_stages_injects_and_acks_startup():
    raw_session = "managed-session-secret"
    raw_fence = "managed-fence-secret"
    route = {
        "route_id": "route-managed-continuity",
        "route_context_hash": "sha256:" + ("a" * 64),
        "prompt_contract_id": "rprompt-managed-continuity",
        "prompt_contract_hash": "sha256:" + ("b" * 64),
        "route_token_ref": "rtok-managed-continuity",
        "visible_injection_manifest_hash": "sha256:" + ("c" * 64),
    }
    identity = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-managed-continuity",
        "task_id": "worker-managed-continuity",
        "parent_task_id": "cex-managed-continuity",
        "contract_execution_id": "cex-managed-continuity",
        "target_project_root": "/tmp/managed-continuity",
        "worker_id": "worker-managed-continuity",
        "worker_slot_id": "worker-managed-continuity",
        "agent_id": "worker-managed-continuity",
        "allocation_owner": "worker-managed-continuity",
        "actual_host_worker_id": "worker-managed-continuity",
        "worker_session_id": "desktop-managed-continuity",
        "host_startup_id": "desktop-managed-continuity",
        "host_session_id": "desktop-managed-continuity",
        "session_token_ref": "wstok-managed-continuity",
        **route,
    }
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        if path.endswith("/session-token/reissue"):
            envelope_identity = {
                key: value
                for key, value in identity.items()
                if key not in route
            }
            return {
                "ok": True,
                "status": "session_token_reissued",
                "delivery": "worker_host_envelope",
                **envelope_identity,
                "route_identity": dict(route),
                "session_token": raw_session,
                "fence_token": raw_fence,
                "session_token_hash": "sha256:"
                + hashlib.sha256(raw_session.encode()).hexdigest(),
                "fence_token_hash": "sha256:"
                + hashlib.sha256(raw_fence.encode()).hexdigest(),
                "host_envelope": {
                    **envelope_identity,
                    "route_identity": dict(route),
                    "env": {
                        "AMING_WORKER_SESSION_TOKEN": raw_session,
                        "AMING_WORKER_FENCE_TOKEN": raw_fence,
                    },
                },
            }
        if "/worker-guide" in path:
            parsed = __import__("urllib.parse", fromlist=["parse_qs", "urlparse"])
            query = parsed.parse_qs(parsed.urlparse(path).query)
            assert query["session_token"] == [raw_session]
            assert query["fence_token"] == [raw_fence]
            return {
                "ok": True,
                "status": "worker_guide_ready",
                "canonical_executable_action": {
                    "mcp_tool": "parallel_branch_startup",
                    "copy_safe_body": {**identity, "worker_role": "mf_sub"},
                },
            }
        assert data is not None
        assert data["session_token"] == raw_session
        assert data["fence_token"] == raw_fence
        return {
            "ok": True,
            "status": (
                "startup_recorded"
                if path.endswith("/parallel-branches/startup")
                else "accepted"
            ),
        }

    dispatcher = ToolDispatcher(fake_api, worker_pool=None)
    issued = dispatcher.dispatch(
        "runtime_context_session_token_reissue",
        {**identity, "reason": "load managed MCP host auth"},
    )
    serialized = json.dumps(issued, sort_keys=True)
    assert issued["auth_loaded"] is True
    assert issued["managed_host_envelope"]["process_local"] is True
    assert "host_envelope" not in issued
    assert raw_session not in serialized
    assert raw_fence not in serialized
    assert dispatcher._host_envelope_continuity.pending_count() == 1

    guide = dispatcher.dispatch(
        "runtime_context_worker_guide",
        {
            "project_id": identity["project_id"],
            "runtime_context_id": identity["runtime_context_id"],
            **{
                key: identity[key]
                for key in mcp_tools._RUNTIME_CONTEXT_QUERY_FIELDS
                if key in identity
            },
        },
    )
    assert guide["ok"] is True
    guide_startup_body = dict(
        guide["canonical_executable_action"]["copy_safe_body"]
    )
    assert guide_startup_body["contract_execution_id"] == identity[
        "contract_execution_id"
    ]
    assert raw_session not in json.dumps(guide_startup_body, sort_keys=True)
    assert raw_fence not in json.dumps(guide_startup_body, sort_keys=True)
    call_count = len(calls)
    wrong_env_receipt = dispatcher.dispatch(
        "runtime_context_read_receipt",
        {**identity, "fence_token": "env:OTHER_WORKER_FENCE_TOKEN"},
    )
    assert wrong_env_receipt["error"] == "managed_host_envelope_scope_mismatch"
    assert wrong_env_receipt["mismatched_fields"] == ["fence_token"]
    assert wrong_env_receipt["http_request_performed"] is False
    assert len(calls) == call_count

    receipt = dispatcher.dispatch(
        "runtime_context_read_receipt",
        {**identity, "fence_token": "env:AMING_WORKER_FENCE_TOKEN"},
    )
    assert receipt["ok"] is True
    assert dispatcher._host_envelope_continuity.pending_count() == 1

    call_count = len(calls)
    missing_execution = dict(guide_startup_body)
    missing_execution.pop("contract_execution_id")
    missing = dispatcher.dispatch(
        "parallel_branch_startup",
        {**missing_execution, "worker_role": "mf_sub"},
    )
    assert missing["error"] == "managed_host_envelope_scope_mismatch"
    assert missing["missing_fields"] == ["contract_execution_id"]
    assert missing["mismatched_fields"] == []
    assert missing["http_request_performed"] is False
    assert len(calls) == call_count
    assert dispatcher._host_envelope_continuity.pending_count() == 1

    wrong = dispatcher.dispatch(
        "parallel_branch_startup",
        {
            **guide_startup_body,
            "contract_execution_id": "cex-cross-managed-continuity",
            "worker_role": "mf_sub",
        },
    )
    assert wrong["error"] == "managed_host_envelope_scope_mismatch"
    assert wrong["missing_fields"] == []
    assert wrong["mismatched_fields"] == ["contract_execution_id"]
    assert wrong["http_request_performed"] is False
    assert len(calls) == call_count
    assert dispatcher._host_envelope_continuity.pending_count() == 1

    startup_properties = _tool_properties("parallel_branch_startup")
    schema_startup_body = {
        key: value
        for key, value in guide_startup_body.items()
        if key in startup_properties
    }
    assert schema_startup_body["target_project_root"] == identity[
        "target_project_root"
    ]
    startup = dispatcher.dispatch(
        "parallel_branch_startup",
        schema_startup_body,
    )
    assert startup["managed_host_envelope_consumed"] is True
    assert dispatcher._host_envelope_continuity.pending_count() == 0
    call_count = len(calls)
    rejected = dispatcher.dispatch("runtime_context_read_receipt", dict(identity))
    assert rejected["error"] == "managed_host_envelope_not_loaded"
    assert len(calls) == call_count


def test_managed_mcp_post_startup_envelope_submits_worker_line_then_acks_finish():
    continuity = mcp_tools.ManagedHostEnvelopeContinuity()
    raw_session = "managed-post-startup-session"
    raw_fence = "managed-post-startup-fence"
    route = {
        "route_id": "route-managed-post-startup",
        "route_context_hash": "sha256:" + ("1" * 64),
        "prompt_contract_id": "rprompt-managed-post-startup",
        "prompt_contract_hash": "sha256:" + ("2" * 64),
        "route_token_ref": "rtok-managed-post-startup",
        "visible_injection_manifest_hash": "sha256:" + ("3" * 64),
    }
    identity = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-managed-post-startup",
        "task_id": "worker-managed-post-startup",
        "parent_task_id": "cex-managed-post-startup",
        "contract_execution_id": "cex-managed-post-startup",
        "target_project_root": "/tmp/managed-post-startup",
        "worker_id": "worker-managed-post-startup",
        "worker_slot_id": "worker-managed-post-startup",
        "worker_session_id": "desktop-managed-post-startup",
        "session_token_ref": "wstok-managed-post-startup",
        **route,
    }
    issued = continuity.dispatch(
        "runtime_context_session_token_rejoin",
        identity,
        lambda _args: {
            "ok": True,
            "status": "session_token_rejoined",
            "delivery": "worker_host_envelope",
            **identity,
            "session_token": raw_session,
            "fence_token": raw_fence,
            "host_envelope": {
                **identity,
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": raw_session,
                    "AMING_WORKER_FENCE_TOKEN": raw_fence,
                },
            },
        },
    )
    assert issued["auth_loaded"] is True
    assert continuity.pending_count() == 1
    calls = []

    malformed = continuity.dispatch(
        "runtime_context_implementation_evidence",
        {
            **identity,
            "session_token": "<host-realized wrong_field>",
            "fence_token": "<host-realized fence_token>",
        },
        lambda args: calls.append(args),
    )
    assert malformed["error"] == (
        "managed_host_envelope_auth_placeholder_invalid"
    )
    assert malformed["http_request_performed"] is False
    assert calls == []

    missing_route_identity = continuity.dispatch(
        "runtime_context_implementation_evidence",
        {
            **{
                field: value
                for field, value in identity.items()
                if field not in route
            },
            "session_token": "<host-realized session_token>",
            "fence_token": "<host-realized fence_token>",
        },
        lambda args: calls.append(args),
    )
    assert missing_route_identity["error"] == (
        "managed_host_envelope_scope_mismatch"
    )
    assert missing_route_identity["missing_fields"] == sorted(route)
    assert missing_route_identity["http_request_performed"] is False
    assert calls == []
    assert continuity.pending_count() == 1

    current = continuity.dispatch(
        "runtime_context_current",
        {**identity, "view": "all"},
        lambda args: calls.append(dict(args))
        or {"ok": True, "status": "current"},
    )
    assert current == {"ok": True, "status": "current"}
    assert calls[-1]["session_token"] == raw_session
    assert calls[-1]["fence_token"] == raw_fence
    assert continuity.pending_count() == 1

    implementation = continuity.dispatch(
        "runtime_context_implementation_evidence",
        {
            **identity,
            "session_token": "<host-realized session_token>",
            "fence_token": "<host-realized fence_token>",
            "changed_files": ["src/app.py"],
            "tests": [{"command": "pytest -q", "status": "passed"}],
            "test_results": {"status": "passed", "passed": True},
            "graph_trace_ids": ["gqt-managed-post-startup"],
        },
        lambda args: calls.append(dict(args))
        or {"ok": True, "status": "accepted"},
    )
    assert implementation == {"ok": True, "status": "accepted"}
    assert calls[-1]["session_token"] == raw_session
    assert calls[-1]["fence_token"] == raw_fence
    assert continuity.pending_count() == 1

    call_count = len(calls)
    wrong_contract_scope = continuity.dispatch(
        "contract_runtime_submit_line",
        {
            **identity,
            "route_id": "route-other-managed-post-startup",
            "actor_role": "mf_sub",
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "line_instance_id": (
                f"runtime_context:{identity['runtime_context_id']}"
            ),
            "evidence_kind": "implementation",
        },
        lambda args: calls.append(dict(args)),
    )
    assert wrong_contract_scope["error"] == (
        "managed_host_envelope_scope_mismatch"
    )
    assert wrong_contract_scope["mismatched_fields"] == ["route_id"]
    assert wrong_contract_scope["http_request_performed"] is False
    assert len(calls) == call_count

    contract_line = continuity.dispatch(
        "contract_runtime_submit_line",
        {
            **identity,
            "actor_role": "mf_sub",
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "line_instance_id": (
                f"runtime_context:{identity['runtime_context_id']}"
            ),
            "evidence_kind": "implementation",
        },
        lambda args: calls.append(dict(args))
        or {
            "ok": True,
            "status": "accepted",
            "session_token": raw_session,
            "fence_token": raw_fence,
        },
    )
    assert contract_line == {"ok": True, "status": "accepted"}
    assert calls[-1]["session_token"] == raw_session
    assert calls[-1]["fence_token"] == raw_fence
    assert continuity.pending_count() == 1

    finished = continuity.dispatch(
        "runtime_context_finish_gate",
        {
            **identity,
            "session_token": "<host-realized session_token>",
            "fence_token": "<host-realized fence_token>",
        },
        lambda args: calls.append(dict(args))
        or {"ok": True, "status": "passed"},
    )
    assert finished["managed_host_envelope_consumed"] is True
    assert finished["managed_host_envelope_consumed_at"] == (
        "runtime_context_finish_gate"
    )
    assert continuity.pending_count() == 0
    serialized = json.dumps(
        [issued, implementation, contract_line, finished], sort_keys=True
    )
    assert raw_session not in serialized
    assert raw_fence not in serialized


def test_managed_mcp_host_envelope_accepts_authoritative_safe_ref_rotation():
    continuity = mcp_tools.ManagedHostEnvelopeContinuity()
    old_ref = "wstok-before-managed-capture"
    new_ref = "wstok-after-managed-capture"
    raw_session = "rotated-managed-session"
    raw_fence = "rotated-managed-fence"
    route = {
        "route_id": "route-managed-rotation",
        "route_context_hash": "sha256:" + ("1" * 64),
        "prompt_contract_id": "rprompt-managed-rotation",
        "prompt_contract_hash": "sha256:" + ("2" * 64),
        "route_token_ref": "rtok-managed-rotation",
        "visible_injection_manifest_hash": "sha256:" + ("3" * 64),
    }
    request = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-managed-rotation",
        "task_id": "worker-managed-rotation",
        "parent_task_id": "cex-managed-rotation",
        "contract_execution_id": "cex-managed-rotation",
        "target_project_root": "/tmp/managed-rotation",
        "worker_id": "worker-managed-rotation",
        "worker_slot_id": "worker-managed-rotation",
        "agent_id": "worker-managed-rotation",
        "allocation_owner": "worker-managed-rotation",
        "actual_host_worker_id": "worker-managed-rotation",
        "worker_session_id": "desktop-managed-rotation",
        "host_startup_id": "desktop-managed-rotation",
        "host_session_id": "desktop-managed-rotation",
        "session_token_ref": old_ref,
        **route,
    }
    response_identity = {**request, "session_token_ref": new_ref}
    issued = continuity.dispatch(
        "runtime_context_session_token_reissue",
        request,
        lambda _args: {
            "ok": True,
            "request_id": "req-managed-rotation",
            "audit_event_ref": "timeline:49",
            "status": "session_token_reissued",
            "delivery": "worker_host_envelope",
            **response_identity,
            "session_token": raw_session,
            "fence_token": raw_fence,
            "session_token_hash": "sha256:"
            + hashlib.sha256(raw_session.encode()).hexdigest(),
            "fence_token_hash": "sha256:"
            + hashlib.sha256(raw_fence.encode()).hexdigest(),
            "host_envelope": {
                **response_identity,
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": raw_session,
                    "AMING_WORKER_FENCE_TOKEN": raw_fence,
                },
            },
        },
    )

    assert issued["auth_loaded"] is True
    assert issued["session_token_ref"] == new_ref
    assert issued["managed_host_envelope"]["session_token_ref"] == new_ref
    assert old_ref not in json.dumps(issued, sort_keys=True)
    calls = []
    continued = continuity.dispatch(
        "runtime_context_worker_guide",
        {**request, "session_token_ref": new_ref},
        lambda args: calls.append(dict(args)) or {"ok": True},
    )
    assert continued == {"ok": True}
    assert calls[0]["session_token"] == raw_session
    assert calls[0]["fence_token"] == raw_fence


def test_managed_mcp_host_envelope_post_response_failure_preserves_write_truth():
    continuity = mcp_tools.ManagedHostEnvelopeContinuity()
    raw_session = "mutation-aware-session"
    raw_fence = "mutation-aware-fence"
    request = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-mutation-aware",
        "task_id": "worker-mutation-aware",
    }
    result = continuity.dispatch(
        "runtime_context_session_token_reissue",
        request,
        lambda _args: {
            "ok": True,
            "request_id": "req-mutation-aware",
            "audit_event_ref": "timeline:49",
            "status": "session_token_reissued",
            "delivery": "worker_host_envelope",
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-mutation-aware",
            "task_id": "different-worker",
            "session_token_ref": "wstok-current-after-write",
            "session_token": raw_session,
            "fence_token": raw_fence,
            "host_envelope": {
                "project_id": "aming-claw",
                "runtime_context_id": "mfrctx-mutation-aware",
                "task_id": "different-worker",
                "session_token_ref": "wstok-current-after-write",
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": raw_session,
                    "AMING_WORKER_FENCE_TOKEN": raw_fence,
                },
            },
        },
    )

    assert result["error"] == "managed_host_envelope_identity_mismatch"
    assert result["field"] == "task_id"
    assert result["http_request_performed"] is True
    assert result["writes_performed"] is True
    assert result["server_mutation_accepted"] is True
    assert result["zero_write_rejection"] is False
    assert result["request_id"] == "req-mutation-aware"
    assert result["audit_event_ref"] == "timeline:49"
    assert result["session_token_ref"] == "wstok-current-after-write"
    assert raw_session not in json.dumps(result, sort_keys=True)
    assert raw_fence not in json.dumps(result, sort_keys=True)
    assert continuity.pending_count() == 0


def test_managed_mcp_host_envelope_preflight_conflict_is_zero_http():
    continuity = mcp_tools.ManagedHostEnvelopeContinuity()
    calls = []
    result = continuity.dispatch(
        "runtime_context_session_token_reissue",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-preflight-conflict",
            "task_id": "worker-preflight-conflict",
            "session_token_ref": "wstok-preflight-conflict",
            "route_id": "route-current",
            "route_identity": {"route_id": "route-stale"},
        },
        lambda args: calls.append(args),
    )
    assert result["error"] == "managed_host_envelope_identity_ambiguous"
    assert result["mismatched_fields"] == ["route_id"]
    assert result["http_request_performed"] is False
    assert result["writes_performed"] is False
    assert calls == []


def test_managed_mcp_host_envelope_cross_scope_is_zero_http():
    continuity = mcp_tools.ManagedHostEnvelopeContinuity()
    calls = []
    request = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-cross-scope",
        "task_id": "worker-cross-scope",
        "parent_task_id": "cex-cross-scope",
        "target_project_root": "/tmp/cross-scope",
        "session_token_ref": "wstok-cross-scope",
        "route_id": "route-cross-scope",
        "route_context_hash": "sha256:" + ("d" * 64),
        "prompt_contract_id": "rprompt-cross-scope",
        "prompt_contract_hash": "sha256:" + ("e" * 64),
        "route_token_ref": "rtok-cross-scope",
        "visible_injection_manifest_hash": "sha256:" + ("f" * 64),
    }
    raw_session = "cross-session-secret"
    raw_fence = "cross-fence-secret"
    response = {
        "ok": True,
        "status": "session_token_reissued",
        "delivery": "worker_host_envelope",
        **request,
        "session_token": raw_session,
        "fence_token": raw_fence,
        "host_envelope": {
            **request,
            "env": {
                "AMING_WORKER_SESSION_TOKEN": raw_session,
                "AMING_WORKER_FENCE_TOKEN": raw_fence,
            },
        },
    }
    continuity.dispatch("runtime_context_session_token_reissue", request, lambda _: response)
    rejected = continuity.dispatch(
        "runtime_context_worker_guide",
        {**request, "route_id": "route-foreign"},
        lambda args: calls.append(args),
    )
    assert rejected["error"] == "managed_host_envelope_scope_mismatch"
    assert rejected["http_request_performed"] is False
    assert calls == []


def test_managed_mcp_host_envelope_expiry_sync_allows_one_no_ref_recovery():
    from agent.cli_agent_service.launchers import HostEnvelopeStore

    clock = [300.0]
    store = HostEnvelopeStore(monotonic_clock=lambda: clock[0])
    continuity = mcp_tools.ManagedHostEnvelopeContinuity(store=store)
    route = {
        "route_id": "route-expiry-sync",
        "route_context_hash": "sha256:" + ("a" * 64),
        "prompt_contract_id": "rprompt-expiry-sync",
        "prompt_contract_hash": "sha256:" + ("b" * 64),
        "route_token_ref": "rtok-expiry-sync",
        "visible_injection_manifest_hash": "sha256:" + ("c" * 64),
    }
    identity = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-expiry-sync",
        "task_id": "worker-expiry-sync",
        "parent_task_id": "cex-expiry-sync",
        "contract_execution_id": "cex-expiry-sync",
        "target_project_root": "/tmp/expiry-sync",
        "worker_id": "worker-expiry-sync",
        "worker_slot_id": "worker-expiry-sync",
        "actual_host_worker_id": "worker-expiry-sync",
        "worker_session_id": "desktop-expiry-sync",
        "session_token_ref": "wstok-expiry-sync",
        **route,
    }
    raw_session = "expiry-sync-session-secret"
    raw_fence = "expiry-sync-fence-secret"
    issued = continuity.dispatch(
        "runtime_context_session_token_reissue",
        identity,
        lambda _args: {
            "ok": True,
            "status": "session_token_reissued",
            "delivery": "worker_host_envelope",
            "ttl_seconds": 2,
            **identity,
            "session_token": raw_session,
            "fence_token": raw_fence,
            "host_envelope": {
                **identity,
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": raw_session,
                    "AMING_WORKER_FENCE_TOKEN": raw_fence,
                },
            },
        },
    )
    assert issued["auth_loaded"] is True
    run_id = next(iter(store._entries))
    buffers = tuple(store._entries[run_id].environment.values())
    no_ref = {key: value for key, value in identity.items() if key != "session_token_ref"}
    calls = []

    active = continuity.dispatch(
        "runtime_context_worker_guide",
        no_ref,
        lambda args: calls.append(dict(args)),
    )
    assert active["error"] == "managed_host_envelope_scope_mismatch"
    assert active["missing_fields"] == ["session_token_ref"]
    assert active["http_request_performed"] is False
    assert calls == []

    clock[0] += 3
    wrong_scope = continuity.dispatch(
        "runtime_context_worker_guide",
        {**no_ref, "route_id": "route-expiry-sync-foreign"},
        lambda args: calls.append(dict(args)),
    )
    assert wrong_scope["error"] == "managed_host_envelope_scope_mismatch"
    assert wrong_scope["mismatched_fields"] == ["route_id"]
    assert wrong_scope["http_request_performed"] is False
    assert all(not value for value in buffers)
    assert calls == []

    recovered = continuity.dispatch(
        "runtime_context_worker_guide",
        no_ref,
        lambda args: calls.append(dict(args))
        or {"ok": True, "status": "recovery_guide_ready"},
    )
    assert recovered == {"ok": True, "status": "recovery_guide_ready"}
    assert calls == [no_ref]
    assert continuity.pending_count() == 0
    serialized = json.dumps([issued, recovered, calls], sort_keys=True)
    assert raw_session not in serialized
    assert raw_fence not in serialized


def test_managed_mcp_host_envelope_revoked_store_stays_zero_http():
    from agent.cli_agent_service.launchers import HostEnvelopeStore

    store = HostEnvelopeStore()
    continuity = mcp_tools.ManagedHostEnvelopeContinuity(store=store)
    identity = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-revoked-sync",
        "task_id": "worker-revoked-sync",
        "parent_task_id": "cex-revoked-sync",
        "target_project_root": "/tmp/revoked-sync",
        "session_token_ref": "wstok-revoked-sync",
        "route_id": "route-revoked-sync",
        "route_context_hash": "sha256:" + ("d" * 64),
        "prompt_contract_id": "rprompt-revoked-sync",
        "prompt_contract_hash": "sha256:" + ("e" * 64),
        "route_token_ref": "rtok-revoked-sync",
        "visible_injection_manifest_hash": "sha256:" + ("f" * 64),
    }
    continuity.dispatch(
        "runtime_context_session_token_reissue",
        identity,
        lambda _args: {
            "ok": True,
            "status": "session_token_reissued",
            "delivery": "worker_host_envelope",
            **identity,
            "session_token": "revoked-sync-session",
            "fence_token": "revoked-sync-fence",
            "host_envelope": {
                **identity,
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": "revoked-sync-session",
                    "AMING_WORKER_FENCE_TOKEN": "revoked-sync-fence",
                },
            },
        },
    )
    entry = continuity._entry_for(identity)
    assert entry is not None
    store.revoke(
        entry.run_id,
        envelope_ref=entry.envelope_ref,
        lease_owner_id="mcp-host-envelope-continuity",
    )
    calls = []
    no_ref = {key: value for key, value in identity.items() if key != "session_token_ref"}
    rejected = continuity.dispatch(
        "runtime_context_worker_guide",
        no_ref,
        lambda args: calls.append(args),
    )
    assert rejected["error"] == "managed_host_envelope_unavailable"
    assert rejected["http_request_performed"] is False
    assert calls == []


def test_managed_mcp_host_envelope_concurrent_continuation_fails_closed():
    continuity = mcp_tools.ManagedHostEnvelopeContinuity()
    route = {
        "route_id": "route-concurrent",
        "route_context_hash": "sha256:" + ("7" * 64),
        "prompt_contract_id": "rprompt-concurrent",
        "prompt_contract_hash": "sha256:" + ("8" * 64),
        "route_token_ref": "rtok-concurrent",
        "visible_injection_manifest_hash": "sha256:" + ("9" * 64),
    }
    request = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-concurrent",
        "task_id": "worker-concurrent",
        "parent_task_id": "cex-concurrent",
        "target_project_root": "/tmp/concurrent",
        "session_token_ref": "wstok-concurrent",
        **route,
    }
    continuity.dispatch(
        "runtime_context_session_token_reissue",
        request,
        lambda _: {
            "ok": True,
            "status": "session_token_reissued",
            "delivery": "worker_host_envelope",
            **request,
            "session_token": "concurrent-session-secret",
            "fence_token": "concurrent-fence-secret",
            "host_envelope": {
                **request,
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": "concurrent-session-secret",
                    "AMING_WORKER_FENCE_TOKEN": "concurrent-fence-secret",
                },
            },
        },
    )
    entered = Event()
    release = Event()
    first_result = []

    def slow_send(_args):
        entered.set()
        assert release.wait(2)
        return {"ok": True, "status": "worker_guide_ready"}

    thread = Thread(
        target=lambda: first_result.append(
            continuity.dispatch("runtime_context_worker_guide", request, slow_send)
        )
    )
    thread.start()
    assert entered.wait(2)
    second = continuity.dispatch(
        "runtime_context_worker_guide",
        request,
        lambda _args: pytest.fail("concurrent request reached HTTP"),
    )
    release.set()
    thread.join(timeout=2)
    assert second["error"] == "managed_host_envelope_concurrent_use"
    assert first_result == [{"ok": True, "status": "worker_guide_ready"}]


def test_guide_facade_schemas_require_project_adapter_and_top_level_revise_count():
    guide_facades = {
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_rejoin",
        "runtime_context_read_receipt",
        "runtime_context_implementation_evidence",
        "runtime_context_worker_commit",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "contract_runtime_submit_line",
        "mf_parallel_revise",
        "parallel_branch_allocate",
    }
    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        by_name = {item["name"]: item for item in registry}
        for facade in guide_facades:
            schema = by_name[facade]["inputSchema"]
            assert "project_id" in schema["properties"]
            assert "project_id" in schema["required"]

        revise = by_name["mf_parallel_revise"]["inputSchema"]
        assert "required_worker_count" in revise["required"]
        assert revise["properties"]["required_worker_count"] == {
            "type": "integer",
            "enum": [1, 2],
        }
        assert "metadata" not in revise["required"]

        hotfix = by_name["observer_hotfix_enter"]["inputSchema"]
        attempt_fields = [
            "project_id",
            "backlog_id",
            "task_id",
            "parent_contract_execution_id",
            "predecessor_contract_execution_id",
            "predecessor_execution_state_revision",
            "predecessor_execution_state_hash",
            "successor_attempt_id",
            "actor",
            "reason",
            "route_token_ref",
        ]
        assert set(attempt_fields).issubset(hotfix["properties"])
        attempt_schema = hotfix["allOf"][0]
        assert attempt_schema["if"] == {
            "required": ["successor_attempt_id"]
        }
        assert attempt_schema["then"]["required"] == attempt_fields
        assert attempt_schema["then"]["propertyNames"]["enum"] == (
            attempt_fields
        )

    graph = next(item for item in TOOLS if item["name"] == "graph_query")
    graph_properties = graph["inputSchema"]["properties"]
    assert graph_properties["args"] == {"type": "object"}
    assert graph_properties["route_identity"]["type"] == "object"


def test_runtime_context_worker_commit_tool_routes_to_canonical_facade():
    assert "runtime_context_worker_commit" in _tool_names()
    tool = next(
        tool for tool in TOOLS if tool.get("name") == "runtime_context_worker_commit"
    )
    schema = tool["inputSchema"]
    properties = schema["properties"]
    assert {
        "contract_execution_id",
        "implementation_event_ref",
        "implementation_lineage_ref",
        "worker_implementation_lineage",
        "worker_commit_sha",
        "owned_files",
        "changed_files",
        "graph_trace_ids",
    }.issubset(properties)
    assert properties["implementation_event_ref"] == {"type": "string"}
    assert properties["implementation_lineage_ref"] == {"type": "string"}
    assert properties["worker_implementation_lineage"] == {"type": "object"}
    assert "implementation_event_ref" not in schema["required"]

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "runtime_context_worker_commit",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-worker-commit",
            "contract_execution_id": "cex-worker-commit",
            "worker_commit_sha": "a" * 40,
            "implementation_lineage_ref": "implementation-lineage:worker-commit",
            "worker_implementation_lineage": {"source": "contract_runtime"},
        },
    )
    assert "implementation_event_ref" not in recorder.calls[-1][2]
    assert recorder.calls[-1] == (
        "POST",
        (
            "/api/graph-governance/aming-claw/runtime-contexts/"
            "mfrctx-worker-commit/worker-commit"
        ),
        {
            "runtime_context_id": "mfrctx-worker-commit",
            "contract_execution_id": "cex-worker-commit",
            "worker_commit_sha": "a" * 40,
            "implementation_lineage_ref": "implementation-lineage:worker-commit",
            "worker_implementation_lineage": {"source": "contract_runtime"},
        },
    )


def test_governance_mcp_exposes_scope_insufficiency_request_schema():
    required = {
        "project_id",
        "runtime_context_id",
        "backlog_id",
        "task_id",
        "parent_task_id",
        "target_project_root",
        "missing_files",
        "requested_files",
        "blocked_acceptance_ids",
        "reason",
        "graph_refs",
    }
    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        tool = next(
            item
            for item in registry
            if item["name"] == "runtime_context_scope_insufficiency_request"
        )
        schema = tool["inputSchema"]
        assert required.issubset(schema["required"])
        assert schema["properties"]["requested_files"] == {
            "type": "array",
            "items": {"type": "string"},
        }

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    body = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-scope-request",
        "backlog_id": "AC-SCOPE-REQUEST",
        "task_id": "scope-request-worker",
        "parent_task_id": "cex-scope-request",
        "target_project_root": "/repo",
        "missing_files": ["missing.py"],
        "requested_files": ["owned.py", "missing.py"],
        "blocked_acceptance_ids": ["AC-1"],
        "reason": "missing.py is required",
        "graph_refs": ["graph-query:gqt-scope-request"],
    }
    dispatcher.dispatch("runtime_context_scope_insufficiency_request", body)
    assert recorder.calls[-1] == (
        "POST",
        (
            "/api/graph-governance/aming-claw/runtime-contexts/"
            "mfrctx-scope-request/scope-insufficiency-requests"
        ),
        {key: value for key, value in body.items() if key != "project_id"},
    )


def test_mf_timeline_precheck_schema_exposes_repair_view():
    view = _tool_properties("mf_timeline_precheck")["view"]
    assert "repair" in view["enum"]


def test_onboard_mcp_defaults_compact_and_routes_bounded_capsule_sections():
    onboard_properties = _tool_properties("onboard_route_guide")
    assert onboard_properties["response_view"]["enum"] == ["compact", "full"]
    assert onboard_properties["response_view"]["default"] == "compact"
    assert "onboard_route_guide_section_fetch" in _tool_names()
    section_properties = _tool_properties(
        "onboard_route_guide_section_fetch"
    )
    assert section_properties["sections"]["maxItems"] == 3

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "onboard_route_guide",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-CAPSULE-MCP",
            "role": "worker",
            "work_type": "parallel_worker",
        },
    )
    assert recorder.calls[-1] == (
        "POST",
        "/api/projects/aming-claw/onboard-route-guide",
        {
            "backlog_id": "AC-CAPSULE-MCP",
            "role": "worker",
            "work_type": "parallel_worker",
            "response_view": "compact",
        },
    )

    dispatcher.dispatch(
        "onboard_route_guide",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-CAPSULE-MCP",
            "role": "worker",
            "work_type": "parallel_worker",
            "response_view": "full",
        },
    )
    assert recorder.calls[-1][2]["response_view"] == "full"

    dispatcher.dispatch(
        "onboard_route_guide_section_fetch",
        {
            "project_id": "aming-claw",
            "guide_capsule_ref": "gcap-copy-safe",
            "sections": ["next_action", "action_input"],
            "backlog_id": "AC-CAPSULE-MCP",
            "role": "worker",
            "work_type": "parallel_worker",
        },
    )
    assert recorder.calls[-1] == (
        "POST",
        "/api/projects/aming-claw/onboard-route-guide/capsule",
        {
            "guide_capsule_ref": "gcap-copy-safe",
            "sections": ["next_action", "action_input"],
            "backlog_id": "AC-CAPSULE-MCP",
            "role": "worker",
            "work_type": "parallel_worker",
        },
    )


def test_release_operator_head_queue_schema_and_dispatch():
    properties = _tool_properties("release_operator_head_queue")
    assert properties["action"]["enum"] == [
        "read",
        "insert",
        "reorder",
        "skip",
        "remove",
    ]
    assert properties["action"]["default"] == "read"
    assert {
        "backlog_id",
        "backlog_ids",
        "reason",
        "route_token_ref",
        "authorization_backlog_id",
        "authorization_task_id",
        "historical_non_schedulable",
        "historical_execution_resume_allowed",
        "evidence_refs",
    }.issubset(properties)

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "release_operator_head_queue",
        {"project_id": "aming-claw"},
    )
    assert recorder.calls[-1] == (
        "GET",
        "/api/projects/aming-claw/release-operator-head-queue",
        None,
    )

    dispatcher.dispatch(
        "release_operator_head_queue",
        {
            "project_id": "aming-claw",
            "action": "remove",
            "backlog_id": "AC-HISTORICAL",
            "reason": "terminal audit-only source",
            "route_token_ref": "rtok-release-queue",
            "authorization_backlog_id": "AC-QUEUE-AUTH",
            "authorization_task_id": "task-queue-auth",
            "historical_non_schedulable": True,
            "historical_execution_resume_allowed": False,
            "evidence_refs": ["backlog:AC-HISTORICAL", "timeline:42"],
        },
    )
    assert recorder.calls[-1] == (
        "POST",
        "/api/projects/aming-claw/release-operator-head-queue",
        {
            "action": "remove",
            "backlog_id": "AC-HISTORICAL",
            "reason": "terminal audit-only source",
            "route_token_ref": "rtok-release-queue",
            "authorization_backlog_id": "AC-QUEUE-AUTH",
            "authorization_task_id": "task-queue-auth",
            "historical_non_schedulable": True,
            "historical_execution_resume_allowed": False,
            "evidence_refs": ["backlog:AC-HISTORICAL", "timeline:42"],
        },
    )


def test_task_timeline_append_schema_separates_qa_audit_from_close_statuses():
    properties = _tool_properties("task_timeline_append")
    description = properties["status"]["description"]
    assert "satisfy the close gate" in description
    assert "audit-only evidence" in description
    assert "never satisfy close" in description
    assert "recomputes base..candidate Git objects" in description
    assert "historical PASS remains rejected" in description
    assert "recursively scrubbed" in properties["payload"]["description"]
    assert "raw authorization" in properties["verification"]["description"]
    assert "Copy-safe artifact references" in properties["artifact_refs"][
        "description"
    ]


def test_managed_task_timeline_append_exposes_exact_runtime_binding_fields():
    properties = _tool_properties("task_timeline_append")
    string_fields = (
        "contract_execution_id",
        "direct_runtime_binding_hash",
        "stage_id",
        "line_id",
        "evidence_kind",
        "runtime_guide_hash",
    )
    for key in string_fields:
        assert properties[key]["type"] == "string"
    assert properties["execution_state_revision"]["type"] == "integer"


def test_managed_task_timeline_append_preserves_runtime_binding_fields(monkeypatch):
    calls = []
    dispatcher = ToolDispatcher(
        lambda method, path, body=None: calls.append((method, path, body))
        or {"ok": True},
        None,
    )
    monkeypatch.setattr(
        dispatcher,
        "_qa_role_token_for_scope",
        lambda *args, **kwargs: ("", None),
    )
    binding = {
        "contract_execution_id": "cex-direct-managed-qa",
        "execution_state_revision": 7,
        "direct_runtime_binding_hash": "sha256:" + "b" * 64,
        "stage_id": "qa_graph_context",
        "line_id": "qa_graph_context",
        "evidence_kind": "verification",
        "runtime_guide_hash": "sha256:" + "a" * 64,
    }

    assert dispatcher.dispatch(
        "task_timeline_append",
        {"project_id": "aming-claw", "event_type": "qa.graph_context", **binding},
    ) == {"ok": True}
    assert calls == [
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {"event_type": "qa.graph_context", **binding},
        )
    ]


def test_mcp_tool_schema_fingerprint_is_deterministic_and_change_detecting():
    first = mcp_tool_schema_fingerprint(TOOLS)
    assert first == mcp_tool_schema_fingerprint(TOOLS)
    changed = json.loads(json.dumps(TOOLS))
    changed[0]["inputSchema"]["properties"]["synthetic_drift"] = {"type": "string"}
    assert mcp_tool_schema_fingerprint(changed) != first


def test_task_timeline_append_schema_exposes_direct_main_qa_runtime_binding_fields():
    tool = next(
        item
        for item in governance_mcp_server.TOOLS
        if item.get("name") == "task_timeline_append"
    )
    properties = tool["inputSchema"]["properties"]

    assert {
        "contract_execution_id",
        "execution_state_revision",
        "stage_id",
        "line_id",
        "runtime_guide_hash",
        "direct_runtime_binding_hash",
    }.issubset(properties)
    for key in (
        "contract_execution_id",
        "stage_id",
        "line_id",
        "runtime_guide_hash",
        "direct_runtime_binding_hash",
    ):
        assert properties[key]["type"] == "string"
    assert properties["execution_state_revision"]["type"] == "integer"


def test_task_timeline_append_dispatch_preserves_top_level_direct_main_qa_binding_header_only(
    monkeypatch,
):
    raw_token = "gov-qa-direct-main-facade-secret"
    calls = []

    def record_http(method, path, data, *, gov_token=""):
        calls.append((method, path, data, gov_token))
        return {"ok": True}

    monkeypatch.setattr(
        governance_mcp_server,
        "_http_with_optional_gov_token",
        record_http,
    )
    commit_sha = "a" * 40
    binding = {
        "contract_execution_id": "cex-direct-main-qa-facade",
        "execution_state_revision": 7,
        "stage_id": "qa_graph_context",
        "line_id": "qa_graph_context",
        "runtime_guide_hash": "sha256:" + ("b" * 64),
        "direct_runtime_binding_hash": "sha256:" + ("c" * 64),
    }

    governance_mcp_server._dispatch_tool(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-DIRECT-MAIN-QA-FACADE",
            "task_id": "cex-direct-main-qa-facade",
            "event_type": "qa.graph_context",
            "event_kind": "qa_graph_context",
            "phase": "verification",
            "actor": "qa:direct-main-facade",
            "status": "passed",
            "commit_sha": commit_sha,
            "qa_session_token": raw_token,
            "payload": {"graph_trace_ids": ["gqt-direct-main-facade"]},
            **binding,
        },
    )

    assert len(calls) == 1
    method, path, body, role_token = calls[0]
    assert method == "POST"
    assert path == "/api/task/aming-claw/timeline"
    assert role_token == raw_token
    assert body is not None
    assert {key: body[key] for key in binding} == binding
    assert "qa_session_token" not in body
    assert raw_token not in json.dumps(body, sort_keys=True)


def test_observer_direct_mutation_exception_managed_dispatch_is_literal_and_fail_closed():
    tool = next(
        item
        for item in TOOLS
        if item.get("name") == "observer_direct_mutation_exception"
    )
    schema = tool["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["event_type"]["enum"] == [
        "mf.observer_direct_implementation_exception"
    ]
    assert schema["properties"]["route_token_ref"]["type"] == "string"

    literal_guide_arguments = {
        "project_id": "aming-claw",
        "backlog_id": "AC-DIRECT-GUIDE-LITERAL",
        "task_id": "onboard-service-direct-guide-literal",
        "event_type": "mf.observer_direct_implementation_exception",
        "event_kind": "observer_direct_implementation_exception",
        "phase": "pre_mutation",
        "status": "accepted",
        "decision": "operator_supervised_direct_main_approved",
        "actor": "observer",
        "payload": {
            "reason": "bounded operator-approved change",
            "observer_direct_mutation": True,
            "tiny_deterministic_scope": True,
        },
        "verification": {
            "db_verified_pre_implementation_graph_trace": True,
        },
        "artifact_refs": {"graph_trace_ids": ["gqt-direct-guide"]},
        "route_token_ref": "rtok-direct-guide-literal",
    }
    expected_body = {
        key: value
        for key, value in literal_guide_arguments.items()
        if key != "project_id"
    }
    recorder = _Recorder()
    result = _dispatcher(recorder).dispatch(
        "observer_direct_mutation_exception",
        literal_guide_arguments,
    )
    assert result["ok"] is True
    assert recorder.calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer/direct-mutation-exception",
            expected_body,
        )
    ]

    class RejectedDirectFacade(_Recorder):
        def api(
            self,
            method: str,
            path: str,
            data: dict | None = None,
        ) -> dict:
            self.calls.append((method, path, data))
            return {
                "error": "observer_direct_mutation_exception_shape_required",
                "zero_write_rejection": True,
                "writes_performed": False,
            }

    rejected_recorder = RejectedDirectFacade()
    rejected = _dispatcher(rejected_recorder).dispatch(
        "observer_direct_mutation_exception",
        {**literal_guide_arguments, "phase": "implementation"},
    )
    assert rejected["zero_write_rejection"] is True
    assert rejected["writes_performed"] is False
    assert len(rejected_recorder.calls) == 1
    assert rejected_recorder.calls[0][1].endswith(
        "/observer/direct-mutation-exception"
    )


def test_parallel_branch_merge_queue_apply_forwards_branch_ref():
    properties = _tool_properties("parallel_branch_merge_queue_apply")
    assert "branch_ref" in properties
    assert "current_target_head" in properties
    assert "latest_target_head" in properties

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "parallel_branch_merge_queue_apply",
        {
            "project_id": "aming-claw",
            "merge_queue_id": "mq-1",
            "task_id": "task-1",
            "branch_ref": "refs/heads/codex/task-1",
            "latest_target_head": "abc123",
            "dry_run": True,
        },
    )

    assert recorder.calls[-1] == (
        "POST",
        "/api/graph-governance/aming-claw/parallel-branches/merge-execute",
        {
            "merge_queue_id": "mq-1",
            "task_id": "task-1",
            "branch_ref": "refs/heads/codex/task-1",
            "current_target_head": "abc123",
            "dry_run": True,
        },
    )


def test_parallel_branch_merge_queue_apply_forwards_explicit_flow():
    properties = _tool_properties("parallel_branch_merge_queue_apply")
    assert set(properties["flow"]["enum"]) == {
        "mf_parallel",
        "mf_batch_parallel",
        "direct_fix",
        "hotfix",
    }

    builders = (
        governance_mcp_server._parallel_branch_merge_queue_apply_body,
        mcp_tools._parallel_branch_merge_queue_apply_body,
    )
    for flow in properties["flow"]["enum"]:
        args = {
            "merge_queue_id": "mq-flow",
            "task_id": "task-flow",
            "flow": flow,
            "unsupported_flow_probe": "must-not-forward",
        }
        for builder in builders:
            body = builder(args)
            assert body["flow"] == flow
            assert "unsupported_flow_probe" not in body

        recorder = _Recorder()
        dispatcher = _dispatcher(recorder)
        dispatcher.dispatch(
            "parallel_branch_merge_queue_apply",
            {"project_id": "aming-claw", **args},
        )
        assert recorder.calls[-1][2]["flow"] == flow
        assert "unsupported_flow_probe" not in recorder.calls[-1][2]

    for builder in builders:
        assert "flow" not in builder(
            {
                "merge_queue_id": "mq-legacy",
                "task_id": "task-legacy",
            }
        )


def test_parallel_branch_merge_queue_materialize_forwards_checkpoint():
    properties = _tool_properties("parallel_branch_merge_queue_materialize")
    assert "checkpoint_id" in properties
    assert "require_finish_gate" in properties

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "parallel_branch_merge_queue_materialize",
        {
            "project_id": "aming-claw",
            "merge_queue_id": "mq-1",
            "task_id": "task-1",
            "checkpoint_id": "ckpt-worker-finish",
            "route_token_ref": "rtok-1",
        },
    )

    assert recorder.calls[-1] == (
        "POST",
        "/api/graph-governance/aming-claw/parallel-branches/merge-queue/materialize",
        {
            "merge_queue_id": "mq-1",
            "task_id": "task-1",
            "checkpoint_id": "ckpt-worker-finish",
            "route_token_ref": "rtok-1",
            "require_finish_gate": True,
        },
    )


def test_parallel_branch_merge_queue_materialize_forwards_audited_postmerge_recovery_refs():
    properties = _tool_properties("parallel_branch_merge_queue_materialize")
    recovery_schema = properties["audited_postmerge_recovery"]
    assert recovery_schema["additionalProperties"] is False
    assert set(recovery_schema["required"]) == {
        "source_contract_execution_id",
        "runtime_context_id",
        "independent_qa_receipt_ref",
        "manual_merge_event_ref",
        "diagnostic_backlog_id",
    }

    recovery = {
        "source_contract_execution_id": "cex-source",
        "runtime_context_id": "mfrctx-source",
        "independent_qa_receipt_ref": "timeline:101",
        "manual_merge_event_ref": "timeline:102",
        "diagnostic_backlog_id": "AC-SYSTEM-RECOVERY",
    }
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "parallel_branch_merge_queue_materialize",
        {
            "project_id": "aming-claw",
            "merge_queue_id": "mq-1",
            "task_id": "task-1",
            "backlog_id": "AC-SOURCE",
            "route_token_ref": "rtok-1",
            "audited_postmerge_recovery": recovery,
        },
    )

    assert recorder.calls[-1] == (
        "POST",
        "/api/graph-governance/aming-claw/parallel-branches/merge-queue/materialize",
        {
            "merge_queue_id": "mq-1",
            "task_id": "task-1",
            "backlog_id": "AC-SOURCE",
            "route_token_ref": "rtok-1",
            "audited_postmerge_recovery": recovery,
            "bug_id": "AC-SOURCE",
        },
    )


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        return {"ok": True, "method": method, "path": path, "data": data}


class _AuthRecorder(_Recorder):
    def __init__(self):
        super().__init__()
        self.auth_calls: list[tuple[str, str, dict | None, str]] = []

    def api_with_role_token(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        *,
        role_token: str,
        timeout_seconds: int = 15,
    ) -> dict:
        self.auth_calls.append((method, path, data, role_token))
        return {
            "ok": True,
            "method": method,
            "path": path,
            "data": data,
            "role_token": role_token,
        }


class _RuntimeGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {
                "status": "ok",
                "version": "abc1234",
                "mcp_tool_schema_version": MCP_TOOL_SCHEMA_VERSION,
                "mcp_tool_schema_min_client_version": MCP_TOOL_SCHEMA_VERSION,
                "mcp_tool_schema_fingerprint": mcp_tool_schema_fingerprint(TOOLS),
            }
        if path == "/api/version-check/aming-claw":
            return {
                "ok": True,
                "head": "abc1234",
                "chain_version": "abc1234",
                "dirty": False,
                "runtime_match": True,
                "gov_runtime_version": "abc1234",
                "sm_runtime_version": "abc1234",
                "target_project_version": {
                    "head": "abc1234",
                    "chain_version": "abc1234",
                    "dirty": False,
                    "synced_with_governance": True,
                    "legacy_project_version": {
                        "chain_version": "abc1234",
                        "git_head": "abc1234",
                        "synced_with_target": True,
                    },
                },
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


class _LegacyRuntimeDriftGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "new1234"}
        if path == "/api/version-check/aming-claw":
            return {
                "ok": False,
                "head": "new1234",
                "chain_version": "new1234",
                "dirty": False,
                "runtime_match": True,
                "gov_runtime_version": "new1234",
                "sm_runtime_version": "new1234",
                "target_project_version": {
                    "head": "new1234",
                    "chain_version": "new1234",
                    "dirty": False,
                    "synced_with_governance": True,
                    "legacy_project_version": {
                        "chain_version": "legacy123",
                        "git_head": "new1234",
                        "synced_with_target": True,
                    },
                },
                "message": "legacy project_version CHAIN_VERSION drift",
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
                "target_project_version": {
                    "head": "new1234",
                    "chain_version": "new1234",
                    "dirty": False,
                    "synced_with_governance": True,
                    "legacy_project_version": {
                        "chain_version": "legacy123",
                        "git_head": "new1234",
                        "synced_with_target": True,
                    },
                },
                "governance_runtime": {
                    "chain_version": "new1234",
                    "gov_runtime_version": "new1234",
                    "sm_runtime_version": "old1234",
                    "runtime_match": False,
                },
                "message": "ServiceManager runtime is behind",
            }
        return {"ok": True, "method": method, "path": path, "data": data}


class _PostMergeRuntimeWaiverGovRecorder(_Recorder):
    head = "7e8d2ee81e8bad06c712e69b10fc8c3851d05317"
    chain_version = "6b8c90a6"
    legacy_chain_version = "d98dc4a27fbf5754846921cd8b041aee7a6d36ad"

    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "7e8d2ee8"}
        if path == "/api/version-check/aming-claw":
            legacy_project_version = {
                "chain_version": self.legacy_chain_version,
                "updated_at": "2026-06-20T13:59:35Z",
                "git_head": self.head,
                "dirty_files": [],
                "git_synced_at": "2026-07-02T03:24:27Z",
                "synced_with_target": True,
            }
            target_project_version = {
                "project_id": "aming-claw",
                "project_root": "/Users/yingzhang/my-system/aming-claw/aming-claw",
                "head": self.head,
                "head_short": "7e8d2ee8",
                "chain_version": self.chain_version,
                "dirty": False,
                "dirty_files": [],
                "source": "trailer",
                "synced_with_governance": True,
                "governance_synced_head": self.head,
                "git_synced_at": "2026-07-02T03:24:27Z",
                "legacy_project_version": legacy_project_version,
            }
            return {
                "ok": False,
                "project_id": "aming-claw",
                "head": self.head,
                "target_head": self.head,
                "target_head_short": "7e8d2ee8",
                "project_root": "/Users/yingzhang/my-system/aming-claw/aming-claw",
                "target_project_root": "/Users/yingzhang/my-system/aming-claw/aming-claw",
                "target_project_version": target_project_version,
                "target_chain_version": self.chain_version,
                "target_synced_with_governance": True,
                "target_dirty": False,
                "target_dirty_files": [],
                "governance_synced_head": self.head,
                "trailer_head": self.chain_version,
                "chain_version": self.chain_version,
                "dirty": False,
                "dirty_files": [],
                "git_synced_at": "2026-07-02T03:24:27Z",
                "source": "trailer",
                "message": f"HEAD ({self.head}) != CHAIN_VERSION ({self.chain_version})",
                "legacy_project_version": legacy_project_version,
                "governance_chain_version": self.chain_version,
                "gov_runtime_version": "7e8d2ee8",
                "sm_runtime_version": "fa63faf4",
                "runtime_scope": "governance",
                "runtime_match": False,
                "governance_runtime": {
                    "project_root": "/Users/yingzhang/my-system/aming-claw/aming-claw",
                    "chain_version": self.chain_version,
                    "gov_runtime_version": "7e8d2ee8",
                    "sm_runtime_version": "fa63faf4",
                    "runtime_match": False,
                },
            }
        return {"ok": True, "method": method, "path": path, "data": data}


class _OfflineGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        return {"ok": False, "error": "<urlopen error timed out>"}


class _TimeoutAwareGovRecorder:
    gov_url = "http://governance.test"

    def __init__(self, *, timeout_current_full: bool = False):
        self.timeout_current_full = timeout_current_full
        self.calls: list[tuple[str, str, dict | None, int]] = []

    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        raise AssertionError("generic governance API should not handle current-full reconcile")

    def _request_json(
        self,
        method: str,
        url: str,
        data: dict | None = None,
        timeout: int = 15,
    ) -> dict:
        self.calls.append((method, url, data, timeout))
        if url.endswith("/reconcile/current-full") and self.timeout_current_full:
            return {"ok": False, "error": "timed out"}
        if "/operations/queue" in url:
            return {
                "ok": True,
                "operations": [
                    {
                        "operation_id": "current-full-run-1",
                        "operation_type": "current_full_reconcile",
                        "status": "running",
                        "progress": {"done": 3, "total": 10},
                        "last_result": "run current-full-run-1 still running",
                    }
                ],
                "count": 1,
            }
        return {"ok": True, "method": method, "url": url, "data": data}


def _dispatcher(recorder: _Recorder, manager: _Recorder | None = None) -> ToolDispatcher:
    return ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=manager.api if manager else None,
        workspace=".",
    )


def _assert_route_issue_result_is_copy_safe(result: dict) -> None:
    def walk(value):
        if isinstance(value, dict):
            assert "route_token" not in value
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    serialized = json.dumps(result, sort_keys=True)
    assert "raw-write-authority" not in serialized
    assert "nested-raw-write-authority" not in serialized
    assert result["route_token_ref"] == "rtok-copy-safe"
    assert result["route_identity"] == {"route_id": "route-copy-safe"}
    assert result["diagnostics"] == {"scope": "exact"}
    assert result["raw_route_token_exposed"] is False
    assert result["nested"]["raw_route_token_exposed"] is False


def test_observer_route_context_issue_mcp_boundaries_strip_raw_route_token(monkeypatch):
    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        schema = next(
            item for item in registry
            if item["name"] == "observer_route_context_issue"
        )
        assert "never a raw route_token" in schema["description"]

    raw_result = {
        "ok": True,
        "route_token_ref": "rtok-copy-safe",
        "route_token": {"token": "raw-write-authority"},
        "route_identity": {"route_id": "route-copy-safe"},
        "diagnostics": {"scope": "exact"},
        "nested": {
            "route_token": {"token": "nested-raw-write-authority"},
            "raw_route_token_exposed": True,
        },
    }

    class RouteIssueRecorder(_Recorder):
        def api(self, method: str, path: str, data: dict | None = None) -> dict:
            self.calls.append((method, path, data))
            return raw_result

    recorder = RouteIssueRecorder()
    direct = _dispatcher(recorder).dispatch(
        "observer_route_context_issue",
        {
            "project_id": "aming-claw",
            "task_id": "copy-safe-route-issue",
            "caller_role": "observer",
        },
    )
    _assert_route_issue_result_is_copy_safe(direct)
    assert recorder.calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer/route-context/issue",
            {
                "project_id": "aming-claw",
                "task_id": "copy-safe-route-issue",
                "caller_role": "observer",
            },
        )
    ]
    assert raw_result["route_token"]["token"] == "raw-write-authority"

    stdio_calls = []

    def fake_http(method, path, body):
        stdio_calls.append((method, path, body))
        return raw_result

    monkeypatch.setattr(
        governance_mcp_server,
        "_http",
        fake_http,
    )
    stdio = governance_mcp_server._dispatch_tool(
        "observer_route_context_issue",
        {
            "project_id": "aming-claw",
            "task_id": "copy-safe-route-issue",
        },
    )
    _assert_route_issue_result_is_copy_safe(stdio)
    assert stdio_calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer/route-context/issue",
            {
                "project_id": "aming-claw",
                "task_id": "copy-safe-route-issue",
                "caller_role": "observer",
            },
        )
    ]
    assert raw_result["nested"]["route_token"]["token"] == (
        "nested-raw-write-authority"
    )


def test_observer_route_context_renew_dispatchers_forward_exact_guide_body(monkeypatch):
    arguments = {
        "project_id": "aming-claw",
        "backlog_id": "AC-DIRECT-RENEW-ADAPTER",
        "task_id": "cex-direct-main-renew-adapter",
        "route_token_ref": "rtok-direct-renew-adapter",
        "observer_session_id": "obs-direct-renew-adapter",
    }
    expected = {**arguments, "caller_role": "observer"}
    result = {
        "ok": True,
        "route_token_ref": "rtok-direct-renew-adapter-next",
        "raw_route_token_exposed": False,
    }

    recorder = _Recorder()
    recorder.api = lambda method, path, data=None: (
        recorder.calls.append((method, path, data)) or result
    )
    assert _dispatcher(recorder).dispatch(
        "observer_route_context_renew", arguments
    ) == result

    stdio_calls = []
    monkeypatch.setattr(
        governance_mcp_server,
        "_http",
        lambda method, path, data=None: (
            stdio_calls.append((method, path, data)) or result
        ),
    )
    assert governance_mcp_server._dispatch_tool(
        "observer_route_context_renew", arguments
    ) == result

    expected_call = (
        "POST",
        "/api/projects/aming-claw/observer/route-context/renew",
        expected,
    )
    assert recorder.calls == [expected_call]
    assert stdio_calls == [expected_call]
    body_hashes = {
        hashlib.sha256(
            json.dumps(call[2], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for call in (recorder.calls[0], stdio_calls[0])
    }
    assert len(body_hashes) == 1
    assert set(expected) == {
        "project_id",
        "caller_role",
        "backlog_id",
        "task_id",
        "route_token_ref",
        "observer_session_id",
    }
    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        schema = next(
            item for item in registry
            if item["name"] == "observer_route_context_renew"
        )
        assert "route_token" not in schema["inputSchema"]["properties"]


def test_observer_route_context_renew_adapters_filter_to_declared_falsey_fields(
    monkeypatch,
):
    arguments = {
        "project_id": "aming-claw",
        "caller_role": "observer",
        "observer_session_id": "obs-allowlist",
        "route_token_ref": "rtok-allowlist",
        "observer_route_token_ref": "rtok-allowlist-alias",
        "backlog_id": "AC-RENEW-ALLOWLIST",
        "bug_id": "AC-RENEW-ALLOWLIST",
        "task_id": "cex-renew-allowlist",
        "contract_execution_id": "cex-renew-allowlist",
        "allowed_actions": [],
        "target_files": [],
        "owned_files": False,
        "evidence_refs": [],
        "ttl_hours": 0,
        "renew_within_seconds": False,
        "route_token": "RAW_ROUTE_SENTINEL",
        "session_token": "RAW_SESSION_SENTINEL",
        "unknown_non_none": "UNKNOWN_SENTINEL",
        "unknown_none": None,
    }
    allowed = set(arguments) - {
        "route_token", "session_token", "unknown_non_none", "unknown_none"
    }
    expected = {key: arguments[key] for key in allowed}
    calls = []
    recorder = _Recorder()
    recorder.api = lambda method, path, data=None: (
        calls.append((method, path, data)) or {"ok": True}
    )
    _dispatcher(recorder).dispatch("observer_route_context_renew", arguments)
    monkeypatch.setattr(
        governance_mcp_server,
        "_http",
        lambda method, path, data=None: (
            calls.append((method, path, data)) or {"ok": True}
        ),
    )
    governance_mcp_server._dispatch_tool(
        "observer_route_context_renew", arguments
    )

    assert [call[2] for call in calls] == [expected, expected]
    assert calls[0][:2] == calls[1][:2] == (
        "POST", "/api/projects/aming-claw/observer/route-context/renew"
    )
    assert calls[0][2]["ttl_hours"] == 0
    assert calls[0][2]["renew_within_seconds"] is False
    assert calls[0][2]["owned_files"] is False
    assert hashlib.sha256(
        json.dumps(calls[0][2], sort_keys=True).encode()
    ).digest() == hashlib.sha256(
        json.dumps(calls[1][2], sort_keys=True).encode()
    ).digest()


def test_real_mcp_jsonrpc_route_renew_strips_undeclared_and_raw_fields(
    monkeypatch,
    tmp_path,
):
    raw_sentinel = "RAW_ROUTE_RENEW_JSONRPC_SENTINEL"
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    mcp = object.__new__(plugin_mcp_server.AmingClawMCP)
    mcp.project_id = "aming-claw"
    mcp.gov_url = "http://127.0.0.1:40008"
    mcp.dispatcher = ToolDispatcher(
        api_fn=mcp._http,
        worker_pool=None,
        workspace=str(tmp_path),
    )
    http_calls = []
    mcp._request_json = lambda method, url, data=None, timeout=15: (
        http_calls.append((method, url, data, timeout))
        or {"ok": True, "raw_route_token_exposed": False}
    )
    messages = []
    monkeypatch.setattr(plugin_mcp_server, "_write", messages.append)
    arguments = {
        "project_id": "aming-claw",
        "backlog_id": "AC-RENEW-JSONRPC",
        "task_id": "cex-renew-jsonrpc",
        "route_token_ref": "rtok-renew-jsonrpc",
        "observer_session_id": "obs-renew-jsonrpc",
        "route_token": raw_sentinel,
        "session_token": raw_sentinel,
        "undeclared": {"nested": raw_sentinel},
    }

    mcp._handle(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "observer_route_context_renew",
            "arguments": arguments,
        },
    }))

    assert http_calls == [(
        "POST",
        "http://127.0.0.1:40008/api/projects/aming-claw/observer/route-context/renew",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-RENEW-JSONRPC",
            "task_id": "cex-renew-jsonrpc",
            "route_token_ref": "rtok-renew-jsonrpc",
            "observer_session_id": "obs-renew-jsonrpc",
            "caller_role": "observer",
        },
        15,
    )]
    assert raw_sentinel not in json.dumps(http_calls)
    assert raw_sentinel not in json.dumps(messages)

    mcp._handle(json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "observer_route_context_renew",
            "arguments": {**arguments, "project_id": "content-sys"},
        },
    }))
    assert len(http_calls) == 1
    rejected = json.loads(messages[-1]["result"]["content"][0]["text"])
    assert rejected == {
        "error": "mcp_world_project_scope_mismatch",
        "writes_performed": False,
        "mutation_performed": False,
    }


def test_backlog_upsert_accepts_structured_acceptance_scope_without_breaking_strings():
    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        tool = next(item for item in registry if item["name"] == "backlog_upsert")
        item_schema = tool["inputSchema"]["properties"]["acceptance_criteria"][
            "items"
        ]
        variants = item_schema["anyOf"]
        assert {variant["type"] for variant in variants} == {"string", "object"}
        object_schema = next(
            variant for variant in variants if variant["type"] == "object"
        )
        assert object_schema["required"] == ["id", "required_scope"]
        scope_schema = object_schema["properties"]["required_scope"]
        assert scope_schema["required"] == ["kind"]
        assert {"files", "nodes", "files_and_nodes"}.issubset(
            scope_schema["properties"]["kind"]["enum"]
        )

    structured = {
        "id": "AC-DEMO-FOCUS-001",
        "description": "Render the Today Focus card.",
        "required_scope": {
            "kind": "files",
            "files": ["src/app.js"],
        },
    }

    class _RoundTripRecorder(_Recorder):
        acceptance_criteria: list = []

        def api(
            self,
            method: str,
            url: str,
            data: dict | None = None,
            timeout: int = 15,
        ) -> dict:
            self.calls.append((method, url, data, timeout))
            if method == "POST":
                self.acceptance_criteria = list(
                    (data or {}).get("acceptance_criteria") or []
                )
                return {"ok": True}
            return {
                "ok": True,
                "bug": {"acceptance_criteria": self.acceptance_criteria},
            }

    recorder = _RoundTripRecorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "daily-planner-lite-fresh",
            "bug_id": "AC-DEMO-STRUCTURED-ACCEPTANCE",
            "acceptance_criteria": ["Legacy free text remains accepted.", structured],
        },
    )
    fetched = dispatcher.dispatch(
        "backlog_get",
        {
            "project_id": "daily-planner-lite-fresh",
            "bug_id": "AC-DEMO-STRUCTURED-ACCEPTANCE",
        },
    )
    assert fetched["bug"]["acceptance_criteria"] == [
        "Legacy free text remains accepted.",
        structured,
    ]
    assert isinstance(fetched["bug"]["acceptance_criteria"][1], dict)

    from agent.governance.contract_state_runtime import (
        acceptance_file_fence_closure_gate,
    )

    closure = acceptance_file_fence_closure_gate(
        [structured],
        ["src/app.js"],
    )
    assert closure["accepted"] is True
    assert closure["criterion_ids"] == ["AC-DEMO-FOCUS-001"]
    assert closure["required_file_union"] == ["src/app.js"]


def test_runtime_mcp_backlog_upsert_exposes_and_forwards_triage_resolution():
    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        tool = next(item for item in registry if item["name"] == "backlog_upsert")
        properties = tool["inputSchema"]["properties"]
        assert properties["triage_action"] == {
            "type": "string",
            "enum": ["admit", "merge_into", "supersede", "reject_dup"],
        }
        assert properties["triage_target_bug_id"] == {"type": "string"}

    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "aming-claw",
            "bug_id": "AC-FRESH-TRIAGE-ROW",
            "title": "Fresh bounded row",
            "triage_action": "admit",
            "triage_target_bug_id": "AC-OLD-UMBRELLA",
        },
    )

    assert recorder.calls[-1] == (
        "POST",
        "/api/backlog/aming-claw/AC-FRESH-TRIAGE-ROW",
        {
            "title": "Fresh bounded row",
            "triage_action": "admit",
            "triage_target_bug_id": "AC-OLD-UMBRELLA",
        },
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
        "mf_batch_parallel_enter",
        "observer_repair_run_plan",
        "observer_repair_run_route_evidence",
        "backlog_export",
        "backlog_import",
        "graph_status",
        "graph_operations_queue",
        "graph_current_full_reconcile",
        "stale_artifact_cleanup",
        "stale_artifact_cleanup_apply",
        "graph_query",
        "runtime_context_current",
        "runtime_context_worker_guide",
        "parallel_branch_allocate_precheck",
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
        "onboard_contract_start",
        "onboard_contract_current",
        "onboard_contract_submit_line",
    }.issubset(names)


def test_mcp_graph_current_full_reconcile_schema_exposes_route_proof_fields():
    props = _tool_properties("graph_current_full_reconcile")

    for key in (
        "backlog_id",
        "task_id",
        "contract_execution_id",
        "observer_session_id",
        "observer_route_token_ref",
        "route_token_ref",
        "project_root",
        "worktree_path",
        "ref_name",
        "branch_ref",
    ):
        assert key in props

    assert props["observer_session_id"]["type"] == "string"
    assert props["timeout_seconds"]["type"] == "integer"
    assert "900 seconds" in props["timeout_seconds"]["description"]
    assert "raw route tokens are not accepted" in props["observer_route_token_ref"][
        "description"
    ]
    assert props["route_token_ref"]["description"] == "Alias for observer_route_token_ref."
    assert "activate=false" in props["project_root"]["description"]
    assert props["worktree_path"]["description"] == (
        "Alias for project_root on candidate-only builds."
    )
    assert props["response_view"] == {
        "type": "string",
        "enum": ["compact", "full"],
        "default": "compact",
        "description": (
            "Bounded MCP response projection. compact is the safe default; "
            "full is compatibility-only and may return a truthful "
            "representation-too-large result after a durable reconcile."
        ),
    }
    mirror = next(
        tool
        for tool in governance_mcp_server.TOOLS
        if tool["name"] == "graph_current_full_reconcile"
    )
    assert mirror["inputSchema"]["properties"]["response_view"] == (
        props["response_view"]
    )


def test_mcp_graph_current_full_reconcile_forwards_route_proof_fields():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "target_commit_sha": "head",
            "activate": True,
            "semantic_use_ai": False,
            "backlog_id": "AC-CURRENT-FULL",
            "contract_execution_id": "cex-current-full",
            "observer_session_id": "obs-current-full",
            "observer_route_token_ref": "rtok-current-full",
            "project_root": "/tmp/ac-candidate",
            "ref_name": "refs/heads/codex/ac-candidate",
            "run_id": "current-full-route-proof",
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/reconcile/current-full",
            {
                "target_commit_sha": "head",
                "activate": True,
                "semantic_use_ai": False,
                "backlog_id": "AC-CURRENT-FULL",
                "contract_execution_id": "cex-current-full",
                "observer_session_id": "obs-current-full",
                "observer_route_token_ref": "rtok-current-full",
                "project_root": "/tmp/ac-candidate",
                "ref_name": "refs/heads/codex/ac-candidate",
                "run_id": "current-full-route-proof",
            },
        )
    ]


def test_mcp_graph_current_full_reconcile_bounds_success_and_keeps_write_truth(
    monkeypatch,
):
    raw_result = {
        "ok": True,
        "status": "complete",
        "project_id": "aming-claw",
        "run_id": "current-full-bounded-result",
        "snapshot_id": "full-bounded-result",
        "active_snapshot_id": "full-bounded-result",
        "snapshot_status": "active",
        "target_commit_sha": "a" * 40,
        "head_commit": "a" * 40,
        "active_graph_commit": "a" * 40,
        "current_full_reconcile": True,
        "activated": True,
        "activation_verification": {
            "verified": True,
            "active_snapshot_id": "full-bounded-result",
            "active_graph_commit": "a" * 40,
            "pending_scope_reconcile_count": 0,
            "pending_scope_reconcile_zero": True,
        },
        "timeline_event_recorded": {
            "id": 25190,
            "ref": "timeline:25190",
            "event_kind": "reconcile",
            "phase": "reconcile",
            "status": "passed",
        },
        "current_full_reconcile_provenance": {
            "provenance_id": "cfrp-bounded-result",
            "snapshot_id": "full-bounded-result",
            "reconcile_event_id": 25190,
        },
        "route_token": {"token": "raw-route-must-not-escape"},
        "trace": {"oversized": "x" * 300_000},
    }

    class LargeReconcileRecorder(_Recorder):
        def api(
            self,
            method: str,
            url: str,
            data: dict | None = None,
            timeout: int = 15,
        ) -> dict:
            self.calls.append((method, url, data, timeout))
            return raw_result

    recorder = LargeReconcileRecorder()
    direct = _dispatcher(recorder).dispatch(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "run_id": "current-full-bounded-result",
        },
    )
    assert direct["ok"] is True
    assert direct["response_view"] == "compact"
    assert direct["bounded_response"] is True
    assert direct["activated"] is True
    assert direct["writes_performed"] is True
    assert direct["mutation_performed"] is True
    assert direct["timeline_event_recorded"]["id"] == 25190
    assert direct["current_full_reconcile_provenance"] == {
        "provenance_id": "cfrp-bounded-result",
        "snapshot_id": "full-bounded-result",
        "reconcile_event_id": 25190,
    }
    assert len(json.dumps(direct).encode()) < 64 * 1024
    assert "raw-route-must-not-escape" not in json.dumps(direct)
    assert recorder.calls[0][2] == {
        "run_id": "current-full-bounded-result"
    }

    calls = []

    def fake_http(
        method: str,
        path: str,
        body: dict | None = None,
        *,
        gov_token: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        calls.append((method, path, body, gov_token, timeout_seconds))
        return raw_result

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    mirror = governance_mcp_server._dispatch_tool(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "run_id": "current-full-bounded-result",
        },
    )
    assert mirror == direct
    assert calls[0][2] == {"run_id": "current-full-bounded-result"}

    full = _dispatcher(LargeReconcileRecorder()).dispatch(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "run_id": "current-full-bounded-result",
            "response_view": "full",
        },
    )
    assert full["ok"] is False
    assert full["reconcile_ok"] is True
    assert full["error"] == (
        "graph_current_full_reconcile_full_response_too_large"
    )
    assert full["writes_performed"] is True
    assert full["safe_retry"] is False
    assert full["compact_result"]["active_snapshot_id"] == (
        "full-bounded-result"
    )


def test_mcp_graph_current_full_reconcile_invalid_view_is_local_zero_write(
    monkeypatch,
):
    recorder = _Recorder()
    direct = _dispatcher(recorder).dispatch(
        "graph_current_full_reconcile",
        {"project_id": "aming-claw", "response_view": "verbose"},
    )
    assert direct == {
        "ok": False,
        "error": "graph_current_full_reconcile_response_view_invalid",
        "message": "response_view must be compact or full",
        "response_view": "verbose",
        "http_request_performed": False,
        "zero_write_rejection": True,
        "writes_performed": False,
        "mutation_performed": False,
    }
    assert recorder.calls == []

    calls = []
    monkeypatch.setattr(
        governance_mcp_server,
        "_http",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    mirror = governance_mcp_server._dispatch_tool(
        "graph_current_full_reconcile",
        {"project_id": "aming-claw", "response_view": "verbose"},
    )
    assert mirror == direct
    assert calls == []


def test_mcp_runtime_context_finish_gate_bounds_durable_success_without_retry():
    huge = "raw-finish-projection-must-not-escape" * 20_000
    raw_result = {
        "ok": True,
        "schema_version": "runtime_context.write_facade_response.v1",
        "project_id": "aming-claw",
        "backlog_id": "AC-FINISH-GATE-BOUNDED",
        "runtime_context_id": "mfrctx-finish-bounded",
        "task_id": "worker-finish-bounded",
        "parent_task_id": "cex-finish-bounded",
        "action": "finish_gate",
        "request_id": "req-finish-bounded",
        "timeline_event": {
            "id": "92",
            "event_ref": "timeline:92",
            "event_type": "mf_subagent.finish_gate",
            "event_kind": "mf_subagent_finish_gate",
            "phase": "finish_gate",
            "status": "passed",
            "task_id": "worker-finish-bounded",
            "backlog_id": "AC-FINISH-GATE-BOUNDED",
        },
        "gate": {
            "schema_version": "mf_subagent_finish_gate.v1",
            "status": "passed",
            "ok": True,
            "checkpoint_id": "ckpt-finish-bounded",
            "validated_head_commit": "a" * 40,
            "recursive_projection": huge,
        },
        "context": {
            "runtime_context_id": "mfrctx-finish-bounded",
            "task_id": "worker-finish-bounded",
            "parent_task_id": "cex-finish-bounded",
            "backlog_id": "AC-FINISH-GATE-BOUNDED",
            "status": "validated",
            "checkpoint_id": "ckpt-finish-bounded",
            "head_commit": "a" * 40,
            "recursive_projection": huge,
        },
        "contract_runtime_canonical_line": {
            "accepted": True,
            "status": "completed",
            "contract_execution_id": "cex-finish-bounded",
            "runtime_context_id": "mfrctx-finish-bounded",
            "task_id": "worker-finish-bounded",
            "stage_id": "worker_finish",
            "line_id": "worker_finish_gate",
            "execution_state_revision": 22,
            "execution_state_hash": "sha256:" + ("b" * 64),
            "contract_runtime_mutated": True,
            "next_legal_action": {
                "id": "qa_independent_verification",
                "action": "record_independent_qa",
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "owner_role": "qa",
                "contract_execution_id": "cex-finish-bounded",
                "recursive_projection": huge,
            },
            "recursive_projection": huge,
        },
        "recursive_projection": huge,
    }

    class LargeFinishRecorder(_Recorder):
        def api(self, method, path, data=None, **_kwargs):
            self.calls.append((method, path, data))
            return raw_result

    result = _dispatcher(LargeFinishRecorder()).dispatch(
        "runtime_context_finish_gate",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-finish-bounded",
            "checkpoint_id": "ckpt-finish-bounded",
        },
    )

    assert result["schema_version"] == (
        "runtime_context.finish_gate.compact_response.v1"
    )
    assert result["bounded_response"] is True
    assert result["checkpoint_id"] == "ckpt-finish-bounded"
    assert result["request_id"] == "req-finish-bounded"
    assert result["writes_performed"] is True
    assert result["mutation_performed"] is True
    assert result["write_disposition"] == "written"
    assert result["safe_retry"] is False
    assert result["timeline_event"]["event_ref"] == "timeline:92"
    assert result["contract_runtime_canonical_line"][
        "execution_state_revision"
    ] == 22
    assert result["next_legal_action"]["line_id"] == (
        "qa_independent_verification"
    )
    assert len(json.dumps(result).encode()) < 64 * 1024
    assert "raw-finish-projection-must-not-escape" not in json.dumps(result)

    explicit_zero = mcp_tools._runtime_context_finish_gate_compact_result(
        {
            **raw_result,
            "ok": False,
            "zero_write_rejection": True,
            "writes_performed": False,
            "mutation_performed": False,
        }
    )
    assert explicit_zero["current_request_mutation_proven"] is False
    assert explicit_zero["writes_performed"] is False
    assert explicit_zero["mutation_performed"] is False
    assert explicit_zero["write_disposition"] == "not_written"


def test_mcp_stdio_oversize_fallback_preserves_explicit_durable_write_truth(
    monkeypatch,
):
    tool_result = {
        "ok": True,
        "request_id": "req-stdio-durable-write",
        "writes_performed": True,
        "mutation_performed": True,
        "safe_retry": False,
        "oversized": "x" * 8_000,
    }
    message = {
        "jsonrpc": "2.0",
        "id": 17,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(tool_result)}
            ]
        },
    }
    stdout = io.StringIO()
    monkeypatch.setattr(plugin_mcp_server.sys, "stdout", stdout)

    plugin_mcp_server._write(message, max_serialized_bytes=1_024)

    fallback = json.loads(stdout.getvalue())
    data = fallback["error"]["data"]
    assert fallback["error"]["message"] == "mcp_response_frame_too_large"
    assert data["writes_performed"] is True
    assert data["mutation_performed"] is True
    assert data["write_disposition"] == "written"
    assert data["safe_retry"] is False
    assert data["retry_same_world_allowed"] is False


def test_mcp_stdio_oversize_fallback_keeps_unknown_write_truth_ambiguous(
    monkeypatch,
):
    message = {
        "jsonrpc": "2.0",
        "id": 18,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"ok": True, "oversized_read": "x" * 8_000}
                    ),
                }
            ]
        },
    }
    stdout = io.StringIO()
    monkeypatch.setattr(plugin_mcp_server.sys, "stdout", stdout)

    plugin_mcp_server._write(message, max_serialized_bytes=1_024)

    data = json.loads(stdout.getvalue())["error"]["data"]
    assert data["write_disposition"] == "ambiguous"
    assert "writes_performed" not in data
    assert "mutation_performed" not in data
    assert data["safe_retry"] is False
    assert data["retry_same_world_allowed"] is False


def test_mcp_contract_runtime_submit_line_bounds_success_and_preserves_write_truth():
    raw_result = {
        "ok": True,
        "project_id": "aming-claw",
        "backlog_id": "AC-SUBMIT-LINE-BOUNDED",
        "contract_execution_id": "cex-submit-line-bounded",
        "contract_id": "mf_parallel.v2",
        "contract_revision_id": "rev9",
        "contract_hash": "sha256:" + ("a" * 64),
        "actor_role": "observer",
        "execution_state_revision": 3,
        "execution_state_hash": "sha256:" + ("b" * 64),
        "runtime_guide_hash": "sha256:" + ("c" * 64),
        "route_token_ref": "rtok-submit-line-bounded",
        "request_id": "req-submit-line-bounded",
        "decision": {
            "schema_version": "contract_write_gate_decision.v1",
            "ok": True,
            "decision": "allow",
        },
        "contract_runtime_current_state": {
            "schema_version": "contract_runtime.current_state.v1",
            "execution_state_revision": 3,
            "execution_state_hash": "sha256:" + ("b" * 64),
            "runtime_guide_hash": "sha256:" + ("c" * 64),
            "readiness_state": "in_progress",
            "raw_state": "raw-current-state-must-not-escape" * 20_000,
        },
        "runtime_guide": {
            "completed_lines": [
                {
                    "stage_id": "dispatch",
                    "line_id": "observer_dispatch_bounded_workers",
                    "line_instance_id": "dispatch:1",
                    "evidence_kind": "bounded_worker_dispatch",
                    "actor_role": "observer",
                    "payload": {
                        "raw_body": "raw-completed-line-must-not-escape" * 20_000,
                    },
                }
            ],
            "instructions": "raw-guide-must-not-escape" * 20_000,
        },
        "next_legal_action": {
            "schema_version": "contract_runtime_next_legal_action.v1",
            "id": "worker_read_runtime_guide",
            "action": "record_read_receipt",
            "stage_id": "worker_read",
            "line_id": "worker_read_runtime_guide",
            "evidence_kind": "read_receipt",
            "owner_role": "mf_sub",
            "runtime_context_id": "mfrctx-submit-line-bounded",
            "task_id": "worker-submit-line-bounded",
            "parent_task_id": "cex-submit-line-bounded",
            "raw_bridge": "raw-bridge-must-not-escape" * 20_000,
        },
        "contract_runtime_dispatch_timeline_event": {
            "id": 90,
            "event_ref": "timeline:90",
            "status": "recorded",
            "event_kind": "bounded_implementation_worker_dispatch",
            "task_id": "worker-submit-line-bounded",
        },
    }

    class LargeSubmitRecorder(_Recorder):
        def api(self, method, path, data=None, **_kwargs):
            self.calls.append((method, path, data))
            return raw_result

    recorder = LargeSubmitRecorder()
    result = _dispatcher(recorder).dispatch(
        "contract_runtime_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-submit-line-bounded",
            "execution_state_revision": 2,
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "evidence_kind": "bounded_worker_dispatch",
        },
    )

    assert result["ok"] is True
    assert result["bounded_response"] is True
    assert result["response_view"] == "compact"
    assert result["execution_state_revision"] == 3
    assert result["writes_performed"] is True
    assert result["mutation_performed"] is True
    assert result["write_disposition"] == "written"
    assert result["mutation_authority"] == {
        "schema_version": "contract_runtime.submit_line.mutation_authority.v1",
        "request_execution_state_revision": 2,
        "response_execution_state_revision": 3,
        "execution_state_revision_advanced": True,
        "dispatch_event_recorded_now": True,
        "explicit_write_flag": False,
        "explicit_zero_write_flag": False,
    }
    assert result["next_legal_action"]["line_id"] == (
        "worker_read_runtime_guide"
    )
    assert result["last_completed_line"]["line_id"] == (
        "observer_dispatch_bounded_workers"
    )
    assert result["contract_runtime_dispatch_timeline_event"]["id"] == 90
    assert len(json.dumps(result).encode()) < 64 * 1024
    serialized = json.dumps(result, sort_keys=True)
    assert "raw-completed-line-must-not-escape" not in serialized
    assert "raw-current-state-must-not-escape" not in serialized
    assert "raw-guide-must-not-escape" not in serialized
    assert "raw-bridge-must-not-escape" not in serialized


def test_mcp_contract_runtime_submit_line_keeps_small_shape_and_large_rejection_zero_write():
    small = {
        "ok": True,
        "execution_state_revision": 3,
        "completed_line": {"line_id": "small"},
    }
    assert mcp_tools._contract_runtime_submit_line_compact_result(
        small,
        request_execution_state_revision=2,
    ) is small

    rejected = {
        "ok": False,
        "error": "contract_runtime_line_rejected",
        "zero_write_rejection": True,
        "writes_performed": False,
        "mutation_performed": False,
        "execution_state_revision": 2,
        "decision": {
            "ok": False,
            "decision": "block",
            "errors": ["line identity mismatch"],
        },
        "runtime_guide": {
            "completed_lines": [],
            "oversized": "rejected-body" * 30_000,
        },
    }
    compact = mcp_tools._contract_runtime_submit_line_compact_result(
        rejected,
        request_execution_state_revision=2,
    )
    assert compact["ok"] is False
    assert compact["writes_performed"] is False
    assert compact["mutation_performed"] is False
    assert compact["current_request_mutation_proven"] is False
    assert compact["write_disposition"] == "not_written"
    assert compact["safe_retry"] is True
    assert compact["decision"]["errors"] == ["line identity mismatch"]


def test_mcp_contract_runtime_precheck_line_response_view_is_local_bounded_and_truthful():
    raw_result = {
        "ok": False,
        "status": "rejected",
        "error": "contract_runtime_line_rejected",
        "project_id": "aming-claw",
        "backlog_id": "AC-PRECHECK-BOUNDED",
        "contract_execution_id": "cex-precheck-bounded",
        "execution_state_revision": 3,
        "runtime_guide_hash": "sha256:guide",
        "would_mutate_completed_lines": False,
        "decision": {
            "ok": False,
            "decision": "block",
            "errors": ["dispatch identity mismatch"],
            "field_mismatches": [
                {
                    "field": "runtime_context_id",
                    "expected": "mfrctx-canonical",
                    "actual": "mfrctx-wrong",
                },
                {
                    "field": "session_token",
                    "expected": "raw-expected-session-must-not-escape",
                    "actual": "raw-actual-session-must-not-escape",
                },
            ],
        },
        "next_legal_action": {
            "action": "observer_dispatch_bounded_workers",
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "runtime_context_id": "mfrctx-canonical",
            "task_id": "worker-precheck",
            "route_token_ref": "rtok-copy-safe",
            "mf_sub_host_bridge_guidance": {
                "raw_envelope": "raw-host-envelope-must-not-escape" * 40_000,
            },
        },
        "runtime_guide": {
            "completed_lines": [
                {
                    "payload": {
                        "session_token": "raw-session-must-not-escape",
                    }
                }
            ],
            "oversized": "oversized-runtime-guide" * 40_000,
        },
    }

    class PrecheckRecorder(_Recorder):
        def api(self, method, path, data=None, **_kwargs):
            self.calls.append((method, path, data))
            return raw_result

    recorder = PrecheckRecorder()
    dispatcher = _dispatcher(recorder)
    common = {
        "project_id": "aming-claw",
        "contract_execution_id": "cex-precheck-bounded",
        "execution_state_revision": 3,
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "evidence_kind": "bounded_worker_dispatch",
    }

    compact = dispatcher.dispatch(
        "contract_runtime_precheck_line",
        dict(common),
    )
    assert compact["schema_version"] == (
        "contract_runtime.precheck_line.compact_response.v1"
    )
    assert compact["ok"] is False
    assert compact["decision"]["errors"] == ["dispatch identity mismatch"]
    assert compact["decision"]["field_mismatches"] == [
        {
            "field": "runtime_context_id",
            "expected": "mfrctx-canonical",
            "actual": "mfrctx-wrong",
        },
        {"field": "session_token"},
    ]
    assert compact["next_legal_action"]["line_id"] == (
        "observer_dispatch_bounded_workers"
    )
    assert compact["writes_performed"] is False
    assert compact["mutation_performed"] is False
    assert compact["write_disposition"] == "not_written"
    assert compact["http_request_performed"] is True
    assert len(json.dumps(compact).encode()) < 64 * 1024
    serialized = json.dumps(compact, sort_keys=True)
    assert "raw-host-envelope-must-not-escape" not in serialized
    assert "raw-session-must-not-escape" not in serialized
    assert "raw-expected-session-must-not-escape" not in serialized
    assert "raw-actual-session-must-not-escape" not in serialized
    assert "oversized-runtime-guide" not in serialized
    assert "response_view" not in recorder.calls[-1][2]

    full = dispatcher.dispatch(
        "contract_runtime_precheck_line",
        {**common, "response_view": "full"},
    )
    assert full["error"] == "contract_runtime_precheck_line_full_response_too_large"
    assert full["precheck_ok"] is False
    assert full["compact_result"]["decision"]["errors"] == [
        "dispatch identity mismatch"
    ]
    assert len(json.dumps(full).encode()) < 64 * 1024
    assert "response_view" not in recorder.calls[-1][2]

    call_count = len(recorder.calls)
    invalid = dispatcher.dispatch(
        "contract_runtime_precheck_line",
        {**common, "response_view": "verbose"},
    )
    assert invalid == {
        "ok": False,
        "error": "contract_runtime_precheck_line_response_view_invalid",
        "message": "response_view must be compact or full",
        "response_view": "verbose",
        "http_request_performed": False,
        "zero_write_rejection": True,
        "writes_performed": False,
        "mutation_performed": False,
    }
    assert len(recorder.calls) == call_count

    small = {"ok": True, "decision": {"ok": True}, "value": "small"}
    assert mcp_tools._contract_runtime_precheck_line_compact_result(
        small,
        response_view="full",
    ) is small
    unsafe_small = {
        "ok": True,
        "decision": {"ok": True},
        "session_token": "raw-small-session-must-not-escape",
    }
    unsafe_full = mcp_tools._contract_runtime_precheck_line_compact_result(
        unsafe_small,
        response_view="full",
    )
    assert unsafe_full["error"] == (
        "contract_runtime_precheck_line_full_response_unsafe"
    )
    assert unsafe_full["unsafe_full_shape_detected"] is True
    assert "raw-small-session-must-not-escape" not in json.dumps(unsafe_full)


def test_mcp_graph_current_full_reconcile_uses_reconcile_timeout(monkeypatch):
    monkeypatch.delenv("AMING_GRAPH_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AMING_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    recorder = _TimeoutAwareGovRecorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        service_mgr=None,
        workspace=".",
    )

    result = dispatcher.dispatch(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "run_id": "current-full-run-1",
            "timeout_seconds": 1200,
        },
    )

    assert result["ok"] is True
    assert recorder.calls == [
        (
            "POST",
            "http://governance.test/api/graph-governance/aming-claw/reconcile/current-full",
            {"run_id": "current-full-run-1"},
            1200,
        )
    ]


def test_mcp_graph_current_full_reconcile_default_timeout_is_long(monkeypatch):
    monkeypatch.delenv("AMING_GRAPH_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AMING_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    recorder = _TimeoutAwareGovRecorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        service_mgr=None,
        workspace=".",
    )

    dispatcher.dispatch(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "run_id": "current-full-run-1",
        },
    )

    assert recorder.calls[0][3] == 900


def test_mcp_graph_current_full_reconcile_timeout_returns_poll_guidance(monkeypatch):
    monkeypatch.delenv("AMING_GRAPH_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AMING_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    recorder = _TimeoutAwareGovRecorder(timeout_current_full=True)
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        service_mgr=None,
        workspace=".",
    )

    result = dispatcher.dispatch(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "run_id": "current-full-run-1",
        },
    )

    assert result["ok"] is False
    assert result["error"] == "reconcile_timeout"
    assert result["run_id"] == "current-full-run-1"
    assert result["status"] == "running"
    assert result["progress"] == {"done": 3, "total": 10}
    assert result["next_legal_action"]["poll_tool"] == "graph_operations_queue"
    assert result["next_legal_action"]["retry_tool"] == "graph_current_full_reconcile"
    assert result["next_legal_action"]["safe_retry"] is False
    assert result["next_legal_action"]["same_run_id_required"] is True
    assert result["next_legal_action"]["retry_disposition"] == (
        "conditional_same_run_id_resume"
    )
    assert recorder.calls[0][3] == 900
    assert recorder.calls[1] == (
        "GET",
        "http://governance.test/api/graph-governance/aming-claw/operations/queue"
        "?include_status_observations=true&include_resolved=false",
        None,
        10,
    )


def test_mcp_current_full_timeout_generates_recoverable_run_id(monkeypatch):
    monkeypatch.setattr(mcp_tools.secrets, "token_hex", lambda _size: "generatedrunid01")
    recorder = _TimeoutAwareGovRecorder(timeout_current_full=True)
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        service_mgr=None,
        workspace=".",
    )

    result = dispatcher.dispatch(
        "graph_current_full_reconcile",
        {"project_id": "aming-claw", "target_commit_sha": "a" * 40},
    )

    generated = "current-full-mcp-generatedrunid01"
    assert result["run_id"] == generated
    assert result["run_id_available"] is True
    assert recorder.calls[0][2]["run_id"] == generated


def test_governance_mcp_graph_current_full_reconcile_uses_reconcile_timeout(monkeypatch):
    monkeypatch.delenv("AMING_GRAPH_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AMING_RECONCILE_MCP_TIMEOUT_SECONDS", raising=False)
    calls = []

    def fake_http(
        method: str,
        path: str,
        body: dict | None = None,
        *,
        gov_token: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        calls.append((method, path, body, gov_token, timeout_seconds))
        return {"ok": True}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    governance_mcp_server._dispatch_tool(
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "run_id": "current-full-run-1",
            "timeout_seconds": 1200,
        },
    )

    assert calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/reconcile/current-full",
            {"run_id": "current-full-run-1"},
            None,
            1200,
        )
    ]


def test_governance_mcp_current_full_timeout_requires_poll_then_same_run_resume():
    result = governance_mcp_server._current_full_reconcile_timeout_response(
        "aming-claw",
        {"run_id": "current-full-governance-timeout"},
        timeout_seconds=900,
        timeout_result={"error": "request_timeout"},
        progress={"status": "running", "progress": {"done": 0, "total": 2}},
    )

    assert result["error"] == "reconcile_timeout"
    assert result["run_id"] == "current-full-governance-timeout"
    assert result["next_legal_action"]["safe_retry"] is False
    assert result["next_legal_action"]["same_run_id_required"] is True
    assert result["next_legal_action"]["action"] == (
        "poll_graph_operations_queue_then_resume_same_run_id"
    )


def test_governance_mcp_current_full_generates_run_id_before_dispatch(monkeypatch):
    monkeypatch.setattr(
        governance_mcp_server.secrets,
        "token_hex",
        lambda _size: "governanceid001",
    )
    calls = []

    def fake_http(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"ok": True}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)

    governance_mcp_server._dispatch_tool(
        "graph_current_full_reconcile",
        {"project_id": "aming-claw", "target_commit_sha": "a" * 40},
    )

    assert calls[0][2]["run_id"] == "current-full-mcp-governanceid001"


def test_current_full_progress_matches_exact_run_id_not_prefix():
    queue = {
        "operations": [
            {
                "operation_id": "current-full:run-10",
                "run_id": "run-10",
                "status": "failed",
            },
            {
                "operation_id": "current-full:run-1",
                "run_id": "run-1",
                "status": "candidate_ready",
            },
        ]
    }

    for summarize in (
        mcp_tools._summarize_reconcile_progress,
        governance_mcp_server._summarize_reconcile_progress,
    ):
        result = summarize(queue, "run-1")
        assert result["operation_id"] == "current-full:run-1"
        assert result["status"] == "candidate_ready"


def test_current_full_progress_does_not_fall_back_to_sole_unrelated_operation():
    queue = {
        "operations": [
            {
                "operation_id": "current-full:unrelated",
                "run_id": "unrelated",
                "status": "complete",
                "progress": {"done": 2, "total": 2},
            }
        ],
        "count": 1,
    }

    for summarize in (
        mcp_tools._summarize_reconcile_progress,
        governance_mcp_server._summarize_reconcile_progress,
    ):
        result = summarize(queue, "wanted-run")
        assert result["available"] is False
        assert result["status"] == "unknown"
        assert result["operation_count"] == 1
        assert "operation_id" not in result


def test_current_full_progress_with_empty_queue_is_unknown():
    queue = {"operations": [], "count": 0}

    for summarize in (
        mcp_tools._summarize_reconcile_progress,
        governance_mcp_server._summarize_reconcile_progress,
    ):
        result = summarize(queue, "wanted-run")
        assert result["available"] is False
        assert result["status"] == "unknown"
        assert result["progress"] == {}
        assert result["operation_count"] == 0
        assert "operation_id" not in result


def test_current_full_progress_retains_sole_operation_fallback_without_run_id():
    queue = {
        "operations": [
            {
                "operation_id": "current-full:legacy-run",
                "run_id": "legacy-run",
                "status": "running",
                "progress": {"done": 0, "total": 2},
            }
        ]
    }

    for summarize in (
        mcp_tools._summarize_reconcile_progress,
        governance_mcp_server._summarize_reconcile_progress,
    ):
        result = summarize(queue, "")
        assert result["available"] is True
        assert result["operation_id"] == "current-full:legacy-run"
        assert result["status"] == "running"


def test_mcp_observer_hotfix_enter_schema_exposes_observer_route_refs():
    hotfix = next(tool for tool in TOOLS if tool.get("name") == "observer_hotfix_enter")
    props = hotfix["inputSchema"]["properties"]

    assert props["observer_session_id"]["type"] == "string"
    assert props["observer_session_token_ref"]["type"] == "string"
    assert props["observer_route_token_ref"]["type"] == "string"
    assert "raw route tokens are not accepted" in props["observer_route_token_ref"][
        "description"
    ]


def test_mcp_observer_command_schemas_accept_managed_session_refs():
    auth_any_of = [
        {"required": ["session_token"]},
        {"required": ["observer_session_token_ref"]},
    ]
    required_by_name = {
        "observer_command_next": {"project_id", "session_id"},
        "observer_command_claim": {"project_id", "session_id"},
        "observer_command_takeover": {
            "project_id",
            "session_id",
            "command_id",
            "reason",
        },
        "observer_command_complete": {"project_id", "session_id", "command_id"},
        "observer_command_fail": {"project_id", "session_id", "command_id"},
    }

    for name, expected_required in required_by_name.items():
        schema = next(tool for tool in TOOLS if tool.get("name") == name)[
            "inputSchema"
        ]
        assert schema["properties"]["observer_session_token_ref"]["type"] == "string"
        assert set(schema["required"]) == expected_required
        assert "session_token" not in schema["required"]
        assert schema["anyOf"] == auth_any_of


def test_mcp_managed_observer_session_ref_heartbeats_and_strips_hotfix_auth():
    raw_token = "observer-secret-must-stay-process-local"

    class Recorder:
        def __init__(self):
            self.calls = []

        def __call__(self, method, path, body=None):
            self.calls.append((method, path, body))
            if path.endswith("/observer-sessions/register"):
                return {
                    "ok": True,
                    "session_id": "obs-managed-hotfix",
                    "session_token": raw_token,
                }
            return {"ok": True}

    recorder = Recorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder,
        worker_pool=None,
        manager_api_fn=recorder,
        workspace="/repo",
    )
    registered = dispatcher.dispatch(
        "observer_session_register",
        {
            "project_id": "aming-claw",
            "observer_kind": "codex",
            "session_label": "managed-hotfix",
        },
    )
    token_ref = registered["observer_session_token_ref"]
    assert token_ref.startswith("observer-session-ref-")
    assert registered["raw_observer_session_token_exposed"] is False
    assert raw_token not in json.dumps(registered, sort_keys=True)

    result = dispatcher.dispatch(
        "observer_hotfix_enter",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-MANAGED-HOTFIX",
            "task_id": "managed-hotfix-attempt-2",
            "parent_contract_execution_id": "cex-parent",
            "predecessor_contract_execution_id": "cex-predecessor",
            "predecessor_execution_state_revision": 5,
            "predecessor_execution_state_hash": "sha256:" + "a" * 64,
            "successor_attempt_id": "attempt-2",
            "actor": "operator",
            "reason": "start a bounded sibling repair",
            "route_token_ref": "rtok-managed-hotfix",
            "observer_session_id": "obs-managed-hotfix",
            "observer_session_token_ref": token_ref,
        },
    )
    assert result["ok"] is True
    assert recorder.calls[1] == (
        "POST",
        "/api/projects/aming-claw/observer-sessions/obs-managed-hotfix/heartbeat",
        {"session_token": raw_token},
    )
    method, path, body = recorder.calls[2]
    assert method == "POST"
    assert path == (
        "/api/projects/aming-claw/hotfix/enter?"
        "observer_session_id=obs-managed-hotfix"
    )
    assert set(body) == {
        "backlog_id",
        "task_id",
        "parent_contract_execution_id",
        "predecessor_contract_execution_id",
        "predecessor_execution_state_revision",
        "predecessor_execution_state_hash",
        "successor_attempt_id",
        "actor",
        "reason",
        "route_token_ref",
    }
    serialized = json.dumps(recorder.calls[2], sort_keys=True)
    assert token_ref not in serialized
    assert raw_token not in serialized

    wrong_scope = dispatcher.dispatch(
        "observer_hotfix_enter",
        {
            "project_id": "other-project",
            "observer_session_token_ref": token_ref,
            "reason": "must fail closed",
        },
    )
    assert wrong_scope["error"] == "observer_session_token_ref_scope_mismatch"

    closed = dispatcher.dispatch(
        "observer_session_close",
        {
            "project_id": "aming-claw",
            "session_id": "obs-managed-hotfix",
            "observer_session_token_ref": token_ref,
        },
    )
    assert closed["ok"] is True
    assert recorder.calls[-1] == (
        "POST",
        "/api/projects/aming-claw/observer-sessions/obs-managed-hotfix/close",
        {"session_token": raw_token},
    )
    stale = dispatcher.dispatch(
        "observer_session_heartbeat",
        {
            "project_id": "aming-claw",
            "session_id": "obs-managed-hotfix",
            "observer_session_token_ref": token_ref,
        },
    )
    assert stale["error"] == "observer_session_token_ref_unknown"


def test_mcp_observer_session_register_exposes_and_forwards_dev_route_authority():
    props = _tool_properties("observer_session_register")
    assert {
        "route_token_ref",
        "backlog_id",
        "task_id",
        "cex_id",
    }.issubset(props)

    class Recorder:
        def __init__(self):
            self.calls = []

        def __call__(self, method, path, body=None):
            self.calls.append((method, path, body))
            return {
                "ok": True,
                "session_id": "obs-route-bound",
                "session_token": "observer-secret",
            }

    recorder = Recorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder,
        worker_pool=None,
        manager_api_fn=recorder,
        workspace="/repo",
    )
    result = dispatcher.dispatch(
        "observer_session_register",
        {
            "project_id": "aming-claw",
            "route_token_ref": "rtok-direct",
            "backlog_id": "AC-DIRECT",
            "task_id": "cex-direct",
            "cex_id": "cex-direct",
            "observer_kind": "codex",
            "session_label": "fresh-route-bound-observer",
        },
    )

    assert result["session_id"] == "obs-route-bound"
    assert recorder.calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/register",
            {
                "route_token_ref": "rtok-direct",
                "backlog_id": "AC-DIRECT",
                "task_id": "cex-direct",
                "cex_id": "cex-direct",
                "observer_kind": "codex",
                "session_label": "fresh-route-bound-observer",
            },
        )
    ]


def test_mcp_managed_observer_session_ref_routes_observer_commands():
    raw_token = "observer-command-secret-must-stay-process-local"

    class Recorder:
        def __init__(self):
            self.calls = []

        def __call__(self, method, path, body=None):
            self.calls.append((method, path, body))
            if path.endswith("/observer-sessions/register"):
                return {
                    "ok": True,
                    "session_id": "obs-managed-command",
                    "session_token": raw_token,
                }
            return {"ok": True}

    recorder = Recorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder,
        worker_pool=None,
        manager_api_fn=recorder,
        workspace="/repo",
    )
    registered = dispatcher.dispatch(
        "observer_session_register",
        {
            "project_id": "aming-claw",
            "observer_kind": "codex",
            "session_label": "managed-command",
        },
    )
    token_ref = registered["observer_session_token_ref"]
    common = {
        "project_id": "aming-claw",
        "session_id": "obs-managed-command",
        "observer_session_token_ref": token_ref,
    }

    results = [
        dispatcher.dispatch("observer_command_next", common),
        dispatcher.dispatch(
            "observer_command_claim", {**common, "command_id": "cmd-claim"}
        ),
        dispatcher.dispatch(
            "observer_command_takeover",
            {**common, "command_id": "cmd-stale", "reason": "bounded takeover"},
        ),
        dispatcher.dispatch(
            "observer_command_complete",
            {**common, "command_id": "cmd-complete", "result": {"ok": True}},
        ),
        dispatcher.dispatch(
            "observer_command_fail",
            {**common, "command_id": "cmd-fail", "error": "bounded failure"},
        ),
    ]

    assert recorder.calls[1:] == [
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/next",
            {"session_id": "obs-managed-command", "session_token": raw_token},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/claim",
            {
                "session_id": "obs-managed-command",
                "session_token": raw_token,
                "command_id": "cmd-claim",
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-stale/takeover",
            {
                "session_id": "obs-managed-command",
                "session_token": raw_token,
                "reason": "bounded takeover",
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-complete/complete",
            {
                "session_id": "obs-managed-command",
                "session_token": raw_token,
                "result": {"ok": True},
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-fail/fail",
            {
                "session_id": "obs-managed-command",
                "session_token": raw_token,
                "error": "bounded failure",
            },
        ),
    ]
    for result in results:
        serialized = json.dumps(result, sort_keys=True)
        assert raw_token not in serialized
        assert token_ref not in serialized

    call_count = len(recorder.calls)
    ambiguous = dispatcher.dispatch(
        "observer_command_next", {**common, "session_token": raw_token}
    )
    assert ambiguous["error"] == "observer_session_auth_ambiguous"
    assert len(recorder.calls) == call_count


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
        "task_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
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
        "task_id": "worker-task",
        "route_id": "route-test",
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-test",
        "prompt_contract_hash": "sha256:prompt-contract",
        "route_token_ref": "rtok-test",
        "visible_injection_manifest_hash": "sha256:visible-manifest",
        "view": "worker_view",
        "graph_trace_id": "gqt-test",
        "session_token": "session-test",
        "target_project_root": "/repo/fixture",
    }

    dispatcher.dispatch("runtime_context_current", args)
    dispatcher.dispatch("runtime_context_worker_guide", args)

    query = (
        "fence_token=fence-test&parent_task_id=AC-PARENT&task_id=worker-task&"
        "route_id=route-test&route_context_hash=sha256%3Aroute-context&"
        "prompt_contract_id=rprompt-test&prompt_contract_hash=sha256%3Aprompt-contract&"
        "route_token_ref=rtok-test&"
        "visible_injection_manifest_hash=sha256%3Avisible-manifest&view=worker_view&"
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


def test_runtime_context_worker_guide_adapter_schemas_preserve_safe_route_identity():
    expected = {
        "task_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
    }

    for registry in (TOOLS, governance_mcp_server.TOOLS):
        tool = next(
            item for item in registry
            if item["name"] == "runtime_context_worker_guide"
        )
        properties = tool["inputSchema"]["properties"]
        assert expected.issubset(properties)
        assert "route_token" not in properties
        assert "session_token_ref" in properties
        assert {"compact", "all", "full"}.issubset(
            properties["view"]["enum"]
        )


def test_worker_guide_managed_adapters_default_compact_and_preserve_explicit_view(
    monkeypatch,
):
    args = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-compact-default",
        "__aming_managed_host_envelope_continuity_bypass": True,
    }
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    assert dispatcher.dispatch("runtime_context_worker_guide", args)["ok"] is True
    default_query = __import__(
        "urllib.parse", fromlist=["parse_qs", "urlparse"]
    ).parse_qs(
        __import__("urllib.parse", fromlist=["urlparse"]).urlparse(
            recorder.calls[-1][1]
        ).query
    )
    assert default_query["view"] == ["compact"]

    dispatcher.dispatch(
        "runtime_context_worker_guide",
        {**args, "view": "all"},
    )
    explicit_query = __import__(
        "urllib.parse", fromlist=["parse_qs", "urlparse"]
    ).parse_qs(
        __import__("urllib.parse", fromlist=["urlparse"]).urlparse(
            recorder.calls[-1][1]
        ).query
    )
    assert explicit_query["view"] == ["all"]

    mirror_calls = []

    def fake_http(method, path, data=None, **_kwargs):
        mirror_calls.append((method, path, data))
        return {"ok": True, "response_view": "compact"}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    mirrored = governance_mcp_server._dispatch_tool(
        "runtime_context_worker_guide",
        dict(args),
    )
    assert mirrored["ok"] is True
    mirror_query = __import__(
        "urllib.parse", fromlist=["parse_qs", "urlparse"]
    ).parse_qs(
        __import__("urllib.parse", fromlist=["urlparse"]).urlparse(
            mirror_calls[-1][1]
        ).query
    )
    assert mirror_query["view"] == ["compact"]


def test_worker_guide_managed_adapter_projects_current_authority_with_parity():
    oversized = {
        "ok": True,
        "schema_version": "runtime_context.worker_guide_response.v1",
        "response_view": "all",
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-post-startup",
        "task_id": "worker-post-startup",
        "next_legal_action": "run_graph_query",
        "graph_query_identity": {
            "runtime_context_id": "mfrctx-post-startup",
            "task_id": "worker-post-startup",
            "parent_task_id": "cex-post-startup",
            "target_project_root": "/repo/worker",
            "route_id": "route-post-startup",
            "route_context_hash": "sha256:route-post-startup",
            "prompt_contract_id": "rprompt-post-startup",
            "prompt_contract_hash": "sha256:prompt-post-startup",
            "route_token_ref": "rtok-post-startup",
        },
        "canonical_executable_actions": {
            "graph": {
                "mcp_tool": "graph_query",
                "copy_safe_body": {
                    "project_id": "aming-claw",
                    "runtime_context_id": "mfrctx-post-startup",
                    "task_id": "worker-post-startup",
                    "query_source": "mf_subagent",
                },
            }
        },
        "session_token": "raw-worker-session-must-not-escape",
        "fence_token": "raw-worker-fence-must-not-escape",
        "recursive_lifecycle_diagnostics": "x"
        * (mcp_tools._WORKER_GUIDE_MANAGED_MAX_SERIALIZED_BYTES + 1),
    }
    primary = mcp_tools._bounded_worker_guide_result(oversized)
    mirror = governance_mcp_server._bounded_worker_guide_result(oversized)

    assert primary == mirror
    assert primary["ok"] is True
    assert primary["schema_version"] == (
        "runtime_context.worker_guide_managed_compact.v1"
    )
    assert primary["next_legal_action"] == "run_graph_query"
    assert primary["graph_query_identity"]["route_id"] == (
        "route-post-startup"
    )
    assert primary["canonical_executable_action"]["mcp_tool"] == (
        "graph_query"
    )
    assert primary["semantic_truncation_performed"] is False
    assert primary["writes_performed"] is False
    assert len(json.dumps(primary).encode()) < 64 * 1024
    assert "recursive_lifecycle_diagnostics" not in primary
    assert "raw-worker-session-must-not-escape" not in json.dumps(primary)
    assert "raw-worker-fence-must-not-escape" not in json.dumps(primary)

    explicit_full = mcp_tools._bounded_worker_guide_result(
        oversized,
        requested_view="all",
    )
    assert explicit_full["error"] == (
        "runtime_context_worker_guide_full_response_too_large"
    )
    assert explicit_full["writes_performed"] is False
    assert explicit_full["semantic_truncation_performed"] is False


def test_post_startup_current_and_graph_query_are_bounded_with_truthful_authority():
    huge = "x" * (300 * 1024)
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        if path.endswith("/current-state?view=compact"):
            return {
                "ok": True,
                "project_id": "aming-claw",
                "runtime_context_id": "mfrctx-post-startup",
                "task_id": "worker-post-startup",
                "runtime_context_status": "running",
                "runtime_context_service": {
                    "schema_version": "runtime_context.service.v1",
                    "project_id": "aming-claw",
                    "runtime_context_id": "mfrctx-post-startup",
                    "content_address": {
                        "projection_hash": "sha256:projection-post-startup",
                        "projection_watermark": "timeline:136",
                    },
                    "views": {
                        "worker_view": {
                            "graph_query_identity": {
                                "runtime_context_id": "mfrctx-post-startup",
                                "task_id": "worker-post-startup",
                                "route_id": "route-post-startup",
                                "target_project_root": "/repo/worker",
                            },
                            "recursive_lifecycle_diagnostics": huge,
                        }
                    },
                },
                "executable_contract": {"recursive": huge},
            }
        return {
            "ok": True,
            "trace_id": "gqt-post-startup",
            "tool": "find_node_by_path",
            "result": {
                "ok": True,
                "nodes": [{"id": "L7.reminders", "path": "src/reminders.js"}],
            },
            "result_count": 1,
            "trace": {
                "trace_id": "gqt-post-startup",
                "status": "complete",
                "recursive_runtime_projection": huge,
            },
            "graph_query_identity": {
                "runtime_context_id": "mfrctx-post-startup",
                "task_id": "worker-post-startup",
                "parent_task_id": "cex-post-startup",
                "route_id": "route-post-startup",
                "target_project_root": "/repo/worker",
                "recursive_runtime_projection": huge,
            },
            "mf_sub_graph_query_canonical_gate": {
                "ok": True,
                "status": "passed",
                "trace_id": "gqt-post-startup",
                "recursive_runtime_projection": huge,
            },
            "contract_runtime_canonical_line": {
                "accepted": True,
                "status": "accepted",
                "contract_execution_id": "cex-post-startup",
                "execution_state_revision": 7,
                "recursive_runtime_projection": huge,
            },
            "session_token": "raw-graph-session-must-not-escape",
            "fence_token": "raw-graph-fence-must-not-escape",
        }

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=".",
    )
    current = dispatcher.dispatch(
        "runtime_context_current",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-post-startup",
        },
    )
    graph = dispatcher.dispatch(
        "graph_query",
        {
            "project_id": "aming-claw",
            "runtime_context_id": "mfrctx-post-startup",
            "task_id": "worker-post-startup",
            "parent_task_id": "cex-post-startup",
            "query_source": "mf_subagent",
            "query_purpose": "subagent_context_build",
            "worker_role": "mf_sub",
            "tool": "find_node_by_path",
            "args": {"path": "src/reminders.js"},
        },
    )

    assert current["schema_version"] == (
        "runtime_context.current_state_managed_compact.v1"
    )
    assert current["runtime_context_service"]["content_address"][
        "projection_hash"
    ] == "sha256:projection-post-startup"
    assert len(json.dumps(current).encode()) < 64 * 1024
    assert graph["schema_version"] == "graph_query.mf_sub_managed_compact.v1"
    assert graph["result"]["nodes"][0]["path"] == "src/reminders.js"
    assert graph["trace_id"] == "gqt-post-startup"
    assert graph["contract_runtime_canonical_line"]["accepted"] is True
    assert graph["writes_performed"] is True
    assert graph["product_mutation_performed"] is False
    assert graph["semantic_truncation_performed"] is False
    assert len(json.dumps(graph).encode()) < 64 * 1024
    assert "raw-graph-session-must-not-escape" not in json.dumps(graph)
    assert "raw-graph-fence-must-not-escape" not in json.dumps(graph)
    assert calls[0][1].endswith("/current-state?view=compact")

    full_current = mcp_tools._bounded_runtime_context_current_result(
        {
            "ok": True,
            "runtime_context_service": {
                "content_address": {
                    "projection_hash": "sha256:projection-post-startup"
                },
                "views": {"worker_view": {"recursive": huge}},
            },
        },
        requested_view="full",
    )
    assert full_current["error"] == (
        "runtime_context_current_full_response_too_large"
    )
    assert full_current["writes_performed"] is False


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


def test_backlog_audit_archive_adapters_preserve_canonical_guide_body_and_route_proof(
    monkeypatch,
):
    canonical_body = {
        "project_id": "aming-claw",
        "bug_id": "BUG/ARCHIVE",
        "commit": "abc1234",
        "reason": "Irreversible runtime recovery is exhausted.",
        "qa_acceptance": {
            "passed": False,
            "used_as_pass": False,
            "overall_release_pass_claimed": False,
        },
        "audit_close_gate": {
            "allowed": True,
            "normal_close_gate": {"can_close": False},
        },
        "route_token_ref": "rtok-copy-safe-archive",
    }
    expected_call = (
        "POST",
        "/api/backlog/aming-claw/BUG%2FARCHIVE/audit-archive",
        canonical_body,
    )

    recorder = _Recorder()
    primary = _dispatcher(recorder).dispatch(
        "backlog_audit_archive",
        dict(canonical_body),
    )

    mirror_calls = []

    def fake_http(method, path, data=None, **_kwargs):
        mirror_calls.append((method, path, data))
        return {"ok": True, "method": method, "path": path, "data": data}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    mirror = governance_mcp_server._dispatch_tool(
        "backlog_audit_archive",
        dict(canonical_body),
    )

    assert recorder.calls == [expected_call]
    assert mirror_calls == [expected_call]
    assert primary["data"] == canonical_body
    assert mirror["data"] == canonical_body
    assert canonical_body["route_token_ref"] == "rtok-copy-safe-archive"


def test_backlog_audit_archive_adapters_preserve_full_signed_waived_only_body(
    monkeypatch,
):
    canonical_body = {
        "project_id": "daily-planner-lite-20260816180518-8858affd",
        "bug_id": "AC-DEMO-R4-BATCH-FOCUS-8858AFFD",
        "commit": "5be0f67b9469a54cd4afaa5595b84f58ec4f8c68",
        "reason": "The accepted runtime recovery lineage is irreversibly exhausted.",
        "timeline_precheck": {
            "can_close": False,
            "missing_event_kinds": ["route_action_precheck"],
            "failed_gates": ["route_context_gate"],
        },
        "failure_audit": {
            "schema_version": "mf_batch_irreversible_runtime_audit.v1",
            "what_happened": "The one-time post-read safe-ref reissue was consumed.",
            "non_reconstructable_evidence_reason": (
                "No canonical startup can be reconstructed after the accepted receipt."
            ),
            "terminal_authority": {
                "status": "runtime_recovery_exhausted",
                "waived_only": True,
            },
        },
        "qa_acceptance": {
            "passed": False,
            "status": "failed",
            "targeted_scope_only": True,
            "used_as_pass": False,
            "overall_release_pass_claimed": False,
            "full_suite_claim": "not_claimed",
            "reviewer": "qa:runtime-audit-terminal-authority",
            "reviewer_role": "qa",
            "tests": ["runtime recovery exhaustion selector"],
            "evidence_refs": ["timeline:54", "timeline:55"],
        },
        "audit_close_gate": {
            "allowed": True,
            "passed": True,
            "normal_close_gate": {
                "can_close": False,
                "close_ready": False,
            },
            "terminal_disposition": "WAIVED",
        },
        "verification": {
            "current_lineage_unique": True,
            "archive_body_signed": True,
            "historical_evidence_reconstructed": False,
        },
        "route_token_ref": "rtok-copy-safe-focus-archive",
    }
    original_body_json = json.dumps(canonical_body, sort_keys=True, separators=(",", ":"))
    expected_call = (
        "POST",
        (
            "/api/backlog/daily-planner-lite-20260816180518-8858affd/"
            "AC-DEMO-R4-BATCH-FOCUS-8858AFFD/audit-archive"
        ),
        canonical_body,
    )

    recorder = _Recorder()
    primary = _dispatcher(recorder).dispatch(
        "backlog_audit_archive",
        json.loads(original_body_json),
    )

    mirror_calls = []

    def fake_http(method, path, data=None, **_kwargs):
        mirror_calls.append((method, path, data))
        return {"ok": True, "method": method, "path": path, "data": data}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    mirror = governance_mcp_server._dispatch_tool(
        "backlog_audit_archive",
        json.loads(original_body_json),
    )

    assert recorder.calls == [expected_call]
    assert mirror_calls == [expected_call]
    assert json.dumps(primary["data"], sort_keys=True, separators=(",", ":")) == original_body_json
    assert json.dumps(mirror["data"], sort_keys=True, separators=(",", ":")) == original_body_json
    assert primary["data"]["qa_acceptance"]["passed"] is False
    assert mirror["data"]["audit_close_gate"]["terminal_disposition"] == "WAIVED"


def test_backlog_audit_archive_public_contract_keeps_routing_fields_in_body_and_path(
    monkeypatch,
):
    tool = next(tool for tool in TOOLS if tool.get("name") == "backlog_audit_archive")
    assert set(tool["inputSchema"]["required"]) >= {"project_id", "bug_id"}

    body = {
        "project_id": "aming-claw",
        "bug_id": "BUG/PARITY-WARRANTY-R2",
        "commit": "9cdee4b4cecc7a4e57556ca0a33cb8e84d5b3118",
        "reason": "Canonical signed archive-body parity warranty.",
        "qa_acceptance": {
            "passed": False,
            "used_as_pass": False,
            "overall_release_pass_claimed": False,
        },
        "audit_close_gate": {
            "allowed": True,
            "normal_close_gate": {"can_close": False},
            "terminal_disposition": "WAIVED",
        },
        "route_token_ref": "rtok-copy-safe-parity-warranty-r2",
    }
    expected_body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected_path = "/api/backlog/aming-claw/BUG%2FPARITY-WARRANTY-R2/audit-archive"

    recorder = _Recorder()
    primary = _dispatcher(recorder).dispatch(
        "backlog_audit_archive",
        json.loads(expected_body_json),
    )

    mirror_calls = []

    def fake_http(method, path, data=None, **_kwargs):
        mirror_calls.append((method, path, data))
        return {"ok": True, "method": method, "path": path, "data": data}

    monkeypatch.setattr(governance_mcp_server, "_http", fake_http)
    mirror = governance_mcp_server._dispatch_tool(
        "backlog_audit_archive",
        json.loads(expected_body_json),
    )

    assert recorder.calls == [("POST", expected_path, body)]
    assert mirror_calls == [("POST", expected_path, body)]
    for result in (primary, mirror):
        assert result["data"]["project_id"] == "aming-claw"
        assert result["data"]["bug_id"] == "BUG/PARITY-WARRANTY-R2"
        assert json.dumps(result["data"], sort_keys=True, separators=(",", ":")) == (
            expected_body_json
        )


@pytest.mark.parametrize("missing_field", ["project_id", "bug_id"])
def test_backlog_audit_archive_adapters_require_routing_identity_before_http(
    monkeypatch,
    missing_field,
):
    body = {
        "project_id": "aming-claw",
        "bug_id": "BUG-ARCHIVE",
        "commit": "abc1234",
        "reason": "Irreversible runtime recovery is exhausted.",
        "route_token_ref": "rtok-copy-safe-archive",
    }
    body.pop(missing_field)

    recorder = _Recorder()
    with pytest.raises(KeyError, match=missing_field):
        _dispatcher(recorder).dispatch("backlog_audit_archive", dict(body))
    assert recorder.calls == []

    mirror_calls = []
    monkeypatch.setattr(
        governance_mcp_server,
        "_http",
        lambda *args, **kwargs: mirror_calls.append((args, kwargs)),
    )
    with pytest.raises(KeyError, match=missing_field):
        governance_mcp_server._dispatch_tool("backlog_audit_archive", dict(body))
    assert mirror_calls == []


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
            "include_compact_ledger": True,
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
            "/api/task/aming-claw/timeline?backlog_id=BUG-1&event_kind=implementation&limit=25&include_compact_ledger=true",
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
            "contract_execution_id": "cex-runtime-text",
            "expected_execution_state_revision": 4,
            "expected_execution_state_hash": "sha256:state-runtime-text",
            "expected_dispatch_identity_hash": "sha256:dispatch-runtime-text",
            "profile_requirements": {
                "profile_id": "inherited-current",
                "harness": "codex",
            },
            "retry_policy": {"attempt": 1, "max_attempts": 2},
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
                "contract_execution_id": "cex-runtime-text",
                "expected_execution_state_revision": 4,
                "expected_execution_state_hash": "sha256:state-runtime-text",
                "expected_dispatch_identity_hash": "sha256:dispatch-runtime-text",
                "profile_requirements": {
                    "profile_id": "inherited-current",
                    "harness": "codex",
                },
                "retry_policy": {"attempt": 1, "max_attempts": 2},
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
        "graph_current_full_reconcile",
        {
            "project_id": "aming-claw",
            "target_commit_sha": "head",
            "activate": True,
            "semantic_use_ai": False,
            "backlog_id": "AC-CURRENT-FULL",
            "contract_execution_id": "cex-current-full",
            "observer_session_id": "obs-current-full",
            "observer_route_token_ref": "rtok-current-full",
            "run_id": "current-full-head",
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
        "/api/graph-governance/aming-claw/reconcile/current-full",
        {
            "target_commit_sha": "head",
            "activate": True,
            "semantic_use_ai": False,
            "backlog_id": "AC-CURRENT-FULL",
            "contract_execution_id": "cex-current-full",
            "observer_session_id": "obs-current-full",
            "observer_route_token_ref": "rtok-current-full",
            "run_id": "current-full-head",
        },
    )
    assert recorder.calls[2] == (
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
    assert recorder.calls[3] == (
        "GET",
        "/api/graph-governance/aming-claw/stale-artifact-cleanup?repo_root=%2Frepo&include_unowned=false",
        None,
    )
    assert recorder.calls[4] == (
        "POST",
        "/api/graph-governance/aming-claw/stale-artifact-cleanup/apply",
        {
            "repo_root": "/repo",
            "candidate_ids": ["batch_worktree:abc"],
            "actor": "observer",
            "reason": "terminal cleanup",
        },
    )
    assert recorder.calls[5] == (
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
        "backlog_id",
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
        "managed_rejoin",
    ):
        assert key in properties
    assert "route_token_ref" in properties["task_id"]["description"]
    assert "route_token_ref" in properties["backlog_id"]["description"]
    assert "Observer queries derive canonical" in properties["route_token_ref"][
        "description"
    ]
    managed_rejoin = properties["managed_rejoin"]
    assert "session_token_ref" in managed_rejoin["properties"]
    assert "reason" in managed_rejoin["properties"]
    assert "session_token" not in managed_rejoin["properties"]
    assert "fence_token" not in managed_rejoin["properties"]
    assert "route_token" not in managed_rejoin["properties"]


def test_mcp_graph_query_forwards_observer_route_scope_claims():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    dispatcher.dispatch(
        "graph_query",
        {
            "project_id": "aming-claw",
            "tool": "function_index",
            "args": {"query": "handle_graph_governance_query"},
            "query_source": "observer",
            "query_purpose": "gate_validation",
            "task_id": "observer-route-task",
            "backlog_id": "AC-OBSERVER-ROUTE",
            "route_token_ref": "rtok-observer-route",
        },
    )

    assert recorder.calls[-1] == (
        "POST",
        "/api/graph-governance/aming-claw/query",
        {
            "tool": "function_index",
            "args": {"query": "handle_graph_governance_query"},
            "query_source": "observer",
            "query_purpose": "gate_validation",
            "task_id": "observer-route-task",
            "backlog_id": "AC-OBSERVER-ROUTE",
            "route_token_ref": "rtok-observer-route",
            "actor": "mcp",
        },
    )


def test_mcp_contract_runtime_generic_tools_route_to_facade():
    names = _tool_names()
    assert {
        "contract_runtime_current",
        "contract_runtime_guide",
        "contract_runtime_precheck_line",
        "contract_runtime_submit_line",
    }.issubset(names)
    submit_properties = _tool_properties("contract_runtime_submit_line")
    assert "execution_state_revision" in submit_properties
    assert "runtime_guide_hash" in submit_properties
    assert submit_properties.keys() >= {
        "backlog_id",
        "definition_hash",
        "instruction_bundle_hash",
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "worker_role",
        "session_token_ref",
        "fence_token",
        "target_project_root",
        "evidence_owner_actor",
        "evidence_owner_role",
        "submitter_session",
        "submitter_principal",
        "materialized_from",
        "authorization_source",
        "qa_session_token_ref",
        "qa_evidence_provenance",
        "status",
        "verdict",
        "verification",
        "tests",
        "test_results",
        "changed_files",
        "owned_changed_files",
        "graph_trace_ids",
        "graph_query_trace_ids",
        "graph_query_trace_id",
        "trace_ids",
        "db_verified",
        "query_source",
        "query_purpose",
        "graph_trace_evidence",
    }
    precheck_properties = _tool_properties("contract_runtime_precheck_line")
    assert {
        key: value
        for key, value in precheck_properties.items()
        if key != "response_view"
    } == submit_properties
    assert precheck_properties["response_view"] == {
        "type": "string",
        "enum": ["compact", "full"],
        "default": "compact",
        "description": (
            "MCP-only response projection. compact is deterministic and "
            "bounded; full preserves the legacy response only when it fits "
            "the managed transport. Never forwarded to governance."
        ),
    }
    timeout_schema = submit_properties["timeout_seconds"]
    assert timeout_schema["default"] == 120
    assert timeout_schema["minimum"] == 10
    assert timeout_schema["maximum"] == 60 * 60

    recorder = _Recorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        manager_api_fn=recorder.api,
        workspace="/repo",
    )

    dispatcher.dispatch(
        "contract_runtime_current",
        {"project_id": "aming-claw", "contract_execution_id": "cex-onboard"},
    )
    dispatcher.dispatch(
        "contract_runtime_guide",
        {"project_id": "aming-claw", "contract_execution_id": "cex-onboard"},
    )
    dispatcher.dispatch(
        "contract_runtime_precheck_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "execution_state_revision": 1,
            "stage_id": "graph_context",
            "line_id": "graph_query_schema_trace",
            "evidence_kind": "graph_query_schema_trace",
            "runtime_context_id": "rctx-worker",
            "task_id": "worker-task",
            "parent_task_id": "observer-task",
            "worker_role": "mf_sub",
            "session_token_ref": "sref-worker",
            "fence_token": "fence-worker",
            "target_project_root": "/tmp/worker",
        },
    )
    dispatcher.dispatch(
        "contract_runtime_submit_line",
        {
            "__aming_managed_host_envelope_continuity_bypass": True,
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "execution_state_revision": 1,
            "stage_id": "graph_context",
            "line_id": "graph_query_schema_trace",
            "evidence_kind": "graph_query_schema_trace",
            "runtime_context_id": "rctx-worker",
            "task_id": "worker-task",
            "parent_task_id": "observer-task",
            "worker_role": "mf_sub",
            "session_token_ref": "sref-worker",
            "fence_token": "fence-worker",
            "target_project_root": "/tmp/worker",
        },
    )

    assert recorder.calls == [
        (
            "GET",
            (
                "/api/projects/aming-claw/contract-runtime/cex-onboard/current-state"
                "?response_view=cli_current"
            ),
            None,
        ),
        (
            "GET",
            (
                "/api/projects/aming-claw/contract-runtime/cex-onboard/guide"
                "?response_view=cli_guide"
            ),
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


def test_runtime_context_implementation_schema_exposes_exact_atomic_writer_binding():
    writer_binding_fields = {
        "backlog_id",
        "definition_hash",
        "instruction_bundle_hash",
        "execution_state_revision",
        "runtime_guide_hash",
        "stage_id",
        "line_id",
        "evidence_kind",
        "line_instance_id",
    }

    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        tools_by_name = {str(tool.get("name") or ""): tool for tool in registry}
        implementation_schema = tools_by_name[
            "runtime_context_implementation_evidence"
        ]["inputSchema"]
        assert implementation_schema["properties"].keys() >= writer_binding_fields
        assert set(implementation_schema["required"]) >= {
            "project_id",
            "runtime_context_id",
            *writer_binding_fields,
        }
        assert tools_by_name["contract_runtime_submit_line"]["inputSchema"][
            "properties"
        ].keys() >= writer_binding_fields


def test_managed_mcp_contract_runtime_timeout_policy_is_bounded_and_configurable(
    monkeypatch,
):
    env_key = "AMING_CONTRACT_RUNTIME_MCP_TIMEOUT_SECONDS"
    monkeypatch.delenv(env_key, raising=False)

    assert mcp_tools._contract_runtime_mcp_timeout_seconds({}) == 120
    assert mcp_tools._contract_runtime_mcp_timeout_seconds(
        {"timeout_seconds": 1}
    ) == 10
    assert mcp_tools._contract_runtime_mcp_timeout_seconds(
        {"timeout_seconds": 60 * 60 + 1}
    ) == 60 * 60

    monkeypatch.setenv(env_key, "75")
    assert mcp_tools._contract_runtime_mcp_timeout_seconds({}) == 75
    assert mcp_tools._contract_runtime_mcp_timeout_seconds(
        {"timeout_seconds": 45}
    ) == 45


def test_managed_runtime_host_issuance_timeout_is_transport_only_and_stages():
    route = {
        "route_id": "route-host-issuance-timeout",
        "route_context_hash": "sha256:" + ("1" * 64),
        "prompt_contract_id": "rprompt-host-issuance-timeout",
        "prompt_contract_hash": "sha256:" + ("2" * 64),
        "route_token_ref": "rtok-host-issuance-timeout",
        "visible_injection_manifest_hash": "sha256:" + ("3" * 64),
    }
    cases = (
        ("runtime_context_session_token_initial_join", 45, 45),
        ("runtime_context_session_token_reissue", 1, 10),
        ("runtime_context_session_token_rejoin", None, 120),
    )

    for index, (tool_name, requested_timeout, expected_timeout) in enumerate(cases):
        raw_session = f"managed-timeout-session-{index}"
        raw_fence = f"managed-timeout-fence-{index}"
        identity = {
            "project_id": "aming-claw",
            "runtime_context_id": f"mfrctx-host-timeout-{index}",
            "task_id": f"worker-host-timeout-{index}",
            "parent_task_id": "cex-host-timeout",
            "contract_execution_id": "cex-host-timeout",
            "target_project_root": f"/tmp/host-timeout-{index}",
            "worker_id": f"worker-host-timeout-{index}",
            "worker_slot_id": f"slot-host-timeout-{index}",
            "agent_id": f"worker-host-timeout-{index}",
            "allocation_owner": f"worker-host-timeout-{index}",
            "actual_host_worker_id": f"worker-host-timeout-{index}",
            "worker_session_id": f"session-host-timeout-{index}",
            "host_session_id": f"session-host-timeout-{index}",
            "session_token_ref": f"wstok-host-timeout-{index}",
            **route,
        }
        calls = []

        def generic_api(*_args, **_kwargs):
            raise AssertionError("host issuance must use the timeout-aware transport")

        dispatcher = ToolDispatcher(generic_api, worker_pool=None)

        def timeout_api(method, path, data=None, *, timeout_seconds):
            calls.append((method, path, data, timeout_seconds))
            return {
                "ok": True,
                "status": "session_token_issued",
                "delivery": "worker_host_envelope",
                **identity,
                "session_token": raw_session,
                "fence_token": raw_fence,
                "host_envelope": {
                    **identity,
                    "env": {
                        "AMING_WORKER_SESSION_TOKEN": raw_session,
                        "AMING_WORKER_FENCE_TOKEN": raw_fence,
                    },
                },
            }

        dispatcher._governance_api_with_timeout = timeout_api
        arguments = {**identity, "reason": "exercise existing transport policy"}
        if requested_timeout is not None:
            arguments["timeout_seconds"] = requested_timeout
        result = dispatcher.dispatch(tool_name, arguments)

        assert calls[0][3] == expected_timeout
        assert "timeout_seconds" not in calls[0][2]
        assert result["auth_loaded"] is True
        assert result["managed_host_envelope"]["status"] == "staged"
        assert raw_session not in json.dumps(result, sort_keys=True)
        assert raw_fence not in json.dumps(result, sort_keys=True)
        assert dispatcher._host_envelope_continuity.pending_count() == 1

    dispatcher = ToolDispatcher(lambda *_args, **_kwargs: {}, worker_pool=None)
    dispatcher._governance_api_with_timeout = lambda *_args, **_kwargs: {
        "ok": False,
        "error": "request_timeout",
        "message": "timed out",
    }
    timeout_result = dispatcher.dispatch(
        "runtime_context_session_token_rejoin",
        {**identity, "reason": "preserve ambiguous transport truth"},
    )
    assert timeout_result["error"] == "request_timeout"
    assert "writes_performed" not in timeout_result
    assert timeout_result.get("zero_write_rejection") is not True


def test_runtime_host_issuance_timeout_schema_matches_existing_policy():
    timeout_schema = _tool_properties("contract_runtime_submit_line")[
        "timeout_seconds"
    ]
    policy_fields = ("type", "minimum", "maximum", "default")
    for tool_name in (
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_reissue",
        "runtime_context_session_token_rejoin",
    ):
        runtime_timeout_schema = _tool_properties(tool_name)["timeout_seconds"]
        assert {
            field: runtime_timeout_schema[field] for field in policy_fields
        } == {field: timeout_schema[field] for field in policy_fields}
        assert "transport timeout only" in runtime_timeout_schema["description"]
        assert "RuntimeContext write body" in runtime_timeout_schema["description"]

    for tool_name in (
        "runtime_context_current",
        "runtime_context_worker_guide",
        "runtime_context_read_receipt",
        "runtime_context_implementation_evidence",
        "runtime_context_scope_insufficiency_request",
        "runtime_context_worker_commit",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "parallel_branch_startup",
    ):
        assert "timeout_seconds" not in _tool_properties(tool_name)
    assert (
        "timeout_seconds"
        not in _tool_properties("graph_query")["managed_rejoin"]["properties"]
    )


def test_managed_mcp_contract_runtime_timeout_is_transport_only_and_exact_once(
):
    calls = []
    state = {"revision": 10, "completed_lines": []}
    submit_attempts = 0

    def api(method: str, path: str, data: dict | None = None) -> dict:
        if path.endswith("/current-state?response_view=cli_current"):
            calls.append((method, path, data, "", None))
            return {
                "ok": True,
                "execution_state_revision": state["revision"],
                "completed_lines": list(state["completed_lines"]),
            }
        raise AssertionError(f"unexpected generic request: {method} {path}")

    def api_with_role_token(
        method: str,
        path: str,
        data: dict | None = None,
        *,
        role_token: str,
        timeout_seconds: int = 15,
    ) -> dict:
        nonlocal submit_attempts
        calls.append((method, path, data, role_token, timeout_seconds))
        if path.endswith("/line-writes/precheck"):
            return {"ok": False, "error": "request_timeout", "message": "timed out"}
        if path.endswith("/line-writes"):
            submit_attempts += 1
            if submit_attempts == 1:
                return {
                    "ok": False,
                    "error": "request_timeout",
                    "message": "timed out",
                }
            state["revision"] += 1
            state["completed_lines"].append({"line_id": "qa_graph_context"})
            return {
                "ok": True,
                "execution_state_revision": state["revision"],
                "completed_line": {"line_id": "qa_graph_context"},
            }
        raise AssertionError(f"unexpected role request: {method} {path}")

    dispatcher = ToolDispatcher(
        api_fn=api,
        worker_pool=None,
        manager_api_fn=api,
        workspace="/repo",
    )
    dispatcher._api_with_role_token = api_with_role_token
    common = {
        "project_id": "aming-claw",
        "backlog_id": "AC-QA",
        "contract_execution_id": "cex-timeout",
        "execution_state_revision": 10,
        "stage_id": "qa",
        "line_id": "qa_graph_context",
        "evidence_kind": "qa_graph_context",
        "qa_session_token": "gov-qa-timeout-secret",
    }

    precheck = dispatcher.dispatch(
        "contract_runtime_precheck_line",
        {**common, "timeout_seconds": 45},
    )
    timed_out = dispatcher.dispatch(
        "contract_runtime_submit_line",
        {**common, "timeout_seconds": 75},
    )

    assert precheck["write_disposition"] == "not_written"
    assert precheck["effective_timeout_seconds"] == 45
    assert precheck["retry_disposition"] == "safe_to_retry_precheck"
    assert timed_out["write_disposition"] == "ambiguous"
    assert timed_out["effective_timeout_seconds"] == 75
    assert timed_out["retry_disposition"] == (
        "poll_authoritative_current_state_before_retry"
    )
    assert timed_out["retry_guidance"]["automatic_retry"] is False
    assert "gov-qa-timeout-secret" not in json.dumps(precheck)
    assert "gov-qa-timeout-secret" not in json.dumps(timed_out)
    assert all("timeout_seconds" not in (call[2] or {}) for call in calls)

    current = dispatcher.dispatch(
        "contract_runtime_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-timeout",
        },
    )
    assert current["execution_state_revision"] == 10
    assert current["completed_lines"] == []

    retried = dispatcher.dispatch(
        "contract_runtime_submit_line",
        {**common, "timeout_seconds": 75},
    )
    assert retried["ok"] is True
    assert retried["execution_state_revision"] == 11
    assert submit_attempts == 2
    assert len(state["completed_lines"]) == 1
    assert [call[4] for call in calls if call[4] is not None] == [45, 75, 75]


def test_mcp_contract_runtime_bypass_line_schema_dispatch_and_auth_are_copy_safe():
    tool = next(
        item for item in TOOLS if item.get("name") == "contract_runtime_bypass_line"
    )
    assert set(tool["inputSchema"]["required"]) == {
        "project_id",
        "contract_execution_id",
        "bypass_identity",
        "stage_id",
        "line_id",
        "execution_state_revision",
        "classification",
        "reason",
        "decision",
    }
    properties = _tool_properties("contract_runtime_bypass_line")
    assert properties.keys() >= {
        "runtime_guide_hash",
        "diagnostic_backlog_id",
        "diagnostic_priority",
        "evidence_refs",
        "graph_trace_ids",
        "observer_route_token_ref",
        "observer_session_id",
        "qa_session_token",
        "qa_session_token_ref",
    }
    assert properties["graph_trace_ids"]["type"] == "array"
    assert properties["graph_trace_ids"]["items"] == {"type": "string"}
    assert "graph_trace_ids" not in tool["inputSchema"]["required"]

    recorder = _AuthRecorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        manager_api_fn=recorder.api,
        workspace="/repo",
    )
    dispatcher._api_with_role_token = recorder.api_with_role_token
    body = {
        "project_id": "aming-claw",
        "contract_execution_id": "cex-bypass",
        "bypass_identity": "bypass:cex-bypass:3",
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "execution_state_revision": 3,
        "runtime_guide_hash": "sha256:guide",
        "classification": "system_logic",
        "reason": "the current gate cannot advance",
        "decision": "continue with linked OPEN diagnostic",
        "evidence_refs": ["timeline-event:13901"],
        "graph_trace_ids": ["gqt-current-full-source"],
        "qa_session_token": "gov-qa-secret",
    }

    dispatcher.dispatch("contract_runtime_bypass_line", body)
    evidence_only_body = {
        **body,
        "bypass_identity": "bypass:cex-bypass:4",
        "evidence_refs": ["graph-query:gqt-evidence-only"],
    }
    evidence_only_body.pop("graph_trace_ids")
    dispatcher.dispatch("contract_runtime_bypass_line", evidence_only_body)

    assert recorder.auth_calls == [
        (
            "POST",
            "/api/projects/aming-claw/contract-runtime/cex-bypass/line-bypasses",
            {
                key: value
                for key, value in body.items()
                if key not in {"project_id", "contract_execution_id", "qa_session_token"}
            },
            "gov-qa-secret",
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-runtime/cex-bypass/line-bypasses",
            {
                key: value
                for key, value in evidence_only_body.items()
                if key not in {"project_id", "contract_execution_id", "qa_session_token"}
            },
            "gov-qa-secret",
        ),
    ]
    assert recorder.auth_calls[0][2]["graph_trace_ids"] == [
        "gqt-current-full-source"
    ]
    assert "graph_trace_ids" not in recorder.auth_calls[1][2]
    assert all(
        "gov-qa-secret" not in json.dumps(call[2], sort_keys=True)
        for call in recorder.auth_calls
    )


def test_mcp_qa_session_tools_and_contract_runtime_auth_token_do_not_leak_body():
    names = _tool_names()
    assert {"qa_session_register", "qa_session_heartbeat"}.issubset(names)
    assert "qa_session_token" in _tool_properties("qa_session_heartbeat")
    assert "qa_session_token" in _tool_properties("graph_query")
    assert "qa_session_token_ref" in _tool_properties("graph_query")
    assert _tool_properties("graph_query")["timeout_seconds"]["default"] == 120
    assert "qa_session_token" in _tool_properties("task_timeline_append")
    qa_register = next(
        tool for tool in TOOLS if tool.get("name") == "qa_session_register"
    )
    assert set(qa_register["inputSchema"]["required"]) == {
        "project_id",
        "backlog_id",
        "task_id",
        "commit_sha",
        "contract_execution_id",
    }
    assert "contract_execution_id" in qa_register["inputSchema"]["properties"]
    for tool_name in (
        "onboard_contract_submit_line",
        "contract_add_current",
        "contract_add_submit_line",
        "contract_update_current",
        "contract_update_submit_line",
        "contract_runtime_current",
        "contract_runtime_guide",
        "contract_runtime_submit_line",
        "contract_runtime_precheck_line",
    ):
        assert "qa_session_token" in _tool_properties(tool_name)
    for tool_name in (
        "contract_runtime_current",
        "contract_runtime_guide",
        "contract_runtime_submit_line",
        "contract_runtime_precheck_line",
    ):
        assert "qa_session_token_ref" in _tool_properties(tool_name)
        assert "backlog_id" in _tool_properties(tool_name)

    recorder = _AuthRecorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        manager_api_fn=recorder.api,
        workspace="/repo",
    )
    dispatcher._api_with_role_token = recorder.api_with_role_token

    dispatcher.dispatch(
        "qa_session_register",
        {
            "project_id": "aming-claw",
            "principal_id": "qa:hooke",
            "scope": ["read:graph"],
            "backlog_id": "AC-QA",
            "task_id": "qa-task",
            "commit_sha": "a" * 40,
        },
    )
    dispatcher.dispatch(
        "qa_session_heartbeat",
        {
            "project_id": "aming-claw",
            "qa_session_token": "gov-qa-token",
            "status": "verifying",
        },
    )
    dispatcher.dispatch(
        "contract_runtime_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-hotfix",
            "qa_session_token": "gov-qa-token",
        },
    )
    dispatcher.dispatch(
        "contract_runtime_guide",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-hotfix",
            "qa_session_token": "gov-qa-token",
        },
    )
    dispatcher.dispatch(
        "onboard_contract_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "qa_session_token": "gov-qa-token",
            "stage_id": "qa",
            "line_id": "qa_independent_verification",
            "evidence_kind": "independent_verification",
            "payload": {"decision": "pass"},
        },
    )
    dispatcher.dispatch(
        "contract_runtime_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-hotfix",
            "qa_session_token": "gov-qa-token",
            "stage_id": "qa",
            "line_id": "qa_independent_verification",
            "evidence_kind": "independent_verification",
            "payload": {"decision": "pass"},
        },
    )
    dispatcher.dispatch(
        "contract_update_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-update",
            "qa_session_token": "gov-qa-token",
            "stage_id": "qa",
            "line_id": "qa_independent_verification",
            "evidence_kind": "independent_verification",
        },
    )
    dispatcher.dispatch(
        "graph_query",
        {
            "project_id": "aming-claw",
            "qa_session_token": "gov-qa-token",
            "tool": "query_schema",
            "query_source": "qa",
            "query_purpose": "independent_verification",
            "backlog_id": "AC-QA",
            "task_id": "qa-task",
            "commit_sha": "a" * 40,
        },
    )
    dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "qa_session_token": "gov-qa-token",
            "backlog_id": "AC-QA",
            "task_id": "qa-task",
            "event_type": "qa.independent_verification",
            "event_kind": "independent_verification",
            "phase": "verification",
            "actor": "qa:hooke",
            "status": "passed",
            "commit_sha": "a" * 40,
            "payload": {"graph_trace_ids": ["gqt-qa"]},
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/role/assign",
            {
                "project_id": "aming-claw",
                "principal_id": "qa:hooke",
                "role": "qa",
                "scope": ["read:graph"],
                "backlog_id": "AC-QA",
                "task_id": "qa-task",
                "commit_sha": "a" * 40,
            },
        )
    ]
    assert recorder.auth_calls == [
        (
            "POST",
            "/api/role/heartbeat",
            {"project_id": "aming-claw", "status": "verifying"},
            "gov-qa-token",
        ),
        (
            "GET",
            (
                "/api/projects/aming-claw/contract-runtime/cex-hotfix/current-state"
                "?response_view=cli_current"
            ),
            None,
            "gov-qa-token",
        ),
        (
            "GET",
            (
                "/api/projects/aming-claw/contract-runtime/cex-hotfix/guide"
                "?response_view=cli_guide"
            ),
            None,
            "gov-qa-token",
        ),
        (
            "POST",
            "/api/projects/aming-claw/onboard-contract/cex-onboard/line-writes",
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "evidence_kind": "independent_verification",
                "payload": {"decision": "pass"},
            },
            "gov-qa-token",
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-runtime/cex-hotfix/line-writes",
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "evidence_kind": "independent_verification",
                "payload": {"decision": "pass"},
            },
            "gov-qa-token",
        ),
        (
            "POST",
            "/api/projects/aming-claw/contract-update/cex-update/line-writes",
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "evidence_kind": "independent_verification",
            },
            "gov-qa-token",
        ),
        (
            "POST",
            "/api/graph-governance/aming-claw/query",
            {
                "tool": "query_schema",
                "query_source": "qa",
                "query_purpose": "independent_verification",
                "backlog_id": "AC-QA",
                "task_id": "qa-task",
                "commit_sha": "a" * 40,
            },
            "gov-qa-token",
        ),
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {
                "backlog_id": "AC-QA",
                "task_id": "qa-task",
                "event_type": "qa.independent_verification",
                "event_kind": "independent_verification",
                "phase": "verification",
                "actor": "qa:hooke",
                "status": "passed",
                "commit_sha": "a" * 40,
                "payload": {"graph_trace_ids": ["gqt-qa"]},
            },
            "gov-qa-token",
        ),
    ]


def test_mcp_graph_query_uses_bounded_transport_timeout_without_forwarding_it():
    calls = []
    dispatcher = ToolDispatcher(
        api_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("graph query must use timeout-aware transport")
        ),
        worker_pool=None,
        workspace="/repo",
    )

    def with_role(method, path, data=None, *, role_token, timeout_seconds=15):
        calls.append((method, path, data, role_token, timeout_seconds))
        return {"ok": True}

    dispatcher._api_with_role_token = with_role
    dispatcher.dispatch(
        "graph_query",
        {
            "project_id": "aming-claw",
            "tool": "query_schema",
            "query_source": "qa",
            "query_purpose": "independent_verification",
            "backlog_id": "AC-QA-TIMEOUT",
            "task_id": "cex-qa-timeout",
            "commit_sha": "a" * 40,
            "qa_session_token": "qa-secret",
            "timeout_seconds": 180,
        },
    )

    assert calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/query",
            {
                "tool": "query_schema",
                "query_source": "qa",
                "query_purpose": "independent_verification",
                "backlog_id": "AC-QA-TIMEOUT",
                "task_id": "cex-qa-timeout",
                "commit_sha": "a" * 40,
            },
            "qa-secret",
            180,
        )
    ]


def test_mcp_qa_session_register_without_contract_does_not_issue_opaque_ref():
    raw_token = "gov-qa-missing-contract-must-stay-private"

    class MissingContractRecorder(_Recorder):
        def api(self, method: str, path: str, data: dict | None = None) -> dict:
            self.calls.append((method, path, data))
            return {
                "session_id": "ses-qa-missing-contract",
                "principal_id": "qa:missing-contract",
                "role": "qa",
                "scope": [],
                "token": raw_token,
                "expires_at": "2099-07-15T12:00:00Z",
            }

    recorder = MissingContractRecorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        manager_api_fn=recorder.api,
        workspace="/repo",
    )

    rejected = dispatcher.dispatch(
        "qa_session_register",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-QA-MISSING-CONTRACT",
            "task_id": "worker-task",
            "commit_sha": "d" * 40,
            "principal_id": "qa:missing-contract",
        },
    )

    assert rejected["error"] == "qa_session_register_invalid_response"
    assert "qa_session_token_ref" not in rejected
    assert raw_token not in json.dumps(rejected, sort_keys=True)
    assert dispatcher._qa_session_refs == {}


def test_mcp_qa_session_opaque_ref_roundtrip_is_scope_bound_and_header_only():
    raw_token = "gov-qa-raw-must-stay-inside-dispatcher"
    commit_sha = "a" * 40

    class QARefRecorder(_AuthRecorder):
        def api(self, method: str, path: str, data: dict | None = None) -> dict:
            self.calls.append((method, path, data))
            if path == "/api/role/assign":
                return {
                    "session_id": "ses-qa-opaque",
                    "principal_id": "qa:opaque",
                    "role": "qa",
                    "scope": [
                        "backlog:AC-QA-OPAQUE",
                        "task:worker-task",
                        f"commit:{commit_sha}",
                    ],
                    "token": raw_token,
                    "expires_at": "2099-07-15T12:00:00Z",
                }
            return {"ok": True}

    recorder = QARefRecorder()
    dispatcher = ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        manager_api_fn=recorder.api,
        workspace="/repo",
    )
    dispatcher._api_with_role_token = recorder.api_with_role_token

    registered = dispatcher.dispatch(
        "qa_session_register",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-QA-OPAQUE",
            "task_id": "worker-task",
            "commit_sha": commit_sha,
            "contract_execution_id": "cex-qa-opaque",
            "principal_id": "qa:opaque",
        },
    )
    token_ref = registered["qa_session_token_ref"]

    assert token_ref.startswith("qa-session-ref-")
    assert len(token_ref) >= 50
    assert registered["session_id"] == "ses-qa-opaque"
    assert registered["expires_at"] == "2099-07-15T12:00:00Z"
    assert registered["qa_session_scope_binding"] == {
        "project_id": "aming-claw",
        "backlog_id": "AC-QA-OPAQUE",
        "task_id": "worker-task",
        "commit_sha": commit_sha,
        "contract_execution_id": "cex-qa-opaque",
        "session_id": "ses-qa-opaque",
    }
    assert registered["raw_qa_session_token_exposed"] is False
    assert raw_token not in json.dumps(registered, sort_keys=True)
    for raw_field in ("token", "raw_token", "qa_session_token", "role_token"):
        assert raw_field not in registered
    assert recorder.calls == [
        (
            "POST",
            "/api/role/assign",
            {
                "project_id": "aming-claw",
                "principal_id": "qa:opaque",
                "role": "qa",
                "backlog_id": "AC-QA-OPAQUE",
                "task_id": "worker-task",
                "commit_sha": commit_sha,
            },
        )
    ]

    dispatcher.dispatch(
        "graph_query",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-QA-OPAQUE",
            "task_id": "worker-task",
            "commit_sha": commit_sha,
            "qa_session_token_ref": token_ref,
            "tool": "query_schema",
            "query_source": "qa",
            "query_purpose": "independent_verification",
        },
    )
    for tool_name in ("contract_runtime_current", "contract_runtime_guide"):
        dispatcher.dispatch(
            tool_name,
            {
                "project_id": "aming-claw",
                "backlog_id": "AC-QA-OPAQUE",
                "contract_execution_id": "cex-qa-opaque",
                "qa_session_token_ref": token_ref,
            },
        )
    qa_line_evidence = {
        "status": "passed",
        "verdict": "pass",
        "verification": {"result": "passed"},
        "tests": [{"name": "focused", "status": "passed"}],
        "test_results": {"passed": 15, "failed": 0},
        "changed_files": ["agent/governance/server.py"],
        "graph_query_trace_ids": ["gqt-qa-opaque"],
    }
    for tool_name in (
        "contract_runtime_precheck_line",
        "contract_runtime_submit_line",
    ):
        dispatcher.dispatch(
            tool_name,
            {
                "project_id": "aming-claw",
                "backlog_id": "AC-QA-OPAQUE",
                "contract_execution_id": "cex-qa-opaque",
                "qa_session_token_ref": token_ref,
                "execution_state_revision": 11,
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "evidence_kind": "independent_verification",
                **qa_line_evidence,
            },
        )

    assert len(recorder.auth_calls) == 5
    assert all(call[3] == raw_token for call in recorder.auth_calls)
    graph_body = recorder.auth_calls[0][2]
    assert graph_body is not None
    assert "qa_session_token_ref" not in graph_body
    assert raw_token not in json.dumps(graph_body, sort_keys=True)
    assert all(token_ref not in call[1] for call in recorder.auth_calls)
    assert recorder.auth_calls[1][2] is None
    assert recorder.auth_calls[2][2] is None
    for line_call in recorder.auth_calls[3:]:
        assert line_call[2]["qa_session_token_ref"] == token_ref
        assert line_call[2]["backlog_id"] == "AC-QA-OPAQUE"
        for field, expected in qa_line_evidence.items():
            assert line_call[2][field] == expected
        assert raw_token not in json.dumps(line_call[2], sort_keys=True)


def test_mcp_qa_session_opaque_ref_rejects_unknown_stale_and_cross_scope():
    commit_sha = "b" * 40

    class QARefRecorder(_AuthRecorder):
        def __init__(self, expires_at: str = "2099-07-15T12:00:00Z"):
            super().__init__()
            self.expires_at = expires_at

        def api(self, method: str, path: str, data: dict | None = None) -> dict:
            self.calls.append((method, path, data))
            if path == "/api/role/assign":
                return {
                    "session_id": "ses-qa-scope",
                    "principal_id": "qa:scope",
                    "role": "qa",
                    "scope": [],
                    "token": "gov-qa-scope-secret",
                    "expires_at": self.expires_at,
                }
            return {"ok": True}

    def registered_dispatcher(expires_at: str = "2099-07-15T12:00:00Z"):
        recorder = QARefRecorder(expires_at)
        dispatcher = ToolDispatcher(
            api_fn=recorder.api,
            worker_pool=None,
            manager_api_fn=recorder.api,
            workspace="/repo",
        )
        dispatcher._api_with_role_token = recorder.api_with_role_token
        result = dispatcher.dispatch(
            "qa_session_register",
            {
                "project_id": "aming-claw",
                "backlog_id": "AC-QA-SCOPE",
                "task_id": "worker-task",
                "commit_sha": commit_sha,
                "contract_execution_id": "cex-qa-scope",
            },
        )
        return dispatcher, recorder, result["qa_session_token_ref"]

    dispatcher, recorder, token_ref = registered_dispatcher()
    graph_args = {
        "project_id": "aming-claw",
        "backlog_id": "AC-QA-SCOPE",
        "task_id": "worker-task",
        "commit_sha": commit_sha,
        "tool": "query_schema",
        "query_source": "qa",
        "query_purpose": "independent_verification",
    }
    unknown = dispatcher.dispatch(
        "graph_query",
        {**graph_args, "qa_session_token_ref": "qa-session-ref-unknown"},
    )
    assert unknown["error"] == "qa_session_token_ref_unknown"

    mismatches = (
        ("project_id", "other-project"),
        ("backlog_id", "AC-OTHER"),
        ("task_id", "other-task"),
        ("commit_sha", "c" * 40),
    )
    for field, value in mismatches:
        rejected = dispatcher.dispatch(
            "graph_query",
            {**graph_args, field: value, "qa_session_token_ref": token_ref},
        )
        assert rejected["error"] == "qa_session_token_ref_scope_mismatch"
        assert field in rejected["mismatched_fields"]

    for field, value in (
        ("backlog_id", "AC-OTHER"),
        ("contract_execution_id", "cex-other"),
    ):
        rejected = dispatcher.dispatch(
            "contract_runtime_current",
            {
                "project_id": "aming-claw",
                "backlog_id": "AC-QA-SCOPE",
                "contract_execution_id": "cex-qa-scope",
                "qa_session_token_ref": token_ref,
                field: value,
            },
        )
        assert rejected["error"] == "qa_session_token_ref_scope_mismatch"
        assert field in rejected["mismatched_fields"]
    assert recorder.auth_calls == []

    stale_dispatcher, stale_recorder, stale_ref = registered_dispatcher(
        "2000-01-01T00:00:00Z"
    )
    stale = stale_dispatcher.dispatch(
        "graph_query",
        {**graph_args, "qa_session_token_ref": stale_ref},
    )
    assert stale["error"] == "qa_session_token_ref_stale"
    assert stale_recorder.auth_calls == []


def test_active_mcp_contract_tools_expose_onboard_root_with_update_facade():
    names = _tool_names()

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
        "qa_session_register",
        "qa_session_heartbeat",
    }.issubset(names)
    assert "contract_execution_id" not in _tool_properties("onboard_contract_start")
    batch_properties = _tool_properties("mf_batch_parallel_enter")
    assert batch_properties.keys() >= {
        "schema_version",
        "backlog_id",
        "bug_id",
        "backlog_ids",
        "observer_session_id",
        "observer_route_token_ref",
        "target_head_commit",
        "target_ref",
        "snapshot_id",
        "graph_snapshot_id",
        "preflight_mode",
        "merge_mode",
        "metadata",
    }
    assert "onboard_service_waiver" not in batch_properties
    assert "merge_queue_id" not in batch_properties
    assert batch_properties["schema_version"]["const"] == (
        "onboard_route_guide.mf_batch_parallel_entry_input.v1"
    )
    assert batch_properties["metadata"]["properties"][
        "nested_worker_fanout_supported"
    ]["const"] is False
    assert _tool_properties("mf_parallel_enter").keys() >= {
        "parent_batch_id",
        "merge_queue_id",
        "merge_queue_item",
        "onboard_service_waiver",
        "owned_files",
        "target_files",
    }
    assert _tool_properties("observer_hotfix_enter").keys() >= {
        "observer_session_id",
        "observer_route_token_ref",
        "onboard_service_waiver",
    }
    onboard_start = next(
        tool for tool in TOOLS if tool.get("name") == "onboard_contract_start"
    )
    assert "Legacy/internal" in onboard_start["description"]
    assert "onboard_route_guide" in onboard_start["description"]
    assert _tool_properties("onboard_contract_submit_line").keys() >= {
        "execution_state_revision",
        "runtime_guide_hash",
        "observer_session_id",
        "observer_route_token_ref",
    }
    assert _tool_properties("contract_runtime_submit_line").keys() >= {
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
    precheck_properties = _tool_properties("contract_runtime_precheck_line")
    submit_properties = _tool_properties("contract_runtime_submit_line")
    assert {
        key: value
        for key, value in precheck_properties.items()
        if key != "response_view"
    } == submit_properties
    assert precheck_properties["response_view"]["enum"] == ["compact", "full"]


def test_tool_dispatcher_mf_parallel_enter_rejects_missing_or_blank_project_prewrite():
    for args in (
        {"backlog_id": "AC-PARALLEL", "task_id": "parallel-task"},
        {
            "project_id": "",
            "backlog_id": "AC-PARALLEL",
            "task_id": "parallel-task",
        },
        {
            "project_id": "   ",
            "backlog_id": "AC-PARALLEL",
            "task_id": "parallel-task",
        },
    ):
        calls = []

        def fake_api(method, path, data=None):
            calls.append((method, path, data))
            return {"ok": True}

        dispatcher = ToolDispatcher(
            api_fn=fake_api,
            worker_pool=None,
            manager_api_fn=fake_api,
        )
        result = dispatcher.dispatch("mf_parallel_enter", args)

        assert result == {
            "ok": False,
            "error": "mf_parallel_enter_project_id_missing_or_empty",
            "code": "mf_parallel_enter_project_id_missing_or_empty",
            "field": "project_id",
            "expected": "non_empty_exact_project_id",
            "actual": "missing_or_empty",
            "guide": (
                "A server-issued per_row_successors[*].body must already include "
                "project_id; external callers must provide the exact project_id "
                "before calling mf_parallel_enter."
            ),
            "source": (
                "runtime_mcp.ToolDispatcher.dispatch."
                "mf_parallel_enter_prewrite_gate"
            ),
            "host_correctable": True,
            "zero_write_rejection": True,
            "writes_performed": False,
        }
        assert calls == []


def test_active_mcp_onboard_contract_tools_route_to_source_backed_facade():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "onboard_contract_start",
        {
            "project_id": "aming-claw",
            "backlog_id": "AC-ONBOARD",
            "contract_execution_id": "cex-must-not-forward",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )
    dispatcher.dispatch(
        "onboard_contract_current",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )
    dispatcher.dispatch(
        "onboard_contract_submit_line",
        {
            "project_id": "aming-claw",
            "contract_execution_id": "cex-onboard",
            "stage_id": "graph_context",
            "line_id": "graph_query_schema_trace",
            "evidence_kind": "graph_query_schema_trace",
            "execution_state_revision": 1,
            "runtime_guide_hash": "sha256:guide",
            "observer_session_id": "obs-onboard",
            "observer_route_token_ref": "rtok-onboard",
        },
    )

    assert recorder.calls == [
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
            "/api/projects/aming-claw/onboard-contract/cex-onboard/current-state"
            "?observer_session_id=obs-onboard&observer_route_token_ref=rtok-onboard",
            None,
        ),
        (
            "POST",
            "/api/projects/aming-claw/onboard-contract/cex-onboard/line-writes",
            {
                "stage_id": "graph_context",
                "line_id": "graph_query_schema_trace",
                "evidence_kind": "graph_query_schema_trace",
                "execution_state_revision": 1,
                "runtime_guide_hash": "sha256:guide",
                "observer_session_id": "obs-onboard",
                "observer_route_token_ref": "rtok-onboard",
            },
        ),
    ]


def test_active_mcp_mf_batch_parallel_enter_routes_to_runtime_facade():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

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

    assert result["path"] == "/api/projects/aming-claw/mf-batch-parallel/enter"
    assert recorder.calls == [
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


def test_mcp_parallel_branch_tool_schemas_expose_bounded_identity_fields():
    precheck = next(
        tool
        for tool in TOOLS
        if tool.get("name") == "parallel_branch_allocate_precheck"
    )
    allocate = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_allocate")
    startup = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_startup")
    checkpoint = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_checkpoint")
    finish_gate = next(tool for tool in TOOLS if tool.get("name") == "parallel_branch_finish_gate")
    initial_join = next(
        tool for tool in TOOLS if tool.get("name") == "runtime_context_session_token_initial_join"
    )
    runtime_text = next(
        tool for tool in TOOLS if tool.get("name") == "observer_runtime_text_prepare"
    )

    precheck_props = precheck["inputSchema"]["properties"]
    allocate_props = allocate["inputSchema"]["properties"]
    startup_props = startup["inputSchema"]["properties"]
    checkpoint_props = checkpoint["inputSchema"]["properties"]
    finish_props = finish_gate["inputSchema"]["properties"]
    initial_join_props = initial_join["inputSchema"]["properties"]
    runtime_text_props = runtime_text["inputSchema"]["properties"]
    assert allocate_props["profile_requirements"]["type"] == "object"
    assert allocate_props["retry_policy"]["type"] == "object"
    assert allocate_props["worker_slot_id"]["type"] == "string"
    assert allocate_props["branch_ref"]["type"] == "string"
    assert allocate_props["acceptance_criteria"]["type"] == "array"
    assert allocate_props["allocation_precheck"]["type"] == "object"
    assert runtime_text["inputSchema"]["required"] == [
        "project_id",
        "backlog_id",
        "observer_command_id",
        "route_context_hash",
        "prompt_contract_id",
    ]
    for key in (
        "contract_execution_id",
        "expected_execution_state_revision",
        "expected_execution_state_hash",
        "expected_dispatch_identity_hash",
        "profile_requirements",
        "retry_policy",
    ):
        assert key in runtime_text_props

    assert precheck["inputSchema"]["required"] == ["project_id", "lanes"]
    assert precheck_props["lanes"]["minItems"] == 1
    assert precheck_props["lanes"]["maxItems"] == 2
    assert precheck_props["expected_lane_count"]["enum"] == [1, 2]
    assert precheck_props["expected_worker_count"]["enum"] == [1, 2]
    assert (
        governance_mcp_server._parallel_branch_allocate_precheck_schema_properties()
        == mcp_tools._parallel_branch_allocate_precheck_schema_properties()
    )
    assert {
        "task_id",
        "backlog_id",
        "contract_execution_id",
        "worker_id",
        "route_token_ref",
        "owned_files",
    } == set(precheck_props["lanes"]["items"]["required"])
    assert allocate["inputSchema"]["required"] == ["project_id", "task_id"]
    for key in (
        "workspace_root",
        "repo_root_path",
        "target_project_root",
        "target_graph_root",
        "backlog_id",
        "contract_execution_id",
        "successor_contract_execution_id",
        "current_contract_execution_id",
        "parent_task_id",
        "root_task_id",
        "stage_task_id",
        "agent_id",
        "worker_id",
        "branch_prefix",
        "worktree_root",
        "worktree_path",
        "worker_worktree_path",
        "assigned_worktree",
        "ref_name",
        "target_branch",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "fence_token",
        "create_worktree",
        "route_token_ref",
    ):
        assert key in allocate_props

    assert allocate_props["route_token_ref"]["type"] == "string"
    assert (
        "Canonical target project/worktree root"
        in allocate_props["target_project_root"]["description"]
    )
    assert (
        "Alias for target_project_root"
        in allocate_props["target_graph_root"]["description"]
    )
    assert (
        allocate_props["route_token_ref"]["description"]
        == "Opaque server-registered route token reference accepted by protected HTTP facades."
    )
    assert startup["inputSchema"]["required"] == ["project_id", "task_id"]
    for key in (
        "contract_execution_id",
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
        "host_session_id",
        "actual_cwd",
        "actual_git_root",
        "target_project_root",
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
    mirror_startup = next(
        tool
        for tool in governance_mcp_server.TOOLS
        if tool.get("name") == "parallel_branch_startup"
    )
    mirror_startup_props = mirror_startup["inputSchema"]["properties"]
    assert startup_props["contract_execution_id"] == mirror_startup_props[
        "contract_execution_id"
    ]

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
    for key in (
        "agent_id",
        "actual_host_worker_id",
        "host_worker_id",
        "worker_session_id",
        "host_startup_id",
        "host_session_id",
    ):
        assert key in initial_join_props

    for props in (allocate_props, checkpoint_props, finish_props):
        assert props["route_token"]["type"] == "object"
        assert props["route_waiver"]["type"] == "object"
        assert props["route_token_waiver"]["type"] == "object"
    assert runtime_text_props["observer_command_id"]["type"] == "string"
    assert runtime_text_props["parent_route_identity"]["type"] == "object"
    assert "canonical parent route identity" in runtime_text_props[
        "parent_route_identity"
    ]["description"]
    for key in (
        "backend_mode",
        "worker_backend",
        "worker_next_legal_action",
        "read_receipt_hash",
        "read_receipt_event_id",
        "actual_host_worker_id",
        "host_startup_id",
        "host_session_id",
        "worker_session_id",
        "worker_transcript_ref",
        "worker_transcript_path",
        "harness_type",
        "filer_principal",
        "session_token_surrogate",
        "startup_prerequisites",
        "startup_source",
    ):
        assert key in runtime_text_props


def test_mcp_parallel_branch_allocate_precheck_routes_atomic_body_unchanged():
    class PrecheckRecorder(_Recorder):
        def api(self, method: str, path: str, data: dict | None = None) -> dict:
            self.calls.append((method, path, data))
            if path.endswith("/parallel-branches/allocation-precheck"):
                return {
                    "ok": True,
                    "copy_safe_allocation_bodies": [
                        {
                            "project_id": "aming-claw",
                            "task_id": "precheck-a",
                            "backlog_id": "AC-PRECHECK",
                            "contract_execution_id": "cex-precheck",
                            "worker_id": "slot-a",
                            "worker_slot_id": "slot-a",
                            "route_token_ref": "rtok-precheck-a",
                            "branch_ref": "refs/heads/codex/precheck-a",
                            "owned_files": ["src/a.py"],
                            "target_files": ["src/a.py"],
                            "acceptance_criteria": [
                                {
                                    "id": "AC-PRECHECK",
                                    "required_scope": {
                                        "kind": "files",
                                        "files": ["src/a.py"],
                                    },
                                }
                            ],
                            "allocation_precheck": {
                                "schema_version": (
                                    "parallel_branch_allocate_precheck.receipt.v2"
                                ),
                                "status": "ready",
                                "submit_unchanged": True,
                                "authority_hash": "sha256:signed-precheck",
                            },
                        }
                    ],
                }
            return {"ok": True, "method": method, "path": path, "data": data}

    recorder = PrecheckRecorder()
    dispatcher = _dispatcher(recorder)
    lanes = [
        {
            "task_id": "precheck-a",
            "backlog_id": "AC-PRECHECK",
            "contract_execution_id": "cex-precheck",
            "worker_id": "slot-a",
            "route_token_ref": "rtok-precheck-a",
            "owned_files": ["src/a.py"],
        },
        {
            "task_id": "precheck-b",
            "backlog_id": "AC-PRECHECK",
            "contract_execution_id": "cex-precheck",
            "worker_id": "slot-b",
            "route_token_ref": "rtok-precheck-b",
            "owned_files": ["src/b.py"],
        },
    ]

    precheck = dispatcher.dispatch(
        "parallel_branch_allocate_precheck",
        {
            "project_id": "aming-claw",
            "expected_lane_count": 2,
            "base_commit": "a" * 40,
            "lanes": lanes,
        },
    )

    assert recorder.calls[-1] == (
        "POST",
        (
            "/api/graph-governance/aming-claw/parallel-branches/"
            "allocation-precheck"
        ),
        {
            "expected_lane_count": 2,
            "base_commit": "a" * 40,
            "lanes": lanes,
        },
    )
    allocation_body = precheck["copy_safe_allocation_bodies"][0]
    assert allocation_body["project_id"] == "aming-claw"

    dispatcher.dispatch("parallel_branch_allocate", allocation_body)

    assert recorder.calls[-1] == (
        "POST",
        "/api/graph-governance/aming-claw/parallel-branches/allocate",
        allocation_body,
    )


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
            "target_project_root": "/repo/.worktrees/mf-sub-1",
            "target_graph_root": "/repo/.worktrees/mf-sub-1",
            "backlog_id": "BUG-1",
            "parent_task_id": "observer-1",
            "root_task_id": "observer-1",
            "stage_task_id": "mf-sub-1",
            "agent_id": "codex",
            "worker_id": "worker-1",
            "profile_requirements": {
                "profile_id": "codex-mf-sub",
                "harness": "codex",
            },
            "retry_policy": {"attempt": 1, "max_attempts": 2},
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
            "route_token_ref": "rtok-allocate",
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
                "target_project_root": "/repo/.worktrees/mf-sub-1",
                "target_graph_root": "/repo/.worktrees/mf-sub-1",
                "backlog_id": "BUG-1",
                "parent_task_id": "observer-1",
                "root_task_id": "observer-1",
                "stage_task_id": "mf-sub-1",
                "agent_id": "codex",
                "worker_id": "worker-1",
                "profile_requirements": {
                    "profile_id": "codex-mf-sub",
                    "harness": "codex",
                },
                "retry_policy": {"attempt": 1, "max_attempts": 2},
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
                "route_token_ref": "rtok-allocate",
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

    expected_redeploy_body = {"chain_version": "abc1234"}
    branch_ref = dispatcher._git_branch()
    if branch_ref:
        expected_redeploy_body["branch_ref"] = branch_ref
    assert manager.calls == [
        ("GET", "/api/manager/health", None),
        ("POST", "/api/manager/redeploy/governance", expected_redeploy_body),
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
    assert status["mcp_tool_schema"]["status"] == "current"
    assert status["mcp_tool_schema"]["client_schema_fresh"] is True
    assert status["mcp_tool_schema"]["loaded_client_tool_schema_fingerprint"] == (
        mcp_tool_schema_fingerprint(TOOLS)
    )
    assert status["mcp_tool_schema"]["server_tool_schema_fingerprint"] == (
        mcp_tool_schema_fingerprint(TOOLS)
    )
    assert status["mcp_tool_schema"]["loaded_client_tool_schema_version"] == (
        MCP_TOOL_SCHEMA_VERSION
    )
    assert status["legacy_runtime_waivers"] == []
    assert governance.calls == [
        ("GET", "/api/health", None),
        ("GET", "/api/version-check/aming-claw", None),
    ]
    assert manager.calls == [("GET", "/api/manager/health", None)]


def test_mcp_runtime_status_detects_live_server_tool_schema_upgrade():
    class UpgradedSchemaGovernance(_RuntimeGovRecorder):
        def api(self, method: str, path: str, data: dict | None = None) -> dict:
            if path == "/api/health":
                self.calls.append((method, path, data))
                return {
                    "status": "ok",
                    "version": "next123",
                    "mcp_tool_schema_version": "2099-01-01.1",
                    "mcp_tool_schema_min_client_version": "2099-01-01.1",
                }
            return super().api(method, path, data)

    status = _dispatcher(UpgradedSchemaGovernance(), _Recorder()).dispatch(
        "runtime_status",
        {"project_id": "aming-claw"},
    )

    schema = status["mcp_tool_schema"]
    assert schema["status"] == "stale_client"
    assert schema["client_schema_fresh"] is False
    assert schema["loaded_client_tool_schema_version"] == MCP_TOOL_SCHEMA_VERSION
    assert schema["server_tool_schema_version"] == "2099-01-01.1"
    assert "restart_or_refresh_mcp_session" in status["recommended_actions"]


@pytest.mark.parametrize(
    (
        "nested_present",
        "nested_value",
        "top_present",
        "top_value",
        "expected_current",
        "resolution_status",
    ),
    [
        (False, None, False, None, False, "absent"),
        (True, "current", False, None, True, "resolved_nested"),
        (False, None, True, "current", True, "resolved_top_level"),
        (True, "current", True, "current", True, "resolved_both_exact"),
        (True, "current", True, "forged", False, "conflict"),
        (True, "forged", True, "current", False, "conflict"),
        (True, "not-a-digest", False, None, False, "invalid"),
        (True, "", False, None, False, "invalid"),
        (True, 42, False, None, False, "invalid"),
        (False, None, True, "", False, "invalid"),
        (False, None, True, {"digest": "current"}, False, "invalid"),
    ],
)
def test_mcp_runtime_status_server_fingerprint_sources_are_closed_and_fail_closed(
    nested_present,
    nested_value,
    top_present,
    top_value,
    expected_current,
    resolution_status,
):
    current = mcp_tool_schema_fingerprint(TOOLS)
    forged = "sha256:" + "0" * 64

    def materialize(value):
        if value == "current":
            return current
        if value == "forged":
            return forged
        return value

    class FingerprintDriftGovernance(_RuntimeGovRecorder):
        def api(self, method: str, path: str, data: dict | None = None) -> dict:
            result = super().api(method, path, data)
            if path == "/api/health":
                result = dict(result)
                result.pop("mcp_tool_schema_fingerprint", None)
                if top_present:
                    result["mcp_tool_schema_fingerprint"] = materialize(top_value)
                if nested_present:
                    result["mcp_tool_schema"] = {
                        "server_tool_schema_fingerprint": materialize(nested_value)
                    }
            return result

    status = _dispatcher(FingerprintDriftGovernance(), _Recorder()).dispatch(
        "runtime_status",
        {"project_id": "aming-claw"},
    )

    schema = status["mcp_tool_schema"]
    assert schema["status"] == ("current" if expected_current else "stale_client")
    assert schema["client_schema_fresh"] is expected_current
    assert schema["client_schema_fingerprint_fresh"] is expected_current
    assert schema["loaded_client_tool_schema_fingerprint"] == (
        mcp_tool_schema_fingerprint(TOOLS)
    )
    resolution = schema["server_fingerprint_resolution"]
    assert resolution["status"] == resolution_status
    assert resolution["nested_present"] is nested_present
    assert resolution["top_level_present"] is top_present
    assert schema["server_tool_schema_fingerprint"] == (
        current if expected_current else ""
    )


def test_current_mcp_schema_bump_marks_pre_current_full_view_client_stale():
    assert MCP_TOOL_SCHEMA_VERSION == "2026-09-01.1"
    reconcile_properties = _tool_properties("graph_current_full_reconcile")
    assert reconcile_properties["response_view"]["enum"] == [
        "compact",
        "full",
    ]
    assert reconcile_properties["response_view"]["default"] == "compact"

    compatibility = mcp_tool_schema_compatibility(
        loaded_schema_version="2026-08-19.1",
        server_schema_version=MCP_TOOL_SCHEMA_VERSION,
        minimum_client_schema_version=MCP_TOOL_SCHEMA_VERSION,
    )

    assert compatibility["loaded_client_tool_schema_version"] == "2026-08-19.1"
    assert compatibility["server_tool_schema_version"] == "2026-09-01.1"
    assert compatibility["minimum_client_tool_schema_version"] == "2026-09-01.1"
    assert compatibility["client_schema_fresh"] is False
    assert compatibility["stale_client_possible"] is True


@pytest.mark.parametrize(
    ("loaded_fingerprint", "server_fingerprint"),
    [
        ("sha256:" + "0" * 64, "current"),
        ("", "current"),
        ("current", ""),
    ],
)
def test_mcp_schema_same_version_old_or_absent_fingerprint_is_not_current(
    loaded_fingerprint,
    server_fingerprint,
):
    current = mcp_tool_schema_fingerprint(TOOLS)
    compatibility = mcp_tool_schema_compatibility(
        loaded_schema_version=MCP_TOOL_SCHEMA_VERSION,
        server_schema_version=MCP_TOOL_SCHEMA_VERSION,
        minimum_client_schema_version=MCP_TOOL_SCHEMA_VERSION,
        loaded_schema_fingerprint=(
            current if loaded_fingerprint == "current" else loaded_fingerprint
        ),
        server_schema_fingerprint=(
            current if server_fingerprint == "current" else server_fingerprint
        ),
    )

    assert compatibility["client_schema_fresh"] is False
    assert compatibility["client_schema_fingerprint_fresh"] is False


def test_mcp_runtime_status_current_target_chain_mismatch_blocks_core():
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
    assert status["legacy_runtime_waivers"] == []
    assert "advanced_chain_ops_redeploy_or_restart" in status["recommended_actions"]


def test_mcp_runtime_status_legacy_chain_drift_has_nonblocking_waiver():
    governance = _LegacyRuntimeDriftGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["strict_ok"] is True
    assert status["severity"] == "ok"
    assert status["capabilities"]["core_runtime"] is True
    assert status["capabilities"]["advanced_chain_ops"] is True
    assert status["recommended_actions"] == []
    assert status["legacy_runtime_waivers"] == [
        {
            "id": "legacy_project_version_chain_drift",
            "scope": "legacy_project_version",
            "blocking": False,
            "capability": "core_runtime",
            "reason": (
                "legacy project_version/CHAIN_VERSION metadata differs from "
                "the current target version but target runtime evidence is clean"
            ),
            "evidence": {
                "legacy_chain_version": "legacy123",
                "target_chain_version": "new1234",
                "target_head": "new1234",
                "legacy_synced_with_target": True,
                "legacy_git_head": "new1234",
            },
        }
    ]


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
    assert "legacy/advanced runtime drift is waived" in status["summary"]
    assert {waiver["id"] for waiver in status["legacy_runtime_waivers"]} == {
        "advanced_runtime_version_mismatch",
        "legacy_project_version_chain_drift",
    }
    assert all(waiver["blocking"] is False for waiver in status["legacy_runtime_waivers"])
    advanced_waiver = next(
        waiver
        for waiver in status["legacy_runtime_waivers"]
        if waiver["id"] == "advanced_runtime_version_mismatch"
    )
    assert advanced_waiver["scope"] == "advanced_chain_ops/executor"
    assert advanced_waiver["evidence"]["sm_runtime_version"] == "old1234"
    assert "advanced_chain_ops_redeploy_or_restart" not in status["recommended_actions"]


def test_mcp_runtime_status_synced_stale_target_chain_uses_waivers():
    governance = _PostMergeRuntimeWaiverGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["strict_ok"] is True
    assert status["severity"] == "ok"
    assert status["usable"] is True
    assert status["capabilities"]["core_runtime"] is True
    assert status["capabilities"]["advanced_chain_ops"] is False
    assert status["capabilities"]["executor"] is False
    assert status["recommended_actions"] == []
    waiver_ids = {waiver["id"] for waiver in status["legacy_runtime_waivers"]}
    assert waiver_ids == {
        "target_project_version_chain_drift",
        "legacy_project_version_chain_drift",
        "advanced_runtime_version_mismatch",
    }
    assert all(waiver["blocking"] is False for waiver in status["legacy_runtime_waivers"])

    target_waiver = next(
        waiver
        for waiver in status["legacy_runtime_waivers"]
        if waiver["id"] == "target_project_version_chain_drift"
    )
    assert target_waiver["evidence"]["target_head"] == _PostMergeRuntimeWaiverGovRecorder.head
    assert target_waiver["evidence"]["target_chain_version"] == "6b8c90a6"
    assert target_waiver["evidence"]["source"] == "trailer"
    assert target_waiver["evidence"]["synced_with_governance"] is True

    legacy_waiver = next(
        waiver
        for waiver in status["legacy_runtime_waivers"]
        if waiver["id"] == "legacy_project_version_chain_drift"
    )
    assert legacy_waiver["evidence"]["legacy_chain_version"] == (
        _PostMergeRuntimeWaiverGovRecorder.legacy_chain_version
    )
    assert legacy_waiver["evidence"]["legacy_git_head"] == _PostMergeRuntimeWaiverGovRecorder.head

    advanced_waiver = next(
        waiver
        for waiver in status["legacy_runtime_waivers"]
        if waiver["id"] == "advanced_runtime_version_mismatch"
    )
    assert advanced_waiver["evidence"]["gov_runtime_version"] == "7e8d2ee8"
    assert advanced_waiver["evidence"]["sm_runtime_version"] == "fa63faf4"


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


def test_mcp_manager_start_schema_accepts_project_binding():
    properties = _tool_properties("manager_start")

    assert properties["project_id"] == {
        "type": "string",
        "description": "Intended project binding. Defaults to the project bound to this MCP dispatcher.",
    }
    assert "ServiceManager HTTP health" in properties["health_wait_seconds"]["description"]


def test_mcp_manager_start_healthy_manager_succeeds_without_executor():
    governance = _Recorder()
    manager = _Recorder()
    dispatcher = ToolDispatcher(
        api_fn=governance.api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=manager.api,
        workspace="/repo",
        project_id="ac-dev",
    )

    result = dispatcher.dispatch("manager_start", {})

    assert result == {
        "ok": True,
        "action": "already_running",
        "project_id": "ac-dev",
        "manager": {
            "ok": True,
            "method": "GET",
            "path": "/api/manager/health",
            "data": None,
        },
        "executor_state": "waived_or_degraded",
    }
    assert manager.calls == [("GET", "/api/manager/health", None)]


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
        return SimpleNamespace(
            returncode=1,
            stdout="Manager healthy.\n  executor_state: waived_or_degraded\n",
            stderr="",
        )

    monkeypatch.setattr(mcp_tools.subprocess, "run", fake_run)

    result = dispatcher.dispatch(
        "manager_start",
        {"project_id": "ac-dev", "health_wait_seconds": 7},
    )

    assert result["ok"] is True
    assert result["project_id"] == "ac-dev"
    assert result["script"] == "start-manager.sh"
    assert result["platform"] == "darwin"
    assert result["executor_state"] == "waived_or_degraded"
    assert result["launcher_degraded"] is True
    assert calls[0][0] == [
        "bash",
        "/repo/scripts/start-manager.sh",
        "--project",
        "ac-dev",
        "--health-wait-seconds",
        "7",
    ]
    assert manager.calls == [
        ("GET", "/api/manager/health", None),
        ("GET", "/api/manager/health", None),
    ]


def test_mcp_manager_start_uses_dispatcher_bound_project_when_omitted(monkeypatch):
    class _BoundGovernance(_Recorder):
        project_id = "ac-dev"

    governance = _BoundGovernance()
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
    monkeypatch.setattr(
        mcp_tools.os.path,
        "exists",
        lambda path: path == "/repo/scripts/start-manager.sh",
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="Manager healthy.\n  executor_state: waived_or_degraded\n",
            stderr="",
        )

    monkeypatch.setattr(mcp_tools.subprocess, "run", fake_run)

    result = dispatcher.dispatch("manager_start", {"health_wait_seconds": 5})

    assert result["ok"] is True
    assert result["project_id"] == "ac-dev"
    assert calls[0][2:4] == ["--project", "ac-dev"]


def test_mcp_manager_start_reports_manager_health_failure(monkeypatch):
    governance = _Recorder()
    manager = _Recorder()

    def manager_api(method: str, path: str, data: dict | None = None) -> dict:
        manager.calls.append((method, path, data))
        return {"ok": False, "error": "connection refused"}

    dispatcher = ToolDispatcher(
        api_fn=governance.api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=manager_api,
        workspace="/repo",
        project_id="ac-dev",
    )
    monkeypatch.setattr(mcp_tools.sys, "platform", "darwin")
    monkeypatch.setattr(
        mcp_tools.os.path,
        "exists",
        lambda path: path == "/repo/scripts/start-manager.sh",
    )
    monkeypatch.setattr(
        mcp_tools.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ServiceManager health did not become healthy",
        ),
    )

    result = dispatcher.dispatch("manager_start", {})

    assert result["ok"] is False
    assert result["error"] == "manager_health_unavailable_after_start"
    assert "ServiceManager HTTP health" in result["message"]
    assert "worker" not in result["message"].lower()


def test_mcp_manager_start_uses_powershell_project_argument(monkeypatch):
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
        workspace=r"C:\repo",
        project_id="aming-claw",
    )
    calls = []
    monkeypatch.setattr(mcp_tools.sys, "platform", "win32")
    monkeypatch.setattr(mcp_tools.os.path, "exists", lambda path: True)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="Manager healthy.\n  executor_state: waived_or_degraded\n",
            stderr="",
        )

    monkeypatch.setattr(mcp_tools.subprocess, "run", fake_run)

    result = dispatcher.dispatch("manager_start", {"project_id": "ac-dev"})

    assert result["ok"] is True
    assert calls[0] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        r"C:\repo/scripts/start-manager.ps1",
        "-Project",
        "ac-dev",
        "-HealthWaitSeconds",
        "90",
    ]


def test_finish_attestation_schema_and_missing_identity_are_zero_http():
    for registry in (governance_mcp_server.TOOLS, mcp_tools.TOOLS):
        schema = next(
            item["inputSchema"]
            for item in registry
            if item["name"] == "runtime_context_finish_time_worker_attestation"
        )
        assert {
            "project_id",
            "runtime_context_id",
            "harness_type",
        }.issubset(schema["required"])

    cases = [
        (
            {"project_id": "aming-claw", "harness_type": "codex"},
            ["runtime_context_id"],
        ),
        (
            {"runtime_context_id": "mfrctx-demo", "harness_type": "codex"},
            ["project_id"],
        ),
        (
            {"project_id": "aming-claw", "runtime_context_id": "mfrctx-demo"},
            ["harness_type"],
        ),
    ]
    for args, missing_fields in cases:
        recorder = _Recorder()
        result = _dispatcher(recorder).dispatch(
            "runtime_context_finish_time_worker_attestation",
            args,
        )
        assert result["ok"] is False
        assert result["error"] == "invalid_request"
        assert result["code"] == "mcp_tool_required_arguments_missing"
        assert result["field"] == missing_fields[0]
        assert result["missing_fields"] == missing_fields
        assert result["zero_write_rejection"] is True
        assert result["http_request_performed"] is False
        assert recorder.calls == []
def test_mcp_tools_ac_endpoint_is_dev_only(monkeypatch):
    monkeypatch.setenv("AMING_CLAW_MCP_PROJECT_ID", "aming-claw")
    monkeypatch.delenv("GOVERNANCE_URL", raising=False)
    tools = ToolDispatcher(lambda *_args, **_kwargs: {}, None)
    assert tools._governance_url() == "http://127.0.0.1:40008"
    monkeypatch.setenv("GOVERNANCE_URL", "http://127.0.0.1:40000")
    with pytest.raises(ValueError, match="40008"):
        tools._governance_url()
