"""Tests for agent.cli — AC1, AC8."""

import os
import hashlib
import json
import re
import subprocess
import sys
import types
from pathlib import Path
import pytest

try:
    from click.testing import CliRunner
    from agent.cli import main
    from agent.plugin_installer import (
        configure_codex_plugin,
        codex_cache_plugin_root,
        install_codex_marketplace,
        install_codex_plugin_cache,
        plugin_root_for,
    )
    HAS_CLICK = True
except ImportError:
    HAS_CLICK = False

pytestmark = pytest.mark.skipif(not HAS_CLICK, reason="click not installed")


class _GovernanceProbeResponse:
    def __init__(self, *, url, body, headers=None, status=200, expected_limit=None):
        self.url = url
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self.status = status
        self.expected_limit = expected_limit

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read(self, limit):
        if self.expected_limit is not None:
            assert limit == self.expected_limit
        return self.body[:limit]


@pytest.mark.parametrize(
    "mode",
    [
        "redirect",
        "oversize_declared",
        "oversize_body",
        "non_json",
        "non_200",
    ],
)
def test_governance_probe_transport_fails_closed(monkeypatch, mode):
    import agent.cli as cli

    max_bytes = 16
    url = "http://127.0.0.1:40000/api/health"
    headers = {"Content-Type": "application/json"}
    body = b'{"ok":true}'
    status = 200
    final_url = url
    if mode == "redirect":
        final_url = "http://127.0.0.1:40000/api/other"
    elif mode == "oversize_declared":
        headers["Content-Length"] = str(max_bytes + 1)
    elif mode == "oversize_body":
        body = b"{" + (b" " * max_bytes)
    elif mode == "non_json":
        headers["Content-Type"] = "text/plain"
    elif mode == "non_200":
        status = 503

    captured = {}
    response = _GovernanceProbeResponse(
        url=final_url,
        body=body,
        headers=headers,
        status=status,
        expected_limit=max_bytes + 1,
    )

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return types.SimpleNamespace(
            open=lambda request, *, timeout: (
                captured.update(request=request, timeout=timeout) or response
            )
        )

    monkeypatch.setattr(cli.urllib.request, "build_opener", build_opener)

    assert (
        cli._strict_local_governance_json_probe(
            40000,
            "/api/health",
            timeout=0.25,
            max_bytes=max_bytes,
        )
        is None
    )
    assert captured["request"].full_url == url
    assert captured["timeout"] == 0.25
    proxy = next(
        handler
        for handler in captured["handlers"]
        if isinstance(handler, cli.urllib.request.ProxyHandler)
    )
    assert proxy.proxies == {}
    assert any(
        isinstance(handler, cli._GovernanceProbeNoRedirect)
        for handler in captured["handlers"]
    )


def test_governance_probe_transport_accepts_only_exact_bounded_json(monkeypatch):
    import agent.cli as cli

    encoded = b'{"status":"ok"}'
    url = "http://127.0.0.1:40000/api/health"

    response = _GovernanceProbeResponse(
        url=url,
        body=encoded,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(encoded)),
        },
        expected_limit=cli._GOVERNANCE_PROBE_HEALTH_BYTES + 1,
    )

    monkeypatch.setattr(
        cli.urllib.request,
        "build_opener",
        lambda *_handlers: types.SimpleNamespace(
            open=lambda *_args, **_kwargs: response
        ),
    )

    assert cli._probe_governance(40000) == {"status": "ok"}


def test_observer_run_dry_run_emits_route_bound_invocation():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "planned"
    assert payload["execute"] is False
    evidence = payload["invocation"]
    assert evidence["schema_version"] == "ai_invocation_result.v1"
    assert evidence["backend_mode"] == "codex_cli"
    assert evidence["calls_models"] is False
    assert evidence["route_prompt_contract"]["route_context_hash"] == "sha256:route"
    assert evidence["route_prompt_contract"]["prompt_contract_id"] == "rprompt-test"
    assert evidence["route_alert_ack"]["status"] == "acknowledged"
    assert evidence["raw_output_stored"] is False


def test_observer_run_passes_timeout_and_early_progress_to_runtime(monkeypatch):
    seen = {}

    def fake_run_observer(request, *, execute=False):
        seen["timeout_sec"] = request.timeout_sec
        seen["early_progress_timeout_sec"] = request.early_progress_timeout_sec
        return {
            "ok": True,
            "schema_version": "observer_run.v1",
            "status": "planned",
            "execute": execute,
            "invocation": {
                "calls_models": False,
                "auth_status": "not_invoked",
                "backend_mode": request.backend_mode,
            },
        }

    monkeypatch.setattr("agent.observer_runtime.run_observer", fake_run_observer)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--timeout-sec",
            "7",
            "--early-progress-timeout-sec",
            "3",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["timeout_sec"] == 7
    assert seen["early_progress_timeout_sec"] == 3.0


def test_runtime_context_current_cli_calls_current_state_endpoint(monkeypatch):
    calls = []

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        return 200, {
            "ok": True,
            "view": "worker_view",
            "runtime_context_id": "mfrctx-cli",
            "runtime_context_service": {
                "views": {
                    "worker_view": {
                        "schema_version": "runtime_context.worker_view.v1",
                        "privacy_boundary": {"raw_private_context_exposed": False},
                    }
                }
            },
        }

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "runtime-context",
            "current",
            "--project-id",
            "aming-claw",
            "--runtime-context-id",
            "mfrctx-cli",
            "--fence-token",
            "fence-cli",
            "--parent-task-id",
            "AC-PARENT",
            "--graph-trace-id",
            "gqt-cli",
            "--view",
            "all",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["runtime_context_service"]["views"]["worker_view"][
        "schema_version"
    ] == "runtime_context.worker_view.v1"
    assert calls == [
        (
            "GET",
            "http://localhost:40000/api/graph-governance/aming-claw/"
            "parallel-branches/runtime-contexts/mfrctx-cli/current-state?"
            "fence_token=fence-cli&parent_task_id=AC-PARENT&view=all&graph_trace_id=gqt-cli",
            None,
            30.0,
        )
    ]


def test_observer_run_rejects_missing_route_identity():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "",
            "--prompt-contract-id",
            "rprompt-test",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "route_context_hash" in payload["missing"]
    assert payload["execute"] is False


def test_observer_run_execute_codex_requires_one_hop_dispatch_gate():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--backend-mode",
            "codex_cli",
            "--execute",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["status"] == "rejected"
    assert payload["execute"] is True
    gate = payload["one_hop_execution_gate"]
    assert gate["required"] is True
    assert gate["allowed"] is False
    assert "dispatch_gate" in gate["missing"]
    assert "invocation" not in payload


def test_observer_run_execute_fixture_does_not_require_one_hop_dispatch_gate():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--provider",
            "fixture",
            "--backend-mode",
            "fixture",
            "--execute",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["one_hop_execution_gate"]["required"] is False
    assert payload["invocation"]["calls_models"] is False


def test_observer_run_execute_rejects_incomplete_dispatch_gate(tmp_path):
    gate_file = tmp_path / "dispatch-gate.json"
    gate_file.write_text(
        json.dumps(
            {
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-test",
                "owned_files": ["agent/observer_runtime.py"],
                "dirty_scope_check": {"dirty_scope_exact_match": True},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--backend-mode",
            "codex_cli",
            "--dispatch-gate-file",
            str(gate_file),
            "--execute",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    gate = payload["one_hop_execution_gate"]
    assert gate["allowed"] is False
    for field in (
        "branch",
        "worktree",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "fence_token",
    ):
        assert field in gate["error"]


def _observer_poll_command(command_id="cmd-route-1"):
    return {
        "command_id": command_id,
        "command_type": "execute_backlog_row",
        "status": "claimed",
        "payload": {
            "backlog_id": "AC-ROUTE-HANDOFF",
            "route_id": "route-20260603-test",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-test",
            "visible_injection_manifest_hash": "sha256:visible",
        },
    }


def _observer_poll_timeline_calls(calls):
    return [call for call in calls if call[1].endswith("/api/task/aming-claw/timeline")]


def _observer_poll_heartbeat_calls(calls):
    return [call for call in calls if "/observer-sessions/obs-1/heartbeat" in call[1]]


def test_observer_poll_registers_claims_and_plans_without_service_manager(monkeypatch):
    calls = []

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        if url.endswith("/observer-sessions/register"):
            return 201, {
                "ok": True,
                "observer_session_id": "obs-1",
                "session_id": "obs-1",
                "session_token": "secret-token",
            }
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/next"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": _observer_poll_command(),
                "empty": False,
            }
        if url.endswith("/api/task/aming-claw/timeline"):
            return 200, {"ok": True, "event_id": len(_observer_poll_timeline_calls(calls))}
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--governance-url",
            "http://governance.local",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "planned"
    assert payload["registered_session"]["observer_session_id"] == "obs-1"
    assert "session_token" not in payload["registered_session"]
    poll = payload["observer_poll"]
    assert poll["service_manager_required"] is False
    assert poll["executor_worker_required"] is False
    assert poll["uses_task_create"] is False
    assert poll["payload_free_reminder"] is True
    assert poll["reminder_payload_required"] is False
    assert poll["observer_command_id"] == "cmd-route-1"
    assert poll["route_identity"]["route_context_hash"] == "sha256:route"
    assert poll["observer_run"]["invocation"]["calls_models"] is False

    register_payload = calls[0][2]
    assert register_payload["capabilities"]["command_types"] == ["execute_backlog_row"]
    assert _observer_poll_heartbeat_calls(calls)
    next_call = next(call for call in calls if call[1].endswith("/observer-commands/next"))
    assert next_call[2]["session_id"] == "obs-1"
    assert next_call[2]["session_token"] == "secret-token"
    timeline_calls = _observer_poll_timeline_calls(calls)
    assert [call[2]["event_type"] for call in timeline_calls] == [
        "observer_poll_claimed",
        "observer_poll_planned",
    ]
    assert [call[2]["task_id"] for call in timeline_calls] == ["cmd-route-1", "cmd-route-1"]
    planned_payload = timeline_calls[-1][2]["payload"]
    assert planned_payload["observer_command_id"] == "cmd-route-1"
    assert planned_payload["backlog_id"] == "AC-ROUTE-HANDOFF"
    assert planned_payload["route_id"] == "route-20260603-test"
    assert planned_payload["route_context_hash"] == "sha256:route"
    assert planned_payload["prompt_contract_id"] == "rprompt-test"
    assert planned_payload["visible_injection_manifest_hash"] == "sha256:visible"
    assert planned_payload["execute"] is False
    assert planned_payload["calls_models"] is False
    assert planned_payload["service_manager_required"] is False
    assert planned_payload["executor_worker_required"] is False
    assert planned_payload["uses_task_create"] is False
    assert planned_payload["payload_free_reminder"] is True
    assert planned_payload["reminder_payload_required"] is False


def test_observer_poll_execute_keeps_session_alive_during_child_run(monkeypatch):
    calls = []

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/claim"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": _observer_poll_command("cmd-execute"),
                "empty": False,
            }
        if url.endswith("/api/task/aming-claw/timeline"):
            return 200, {"ok": True, "event_id": len(_observer_poll_timeline_calls(calls))}
        raise AssertionError(f"unexpected call: {method} {url}")

    def fake_run_observer(request, *, execute=False):
        assert execute is True
        assert callable(request.heartbeat_callback)
        assert request.early_progress_timeout_sec == 4.0
        heartbeat = request.heartbeat_callback()
        assert heartbeat["ok"] is True
        assert heartbeat["phase"] == "execute_child"
        return {
            "ok": True,
            "schema_version": "observer_run.v1",
            "status": "completed",
            "execute": execute,
            "invocation": {
                "calls_models": True,
                "auth_status": "cli_auth_unknown",
                "backend_mode": request.backend_mode,
            },
        }

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    monkeypatch.setattr("agent.observer_runtime.run_observer", fake_run_observer)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--session-id",
            "obs-1",
            "--session-token",
            "secret-token",
            "--command-id",
            "cmd-execute",
            "--execute",
            "--early-progress-timeout-sec",
            "4",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert payload["loop"]["heartbeat_count"] == 2
    assert len(_observer_poll_heartbeat_calls(calls)) == 2
    assert payload["heartbeats"][-1]["phase"] == "execute_child"


def test_observer_poll_can_complete_planned_command(monkeypatch):
    calls = []

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/claim"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": _observer_poll_command("cmd-complete"),
                "empty": False,
            }
        if url.endswith("/observer-commands/cmd-complete/complete"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": {"command_id": "cmd-complete", "status": "completed"},
            }
        if url.endswith("/api/task/aming-claw/timeline"):
            return 200, {"ok": True, "event_id": len(_observer_poll_timeline_calls(calls))}
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--governance-url",
            "http://governance.local",
            "--session-id",
            "obs-1",
            "--session-token",
            "secret-token",
            "--command-id",
            "cmd-complete",
            "--complete-planned",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["loop"]["heartbeat_count"] == 1
    assert payload["completion"]["ok"] is True
    assert payload["completion"]["observer_command_id"] == "cmd-complete"
    complete_payload = next(
        call[2]
        for call in calls
        if call[1].endswith("/observer-commands/cmd-complete/complete")
    )
    assert complete_payload["session_id"] == "obs-1"
    assert complete_payload["session_token"] == "secret-token"
    assert complete_payload["result"]["status"] == "planned"
    assert complete_payload["result"]["route_id"] == "route-20260603-test"
    assert complete_payload["result"]["visible_injection_manifest_hash"] == "sha256:visible"
    assert complete_payload["result"]["service_manager_required"] is False
    assert complete_payload["result"]["executor_worker_required"] is False
    assert complete_payload["result"]["uses_task_create"] is False
    timeline_calls = _observer_poll_timeline_calls(calls)
    assert [call[2]["event_type"] for call in timeline_calls] == [
        "observer_poll_claimed",
        "observer_poll_planned",
        "observer_poll_completed",
    ]
    assert [call[2]["task_id"] for call in timeline_calls] == [
        "cmd-complete",
        "cmd-complete",
        "cmd-complete",
    ]
    completed_payload = timeline_calls[-1][2]["payload"]
    assert completed_payload["observer_command_id"] == "cmd-complete"
    assert completed_payload["route_id"] == "route-20260603-test"
    assert completed_payload["route_context_hash"] == "sha256:route"
    assert completed_payload["prompt_contract_id"] == "rprompt-test"
    assert completed_payload["visible_injection_manifest_hash"] == "sha256:visible"
    assert completed_payload["execute"] is False
    assert completed_payload["calls_models"] is False
    assert completed_payload["service_manager_required"] is False
    assert completed_payload["executor_worker_required"] is False
    assert completed_payload["uses_task_create"] is False


def test_observer_poll_fails_terminal_blocked_command(monkeypatch):
    calls = []

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/claim"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": _observer_poll_command("cmd-terminal"),
                "empty": False,
            }
        if url.endswith("/observer-commands/cmd-terminal/fail"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": {"command_id": "cmd-terminal", "status": "failed"},
            }
        if url.endswith("/api/task/aming-claw/timeline"):
            return 200, {"ok": True, "event_id": len(_observer_poll_timeline_calls(calls))}
        raise AssertionError(f"unexpected call: {method} {url}")

    def fake_build_plan(request, *, execute=False):
        assert execute is True
        return {
            "ok": False,
            "schema_version": "observer_poll.v1",
            "status": "blocked",
            "observer_command_id": "cmd-terminal",
            "backlog_id": "AC-ROUTE-HANDOFF",
            "execute": True,
            "calls_models": False,
            "terminal_dispatch_blocker": True,
            "command_projection_status": "failed",
            "canonical_contract_state": "blocked",
            "route_identity": {
                "route_id": "route-20260603-test",
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-test",
                "visible_injection_manifest_hash": "sha256:visible",
            },
            "terminal_contract_projection": {
                "command_projection_status": "failed",
                "canonical_contract_state": "blocked",
                "divergence_reason": "codex_cli_timeout_no_finish",
            },
            "failure_evidence": {
                "blocker_id": "codex_cli_timeout_no_finish",
                "observer_command_id": "cmd-terminal",
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-test",
                "visible_injection_manifest_hash": "sha256:visible",
            },
        }

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    monkeypatch.setattr("agent.observer_runtime.build_observer_poll_plan", fake_build_plan)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--session-id",
            "obs-1",
            "--session-token",
            "secret-token",
            "--command-id",
            "cmd-terminal",
            "--execute",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert payload["failure"]["ok"] is True
    assert payload["failure"]["observer_command_id"] == "cmd-terminal"
    assert payload["loop"]["stop_reason"] == "command_failed"
    fail_payload = next(
        call[2] for call in calls if call[1].endswith("/observer-commands/cmd-terminal/fail")
    )
    assert fail_payload["result"]["command_projection_status"] == "failed"
    assert fail_payload["result"]["terminal_dispatch_blocker"] is True
    timeline_calls = _observer_poll_timeline_calls(calls)
    assert [call[2]["event_type"] for call in timeline_calls] == [
        "observer_poll_claimed",
        "observer_poll_planned",
        "observer_poll_failed",
    ]
    assert timeline_calls[-1][2]["payload"]["observer_command_id"] == "cmd-terminal"


def test_observer_poll_reports_empty_queue(monkeypatch):
    def fake_http(method, url, payload=None, *, timeout=30.0):
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/next"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": None,
                "empty": True,
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--session-id",
            "obs-1",
            "--session-token",
            "secret-token",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "empty"
    assert payload["empty"] is True
    assert payload["loop"]["stop_reason"] == "empty"
    assert payload["loop"]["empty_polls"] == 1
    assert payload["observer_poll"]["service_manager_required"] is False


def test_observer_poll_accepts_reconnect_raw_claim_response(monkeypatch):
    calls = []

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/next"):
            command = _observer_poll_command("cmd-owned")
            command["claimed_by_session_id"] = "obs-1"
            return 200, command
        if url.endswith("/api/task/aming-claw/timeline"):
            return 200, {"ok": True, "event_id": len(_observer_poll_timeline_calls(calls))}
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--session-id",
            "obs-1",
            "--session-token",
            "secret-token",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["claim"]["observer_command_id"] == "cmd-owned"
    assert payload["observer_poll"]["observer_command_id"] == "cmd-owned"
    assert payload["observer_poll"]["status"] == "planned"
    timeline_calls = _observer_poll_timeline_calls(calls)
    assert [call[2]["event_type"] for call in timeline_calls] == [
        "observer_poll_claimed",
        "observer_poll_planned",
    ]
    assert [call[2]["task_id"] for call in timeline_calls] == ["cmd-owned", "cmd-owned"]
    assert timeline_calls[-1][2]["payload"]["observer_command_id"] == "cmd-owned"


def test_observer_poll_watch_claims_notified_command_and_completes_planned(monkeypatch):
    calls = []
    pending = [_observer_poll_command("cmd-notified")]
    pending[0]["status"] = "notified"

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/next"):
            if pending:
                command = pending.pop(0)
                command["status"] = "claimed"
                command["claimed_by_session_id"] = "obs-1"
                return 200, {
                    "ok": True,
                    "project_id": "aming-claw",
                    "observer_session_id": "obs-1",
                    "command": command,
                    "empty": False,
                }
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": None,
                "empty": True,
            }
        if url.endswith("/observer-commands/cmd-notified/complete"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": {"command_id": "cmd-notified", "status": "completed"},
            }
        if url.endswith("/api/task/aming-claw/timeline"):
            return 200, {"ok": True, "event_id": len(_observer_poll_timeline_calls(calls))}
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--session-id",
            "obs-1",
            "--session-token",
            "secret-token",
            "--watch",
            "--idle-timeout-sec",
            "0",
            "--poll-interval-sec",
            "0",
            "--complete-planned",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "empty"
    assert payload["loop"]["watch"] is True
    assert payload["loop"]["processed_count"] == 1
    assert payload["loop"]["empty_polls"] == 1
    assert payload["loop"]["heartbeat_count"] == 2
    assert payload["loop"]["stop_reason"] == "idle_timeout"
    assert payload["observer_polls"][0]["observer_command_id"] == "cmd-notified"
    assert payload["observer_polls"][0]["route_identity"]["route_context_hash"] == "sha256:route"
    assert payload["completion"]["ok"] is True
    assert payload["completions"][0]["observer_command_id"] == "cmd-notified"
    timeline_calls = _observer_poll_timeline_calls(calls)
    assert [call[2]["event_type"] for call in timeline_calls] == [
        "observer_poll_claimed",
        "observer_poll_planned",
        "observer_poll_completed",
    ]
    completed_payload = timeline_calls[-1][2]["payload"]
    assert completed_payload["observer_command_id"] == "cmd-notified"
    assert completed_payload["route_context_hash"] == "sha256:route"
    assert completed_payload["payload_free_reminder"] is True
    assert completed_payload["reminder_payload_required"] is False
    called_urls = [call[1] for call in calls]
    assert not any("task_create" in url or "executor" in url for url in called_urls)


def test_observer_poll_watch_empty_queue_exits_after_bounded_idle(monkeypatch):
    calls = []

    def fake_http(method, url, payload=None, *, timeout=30.0):
        calls.append((method, url, payload, timeout))
        if url.endswith("/observer-sessions/obs-1/heartbeat"):
            return 200, {
                "ok": True,
                "observer_session_id": "obs-1",
                "heartbeat_interval_sec": 30,
            }
        if url.endswith("/observer-commands/next"):
            return 200, {
                "ok": True,
                "project_id": "aming-claw",
                "observer_session_id": "obs-1",
                "command": None,
                "empty": True,
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("agent.cli._http_json", fake_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "poll",
            "--project-id",
            "aming-claw",
            "--session-id",
            "obs-1",
            "--session-token",
            "secret-token",
            "--watch",
            "--idle-timeout-sec",
            "0",
            "--poll-interval-sec",
            "0",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "empty"
    assert payload["loop"]["watch"] is True
    assert payload["loop"]["processed_count"] == 0
    assert payload["loop"]["empty_polls"] == 1
    assert payload["loop"]["stop_reason"] == "idle_timeout"
    assert payload["observer_poll"]["empty"] is True
    assert len(_observer_poll_heartbeat_calls(calls)) == 1
    assert not any("task_create" in call[1] or "executor" in call[1] for call in calls)


DOGFOOD_BACKLOG_ID = "AC-OBSERVER-CLI-LAUNCHER-JUDGE-OBSERVER-SUBAGENT-20260602"
DOGFOOD_ROUTE_CONTEXT_HASH = "sha256:206c6621998609402a7f4276bf33eb9b6d9468f2096116505d388670dab6e352"
DOGFOOD_PROMPT_CONTRACT_ID = "rprompt-205a50783038d2f0"
DOGFOOD_VISIBLE_MANIFEST_HASH = "sha256:0603ba125fff6a7fa5267872d3e4e93ec090f6456f8a656ee0e16c77460b7b23"


def _dogfood_args(
    tmp_path,
    *,
    main_worktree=None,
    workspace_root=None,
    worktree_root="worktrees",
    base_commit="base123",
    target_head_commit="head123",
):
    main = Path(main_worktree or (tmp_path / "main"))
    main.mkdir(parents=True, exist_ok=True)
    workspace_root = Path(workspace_root or (tmp_path / "workers"))
    worker_worktree = (
        workspace_root
        / worktree_root
        / "worker-a"
        / f"{DOGFOOD_BACKLOG_ID.lower()}-attempt-2"
    )
    evidence_file = tmp_path / "dogfood-branch-runtime-evidence.json"
    evidence_file.write_text(
        json.dumps(
            {
                "schema_version": "mf_subagent_branch_runtime.v1",
                "status": "allocated",
                "ok": True,
                "present": True,
                "registered": True,
                "allocation_required": False,
                "source_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
                "registration_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
                "registration_source": "parallel_branch_allocate",
                "runtime_context_id": "mfrctx-dogfood-cli",
                "context": {
                    "project_id": "aming-claw",
                    "runtime_context_id": "mfrctx-dogfood-cli",
                    "task_id": DOGFOOD_BACKLOG_ID,
                    "parent_task_id": DOGFOOD_BACKLOG_ID,
                    "backlog_id": DOGFOOD_BACKLOG_ID,
                    "worker_id": "worker-a",
                    "attempt": 2,
                    "branch_ref": f"refs/heads/dogfood/{DOGFOOD_BACKLOG_ID.lower()}-attempt-2",
                    "worktree_path": str(worker_worktree),
                    "fence_token": "fence-dogfood-test",
                    "base_commit": base_commit,
                    "target_head_commit": target_head_commit,
                    "merge_queue_id": "mq-dogfood-test",
                },
            }
        ),
        encoding="utf-8",
    )
    return [
        "observer",
        "dogfood",
        "--project-id",
        "aming-claw",
        "--backlog-id",
        DOGFOOD_BACKLOG_ID,
        "--route-context-hash",
        DOGFOOD_ROUTE_CONTEXT_HASH,
        "--prompt-contract-id",
        DOGFOOD_PROMPT_CONTRACT_ID,
        "--prompt-contract-hash",
        "sha256:prompt-contract",
        "--route-token-ref",
        "route-token-ref",
        "--route-id",
        "route-20260602-ebc022240d",
        "--precheck-run-id",
        "precheck-judgment-plan-topology-f27328488fb9",
        "--visible-injection-manifest-hash",
        DOGFOOD_VISIBLE_MANIFEST_HASH,
        "--provider",
        "openai",
        "--backend-mode",
        "codex_cli",
        "--main-worktree",
        str(main),
        "--workspace-root",
        str(workspace_root),
        "--owned-file",
        "agent/observer_runtime.py",
        "--owned-file",
        "agent/cli.py",
        "--task-id",
        DOGFOOD_BACKLOG_ID,
        "--worker-id",
        "worker-a",
        "--attempt",
        "2",
        "--worktree-root",
        worktree_root,
        "--branch-prefix",
        "dogfood",
        "--merge-queue-id",
        "mq-dogfood-test",
        "--fence-token",
        "fence-dogfood-test",
        "--branch-runtime-registration-ref",
        "/api/graph-governance/aming-claw/parallel-branches/allocate",
        "--branch-runtime-evidence-file",
        str(evidence_file),
        "--graph-trace-id",
        "gqt-20260602-testtrace",
        "--base-commit",
        base_commit,
        "--target-head-commit",
        target_head_commit,
        "--json-output",
    ]


def _without_option(args, option):
    result = list(args)
    index = result.index(option)
    del result[index : index + 2]
    return result


def _replace_option(args, option, value):
    result = list(args)
    index = result.index(option)
    result[index + 1] = value
    return result


def test_observer_dogfood_dry_run_generates_valid_gate_and_plan_without_model_call(tmp_path):
    runner = CliRunner()

    result = runner.invoke(main, _dogfood_args(tmp_path))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "planned"
    assert payload["execute"] is False
    assert payload["calls_models"] is False
    gate = payload["dispatch_gate_validation"]
    assert gate["schema_version"] == "mf_subagent_dispatch_gate.v1"
    assert gate["allowed"] is True
    assert gate["route_context_hash"] == DOGFOOD_ROUTE_CONTEXT_HASH
    assert gate["prompt_contract_id"] == DOGFOOD_PROMPT_CONTRACT_ID
    assert gate["merge_queue_id"] == "mq-dogfood-test"
    assert gate["fence_token"] == "fence-dogfood-test"
    assert gate["isolated_worktree"] is True
    assert set(gate["owned_files"]) == {"agent/observer_runtime.py", "agent/cli.py"}
    assert payload["dispatch_gate"]["prelaunch_graph_context"]["trace_ids"] == [
        "gqt-20260602-testtrace"
    ]
    assert (
        payload["dispatch_gate"]["prelaunch_graph_context"][
            "counts_as_worker_graph_trace_evidence"
        ]
        is False
    )
    assert payload["dispatch_gate_validation"]["dispatch_graph_obligation"][
        "finish_gate_requires_worker_graph_trace"
    ] is True
    assert payload["dispatch_gate"]["route_evidence"]["visible_injection_manifest_hash"] == DOGFOOD_VISIBLE_MANIFEST_HASH
    observer_run = payload["observer_run"]
    assert observer_run["status"] == "planned"
    evidence = observer_run["invocation"]
    assert evidence["calls_models"] is False
    assert evidence["auth_status"] == "not_invoked"
    assert evidence["route_prompt_contract"]["route_context_hash"] == DOGFOOD_ROUTE_CONTEXT_HASH


def test_observer_dogfood_hydrates_runtime_contract_service_after_db_miss(
    monkeypatch,
    tmp_path,
):
    from agent.governance.parallel_branch_runtime import (
        BranchTaskRuntimeContext,
        STATE_WORKTREE_READY,
        branch_runtime_allocation_evidence,
    )

    import agent.observer_runtime as observer_runtime

    runner = CliRunner()
    persisted_worktree = (
        tmp_path
        / "workers"
        / "persisted-worktrees"
        / "persisted-worker"
        / f"{DOGFOOD_BACKLOG_ID.lower()}-attempt-2"
    )
    persisted_context = BranchTaskRuntimeContext(
        project_id="aming-claw",
        task_id=DOGFOOD_BACKLOG_ID,
        runtime_context_id="mfrctx-dogfood-cli",
        backlog_id=DOGFOOD_BACKLOG_ID,
        root_task_id=DOGFOOD_BACKLOG_ID,
        stage_task_id=DOGFOOD_BACKLOG_ID,
        stage_type="observer_dogfood",
        agent_id="persisted-observer-owner",
        allocation_owner="persisted-observer-owner",
        worker_id="persisted-worker",
        worker_slot_id="persisted-worker",
        fence_token="fence-dogfood-test",
        branch_ref=f"refs/heads/dogfood/{DOGFOOD_BACKLOG_ID.lower()}-persisted",
        worktree_id="wt-dogfood-persisted",
        worktree_path=str(persisted_worktree),
        base_commit="base123",
        target_head_commit="head123",
        merge_queue_id="mq-dogfood-test",
        status=STATE_WORKTREE_READY,
    )
    service_lookups = []
    db_lookups = []

    def fake_service_lookup(
        *,
        project_id,
        runtime_context_id,
        task_id="",
        parent_task_id="",
        fence_token="",
    ):
        service_lookups.append(
            (project_id, runtime_context_id, task_id, parent_task_id, fence_token)
        )
        return branch_runtime_allocation_evidence(
            persisted_context,
            source_ref=(
                "http://localhost:40000/api/graph-governance/aming-claw/"
                "parallel-branches/runtime-contexts/mfrctx-dogfood-cli/runtime-contract"
            ),
            registration_source="runtime_contract_service",
        )

    def fake_db_lookup(*, project_id, runtime_context_id="", task_id=""):
        db_lookups.append((project_id, runtime_context_id, task_id))
        return None

    monkeypatch.setattr(
        observer_runtime,
        "_runtime_text_get_service_branch_runtime_evidence",
        fake_service_lookup,
    )
    monkeypatch.setattr(
        observer_runtime,
        "_runtime_text_get_persisted_branch_context",
        fake_db_lookup,
    )
    args = _without_option(
        _dogfood_args(tmp_path),
        "--branch-runtime-evidence-file",
    )
    args[-1:-1] = ["--runtime-context-id", "mfrctx-dogfood-cli"]

    result = runner.invoke(main, args)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert service_lookups == [
        (
            "aming-claw",
            "mfrctx-dogfood-cli",
            DOGFOOD_BACKLOG_ID,
            DOGFOOD_BACKLOG_ID,
            "fence-dogfood-test",
        )
    ]
    assert db_lookups == []
    assert payload["ok"] is True
    assert payload["calls_models"] is False
    assert payload["runtime_context"]["runtime_context_id"] == "mfrctx-dogfood-cli"
    assert payload["runtime_context"]["allocation_owner"] == "persisted-observer-owner"
    assert payload["runtime_context"]["worker_id"] == "persisted-worker"
    assert payload["runtime_context"]["worktree_path"] == str(persisted_worktree)
    assert payload["dispatch_gate"]["allocation_owner"] == "persisted-observer-owner"
    assert payload["dispatch_gate"]["branch"] == persisted_context.branch_ref
    assert payload["dispatch_gate"]["worktree"] == str(persisted_worktree)
    assert payload["dispatch_gate"]["base_commit"] == "base123"
    assert payload["dispatch_gate"]["target_head_commit"] == "head123"
    assert payload["dispatch_gate"]["merge_queue_id"] == "mq-dogfood-test"
    assert payload["dispatch_gate"]["fence_token"] == "fence-dogfood-test"
    branch_runtime = payload["dispatch_gate"]["branch_runtime_evidence"]
    assert branch_runtime["registered"] is True
    assert branch_runtime["allocation_required"] is False
    assert branch_runtime["registration_source"] == "runtime_contract_service"
    assert payload["dispatch_gate_validation"]["allowed"] is True
    assert payload["runtime_text"]["runtime_context"]["allocation_owner"] == (
        "persisted-observer-owner"
    )


def test_observer_dogfood_requires_branch_runtime_registration_before_runtime_text(tmp_path):
    runner = CliRunner()
    args = _without_option(
        _without_option(_dogfood_args(tmp_path), "--branch-runtime-evidence-file"),
        "--branch-runtime-registration-ref",
    )

    result = runner.invoke(main, args)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["status"] == "allocation_required"
    assert payload["calls_models"] is False
    validation = payload["dispatch_gate_validation"]
    assert validation["allowed"] is False
    assert validation["allocation_required"] is True
    assert validation["branch_runtime_evidence"]["registered"] is False
    assert "branch runtime allocation is required" in validation["error"]
    assert "observer_run" not in payload


def test_observer_dogfood_rejects_marker_only_parallel_branch_allocate_registration_ref(tmp_path):
    runner = CliRunner()
    args = _without_option(_dogfood_args(tmp_path), "--branch-runtime-evidence-file")
    args = _replace_option(
        args,
        "--branch-runtime-registration-ref",
        "parallel-branches/allocate:req-dogfood-cli",
    )

    result = runner.invoke(main, args)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    branch_runtime = payload["dispatch_gate"]["branch_runtime_evidence"]
    assert payload["ok"] is False
    assert payload["status"] == "allocation_required"
    assert branch_runtime["registered"] is False
    assert branch_runtime["allocation_required"] is True
    assert branch_runtime["supplied_source_ref"] == "parallel-branches/allocate:req-dogfood-cli"
    assert "runtime_context_id" in branch_runtime["missing_fields"]
    assert payload["dispatch_gate_validation"]["branch_runtime_evidence"]["registered"] is False


def test_observer_dogfood_rejects_missing_visible_injection_manifest(tmp_path):
    runner = CliRunner()
    gate_output = tmp_path / "dispatch-gate.json"

    result = runner.invoke(
        main,
        _without_option(_dogfood_args(tmp_path), "--visible-injection-manifest-hash")[:-1]
        + ["--gate-output-path", str(gate_output), "--json-output"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["calls_models"] is False
    assert payload["route_identity_validation"]["allowed"] is False
    assert "visible_injection_manifest_hash" in payload["route_identity_validation"]["missing"]
    assert payload["gate_output_skipped"]["route_identity_allowed"] is False
    assert not gate_output.exists()
    assert "observer_run" not in payload


def test_observer_dogfood_rejects_missing_route_id(tmp_path):
    runner = CliRunner()

    result = runner.invoke(main, _without_option(_dogfood_args(tmp_path), "--route-id"))

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["calls_models"] is False
    assert payload["route_identity_validation"]["allowed"] is False
    assert "route_id" in payload["route_identity_validation"]["missing"]
    assert "observer_run" not in payload


def test_observer_dogfood_writes_gate_output_file(tmp_path):
    runner = CliRunner()
    gate_output = tmp_path / "evidence" / "dispatch-gate.json"

    result = runner.invoke(
        main,
        _dogfood_args(tmp_path)[:-1] + ["--gate-output-path", str(gate_output), "--json-output"],
    )

    assert result.exit_code == 0, result.output
    written = json.loads(gate_output.read_text(encoding="utf-8"))
    assert written["schema_version"] == "mf_subagent_dispatch_gate.v1"
    assert written["route_context_hash"] == DOGFOOD_ROUTE_CONTEXT_HASH
    assert written["prompt_contract_id"] == DOGFOOD_PROMPT_CONTRACT_ID
    assert written["prelaunch_graph_context"]["trace_ids"] == [
        "gqt-20260602-testtrace"
    ]
    assert (
        written["prelaunch_graph_context"]["counts_as_worker_graph_trace_evidence"]
        is False
    )
    assert written["dispatch_graph_obligation"]["finish_gate_requires_worker_graph_trace"] is True
    payload = json.loads(result.output)
    assert payload["gate_output"] == str(gate_output)


def test_observer_execute_gate_rejects_mismatched_route_identity(tmp_path):
    from agent.ai_invocation import RoutePromptContract
    from agent.observer_runtime import ObserverRunRequest, validate_one_hop_execution_gate

    gate = {
        "branch": "refs/heads/dogfood/test",
        "worktree": str(tmp_path / "worker"),
        "base_commit": "base123",
        "target_head_commit": "head123",
        "merge_queue_id": "mq-test",
        "fence_token": "fence-test",
        "route_context_hash": "sha256:gate-route",
        "prompt_contract_id": "rprompt-gate",
        "prompt_contract_hash": "sha256:gate-prompt",
        "route_token_ref": "rtok-gate",
        "owned_files": ["agent/observer_runtime.py"],
        "dirty_scope_check": {
            "status": "passed",
            "passed": True,
            "dirty_scope_exact_match": True,
            "owned_files": ["agent/observer_runtime.py"],
        },
    }
    request = ObserverRunRequest(
        project_id="aming-claw",
        backlog_id=DOGFOOD_BACKLOG_ID,
        route=RoutePromptContract(
            route_context_hash=DOGFOOD_ROUTE_CONTEXT_HASH,
            prompt_contract_id=DOGFOOD_PROMPT_CONTRACT_ID,
            prompt_contract_hash="sha256:request-prompt",
        ),
        backend_mode="codex_cli",
        workspace=str(tmp_path / "worker"),
        main_worktree=str(tmp_path / "main"),
        dispatch_gate=gate,
    )

    result = validate_one_hop_execution_gate(request)

    assert result["allowed"] is False
    assert "route identity" in result["error"]
    mismatch_fields = {item["field"] for item in result["route_identity_mismatches"]}
    assert mismatch_fields == {
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
    }


def test_observer_dogfood_execute_rejects_missing_materialized_worktree(tmp_path):
    runner = CliRunner()

    result = runner.invoke(main, _dogfood_args(tmp_path)[:-1] + ["--execute", "--json-output"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["status"] == "rejected"
    assert payload["execute"] is True
    assert payload["calls_models"] is False
    assert payload["auth_status"] == "not_invoked"
    assert payload["execute_preflight"]["allowed"] is False
    assert "isolated real git worktree" in payload["execute_preflight"]["error"]
    assert payload["execute_preflight"]["missing_fields"] == [
        "worktree_path.real_git_worktree"
    ]
    assert payload["execute_preflight"]["executable_worker_launch"]["command_display"]
    assert "observer_run" not in payload


def test_observer_dogfood_execute_rejects_existing_non_git_worker_directory(tmp_path):
    runner = CliRunner()
    args = _dogfood_args(tmp_path)
    plan_result = runner.invoke(main, args)
    assert plan_result.exit_code == 0, plan_result.output
    planned = json.loads(plan_result.output)
    worker_dir = Path(planned["runtime_context"]["worktree_path"])
    worker_dir.mkdir(parents=True)

    result = runner.invoke(main, args[:-1] + ["--execute", "--json-output"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["calls_models"] is False
    assert payload["execute_preflight"]["allowed"] is False
    status = payload["execute_preflight"]["worktree_status"]
    assert status["exists"] is True
    assert status["git_marker_exists"] is False
    assert status["is_git_worktree"] is False
    assert "observer_run" not in payload


def test_observer_dogfood_materialize_worktree_creates_real_git_worktree_without_model_call(tmp_path):
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    (repo / "README.md").write_text("dogfood materialization fixture\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial fixture",
        ],
        repo,
    )
    commit = _git(["rev-parse", "HEAD"], repo)
    args = _dogfood_args(
        tmp_path,
        main_worktree=repo,
        workspace_root=repo,
        worktree_root=".worktrees",
        base_commit=commit,
        target_head_commit=commit,
    )
    args = _replace_option(args, "--backend-mode", "codex_cli")

    result = runner.invoke(main, args[:-1] + ["--materialize-worktree", "--json-output"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["calls_models"] is False
    assert payload["worktree_materialization"]["materialized"] is True
    worker_path = Path(payload["runtime_context"]["worktree_path"]).resolve()
    assert worker_path != repo.resolve()
    assert (worker_path / ".git").exists()
    status = payload["worktree_materialization"]["worktree_status"]
    assert status["is_git_worktree"] is True
    assert status["differs_from_main_worktree"] is True


def test_observer_dogfood_execute_blocks_missing_cli_launch_backend_before_materialization(tmp_path):
    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    commit = _git(["rev-parse", "HEAD"], repo)
    args = _dogfood_args(
        tmp_path,
        main_worktree=repo,
        workspace_root=repo,
        worktree_root=".worktrees",
        base_commit=commit,
        target_head_commit=commit,
    )
    args = _replace_option(args, "--backend-mode", "fixture")

    result = runner.invoke(
        main,
        args[:-1] + ["--materialize-worktree", "--execute", "--json-output"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    blocker = payload["launch_backend_blocker"]
    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["terminal_dispatch_blocker"] is True
    assert blocker["blocker_id"] == "missing_cli_launch_backend"
    assert blocker["worktree_materialization_allowed"] is False
    assert payload["worktree_materialization"]["status"] == "skipped_terminal_dispatch_blocker"
    assert payload["worktree_materialization"]["materialized"] is False
    assert not Path(payload["runtime_context"]["worktree_path"]).exists()


def test_observer_dogfood_execute_launches_without_fabricating_startup(
    tmp_path, monkeypatch
):
    from agent.ai_invocation import AIInvocationResult

    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    commit = _git(["rev-parse", "HEAD"], repo)
    args = _dogfood_args(
        tmp_path,
        main_worktree=repo,
        workspace_root=repo,
        worktree_root=".worktrees",
        base_commit=commit,
        target_head_commit=commit,
    )
    captured = {}
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", "worker-session-token-test")
    monkeypatch.setattr(
        "agent.observer_runtime._dogfood_submit_read_receipt_facade",
        lambda **_: {
            "schema_version": "observer_dogfood_read_receipt_submission.v1",
            "ok": True,
            "status": "test_skipped",
            "read_receipt_recorded": False,
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
        },
    )

    def fake_invoke_ai(request):
        captured["prompt"] = request.prompt
        captured["metadata"] = dict(request.metadata)
        captured["env"] = dict(request.env)
        return AIInvocationResult(
            request=request,
            status="completed",
            output_text='{"status":"review_ready"}',
            command=["codex", "exec"],
            returncode=0,
            provider_backed=True,
            calls_models=True,
            auth_status="cli_auth_unknown",
        )

    monkeypatch.setattr("agent.observer_runtime.invoke_ai", fake_invoke_ai)

    result = runner.invoke(
        main,
        args[:-1] + ["--materialize-worktree", "--execute", "--json-output"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert captured["prompt"].startswith("You are a bounded mf_sub implementation worker")
    assert "mf_subagent_read_receipt" in captured["prompt"]
    assert "precommit-check" in captured["prompt"]
    assert captured["metadata"]["early_progress_timeout_sec"] == 20.0
    assert captured["env"]["AMING_WORKER_SESSION_TOKEN"] == "worker-session-token-test"
    assert captured["env"]["AMING_WORKER_FENCE_TOKEN"] == "fence-dogfood-test"
    executable_launch = payload["executable_worker_launch"]
    assert executable_launch["status"] == "ready"
    assert executable_launch["executable"] is True
    assert executable_launch["payload"]["task_id"] == DOGFOOD_BACKLOG_ID
    assert executable_launch["payload"]["route_context_hash"] == DOGFOOD_ROUTE_CONTEXT_HASH
    assert executable_launch["payload"]["prompt_contract_id"] == DOGFOOD_PROMPT_CONTRACT_ID
    assert executable_launch["payload"]["visible_injection_manifest_hash"] == (
        DOGFOOD_VISIBLE_MANIFEST_HASH
    )
    assert executable_launch["payload"]["owned_files"] == [
        "agent/observer_runtime.py",
        "agent/cli.py",
    ]
    assert "codex exec" in executable_launch["command_display"]
    assert "AMING_WORKER_SESSION_TOKEN" in executable_launch["command_display"]
    assert "startup_timeline_event" not in payload
    assert "read_receipt" not in payload
    assert "startup_recording" not in payload["observer_run"]
    assert payload["observer_run"]["executable_worker_launch"] == executable_launch


def test_observer_dogfood_execute_timeout_records_no_diff_blocker(tmp_path, monkeypatch):
    from agent.ai_invocation import AIInvocationResult

    runner = CliRunner()
    repo = _init_git_repo(tmp_path)
    commit = _git(["rev-parse", "HEAD"], repo)
    args = _dogfood_args(
        tmp_path,
        main_worktree=repo,
        workspace_root=repo,
        worktree_root=".worktrees",
        base_commit=commit,
        target_head_commit=commit,
    )
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", "worker-session-token-test")
    monkeypatch.setattr(
        "agent.observer_runtime._dogfood_submit_read_receipt_facade",
        lambda **_: {
            "schema_version": "observer_dogfood_read_receipt_submission.v1",
            "ok": True,
            "status": "test_skipped",
            "read_receipt_recorded": False,
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
        },
    )

    def fake_invoke_ai(request):
        return AIInvocationResult(
            request=request,
            status="blocked",
            output_text="",
            error="codex_cli invocation timed out after 1s",
            command=["codex", "exec"],
            returncode=124,
            provider_backed=True,
            calls_models=False,
            auth_status="cli_timeout",
        )

    monkeypatch.setattr("agent.observer_runtime.invoke_ai", fake_invoke_ai)
    monkeypatch.setattr(
        "agent.observer_runtime._timeline_startup_read_receipt_recording_status",
        lambda **_: {},
    )

    result = runner.invoke(
        main,
        args[:-1]
        + ["--materialize-worktree", "--execute", "--timeout-sec", "1", "--json-output"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    blocker = payload["cli_timeout_blocker"]
    diff_scope = blocker["worktree_diff_scope"]
    projection = payload["terminal_contract_projection"]
    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["calls_models"] is False
    assert payload["auth_status"] == "cli_timeout"
    assert blocker["no_output"] is True
    assert blocker["no_finish_evidence"] is True
    assert blocker["terminal_dispatch_blocker"] is True
    assert blocker["failure_evidence_appended"] is True
    assert blocker["command_projection_status"] == "failed"
    assert blocker["startup_recorded"] is False
    assert blocker["read_receipt_recorded"] is False
    assert blocker["read_receipt_recorded_before_implementation_wait"] is False
    assert blocker["startup_timeline_event_id"] == ""
    assert blocker["read_receipt_timeline_event_id"] == ""
    assert blocker["observer_command_id"] == DOGFOOD_BACKLOG_ID
    assert blocker["task_id"] == DOGFOOD_BACKLOG_ID
    assert blocker["route_id"]
    assert blocker["route_context_hash"] == DOGFOOD_ROUTE_CONTEXT_HASH
    assert blocker["prompt_contract_id"] == DOGFOOD_PROMPT_CONTRACT_ID
    assert blocker["visible_injection_manifest_hash"] == DOGFOOD_VISIBLE_MANIFEST_HASH
    assert blocker["fence_token"] == "fence-dogfood-test"
    assert blocker["owned_files"] == ["agent/observer_runtime.py", "agent/cli.py"]
    assert diff_scope["no_diff"] is True
    assert diff_scope["implementation_changed_files"] == []
    assert diff_scope["worktree_clean"] is True or diff_scope["dirty_files"]
    assert projection["canonical_contract_state"] == "blocked"
    assert projection["command_projection_status"] == "failed"
    assert projection["observer_command_id"] == DOGFOOD_BACKLOG_ID
    assert "executable_worker_launch_payload" in projection["terminal_evidence_refs"]
    assert "mf_subagent_startup_not_recorded" in projection["terminal_evidence_refs"]
    assert "mf_subagent_read_receipt_not_recorded" in projection["terminal_evidence_refs"]
    assert blocker["executable_worker_launch"]["payload"]["task_id"] == DOGFOOD_BACKLOG_ID
    assert blocker["executable_worker_launch"]["payload"]["owned_files"] == [
        "agent/observer_runtime.py",
        "agent/cli.py",
    ]
    assert "startup_timeline_event" not in payload
    assert "actual_startup_recorded" not in payload
    assert "read_receipt_recorded" not in payload


def test_observer_dogfood_generated_worker_workspace_differs_from_main(tmp_path):
    runner = CliRunner()
    main_worktree = tmp_path / "main"

    result = runner.invoke(main, _dogfood_args(tmp_path, main_worktree=main_worktree))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    worker_workspace = Path(payload["runtime_context"]["worktree_path"]).resolve()
    assert worker_workspace != main_worktree.resolve()


def test_observer_runtime_text_prepare_json_includes_launch_text_and_hash(tmp_path):
    runner = CliRunner()
    main_worktree = tmp_path / "main"
    main_worktree.mkdir(parents=True)
    worker_worktree = (
        tmp_path
        / "workers"
        / ".worktrees"
        / "runtime-text-worker"
        / f"{DOGFOOD_BACKLOG_ID.lower()}-impl-1"
    )
    evidence_file = tmp_path / "branch-runtime-evidence.json"
    evidence_file.write_text(
        json.dumps(
            {
                "schema_version": "mf_subagent_branch_runtime.v1",
                "status": "worktree_ready",
                "ok": True,
                "present": True,
                "registered": True,
                "allocation_required": False,
                "source_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
                "registration_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
                "registration_source": "parallel_branch_allocate",
                "runtime_context_id": "mfrctx-cli-runtime-text",
                "context": {
                    "project_id": "aming-claw",
                    "runtime_context_id": "mfrctx-cli-runtime-text",
                    "task_id": f"{DOGFOOD_BACKLOG_ID}-impl-1",
                    "backlog_id": DOGFOOD_BACKLOG_ID,
                    "root_task_id": DOGFOOD_BACKLOG_ID,
                    "worker_id": "runtime-text-worker",
                    "attempt": 1,
                    "branch_ref": f"refs/heads/runtime-text/{DOGFOOD_BACKLOG_ID.lower()}-impl-1",
                    "worktree_path": str(worker_worktree),
                    "fence_token": "fence-runtime-text-test",
                    "base_commit": "base123",
                    "target_head_commit": "target123",
                    "merge_queue_id": "mq-runtime-text-test",
                },
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        main,
        [
            "observer",
            "runtime-text",
            "prepare",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            DOGFOOD_BACKLOG_ID,
            "--route-context-hash",
            DOGFOOD_ROUTE_CONTEXT_HASH,
            "--prompt-contract-id",
            DOGFOOD_PROMPT_CONTRACT_ID,
            "--prompt-contract-hash",
            "sha256:prompt-contract",
            "--route-id",
            "route-20260603-runtime-text",
            "--route-token-ref",
            "rtok-runtime-text-test",
            "--visible-injection-manifest-hash",
            DOGFOOD_VISIBLE_MANIFEST_HASH,
            "--main-worktree",
            str(main_worktree),
            "--workspace-root",
            str(tmp_path / "workers"),
            "--owned-file",
            "agent/observer_runtime.py",
            "--observer-command-id",
            "cmd-runtime-text-test",
            "--task-id",
            f"{DOGFOOD_BACKLOG_ID}-impl-1",
            "--parent-task-id",
            DOGFOOD_BACKLOG_ID,
            "--merge-queue-id",
            "mq-runtime-text-test",
            "--fence-token",
            "fence-runtime-text-test",
            "--branch-runtime-registration-ref",
            "/api/graph-governance/aming-claw/parallel-branches/allocate",
            "--branch-runtime-evidence-file",
            str(evidence_file),
            "--graph-trace-id",
            "gqt-runtime-text-test",
            "--base-commit",
            "base123",
            "--target-head-commit",
            "target123",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["runtime_context_id"] == "mfrctx-cli-runtime-text"
    assert payload["observer_command_id"] == "cmd-runtime-text-test"
    assert payload["runtime_context"]["worktree_path"] == str(worker_worktree)
    assert payload["launch_text"]
    assert payload["launch_text_hash"].startswith("sha256:")
    assert payload["raw_launch_text_persisted"] is False
    assert payload["persistent_evidence"]["launch_text_hash"] == payload["launch_text_hash"]
    assert payload["persistent_evidence"]["startup_intent_event_generated"] is True
    assert payload["persistent_evidence"]["actual_startup_required"] is True
    assert payload["persistent_evidence"]["actual_startup_recorded"] is False
    assert payload["persistent_evidence"]["close_ready"] is False
    assert payload["startup_recording"]["append_tool"] == "parallel_branch_startup"
    same_owner = payload["same_owner_session_token_startup"]
    host_surrogate = payload["host_adapter_surrogate_startup"]
    registered = payload["registered_host_adapter_spawn"]
    refusal_policy = payload["worker_launch_pack"]["startup_refusal_policy"]
    assert "host_startup_id" not in refusal_policy["required_retry_fields"]
    assert "session_token_surrogate" not in refusal_policy["required_retry_fields"]
    assert "session_token" in refusal_policy["required_retry_fields"]
    assert "host_startup_id" not in same_owner
    assert "session_token_surrogate" not in same_owner
    assert "host_startup_id" in host_surrogate
    assert "session_token_surrogate" in host_surrogate
    assert payload["startup_alternatives"]["default"] == (
        "same_owner_session_token_startup"
    )
    assert same_owner == payload["startup_identity"]
    assert same_owner == payload["startup_recording"]["startup_identity"]
    assert same_owner == payload["worker_launch_pack"]["startup_identity"]
    assert same_owner["session_token_source"] == "env:AMING_WORKER_SESSION_TOKEN"
    assert same_owner["session_token_persisted"] is False
    assert same_owner["raw_session_token_persisted"] is False
    assert host_surrogate == payload["startup_recording"][
        "host_adapter_surrogate_startup"
    ]
    assert host_surrogate["session_token_evidence_type"] == "surrogate"
    assert host_surrogate["close_satisfying"] is False
    assert host_surrogate["not_finish_gate_sufficient"] is True
    assert registered == host_surrogate["registered_host_adapter_spawn"]
    assert registered == payload["worker_launch_pack"][
        "registered_host_adapter_spawn"
    ]
    assert registered["session_token_surrogate"].startswith("host-adapter:")
    assert payload["startup_intent_event"]["event_kind"] == "mf_subagent_startup_intent"
    assert payload["startup_intent_event"]["close_satisfying"] is False
    assert payload["startup_intent_event"]["payload"]["mf_subagent_startup_intent"][
        "launch_text_hash"
    ] == payload["launch_text_hash"]
    assert "launch_text" not in payload["persistent_evidence"]
    for surface in (
        payload["startup_recording"],
        payload["worker_launch_pack"],
        payload["persistent_evidence"],
    ):
        assert "session_token" not in surface
        assert "launch_text" not in surface


def test_observer_runtime_text_prepare_json_requires_branch_allocation_ref(tmp_path):
    runner = CliRunner()
    main_worktree = tmp_path / "main"
    main_worktree.mkdir(parents=True)

    result = runner.invoke(
        main,
        [
            "observer",
            "runtime-text",
            "prepare",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            DOGFOOD_BACKLOG_ID,
            "--route-context-hash",
            DOGFOOD_ROUTE_CONTEXT_HASH,
            "--prompt-contract-id",
            DOGFOOD_PROMPT_CONTRACT_ID,
            "--route-id",
            "route-20260603-runtime-text",
            "--visible-injection-manifest-hash",
            DOGFOOD_VISIBLE_MANIFEST_HASH,
            "--main-worktree",
            str(main_worktree),
            "--workspace-root",
            str(tmp_path / "workers"),
            "--owned-file",
            "agent/observer_runtime.py",
            "--task-id",
            f"{DOGFOOD_BACKLOG_ID}-impl-1",
            "--parent-task-id",
            DOGFOOD_BACKLOG_ID,
            "--merge-queue-id",
            "mq-runtime-text-test",
            "--fence-token",
            "fence-runtime-text-test",
            "--graph-trace-id",
            "gqt-runtime-text-test",
            "--base-commit",
            "base123",
            "--target-head-commit",
            "target123",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["status"] == "allocation_required"
    assert payload["dispatch_gate_validation"]["allowed"] is False
    assert payload["persistent_evidence"]["dispatch_ready"] is False
    assert payload["persistent_evidence"]["allocation_required"] is True


def _init_git_repo(tmp_path):
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(["init"], repo)
    (repo / "README.md").write_text("dogfood execution fixture\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial fixture",
        ],
        repo,
    )
    return repo


def _git(args: list[str], cwd):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _write_cli_plugin_fixture(root):
    seed_payload = {"schema_version": 1, "project_id": "aming-claw"}
    seed_text = json.dumps(seed_payload)
    seed_hash = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    for rel, text in {
        ".codex-plugin/plugin.json": {"name": "aming-claw", "version": "0.1.1"},
        ".agents/plugins/marketplace.json": {
            "name": "aming-claw-local",
            "plugins": [
                {"name": "aming-claw", "source": {"source": "local", "path": "./."}}
            ],
        },
        ".claude-plugin/plugin.json": {
            "name": "aming-claw",
            "version": "0.1.1",
            "description": "Test plugin.",
            "mcpServers": {"aming-claw": {"command": "python", "args": ["-m", "agent.mcp.server"]}},
        },
        ".claude-plugin/marketplace.json": {
            "name": "aming-claw-local",
            "metadata": {"description": "Test marketplace."},
            "owner": {"name": "Aming Claw"},
            "plugins": [{"name": "aming-claw", "source": "./", "version": "0.1.1"}],
        },
        ".mcp.json": {"mcpServers": {"aming-claw": {"command": "python"}}},
        "agent/mcp/resources/seed-graph-summary.json": seed_payload,
        "agent/mcp/resources/self-graph-bundle-manifest.json": {
            "schema_version": 1,
            "bundle_kind": "aming_claw_self_graph_semantic_bundle",
            "bundle_major": 1,
            "bundle_version": "1.0.0",
            "project_id": "aming-claw",
            "source_commit": "abc1234",
            "snapshot_id": "scope-abc1234-test",
            "projection_id": "semproj-abc1234-test",
            "event_watermark": 7,
            "resources": [
                {
                    "path": "agent/mcp/resources/seed-graph-summary.json",
                    "role": "seed_graph_summary",
                    "required": True,
                    "sha256": seed_hash,
                }
            ],
        },
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(text), encoding="utf-8")
    for rel in (
        "skills/aming-claw-onboard/SKILL.md",
        "Archive/skills/index.json",
        "Archive/skills/aming-claw/SKILL.md",
        "Archive/skills/aming-claw/references/graph-first.md",
        "Archive/skills/aming-claw/references/mcp-tools.md",
        "Archive/skills/aming-claw/references/mf-sop.md",
        "Archive/skills/aming-claw/references/plugin-packaging.md",
        "frontend/dashboard/scripts/e2e-hn-demo.mjs",
        "frontend/dashboard/scripts/e2e-vibe-queue-fixture.mjs",
        "frontend/dashboard/scripts/e2e-vibe-queue-audit.mjs",
        "frontend/dashboard/scripts/e2e-drift-demo-fixture.mjs",
        "frontend/dashboard/scripts/e2e-drift-demo-audit.mjs",
        "frontend/dashboard/scripts/e2e-backlog-dupe-fixture.mjs",
        "frontend/dashboard/scripts/e2e-backlog-dupe-audit.mjs",
        "docs/vibe-queue-demo/README.md",
        "docs/vibe-queue-demo/prompts.md",
        "docs/drift-demo/README.md",
        "docs/drift-demo/prompts.md",
        "docs/backlog-dupe-demo/README.md",
        "docs/backlog-dupe-demo/prompts.md",
        "docker/hn-install-audit/run-install-audit.sh",
        "docker/hn-install-audit/common/install-audit.mjs",
        "docker/hn-install-audit/common/state-manager.mjs",
        "docker/hn-install-audit/validate-report.mjs",
        "docker/hn-install-audit/codex/Dockerfile",
        "docker/hn-install-audit/claude/Dockerfile",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".mjs"):
            path.write_text("#!/usr/bin/env node\nconsole.log('hn demo fixture ok');\n", encoding="utf-8")
        elif rel.endswith(".sh"):
            path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        elif rel.endswith("Dockerfile"):
            path.write_text("FROM scratch\n", encoding="utf-8")
        else:
            path.write_text("---\nname: test\n---\n", encoding="utf-8")
    server_path = root / "agent" / "mcp" / "server.py"
    server_path.parent.mkdir(parents=True, exist_ok=True)
    server_path.write_text("# test runtime entrypoint\n", encoding="utf-8")


def _make_cli_remote_plugin_repo_with_source(tmp_path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git(["init", "--bare", str(remote)], tmp_path)
    source.mkdir()
    _git(["init"], source)
    _git(["checkout", "-b", "main"], source)
    _write_cli_plugin_fixture(source)
    _git(["add", "."], source)
    _git(["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial plugin"], source)
    _git(["remote", "add", "origin", str(remote)], source)
    _git(["push", "-u", "origin", "main"], source)
    return remote, source


def _make_cli_remote_plugin_repo(tmp_path):
    remote, _source = _make_cli_remote_plugin_repo_with_source(tmp_path)
    return remote


def _git_commit_all(repo: Path, message: str) -> str:
    _git(["add", "."], repo)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        repo,
    )
    return _git(["rev-parse", "HEAD"], repo)


def _write_noisy_fake_python(tmp_path: Path) -> Path:
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import sys",
                "if sys.argv[1:] == ['--version']:",
                "    print('Python 3.11.0')",
                "    raise SystemExit(0)",
                "if sys.argv[1:4] == ['-m', 'pip', 'install']:",
                "    print('PIP NOISE THAT MUST NOT POLLUTE JSON')",
                "    raise SystemExit(0)",
                "print('unexpected fake-python args: ' + repr(sys.argv), file=sys.stderr)",
                "raise SystemExit(1)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_python


class TestCliHelp:
    """AC1: aming-claw --help contains subcommands."""

    def test_help_output(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        for cmd in ("init", "bootstrap", "scan", "status", "start", "open", "launcher", "run-executor", "backlog", "plugin", "mf"):
            assert cmd in result.output


class TestCliInit:
    """AC8: init creates .aming-claw.yaml."""

    def test_init_creates_yaml(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["init"])
            assert result.exit_code == 0
            assert os.path.exists(".aming-claw.yaml")

    def test_init_idempotent(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            runner.invoke(main, ["init"])
            result = runner.invoke(main, ["init"])
            assert "already exists" in result.output


class TestCliLauncher:
    def test_launcher_writes_local_html(self, tmp_path):
        runner = CliRunner()
        output = tmp_path / "launcher.html"

        result = runner.invoke(main, [
            "launcher",
            "--governance-url",
            "http://127.0.0.1:45555",
            "--output",
            str(output),
        ])

        assert result.exit_code == 0
        text = output.read_text(encoding="utf-8")
        assert "Aming Claw Launcher" in text
        assert "http://127.0.0.1:45555/dashboard" in text
        assert "aming-claw start" in text


class TestCliStart:
    @pytest.mark.parametrize("explicit_workspace", [False, True])
    def test_start_refuses_wrong_package_root_before_health_probe(
        self, monkeypatch, tmp_path, explicit_workspace
    ):
        import agent.cli as cli

        runner = CliRunner()
        installed_root = tmp_path / "installed-release"
        (installed_root / "agent").mkdir(parents=True)
        health_probes = []
        monkeypatch.setattr(cli, "_default_runtime_workspace", lambda: installed_root)
        monkeypatch.setattr(
            cli,
            "_probe_governance",
            lambda port: health_probes.append(port)
            or {"status": "ok", "service": "governance", "version": "old"},
        )

        source_root = tmp_path / "source-checkout"
        (source_root / ".git").mkdir(parents=True)
        (source_root / "agent").mkdir()
        (source_root / "agent/cli.py").write_text(
            "# source checkout\n", encoding="utf-8"
        )
        (source_root / "start_governance.py").write_text(
            "# source checkout\n", encoding="utf-8"
        )
        (source_root / "pyproject.toml").write_text(
            "[project]\nname='aming-claw'\n", encoding="utf-8"
        )
        source_root = source_root.resolve()
        invocation_root = source_root if not explicit_workspace else tmp_path / "elsewhere"
        invocation_root.mkdir(exist_ok=True)
        with runner.isolated_filesystem(temp_dir=invocation_root):
            args = ["start", "--port", "45555"]
            if explicit_workspace:
                args.extend(["--workspace", str(source_root)])
            result = runner.invoke(main, args)

        assert result.exit_code != 0
        assert "source checkout does not match the loaded package root" in result.output
        assert str(source_root) in result.output
        assert str(installed_root.resolve()) in result.output
        assert '"source_cli_sha256": "sha256:' in result.output
        assert '"loaded_cli_sha256": "sha256:' in result.output
        assert '"zero_write_rejection": true' in result.output
        assert health_probes == []

    def test_start_allows_matching_source_checkout(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        health_probes = []
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path(".git").mkdir()
            Path("agent").mkdir()
            Path("agent/cli.py").write_text("# source checkout\n", encoding="utf-8")
            Path("start_governance.py").write_text("# source checkout\n", encoding="utf-8")
            Path("pyproject.toml").write_text("[project]\nname='aming-claw'\n", encoding="utf-8")
            source_root = Path.cwd().resolve()
            monkeypatch.setattr(cli, "_default_runtime_workspace", lambda: source_root)
            monkeypatch.setattr(
                cli,
                "_probe_governance",
                lambda port: health_probes.append(port)
                or {"status": "ok", "service": "governance", "version": "current"},
            )
            result = runner.invoke(main, ["start", "--port", "45555"])

        assert result.exit_code == 0
        assert "already running" in result.output
        assert health_probes == [45555]

    def test_start_without_workspace_uses_plugin_runtime_root_not_cwd(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        calls = []
        fake_start_governance = types.SimpleNamespace(
            main=lambda workspace_root=None: calls.append(Path(workspace_root).resolve())
        )
        monkeypatch.setitem(sys.modules, "start_governance", fake_start_governance)
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)
        monkeypatch.setattr(
            cli,
            "_governance_start_resource_preflight",
            lambda: {
                "schema_version": "governance_startup_resource_preflight.v1",
                "status": "already_sufficient",
            },
        )
        monkeypatch.delenv("AMING_CLAW_HOME", raising=False)
        monkeypatch.delenv("SHARED_VOLUME_PATH", raising=False)

        with runner.isolated_filesystem(temp_dir=tmp_path):
            cwd = Path.cwd()
            result = runner.invoke(main, ["start", "--port", "45555"])

        assert result.exit_code == 0
        assert calls == [Path(cli.__file__).resolve().parents[1]]
        assert not (cwd / "shared-volume").exists()
        assert not (cwd / ".mcp.json").exists()

    def test_start_exits_when_governance_already_healthy(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        monkeypatch.setattr(
            cli,
            "_probe_governance",
            lambda port: {"status": "ok", "service": "governance", "version": "abc123", "port": port},
        )
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)

        result = runner.invoke(main, ["start", "--workspace", str(tmp_path), "--port", "45555"])

        assert result.exit_code == 0
        assert "Governance already running on port 45555" in result.output
        assert "http://localhost:45555/dashboard" in result.output

    def test_start_reports_non_governance_port_conflict(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: True)
        monkeypatch.setattr(cli, "_port_owner_hint", lambda port: " PID=1234")

        result = runner.invoke(main, ["start", "--workspace", str(tmp_path), "--port", "45555"])

        assert result.exit_code != 0
        assert "Port 45555 is already in use PID=1234" in result.output
        assert "not Aming Claw governance" in result.output

    def test_resource_preflight_raises_256_soft_limit_to_release_minimum(self, monkeypatch):
        import agent.cli as cli

        class FakeResource:
            RLIMIT_NOFILE = 7
            RLIM_INFINITY = -1

            def __init__(self):
                self.limit = (256, 8192)
                self.set_calls = []

            def getrlimit(self, resource_id):
                assert resource_id == self.RLIMIT_NOFILE
                return self.limit

            def setrlimit(self, resource_id, limits):
                assert resource_id == self.RLIMIT_NOFILE
                self.set_calls.append(limits)
                self.limit = limits

        fake_resource = FakeResource()
        monkeypatch.setitem(sys.modules, "resource", fake_resource)

        diagnostic = cli._governance_start_resource_preflight()

        assert fake_resource.set_calls == [(cli._GOVERNANCE_MIN_NOFILE, 8192)]
        assert diagnostic == {
            "schema_version": "governance_startup_resource_preflight.v1",
            "resource": "RLIMIT_NOFILE",
            "required_soft_limit": cli._GOVERNANCE_MIN_NOFILE,
            "process_scope_only": True,
            "global_host_mutation": False,
            "supported": True,
            "original_soft_limit": 256,
            "hard_limit": 8192,
            "hard_limit_sufficient": True,
            "status": "raised",
            "policy": "raise_process_soft_limit_to_release_minimum",
            "effective_soft_limit": cli._GOVERNANCE_MIN_NOFILE,
            "effective_hard_limit": 8192,
        }

    def test_resource_preflight_never_lowers_existing_higher_limit(self, monkeypatch):
        import agent.cli as cli

        fake_resource = types.SimpleNamespace(
            RLIMIT_NOFILE=7,
            RLIM_INFINITY=-1,
            getrlimit=lambda resource_id: (8192, 16384),
            setrlimit=lambda resource_id, limits: pytest.fail("must not lower limit"),
        )
        monkeypatch.setitem(sys.modules, "resource", fake_resource)

        diagnostic = cli._governance_start_resource_preflight()

        assert diagnostic["status"] == "already_sufficient"
        assert diagnostic["policy"] == "preserve_higher_existing_limit"
        assert diagnostic["original_soft_limit"] == 8192
        assert diagnostic["effective_soft_limit"] == 8192

    def test_resource_preflight_fails_closed_when_finite_hard_limit_is_too_low(
        self, monkeypatch
    ):
        import agent.cli as cli

        fake_resource = types.SimpleNamespace(
            RLIMIT_NOFILE=7,
            RLIM_INFINITY=-1,
            getrlimit=lambda resource_id: (256, 2048),
            setrlimit=lambda resource_id, limits: pytest.fail("must not attempt raise"),
        )
        monkeypatch.setitem(sys.modules, "resource", fake_resource)

        with pytest.raises(cli.click.ClickException, match="finite hard limit is 2048"):
            cli._governance_start_resource_preflight()

    def test_resource_preflight_documents_unavailable_cross_platform_policy(
        self, monkeypatch
    ):
        import agent.cli as cli

        monkeypatch.setitem(sys.modules, "resource", None)

        diagnostic = cli._governance_start_resource_preflight()

        assert diagnostic == {
            "schema_version": "governance_startup_resource_preflight.v1",
            "resource": "RLIMIT_NOFILE",
            "required_soft_limit": cli._GOVERNANCE_MIN_NOFILE,
            "process_scope_only": True,
            "global_host_mutation": False,
            "supported": False,
            "status": "unavailable",
            "policy": "continue_when_resource_api_unavailable",
        }

    def test_start_runs_resource_preflight_before_importing_governance(
        self, monkeypatch, tmp_path
    ):
        import agent.cli as cli

        runner = CliRunner()
        events = []

        class StartGovernanceModule(types.ModuleType):
            def __getattribute__(self, name):
                if name == "main":
                    events.append("import_start_governance")
                return super().__getattribute__(name)

        fake_start_governance = StartGovernanceModule("start_governance")
        fake_start_governance.main = lambda workspace_root=None: events.append(
            "start_governance.main"
        )
        monkeypatch.setitem(sys.modules, "start_governance", fake_start_governance)
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)
        monkeypatch.setattr(
            cli,
            "_governance_start_resource_preflight",
            lambda: events.append("resource_preflight")
            or {
                "schema_version": "governance_startup_resource_preflight.v1",
                "status": "raised",
            },
        )

        result = runner.invoke(
            main,
            ["start", "--workspace", str(tmp_path), "--port", "45555"],
        )

        assert result.exit_code == 0, result.output
        assert events == [
            "resource_preflight",
            "import_start_governance",
            "start_governance.main",
        ]
        assert "Governance startup resource preflight:" in result.output


class TestCliPlugin:
    def test_plugin_install_json_suppresses_subprocess_stdout(self, tmp_path):
        runner = CliRunner()
        remote = _make_cli_remote_plugin_repo(tmp_path)
        fake_python = _write_noisy_fake_python(tmp_path)
        install_root = tmp_path / "install"
        codex_home = tmp_path / "codex-home"
        marketplace_root = tmp_path / "marketplace-root"

        result = runner.invoke(main, [
            "plugin",
            "install",
            str(remote),
            "--install-root",
            str(install_root),
            "--python",
            str(fake_python),
            "--codex-home",
            str(codex_home),
            "--codex-config",
            str(codex_home / "config.toml"),
            "--codex-marketplace-root",
            str(marketplace_root),
            "--json-output",
        ], env={"AMING_CLAW_PLUGIN_STATE_HOME": str(tmp_path / "state-home")})

        assert result.exit_code == 0
        assert "PIP NOISE" not in result.output
        payload = json.loads(result.output)
        assert payload["installed_package"] is True
        assert payload["installed_codex_plugin"] is True

    def test_plugin_install_dry_run_prints_plan(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(main, [
            "plugin",
            "install",
            "https://github.com/amingclawdev/aming-claw.git",
            "--install-root",
            str(tmp_path),
            "--dry-run",
            "--no-pip",
        ])

        assert result.exit_code == 0
        assert "Aming Claw plugin bootstrap" in result.output
        assert "git clone" in result.output
        assert "Claude Code: /plugin marketplace add" in result.output

    def test_plugin_doctor_reports_aftercare(self, tmp_path):
        runner = CliRunner()
        _write_cli_plugin_fixture(tmp_path)

        codex_home = tmp_path / "codex-home"
        marketplace_root = install_codex_marketplace(tmp_path, marketplace_root=tmp_path / "marketplace-root")
        install_codex_plugin_cache(tmp_path, codex_home=codex_home)
        config = configure_codex_plugin(
            codex_config=codex_home / "config.toml",
            marketplace_root=marketplace_root,
        )

        result = runner.invoke(main, [
            "plugin",
            "doctor",
            "--plugin-root",
            str(tmp_path),
            "--codex-config",
            str(config),
            "--codex-home",
            str(codex_home),
            "--skip-governance",
        ])

        assert result.exit_code == 0
        assert "Aming Claw plugin doctor" in result.output
        assert "Restart/reload Codex" in result.output
        assert "dashboard_static_assets" in result.output
        assert "ai_cli_openai" in result.output
        assert "service_manager_health" not in result.output
        assert "ServiceManager/executor checks are advanced" in result.output

    def test_plugin_update_check_json_reports_current(self, tmp_path):
        runner = CliRunner()
        remote = _make_cli_remote_plugin_repo(tmp_path)
        install_root = tmp_path / "install"
        install_root.mkdir()
        plugin_root = plugin_root_for(str(remote), install_root)
        _git(["clone", str(remote), str(plugin_root)], install_root)
        _git(["checkout", "main"], plugin_root)

        result = runner.invoke(main, [
            "plugin",
            "update",
            str(remote),
            "--check",
            "--install-root",
            str(install_root),
            "--plugin-state",
            str(tmp_path / "state.json"),
            "--no-pip",
            "--no-codex-install",
            "--json-output",
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["status"] == "current"
        assert payload["update_available"] is False

    def test_plugin_update_apply_from_external_cwd_does_not_pollute_target(self, tmp_path, monkeypatch):
        runner = CliRunner()
        remote, source = _make_cli_remote_plugin_repo_with_source(tmp_path)
        install_root = tmp_path / "install"
        install_root.mkdir()
        plugin_root = plugin_root_for(str(remote), install_root)
        _git(["clone", str(remote), str(plugin_root)], install_root)
        _git(["checkout", "main"], plugin_root)

        skill = source / "skills" / "aming-claw-onboard" / "SKILL.md"
        skill.write_text("---\nname: test\n---\nupdated\n", encoding="utf-8")
        remote_commit = _git_commit_all(source, "update skill")
        _git(["push", "origin", "main"], source)

        external_project = tmp_path / "my-app"
        (external_project / "src").mkdir(parents=True)
        (external_project / "src" / "App.js").write_text(
            "export default function App() { return null; }\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(external_project)

        codex_home = tmp_path / "codex-home"
        marketplace_root = tmp_path / "marketplace-root"
        state_path = tmp_path / "state.json"
        result = runner.invoke(main, [
            "plugin",
            "update",
            str(remote),
            "--apply",
            "--install-root",
            str(install_root),
            "--plugin-state",
            str(state_path),
            "--no-pip",
            "--codex-home",
            str(codex_home),
            "--codex-config",
            str(codex_home / "config.toml"),
            "--codex-marketplace-root",
            str(marketplace_root),
            "--json-output",
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["applied"] is True
        assert payload["installed_package"] is False
        assert payload["installed_codex_plugin"] is True
        assert payload["status"] == "applied_pending_restart"
        assert payload["changed_surfaces"] == ["mcp"]
        assert _git(["rev-parse", "HEAD"], plugin_root) == remote_commit
        assert (codex_cache_plugin_root(plugin_root, codex_home=codex_home) / ".mcp.json").is_file()
        assert (marketplace_root / ".agents" / "plugins" / "aming-claw" / ".mcp.json").is_file()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["update_status"] == "applied_pending_restart"
        assert state["remote_commit"] == remote_commit

        for rel in (
            ".mcp.json",
            "shared-volume",
            ".codex-plugin",
            ".claude-plugin",
            ".agents/plugins",
            "agent/mcp/resources",
        ):
            assert not (external_project / rel).exists(), f"unexpected target-local plugin artifact: {rel}"

    def test_plugin_update_apply_json_suppresses_subprocess_stdout(self, tmp_path):
        runner = CliRunner()
        remote, source = _make_cli_remote_plugin_repo_with_source(tmp_path)
        install_root = tmp_path / "install"
        install_root.mkdir()
        plugin_root = plugin_root_for(str(remote), install_root)
        _git(["clone", str(remote), str(plugin_root)], install_root)
        _git(["checkout", "main"], plugin_root)

        skill = source / "skills" / "aming-claw-onboard" / "SKILL.md"
        skill.write_text("---\nname: test\n---\nupdated\n", encoding="utf-8")
        _git_commit_all(source, "update skill")
        _git(["push", "origin", "main"], source)

        fake_python = _write_noisy_fake_python(tmp_path)
        codex_home = tmp_path / "codex-home"
        marketplace_root = tmp_path / "marketplace-root"
        result = runner.invoke(main, [
            "plugin",
            "update",
            str(remote),
            "--apply",
            "--install-root",
            str(install_root),
            "--python",
            str(fake_python),
            "--plugin-state",
            str(tmp_path / "state.json"),
            "--codex-home",
            str(codex_home),
            "--codex-config",
            str(codex_home / "config.toml"),
            "--codex-marketplace-root",
            str(marketplace_root),
            "--json-output",
        ])

        assert result.exit_code == 0
        assert "PIP NOISE" not in result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["applied"] is True
        assert payload["installed_package"] is True
        assert payload["installed_codex_plugin"] is True

    def test_plugin_update_missing_checkout_exits_nonzero(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(main, [
            "plugin",
            "update",
            "https://example.com/aming-claw.git",
            "--check",
            "--install-root",
            str(tmp_path / "missing-install"),
            "--plugin-state",
            str(tmp_path / "state.json"),
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["status"] == "failed"
        assert "plugin checkout not found" in payload["error"]


class TestCliBacklog:
    def test_backlog_export_writes_payload(self, monkeypatch, tmp_path):
        import agent.cli as cli

        calls = []

        def fake_http(method, url, payload=None, timeout=30.0):
            calls.append((method, url, payload))
            return 200, {
                "schema": "aming-claw.backlog.export",
                "schema_version": 1,
                "project_id": "aming-claw",
                "row_count": 1,
                "rows": [{"bug_id": "BUG-1"}],
            }

        monkeypatch.setattr(cli, "_http_json", fake_http)
        runner = CliRunner()
        output = tmp_path / "backlog.json"

        result = runner.invoke(main, [
            "backlog",
            "export",
            "--project-id",
            "aming-claw",
            "--status",
            "OPEN",
            "--bug-id",
            "BUG-1",
            "--output",
            str(output),
        ])

        assert result.exit_code == 0
        assert "Exported 1 backlog row" in result.output
        assert json.loads(output.read_text(encoding="utf-8"))["rows"][0]["bug_id"] == "BUG-1"
        assert calls[0][0] == "GET"
        assert "/api/backlog/aming-claw/portable/export" in calls[0][1]
        assert "status=OPEN" in calls[0][1]

    def test_backlog_import_posts_payload_and_exits_nonzero_on_conflict(self, monkeypatch, tmp_path):
        import agent.cli as cli

        input_path = tmp_path / "backlog.json"
        input_path.write_text(json.dumps({
            "schema": "aming-claw.backlog.export",
            "schema_version": 1,
            "rows": [{"bug_id": "BUG-1"}],
        }), encoding="utf-8")
        calls = []

        def fake_http(method, url, payload=None, timeout=30.0):
            calls.append((method, url, payload))
            return 409, {
                "ok": False,
                "inserted_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"bug_id": "BUG-1", "error": "bug_id already exists"}],
            }

        monkeypatch.setattr(cli, "_http_json", fake_http)
        runner = CliRunner()

        result = runner.invoke(main, [
            "backlog",
            "import",
            "--project-id",
            "aming-claw",
            "--input",
            str(input_path),
            "--on-conflict",
            "fail",
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert calls[0][0] == "POST"
        assert calls[0][2]["on_conflict"] == "fail"
        assert calls[0][2]["payload"]["rows"][0]["bug_id"] == "BUG-1"


class TestCliMf:
    def test_mf_dispatch_gate_help_visible(self):
        runner = CliRunner()

        result = runner.invoke(main, ["mf", "--help"])
        assert result.exit_code == 0
        assert "dispatch-gate" in result.output

        command_help = runner.invoke(main, ["mf", "dispatch-gate", "--help"])
        assert command_help.exit_code == 0
        assert "--contract-file" in command_help.output
        assert "--target-worktree" in command_help.output
        assert "--main-worktree" in command_help.output

    def test_mf_dispatch_gate_rejects_invalid_payload(self, tmp_path):
        runner = CliRunner()
        contract_path = tmp_path / "dispatch.json"
        contract_path.write_text(json.dumps({"owned_files": []}), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "dispatch-gate",
            "--contract-file",
            str(contract_path),
        ])

        assert result.exit_code == 1
        captured = result.stderr or result.output
        assert "REJECT: MF subagent dispatch missing required fields:" in captured
        assert "branch" in captured

    def test_mf_dispatch_gate_prints_pretty_json_on_pass(self, tmp_path):
        runner = CliRunner()
        contract_path = tmp_path / "dispatch.json"
        contract_path.write_text(json.dumps({
            "branch": "mf/test-worker",
            "worktree": str(tmp_path / "worker"),
            "base_commit": "abc123",
            "target_head_commit": "def456",
            "merge_queue_id": "mq-test",
            "fence_token": "fence-test",
            "route_context_hash": "sha256:test-route-context",
            "prompt_contract_id": "prompt-contract-test",
            "prompt_contract_hash": "sha256:test-prompt-contract",
            "route_token_ref": "rtok-test",
            "owned_files": ["agent/cli.py"],
            "dirty_scope_check": {
                "status": "passed",
                "changed_files": [],
            },
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "dispatch-gate",
            "--contract-file",
            str(contract_path),
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema_version"] == "mf_subagent_dispatch_gate.v1"
        assert payload["fence_token"] == "fence-test"
        assert payload["route_context_hash"] == "sha256:test-route-context"
        assert payload["base_commit"] == "abc123"
        assert payload["target_head_commit"] == "def456"
        assert payload["owned_files"] == ["agent/cli.py"]
        assert "\n  \"base_commit\": \"abc123\"" in result.output

    def test_mf_precommit_check_passes_on_missing_state_warning(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--plugin-state",
            str(tmp_path / "missing.json"),
        ])

        assert result.exit_code == 0
        assert "Aming Claw MF precommit check" in result.output
        assert "plugin update state file not found" in result.output

    def test_mf_precommit_check_fails_on_restart_blocker(self, tmp_path):
        runner = CliRunner()
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({
            "schema_version": 1,
            "plugin_id": "aming-claw@aming-claw-local",
            "update_status": "applied_pending_restart",
            "restart_required": {
                "mcp": {"required": True, "reason": "skills changed"}
            },
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--plugin-state",
            str(state_path),
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["checks"]["plugin_update_state"]["status"] == "fail"

    def test_mf_precommit_check_blocks_missing_route_consumption(self, tmp_path):
        runner = CliRunner()
        route_path = tmp_path / "route.json"
        route_path.write_text(json.dumps({
            "contract": {
                "selected_topology": "observer_led_parallel_lanes",
                "recommended_topology": "mf_parallel.v1",
            },
            "timeline_evidence": [
                {
                    "event_kind": "route_context_advisory",
                    "status": "passed",
                    "payload": {"message": "route docs say to use a worker"},
                }
            ],
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--route-consumption-file",
            str(route_path),
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert "bounded_implementation_worker_dispatch" in payload["checks"][
            "route_context_consumption"
        ]["missing_requirement_ids"]

    def test_mf_precommit_check_accepts_consumed_route_context(self, tmp_path):
        runner = CliRunner()
        identity = {
            "route_context_hash": "sha256:test-route-context",
            "prompt_contract_id": "prompt-contract-test",
            "prompt_contract_hash": "sha256:test-prompt-contract",
            "visible_injection_manifest_hash": "sha256:test-visible-manifest",
        }
        route_path = tmp_path / "route.json"
        route_path.write_text(json.dumps({
            "contract": {
                "selected_topology": "observer_led_parallel_lanes",
                "recommended_topology": "mf_parallel.v1",
            },
            "timeline_evidence": [
                {
                    "event_kind": "route_context",
                    "status": "passed",
                    "payload": {"route_context": identity},
                },
                {
                    "event_kind": "route_action_precheck",
                    "status": "allowed",
                    "verification": {**identity, "allowed_action": "dispatch_worker"},
                },
                {
                    "event_kind": "mf_subagent_dispatch",
                    "status": "passed",
                    "payload": {"mf_subagent_dispatch_gate": {**identity, "bounded": True}},
                },
                {
                    "event_kind": "mf_subagent_startup",
                    "status": "passed",
                    "payload": {
                        "mf_subagent_startup_gate": {
                            **identity,
                            "worker_id": "mf-sub",
                            "fence_token": "fence-test",
                            "actual_cwd": "/repo/.worktrees/mf-sub",
                            "actual_git_root": "/repo/.worktrees/mf-sub",
                            "branch": "refs/heads/codex/mf-sub",
                            "head_commit": "head-test",
                        }
                    },
                },
                {
                    "event_kind": "qa_verification",
                    "status": "passed",
                    "verification": {
                        **identity,
                        "contract_evidence": [
                            {
                                "requirement_id": "independent_verification_lane",
                                "status": "passed",
                                "reviewer_role": "qa",
                            }
                        ],
                    },
                },
            ],
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--plugin-state",
            str(tmp_path / "missing.json"),
            "--route-consumption-file",
            str(route_path),
            "--json-output",
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["checks"]["route_context_consumption"]["status"] == "pass"


class TestACDevRuntimeCli:
    @staticmethod
    def _generic_stable_health(cli, *, commit, root, **overrides):
        source_hash = "sha256:" + "1" * 64
        health = {
            "status": "ok",
            "service": "governance",
            "port": cli.AC_STABLE_SERVICE_PORT,
            "pid": 12345,
            "runtime_loaded_version": commit,
            "runtime_stale": False,
            "runtime_plane": "generic",
            "runtime_plane_identity": {
                "schema_version": "ac_runtime_plane_identity.v1",
                "status": "ready",
                "plane": "generic",
                "bind_host": "0.0.0.0",
                "port": cli.AC_STABLE_SERVICE_PORT,
                "expected_port": cli.AC_STABLE_SERVICE_PORT,
                "pid": 12345,
                "worktree_root": str(root),
                "branch": cli.AC_STABLE_BRANCH,
                "expected_branch": cli.AC_STABLE_BRANCH,
                "commit": commit,
                "worktree_dirty": False,
                "worktree_dirty_files": [],
            },
            "loaded_runtime_identity": {
                "schema_version": "governance_loaded_runtime_identity.v1",
                "loaded_commit": commit,
                "loaded_pid": 12345,
                "worktree_head_version": commit[:12],
                "runtime_stale": False,
                "runtime_stale_reasons": [],
                "loaded_source_sha256": source_hash,
                "worktree_source_sha256": source_hash,
            },
        }
        health.update(overrides)
        return health

    @staticmethod
    def _generic_graph(commit, **overrides):
        graph = {
            "ok": True,
            "project_id": "aming-claw",
            "active_snapshot_id": "full-generic-stable",
            "graph_snapshot_commit": commit,
            "materialized_graph_baseline_commit": commit,
            "current_state": {
                "graph_stale": {
                    "is_stale": False,
                    "head_commit": commit,
                    "active_graph_commit": commit,
                }
            },
        }
        graph.update(overrides)
        return graph

    def test_verified_generic_anchor_requires_exact_source_git_graph_and_free_dev_port(
        self, monkeypatch, tmp_path
    ):
        import agent.cli as cli

        stable = tmp_path / "stable"
        stable.mkdir()
        subprocess.run(
            ["git", "init", "-qb", cli.AC_STABLE_BRANCH],
            cwd=stable,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=stable,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=stable,
            check=True,
        )
        (stable / "README.md").write_text("stable\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=stable, check=True)
        subprocess.run(["git", "commit", "-qm", "stable"], cwd=stable, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=stable,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        health = self._generic_stable_health(
            cli, commit=commit, root=stable.resolve()
        )
        monkeypatch.setattr(cli, "_port_is_open", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(
            cli,
            "_probe_governance_path",
            lambda *_args, **_kwargs: self._generic_graph(commit),
        )

        authority = cli._verified_generic_stable_authority(health)

        assert authority["mode"] == "verified_generic"
        assert authority["commit"] == commit
        assert authority["graph"]["graph_snapshot_commit"] == commit

        monkeypatch.setattr(cli, "_port_is_open", lambda *_args, **_kwargs: True)
        assert cli._verified_generic_stable_authority(health) == {}

    @pytest.mark.parametrize(
        "mutation",
        [
            "runtime_stale",
            "dirty",
            "source_hash",
            "branch",
            "commit",
            "port",
            "pid",
            "loaded_pid",
            "loaded_schema",
            "worktree_head",
            "plane_schema",
            "bind_host",
            "expected_branch",
        ],
    )
    def test_verified_generic_health_fails_closed_on_identity_drift(
        self, tmp_path, mutation
    ):
        import agent.cli as cli

        commit = "c" * 40
        health = self._generic_stable_health(
            cli, commit=commit, root=tmp_path
        )
        if mutation == "runtime_stale":
            health["runtime_stale"] = True
        elif mutation == "dirty":
            health["runtime_plane_identity"]["worktree_dirty"] = True
            health["runtime_plane_identity"]["worktree_dirty_files"] = ["M file"]
        elif mutation == "source_hash":
            health["loaded_runtime_identity"]["worktree_source_sha256"] = (
                "sha256:" + "2" * 64
            )
        elif mutation == "branch":
            health["runtime_plane_identity"]["branch"] = "codex/ac-dev"
        elif mutation == "commit":
            health["runtime_plane_identity"]["commit"] = "d" * 40
        elif mutation == "port":
            health["runtime_plane_identity"]["expected_port"] = (
                cli.AC_DEV_SERVICE_PORT
            )
        elif mutation == "pid":
            health["runtime_plane_identity"]["pid"] = 999
        elif mutation == "loaded_pid":
            health["loaded_runtime_identity"]["loaded_pid"] = 999
        elif mutation == "loaded_schema":
            health["loaded_runtime_identity"]["schema_version"] = "legacy"
        elif mutation == "worktree_head":
            health["loaded_runtime_identity"]["worktree_head_version"] = "d" * 40
        elif mutation == "plane_schema":
            health["runtime_plane_identity"]["schema_version"] = "legacy"
        elif mutation == "bind_host":
            health["runtime_plane_identity"]["bind_host"] = "127.0.0.1"
        else:
            health["runtime_plane_identity"]["expected_branch"] = "codex/ac-dev"

        assert cli._verified_generic_health_identity(health) == {}

    @pytest.mark.parametrize(
        "mutation",
        ["snapshot", "commit", "baseline", "stale", "head", "active"],
    )
    def test_verified_generic_graph_fails_closed_on_graph_drift(self, mutation):
        import agent.cli as cli

        commit = "c" * 40
        graph = self._generic_graph(commit)
        if mutation == "snapshot":
            graph["active_snapshot_id"] = ""
        elif mutation == "commit":
            graph["graph_snapshot_commit"] = "d" * 40
        elif mutation == "baseline":
            graph["materialized_graph_baseline_commit"] = "d" * 40
        elif mutation == "stale":
            graph["current_state"]["graph_stale"]["is_stale"] = True
        elif mutation == "head":
            graph["current_state"]["graph_stale"]["head_commit"] = "d" * 40
        else:
            graph["current_state"]["graph_stale"]["active_graph_commit"] = (
                "d" * 40
            )

        assert (
            cli._verified_generic_graph_identity(graph, loaded_commit=commit) == {}
        )

    def test_generic_database_binding_allows_absent_health_identity_but_rejects_mismatch(
        self, monkeypatch, tmp_path
    ):
        import agent.cli as cli

        source = tmp_path / "ac-dev"
        source.mkdir()
        stable = tmp_path / "stable"
        database = (
            stable
            / cli.AC_DATABASE_STABLE_RELATIVE_PATH
        )
        database.parent.mkdir(parents=True)
        database.touch()
        stable_commit = "c" * 40
        monkeypatch.setattr(
            cli,
            "_source_git_identity",
            lambda: {
                "root": str(source),
                "branch": cli.AC_DEV_BRANCH,
                "commit": "d" * 40,
                "dirty": "",
            },
        )
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *_args, **_kwargs: types.SimpleNamespace(
                returncode=0,
                stdout=(
                    f"worktree {stable}\n"
                    f"branch refs/heads/{cli.AC_STABLE_BRANCH}\n"
                ),
                stderr="",
            ),
        )
        monkeypatch.setattr(
            cli,
            "_current_stable_runtime_authority",
            lambda: {
                "commit": stable_commit,
                "mode": "verified_generic",
                "health": {"runtime_plane_identity": {}},
            },
        )

        binding = cli._canonical_stable_database_binding(
            str(stable / "shared-volume"),
            stable_anchor_commit=stable_commit,
        )
        assert binding["stable_database_identity"]["inode"] == database.stat().st_ino

        monkeypatch.setattr(
            cli,
            "_current_stable_runtime_authority",
            lambda: {
                "commit": stable_commit,
                "mode": "verified_generic",
                "health": {
                    "runtime_plane_identity": {
                        "stable_database_identity": {
                            **binding["stable_database_identity"],
                            "inode": binding["stable_database_identity"]["inode"] + 1,
                        }
                    }
                },
            },
        )
        with pytest.raises(cli.click.ClickException, match="differs"):
            cli._canonical_stable_database_binding(
                str(stable / "shared-volume"),
                stable_anchor_commit=stable_commit,
            )

    def test_dev_anchor_tracks_exact_current_stable_health(self, monkeypatch):
        import agent.cli as cli

        commit = "c" * 40
        monkeypatch.setattr(
            cli,
            "_probe_governance",
            lambda port: {
                "status": "ok",
                "service": "governance",
                "port": port,
                "runtime_loaded_version": commit,
                "runtime_stale": False,
                "runtime_plane": "stable",
                "runtime_plane_identity": {
                    "status": "ready",
                    "branch": cli.AC_STABLE_BRANCH,
                    "commit": commit,
                    "stable_anchor_commit": commit,
                },
            },
        )

        assert cli._current_stable_anchor_commit() == commit

    def test_explicit_stable_runtime_rejects_ac_dev_checkout(self, monkeypatch):
        import agent.cli as cli

        monkeypatch.setattr(
            cli,
            "_source_git_identity",
            lambda: {
                "root": "/tmp/ac-dev",
                "branch": cli.AC_DEV_BRANCH,
                "commit": "b" * 40,
                "dirty": "",
            },
        )
        monkeypatch.setattr(
            cli,
            "_require_source_checkout_matches_loaded_package",
            lambda workspace: None,
        )

        result = CliRunner().invoke(
            main,
            ["start", "--runtime-plane", "stable", "--port", "40000"],
        )

        assert result.exit_code != 0
        assert cli.AC_STABLE_BRANCH in result.output

    def test_dev_runtime_refuses_non_reserved_port_before_start(self, monkeypatch):
        import agent.cli as cli

        monkeypatch.setattr(cli, "_require_source_checkout_matches_loaded_package", lambda workspace: None)
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)
        result = CliRunner().invoke(
            main,
            ["start", "--runtime-plane", "dev", "--port", "40009"],
        )

        assert result.exit_code != 0
        assert "reserved to port 40008" in result.output

    def test_dev_runtime_sets_bounded_plane_environment(
        self, monkeypatch, tmp_path
    ):
        import agent.cli as cli

        runtime_root = tmp_path / "runtime"
        dev_storage_root = tmp_path / "dev-world"
        calls = []
        legacy_start = types.ModuleType("start_governance")
        legacy_start.__file__ = "<test-start-governance>"

        def reject_legacy_start(_name):
            pytest.fail("dev startup must not import the legacy backfill wrapper")

        legacy_start.__getattr__ = reject_legacy_start
        monkeypatch.setitem(sys.modules, "start_governance", legacy_start)
        monkeypatch.setattr(cli, "_run_dev_governance", lambda: calls.append("guarded"))
        monkeypatch.setattr(
            cli,
            "_local_stable_source_anchor",
            lambda: cli.AC_STABLE_ANCHOR_COMMIT,
        )
        monkeypatch.setattr(
            cli,
            "_source_git_identity",
            lambda: {
                "root": str(Path(cli.__file__).resolve().parents[1]),
                "branch": cli.AC_DEV_BRANCH,
                "commit": "b" * 40,
                "dirty": "",
                "source_sha256": "sha256:" + "c" * 64,
            },
        )
        monkeypatch.delenv("SHARED_VOLUME_PATH", raising=False)
        monkeypatch.setattr(cli, "_require_source_checkout_matches_loaded_package", lambda workspace: None)
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)
        monkeypatch.setattr(
            cli,
            "_governance_start_resource_preflight",
            lambda: {"status": "already_sufficient"},
        )
        monkeypatch.setattr(
            cli,
            "_require_dev_cutover_activation",
            lambda *_args, **_kwargs: pytest.fail(
                "operator marker must not authorize dev startup"
            ),
        )
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *args, **kwargs: types.SimpleNamespace(
                returncode=0, stdout="codex/ac-dev\n", stderr=""
            ),
        )

        result = CliRunner().invoke(
            main,
            [
                "start",
                "--runtime-plane",
                "dev",
                "--port",
                "40008",
                "--runtime-workspace",
                str(runtime_root),
                "--dev-storage-root",
                str(dev_storage_root),
            ],
        )

        assert result.exit_code == 0, result.output
        assert calls == ["guarded"]
        assert os.environ["AMING_CLAW_RUNTIME_PLANE"] == "dev"
        assert os.environ["AMING_CLAW_STABLE_ANCHOR_COMMIT"] == cli.AC_STABLE_ANCHOR_COMMIT
        assert os.environ["AMING_CLAW_ALLOWED_PROJECT_IDS"] == "aming-claw"
        assert os.environ["AMING_CLAW_DB_MIGRATION_POLICY"] == "verify-only"
        assert os.environ["AMING_CLAW_STABLE_DEPLOYMENT"] == "deny"
        assert os.environ["AMING_CLAW_DEV_STORAGE_ROOT"] == str(
            dev_storage_root.resolve()
        )
        assert "AMING_CLAW_DEV_CUTOVER_PREFLIGHT_HASH" not in os.environ
        assert "SHARED_VOLUME_PATH" not in os.environ
        for key in (
            "AMING_CLAW_RUNTIME_PLANE",
            "AMING_CLAW_STABLE_ANCHOR_COMMIT",
            "AMING_CLAW_ALLOWED_PROJECT_IDS",
            "AMING_CLAW_DB_MIGRATION_POLICY",
            "AMING_CLAW_STABLE_DEPLOYMENT",
            "SHARED_VOLUME_PATH",
            "AMING_CLAW_HOME",
            "GOVERNANCE_PORT",
            "AMING_CLAW_DEV_STORAGE_ROOT",
            "AMING_CLAW_DEV_CUTOVER_PREFLIGHT_HASH",
        ):
            os.environ.pop(key, None)

    @pytest.mark.parametrize(
        "identity_override,health_override",
        [
            ({"commit": "c" * 40}, {"runtime_loaded_version": "c" * 40}),
            ({"worktree_root": "/tmp/other-ac-dev"}, {}),
        ],
    )
    def test_existing_dev_service_requires_exact_invoking_checkout_identity(
        self,
        monkeypatch,
        tmp_path,
        identity_override,
        health_override,
    ):
        import agent.cli as cli

        candidate_root = tmp_path / "ac-dev"
        candidate_root.mkdir()
        candidate = "b" * 40
        stable = "a" * 40
        source_identity = {
            "root": str(candidate_root),
            "branch": cli.AC_DEV_BRANCH,
            "commit": candidate,
            "dirty": "",
            "source_sha256": "sha256:" + "c" * 64,
        }
        runtime_identity = {
            "schema_version": "ac_runtime_plane_identity.v1",
            "status": "ready",
            "plane": "dev",
            "bind_host": "127.0.0.1",
            "port": cli.AC_DEV_SERVICE_PORT,
            "expected_port": cli.AC_DEV_SERVICE_PORT,
            "pid": 12345,
            "worktree_root": str(candidate_root.resolve()),
            "branch": cli.AC_DEV_BRANCH,
            "expected_branch": cli.AC_DEV_BRANCH,
            "commit": candidate,
            "worktree_dirty": False,
            "worktree_dirty_files": [],
            "stable_anchor_commit": stable,
        }
        database_identity = {
            "schema_version": "ac_governance_database_identity.v2",
            "world_id": "ac-dev",
            "project_id": "aming-claw",
            "device": 1,
            "inode": 2,
            "relative_path_sha256": "sha256:" + "1" * 64,
            "genesis_sha256": "sha256:" + "2" * 64,
        }
        runtime_identity["database_identity"] = database_identity
        runtime_identity["world_id"] = "ac-dev"
        runtime_identity.update(identity_override)
        health = {
            "status": "ok",
            "service": "governance",
            "port": cli.AC_DEV_SERVICE_PORT,
            "runtime_plane": "dev",
            "bind_host": "127.0.0.1",
            "pid": 12345,
            "runtime_loaded_version": candidate,
            "runtime_stale": False,
            "runtime_plane_identity": runtime_identity,
            "loaded_runtime_identity": {
                "schema_version": "governance_loaded_runtime_identity.v1",
                "loaded_commit": candidate,
                "loaded_pid": 12345,
                "worktree_head_version": candidate[:12],
                "runtime_stale": False,
                "runtime_stale_reasons": [],
                "loaded_source_sha256": source_identity["source_sha256"],
                "worktree_source_sha256": source_identity["source_sha256"],
            },
        }
        health.update(health_override)
        monkeypatch.setattr(
            cli, "_require_source_checkout_matches_loaded_package", lambda _workspace: None
        )
        monkeypatch.setattr(cli, "_source_git_identity", lambda: source_identity)
        monkeypatch.setattr(cli, "_local_stable_source_anchor", lambda: stable)
        side_effects = []

        def forbidden_storage(*_args, **_kwargs):
            side_effects.append("storage")
            raise AssertionError("occupied port must reject before storage")

        def forbidden_activation(*_args, **_kwargs):
            side_effects.append("activation")
            raise AssertionError("mismatched runtime must reject before activation")

        monkeypatch.setattr(cli, "_canonical_dev_database_binding", forbidden_storage)
        monkeypatch.setattr(cli, "_require_dev_cutover_activation", forbidden_activation)
        monkeypatch.setattr(cli, "_probe_governance", lambda _port: health)
        dev_root = tmp_path / "dev-world"

        result = CliRunner().invoke(
            main,
            [
                "start", "--runtime-plane", "dev", "--port", "40008",
                "--dev-storage-root", str(dev_root),
            ],
        )

        assert result.exit_code != 0
        assert "mismatched AC dev runtime identity" in result.output
        assert side_effects == []
        assert not dev_root.exists()

    def test_dev_start_rejects_non_governance_listener_before_storage(
        self, monkeypatch, tmp_path
    ):
        import agent.cli as cli

        candidate_root = tmp_path / "ac-dev"
        candidate_root.mkdir()
        source_identity = {
            "root": str(candidate_root.resolve()),
            "branch": cli.AC_DEV_BRANCH,
            "commit": "b" * 40,
            "dirty": "",
            "source_sha256": "sha256:" + "c" * 64,
        }
        side_effects = []

        def forbidden(name):
            def reject(*_args, **_kwargs):
                side_effects.append(name)
                raise AssertionError("occupied foreign port must be pre-side-effect")

            return reject

        monkeypatch.setattr(
            cli, "_require_source_checkout_matches_loaded_package", lambda _workspace: None
        )
        monkeypatch.setattr(cli, "_source_git_identity", lambda: source_identity)
        monkeypatch.setattr(cli, "_local_stable_source_anchor", lambda: "a" * 40)
        monkeypatch.setattr(cli, "_probe_governance", lambda _port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda _port: True)
        monkeypatch.setattr(cli, "_port_owner_hint", lambda _port: " PID=4321")
        monkeypatch.setattr(
            cli, "_canonical_dev_database_binding", forbidden("storage")
        )
        monkeypatch.setattr(
            cli, "_require_dev_cutover_activation", forbidden("activation")
        )
        dev_root = tmp_path / "dev-world"

        result = CliRunner().invoke(
            main,
            [
                "start", "--runtime-plane", "dev", "--port", "40008",
                "--dev-storage-root", str(dev_root),
            ],
        )

        assert result.exit_code != 0
        assert "not Aming Claw governance" in result.output
        assert "PID=4321" in result.output
        assert side_effects == []
        assert not dev_root.exists()

    def test_branch_service_validate_defaults_to_40008_and_binds_anchor(
        self, monkeypatch, tmp_path
    ):
        import agent.cli as cli

        result = CliRunner().invoke(
            main,
            [
                "branch-service",
                "validate",
                "--worktree",
                str(tmp_path),
                "--shared-volume-path",
                str(tmp_path / "shared"),
            ],
        )

        assert result.exit_code != 0
        assert "Shared-database branch-service validation is retired" in result.output
        assert "--runtime-plane dev --dev-storage-root" in result.output

    def test_dev_database_binding_rejects_alternate_ac_shaped_volume(
        self, monkeypatch, tmp_path
    ):
        import agent.cli as cli

        source = tmp_path / "ac-dev"
        source.mkdir()
        stable = tmp_path / "stable"
        canonical_shared = stable / "shared-volume"
        database = (
            canonical_shared
            / "codex-tasks"
            / "state"
            / "governance"
            / "aming-claw"
            / "governance.db"
        )
        database.parent.mkdir(parents=True)
        database.touch()
        alternate = tmp_path / "alternate"
        alternate.mkdir()
        monkeypatch.setattr(
            cli,
            "_source_git_identity",
            lambda: {
                "root": str(source),
                "branch": cli.AC_DEV_BRANCH,
                "commit": "b" * 40,
                "dirty": "",
            },
        )
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *_args, **_kwargs: types.SimpleNamespace(
                returncode=0,
                stdout=(
                    f"worktree {stable}\n"
                    f"branch refs/heads/{cli.AC_STABLE_BRANCH}\n"
                ),
                stderr="",
            ),
        )

        with pytest.raises(cli.click.ClickException, match="exact stable-worktree"):
            cli._canonical_stable_database_binding(
                str(alternate),
                stable_anchor_commit=cli.AC_STABLE_ANCHOR_COMMIT,
            )


def test_branch_service_orphan_handoff_cli_is_two_phase_and_copy_safe(
    tmp_path,
    monkeypatch,
):
    import agent.cli as cli

    orphan = tmp_path / "orphan"
    successor = tmp_path / "successor"
    orphan.mkdir()
    successor.mkdir()
    inspect_result = CliRunner().invoke(
        main,
        [
            "branch-service",
            "adopt-stop-handoff",
            "--orphan-worktree",
            str(orphan),
            "--successor-worktree",
            str(successor),
            "--orphan-pid",
            "43210",
        ],
    )
    assert inspect_result.exit_code != 0
    assert "Shared-database orphan handoff is retired" in inspect_result.output
    assert "fresh genesis" in inspect_result.output


def test_branch_service_orphan_handoff_cli_never_authorizes_kill_at_inspection(
    tmp_path,
):
    orphan = tmp_path / "orphan"
    successor = tmp_path / "successor"
    orphan.mkdir()
    successor.mkdir()
    result = CliRunner().invoke(
        main,
        [
            "branch-service",
            "adopt-stop-handoff",
            "--orphan-worktree",
            str(orphan),
            "--successor-worktree",
            str(successor),
            "--orphan-pid",
            "43210",
            "--allow-kill",
        ],
    )
    assert result.exit_code != 0
    assert "Shared-database orphan handoff is retired" in result.output


def test_v27_dev_startup_does_not_use_cutover_marker_as_authority():
    import inspect

    import agent.cli as cli
    from agent.governance import server

    start_source = inspect.getsource(cli.start.callback)
    startup_source = inspect.getsource(server._validate_runtime_plane_startup)
    assert "_require_dev_cutover_activation(" not in start_source
    assert "validate_dev_world_cutover_activation(" not in startup_source
    assert "AMING_CLAW_DEV_CUTOVER_PREFLIGHT_HASH" not in start_source


def test_dev_admit_schema_rejects_wrong_plane_before_database_write(tmp_path):
    root = tmp_path / "external-dev-world"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    import sqlite3

    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE backlog_bugs (bug_id TEXT, updated_at TEXT, created_at TEXT)")
    conn.executemany(
        "INSERT INTO schema_meta VALUES (?, ?)",
        [("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"), ("governance_world_source_tip_json", "{}")],
    )
    conn.commit()
    conn.close()
    (root / "launch-receipt.json").write_text(
        json.dumps({"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}),
        encoding="utf-8",
    )
    before = database.read_bytes()
    result = CliRunner().invoke(
        main,
        ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "wrong", "--port", "40008"],
    )
    assert result.exit_code != 0
    assert database.read_bytes() == before


def test_dev_admit_schema_repairs_exact_missing_set_offline(tmp_path, monkeypatch):
    import agent.cli as cli
    from agent.governance import db
    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    root = tmp_path / "external-dev-world"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    import sqlite3

    conn = sqlite3.connect(database)
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"), ("governance_world_source_tip_json", "{}")])
    conn.commit()
    conn.close()
    (root / "launch-receipt.json").write_text(json.dumps({"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")
    result = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["status"] == "admitted"
    assert Path(output["receipt_path"]).is_file()
    receipt_path = Path(output["receipt_path"])
    assert output["receipt_sha256"] == "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT generation FROM dashboard_backlog_cache_generation WHERE resource='backlog'").fetchone()[0] >= 1
    finally:
        conn.close()
    resumed = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008", "--resume-receipt", str(receipt_path)])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["status"] == "already_admitted"


def test_dev_admit_authority_schema_offline_receipt_and_resume(tmp_path, monkeypatch):
    """The new CLI path uses the same quarantine/backup receipt choreography."""
    import agent.cli as cli
    from agent.governance import db
    import sqlite3

    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    root = tmp_path / "external-authority-world"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [
        ("governance_world_id", "ac-dev"),
        ("governance_world_genesis_json", "{}"),
        ("governance_world_source_tip_json", "{}"),
    ])
    conn.commit(); conn.close()
    (root / "launch-receipt.json").write_text(
        json.dumps({"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}),
        encoding="utf-8",
    )
    command = ["dev-admit-authority-schema", "--dev-storage-root", str(root),
               "--project-id", "aming-claw", "--port", "40008"]
    first = CliRunner().invoke(main, command)
    assert first.exit_code == 0, first.output
    output = json.loads(first.output)
    receipt = Path(output["receipt_path"])
    assert output["status"] == "admitted" and receipt.is_file()
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    durable_hash = "sha256:" + hashlib.sha256(database.read_bytes()).hexdigest()
    assert receipt_payload["changed"] is True
    assert receipt_payload["database_sha256_after"] == durable_hash == output["post_sha256"]
    conn = sqlite3.connect(database)
    try:
        assert db.authority_projection_schema_drift(conn) == {"missing": [], "invalid": []}
    finally:
        conn.close()
    resumed = CliRunner().invoke(main, command + ["--resume-receipt", str(receipt)])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["status"] == "already_admitted"


def test_dev_admit_authority_schema_existing_byte_recertification_is_read_only_and_stale_resume_fails(tmp_path, monkeypatch):
    import agent.cli as cli
    from agent.governance import db
    import sqlite3

    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    monkeypatch.setattr(cli, "_source_git_identity", lambda: {
        "root": "/source/ac-dev", "branch": "codex/ac-dev", "commit": "a" * 40,
        "tree": "b" * 40, "source_sha256": "sha256:" + "c" * 64, "dirty": "",
    })
    root = tmp_path / "external-authority-recertify"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [
        ("governance_world_id", "ac-dev"),
        ("governance_world_genesis_json", "{}"),
        ("governance_world_source_tip_json", "{}"),
    ])
    conn.commit(); conn.close()
    (root / "launch-receipt.json").write_text(json.dumps(
        {"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")
    command = ["dev-admit-authority-schema", "--dev-storage-root", str(root),
               "--project-id", "aming-claw", "--port", "40008"]
    admitted = CliRunner().invoke(main, command)
    assert admitted.exit_code == 0, admitted.output
    stale_receipt = Path(json.loads(admitted.output)["receipt_path"])

    generic = CliRunner().invoke(main, command + ["--recertify-existing-bytes"])
    assert generic.exit_code != 0 and "requires an argument" in generic.output
    same_hash_bytes = database.read_bytes()
    same_hash = CliRunner().invoke(
        main, command + ["--recertify-existing-bytes", str(stale_receipt)]
    )
    assert same_hash.exit_code != 0
    assert "requires a stale completed receipt" in same_hash.output
    assert database.read_bytes() == same_hash_bytes

    changed = sqlite3.connect(database)
    changed.execute("PRAGMA user_version=313")
    changed.commit(); changed.close()
    current_bytes = database.read_bytes()
    rejected = CliRunner().invoke(main, command + ["--resume-receipt", str(stale_receipt)])
    assert rejected.exit_code != 0
    assert "completed receipt does not match current database" in rejected.output
    assert database.read_bytes() == current_bytes
    conflicting = CliRunner().invoke(main, command + [
        "--resume-receipt", str(stale_receipt),
        "--recertify-existing-bytes", str(stale_receipt),
    ])
    assert conflicting.exit_code != 0
    assert "no resume receipt" in conflicting.output
    assert database.read_bytes() == current_bytes
    # The source-owned repair necessarily runs from a successor CLI commit;
    # historical source identity remains immutable predecessor evidence.
    monkeypatch.setattr(cli, "_source_git_identity", lambda: {
        "root": "/source/ac-dev", "branch": "codex/ac-dev", "commit": "d" * 40,
        "tree": "e" * 40, "source_sha256": "sha256:" + "f" * 64, "dirty": "",
    })

    old_receipt_bytes = stale_receipt.read_bytes()
    old_sidecar = stale_receipt.with_suffix(".sha256")
    old_sidecar_bytes = old_sidecar.read_bytes()
    recertified = CliRunner().invoke(
        main, command + ["--recertify-existing-bytes", str(stale_receipt)]
    )
    assert recertified.exit_code == 0, recertified.output
    output = json.loads(recertified.output)
    assert output["status"] == "recertified_existing_bytes"
    assert output["changed"] is False and output["missing"] == []
    assert database.read_bytes() == current_bytes
    receipt = Path(output["receipt_path"])
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["previous_receipt_sha256"] == "sha256:" + hashlib.sha256(old_receipt_bytes).hexdigest()
    assert payload["database_sha256_before"] == payload["database_sha256_after"]
    assert payload["database_sha256_after"] == "sha256:" + hashlib.sha256(current_bytes).hexdigest()
    assert stale_receipt.read_bytes() == old_receipt_bytes
    assert old_sidecar.read_bytes() == old_sidecar_bytes
    resumed = CliRunner().invoke(main, command + ["--resume-receipt", str(receipt)])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["status"] == "already_admitted"


@pytest.mark.parametrize(
    "mismatch", [
        "rolled_back", "project", "port", "root", "database", "source", "plan",
        "inventory_count", "inventory_hash", "backup", "chain", "content_hash",
        "sidecar", "symlink", "wrong_path",
    ],
)
def test_dev_admit_authority_schema_recertification_rejects_foreign_predecessor_before_target_sqlite_open(tmp_path, monkeypatch, mismatch):
    import agent.cli as cli
    from agent.governance import db
    import sqlite3

    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    monkeypatch.setattr(cli, "_source_git_identity", lambda: {
        "root": "/source/ac-dev", "branch": "codex/ac-dev", "commit": "a" * 40,
        "tree": "b" * 40, "source_sha256": "sha256:" + "c" * 64, "dirty": "",
    })
    root = tmp_path / "external-authority-noncompleted"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database); conn.execute("PRAGMA journal_mode=WAL")
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [
        ("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"),
        ("governance_world_source_tip_json", "{}"),
    ]); conn.commit(); conn.close()
    (root / "launch-receipt.json").write_text(json.dumps(
        {"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")
    command = ["dev-admit-authority-schema", "--dev-storage-root", str(root),
               "--project-id", "aming-claw", "--port", "40008"]
    admitted = CliRunner().invoke(main, command)
    assert admitted.exit_code == 0, admitted.output
    completed = Path(json.loads(admitted.output)["receipt_path"])
    archive = completed.parent
    payload = json.loads(completed.read_text(encoding="utf-8"))
    forged = completed
    if mismatch == "rolled_back":
        payload["stage"] = "rolled_back"
    elif mismatch == "project":
        payload["project_id"] = "foreign"
    elif mismatch == "port":
        payload["port"] = 40009
    elif mismatch == "root":
        payload["root_identity"] = {**payload["root_identity"], "inode": -1}
    elif mismatch == "database":
        payload["database_identity"] = {**payload["database_identity"], "inode": -1}
    elif mismatch == "source":
        payload["source_identity"] = {**payload["source_identity"], "cli_source_sha256": "sha256:" + "0" * 64}
    elif mismatch == "plan":
        payload["plan_sha256"] = "sha256:" + "0" * 64
    elif mismatch == "inventory_count":
        payload["schema_inventory_after"] = {
            **payload["schema_inventory_after"],
            "inventory": payload["schema_inventory_after"]["inventory"][:-1],
        }
    elif mismatch == "inventory_hash":
        payload["schema_inventory_after"] = {
            **payload["schema_inventory_after"], "sha256": "sha256:" + "0" * 64,
        }
    elif mismatch == "backup":
        payload["backup"] = {**payload["backup"], "sha256": "sha256:" + "0" * 64}
    elif mismatch == "chain":
        payload["previous_receipt_sha256"] = "sha256:" + "0" * 64
    elif mismatch == "content_hash":
        completed.write_bytes(completed.read_bytes() + b"\n")
    elif mismatch == "sidecar":
        completed.with_suffix(".sha256").write_text("foreign\n", encoding="utf-8")
    elif mismatch == "symlink":
        forged = archive / ("0" * 64 + ".json")
        forged.symlink_to(completed)
    elif mismatch == "wrong_path":
        forged = tmp_path / completed.name
        cli.shutil.copy2(completed, forged)
    if mismatch not in {"content_hash", "sidecar", "symlink", "wrong_path"}:
        forged, _digest = cli._write_admission_receipt(archive, payload)
    opened = []
    writer_calls = []
    original_connect = cli.sqlite3.connect
    def tracked_connect(target, *args, **kwargs):
        opened.append(str(target))
        return original_connect(target, *args, **kwargs)
    monkeypatch.setattr(cli.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(cli, "_write_admission_receipt", lambda *_args, **_kwargs: writer_calls.append("receipt"))
    monkeypatch.setattr(cli.shutil, "copy2", lambda *_args, **_kwargs: writer_calls.append("copy"))
    monkeypatch.setattr(cli.os, "replace", lambda *_args, **_kwargs: writer_calls.append("replace"))
    monkeypatch.setattr(cli.Path, "mkdir", lambda *_args, **_kwargs: writer_calls.append("mkdir"))
    before = database.read_bytes()
    rejected = CliRunner().invoke(
        main, command + ["--recertify-existing-bytes", str(forged)]
    )
    assert rejected.exit_code != 0
    assert database.read_bytes() == before
    assert opened == []
    assert writer_calls == []


@pytest.mark.parametrize("path_class", ["missing", "nonregular"])
def test_dev_admit_authority_schema_recertification_path_rejection_has_zero_connect_or_writer(tmp_path, monkeypatch, path_class):
    import agent.cli as cli

    root = tmp_path / "root"; root.mkdir()
    predecessor = tmp_path / "missing.json"
    if path_class == "nonregular":
        predecessor.mkdir()
    connect_calls = []
    writer_calls = []
    monkeypatch.setattr(cli.sqlite3, "connect", lambda *_args, **_kwargs: connect_calls.append("connect"))
    monkeypatch.setattr(cli, "_write_admission_receipt", lambda *_args, **_kwargs: writer_calls.append("receipt"))
    monkeypatch.setattr(cli.shutil, "copy2", lambda *_args, **_kwargs: writer_calls.append("copy"))
    monkeypatch.setattr(cli.os, "replace", lambda *_args, **_kwargs: writer_calls.append("replace"))
    monkeypatch.setattr(cli.Path, "mkdir", lambda *_args, **_kwargs: writer_calls.append("mkdir"))
    result = CliRunner().invoke(main, [
        "dev-admit-authority-schema", "--dev-storage-root", str(root),
        "--project-id", "aming-claw", "--port", "40008",
        "--recertify-existing-bytes", str(predecessor),
    ])
    assert result.exit_code != 0
    assert connect_calls == [] and writer_calls == []


@pytest.mark.parametrize("drift", ["wal_replacement", "database_replacement", "database_hash"])
def test_dev_admit_authority_schema_rejects_real_post_checkpoint_identity_or_hash_drift(tmp_path, monkeypatch, drift):
    import agent.cli as cli
    from agent.governance import db
    import sqlite3

    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    root = tmp_path / ("external-authority-" + drift)
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database); conn.execute("PRAGMA journal_mode=WAL")
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [
        ("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"),
        ("governance_world_source_tip_json", "{}"),
    ]); conn.commit(); conn.close()
    (root / "launch-receipt.json").write_text(json.dumps(
        {"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")

    def hostile_drift():
        if drift == "wal_replacement":
            wal = Path(str(database) + "-wal")
            replacement = tmp_path / "replacement-wal"
            replacement.write_bytes(wal.read_bytes())
            os.replace(replacement, wal)
        elif drift == "database_replacement":
            replacement = tmp_path / "replacement-db"
            replacement.write_bytes(database.read_bytes())
            os.replace(replacement, database)
        else:
            with database.open("r+b") as handle:
                handle.seek(68)
                value = handle.read(1)
                handle.seek(68)
                handle.write(bytes([value[0] ^ 1]))

    with pytest.raises(cli.click.ClickException, match="identity|sidecar|drifted"):
        cli._offline_dev_schema_admission(
            root, project_id="aming-claw", port=40008, resume_receipt=None,
            authority_projection=True, _after_checkpoint_for_test=hostile_drift,
        )
    archive = root / "archive" / "schema-admission"
    assert not list(archive.glob("*.json"))
    if drift != "database_replacement":
        check = sqlite3.connect(database)
        try:
            assert dict(check.execute("SELECT key,value FROM schema_meta"))["governance_world_source_tip_json"] == "{}"
        finally:
            check.close()


def test_dev_admit_authority_schema_checkpoint_busy_emits_no_completed_receipt(tmp_path, monkeypatch):
    import agent.cli as cli
    from agent.governance import db
    import sqlite3
    import types

    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    root = tmp_path / "external-authority-busy"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database); conn.execute("PRAGMA journal_mode=WAL")
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [
        ("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"),
        ("governance_world_source_tip_json", "{}"),
    ]); conn.commit(); conn.close()
    (root / "launch-receipt.json").write_text(json.dumps(
        {"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")
    reader = sqlite3.connect(database)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    original_run = cli.subprocess.run
    def no_external_holders(args, *positional, **kwargs):
        if args and args[0] == "lsof":
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return original_run(args, *positional, **kwargs)
    monkeypatch.setattr(cli.subprocess, "run", no_external_holders)
    try:
        with pytest.raises(cli.click.ClickException, match="checkpoint did not truncate"):
            cli._offline_dev_schema_admission(
                root, project_id="aming-claw", port=40008,
                resume_receipt=None, authority_projection=True,
            )
    finally:
        reader.close()
    assert not list((root / "archive" / "schema-admission").glob("*.json"))


def test_dev_admit_authority_schema_rollback_receipt_resumes_without_drift(tmp_path, monkeypatch):
    import agent.cli as cli
    from agent.governance import db
    import sqlite3

    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    root = tmp_path / "external-authority-rollback"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database); conn.execute("PRAGMA journal_mode=WAL"); db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [
        ("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"),
        ("governance_world_source_tip_json", "{}"),
    ]); conn.commit(); conn.close()
    (root / "launch-receipt.json").write_text(json.dumps(
        {"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")
    command = ["dev-admit-authority-schema", "--dev-storage-root", str(root),
               "--project-id", "aming-claw", "--port", "40008"]
    original = db.admit_missing_authority_projection_schema
    def interrupted(connection):
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE authority_admission_interrupted (id TEXT)")
        raise sqlite3.OperationalError("forced authority interruption")
    monkeypatch.setattr(db, "admit_missing_authority_projection_schema", interrupted)
    failed = CliRunner().invoke(main, command)
    assert failed.exit_code != 0
    match = re.search(r"receipt=([^ ]+) sha256=(sha256:[0-9a-f]{64})", failed.output)
    assert match, failed.output
    receipt = Path(match.group(1)); payload = json.loads(receipt.read_text())
    assert payload["stage"] == "rolled_back"
    assert payload["schema_inventory_before"] == payload["schema_inventory_after"]
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='authority_admission_interrupted'").fetchone() is None
        assert dict(conn.execute("SELECT key,value FROM schema_meta"))["governance_world_source_tip_json"] == "{}"
    finally:
        conn.close()
    monkeypatch.setattr(db, "admit_missing_authority_projection_schema", original)
    resumed = CliRunner().invoke(main, command + ["--resume-receipt", str(receipt)])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["status"] == "admitted"
    before = database.read_bytes()
    receipt.write_text("{}", encoding="utf-8")
    tampered = CliRunner().invoke(main, command + ["--resume-receipt", str(receipt)])
    assert tampered.exit_code != 0 and database.read_bytes() == before


def test_dev_admit_schema_rejects_tampered_or_foreign_resume_before_effect(tmp_path, monkeypatch):
    import agent.cli as cli
    from agent.governance import db
    import sqlite3
    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    root = tmp_path / "external-dev-world"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database)
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"), ("governance_world_source_tip_json", "{}")])
    conn.commit()
    conn.close()
    (root / "launch-receipt.json").write_text(json.dumps({"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")
    first = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008"])
    assert first.exit_code == 0, first.output
    receipt = Path(json.loads(first.output)["receipt_path"])
    before = database.read_bytes()
    receipt.write_text("{}", encoding="utf-8")
    tampered = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008", "--resume-receipt", str(receipt)])
    assert tampered.exit_code != 0
    assert "digest mismatch" in tampered.output
    assert database.read_bytes() == before
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008", "--resume-receipt", str(foreign)])
    assert result.exit_code != 0
    assert database.read_bytes() == before


def test_dev_admit_schema_forced_ddl_error_emits_rollback_receipt_and_resumes(tmp_path, monkeypatch):
    import agent.cli as cli
    from agent.governance import db
    import sqlite3
    monkeypatch.setattr(cli, "_port_is_open", lambda _port: False)
    root = tmp_path / "external-dev-world"
    database = root / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database)
    db._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [("governance_world_id", "ac-dev"), ("governance_world_genesis_json", "{}"), ("governance_world_source_tip_json", "{}")])
    conn.commit()
    conn.close()
    (root / "launch-receipt.json").write_text(json.dumps({"world_id": "ac-dev", "project_id": "aming-claw", "port": 40008}), encoding="utf-8")

    def forced_ddl_error(connection):
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE interrupted_schema_admission (id TEXT)")
        raise sqlite3.OperationalError("forced DDL interruption")

    original_admission = db.admit_missing_backlog_read_schema
    monkeypatch.setattr(db, "admit_missing_backlog_read_schema", forced_ddl_error)
    failed = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008"])
    assert failed.exit_code != 0
    match = re.search(r"receipt=([^ ]+) sha256=(sha256:[0-9a-f]{64})", failed.output)
    assert match, failed.output
    rollback_receipt = Path(match.group(1))
    payload = json.loads(rollback_receipt.read_text(encoding="utf-8"))
    assert payload["stage"] == "rolled_back"
    assert payload["schema_inventory_before"] == payload["schema_inventory_after"]
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT name FROM sqlite_master WHERE name='interrupted_schema_admission'").fetchone() is None
    finally:
        check.close()
    monkeypatch.setattr(db, "admit_missing_backlog_read_schema", original_admission)
    rollback_database_bytes = database.read_bytes()

    changed = sqlite3.connect(database)
    changed.execute("PRAGMA user_version = 194")
    changed.commit()
    changed.close()
    user_version_drift = database.read_bytes()
    rejected_user_version = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008", "--resume-receipt", str(rollback_receipt)])
    assert rejected_user_version.exit_code != 0
    assert "rollback receipt does not match current database" in rejected_user_version.output
    assert database.read_bytes() == user_version_drift

    database.write_bytes(rollback_database_bytes)
    arbitrary_bytes = bytearray(database.read_bytes())
    arbitrary_bytes[68] ^= 1  # SQLite application_id: semantically inert but byte-distinct.
    database.write_bytes(arbitrary_bytes)
    arbitrary_byte_drift = database.read_bytes()
    rejected_byte_drift = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008", "--resume-receipt", str(rollback_receipt)])
    assert rejected_byte_drift.exit_code != 0
    assert "rollback receipt does not match current database" in rejected_byte_drift.output
    assert database.read_bytes() == arbitrary_byte_drift

    database.write_bytes(rollback_database_bytes)
    resumed = CliRunner().invoke(main, ["dev-admit-schema", "--dev-storage-root", str(root), "--project-id", "aming-claw", "--port", "40008", "--resume-receipt", str(rollback_receipt)])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["status"] == "admitted"


def test_admission_database_sha256_reads_bounded_chunks_without_mutating_file(tmp_path, monkeypatch):
    import agent.cli as cli

    database = tmp_path / "governance.db"
    payload = b"AC-dev-digest\n" * (3 * 1024 * 1024 // len(b"AC-dev-digest\n") + 1)
    database.write_bytes(payload)
    before = database.read_bytes()
    expected_identity = cli._admission_identity(database)
    original_read = cli.os.read
    read_sizes = []

    def bounded_read(descriptor, amount):
        read_sizes.append(amount)
        return original_read(descriptor, amount)

    monkeypatch.setattr(cli.os, "read", bounded_read)
    digest = cli._admission_database_sha256(database, expected_identity=expected_identity)

    assert digest == "sha256:" + hashlib.sha256(before).hexdigest()
    assert read_sizes and max(read_sizes) <= 1024 * 1024
    assert database.read_bytes() == before


def test_posix_detached_popen_survives_launcher_parent_on_real_temp_port(tmp_path):
    import socket
    import signal
    import time

    if os.name != "posix":
        pytest.skip("POSIX lifecycle only")
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    log = tmp_path / "child.log"
    child_code = (
        "import socket,time,sys; "
        "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
        "s.bind(('127.0.0.1',int(sys.argv[1]))); s.listen(); time.sleep(60)"
    )
    launcher_code = (
        "import os,sys; from pathlib import Path; from agent.cli import _posix_detached_popen; "
        "fd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
        "p=_posix_detached_popen([sys.executable,'-c',sys.argv[3],sys.argv[1]],cwd=Path.cwd(),log_fd=fd); "
        "os.close(fd); print(p.pid,flush=True)"
    )
    launcher = subprocess.run(
        [sys.executable, "-c", launcher_code, str(port), str(log), child_code],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
        timeout=10, check=True,
    )
    pid = int(launcher.stdout.strip())
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("detached child did not survive launcher parent")
        os.kill(pid, 0)
    finally:
        os.kill(pid, signal.SIGTERM)


def test_posix_exclusive_durable_receipt_rejects_collision_and_symlink(tmp_path):
    import agent.cli as cli

    receipt = tmp_path / "receipt.json"
    cli._posix_exclusive_json(receipt, {"stage": "pending"})
    before = receipt.read_bytes()
    with pytest.raises(cli.click.ClickException, match="collision"):
        cli._posix_exclusive_json(receipt, {"stage": "completed"})
    assert receipt.read_bytes() == before
    target = tmp_path / "target"; target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.json"; link.symlink_to(target)
    with pytest.raises(cli.click.ClickException, match="canonical"):
        cli._posix_exclusive_json(link, {"stage": "pending"})

    pending = tmp_path / "launch.pending.json"
    completed = tmp_path / "launch.completed.json"
    cli._posix_exclusive_json(pending, {"stage": "completed", "final": True})
    inode = pending.stat().st_ino
    cli._noreplace_promote(pending, completed)
    assert not pending.exists()
    assert completed.stat().st_ino == inode
    assert json.loads(completed.read_text(encoding="utf-8")) == {"final": True, "stage": "completed"}


def test_posix_durable_launch_lock_has_exactly_one_concurrent_winner(tmp_path):
    import concurrent.futures
    import agent.cli as cli

    lock = tmp_path / "launch.lock"
    def contender(number):
        try:
            cli._posix_exclusive_json(lock, {"winner": number})
            return number
        except cli.click.ClickException:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(contender, range(16)))
    winners = [value for value in results if value is not None]
    assert len(winners) == 1
    assert json.loads(lock.read_text(encoding="utf-8")) == {"winner": winners[0]}

    launch, digest = cli._durable_content_receipt(tmp_path, "launch", {"immutable": True})
    assert launch.name == f"launch.{digest[7:]}.json"
    payload, read_digest = cli._read_durable_content_receipt(launch, "launch")
    assert payload == {"immutable": True} and read_digest == digest
    with pytest.raises(cli.click.ClickException, match="collision"):
        cli._durable_content_receipt(tmp_path, "launch", {"immutable": True})


def test_posix_detached_popen_uses_no_shell_new_session_and_devnull(tmp_path, monkeypatch):
    import agent.cli as cli

    captured = {}
    sentinel = object()
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kwargs: (
        captured.update(argv=argv, **kwargs) or sentinel
    ))
    result = cli._posix_detached_popen(
        [sys.executable, "-m", "agent.cli"], cwd=tmp_path, log_fd=17,
    )
    assert result is sentinel
    assert captured == {
        "argv": [sys.executable, "-m", "agent.cli"], "cwd": tmp_path,
        "stdin": subprocess.DEVNULL, "stdout": 17, "stderr": 17,
        "start_new_session": True, "shell": False, "close_fds": True,
    }


def test_durable_exit_binding_discriminates_every_authorized_launch_field():
    import copy
    import agent.cli as cli

    receipt = {
        "launch_id": "launch", "pid": 123,
        "process": {"start_identity": "sha256:" + "1" * 64},
        "argv": ["python", "-m", "agent.cli"], "cwd": "/source",
        "python": "/python", "source_commit": "a" * 40, "source_tree": "b" * 40,
        "server_sha256": "sha256:" + "2" * 64, "source_root": "/source",
        "database_path": "/dev/governance.db", "database_identity": {"device": 1, "inode": 2},
        "dev_storage_root": "/dev", "project_id": "aming-claw", "port": 40008,
        "policy": {"runtime_plane": "dev", "migration": "verify-only",
                   "stable_deployment": "deny", "graph_activation": "deny",
                   "background_workers": "deny"},
        "linked_v3_receipt_sha256": "sha256:" + "4" * 64,
        "log_path": "/dev/log", "log_identity": {"path": "/dev/log", "device": 3, "inode": 4},
        "health": {"pid": 123},
    }
    baseline = cli._durable_exit_binding(receipt, "sha256:" + "3" * 64)
    assert baseline["completed_receipt_sha256"] == "sha256:" + "3" * 64
    for field in ("launch_id", "pid", "argv", "cwd", "python", "source_commit", "source_tree",
                  "server_sha256", "source_root", "database_path", "database_identity",
                  "dev_storage_root", "project_id", "port", "policy",
                  "linked_v3_receipt_sha256", "log_path", "log_identity", "health"):
        mutated = copy.deepcopy(receipt)
        mutated[field] = ["mutated"] if field == "argv" else "mutated"
        assert cli._durable_exit_binding(mutated, "sha256:" + "3" * 64) != baseline, field
    mutated = copy.deepcopy(receipt); mutated["process"]["start_identity"] = "mutated"
    assert cli._durable_exit_binding(mutated, "sha256:" + "3" * 64) != baseline


@pytest.mark.parametrize("attack", ["identity_drift", "preforged_exit", "missing_exit_after_term"])
def test_durable_stop_attacks_fail_closed(tmp_path, monkeypatch, attack):
    import agent.cli as cli

    dev = tmp_path / "dev"; runtime = dev / "runtime" / "durable-launch"; runtime.mkdir(parents=True)
    source = tmp_path / "source"; (source / "agent" / "governance").mkdir(parents=True)
    server = source / "agent" / "governance" / "server.py"; server.write_text("server\n", encoding="utf-8")
    database = dev / "governance" / "aming-claw" / "governance.db"; database.parent.mkdir(parents=True); database.write_bytes(b"db")
    details = database.stat()
    log = runtime / "governance.log"; log.write_text("", encoding="utf-8")
    receipt = {
        "schema_version": cli._AC_DEV_DURABLE_LAUNCH_VERSION, "stage": "completed",
        "launch_id": "fixture", "pid": 424242, "project_id": "aming-claw", "port": 40008,
        "dev_storage_root": str(dev), "source_root": str(source), "source_commit": "a" * 40,
        "source_tree": "b" * 40, "server_sha256": "sha256:" + hashlib.sha256(server.read_bytes()).hexdigest(),
        "python": str(Path(sys.executable).resolve()), "database_path": str(database),
        "database_identity": {"path": str(database), "device": details.st_dev, "inode": details.st_ino},
        "process": {"start_identity": "sha256:" + "c" * 64,
                    "argv": f"{sys.executable} child", "cwd": str(source)},
        "argv": [sys.executable, "child"], "cwd": str(source), "exit_receipt": str(runtime / "exit-status.json"),
        "linked_v3_receipt_sha256": "sha256:" + "e" * 64,
        "policy": {"runtime_plane": "dev", "migration": "verify-only",
                   "stable_deployment": "deny", "graph_activation": "deny",
                   "background_workers": "deny"},
        "log_path": str(log), "log_identity": cli._admission_identity(log),
        "health": {"pid": 424242},
    }
    cli._durable_content_receipt(runtime, "launch", receipt)
    monkeypatch.setattr(cli, "_source_git_identity", lambda: {
        "root": str(source), "commit": "a" * 40, "tree": "b" * 40, "dirty": "",
    })
    if attack in {"preforged_exit", "missing_exit_after_term"}:
        cli._durable_content_receipt(runtime, "exit", {"forged": True})
        if attack == "missing_exit_after_term":
            for candidate in runtime.glob("exit.*.json"):
                candidate.unlink()
        monkeypatch.setattr(cli, "_posix_process_identity", lambda _pid: receipt["process"])
        monkeypatch.setattr(cli, "_durable_listener_pid", lambda _port: receipt["pid"])
        monkeypatch.setattr(cli, "_probe_governance", lambda *_args, **_kwargs: receipt["health"])
        expected = "exit receipt is missing"
    else:
        monkeypatch.setattr(cli, "_posix_process_identity", lambda _pid: {
            "start_identity": "sha256:" + "d" * 64, "argv": "attacker", "cwd": str(source),
        })
        expected = "process identity mismatch"
    monkeypatch.setattr(cli, "_validated_linked_v3_receipt", lambda *_args, **_kwargs: (
        "sha256:" + "e" * 64, {},
    ))
    signals = []
    def fake_kill(pid, sig):
        signals.append((pid, sig))
        if attack in {"missing_exit_after_term", "preforged_exit"} and sig == 0:
            raise ProcessLookupError
    monkeypatch.setattr(cli.os, "kill", fake_kill)
    with pytest.raises(cli.click.ClickException, match=expected):
        cli._durable_dev_stop(dev)
    if attack in {"missing_exit_after_term", "preforged_exit"}:
        assert signals == [(receipt["pid"], cli.signal.SIGTERM), (receipt["pid"], 0)]
    else:
        assert signals == []
