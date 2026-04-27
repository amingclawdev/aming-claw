"""Tests that /api/manager/health response contains runtime_version key."""

import io
import json
from unittest import mock


def test_manager_health_contains_runtime_version():
    """GET /api/manager/health must include runtime_version in response."""
    from agent.manager_http_server import ManagerHTTPHandler

    # Build a minimal mock request environment
    handler = object.__new__(ManagerHTTPHandler)
    handler.path = "/api/manager/health"
    handler.headers = {}

    captured = io.BytesIO()
    handler.wfile = captured
    handler.requestline = "GET /api/manager/health HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.client_address = ("127.0.0.1", 0)

    responses_sent = []

    def fake_send_response(code):
        responses_sent.append(code)

    def fake_send_header(key, value):
        pass

    def fake_end_headers():
        pass

    handler.send_response = fake_send_response
    handler.send_header = fake_send_header
    handler.end_headers = fake_end_headers

    handler.do_GET()

    # Parse the JSON body written to wfile
    body = captured.getvalue()
    data = json.loads(body.decode("utf-8"))

    assert "runtime_version" in data
    assert data["ok"] is True
    assert data["service"] == "manager_http_server"
    assert isinstance(data["runtime_version"], str)


def test_manager_health_trailing_slash():
    """GET /api/manager/health/ (with trailing slash) should also work."""
    from agent.manager_http_server import ManagerHTTPHandler

    handler = object.__new__(ManagerHTTPHandler)
    handler.path = "/api/manager/health/"
    handler.headers = {}

    captured = io.BytesIO()
    handler.wfile = captured
    handler.requestline = "GET /api/manager/health/ HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.client_address = ("127.0.0.1", 0)

    responses_sent = []

    handler.send_response = lambda code: responses_sent.append(code)
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None

    handler.do_GET()

    body = captured.getvalue()
    data = json.loads(body.decode("utf-8"))
    assert "runtime_version" in data
    assert data["ok"] is True
