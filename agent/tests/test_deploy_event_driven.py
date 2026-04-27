"""Tests for event-driven deploy (pending_executor_reloads table, no inline restart_executor)."""

import sqlite3
from unittest import mock


def test_ensure_pending_reload_table_creates_table():
    """_ensure_pending_reload_table must create the table in an empty DB."""
    from agent.deploy_chain import _ensure_pending_reload_table
    conn = sqlite3.connect(":memory:")
    _ensure_pending_reload_table(conn)
    # Table should exist
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_executor_reloads'"
    ).fetchone()
    assert row is not None
    assert row[0] == "pending_executor_reloads"


def test_ensure_pending_reload_table_idempotent():
    """Calling _ensure_pending_reload_table twice should not error."""
    from agent.deploy_chain import _ensure_pending_reload_table
    conn = sqlite3.connect(":memory:")
    _ensure_pending_reload_table(conn)
    _ensure_pending_reload_table(conn)  # Should not raise


def test_pending_reload_status_enum():
    """Only 'pending' and 'processed' status values are allowed."""
    from agent.deploy_chain import _ensure_pending_reload_table
    conn = sqlite3.connect(":memory:")
    _ensure_pending_reload_table(conn)

    # Valid statuses
    conn.execute(
        "INSERT INTO pending_executor_reloads (chain_version, requested_at, status) "
        "VALUES ('abc', '2026-01-01', 'pending')"
    )
    conn.execute(
        "INSERT INTO pending_executor_reloads (chain_version, requested_at, status) "
        "VALUES ('def', '2026-01-01', 'processed')"
    )
    conn.commit()

    # Invalid status should fail
    import pytest
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO pending_executor_reloads (chain_version, requested_at, status) "
            "VALUES ('ghi', '2026-01-01', 'invalid')"
        )


def test_run_deploy_governance_creates_reload_row():
    """run_deploy with governance affected must create a processed reload row
    and call restart_local_governance, NOT restart_executor."""
    restart_gov_calls = []
    restart_exec_calls = []

    def fake_restart_local_governance(port=40000):
        restart_gov_calls.append(port)
        return True, "mock restart OK"

    def fake_restart_executor():
        restart_exec_calls.append(True)
        return True

    # Mock governance.db path to use a temp file
    import tempfile, os
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()

    try:
        real_sqlite3_connect = sqlite3.connect
        gov_conn = sqlite3.connect(tmp_db.name)

        def mock_sqlite3_connect(path, **kwargs):
            if "governance" in str(path):
                return gov_conn
            return real_sqlite3_connect(path, **kwargs)

        import agent.deploy_chain as dc

        with mock.patch("agent.deploy_chain.restart_local_governance", side_effect=fake_restart_local_governance), \
             mock.patch("agent.deploy_chain.restart_executor", side_effect=fake_restart_executor), \
             mock.patch("agent.deploy_chain.smoke_test", return_value={"all_pass": True, "governance": True}), \
             mock.patch("agent.deploy_chain._save_report"), \
             mock.patch("agent.deploy_chain._post_redeploy", return_value={"ok": True}), \
             mock.patch("agent.deploy_chain._post_manager_redeploy_governance", return_value={"ok": True}), \
             mock.patch("sqlite3.connect", side_effect=mock_sqlite3_connect):

            result = dc.run_deploy(
                changed_files=["agent/governance/server.py"],
                project_id="test-proj",
                expected_head="abc1234",
            )

        # Verify restart_local_governance was called
        assert len(restart_gov_calls) >= 1, "restart_local_governance must be called"

        # Verify restart_executor was NOT called inline
        assert len(restart_exec_calls) == 0, "restart_executor must NOT be called"

        # Verify the reload row exists and is processed
        check_conn = sqlite3.connect(tmp_db.name)
        rows = check_conn.execute(
            "SELECT chain_version, status, processed_at FROM pending_executor_reloads"
        ).fetchall()
        check_conn.close()

        assert len(rows) >= 1, "Must have at least one pending_executor_reloads row"
        row = rows[-1]
        assert row[0] == "abc1234", f"chain_version should be abc1234, got {row[0]}"
        assert row[1] == "processed", f"status should be 'processed', got {row[1]}"
        assert row[2] is not None, "processed_at must be set"

    finally:
        try:
            gov_conn.close()
        except Exception:
            pass
        os.unlink(tmp_db.name)
