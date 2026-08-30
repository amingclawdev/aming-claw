"""Focused executor API plane-identity tests."""

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
