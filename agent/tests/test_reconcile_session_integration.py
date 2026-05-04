"""Integration tests for CR0b — reconcile session HTTP endpoints, scoped-task
blocker, and auto_chain bypass middleware.

Uses a temporary SQLite governance DB and exercises the handlers directly via
a stub RequestContext. Does not open any sockets — runs offline under pytest.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from governance import reconcile_session as rs
from governance import reconcile_deferred_queue as q
from governance import server, auto_chain
from governance.db import _configure_connection, _ensure_schema


PROJECT_ID = "p-int"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _StubCtx:
    """Minimal RequestContext stand-in for direct handler invocation."""

    def __init__(self, project_id=PROJECT_ID, body=None, query=None,
                 path_params=None, request_id="req-test"):
        self.body = body or {}
        self.query = query or {}
        pp = {"project_id": project_id}
        if path_params:
            pp.update(path_params)
        self.path_params = pp
        self.request_id = request_id
        self.token = ""
        self.idem_key = ""
        self.handler = None
        self.method = "POST"
        self._session = None
        self._conn = None

    def get_project_id(self):
        return self.path_params.get("project_id", "")

    def require_auth(self, conn):
        return {"principal_id": "anonymous", "role": "coordinator"}


def _unwrap(result):
    """Normalize handler return shapes into (status, body)."""
    if isinstance(result, tuple):
        if len(result) == 2:
            a, b = result
            if isinstance(a, int):
                return a, b
            return b, a
        if len(result) == 3:
            a, b, _ = result
            return a, b
    return 200, result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def gov_dir(tmp_path: Path) -> Path:
    d = tmp_path / "governance"
    d.mkdir(parents=True, exist_ok=True)
    graph = {
        "version": 1,
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [{
                "id": "L1.1",
                "title": "Integration Root",
                "layer": "L1",
                "primary": [],
                "secondary": [],
                "test": [],
                "_deps": [],
            }],
            "edges": [],
        },
        "gates_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [],
            "edges": [],
        },
    }
    (d / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return d


@pytest.fixture()
def gov_db(tmp_path: Path, monkeypatch, gov_dir: Path):
    """In-process SQLite DB with full governance schema. Patches DBContext to use
    a single shared connection so commits in one handler are visible in the next.
    """
    db_path = tmp_path / "gov.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _configure_connection(conn, busy_timeout=0)
    _ensure_schema(conn)

    # Some auto_chain helpers expect a legacy audit_log table.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, action TEXT, actor TEXT, ok INTEGER,
            ts TEXT, task_id TEXT, details_json TEXT
        )
    """)
    conn.commit()

    # Patch DBContext / get_connection to use this connection across handlers.
    class _FakeCtx:
        def __init__(self, _project_id):
            self.project_id = _project_id
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                try:
                    self.conn.commit()
                except sqlite3.Error:
                    pass
            else:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
            return False

    monkeypatch.setattr(server, "DBContext", _FakeCtx)
    monkeypatch.setattr("governance.db.DBContext", _FakeCtx)
    monkeypatch.setattr("governance.auto_chain.DBContext", _FakeCtx, raising=False)
    monkeypatch.setattr("governance.db.get_connection",
                        lambda *_a, **_k: conn)
    monkeypatch.setattr("governance.db._resolve_project_dir",
                        lambda *_a, **_k: gov_dir)

    # Patch reconcile_session governance_dir to tmp_path so overlay file lifecycle
    # writes happen under tmp_path (not the real repo).
    monkeypatch.setattr(rs, "_GOVERNANCE_DIR", gov_dir)

    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Endpoint tests (5)
# ---------------------------------------------------------------------------

def test_start_endpoint_201_and_409_on_dup(gov_db):
    ctx = _StubCtx(body={"started_by": "tester", "bypass_gates": ["g1"]})
    status, body = _unwrap(server.handle_reconcile_session_start(ctx))
    assert status == 201, body
    assert body["session"]["status"] == "active"
    sid = body["session"]["session_id"]

    # Duplicate start -> 409 reconcile_session_active_exists
    ctx2 = _StubCtx(body={"started_by": "tester2"})
    status2, body2 = _unwrap(server.handle_reconcile_session_start(ctx2))
    assert status2 == 409
    assert body2["error"] == "reconcile_session_active_exists"
    assert body2.get("session_id") == sid


def test_active_endpoint_returns_session_or_null(gov_db):
    # No active session -> session is None
    ctx = _StubCtx()
    status, body = _unwrap(server.handle_reconcile_session_active(ctx))
    assert status == 200
    assert body["session"] is None

    # Start one, then active should return it.
    server.handle_reconcile_session_start(_StubCtx(body={"started_by": "tester"}))
    status2, body2 = _unwrap(server.handle_reconcile_session_active(_StubCtx()))
    assert status2 == 200
    assert body2["session"] is not None
    assert body2["session"]["status"] == "active"


def test_history_endpoint_orders_desc(gov_db):
    # Insert 3 historical sessions with distinct started_at timestamps.
    rows = [
        ("s-old", "2026-01-01T00:00:00Z", "rolled_back"),
        ("s-mid", "2026-02-01T00:00:00Z", "finalized"),
        ("s-new", "2026-03-01T00:00:00Z", "active"),
    ]
    for sid, ts, status in rows:
        gov_db.execute(
            "INSERT INTO reconcile_sessions (project_id, session_id, status, "
            "started_at, bypass_gates_json) VALUES (?, ?, ?, ?, '[]')",
            (PROJECT_ID, sid, status, ts))
    gov_db.commit()
    status, body = _unwrap(server.handle_reconcile_session_history(_StubCtx()))
    assert status == 200
    sids = [s["session_id"] for s in body["sessions"]]
    assert sids == ["s-new", "s-mid", "s-old"], sids


def test_finalize_endpoint_idempotent(gov_db):
    start_status, start_body = _unwrap(
        server.handle_reconcile_session_start(_StubCtx(body={"started_by": "tester"})))
    assert start_status == 201
    sid = start_body["session"]["session_id"]

    ctx = _StubCtx(path_params={"session_id": sid})
    status1, body1 = _unwrap(server.handle_reconcile_session_finalize(ctx))
    assert status1 == 200
    assert body1["result"]["status"] == "finalized"
    assert body1.get("idempotent") is False

    # Second call should be idempotent: still 200 with status finalized.
    ctx2 = _StubCtx(path_params={"session_id": sid})
    status2, body2 = _unwrap(server.handle_reconcile_session_finalize(ctx2))
    assert status2 == 200
    assert body2["result"]["status"] == "finalized"
    assert body2.get("idempotent") is True


def test_finalize_endpoint_cluster_gate_keeps_session_active(gov_db):
    start_status, start_body = _unwrap(
        server.handle_reconcile_session_start(_StubCtx(
            body={"started_by": "tester", "run_id": "phase-z-http"})))
    assert start_status == 201
    sid = start_body["session"]["session_id"]
    q.enqueue_or_lookup(PROJECT_ID, "fp-http-block", payload={},
                        run_id="phase-z-http", conn=gov_db)

    status, body = _unwrap(server.handle_reconcile_session_finalize(
        _StubCtx(path_params={"session_id": sid})))
    assert status == 409
    assert body["error"] == "reconcile_clusters_incomplete"
    assert body["summary"]["active_count"] == 1

    row = gov_db.execute(
        "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
        (PROJECT_ID, sid),
    ).fetchone()
    assert row[0] == "active"


def test_rollback_endpoint_writes_audit(gov_db):
    start_body = _unwrap(
        server.handle_reconcile_session_start(_StubCtx(body={"started_by": "tester"})))[1]
    sid = start_body["session"]["session_id"]

    ctx = _StubCtx(path_params={"session_id": sid}, body={"actor": "observer-1"})
    status, body = _unwrap(server.handle_reconcile_session_rollback(ctx))
    assert status == 200
    assert body["result"]["status"] == "rolled_back"

    # Audit row recorded with event reconcile_session.rolled_back
    row = gov_db.execute(
        "SELECT event, actor FROM audit_index "
        "WHERE event='reconcile_session.rolled_back' AND project_id=?",
        (PROJECT_ID,)).fetchone()
    assert row is not None, "no audit row written for rollback"
    assert row[0] == "reconcile_session.rolled_back"


# ---------------------------------------------------------------------------
# Scoped-task blocker tests (3)
# ---------------------------------------------------------------------------

def _create_reconcile_scoped_task(body_overrides=None):
    body = {
        "type": "reconcile_doc_sweep",
        "prompt": "scoped reconcile work",
        "metadata": {},
    }
    if body_overrides:
        body.update(body_overrides)
    return server.handle_task_create(_StubCtx(body=body))


def test_scoped_task_blocked_when_session_active(gov_db, monkeypatch):
    # Disable backlog enforcement to focus on session blocker.
    monkeypatch.setenv("OPT_BACKLOG_ENFORCE", "warn")
    # Start a session
    server.handle_reconcile_session_start(_StubCtx(body={"started_by": "tester"}))

    # Now creating a scoped reconcile_* task must be blocked with HTTP 409.
    status, body = _unwrap(_create_reconcile_scoped_task())
    assert status == 409, (status, body)
    assert body["error"] == "reconcile_session_active_blocks_scoped"
    assert body["task_type"] == "reconcile_doc_sweep"
    assert body.get("session_id")


def test_scoped_task_allowed_when_session_idle(gov_db, monkeypatch):
    # No session active -> scoped task should be created normally.
    monkeypatch.setenv("OPT_BACKLOG_ENFORCE", "warn")
    result = _create_reconcile_scoped_task()
    status, body = _unwrap(result)
    # 200/201 path: server returns the task_registry.create_task dict.
    assert status == 200, body
    assert body.get("task_id"), body
    assert body.get("type") == "reconcile_doc_sweep"


def test_inflight_scoped_not_cancelled(gov_db, monkeypatch):
    """Pre-existing claimed/in_progress reconcile_* tasks must NOT be cancelled
    when a session starts."""
    monkeypatch.setenv("OPT_BACKLOG_ENFORCE", "warn")
    # Insert an in-flight task directly.
    gov_db.execute(
        "INSERT INTO tasks (task_id, project_id, status, execution_status, "
        "notification_status, type, prompt, related_nodes, created_by, "
        "created_at, updated_at, priority, max_attempts, metadata_json) "
        "VALUES (?, ?, 'claimed', 'claimed', 'none', 'reconcile_doc_sweep', "
        "'work', '[]', 'tester', '2026-05-02T00:00:00Z', "
        "'2026-05-02T00:00:00Z', 0, 3, '{}')",
        ("task-inflight-1", PROJECT_ID))
    gov_db.commit()

    # Start a session — should not cancel the in-flight task.
    server.handle_reconcile_session_start(_StubCtx(body={"started_by": "tester"}))

    row = gov_db.execute(
        "SELECT status FROM tasks WHERE task_id=?", ("task-inflight-1",)
    ).fetchone()
    assert row is not None
    assert row[0] == "claimed", f"in-flight task was modified: status={row[0]!r}"


# ---------------------------------------------------------------------------
# Bypass middleware tests (2)
# ---------------------------------------------------------------------------

def test_bypass_short_circuits_release_gate(gov_db):
    # Active session with the release-gate bypass in bypass_gates.
    server.handle_reconcile_session_start(_StubCtx(body={
        "started_by": "tester",
        "bypass_gates": ["_gate_release.related_nodes"],
    }))

    # Force a metadata that would otherwise fail — related_nodes referencing
    # a node not at qa_pass would normally block. But the bypass should
    # short-circuit and return passed=True.
    metadata = {
        "task_id": "task-merge-1",
        "related_nodes": ["L99.99"],  # does not exist; would normally fail
        "parent_task_id": "task-pm-1",
    }
    passed, reason = auto_chain._gate_release(gov_db, PROJECT_ID, {}, metadata)
    assert passed is True, reason
    assert reason.startswith("reconcile_session_active_bypass:")
    assert "_gate_release.related_nodes" in reason


def test_bypass_emits_audit_event(gov_db):
    # Active session with bypass for the qa-pass gate.
    server.handle_reconcile_session_start(_StubCtx(body={
        "started_by": "tester",
        "bypass_gates": ["_gate_qa_pass.related_nodes"],
    }))

    metadata = {"task_id": "task-qa-1"}
    bypassed, reason = auto_chain._check_session_bypass(
        "_gate_qa_pass.related_nodes", PROJECT_ID, metadata["task_id"])
    assert bypassed is True
    assert "_gate_qa_pass.related_nodes" in reason

    # Audit row exists with event gate.bypassed.reconcile_session_active.
    row = gov_db.execute(
        "SELECT event, actor FROM audit_index "
        "WHERE event='gate.bypassed.reconcile_session_active' AND project_id=?",
        (PROJECT_ID,)).fetchone()
    assert row is not None, "no audit row written for bypass"
    assert row[0] == "gate.bypassed.reconcile_session_active"
    assert row[1] == "auto-chain"
