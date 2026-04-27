"""Tests for POST /api/manager/respawn-executor endpoint."""

import json
import io
from unittest import mock


def test_respawn_executor_writes_signal_file(tmp_path):
    """Handler must write manager_signal.json with action='respawn_executor'."""
    from agent.manager_http_server import ManagerHTTPHandler

    body = json.dumps({"chain_version": "abc123"}).encode()

    handler = mock.MagicMock(spec=ManagerHTTPHandler)
    handler.path = "/api/manager/respawn-executor"
    handler._read_json_body = mock.MagicMock(return_value={"chain_version": "abc123"})
    handler._send_json = mock.MagicMock()

    # Patch _project_root to use tmp_path
    state_dir = tmp_path / "shared-volume" / "codex-tasks" / "state"

    with mock.patch("agent.manager_http_server._project_root", return_value=tmp_path):
        ManagerHTTPHandler._handle_respawn_executor(handler)

    handler._send_json.assert_called_once()
    resp = handler._send_json.call_args[0][0]
    assert resp["ok"] is True

    signal_path = state_dir / "manager_signal.json"
    assert signal_path.exists(), "manager_signal.json must be created"
    data = json.loads(signal_path.read_text(encoding="utf-8"))
    assert data["action"] == "respawn_executor"
    assert data["chain_version"] == "abc123"


def test_respawn_executor_route_matches():
    """do_POST must route /api/manager/respawn-executor correctly."""
    from agent.manager_http_server import ManagerHTTPHandler

    handler = mock.MagicMock(spec=ManagerHTTPHandler)
    handler.path = "/api/manager/respawn-executor"
    handler._handle_respawn_executor = mock.MagicMock()
    handler._send_json = mock.MagicMock()

    # Call real do_POST
    ManagerHTTPHandler.do_POST(handler)

    handler._handle_respawn_executor.assert_called_once()
