"""Tests for handle_backlog_close commit verification (OPT-BACKLOG-CH5).

AC6: At least 3 test functions covering real commit, fake commit, empty commit.
AC8: All tests use unittest.mock.patch to mock subprocess.run.
"""

import hashlib
import subprocess
from unittest.mock import MagicMock, patch

import pytest


def _fake_sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


_ROUTE_CONTEXT_HASH = _fake_sha("test-route-context-backlog-close")
_PROMPT_CONTRACT_HASH = _fake_sha("test-prompt-contract-backlog-close")
_VISIBLE_MANIFEST_HASH = _fake_sha("test-visible-manifest-backlog-close")


def _make_ctx(bug_id="BUG-001", commit="abc123", project_id="test-proj"):
    """Build a minimal RequestContext-like object for handle_backlog_close."""
    route_identity = _valid_route_token(bug_id=bug_id, project_id=project_id)
    ctx = MagicMock()
    ctx.path_params = {"project_id": project_id, "bug_id": bug_id}
    ctx.get_project_id.return_value = project_id
    ctx.body = {
        "commit": commit,
        "actor": "test",
        "route_waiver": {
            "accepted": True,
            "waiver_type": "manual_fix",
            "allowed_action": "backlog_close",
            "route_context_hash": route_identity["route_context_hash"],
            "prompt_contract_id": route_identity["prompt_contract_id"],
            "prompt_contract_hash": route_identity["prompt_contract_hash"],
            "caller_role": route_identity["caller_role"],
            "project_id": project_id,
            "backlog_id": bug_id,
            "reason": "Unit test supplies explicit route gate waiver evidence.",
            "timeline_evidence": {"event_id": "test-route-gate"},
        },
    }
    return ctx


def _valid_route_token(action="backlog_close", bug_id="BUG-001", project_id="test-proj"):
    return {
        "route_id": "route-test-backlog-close",
        "route_context_hash": _ROUTE_CONTEXT_HASH,
        "prompt_contract_id": "prompt-contract-backlog-close",
        "prompt_contract_hash": _PROMPT_CONTRACT_HASH,
        "caller_role": "observer",
        "allowed_action": action,
        "scope": {"project_id": project_id, "backlog_id": bug_id},
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:test-route-token-backlog-close"],
    }


def _route_context_consumption_events():
    identity = {
        "route_context_hash": _ROUTE_CONTEXT_HASH,
        "prompt_contract_id": "prompt-contract-backlog-close",
        "prompt_contract_hash": _PROMPT_CONTRACT_HASH,
        "visible_injection_manifest_hash": _VISIBLE_MANIFEST_HASH,
    }
    return [
        {
            "event_kind": "route_context",
            "phase": "dispatch",
            "status": "passed",
            "payload": {
                "route_context": {
                    **identity,
                    "caller_role": "observer",
                    "blocked_actions": ["apply_patch"],
                    "required_lanes": ["bounded_implementation_worker"],
                }
            },
        },
        {
            "event_kind": "route_action_precheck",
            "phase": "pre_mutation",
            "status": "allowed",
            "verification": {**identity, "allowed_action": "dispatch_worker"},
        },
        {
            "event_kind": "mf_subagent_dispatch",
            "phase": "dispatch",
            "status": "passed",
            "payload": {
                "mf_subagent_dispatch_gate": {
                    **identity,
                    "worker_id": "mf-sub-test",
                    "bounded": True,
                }
            },
        },
        {
            "event_kind": "mf_subagent_startup",
            "phase": "startup_gate",
            "status": "passed",
            "payload": {
                "mf_subagent_startup_gate": {
                    **identity,
                    "worker_id": "mf-sub-test",
                    "fence_token": "fence-test",
                    "actual_cwd": "/repo/.worktrees/mf-sub-test",
                    "actual_git_root": "/repo/.worktrees/mf-sub-test",
                    "branch": "refs/heads/codex/mf-sub-test",
                    "head_commit": "head-test",
                }
            },
        },
        {
            "event_kind": "qa_verification",
            "phase": "verification",
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
    ]


@pytest.fixture
def _mock_db():
    """Patch get_connection so SELECT returns a row and UPDATE/commit succeed."""
    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        # SELECT returns a row (bug exists) with OPEN status for close eligibility
        conn.execute.return_value.fetchone.return_value = {"bug_id": "BUG-001", "status": "OPEN"}
        mock_gc.return_value = conn
        yield conn


@pytest.fixture
def _mock_audit():
    """Patch audit_service.record to no-op."""
    with patch("agent.governance.server.audit_service") as mock_audit:
        yield mock_audit


@patch("agent.governance.server.subprocess.run")
def test_close_with_real_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC1/AC5: When commit resolves (returncode=0), close succeeds."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
    _mock_subprocess.assert_called_once()
    call_args = _mock_subprocess.call_args
    assert "git" in call_args[0][0]
    assert "rev-parse" in call_args[0][0]
    assert "--verify" in call_args[0][0]
    assert "abc123" in call_args[0][0]


@patch("agent.governance.server.subprocess.run")
def test_backlog_close_without_route_token_or_waiver_is_blocked(_mock_subprocess, _mock_db, _mock_audit):
    """Protected backlog_close rejects callers that provide no route gate evidence."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    ctx = _make_ctx(commit="abc123")
    ctx.body.pop("route_waiver")

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_close(ctx)

    assert exc_info.value.code == "route_token_required"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_backlog_close_accepts_valid_route_token(_mock_subprocess, _mock_db, _mock_audit):
    """Protected backlog_close accepts public route-token evidence through the HTTP body."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    ctx = _make_ctx(commit="abc123")
    ctx.body.pop("route_waiver")
    token = _valid_route_token()
    ctx.body["route_token"] = token

    binding = {
        **token,
        "server_issued_binding": True,
        "route_token_ref": "rtok-test-backlog-close",
        "binding_source": "observer_route_token_refs",
    }
    with patch(
        "agent.governance.server._verify_route_token_binding_server_side",
        return_value=binding,
    ):
        result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["route_token_gate"]["decision"] == "route_token"
    assert result["route_token_gate"]["server_issued_binding"] is True
    assert result["route_token_gate"]["scope"]["backlog_id"] == "BUG-001"


@patch("agent.governance.server.subprocess.run")
def test_close_with_fake_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC2: When commit doesn't resolve (returncode!=0), raise 422 commit_not_found."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=1, stderr="fatal: not a valid object")
    ctx = _make_ctx(commit="deadbeef999")

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_close(ctx)

    assert exc_info.value.code == "commit_not_found"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_close_with_empty_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC5: When commit is empty string, skip verification entirely."""
    from agent.governance.server import handle_backlog_close

    ctx = _make_ctx(commit="")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
    _mock_subprocess.assert_not_called()


@patch("agent.governance.server.subprocess.run")
def test_close_with_timeout(_mock_subprocess, _mock_db, _mock_audit):
    """AC3: When git times out, log warning and allow close."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"


@patch("agent.governance.server.subprocess.run")
def test_close_with_git_not_found(_mock_subprocess, _mock_db, _mock_audit):
    """AC4: When git binary not found, log warning and allow close."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.side_effect = FileNotFoundError("git not found")
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"


@patch("agent.governance.server.subprocess.run")
def test_mf_close_without_timeline_evidence_is_blocked(_mock_subprocess, _mock_db, _mock_audit):
    """Observer/MF close requires implementation, verification, and close-ready timeline rows."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
    }
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=[]):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_mf_like_policy_alias_close_cannot_skip_timeline_gate(_mock_subprocess, _mock_db, _mock_audit):
    """MF-like observer-hotfix policy rows must not close as ordinary backlog rows."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "",
        "bypass_policy_json": '{"mf_type": "observer-hotfix"}',
        "chain_stage": "",
    }
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=[]):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_mf_close_with_required_timeline_evidence_passes(_mock_subprocess, _mock_db, _mock_audit):
    """Required observer/MF timeline evidence is returned in the close response."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
    }
    events = [
        {"event_kind": "implementation", "phase": "implement", "status": "passed"},
        {"event_kind": "verification", "phase": "verify", "status": "passed"},
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["gate_summary"]["ok"] is True
    assert result["gate_summary"]["can_close"] is True
    assert result["gate_summary"]["missing_event_kinds"] == []
    assert result["gate_summary"]["event_count"] == 3


@patch("agent.governance.server.subprocess.run")
def test_mf_close_p1_governance_route_requires_consumed_route_context(
    _mock_subprocess,
    _mock_db,
    _mock_audit,
):
    """P0/P1 governance route work cannot close on a route waiver alone."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "priority": "P1",
        "target_files": '["agent/governance/service_router.py"]',
        "title": "Route close gate",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
        "chain_trigger_json": "{}",
    }
    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "passed"},
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert "bounded_implementation_worker_dispatch" in str(exc_info.value)
    assert "mf_subagent_startup" in str(exc_info.value)


@patch("agent.governance.server.subprocess.run")
def test_mf_close_instantiated_contract_missing_e2e_is_blocked(_mock_subprocess, _mock_db, _mock_audit):
    """Instantiated MF contracts can require specific timeline evidence before close."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
        "chain_trigger_json": {
            "parallel_contract": {
                "template_id": "mf_parallel.v1",
                "contract_instance_id": "BUG-001",
                "evidence_requirements": [
                    {"id": "unit_tests", "required": True, "phase": "verification"},
                    {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                ],
            }
        },
    }
    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "passed"},
        {
            "event_kind": "verification",
            "phase": "verification",
            "status": "passed",
            "verification": {"requirement_id": "unit_tests"},
        },
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        *_route_context_consumption_events(),
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert "dashboard_e2e" in str(exc_info.value)


@patch("agent.governance.server.subprocess.run")
def test_mf_close_subagent_lane_requirement_missing_is_reported(_mock_subprocess, _mock_db, _mock_audit):
    """Close errors include missing bounded subagent lane ownership evidence."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
        "chain_trigger_json": {
            "route_id": "route-lane-required",
            "route_context_hash": "sha256:lane-required",
            "required_lanes": [
                {"id": "bounded_implementation_subagent", "role": "implementation_worker"},
            ],
            "blocked_actions": ["edit_files_as_observer_or_independent_reviewer"],
        },
    }
    events = [
        {
            "event_kind": "implementation",
            "phase": "implementation",
            "actor": "codex-observer",
            "status": "accepted",
        },
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert "bounded_implementation_subagent.dispatch" in str(exc_info.value)
    assert "bounded_implementation_subagent.review_ready" in str(exc_info.value)


@patch("agent.governance.server.subprocess.run")
def test_mf_close_instantiated_contract_evidence_passes(_mock_subprocess, _mock_db, _mock_audit):
    """Contract requirement evidence is returned in the close response."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
        "chain_trigger_json": {
            "parallel_contract": {
                "template_id": "mf_parallel.v1",
                "contract_instance_id": "BUG-001",
                "evidence_requirements": [
                    {"id": "unit_tests", "required": True, "phase": "verification"},
                    {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                ],
            }
        },
    }
    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "passed"},
        {
            "event_kind": "verification",
            "phase": "verification",
            "status": "passed",
            "verification": {"requirement_id": "unit_tests"},
        },
        {
            "event_kind": "verification",
            "phase": "integration",
            "status": "passed",
            "verification": {
                "contract_evidence": [
                    {"requirement_id": "dashboard_e2e", "status": "passed", "command": "npm run e2e"}
                ]
            },
        },
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        *_route_context_consumption_events(),
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["gate_summary"]["ok"] is True
    assert result["gate_summary"]["can_close"] is True
    assert result["gate_summary"]["event_count"] == len(events)


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("route_context_hash", "sha256:wrong-close-route-context"),
        ("prompt_contract_id", "rprompt-wrong-close-contract"),
    ],
)
@patch("agent.governance.server.subprocess.run")
def test_mf_close_rejects_route_waiver_identity_mismatch_before_status_update(
    _mock_subprocess,
    _mock_db,
    _mock_audit,
    field,
    wrong_value,
):
    """A stale close waiver cannot override the route identity selected by timeline evidence."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "priority": "P1",
        "target_files": '["agent/governance/server.py"]',
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
        "chain_trigger_json": "{}",
    }
    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "passed"},
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        *_route_context_consumption_events(),
    ]
    ctx = _make_ctx(commit="abc123")
    ctx.body["route_waiver"][field] = wrong_value

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "route_token_required"
    assert exc_info.value.status == 422
    assert exc_info.value.details["route_identity_mismatch_fields"] == [field]
    assert (
        exc_info.value.details["expected_route_identity"][field]
        == _valid_route_token()[field]
    )
    assert exc_info.value.details["supplied_route_identity"][field] == wrong_value
    assert (
        exc_info.value.details["selected_route_identity_source"]
        == "mf_close_timeline_gate.route_context_gate.route_identity"
    )
    executed_sql = [
        str(call.args[0])
        for call in _mock_db.execute.call_args_list
        if call.args
    ]
    assert not any("UPDATE backlog_bugs" in sql for sql in executed_sql)
    _mock_db.commit.assert_not_called()


@patch("agent.governance.server.subprocess.run")
def test_mf_close_timeline_gate_explicit_bypass_requires_reason(_mock_subprocess, _mock_db, _mock_audit):
    """Timeline bypass is forbidden even for system_recovery rows."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "system_recovery",
        "bypass_policy_json": "{}",
        "chain_stage": "",
    }
    ctx = _make_ctx(commit="abc123")
    ctx.body["bypass_timeline_gate"] = True

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_bypass_reason_required"

    ctx.body["timeline_bypass_reason"] = "system recovery bootstrap could not write normal timeline"
    with patch("agent.governance.task_timeline.record_event") as record_event:
        with pytest.raises(GovernanceError) as forbidden:
            handle_backlog_close(ctx)

    assert forbidden.value.code == "mf_timeline_bypass_forbidden"
    assert record_event.call_count == 1
