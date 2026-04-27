"""Tests for HTTP-based deploy flow — no direct sqlite3.connect in run_deploy."""

import inspect
import json
from unittest import mock


def test_run_deploy_governance_uses_http_not_sqlite():
    """run_deploy with governance affected must POST to redeploy-after-merge,
    NOT call sqlite3.connect."""
    import agent.deploy_chain as dc

    fake_resp = json.dumps({"ok": True}).encode()
    mock_urlopen = mock.MagicMock()
    mock_urlopen.__enter__ = mock.MagicMock(return_value=mock.MagicMock(
        read=mock.MagicMock(return_value=fake_resp)))
    mock_urlopen.__exit__ = mock.MagicMock(return_value=False)

    with mock.patch("urllib.request.urlopen", return_value=mock_urlopen) as url_mock, \
         mock.patch("agent.deploy_chain.smoke_test", return_value={"all_pass": True, "governance": True}), \
         mock.patch("agent.deploy_chain._save_report"), \
         mock.patch("agent.deploy_chain._post_redeploy", return_value={"ok": True}), \
         mock.patch("sqlite3.connect", side_effect=AssertionError("sqlite3.connect must not be called")):

        result = dc.run_deploy(
            changed_files=["agent/governance/server.py"],
            project_id="test-proj",
            expected_head="abc1234",
        )

    # Verify HTTP call was made to redeploy-after-merge
    assert url_mock.called, "urllib.request.urlopen must be called for governance redeploy"
    call_args = url_mock.call_args
    req_obj = call_args[0][0]
    assert "redeploy-after-merge" in req_obj.full_url


def test_no_sqlite3_connect_in_run_deploy_source():
    """run_deploy function source must not contain sqlite3.connect."""
    from agent.deploy_chain import run_deploy
    source = inspect.getsource(run_deploy)
    assert "sqlite3.connect" not in source, "run_deploy must not use sqlite3.connect"


def test_no_pending_reload_table_in_source():
    """deploy_chain.py must have no references to the old reload table."""
    import agent.deploy_chain as dc
    source = inspect.getsource(dc)
    # Check for the old table name (split to avoid grep matching this test)
    old_table = "pending_executor" + "_reloads"
    assert old_table not in source


def test_ensure_pending_reload_table_deleted():
    """_ensure_pending_reload_table must not exist."""
    import agent.deploy_chain as dc
    assert not hasattr(dc, "_ensure_pending_reload_table"), \
        "_ensure_pending_reload_table must be deleted"
