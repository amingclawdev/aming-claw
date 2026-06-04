"""Tests for agent.cli — AC1, AC8."""

import os
import hashlib
import json
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
    assert payload["dispatch_gate"]["graph_evidence"]["trace_ids"] == ["gqt-20260602-testtrace"]
    assert payload["dispatch_gate"]["route_evidence"]["visible_injection_manifest_hash"] == DOGFOOD_VISIBLE_MANIFEST_HASH
    observer_run = payload["observer_run"]
    assert observer_run["status"] == "planned"
    evidence = observer_run["invocation"]
    assert evidence["calls_models"] is False
    assert evidence["auth_status"] == "not_invoked"
    assert evidence["route_prompt_contract"]["route_context_hash"] == DOGFOOD_ROUTE_CONTEXT_HASH


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
    assert written["graph_evidence"]["trace_ids"] == ["gqt-20260602-testtrace"]
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
    args = _replace_option(args, "--backend-mode", "fixture")

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
    assert payload["runtime_context"]["worktree_path"] == str(worker_worktree)
    assert payload["launch_text"]
    assert payload["launch_text_hash"].startswith("sha256:")
    assert payload["raw_launch_text_persisted"] is False
    assert payload["persistent_evidence"]["launch_text_hash"] == payload["launch_text_hash"]
    assert payload["persistent_evidence"]["startup_intent_event_generated"] is True
    assert payload["persistent_evidence"]["actual_startup_required"] is True
    assert payload["persistent_evidence"]["actual_startup_recorded"] is False
    assert payload["persistent_evidence"]["close_ready"] is False
    assert payload["startup_intent_event"]["event_kind"] == "mf_subagent_startup_intent"
    assert payload["startup_intent_event"]["close_satisfying"] is False
    assert payload["startup_intent_event"]["payload"]["mf_subagent_startup_intent"][
        "launch_text_hash"
    ] == payload["launch_text_hash"]
    assert "launch_text" not in payload["persistent_evidence"]


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
        "skills/aming-claw/SKILL.md",
        "skills/aming-claw-hn-challenge/SKILL.md",
        "skills/aming-claw-hn-demo/SKILL.md",
        "skills/aming-claw-hn-demo-after-work/SKILL.md",
        "skills/aming-claw-hn-demo-before-work/SKILL.md",
        "skills/aming-claw-hn-demo-during-work/SKILL.md",
        "skills/aming-claw-vibe-queue-demo/SKILL.md",
        "skills/aming-claw-drift-demo/SKILL.md",
        "skills/aming-claw-backlog-dupe-demo/SKILL.md",
        "skills/aming-claw-launcher/SKILL.md",
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

        skill = source / "skills" / "aming-claw" / "SKILL.md"
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

        skill = source / "skills" / "aming-claw" / "SKILL.md"
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
        runner = CliRunner(mix_stderr=False)
        contract_path = tmp_path / "dispatch.json"
        contract_path.write_text(json.dumps({"owned_files": []}), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "dispatch-gate",
            "--contract-file",
            str(contract_path),
        ])

        assert result.exit_code == 1
        assert result.output == ""
        assert "REJECT: MF subagent dispatch missing required fields:" in result.stderr
        assert "branch" in result.stderr

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
