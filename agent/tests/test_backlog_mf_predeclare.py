"""Tests for MF predeclare/start endpoints (OPT-BACKLOG-CH6-MF-PREDECLARE).

AC12: 7 tests covering the full MF lifecycle state machine.
"""

from unittest.mock import MagicMock, patch

import pytest


def _make_ctx(bug_id="BUG-001", project_id="test-proj", **body_fields):
    """Build a minimal RequestContext-like object."""
    ctx = MagicMock()
    ctx.path_params = {"project_id": project_id, "bug_id": bug_id}
    ctx.body = body_fields
    return ctx


@pytest.fixture
def _mock_audit():
    """Patch audit_service.record to no-op."""
    with patch("agent.governance.server.audit_service") as mock_audit:
        yield mock_audit


@pytest.fixture
def _mock_db_open():
    """Patch get_connection: bug exists with status=OPEN."""
    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "bug_id": "BUG-001",
            "status": "OPEN",
            "details_md": "",
        }
        mock_gc.return_value = conn
        yield conn


@pytest.fixture
def _mock_db_mf_planned():
    """Patch get_connection: bug exists with status=MF_PLANNED and mf_id in details_md."""
    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "bug_id": "BUG-001",
            "status": "MF_PLANNED",
            "details_md": "\n\n<!-- MF-PREDECLARE mf_id=MF-2026-04-26-001 reason=This is a valid reason text -->"
        }
        mock_gc.return_value = conn
        yield conn


@pytest.fixture
def _mock_db_mf_in_progress():
    """Patch get_connection: bug exists with status=MF_IN_PROGRESS."""
    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "bug_id": "BUG-001",
            "status": "MF_IN_PROGRESS",
            "details_md": "",
        }
        mock_gc.return_value = conn
        yield conn


# --- predeclare-mf tests ---


def test_predeclare_open_to_planned(_mock_db_open, _mock_audit):
    """AC5/AC6: OPEN bug transitions to MF_PLANNED with mf_id stored."""
    from agent.governance.server import handle_backlog_predeclare_mf

    ctx = _make_ctx(
        mf_id="MF-2026-04-26-001",
        actor="test-user",
        reason="This is a sufficiently long reason for predeclare",
    )

    result = handle_backlog_predeclare_mf(ctx)

    assert result["ok"] is True
    assert result["status"] == "MF_PLANNED"
    assert result["mf_id"] == "MF-2026-04-26-001"


def test_predeclare_invalid_mf_id_format(_mock_db_open, _mock_audit):
    """AC3: Invalid mf_id format returns 422."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_predeclare_mf

    ctx = _make_ctx(
        mf_id="INVALID-ID",
        actor="test-user",
        reason="This is a sufficiently long reason for predeclare",
    )

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_predeclare_mf(ctx)

    assert exc_info.value.status == 422
    assert "mf_id" in str(exc_info.value.message).lower() or "invalid" in str(exc_info.value.code).lower()


def test_predeclare_short_reason(_mock_db_open, _mock_audit):
    """AC4: Reason < 20 chars returns 422."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_predeclare_mf

    ctx = _make_ctx(
        mf_id="MF-2026-04-26-001",
        actor="test-user",
        reason="too short",
    )

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_predeclare_mf(ctx)

    assert exc_info.value.status == 422
    assert "reason" in str(exc_info.value.code).lower()


def test_predeclare_already_in_progress(_mock_audit):
    """AC5: Bug not in OPEN status returns 422."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_predeclare_mf

    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "bug_id": "BUG-001",
            "status": "MF_IN_PROGRESS",
            "details_md": "",
        }
        mock_gc.return_value = conn

        ctx = _make_ctx(
            mf_id="MF-2026-04-26-001",
            actor="test-user",
            reason="This is a sufficiently long reason for predeclare",
        )

        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_predeclare_mf(ctx)

        assert exc_info.value.status == 422
        assert "OPEN" in exc_info.value.message


# --- start-mf tests ---


def test_start_planned_to_in_progress(_mock_db_mf_planned, _mock_audit):
    """AC7/AC8/AC9: MF_PLANNED bug with correct mf_id transitions to MF_IN_PROGRESS."""
    from agent.governance.server import handle_backlog_start_mf

    ctx = _make_ctx(
        mf_id="MF-2026-04-26-001",
        actor="test-user",
    )

    result = handle_backlog_start_mf(ctx)

    assert result["ok"] is True
    assert result["status"] == "MF_IN_PROGRESS"
    assert result["mf_id"] == "MF-2026-04-26-001"
    assert result["mf_type"] == "chain_rescue"
    assert result["bypass_policy"]["graph_governance"] == "enforce"
    assert result["bypass_policy"]["bypass_graph_governance"] is False


def test_start_system_recovery_bypasses_graph(_mock_db_mf_planned, _mock_audit):
    """System-recovery MF is the explicit graph-bypass profile."""
    from agent.governance.server import handle_backlog_start_mf

    ctx = _make_ctx(
        mf_id="MF-2026-04-26-001",
        actor="test-user",
        mf_type="system_recovery",
        observer_authorized=True,
        reason="Repair governance runtime while graph is unreliable",
    )

    result = handle_backlog_start_mf(ctx)

    assert result["ok"] is True
    assert result["mf_type"] == "system_recovery"
    assert result["bypass_policy"]["graph_governance"] == "bypass"
    assert result["bypass_policy"]["bypass_graph_governance"] is True


def test_start_chain_rescue_rejects_graph_bypass(_mock_db_mf_planned, _mock_audit):
    """Chain-rescue MF remains graph-governed; bypass requires system_recovery."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_start_mf

    ctx = _make_ctx(
        mf_id="MF-2026-04-26-001",
        actor="test-user",
        mf_type="chain_rescue",
        bypass_graph_governance=True,
    )

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_start_mf(ctx)

    assert exc_info.value.status == 422
    assert "system_recovery" in exc_info.value.message


def test_apply_mf_takeover_holds_current_task():
    """MF takeover can hold the current unfinished chain task."""
    import sqlite3
    from agent.governance.server import _apply_mf_takeover

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE tasks (
             task_id TEXT PRIMARY KEY,
             project_id TEXT,
             status TEXT,
             execution_status TEXT,
             metadata_json TEXT,
             error_message TEXT,
             completed_at TEXT,
             updated_at TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO tasks (task_id, project_id, status, execution_status, metadata_json) "
        "VALUES ('task-current', 'test-proj', 'queued', 'queued', '{}')"
    )

    takeover = _apply_mf_takeover(
        conn,
        "test-proj",
        "BUG-001",
        {
            "mf_id": "MF-2026-04-26-001",
            "actor": "test-user",
            "takeover_action": "hold_current_chain",
            "reason": "observer takes over failed chain",
        },
        {"current_task_id": "task-current"},
        {"mf_type": "chain_rescue"},
    )

    row = conn.execute("SELECT status, execution_status, metadata_json FROM tasks WHERE task_id='task-current'").fetchone()
    assert takeover["outcome"] == "observer_hold"
    assert row["status"] == "observer_hold"
    assert row["execution_status"] == "observer_hold"
    assert "mf_takeover" in row["metadata_json"]


def test_start_wrong_mf_id(_mock_audit):
    """AC8: Wrong mf_id returns 422 (ownership check fails)."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_start_mf

    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "bug_id": "BUG-001",
            "status": "MF_PLANNED",
            "details_md": "\n\n<!-- MF-PREDECLARE mf_id=MF-2026-04-26-001 -->",
        }
        mock_gc.return_value = conn

        ctx = _make_ctx(
            mf_id="MF-2026-04-26-999",
            actor="test-user",
        )

        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_start_mf(ctx)

        assert exc_info.value.status == 422
        assert "mf_id" in exc_info.value.code.lower() or "mismatch" in exc_info.value.code.lower()


# --- close from MF_IN_PROGRESS test ---


@patch("agent.governance.server.subprocess.run")
def test_close_from_mf_in_progress(_mock_subprocess, _mock_db_mf_in_progress, _mock_audit):
    """AC10/AC11: Closing from MF_IN_PROGRESS sets chain_stage='manual-fix'."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)

    ctx = _make_ctx(commit="abc123", actor="test-user")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
    assert result["chain_stage"] == "manual-fix"
