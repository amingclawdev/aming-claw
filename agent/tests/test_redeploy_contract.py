"""Tests for redeploy_handler.py contract (PR-2).

Parametrized tests covering:
  (a) governance refuses target=governance (mutual-exclusion)
  (b) manager refuses target=service_manager (not a governance target — valid)
  (c) successful redeploy writes chain_version exactly once
  (d) failed restart does NOT write chain_version
  (e) version-update still responds but logs deprecation
"""
import json
import logging
import sqlite3
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeCtx:
    """Minimal RequestContext stand-in."""
    def __init__(self, body=None):
        self.body = body or {}
        self.handler = None

    def get_project_id(self):
        return "aming-claw"


# ---------------------------------------------------------------------------
# (a) Mutual-exclusion: governance refuses target=governance
# ---------------------------------------------------------------------------

class TestMutualExclusion:
    """R2: governance redeploy endpoints MUST refuse target=governance."""

    def test_governance_target_returns_400(self):
        from agent.governance.redeploy_handler import handle_redeploy

        result, status = handle_redeploy("governance", {
            "task_id": "test-task",
            "expected_head": "abc1234",
        })
        assert status == 400
        assert result["ok"] is False
        assert "mutual-exclusion" in result["error"].lower() or "cannot restart itself" in result["error"].lower()

    def test_governance_target_does_not_write_db(self):
        """DB write must NOT happen when target=governance."""
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._db_write_chain_version") as mock_db:
            result, status = handle_redeploy("governance", {
                "task_id": "test-task",
                "expected_head": "abc1234",
            })
            mock_db.assert_not_called()

    @pytest.mark.parametrize("target", ["executor", "gateway", "coordinator"])
    def test_valid_targets_accepted(self, target):
        """Valid targets should not return 400 for mutual-exclusion."""
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._find_pid_for_target", return_value=None), \
             patch("agent.governance.redeploy_handler._spawn_target", return_value=12345), \
             patch("agent.governance.redeploy_handler._health_check", return_value=True), \
             patch("agent.governance.redeploy_handler._db_write_chain_version", return_value=True):
            result, status = handle_redeploy(target, {
                "task_id": "test-task",
                "expected_head": "abc1234",
            })
            assert status == 200
            assert result["ok"] is True

    def test_service_manager_target_returns_400(self):
        from agent.governance.redeploy_handler import handle_redeploy

        result, status = handle_redeploy("service_manager", {
            "task_id": "test-task",
            "expected_head": "abc1234",
        })
        assert status == 400
        assert result["ok"] is False
        assert "supervisor" in result["error"].lower()


# ---------------------------------------------------------------------------
# (b) Unknown / invalid targets
# ---------------------------------------------------------------------------

class TestInvalidTarget:

    def test_unknown_target_returns_400(self):
        from agent.governance.redeploy_handler import handle_redeploy

        result, status = handle_redeploy("unknown_service", {
            "task_id": "t1",
            "expected_head": "abc",
        })
        assert status == 400
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# (c) Successful redeploy writes chain_version exactly once
# ---------------------------------------------------------------------------

class TestSuccessfulRedeploy:

    def test_chain_version_written_once_on_success(self):
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._find_pid_for_target", return_value=None), \
             patch("agent.governance.redeploy_handler._spawn_target", return_value=99999), \
             patch("agent.governance.redeploy_handler._health_check", return_value=True), \
             patch("agent.governance.redeploy_handler._db_write_chain_version", return_value=True) as mock_db:
            result, status = handle_redeploy("executor", {
                "task_id": "chain-task-1",
                "expected_head": "deadbeef",
                "drain_grace_seconds": 0,
            })
            assert status == 200
            assert result["ok"] is True
            assert result["new_chain_version"] == "deadbeef"
            # Written exactly once
            mock_db.assert_called_once_with("deadbeef", "chain-task-1", "executor")

    def test_db_write_uses_redeploy_orchestrator(self):
        """AC9: updated_by='redeploy-orchestrator' and includes task_id."""
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._find_pid_for_target", return_value=None), \
             patch("agent.governance.redeploy_handler._spawn_target", return_value=99999), \
             patch("agent.governance.redeploy_handler._health_check", return_value=True), \
             patch("agent.governance.redeploy_handler._db_write_chain_version", return_value=True) as mock_db:
            handle_redeploy("executor", {
                "task_id": "my-task-123",
                "expected_head": "abc",
                "drain_grace_seconds": 0,
            })
            args = mock_db.call_args[0]
            assert args[0] == "abc"       # expected_head
            assert args[1] == "my-task-123"  # task_id
            assert args[2] == "executor"  # target

    def test_db_write_chain_version_uses_governance_db_helper(self, tmp_path, monkeypatch):
        """Regression: DB write must not import the removed dbservice module."""
        from agent.governance.redeploy_handler import _db_write_chain_version

        monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))

        assert _db_write_chain_version("feed123", "deploy-task-1", "executor") is True

        db_path = (
            tmp_path
            / "codex-tasks"
            / "state"
            / "governance"
            / "aming-claw"
            / "governance.db"
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT chain_version, updated_by FROM project_version WHERE project_id=?",
                ("aming-claw",),
            ).fetchone()
            audit = conn.execute(
                "SELECT event, actor FROM audit_index WHERE project_id=?",
                ("aming-claw",),
            ).fetchone()
        finally:
            conn.close()

        assert row["chain_version"] == "feed123"
        assert row["updated_by"] == "redeploy-orchestrator"
        assert audit["event"] == "redeploy.version_write"
        assert audit["actor"] == "redeploy-orchestrator"

        audit_files = list((tmp_path / "codex-tasks" / "state" / "governance" / "aming-claw").glob("audit-*.jsonl"))
        assert audit_files
        raw_events = [
            json.loads(line)
            for path in audit_files
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        version_events = [event for event in raw_events if event["event"] == "redeploy.version_write"]
        assert version_events[-1]["task_id"] == "deploy-task-1"
        assert version_events[-1]["target"] == "executor"


# ---------------------------------------------------------------------------
# (d) Failed restart does NOT write chain_version
# ---------------------------------------------------------------------------

class TestFailedRedeploy:

    def test_executor_signal_write_failure_no_db_write(self, tmp_path):
        from agent.governance.redeploy_handler import handle_redeploy

        signal_path = tmp_path / "codex-tasks" / "state" / "manager_signal.json"
        with patch("agent.governance.redeploy_handler._manager_signal_path", return_value=signal_path), \
             patch("pathlib.Path.write_text", side_effect=OSError("disk full")), \
             patch("agent.governance.redeploy_handler._db_write_chain_version") as mock_db:
            result, status = handle_redeploy("executor", {
                "task_id": "t1",
                "expected_head": "abc",
                "drain_grace_seconds": 0,
            })
            assert status == 500
            assert result["ok"] is False
            assert result["step"] == "signal_write"
            mock_db.assert_not_called()


# ---------------------------------------------------------------------------
# (e) version-update still responds but logs deprecation
# ---------------------------------------------------------------------------

class TestVersionUpdateDeprecation:

    def test_handle_version_update_logs_deprecated(self, caplog):
        """AC10: handle_version_update contains log.warning with 'DEPRECATED'."""
        # We just verify the deprecation log is emitted on entry.
        # The function will fail due to missing DB, but that's expected.
        from agent.governance.server import handle_version_update

        ctx = _FakeCtx(body={
            "chain_version": "abc123",
            "updated_by": "auto-chain",
        })

        with caplog.at_level(logging.WARNING):
            try:
                handle_version_update(ctx)
            except Exception:
                pass  # Expected — no real DB connection

        deprecated_logged = any("deprecated_write_ignored" in record.message for record in caplog.records)
        assert deprecated_logged, f"Expected deprecated_write_ignored in log, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Pipeline step validation
# ---------------------------------------------------------------------------

class TestPipelineSteps:
    """AC4: 5-step pipeline with db_write gated on prior steps."""

    def test_missing_expected_head_returns_400(self):
        from agent.governance.redeploy_handler import handle_redeploy

        result, status = handle_redeploy("executor", {
            "task_id": "t1",
        })
        assert status == 400
        assert "expected_head" in result["error"]

    def test_executor_signal_pipeline_returns_expected_fields(self, tmp_path):
        from agent.governance.redeploy_handler import handle_redeploy

        signal_path = tmp_path / "codex-tasks" / "state" / "manager_signal.json"
        with patch("agent.governance.redeploy_handler._manager_signal_path", return_value=signal_path), \
             patch("agent.governance.redeploy_handler._db_write_chain_version", return_value=True):
            result, status = handle_redeploy("executor", {
                "task_id": "t1",
                "expected_head": "abc123",
                "drain_grace_seconds": 0,
            })
            assert status == 200
            assert result["ok"] is True
            assert result["target"] == "executor"
            assert result["mechanism"] == "manager_signal.json"
            assert result["signal_path"] == str(signal_path)
            assert result["new_chain_version"] == "abc123"
            assert result["db_write"] is True
            assert "updated_at" in result
