"""Direct handler tests for reconcile batch memory API."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.governance import server
from agent.governance.db import _configure_connection, _ensure_schema


PROJECT_ID = "p-batch-api"


class _StubCtx:
    def __init__(self, *, body=None, query=None, path_params=None):
        self.body = body or {}
        self.query = query or {}
        pp = {"project_id": PROJECT_ID}
        if path_params:
            pp.update(path_params)
        self.path_params = pp
        self.request_id = "req-test"
        self.handler = None
        self.method = "POST"

    def get_project_id(self):
        return self.path_params["project_id"]

    def require_auth(self, _conn):
        return {"principal_id": "tester", "role": "coordinator"}


def _unwrap(result):
    if isinstance(result, tuple):
        return result
    return 200, result


@pytest.fixture()
def gov_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "gov.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _configure_connection(conn, busy_timeout=0)
    _ensure_schema(conn)

    class _FakeCtx:
        def __init__(self, _project_id):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, _exc_val, _exc_tb):
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False

    monkeypatch.setattr(server, "DBContext", _FakeCtx)
    yield conn
    conn.close()


def test_batch_memory_create_get_and_record_decision(gov_db):
    status, body = _unwrap(server.handle_reconcile_batch_memory_create(_StubCtx(body={
        "session_id": "session-1",
        "batch_id": "batch-1",
        "created_by": "pm",
    })))
    assert status == 201
    assert body["batch"]["batch_id"] == "batch-1"

    status2, body2 = _unwrap(server.handle_reconcile_batch_memory_get(_StubCtx(
        path_params={"batch_id": "batch-1"},
    )))
    assert status2 == 200
    assert body2["batch"]["memory"]["processed_clusters"] == {}

    status3, body3 = _unwrap(server.handle_reconcile_batch_memory_pm_decision(_StubCtx(
        path_params={"batch_id": "batch-1"},
        body={
            "cluster_fingerprint": "fp-api",
            "decision": "new_feature",
            "feature_name": "Batch Memory API",
            "owned_files": ["agent/governance/reconcile_batch_memory.py"],
            "actor": "pm",
        },
    )))
    assert status3 == 200
    memory = body3["batch"]["memory"]
    assert memory["processed_clusters"]["fp-api"]["decision"] == "new_feature"
    assert memory["file_ownership"]["agent/governance/reconcile_batch_memory.py"] == "Batch Memory API"


def test_batch_memory_api_returns_404_for_missing_batch(gov_db):
    status, body = _unwrap(server.handle_reconcile_batch_memory_get(_StubCtx(
        path_params={"batch_id": "missing"},
    )))
    assert status == 404
    assert body["error"] == "batch_memory_not_found"

    status2, body2 = _unwrap(server.handle_reconcile_batch_memory_pm_decision(_StubCtx(
        path_params={"batch_id": "missing"},
        body={"cluster_fingerprint": "fp", "decision": "defer"},
    )))
    assert status2 == 404
    assert body2["error"] == "batch_memory_not_found"


def test_batch_memory_api_validates_decision(gov_db):
    server.handle_reconcile_batch_memory_create(_StubCtx(body={"batch_id": "batch-1"}))
    status, body = _unwrap(server.handle_reconcile_batch_memory_pm_decision(_StubCtx(
        path_params={"batch_id": "batch-1"},
        body={"cluster_fingerprint": "fp", "decision": "unknown"},
    )))
    assert status == 400
    assert body["error"] == "invalid_pm_decision"
