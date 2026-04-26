"""Tests for version-update endpoint manual-fix-* updated_by extension (B32).

AC5-AC9: Four tests covering manual-fix with reason, without reason, bare prefix,
and backward compat for existing auto-chain updated_by.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


def _make_ctx(body, project_id="test-proj"):
    """Build a minimal RequestContext-like object for handle_version_update."""
    ctx = MagicMock()
    ctx.body = body
    ctx.path_params = {"project_id": project_id}
    ctx.get_project_id.return_value = project_id
    ctx.handler = None  # no HTTP handler in tests
    return ctx


def _base_body(**overrides):
    """Return a valid version-update body with optional overrides."""
    base = {
        "chain_version": "abc1234",
        "updated_by": "auto-chain",
        "task_id": "",
        "chain_stage": "merge",
        "internal_token": "test-token-123",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _env_token(monkeypatch):
    """Set VERSION_UPDATE_TOKEN so non-auto-chain callers can authenticate."""
    monkeypatch.setenv("VERSION_UPDATE_TOKEN", "test-token-123")


@pytest.fixture(autouse=True)
def _mock_deps():
    """Patch DB and audit so handler never touches real SQLite."""
    with patch("agent.governance.server.independent_connection") as mock_ic, \
         patch("agent.governance.server.audit_service") as _audit, \
         patch("agent.governance.server._retry_on_busy") as mock_retry:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_ic.return_value = conn
        # _retry_on_busy: just call the function
        mock_retry.side_effect = lambda fn: fn()
        yield


def test_manual_fix_with_reason_succeeds():
    """AC5: manual-fix-demo-slug with manual_fix_reason='test' returns ok=True."""
    from agent.governance.server import handle_version_update

    body = _base_body(
        updated_by="manual-fix-demo-slug",
        manual_fix_reason="test",
        chain_stage="",
    )
    ctx = _make_ctx(body)
    result = handle_version_update(ctx)

    # Successful return is a plain dict (no tuple status code)
    if isinstance(result, tuple):
        pytest.fail(f"Expected success but got error: {result}")
    assert result["ok"] is True


def test_manual_fix_without_reason_returns_400():
    """AC6: manual-fix-demo-slug without manual_fix_reason returns 400 MANUAL_FIX_REASON_MISSING."""
    from agent.governance.server import handle_version_update

    body = _base_body(
        updated_by="manual-fix-demo-slug",
        chain_stage="",
    )
    ctx = _make_ctx(body)
    result = handle_version_update(ctx)

    assert isinstance(result, tuple)
    payload, status = result
    assert status == 400
    assert payload["error"] == "MANUAL_FIX_REASON_MISSING"


def test_bare_manual_fix_rejected():
    """AC7: updated_by='manual-fix-' (no slug) returns 403 INVALID_UPDATED_BY."""
    from agent.governance.server import handle_version_update

    body = _base_body(
        updated_by="manual-fix-",
        chain_stage="",
    )
    ctx = _make_ctx(body)
    result = handle_version_update(ctx)

    assert isinstance(result, tuple)
    payload, status = result
    assert status == 403
    assert payload["error"] == "INVALID_UPDATED_BY"


def test_existing_auto_chain_unchanged():
    """AC8: updated_by='auto-chain' with chain_stage='merge' still works (backward compat)."""
    from agent.governance.server import handle_version_update

    body = _base_body(updated_by="auto-chain", chain_stage="merge")
    ctx = _make_ctx(body)
    result = handle_version_update(ctx)

    if isinstance(result, tuple):
        pytest.fail(f"Expected success but got error: {result}")
    assert result["ok"] is True
