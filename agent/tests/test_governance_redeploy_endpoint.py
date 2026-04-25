"""Tests for redeploy_handler.py mutual-exclusion guards and stubs.

AC1: governance target returns 400 'cannot restart itself'.
AC2: service_manager target returns 400 'cannot redeploy supervisor'.
AC4: gateway/coordinator targets return stub with stub=true.
"""

from __future__ import annotations

import pytest


def test_governance_self_guard():
    """AC1: handle_redeploy('governance', ...) returns 400 with 'cannot restart itself'."""
    from agent.governance.redeploy_handler import handle_redeploy

    result, status = handle_redeploy("governance", {"expected_head": "abc123"})

    assert status == 400
    assert result["ok"] is False
    assert "cannot restart itself" in result["error"].lower() or \
           "cannot restart itself" in result["error"]


def test_service_manager_guard():
    """AC2: handle_redeploy('service_manager', ...) returns 400 with 'cannot redeploy supervisor'."""
    from agent.governance.redeploy_handler import handle_redeploy

    result, status = handle_redeploy("service_manager", {"expected_head": "abc123"})

    assert status == 400
    assert result["ok"] is False
    assert "cannot redeploy supervisor" in result["error"]


def test_gateway_stub():
    """AC4: handle_redeploy('gateway', ...) returns stub response."""
    from agent.governance.redeploy_handler import handle_redeploy

    result, status = handle_redeploy("gateway", {"expected_head": "abc123"})

    assert status == 200
    assert result.get("stub") is True
    assert result.get("ok") is True
    assert "todo" in result


def test_coordinator_stub():
    """AC4: handle_redeploy('coordinator', ...) returns stub response."""
    from agent.governance.redeploy_handler import handle_redeploy

    result, status = handle_redeploy("coordinator", {"expected_head": "abc123"})

    assert status == 200
    assert result.get("stub") is True
    assert result.get("ok") is True
    assert "todo" in result


def test_unknown_target_rejected():
    """Unknown target returns 400."""
    from agent.governance.redeploy_handler import handle_redeploy

    result, status = handle_redeploy("nonexistent", {"expected_head": "abc123"})

    assert status == 400
    assert result["ok"] is False
