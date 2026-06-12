from __future__ import annotations

import io
import json

from agent.hooks import onboarding_guard


def _run_guard(payload: object, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = onboarding_guard.main(
        stdin=io.StringIO(json.dumps(payload)),
        stdout=stdout,
        stderr=stderr,
        env={} if env is None else env,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_denies_protected_tool_without_onboarding() -> None:
    code, stdout, stderr = _run_guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__aming_claw__backlog_close",
            "tool_input": {"bug_id": "AC-1"},
        }
    )

    assert code == 2
    assert stderr == ""
    response = json.loads(stdout)
    assert response["permissionDecision"] == "deny"
    assert "/aming-claw:onboard" in response["permissionDecisionReason"]


def test_allows_read_only_tool_without_onboarding() -> None:
    code, stdout, stderr = _run_guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__aming_claw__graph_status",
            "tool_input": {"project_id": "aming-claw"},
        }
    )

    assert code == 0
    assert stdout == ""
    assert stderr == ""


def test_allows_protected_tool_when_onboarded() -> None:
    env_code, env_stdout, env_stderr = _run_guard(
        {"tool_name": "parallel_branch_startup"},
        env={"AMING_CLAW_ONBOARDED": "1"},
    )
    payload_code, payload_stdout, payload_stderr = _run_guard(
        {
            "tool_name": "project bootstrap",
            "onboard_state": {"status": "complete"},
        }
    )

    assert (env_code, env_stdout, env_stderr) == (0, "", "")
    assert (payload_code, payload_stdout, payload_stderr) == (0, "", "")


def test_denied_attempt_writes_public_safe_audit_record(tmp_path) -> None:
    audit_path = tmp_path / "guard" / "audit.jsonl"
    code, stdout, stderr = _run_guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": "curl -X POST http://localhost:40000/api/project/bootstrap"
            },
        },
        env={"AMING_CLAW_ONBOARDING_GUARD_AUDIT": str(audit_path)},
    )

    assert code == 2
    assert stderr == ""
    assert json.loads(stdout)["permissionDecision"] == "deny"

    rows = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    record = json.loads(rows[0])
    assert record["schema_version"] == "aming_claw_onboarding_guard_audit.v1"
    assert record["decision"] == "deny"
    assert record["next_action"] == "/aming-claw:onboard"
    assert record["tool_match"] == "project_bootstrap"
    assert "tool_input" not in record
