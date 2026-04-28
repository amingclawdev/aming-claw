"""Tests: deploy_chain executor orchestration (v4 design)."""
import inspect
import json
from unittest import mock


def test_three_sequential_posts():
    """Governance branch makes 3 POSTs: gov-ack, sm-redeploy, sm-respawn."""
    from agent.deploy_chain import run_deploy
    src = inspect.getsource(run_deploy)
    assert "redeploy-after-merge" in src
    assert "/api/manager/redeploy/governance" in src
    assert "/api/manager/respawn-executor" in src
    # Order: gov-ack before sm-redeploy before sm-respawn
    i1 = src.index("redeploy-after-merge")
    i2 = src.index("/api/manager/redeploy/governance")
    i3 = src.index("/api/manager/respawn-executor")
    assert i1 < i2 < i3


def test_no_sqlite3_in_governance_branch():
    from agent.deploy_chain import run_deploy
    src = inspect.getsource(run_deploy)
    assert "sqlite3.connect" not in src


def test_graceful_sm_failure():
    """SM failure on one POST doesn't crash; steps['governance'] reports failure."""
    import agent.deploy_chain as dc
    call_log: list[str] = []

    def fake_urlopen(req, timeout=30):
        url = req.full_url
        call_log.append(url)
        if "respawn-executor" in url:
            raise ConnectionRefusedError("SM down")
        resp = mock.MagicMock()
        resp.__enter__ = mock.MagicMock(return_value=mock.MagicMock(
            read=mock.MagicMock(return_value=json.dumps({"ok": True}).encode())))
        resp.__exit__ = mock.MagicMock(return_value=False)
        return resp

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         mock.patch("agent.deploy_chain.smoke_test", return_value={"all_pass": True}), \
         mock.patch("agent.deploy_chain._save_report"), \
         mock.patch("agent.deploy_chain._post_redeploy", return_value={"ok": True}):
        result = dc.run_deploy(
            changed_files=["agent/governance/server.py"],
            project_id="test-proj", expected_head="abc1234")
    assert len(call_log) == 3
    gov_step = result.get("steps", {}).get("governance", {})
    assert gov_step.get("success") is False
    assert "FAIL" in gov_step.get("summary", "")
