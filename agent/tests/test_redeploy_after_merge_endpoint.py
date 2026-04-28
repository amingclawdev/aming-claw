"""Tests for POST /api/governance/redeploy-after-merge/{project_id}."""
from pathlib import Path


def _handler_src():
    src = (Path(__file__).resolve().parent.parent / "governance" / "server.py").read_text()
    s = src.index("def handle_redeploy_after_merge")
    e = src.index("\n@route(", s + 1)
    return src[s:e]


def test_endpoint_writes_two_audit_rows():
    h = _handler_src()
    assert h.count("audit_service.record(") == 2
    assert h.count("with DBContext(") == 2
    assert h.index("redeploy_after_merge.requested") < h.index("respawn-executor")


def test_endpoint_calls_respawn_executor_first():
    h = _handler_src()
    assert "/api/manager/respawn-executor" in h
    assert h.index("respawn-executor") < h.index("redeploy/governance")


def test_endpoint_calls_redeploy_governance_second():
    h = _handler_src()
    assert "/api/manager/redeploy/governance" in h


def test_endpoint_returns_ok_when_both_sm_succeed():
    h = _handler_src()
    assert "sm_respawn_ok and sm_redeploy_ok" in h


def test_endpoint_returns_partial_ok_when_one_fails():
    h = _handler_src()
    assert '"sm_respawn_ok": sm_respawn_ok' in h
    assert '"sm_redeploy_ok": sm_redeploy_ok' in h


def test_endpoint_does_NOT_create_thread():
    h = _handler_src()
    assert "threading" not in h
    assert "Thread" not in h


def test_endpoint_does_NOT_call_restart_local_governance():
    h = _handler_src()
    assert "restart_local_governance" not in h
