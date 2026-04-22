"""Tests for agent/manager_http_server.py.

Covers:
  (a) HTTP 400 for target=service_manager (mutual-exclusion guard)
  (b) HTTP 404 for unknown target
  (c) Successful governance redeploy writes chain_version exactly once
  (d) Failed spawn does NOT write chain_version
  (e) Missing chain_version returns 400
"""

import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

# Ensure agent directory is on the path
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

# Ensure project root is on the path for agent.manager_http_server import
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from agent.manager_http_server import create_app, handle_redeploy


# ---------------------------------------------------------------------------
# pytest-aiohttp style tests (preferred)
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Create the aiohttp application for testing."""
    return create_app()


@pytest.fixture
def client(event_loop, aiohttp_client, app):
    """Create a test client for the app."""
    return event_loop.run_until_complete(aiohttp_client(app))


class TestMutualExclusionGuard:
    """AC3: target=service_manager returns HTTP 400."""

    @pytest.mark.asyncio
    async def test_service_manager_target_returns_400(self, aiohttp_client, app):
        """POST /api/manager/redeploy/service_manager must return 400."""
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/manager/redeploy/service_manager",
            json={"chain_version": "abc1234"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "service_manager" in body["detail"]
        assert body["error_code"] == "SELF_REDEPLOY_FORBIDDEN"


class TestUnknownTarget:
    """AC4: unknown target returns HTTP 404."""

    @pytest.mark.asyncio
    async def test_unknown_target_returns_404(self, aiohttp_client, app):
        """POST /api/manager/redeploy/foobar must return 404."""
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/manager/redeploy/foobar",
            json={"chain_version": "abc1234"},
        )
        assert resp.status == 404
        body = await resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "UNKNOWN_TARGET"

    @pytest.mark.asyncio
    async def test_executor_target_returns_404(self, aiohttp_client, app):
        """POST /api/manager/redeploy/executor must return 404 (not a valid target)."""
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/manager/redeploy/executor",
            json={"chain_version": "abc1234"},
        )
        assert resp.status == 404


class TestGovernanceRedeploySuccess:
    """AC7/R5: Successful governance redeploy writes chain_version exactly once."""

    @pytest.mark.asyncio
    async def test_successful_redeploy_writes_chain_version(self, aiohttp_client, app):
        """Full success path: stop → spawn → health → version-update → 200."""
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with patch("agent.manager_http_server._stop_governance_process", return_value=True), \
             patch("agent.manager_http_server._spawn_governance_process", return_value=mock_proc), \
             patch("agent.manager_http_server._wait_for_health", return_value=True), \
             patch("agent.manager_http_server._write_chain_version", return_value=True) as mock_write:

            client = await aiohttp_client(app)
            resp = await client.post(
                "/api/manager/redeploy/governance",
                json={"chain_version": "abc1234"},
            )

            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["pid"] == 99999
            assert body["chain_version"] == "abc1234"

            # chain_version written exactly once
            mock_write.assert_called_once_with("abc1234")


class TestGovernanceRedeployFailure:
    """R5: Failed spawn does NOT write chain_version."""

    @pytest.mark.asyncio
    async def test_failed_spawn_does_not_write_chain_version(self, aiohttp_client, app):
        """If spawn raises, version-update must NOT be called."""
        with patch("agent.manager_http_server._stop_governance_process", return_value=True), \
             patch("agent.manager_http_server._spawn_governance_process", side_effect=RuntimeError("spawn failed")), \
             patch("agent.manager_http_server._write_chain_version") as mock_write:

            client = await aiohttp_client(app)
            resp = await client.post(
                "/api/manager/redeploy/governance",
                json={"chain_version": "abc1234"},
            )

            assert resp.status == 500
            body = await resp.json()
            assert body["ok"] is False
            assert "spawn" in body["detail"].lower()

            # chain_version must NOT be written
            mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_health_check_does_not_write_chain_version(self, aiohttp_client, app):
        """If health check fails, version-update must NOT be called."""
        mock_proc = MagicMock()
        mock_proc.pid = 88888

        with patch("agent.manager_http_server._stop_governance_process", return_value=True), \
             patch("agent.manager_http_server._spawn_governance_process", return_value=mock_proc), \
             patch("agent.manager_http_server._wait_for_health", return_value=False), \
             patch("agent.manager_http_server._write_chain_version") as mock_write:

            client = await aiohttp_client(app)
            resp = await client.post(
                "/api/manager/redeploy/governance",
                json={"chain_version": "abc1234"},
            )

            assert resp.status == 500
            body = await resp.json()
            assert body["ok"] is False

            # chain_version must NOT be written
            mock_write.assert_not_called()


class TestMissingChainVersion:
    """Missing chain_version in request body returns 400."""

    @pytest.mark.asyncio
    async def test_missing_chain_version_returns_400(self, aiohttp_client, app):
        """POST /api/manager/redeploy/governance without chain_version → 400."""
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/manager/redeploy/governance",
            json={},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "chain_version" in body["detail"]
