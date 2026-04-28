"""Tests: handle_redeploy_after_merge is audit-only (v4 design)."""
from pathlib import Path


def _handler_src():
    src = (Path(__file__).resolve().parent.parent / "governance" / "server.py").read_text()
    s = src.index("def handle_redeploy_after_merge")
    e = src.index("\n@route(", s + 1)
    return src[s:e]


def test_ack_response_shape():
    h = _handler_src()
    assert '"audit recorded; executor must orchestrate sm calls"' in h


def test_single_audit_row():
    h = _handler_src()
    assert h.count("audit_service.record(") == 1
    assert "redeploy_after_merge.requested" in h
    assert "sm_notified" not in h


def test_no_sm_calls():
    h = _handler_src()
    assert "urllib.request" not in h
    assert "40101" not in h


def test_no_self_kill_mechanisms():
    h = _handler_src()
    assert "threading.Thread" not in h
    assert "restart_local_governance" not in h
    assert "os.kill" not in h
