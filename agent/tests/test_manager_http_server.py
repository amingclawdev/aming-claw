"""Tests for agent.manager_http_server's stdlib HTTP sidecar."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch


_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent import manager_http_server as manager_http_server  # noqa: E402


@contextmanager
def _running_manager():
    server = manager_http_server.create_server("127.0.0.1", 0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post_json(base_url: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_service_manager_target_returns_400():
    with _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/redeploy/service_manager",
            {"chain_version": "abc1234"},
        )

    assert status == 400
    assert body["ok"] is False
    assert body["error_code"] == "SELF_REDEPLOY_FORBIDDEN"
    assert "self" in body["detail"]


def test_unknown_target_returns_404():
    with _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/redeploy/foobar",
            {"chain_version": "abc1234"},
        )

    assert status == 404
    assert body["ok"] is False
    assert body["error_code"] == "UNKNOWN_TARGET"


def test_executor_target_returns_404():
    with _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/redeploy/executor",
            {"chain_version": "abc1234"},
        )

    assert status == 404
    assert body["ok"] is False
    assert body["error_code"] == "UNKNOWN_TARGET"


def test_successful_redeploy_writes_chain_version_once():
    mock_proc = MagicMock()
    mock_proc.pid = 99999

    with patch.object(manager_http_server, "_stop_governance_process", return_value=True), \
            patch.object(manager_http_server, "_spawn_governance_process", return_value=mock_proc), \
            patch.object(manager_http_server, "_wait_for_health", return_value=True), \
            patch.object(manager_http_server, "_write_chain_version", return_value=True) as mock_write, \
            _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/redeploy/governance",
            {"chain_version": "abc1234"},
        )

    assert status == 200
    assert body["ok"] is True
    assert body["pid"] == 99999
    assert body["chain_version"] == "abc1234"
    mock_write.assert_called_once_with("abc1234")


def test_failed_spawn_does_not_write_chain_version():
    with patch.object(manager_http_server, "_stop_governance_process", return_value=True), \
            patch.object(
                manager_http_server,
                "_spawn_governance_process",
                side_effect=RuntimeError("spawn failed"),
            ), \
            patch.object(manager_http_server, "_write_chain_version") as mock_write, \
            _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/redeploy/governance",
            {"chain_version": "abc1234"},
        )

    assert status == 500
    assert body["ok"] is False
    assert "spawn" in body["detail"].lower()
    mock_write.assert_not_called()


def test_failed_health_check_does_not_write_chain_version():
    mock_proc = MagicMock()
    mock_proc.pid = 88888

    with patch.object(manager_http_server, "_stop_governance_process", return_value=True), \
            patch.object(manager_http_server, "_spawn_governance_process", return_value=mock_proc), \
            patch.object(manager_http_server, "_wait_for_health", return_value=False), \
            patch.object(manager_http_server, "_write_chain_version") as mock_write, \
            _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/redeploy/governance",
            {"chain_version": "abc1234"},
        )

    assert status == 500
    assert body["ok"] is False
    mock_write.assert_not_called()


def test_missing_chain_version_returns_400():
    with _running_manager() as base:
        status, body = _post_json(base, "/api/manager/redeploy/governance", {})

    assert status == 400
    assert body["ok"] is False
    assert "chain_version" in body["detail"]
