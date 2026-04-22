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

    @pytest.mark.parametrize("target", ["executor", "gateway", "coordinator", "service_manager"])
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


# ---------------------------------------------------------------------------
# (d) Failed restart does NOT write chain_version
# ---------------------------------------------------------------------------

class TestFailedRedeploy:

    def test_spawn_failure_no_db_write(self):
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._find_pid_for_target", return_value=None), \
             patch("agent.governance.redeploy_handler._spawn_target", return_value=None), \
             patch("agent.governance.redeploy_handler._db_write_chain_version") as mock_db:
            result, status = handle_redeploy("executor", {
                "task_id": "t1",
                "expected_head": "abc",
                "drain_grace_seconds": 0,
            })
            assert status == 500
            assert result["ok"] is False
            assert result["step"] == "spawn"
            mock_db.assert_not_called()

    def test_health_check_failure_no_db_write(self):
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._find_pid_for_target", return_value=None), \
             patch("agent.governance.redeploy_handler._spawn_target", return_value=12345), \
             patch("agent.governance.redeploy_handler._health_check", return_value=False), \
             patch("agent.governance.redeploy_handler._db_write_chain_version") as mock_db:
            result, status = handle_redeploy("executor", {
                "task_id": "t1",
                "expected_head": "abc",
                "drain_grace_seconds": 0,
            })
            assert status == 500
            assert result["ok"] is False
            assert result["step"] == "wait"
            mock_db.assert_not_called()

    def test_stop_failure_no_db_write(self):
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._find_pid_for_target", return_value=54321), \
             patch("agent.governance.redeploy_handler._stop_process", return_value=False), \
             patch("agent.governance.redeploy_handler._db_write_chain_version") as mock_db:
            result, status = handle_redeploy("executor", {
                "task_id": "t1",
                "expected_head": "abc",
                "drain_grace_seconds": 0,
            })
            assert status == 500
            assert result["ok"] is False
            assert result["step"] == "stop"
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

        # Check that DEPRECATED was logged
        deprecated_logged = any("DEPRECATED" in record.message for record in caplog.records)
        assert deprecated_logged, f"Expected DEPRECATED in log, got: {[r.message for r in caplog.records]}"


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

    def test_successful_pipeline_returns_all_fields(self):
        from agent.governance.redeploy_handler import handle_redeploy

        with patch("agent.governance.redeploy_handler._find_pid_for_target", return_value=111), \
             patch("agent.governance.redeploy_handler._stop_process", return_value=True), \
             patch("agent.governance.redeploy_handler._spawn_target", return_value=222), \
             patch("agent.governance.redeploy_handler._health_check", return_value=True), \
             patch("agent.governance.redeploy_handler._db_write_chain_version", return_value=True):
            result, status = handle_redeploy("executor", {
                "task_id": "t1",
                "expected_head": "abc123",
                "drain_grace_seconds": 0,
            })
            assert status == 200
            assert result["ok"] is True
            assert result["target"] == "executor"
            assert result["old_pid"] == 111
            assert result["new_pid"] == 222
            assert result["new_chain_version"] == "abc123"
            assert "updated_at" in result
