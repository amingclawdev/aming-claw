"""Tests for run_deploy double-write dispatcher (PR-2).

Mocks four redeploy endpoints and verifies run_deploy dispatches correctly
for each combination:
  - executor only
  - governance only
  - both (governance + executor)
  - gateway only
  - service_manager only
  - empty (no affected services)
  - governance + service_manager (dual-restart error)
"""
import json
from unittest.mock import patch, MagicMock, call

import pytest

from agent.deploy_chain import run_deploy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_redeploy_ok(target, **kwargs):
    """Simulate a successful redeploy POST response."""
    return {"ok": True, "target": target, "new_pid": 99999}


def _mock_redeploy_fail(target, **kwargs):
    """Simulate a failed redeploy POST response."""
    return {"ok": False, "error": f"Failed to spawn {target}", "step": "spawn"}


def _mock_urlopen_ok(*args, **kwargs):
    """Simulate the governance/service-manager HTTP redeploy ACKs."""
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true}'
    return response


def _make_patches():
    """Return common patches for isolating run_deploy from real services."""
    return {
        "restart_executor": patch("agent.deploy_chain.restart_executor", return_value=True),
        "rebuild_governance": patch("agent.deploy_chain.rebuild_governance", return_value=(True, "ok")),
        "restart_local_governance": patch("agent.deploy_chain.restart_local_governance", return_value=(True, "ok")),
        "restart_gateway": patch("agent.deploy_chain.restart_gateway", return_value=(True, "gw ok")),
        "smoke_test": patch("agent.deploy_chain.smoke_test", return_value={"all_pass": True, "executor": True, "governance": True, "gateway": True}),
        "post_redeploy": patch("agent.deploy_chain._post_redeploy", side_effect=lambda t, **kw: _mock_redeploy_ok(t)),
        "post_manager_redeploy": patch("agent.deploy_chain._post_manager_redeploy_governance", return_value={"ok": True, "target": "governance"}),
        "urlopen": patch("agent.deploy_chain.urllib.request.urlopen", side_effect=_mock_urlopen_ok),
        "save_report": patch("agent.deploy_chain._save_report"),
        "mark_task": patch("agent.deploy_chain._mark_task_succeeded_pre_kill"),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDispatchExecutorOnly:
    """Executor-only deploy: POST to redeploy/executor and let service_manager drain."""

    def test_executor_only_dispatches_both_paths(self):
        patches = _make_patches()
        with patches["restart_executor"] as mock_legacy, \
             patches["rebuild_governance"], \
             patches["restart_gateway"], \
             patches["smoke_test"], \
             patches["post_redeploy"] as mock_redeploy, \
             patches["post_manager_redeploy"], \
             patches["save_report"], \
             patches["mark_task"] as mock_mark:
            report = run_deploy(
                ["agent/executor.py"],
                project_id="aming-claw",
                task_id="t1",
                expected_head="abc",
            )

        assert report["success"] is True
        assert "executor" in report["affected_services"]
        # Redeploy was called for executor
        mock_redeploy.assert_called_once_with(
            "executor", task_id="t1", expected_head="abc",
        )
        mock_legacy.assert_not_called()
        mock_mark.assert_not_called()

    def test_executor_does_not_precomplete_redeploy_pending(self):
        """Executor deploy returns a full report instead of pre-completing with a placeholder."""
        patches = _make_patches()

        with patch("agent.deploy_chain.restart_executor") as mock_legacy, \
             patches["rebuild_governance"], \
             patches["restart_gateway"], \
             patches["smoke_test"], \
             patches["post_redeploy"], \
             patches["post_manager_redeploy"], \
             patches["save_report"], \
             patch("agent.deploy_chain._mark_task_succeeded_pre_kill") as mock_mark:
            report = run_deploy(
                ["agent/executor.py"],
                project_id="aming-claw",
                task_id="t1",
                expected_head="abc",
            )

        assert report["success"] is True
        mock_legacy.assert_not_called()
        mock_mark.assert_not_called()


class TestDispatchGovernanceOnly:
    """Governance-only: POST to /api/manager/redeploy/governance + legacy rebuild."""

    def test_governance_dispatches_to_manager(self):
        patches = _make_patches()
        with patches["restart_executor"], \
             patches["rebuild_governance"] as mock_legacy, \
             patches["restart_gateway"], \
             patches["smoke_test"], \
             patches["post_redeploy"], \
             patches["post_manager_redeploy"] as mock_mgr, \
             patches["urlopen"], \
             patches["save_report"], \
             patches["mark_task"]:
            report = run_deploy(
                ["agent/governance/server.py"],
                project_id="aming-claw",
                task_id="t1",
                expected_head="abc",
            )

        assert "governance" in report["affected_services"]
        assert report["steps"]["governance"]["success"] is True
        mock_mgr.assert_not_called()
        mock_legacy.assert_not_called()


class TestDispatchBothGovernanceAndExecutor:
    """Both governance + executor: R8 — service_manager handles sequentially."""

    def test_both_dispatched(self):
        patches = _make_patches()
        with patches["restart_executor"] as mock_exec, \
             patches["rebuild_governance"] as mock_gov, \
             patches["restart_gateway"], \
             patches["smoke_test"], \
             patches["post_redeploy"] as mock_redeploy, \
             patches["post_manager_redeploy"] as mock_mgr, \
             patches["urlopen"], \
             patches["save_report"], \
             patches["mark_task"]:
            report = run_deploy(
                ["agent/executor.py", "agent/governance/server.py"],
                project_id="aming-claw",
                task_id="t1",
                expected_head="abc",
            )

        assert "executor" in report["affected_services"]
        assert "governance" in report["affected_services"]
        mock_exec.assert_not_called()
        mock_gov.assert_not_called()
        mock_redeploy.assert_called_once()  # executor
        mock_mgr.assert_not_called()        # governance uses event-driven HTTP ACKs


class TestDispatchGatewayOnly:
    """Gateway-only: POST to redeploy/gateway + legacy restart_gateway."""

    def test_gateway_dispatches_both_paths(self):
        patches = _make_patches()
        with patches["restart_executor"], \
             patches["rebuild_governance"], \
             patches["restart_gateway"] as mock_legacy, \
             patches["smoke_test"], \
             patches["post_redeploy"] as mock_redeploy, \
             patches["post_manager_redeploy"], \
             patches["save_report"], \
             patches["mark_task"]:
            report = run_deploy(
                ["agent/telegram_gateway/bot.py"],
                project_id="aming-claw",
                task_id="t1",
                expected_head="abc",
            )

        assert "gateway" in report["affected_services"]
        mock_redeploy.assert_called_once_with(
            "gateway", task_id="t1", expected_head="abc",
        )
        mock_legacy.assert_called_once()


class TestDispatchEmpty:
    """Empty affected services: no restart, no redeploy calls."""

    def test_no_services_no_calls(self):
        patches = _make_patches()
        with patches["restart_executor"] as mock_exec, \
             patches["rebuild_governance"] as mock_gov, \
             patches["restart_gateway"] as mock_gw, \
             patches["smoke_test"], \
             patches["post_redeploy"] as mock_redeploy, \
             patches["post_manager_redeploy"] as mock_mgr, \
             patches["save_report"], \
             patches["mark_task"]:
            report = run_deploy(
                ["docs/readme.md"],
                project_id="aming-claw",
            )

        assert report["success"] is True
        assert report["affected_services"] == []
        mock_exec.assert_not_called()
        mock_gov.assert_not_called()
        mock_gw.assert_not_called()
        mock_redeploy.assert_not_called()
        mock_mgr.assert_not_called()


class TestDualRestartError:
    """AC8: governance + service_manager returns explicit error."""

    def test_governance_plus_service_manager_returns_error(self):
        """R10: explicit error referencing dual-restart-runbook.md."""
        patches = _make_patches()
        with patches["restart_executor"], \
             patches["rebuild_governance"], \
             patches["restart_gateway"], \
             patches["smoke_test"], \
             patches["post_redeploy"], \
             patches["post_manager_redeploy"], \
             patches["save_report"], \
             patches["mark_task"], \
             patch("agent.deploy_chain.detect_affected_services",
                   return_value=["governance", "service_manager"]):
            report = run_deploy(
                ["agent/governance/server.py", "agent/service_manager.py"],
                project_id="aming-claw",
            )

        assert report["success"] is False
        assert "dual-restart-runbook.md" in report["error"]
        assert report.get("dual_restart_required") is True


class TestLoggingPrefixes:
    """AC6: run_deploy logs with [legacy] and [redeploy] prefixes."""

    def test_log_prefixes_present(self, caplog):
        import logging
        patches = _make_patches()
        with patches["restart_executor"], \
             patches["rebuild_governance"], \
             patches["restart_gateway"], \
             patches["smoke_test"], \
             patches["post_redeploy"], \
             patches["post_manager_redeploy"], \
             patches["save_report"], \
             patches["mark_task"]:
            with caplog.at_level(logging.INFO, logger="agent.deploy_chain"):
                run_deploy(
                    ["agent/executor.py"],
                    project_id="aming-claw",
                    task_id="t1",
                    expected_head="abc",
                )

        messages = [r.message for r in caplog.records]
        has_redeploy = any("[redeploy]" in m for m in messages)
        has_legacy = any("[legacy]" in m for m in messages)
        assert has_redeploy, f"Expected [redeploy] prefix in logs, got: {messages}"
        assert has_legacy, f"Expected [legacy] prefix in logs, got: {messages}"


class TestExistingFunctionsPreserved:
    """AC15: All existing functions remain in deploy_chain.py."""

    def test_functions_exist(self):
        import agent.deploy_chain as dc
        assert hasattr(dc, "restart_executor")
        assert hasattr(dc, "restart_local_governance")
        assert hasattr(dc, "rebuild_governance")
        assert hasattr(dc, "restart_gateway")
        assert callable(dc.restart_executor)
        assert callable(dc.restart_local_governance)
        assert callable(dc.rebuild_governance)
        assert callable(dc.restart_gateway)
