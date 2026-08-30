"""Focused executor API plane-identity tests."""

import json
import threading
import urllib.request
from http.server import HTTPServer
from unittest.mock import MagicMock, patch

import pytest

from agent import executor_api


@pytest.mark.parametrize("project_id", ["", " aming-claw", "aming-claw ", "amingClaw", "aming_claw"])
def test_executor_api_rejects_noncanonical_identity_before_bind(project_id):
    with patch.object(executor_api, "HTTPServer") as server:
        with pytest.raises(ValueError):
            executor_api.start_api_server(project_id)
    server.assert_not_called()


def test_executor_api_stores_bound_identity_before_server_thread(monkeypatch):
    fake_server = MagicMock()
    fake_thread = MagicMock()
    monkeypatch.setattr(executor_api, "PORT", 40100)
    with patch.object(executor_api, "HTTPServer", return_value=fake_server) as server, \
            patch.object(executor_api.threading, "Thread", return_value=fake_thread):
        assert executor_api.start_api_server("proj") is fake_server
    assert fake_server.executor_identity["project_id"] == "proj"
    assert fake_server.executor_identity["governance_url"] == "http://127.0.0.1:40000"
    server.assert_called_once()
    fake_thread.start.assert_called_once()


@pytest.mark.parametrize("project_id", ["proj", "aming-claw"])
def test_real_handler_status_and_cancel_use_bound_root_despite_hostile_env(
    monkeypatch, tmp_path, project_id,
):
    from agent import manager_http_server

    if project_id == "aming-claw":
        storage = tmp_path / "dev-storage"
        root = storage / "runtime"
        root.mkdir(parents=True)
        monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(storage))
        url = "http://127.0.0.1:40008"
    else:
        root = tmp_path / "shared-volume"
        root.mkdir()
        monkeypatch.setattr(manager_http_server, "_project_root", lambda: tmp_path)
        url = "http://127.0.0.1:40000"
    monkeypatch.setenv("SHARED_VOLUME_PATH", "/hostile/root")
    monkeypatch.setenv("PROJECT_ID", "hostile-project")
    identity = manager_http_server.plane_bound_manager_identity(project_id, url, str(root))
    pending = root / "codex-tasks" / "pending"
    pending.mkdir(parents=True)
    (pending / "task-1.json").write_text("{}")
    server = HTTPServer(("127.0.0.1", 0), executor_api.ExecutorAPIHandler)
    server.executor_identity = identity
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{server.server_address[0]}:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/status") as response:
            status = json.loads(response.read())
        request = urllib.request.Request(f"{base}/task/task-1/cancel", data=b"{}", method="POST")
        with urllib.request.urlopen(request) as response:
            cancelled = json.loads(response.read())
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status["pending_tasks"] == 1
    assert cancelled["cancelled"] is True
    assert not (pending / "task-1.json").exists()
