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


def test_respawn_executor_writes_restart_signal(tmp_path):
    with patch.object(manager_http_server, "_project_root", return_value=tmp_path), \
            _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/respawn-executor",
            {"chain_version": "abc1234"},
        )

    signal_path = tmp_path / "shared-volume" / "codex-tasks" / "state" / "manager_signal.json"
    payload = json.loads(signal_path.read_text(encoding="utf-8"))

    assert status == 200
    assert body["ok"] is True
    assert payload["action"] == "restart"
    assert payload["requested_action"] == "respawn_executor"
    assert payload["chain_version"] == "abc1234"


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


def test_profile_login_prepare_dispatches_only_fixed_identity_fields():
    calls = []

    class Result:
        def to_public_dict(self):
            return {
                "ok": True,
                "profile_id": "profile-codex-a",
                "provider": "codex",
                "state": "login_in_progress",
            }

    class Controller:
        def prepare_login(self, *, profile_id, provider):
            calls.append(("prepare_login", profile_id, provider))
            return Result()

    with patch.object(
        manager_http_server,
        "_profile_auth_controller",
        return_value=Controller(),
    ), _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/agent-profile-login-prepare",
            {"profile_id": "profile-codex-a", "provider": "codex"},
        )

    assert status == 200
    assert body["ok"] is True
    assert body["state"] == "login_in_progress"
    assert calls == [("prepare_login", "profile-codex-a", "codex")]


def test_profile_auth_status_uses_fixed_operation_alias():
    calls = []

    class Controller:
        def auth_status(self, *, profile_id, provider):
            calls.append(("auth_status", profile_id, provider))
            return {
                "ok": True,
                "profile_id": profile_id,
                "provider": provider,
                "state": "login_required",
            }

    with patch.object(
        manager_http_server,
        "_profile_auth_controller",
        return_value=Controller(),
    ), _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/agent-profiles/auth/status",
            {"profile_id": "profile-claude-a", "provider": "claude"},
        )

    assert status == 200
    assert body["state"] == "login_required"
    assert calls == [("auth_status", "profile-claude-a", "claude")]


def test_profile_login_endpoint_rejects_arbitrary_command_fields():
    with patch.object(manager_http_server, "_profile_auth_controller") as factory, \
            _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/agent-profile-login-prepare",
            {
                "profile_id": "profile-codex-a",
                "provider": "codex",
                "command": ["sh", "-c", "echo unsafe"],
                "environment": {"PATH": "/tmp"},
            },
        )

    assert status == 400
    assert body["ok"] is False
    assert body["error_code"] == "UNSUPPORTED_PROFILE_OPERATION_FIELDS"
    assert body["unsupported_fields"] == ["command", "environment"]
    factory.assert_not_called()


def test_profile_login_endpoint_rejects_path_traversal_identity():
    from agent.cli_agent_service.auth import ProfileAuthController

    with patch.object(
        manager_http_server,
        "_profile_auth_controller",
        return_value=ProfileAuthController(Path("/tmp/unused-profile-root")),
    ), _running_manager() as base:
        status, body = _post_json(
            base,
            "/api/manager/agent-profile-auth-status",
            {"profile_id": "../outside", "provider": "codex"},
        )

    assert status == 400
    assert body["ok"] is False
    assert body["error_code"] == "INVALID_PROFILE_OPERATION"
