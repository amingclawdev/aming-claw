"""Tests for version-update endpoint auth: manual-fix-* (B32) + task.type gate + cron-writeback (B40).

B32 AC5-AC9: manual-fix with reason, without reason, bare prefix, backward compat.
B40 AC1/AC4/AC5a-d: task.type='merge' enforcement, cron-writeback allowlist + token.
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


# ---------------------------------------------------------------------------
# B40 tests: task.type gate + cron-writeback
# ---------------------------------------------------------------------------


def test_auto_chain_with_non_merge_task_returns_400():
    """AC5a: auto-chain with a task whose type='dev' is rejected TASK_TYPE_NOT_MERGE."""
    from agent.governance.server import handle_version_update

    task_id = "task-fake-dev-123"
    body = _base_body(
        updated_by="auto-chain",
        task_id=task_id,
        chain_stage="merge",
    )
    ctx = _make_ctx(body)

    # Mock the DB to return a task with type='dev', status='succeeded'
    mock_row = {"status": "succeeded", "type": "dev"}
    with patch("agent.governance.server.independent_connection") as mock_ic, \
         patch("agent.governance.server.audit_service"), \
         patch("agent.governance.server._retry_on_busy", side_effect=lambda fn: fn()):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = mock_row
        mock_ic.return_value = conn
        result = handle_version_update(ctx)

    assert isinstance(result, tuple), f"Expected error tuple, got {result}"
    payload, status = result
    assert status == 400
    assert payload["error"] == "TASK_TYPE_NOT_MERGE"


def test_auto_chain_with_merge_task_succeeds():
    """AC5b: auto-chain with a task whose type='merge' succeeds (200 ok)."""
    from agent.governance.server import handle_version_update

    task_id = "task-fake-merge-456"
    body = _base_body(
        updated_by="auto-chain",
        task_id=task_id,
        chain_stage="merge",
    )
    ctx = _make_ctx(body)

    # Mock the DB to return a task with type='merge', status='succeeded'
    mock_task_row = {"status": "succeeded", "type": "merge"}
    with patch("agent.governance.server.independent_connection") as mock_ic, \
         patch("agent.governance.server.audit_service"), \
         patch("agent.governance.server._retry_on_busy", side_effect=lambda fn: fn()):
        conn = MagicMock()
        # First call: step 3b task lookup → merge task
        # Second call: step 4 old_version check → None (no existing version)
        # Third+ calls: step 5 update
        conn.execute.return_value.fetchone.return_value = mock_task_row
        mock_ic.return_value = conn
        result = handle_version_update(ctx)

    if isinstance(result, tuple):
        pytest.fail(f"Expected success but got error: {result}")
    assert result["ok"] is True


def test_cron_writeback_no_task_id_succeeds():
    """AC5c: cron-writeback with chain_version, no task_id, returns 200 ok."""
    from agent.governance.server import handle_version_update

    body = _base_body(
        updated_by="cron-writeback",
        chain_version="abc",
        task_id="",
        chain_stage="",
    )
    ctx = _make_ctx(body)

    # cron-writeback skips task lookup entirely, goes straight to version update
    with patch("agent.governance.server.independent_connection") as mock_ic, \
         patch("agent.governance.server.audit_service"), \
         patch("agent.governance.server._retry_on_busy", side_effect=lambda fn: fn()):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_ic.return_value = conn
        result = handle_version_update(ctx)

    if isinstance(result, tuple):
        pytest.fail(f"Expected success but got error: {result}")
    assert result["ok"] is True


def test_cron_writeback_with_token_required():
    """AC5d: cron-writeback without valid token is rejected (token enforcement)."""
    from agent.governance.server import handle_version_update

    body = _base_body(
        updated_by="cron-writeback",
        chain_version="abc",
        task_id="",
        chain_stage="",
        internal_token="wrong-token",
    )
    ctx = _make_ctx(body)

    # Token is set in env but body has wrong token → should be rejected at step 1
    with patch("agent.governance.server.independent_connection") as mock_ic, \
         patch("agent.governance.server.audit_service"), \
         patch("agent.governance.server._retry_on_busy", side_effect=lambda fn: fn()):
        conn = MagicMock()
        mock_ic.return_value = conn
        result = handle_version_update(ctx)

    assert isinstance(result, tuple), f"Expected error tuple, got {result}"
    payload, status = result
    assert status == 403
    assert payload["error"] == "INVALID_TOKEN"
