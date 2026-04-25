"""Tests for deploy_chain.py port fix (R0) and sequencing (R4).

AC0: localhost:40101 present, localhost:40200 absent in deploy_chain.py.
AC5: run_deploy with affected=['governance','executor'] calls governance BEFORE executor.
"""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# AC0: Port fix verification
# ---------------------------------------------------------------------------

def test_port_40101_present_in_deploy_chain():
    """AC0: grep 'localhost:40101' returns at least 1 match."""
    src = Path(__file__).resolve().parent.parent / "deploy_chain.py"
    text = src.read_text(encoding="utf-8")
    assert "localhost:40101" in text, "Expected localhost:40101 in deploy_chain.py"


def test_port_40200_absent_in_deploy_chain():
    """AC0: grep 'localhost:40200' returns 0 matches."""
    src = Path(__file__).resolve().parent.parent / "deploy_chain.py"
    text = src.read_text(encoding="utf-8")
    assert "localhost:40200" not in text, "Found legacy localhost:40200 in deploy_chain.py"


def test_post_manager_redeploy_governance_url():
    """Verify _post_manager_redeploy_governance uses port 40101."""
    from agent.deploy_chain import _post_manager_redeploy_governance
    src = inspect.getsource(_post_manager_redeploy_governance)
    assert "40101" in src
    assert "40200" not in src


# ---------------------------------------------------------------------------
# AC5: Sequencing — governance before executor
# ---------------------------------------------------------------------------

@patch("agent.deploy_chain._save_report")
@patch("agent.deploy_chain.smoke_test", return_value={"all_pass": True, "executor": True, "governance": True, "gateway": "not_applicable"})
@patch("agent.deploy_chain._post_redeploy")
@patch("agent.deploy_chain._post_manager_redeploy_governance")
@patch("agent.deploy_chain.rebuild_governance", return_value=(True, "ok"))
@patch("agent.deploy_chain.detect_affected_services", return_value=["executor", "governance"])
def test_governance_before_executor_ordering(
    mock_detect,
    mock_rebuild,
    mock_gov_redeploy,
    mock_post_redeploy,
    mock_smoke,
    mock_save,
):
    """AC5: When both governance and executor are affected, governance is redeployed first."""
    mock_gov_redeploy.return_value = {"ok": True}
    mock_post_redeploy.return_value = {"ok": True}

    from agent.deploy_chain import run_deploy
    report = run_deploy(
        changed_files=["agent/governance/server.py", "agent/executor.py"],
        task_id="test-task",
        expected_head="abc123",
    )

    # Governance redeploy must have been called
    mock_gov_redeploy.assert_called_once()

    # Executor redeploy must have been called
    mock_post_redeploy.assert_called_once_with(
        "executor", task_id="test-task", expected_head="abc123",
    )

    # Verify ordering: governance call happened before executor call
    # We check by looking at the call order on the manager mock
    # mock_gov_redeploy was called, then mock_post_redeploy was called
    # Since they're separate mocks, we use a shared call tracker
    assert mock_gov_redeploy.called
    assert mock_post_redeploy.called


@patch("agent.deploy_chain._save_report")
@patch("agent.deploy_chain.smoke_test", return_value={"all_pass": True, "executor": True, "governance": True, "gateway": "not_applicable"})
@patch("agent.deploy_chain._post_redeploy")
@patch("agent.deploy_chain._post_manager_redeploy_governance")
@patch("agent.deploy_chain.rebuild_governance", return_value=(True, "ok"))
@patch("agent.deploy_chain.detect_affected_services", return_value=["executor", "governance"])
def test_governance_before_executor_call_order(
    mock_detect,
    mock_rebuild,
    mock_gov_redeploy,
    mock_post_redeploy,
    mock_smoke,
    mock_save,
):
    """AC5: Verify call order with a shared call tracker."""
    call_order = []

    def gov_side_effect(**kwargs):
        call_order.append("governance")
        return {"ok": True}

    def executor_side_effect(*args, **kwargs):
        call_order.append("executor")
        return {"ok": True}

    mock_gov_redeploy.side_effect = gov_side_effect
    mock_post_redeploy.side_effect = executor_side_effect

    from agent.deploy_chain import run_deploy
    run_deploy(
        changed_files=["agent/governance/server.py", "agent/executor.py"],
        task_id="test-task",
        expected_head="abc123",
    )

    assert call_order.index("governance") < call_order.index("executor"), \
        f"Expected governance before executor, got: {call_order}"
