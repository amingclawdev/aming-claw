from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_correction_patches
from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import reconcile_semantic_enrichment as semantic_enrichment
from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.errors import ValidationError
from agent.governance.governance_index import merge_feature_hashes_into_graph_nodes


PID = "graph-api-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(path_params: dict, *, method: str = "GET", query: dict | None = None, body: dict | None = None):
    return server.RequestContext(
        None,
        method,
        path_params,
        query or {},
        body or {},
        "req-test",
        "",
        "",
    )


def _bare_handler():
    handler = object.__new__(server.GovernanceHandler)
    handler.path = "/api/health"
    handler.headers = {}
    handler.wfile = io.BytesIO()
    handler.requestline = "GET /api/health HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.client_address = ("127.0.0.1", 0)
    handler.sent_statuses = []
    handler.sent_headers = []
    handler.send_response = lambda code: handler.sent_statuses.append(code)
    handler.send_header = lambda key, value: handler.sent_headers.append((key, value))
    handler.end_headers = lambda: None
    return handler


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    yield c
    c.close()


def _graph(node_id: str = "L7.1") -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": node_id,
                    "layer": "L7",
                    "title": "Feature Node",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/server.py"],
                    "secondary": ["docs/dev/proposal.md"],
                    "test": ["agent/tests/test_graph_governance_api.py"],
                    "metadata": {"subsystem": "governance"},
                }
            ],
            "edges": [
                {
                    "source": node_id,
                    "target": "L3.1",
                    "edge_type": "contains",
                    "direction": "hierarchy",
                    "evidence": {"source": "test"},
                }
            ],
        }
    }


def _graph_with_dependency() -> dict:
    graph = _graph("L7.1")
    graph["deps_graph"]["nodes"].append(
        {
            "id": "L7.2",
            "layer": "L7",
            "title": "Dependency Node",
            "kind": "service_runtime",
            "primary": ["agent/governance/dependency.py"],
            "secondary": [],
            "test": [],
            "metadata": {"subsystem": "governance"},
        }
    )
    graph["deps_graph"]["edges"].append(
        {
            "source": "L7.1",
            "target": "L7.2",
            "edge_type": "depends_on",
            "direction": "dependency",
            "evidence": {"source": "test-dependency"},
        }
    )
    return graph


def test_governance_handler_json_response_includes_dev_cors_headers():
    handler = _bare_handler()

    handler._respond(200, {"ok": True})

    headers = dict(handler.sent_headers)
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in headers["Access-Control-Allow-Methods"]
    assert "POST" in headers["Access-Control-Allow-Methods"]
    assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
    assert "Content-Type" in headers["Access-Control-Allow-Headers"]
    assert "X-Gov-Token" in headers["Access-Control-Allow-Headers"]


def test_governance_handler_options_preflight_includes_dev_cors_headers():
    handler = _bare_handler()

    handler.do_OPTIONS()

    headers = dict(handler.sent_headers)
    assert handler.sent_statuses == [204]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
    assert headers["Access-Control-Max-Age"] == "86400"
    assert headers["Content-Length"] == "0"


def test_graph_governance_status_and_snapshot_query_api(conn):
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old",
        commit_sha="old",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-head",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        candidate["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
    )
    conn.commit()

    status = server.handle_graph_governance_status(
        _ctx({"project_id": PID}, query={"target_commit": "head"})
    )
    assert status["ok"] is True
    assert status["active_snapshot_id"] == "imported-old"
    assert status["pending_scope_reconcile_count"] == 1
    assert status["strict_ready"]["ok"] is False

    snapshots = server.handle_graph_governance_snapshot_list(
        _ctx({"project_id": PID}, query={"status": "candidate,active"})
    )
    assert snapshots["count"] == 2
    assert {row["snapshot_id"] for row in snapshots["snapshots"]} == {"imported-old", "full-head"}

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": "full-head"})
    )
    assert nodes["count"] == 1
    assert nodes["nodes"][0]["primary_files"] == ["agent/governance/server.py"]
    assert nodes["nodes"][0]["metadata"]["subsystem"] == "governance"

    edges = server.handle_graph_governance_snapshot_edges(
        _ctx({"project_id": PID, "snapshot_id": "full-head"})
    )
    assert edges["count"] == 1
    assert edges["edges"][0]["edge_type"] == "contains"


def test_graph_governance_correction_patch_api_lifecycle(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )

    created = server.handle_graph_governance_correction_patch_create(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "patch_id": "patch-api-package-marker",
                "patch_type": "mark_package_marker",
                "target_node_id": "L7.1",
                "patch_json": {"target_node_id": "L7.1"},
                "evidence": {"reason": "empty package initializer"},
                "actor": "observer",
            },
        )
    )
    status, payload = created
    assert status == 201
    assert payload["patch_id"] == "patch-api-package-marker"

    listed = server.handle_graph_governance_correction_patch_list(
        _ctx({"project_id": PID}, query={"status": "proposed"})
    )
    assert listed["count"] == 1
    assert listed["patches"][0]["patch_json"]["target_node_id"] == "L7.1"

    accepted = server.handle_graph_governance_correction_patch_accept(
        _ctx(
            {"project_id": PID, "patch_id": "patch-api-package-marker"},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert accepted["status"] == "accepted"

    listed = server.handle_graph_governance_correction_patch_list(
        _ctx({"project_id": PID}, query={"status": "accepted"})
    )
    assert listed["count"] == 1
    assert listed["patches"][0]["status"] == "accepted"


def test_feedback_decision_accept_graph_correction_creates_patch(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-decision",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    from agent.governance import reconcile_feedback

    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="semantic-ai",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "type": "add_relation",
                "target": "L7.2",
                "edge_type": "depends_on",
                "summary": "L7.1 depends on L7.2",
            }
        ],
    )
    feedback_id = classified["items"][0]["feedback_id"]

    decided = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": feedback_id,
                "action": "accept_graph_correction",
                "actor": "observer",
            },
        )
    )

    assert decided["decided_count"] == 1
    assert decided["graph_patches"]["created_count"] == 1
    assert decided["graph_patches"]["patches"][0]["status"] == "accepted"

    listed = server.handle_graph_governance_correction_patch_list(
        _ctx({"project_id": PID}, query={"status": "accepted"})
    )
    assert listed["count"] == 1
    assert listed["patches"][0]["patch_json"]["edge"]["dst"] == "L7.2"


def test_graph_governance_active_alias_resolves_for_nodes_and_edges(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-active-alias",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": "active"})
    )
    edges = server.handle_graph_governance_snapshot_edges(
        _ctx({"project_id": PID, "snapshot_id": "active"})
    )

    assert nodes["snapshot_id"] == "full-active-alias"
    assert nodes["count"] == 1
    assert edges["snapshot_id"] == "full-active-alias"
    assert edges["count"] == 1


def test_graph_governance_snapshot_nodes_include_semantic_overlay(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-nodes",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    semantic_payload = {
        "feature_name": "Governance Server Feature",
        "domain_label": "governance/api",
        "intent": "Expose graph-governance HTTP routes for dashboard users.",
        "doc_status": "adequate",
        "test_status": "adequate",
        "config_status": "n/a",
        "quality_flags": [],
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:feature',
                '{"agent/governance/server.py":"sha256:file"}', ?, 2, 7, '2026-05-09T20:31:24Z')
        """,
        (PID, snapshot["snapshot_id"], json.dumps(semantic_payload)),
    )
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:feature',
                '{"agent/governance/server.py":"sha256:file"}', 2, 7, 1,
                '2026-05-09T20:31:24Z', '2026-05-09T20:00:00Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    semantic = nodes["nodes"][0]["semantic"]
    assert semantic["status"] == "ai_complete"
    assert semantic["node_status"] == "ai_complete"
    assert semantic["job_status"] == "ai_complete"
    assert semantic["hash_state"] == "current"
    assert semantic["has_semantic_payload"] is True
    assert semantic["feature_name"] == "Governance Server Feature"
    assert semantic["domain_label"] == "governance/api"
    assert semantic["file_hashes"]["agent/governance/server.py"] == "sha256:file"
    assert semantic["job"]["attempt_count"] == 1

    structure_only = server.handle_graph_governance_snapshot_nodes(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"include_semantic": "false"},
        )
    )
    assert "semantic" not in structure_only["nodes"][0]


def test_graph_governance_semantic_jobs_endpoint_enqueues_existing_semantic_jobs(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-jobs-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    created = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "node",
                "target_ids": ["L7.1"],
                "options": {"skip_current": False},
                "created_by": "dashboard_user",
            },
        )
    )

    status, payload = created
    assert status == 202
    assert payload["ok"] is True
    assert payload["status"] == "queued"
    assert payload["summary"]["by_status"]["ai_pending"] == 1
    assert payload["summary"]["progress"]["open"] == 1
    assert payload["operator_request"]["requested_by"] == "dashboard_user"
    assert payload["operator_request"]["query_source"] == "dashboard"
    assert payload["operator_request"]["analyzer"]["model"]
    assert payload["batch_plan"]["target_scope"] == "node"
    assert payload["batch_plan"]["target_ids"] == ["L7.1"]

    listed = server.handle_graph_governance_snapshot_semantic_jobs_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"status": "ai_pending"},
        )
    )
    assert listed["count"] == 1
    assert listed["summary"]["progress"]["pending"] == 1
    assert listed["jobs"][0]["node_id"] == "L7.1"
    assert listed["jobs"][0]["status"] == "ai_pending"
    assert listed["jobs"][0]["job_id"] == "L7.1"
    fetched = server.handle_graph_governance_snapshot_semantic_job_get(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": "L7.1"})
    )
    assert fetched["job"]["status"] == "ai_pending"
    cancelled = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": "L7.1"},
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert cancelled["job"]["status"] == "cancelled"
    retried = server.handle_graph_governance_snapshot_semantic_job_retry(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": "L7.1"},
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert retried["job"]["status"] == "pending_ai"
    events = server.handle_graph_governance_snapshot_events_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"event_type": "semantic_retry_requested"},
        )
    )
    assert events["count"] == 2
    assert events["events"][0]["target_type"] == "node"
    assert events["events"][0]["target_id"] == "L7.1"
    assert events["events"][0]["payload"]["operator_request"]["requested_by"] == "dashboard_user"
    assert events["events"][0]["payload"]["batch_plan"]["target_ids"] == ["L7.1"]


def test_graph_governance_semantic_jobs_endpoint_records_edge_requests_as_events(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-jobs-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "edges": [{"src": "L7.1", "dst": "L3.1", "edge_type": "contains"}],
            },
        )
    )

    assert status == 202
    assert payload["target_scope"] == "edge"
    assert payload["queued_count"] == 1
    assert payload["operator_request"]["query_source"] == "dashboard"
    assert payload["batch_plan"]["target_scope"] == "edge"
    assert payload["events"][0]["event_type"] == "edge_semantic_requested"
    assert payload["events"][0]["target_id"] == "L7.1->L3.1:contains"
    assert payload["events"][0]["payload"]["operator_request"]["batch_plan"]["target_scope"] == "edge"
    assert payload["events"][0]["payload"]["edge_context"]["edge_id"] == "L7.1->L3.1:contains"


def test_graph_governance_edge_semantic_projection_tracks_requested_and_enriched_edges(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-projection",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "selector": {"all_eligible": True, "edge_types": ["depends_on"], "limit": 10},
                "actor": "dashboard_user",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["batch_plan"]["target_ids"] == ["L7.1->L7.2:depends_on"]
    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-requested"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_requested"
    assert projected["health"]["edge_semantic_eligible_count"] == 1
    assert projected["health"]["edge_semantic_requested_count"] == 1
    assert projected["health"]["edge_semantic_current_count"] == 0
    edge_jobs = server.handle_graph_governance_snapshot_semantic_jobs_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"target_scope": "edge"},
        )
    )
    assert edge_jobs["target_scope"] == "edge"
    assert edge_jobs["count"] == 1
    assert edge_jobs["jobs"][0]["edge_id"] == "L7.1->L7.2:depends_on"
    assert edge_jobs["jobs"][0]["status"] == "ai_pending"
    assert edge_jobs["summary"]["by_status"] == {"ai_pending": 1}

    status, enriched = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "edges": [{"src": "L7.1", "dst": "L7.2", "edge_type": "depends_on"}],
                "edge_semantics": [
                    {
                        "src": "L7.1",
                        "dst": "L7.2",
                        "edge_type": "depends_on",
                        "relation_purpose": "Feature Node calls Dependency Node.",
                        "confidence": 0.9,
                    }
                ],
                "actor": "semantic-ai",
            },
        )
    )
    assert status == 202
    assert enriched["events"][0]["event_type"] == "edge_semantic_enriched"

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-enriched"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "Feature Node calls Dependency Node."
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_coverage_ratio"] == 1.0
    edge_jobs = server.handle_graph_governance_snapshot_semantic_jobs_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"target_scope": "edge"},
        )
    )
    assert edge_jobs["count"] == 1
    assert edge_jobs["jobs"][0]["status"] == "ai_complete"
    assert edge_jobs["jobs"][0]["semantic"]["relation_purpose"] == "Feature Node calls Dependency Node."
    assert edge_jobs["summary"]["progress"]["complete"] == 1

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )
    semantic_health = summary["health"]["semantic_health"]
    assert semantic_health["edge_semantic_eligible_count"] == 1
    assert semantic_health["edge_semantic_current_count"] == 1
    assert semantic_health["edge_semantic_requested_count"] == 0
    assert semantic_health["edge_semantic_missing_count"] == 0
    assert semantic_health["edge_semantic_coverage_ratio"] == 1.0


def test_graph_governance_edge_semantic_jobs_auto_enrich_and_controls(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-auto-runner",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "selector": {"all_eligible": True, "edge_types": ["depends_on"], "limit": 10},
                "semantic_mode": "auto",
                "actor": "dashboard_user",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["enriched_count"] == 1
    assert [event["event_type"] for event in payload["events"]] == [
        "edge_semantic_requested",
        "edge_semantic_enriched",
    ]
    assert payload["jobs"][0]["status"] == "rule_complete"
    assert payload["jobs"][0]["semantic"]["relation_purpose"] == "L7.1 depends on L7.2."
    assert payload["jobs"][0]["semantic"]["analyzer_role"] == "reconcile_edge_semantic_analyzer"
    assert payload["jobs"][0]["semantic_source"] == "edge_semantic_rule"
    assert payload["jobs"][0]["requires_ai"] is True

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-auto-runner"},
        )
    )
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_rule_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 1
    assert projected["health"]["edge_semantic_needs_ai_count"] == 1
    assert projected["health"]["edge_semantic_payload_current_count"] == 1
    assert projected["health"]["edge_semantic_coverage_ratio"] == 0.0
    assert projected["health"]["edge_semantic_payload_coverage_ratio"] == 1.0

    # MF-2026-05-10-013: terminal-status edge rows (including rule_complete)
    # are now hidden by default; pass include_terminal to assert on them.
    queue = server.handle_graph_governance_operations_queue(
        _ctx(
            {"project_id": PID},
            query={
                "snapshot_id": snapshot["snapshot_id"],
                "include_terminal": "true",
            },
        )
    )
    operations = {row["operation_type"]: row for row in queue["operations"]}
    assert operations["edge_semantic"]["status"] == "rule_complete"
    assert "run_edge_semantics" in operations["edge_semantic"]["supported_actions"]
    assert "retry" in operations["edge_semantic"]["supported_actions"]
    assert "edge-semantic:not-queued" not in {row["operation_id"] for row in queue["operations"]}

    edge_event_id = payload["jobs"][0]["event_id"]
    cancel = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": edge_event_id},
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert cancel["job"]["status"] == "rejected"
    retry = server.handle_graph_governance_snapshot_semantic_job_retry(
        _ctx(
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "job_id": "L7.1->L7.2:depends_on",
            },
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert retry["job"]["status"] == "ai_pending"
    assert retry["event"]["event_type"] == "edge_semantic_requested"


def test_graph_governance_semantic_events_backfill_and_projection_are_hash_aware(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph()
    feature_hash = graph_events.feature_hash_for_node(graph["deps_graph"]["nodes"][0])
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-event-source",
        commit_sha="commit-a",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    semantic_payload = {
        "feature_name": "Graph Governance API",
        "semantic_purpose": "Expose graph state and semantic controls for dashboard workflows.",
        "domain_label": "governance/graph",
        "quality_flags": [],
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?, '{"agent/governance/server.py":"sha256:file-a"}',
                ?, 1, 0, '2026-05-09T20:31:24Z')
        """,
        (PID, snapshot["snapshot_id"], feature_hash, json.dumps(semantic_payload)),
    )
    conn.commit()

    backfilled = server.handle_graph_governance_snapshot_semantic_events_backfill(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert backfilled["semantic_node_events_created"] == 1
    events = server.handle_graph_governance_snapshot_events_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"event_type": "semantic_node_enriched"},
        )
    )
    assert events["count"] == 1
    assert events["events"][0]["feature_hash"] == feature_hash
    assert events["events"][0]["file_hashes"]["agent/governance/server.py"] == "sha256:file-a"

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-current"},
        )
    )
    assert projected["health"]["semantic_current_count"] == 1
    assert projected["projection"]["node_semantics"]["L7.1"]["validity"]["status"] == "semantic_current"
    assert projected["projection"]["node_semantics"]["L7.1"]["semantic"]["feature_name"] == "Graph Governance API"
    fetched = server.handle_graph_governance_snapshot_semantic_projection_get(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )
    assert fetched["projection_id"] == "semproj-current"

    changed_graph = _graph()
    changed_graph["deps_graph"]["nodes"][0]["title"] = "Renamed Feature Node"
    changed_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-semantic-event-source",
        commit_sha="commit-b",
        snapshot_kind="scope",
        parent_snapshot_id=snapshot["snapshot_id"],
        graph_json=changed_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        changed_snapshot["snapshot_id"],
        nodes=changed_graph["deps_graph"]["nodes"],
        edges=changed_graph["deps_graph"]["edges"],
    )
    conn.commit()

    changed_projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": changed_snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "backfill_existing": False},
        )
    )
    changed_semantic = changed_projected["projection"]["node_semantics"]["L7.1"]
    assert changed_semantic["semantic"]["feature_name"] == "Graph Governance API"
    assert changed_semantic["validity"]["status"] == "semantic_stale_feature_hash"
    assert changed_projected["health"]["semantic_stale_count"] == 1


def test_graph_governance_current_state_contract_reports_graph_and_semantic_drift(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: Path("."))
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "commit-b")
    monkeypatch.setattr(
        server,
        "_git_changed_paths_between",
        lambda _root, _base, _target, limit=25: ["agent/governance/server.py"],
    )
    base_graph = _graph_with_dependency()
    feature_hash = graph_events.feature_hash_for_node(base_graph["deps_graph"]["nodes"][0])
    base_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-current-state-base",
        commit_sha="commit-base",
        snapshot_kind="full",
        graph_json=base_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        base_snapshot["snapshot_id"],
        nodes=base_graph["deps_graph"]["nodes"],
        edges=base_graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?,
                '{"agent/governance/server.py":"sha256:file-base"}',
                ?, 1, 0, '2026-05-10T00:00:00Z')
        """,
        (
            PID,
            base_snapshot["snapshot_id"],
            feature_hash,
            json.dumps({"feature_name": "Stale semantic"}),
        ),
    )
    conn.commit()
    server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": base_snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-current-state-base"},
        )
    )
    changed_graph = _graph_with_dependency()
    changed_graph["deps_graph"]["nodes"][0]["title"] = "Renamed Feature Node"
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-current-state-contract",
        commit_sha="commit-a",
        snapshot_kind="full",
        parent_snapshot_id=base_snapshot["snapshot_id"],
        graph_json=changed_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=changed_graph["deps_graph"]["nodes"],
        edges=changed_graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-current-state", "backfill_existing": False},
        )
    )
    assert projected["health"]["semantic_stale_count"] == 1
    assert projected["health"]["semantic_missing_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 1

    status = server.handle_graph_governance_status(_ctx({"project_id": PID}))

    current = status["current_state"]
    assert current["graph_stale"]["is_stale"] is True
    assert current["graph_stale"]["active_graph_commit"] == "commit-a"
    assert current["graph_stale"]["head_commit"] == "commit-b"
    assert current["graph_stale"]["changed_file_count"] == 1
    assert current["semantic_snapshot"]["projection_id"] == "semproj-current-state"
    assert current["semantic_snapshot"]["base_commit"] == "commit-a"
    assert current["semantic_drift"]["node_stale"] == 1
    assert current["semantic_drift"]["node_missing"] == 1
    assert current["semantic_drift"]["edge_missing"] == 1
    assert current["semantic_drift"]["semantic_status_counts"]["semantic_stale_feature_hash"] == 1
    assert current["drift_ledger"]["count"] == 0
    assert current["drift_ledger"]["ledger_only"] is True

    drift = server.handle_graph_governance_drift_list(_ctx({"project_id": PID}))
    assert drift["count"] == 0
    assert drift["ledger_only"] is True
    assert drift["graph_stale"]["is_stale"] is True
    assert drift["semantic_drift"]["edge_missing"] == 1

    operations = server.handle_graph_governance_operations_queue(
        _ctx(
            {"project_id": PID},
            query={"include_status_observations": "true", "include_resolved": "true"},
        )
    )
    assert operations["summary"]["current_state"]["graph_stale"]["is_stale"] is True
    assert operations["summary"]["semantic_snapshot"]["projection_id"] == "semproj-current-state"
    assert operations["summary"]["semantic_drift"]["node_stale"] == 1
    ops_by_id = {row["operation_id"]: row for row in operations["operations"]}
    stale_node_row = ops_by_id["node-semantic:not-queued"]
    assert stale_node_row["operation_type"] == "node_semantic"
    assert stale_node_row["status"] == "not_queued"
    assert stale_node_row["progress"] == {"done": 0, "total": 1}
    assert stale_node_row["supported_actions"] == ["queue_node_semantics", "file_backlog", "view_trace"]
    assert operations["summary"]["by_type"]["node_semantic"] == 1
    assert operations["summary"]["by_status"]["not_queued"] == 3


def test_graph_governance_semantic_projection_treats_hash_source_gap_as_internal(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    base_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-unverified-base",
        commit_sha="commit-base",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        base_snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete',
                'sha256:1111111111111111111111111111111111111111111111111111111111111111',
                '{"agent/governance/server.py":"sha256:file-a"}',
                ?, 1, 0, '2026-05-10T00:00:00Z')
        """,
        (
            PID,
            base_snapshot["snapshot_id"],
            json.dumps({"feature_name": "Old semantic with indexed hash"}),
        ),
    )
    conn.commit()
    server.handle_graph_governance_snapshot_semantic_events_backfill(
        _ctx(
            {"project_id": PID, "snapshot_id": base_snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-semantic-unverified",
        commit_sha="commit-unverified",
        snapshot_kind="scope",
        parent_snapshot_id=base_snapshot["snapshot_id"],
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-unverified", "backfill_existing": False},
        )
    )

    health = projected["health"]
    assert health["semantic_unverified_hash_count"] == 0
    assert health["semantic_review_debt_count"] == 0
    assert health["semantic_trusted_ratio"] == 1.0
    assert health["semantic_debt_penalty"] == 0.0
    assert health["project_health_score"] > 90
    assert projected["projection"]["node_semantics"]["L7.1"]["validity"]["status"] == (
        "semantic_carried_forward_current"
    )
    assert projected["projection"]["node_semantics"]["L7.1"]["validity"]["hash_validation"] == (
        "hash_source_unavailable"
    )


def test_graph_governance_active_dashboard_bundle_returns_common_dashboard_data(conn):
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-dashboard-active-bundle",
        commit_sha="commit-dashboard",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=[
            {
                "path": "agent/governance/server.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
            },
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'pending_ai', 'sha256:feature', '{}',
                1, 0, 0, '2026-05-10T00:01:00Z', '2026-05-10T00:00:00Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="semantic_retry_requested",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status="observed",
        payload={"reason": "dashboard bundle test"},
        created_by="observer",
    )
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-dashboard-bundle",
    )
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add a relation for the dashboard bundle.",
            "target": "agent.governance.server",
            "type": "add_typed_relation",
        }],
    )
    conn.commit()

    bundle = server.handle_graph_governance_dashboard_active_bundle(
        _ctx(
            {"project_id": PID},
            query={"node_limit": "10", "edge_limit": "10", "event_limit": "10", "job_limit": "10"},
        )
    )

    assert bundle["ok"] is True
    assert bundle["snapshot_id"] == snapshot["snapshot_id"]
    assert bundle["summary"]["snapshot_id"] == snapshot["snapshot_id"]
    assert bundle["nodes"][0]["node_id"] == "L7.1"
    assert bundle["events"]["count"] >= 1
    assert bundle["semantic_jobs"]["summary"]["by_status"] == {"pending_ai": 1}
    assert bundle["semantic_projection"]["projection_id"] == "semproj-dashboard-bundle"
    assert bundle["feedback_queue"]["summary"]["raw_count"] == 1
    assert bundle["commit_timeline"]["count"] == 1
    assert "semantic_projection" in bundle["endpoints"]


def test_graph_governance_node_timeline_combines_events_semantics_jobs_and_feedback(conn):
    graph = _graph()
    feature_hash = graph_events.feature_hash_for_node(graph["deps_graph"]["nodes"][0])
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-node-timeline",
        commit_sha="commit-node-timeline",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?, '{"agent/governance/server.py":"sha256:file"}',
                1, 0, 1, '2026-05-10T01:02:00Z', '2026-05-10T01:00:00Z')
        """,
        (PID, snapshot["snapshot_id"], feature_hash),
    )
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status="observed",
        feature_hash=feature_hash,
        file_hashes={"agent/governance/server.py": "sha256:file"},
        payload={"semantic_payload": {"feature_name": "Timeline Feature"}},
        created_by="semantic-ai",
    )
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-node-timeline",
    )
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "coverage_gap",
            "summary": "Timeline feature needs review.",
            "type": "missing_doc_binding",
        }],
    )
    conn.commit()

    result = server.handle_graph_governance_snapshot_node_timeline(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "node_id": "L7.1"})
    )

    assert result["ok"] is True
    assert result["node"]["node_id"] == "L7.1"
    assert result["semantic"]["semantic"]["feature_name"] == "Timeline Feature"
    assert result["semantic_job"]["status"] == "ai_complete"
    assert result["summary"]["event_count"] >= 1
    assert result["summary"]["feedback_count"] == 1
    assert {item["source"] for item in result["timeline"]} >= {
        "snapshot_node",
        "semantic_projection",
        "semantic_job",
        "graph_event",
        "feedback",
    }


def test_graph_governance_semantic_projection_excludes_package_markers_from_feature_health(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph()
    graph["deps_graph"]["nodes"].append({
        "id": "L7.2",
        "layer": "L7",
        "title": "agent.governance",
        "kind": "service_runtime",
        "primary": ["agent/governance/__init__.py"],
        "secondary": [],
        "test": [],
        "metadata": {
            "exclude_as_feature": True,
            "file_role": "package_marker",
        },
    })
    graph["deps_graph"]["edges"].append({
        "source": "L7.2",
        "target": "L3.1",
        "edge_type": "contains",
        "direction": "hierarchy",
        "evidence": {"source": "test"},
    })
    feature_hash = graph_events.feature_hash_for_node(graph["deps_graph"]["nodes"][0])
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-exclude-marker",
        commit_sha="commit-marker",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?, '{}',
                ?, 1, 0, '2026-05-10T00:00:00Z')
        """,
        (
            PID,
            snapshot["snapshot_id"],
            feature_hash,
            json.dumps({"feature_name": "Governed feature"}),
        ),
    )
    conn.commit()

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )

    assert projected["health"]["raw_feature_count"] == 2
    assert projected["health"]["governed_feature_count"] == 1
    assert projected["health"]["excluded_feature_count"] == 1
    assert projected["health"]["feature_count"] == 1
    assert projected["health"]["semantic_current_count"] == 1
    assert projected["health"]["doc_coverage_ratio"] == 1.0
    assert projected["health"]["test_coverage_ratio"] == 1.0


def test_graph_governance_events_lifecycle_and_materialize_candidate_snapshot(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-events-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'semantic_complete', 'oldhash', '{}', '{}', 1, 0, 'now')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    status, created = server.handle_graph_governance_snapshot_events_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "event_type": "node_rename_proposed",
                "target_type": "node",
                "target_id": "L7.1",
                "payload": {"new_title": "Renamed Feature"},
                "actor": "dashboard_user",
            },
        )
    )
    assert status == 201
    event_id = created["event"]["event_id"]

    accepted = server.handle_graph_governance_snapshot_event_accept(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "event_id": event_id},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert accepted["event"]["status"] == graph_events.EVENT_STATUS_ACCEPTED
    status, stale_candidate = server.handle_graph_governance_snapshot_events_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "event_type": "doc_binding_added",
                "target_type": "node",
                "target_id": "L7.1",
                "payload": {"files": ["docs/dev/new-doc.md"]},
                "precondition": {"expected_node_title": "Not The Current Title"},
            },
        )
    )
    assert status == 201
    stale_event_id = stale_candidate["event"]["event_id"]
    server.handle_graph_governance_snapshot_event_accept(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "event_id": stale_event_id},
            method="POST",
            body={"actor": "observer"},
        )
    )

    materialized = server.handle_graph_governance_snapshot_events_materialize(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert materialized["materialized_count"] == 1
    assert materialized["new_snapshot_id"]
    graph_json = json.loads(store.snapshot_graph_path(PID, materialized["new_snapshot_id"]).read_text(encoding="utf-8"))
    assert graph_json["deps_graph"]["nodes"][0]["title"] == "Renamed Feature"
    event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], event_id)
    assert event["status"] == graph_events.EVENT_STATUS_MATERIALIZED
    stale_event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], stale_event_id)
    assert stale_event["status"] == graph_events.EVENT_STATUS_STALE
    semantic_row = conn.execute(
        """
        SELECT status FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L7.1'
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert semantic_row["status"] == "semantic_stale"


def test_graph_governance_materialize_preview_does_not_mutate_events_or_snapshots(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-events-preview-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    status, created = server.handle_graph_governance_snapshot_events_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "event_type": "node_rename_proposed",
                "target_type": "node",
                "target_id": "L7.1",
                "payload": {"new_title": "Previewed Feature"},
                "actor": "dashboard_user",
            },
        )
    )
    assert status == 201
    event_id = created["event"]["event_id"]

    preview = server.handle_graph_governance_snapshot_events_materialize_preview(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "event_id": event_id},
        )
    )

    assert preview["ok"] is True
    assert preview["would_create_snapshot"] is True
    assert preview["would_materialize_count"] == 1
    assert preview["diff"]["nodes"]["changed_count"] == 1
    assert preview["diff"]["nodes"]["changed"][0]["after"]["title"] == "Previewed Feature"
    event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], event_id)
    assert event["status"] == graph_events.EVENT_STATUS_PROPOSED
    rows = conn.execute(
        """
        SELECT COUNT(*) AS count FROM graph_snapshots
        WHERE project_id=? AND parent_snapshot_id=?
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert rows["count"] == 0


def test_graph_governance_dashboard_api_summarizes_active_state(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-dashboard",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "agent/service.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
            },
            {
                "path": "README.md",
                "file_kind": "index_doc",
                "scan_status": "index_asset",
                "graph_status": "index_asset",
                "decision": "attach_to_index_wrapper",
            },
        ],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="head",
        path="README.md",
        drift_type="missing_test",
        target_symbol="doc:index",
    )
    conn.commit()

    dashboard = server.handle_graph_governance_dashboard(
        _ctx({"project_id": PID}, query={"file_sample_limit": "1"})
    )

    assert dashboard["ok"] is True
    assert dashboard["snapshot_id"] == snapshot["snapshot_id"]
    assert dashboard["status"]["active_snapshot_id"] == snapshot["snapshot_id"]
    assert dashboard["file_state"]["summary"]["by_kind"]["source"] == 1
    assert dashboard["file_state"]["total_count"] == 2
    assert dashboard["drift_summary"]["by_status"]["open"] == 1
    assert dashboard["drift_summary"]["by_type"]["missing_test"] == 1


def test_graph_governance_commit_anchored_dashboard_p0_apis(conn, monkeypatch):
    monkeypatch.setattr(server, "_git_commit_subject", lambda sha: f"subject {sha[:7]}")
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-old-dashboard",
        commit_sha="oldcommit",
        snapshot_kind="scope",
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "agent/governance/server.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
            },
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
            },
        ],
        notes=json.dumps({
            "semantic_enrichment": {
                "semantic_graph_state": {"open_issue_count": 3}
            },
            "global_semantic_review": {
                "latest_full_semantic_coverage_ratio": 0.5
            },
        }),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        old["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    # MF-2026-05-10-012: keep this fixture's semantic_health=="metadata_only"
    # behaviour by skipping the new auto-rebuild hook. The test asserts the
    # placeholder status that legacy snapshots carry before any projection
    # has been built.
    store.activate_graph_snapshot(
        conn, PID, old["snapshot_id"], auto_rebuild_projection=False
    )
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-new-dashboard",
        commit_sha="newcommit",
        snapshot_kind="scope",
        graph_json=_graph("L7.2"),
        file_inventory=[],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        candidate["snapshot_id"],
        nodes=_graph("L7.2")["deps_graph"]["nodes"],
        edges=_graph("L7.2")["deps_graph"]["edges"],
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="pendingcommit",
        parent_commit_sha="oldcommit",
    )
    graph_correction_patches.create_patch(
        conn,
        PID,
        patch_id="patch-summary-accepted",
        patch_type="add_edge",
        risk_level="low",
        target_node_id="L7.1",
        patch_json={
            "edge": {
                "src": "L7.1",
                "dst": "L7.2",
                "edge_type": "depends_on",
                "direction": "dependency",
            }
        },
        evidence={"reason": "dashboard summary test"},
    )
    graph_correction_patches.create_patch(
        conn,
        PID,
        patch_id="patch-summary-proposed-high",
        patch_type="merge_nodes",
        risk_level="high",
        target_node_id="L7.1",
        patch_json={"source_node_ids": ["L7.1", "L7.2"], "target_node_id": "L7.1"},
        evidence={"reason": "dashboard summary test"},
    )
    graph_correction_patches.accept_patch(conn, PID, "patch-summary-accepted", accepted_by="observer")
    conn.commit()

    timeline = server.handle_graph_governance_commit_timeline(
        _ctx({"project_id": PID}, query={"include_backlog": "false"})
    )
    assert timeline["ok"] is True
    assert timeline["active_snapshot_id"] == old["snapshot_id"]
    commits = {row["commit_sha"]: row for row in timeline["commits"]}
    assert commits["oldcommit"]["is_active"] is True
    assert commits["oldcommit"]["subject"] == "subject oldcomm"
    assert commits["oldcommit"]["counts"]["features"] == 1
    assert commits["oldcommit"]["counts"]["orphan_files"] == 1
    assert commits["oldcommit"]["counts"]["ai_review_feedback"] == 3
    assert commits["newcommit"]["snapshot_status"] == "candidate"

    exact = server.handle_graph_governance_commit_graph_state(
        _ctx({"project_id": PID, "commit_sha": "oldcommit"})
    )
    assert exact["resolution"] == "exact"
    assert exact["resolved_snapshot_id"] == old["snapshot_id"]
    assert exact["is_active"] is True
    assert exact["has_semantic_review"] is True

    pending = server.handle_graph_governance_commit_graph_state(
        _ctx({"project_id": PID, "commit_sha": "pendingcommit"})
    )
    assert pending["resolution"] == "pending"
    assert pending["pending_scope_reconcile"] is True

    advisory = server.handle_graph_governance_commit_graph_state(
        _ctx({"project_id": PID, "commit_sha": "missingcommit"})
    )
    assert advisory["resolution"] == "advisory_latest"
    assert advisory["resolved_snapshot_id"] == old["snapshot_id"]
    assert advisory["warnings"]

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": old["snapshot_id"]})
    )
    assert summary["counts"]["nodes"] == 1
    assert summary["counts"]["edges"] == 1
    assert summary["counts"]["files"] == 2
    assert summary["health"]["semantic_coverage_ratio"] == 0.5
    assert summary["health"]["structure_health_score"] is not None
    assert summary["health"]["structure_health"]["governed_feature_count"] == 1
    assert summary["health"]["structure_health"]["l4_asset_health"]["asset_count"] == 0
    assert summary["health"]["semantic_health"]["status"] == "metadata_only"
    assert summary["health"]["project_insight_health"]["status"] == "metadata_only"
    assert summary["graph_correction_patches"]["total"] == 2
    assert summary["graph_correction_patches"]["replayable_count"] == 1
    assert summary["graph_correction_patches"]["high_risk_proposed_count"] == 1


def test_graph_governance_summary_exposes_file_hygiene_review_samples(conn, tmp_path):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-summary",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    report_path = tmp_path / "global-review.json"
    report_path.write_text(
        json.dumps(
            {
                "health_picture": {
                    "project_health_score": 81.5,
                    "file_hygiene_score": 57.45,
                    "low_health_count": 3,
                    "project_health_issue_counts": {"file_hygiene": 2},
                    "file_hygiene": {
                        "available": True,
                        "run_id": "inventory-run",
                        "total_files": 7,
                        "review_required_count": 2,
                        "orphan_count": 1,
                        "pending_decision_count": 1,
                        "cleanup_candidate_count": 1,
                        "cleanup_candidate_mb": 4.5,
                        "by_kind": {"doc": 1, "generated": 1},
                        "by_scan_status": {"orphan": 1, "ignored": 1},
                        "by_graph_status": {"unmapped": 1, "ignored": 1},
                        "review_required_sample": [
                            {
                                "path": "docs/orphan.md",
                                "file_kind": "doc",
                                "suggested_dashboard_actions": ["attach_to_node", "waive"],
                            }
                        ],
                        "cleanup_candidate_sample": [
                            {
                                "path": "docs/dev/scratch/ai-output.json",
                                "file_kind": "generated",
                                "size_bytes": 4718592,
                                "suggested_dashboard_actions": ["delete_candidate", "waive"],
                            }
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    notes = {
        "global_semantic_review": {
            "latest_full_review_path": str(report_path),
            "latest_full_run_id": "full-review-file-hygiene",
            "latest_full_status": "completed",
        }
    }
    conn.execute(
        "UPDATE graph_snapshots SET notes=? WHERE project_id=? AND snapshot_id=?",
        (json.dumps(notes), PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    insight = summary["health"]["project_insight_health"]
    assert insight["status"] == "reviewed"
    assert insight["file_hygiene_score"] == 57.45
    assert insight["file_hygiene"]["available"] is True
    assert insight["file_hygiene"]["review_required_count"] == 2
    assert insight["file_hygiene"]["cleanup_candidate_count"] == 1
    assert insight["file_hygiene"]["review_required_sample"][0]["path"] == "docs/orphan.md"
    assert (
        insight["file_hygiene"]["cleanup_candidate_sample"][0]["path"]
        == "docs/dev/scratch/ai-output.json"
    )


def test_graph_governance_file_hygiene_actions_create_auditable_events(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-actions",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.write_companion_files(
        PID,
        snapshot["snapshot_id"],
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
                "size_bytes": 123,
            },
            {
                "path": "docs/dev/scratch/ai-output.json",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 456,
            },
        ],
    )
    conn.commit()

    status, attached = server.handle_graph_governance_snapshot_file_hygiene_action(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "action": "attach_to_node",
                "path": "docs/orphan.md",
                "node_id": "L7.1",
                "actor": "dashboard-user",
            },
        )
    )
    assert status == 201
    assert attached["event"]["event_type"] == "doc_binding_added"
    assert attached["event"]["target_type"] == "node"
    assert attached["event"]["target_id"] == "L7.1"
    assert attached["event"]["payload"]["files"] == ["docs/orphan.md"]
    assert attached["event"]["payload"]["destructive_mutation_performed"] is False

    with pytest.raises(ValidationError, match="confirm_delete_candidate"):
        server.handle_graph_governance_snapshot_file_hygiene_action(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={
                    "action": "delete_candidate",
                    "path": "docs/dev/scratch/ai-output.json",
                    "actor": "dashboard-user",
                },
            )
        )

    status, delete_candidate = server.handle_graph_governance_snapshot_file_hygiene_action(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "action": "delete_candidate",
                "path": "docs/dev/scratch/ai-output.json",
                "confirm_delete_candidate": True,
                "actor": "dashboard-user",
            },
        )
    )
    assert status == 201
    assert delete_candidate["event"]["event_type"] == "file_delete_candidate"
    assert delete_candidate["event"]["target_type"] == "file"
    assert delete_candidate["event"]["target_id"] == "docs/dev/scratch/ai-output.json"
    assert delete_candidate["event"]["risk_level"] == "high"
    assert delete_candidate["event"]["payload"]["destructive_mutation_performed"] is False


def test_graph_governance_file_hygiene_batch_actions_create_auditable_events(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-batch-actions",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.write_companion_files(
        PID,
        snapshot["snapshot_id"],
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
                "size_bytes": 123,
            },
            {
                "path": "docs/dev/scratch/ai-output.json",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 456,
            },
        ],
    )
    conn.commit()

    with pytest.raises(ValidationError, match="file inventory row not found"):
        server.handle_graph_governance_snapshot_file_hygiene_actions_batch(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={
                    "actor": "dashboard-user",
                    "actions": [
                        {"action": "waive", "path": "missing.md"},
                    ],
                },
            )
        )

    status, result = server.handle_graph_governance_snapshot_file_hygiene_actions_batch(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "dashboard-user",
                "confirm_delete_candidate": True,
                "actions": [
                    {"action": "attach_to_node", "path": "docs/orphan.md", "node_id": "L7.1"},
                    {"action": "delete_candidate", "path": "docs/dev/scratch/ai-output.json"},
                ],
            },
        )
    )

    assert status == 201
    assert result["count"] == 2
    assert [event["event_type"] for event in result["events"]] == [
        "doc_binding_added",
        "file_delete_candidate",
    ]
    assert result["events"][0]["target_type"] == "node"
    assert result["events"][0]["target_id"] == "L7.1"
    assert result["events"][1]["risk_level"] == "high"
    assert result["events"][1]["payload"]["destructive_mutation_performed"] is False
    persisted = graph_events.list_events(conn, PID, snapshot["snapshot_id"])
    assert [event["evidence"]["source"] for event in persisted] == [
        "file_hygiene_batch_action_api",
        "file_hygiene_batch_action_api",
    ]


def test_graph_governance_query_trace_api_records_source_and_events(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-query-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    started = server.handle_graph_governance_query_trace_start(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "query_source": "ai_global_review",
                "query_purpose": "global_architecture_review",
                "actor": "test-ai",
                "query_budget": {"max_queries": 3},
            },
        )
    )
    trace_id = started["trace"]["trace_id"]
    assert started["trace"]["query_source"] == "ai_global_review"

    queried = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "trace_id": trace_id,
                "tool": "get_node",
                "args": {"node_id": "L7.1"},
            },
        )
    )
    assert queried["ok"] is True
    assert queried["result"]["node"]["title"] == "Feature Node"

    fetched = server.handle_graph_governance_query_trace_get(
        _ctx({"project_id": PID, "trace_id": trace_id})
    )
    assert fetched["trace"]["event_count"] == 1
    assert fetched["trace"]["events"][0]["tool"] == "get_node"

    finished = server.handle_graph_governance_query_trace_finish(
        _ctx(
            {"project_id": PID, "trace_id": trace_id},
            method="POST",
            body={"status": "complete"},
        )
    )
    assert finished["trace"]["status"] == "complete"


def test_graph_governance_queue_finalize_and_abandon_api(conn):
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old",
        commit_sha="old",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-head",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=_graph("L7.2"),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        candidate["snapshot_id"],
        nodes=_graph("L7.2")["deps_graph"]["nodes"],
        edges=[],
    )
    abandon_candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-abandon",
        commit_sha="head",
        snapshot_kind="full",
    )
    conn.commit()

    code, queued = server.handle_graph_governance_pending_scope_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"commit_sha": "head", "parent_commit_sha": "old"},
        )
    )
    assert code == 201
    assert queued["pending_scope_reconcile"]["status"] == store.PENDING_STATUS_QUEUED

    finalized = server.handle_graph_governance_snapshot_finalize(
        _ctx(
            {"project_id": PID, "snapshot_id": "scope-head"},
            method="POST",
            body={
                "target_commit_sha": "head",
                "expected_old_snapshot_id": "imported-old",
                "covered_commit_shas": ["head"],
            },
        )
    )
    assert finalized["ok"] is True
    assert finalized["activation"]["snapshot_id"] == "scope-head"
    assert finalized["pending_materialized_count"] == 1
    assert store.get_active_graph_snapshot(conn, PID)["snapshot_id"] == "scope-head"

    abandoned = server.handle_graph_governance_snapshot_abandon(
        _ctx(
            {"project_id": PID, "snapshot_id": abandon_candidate["snapshot_id"]},
            method="POST",
            body={"reason": "superseded by scope candidate"},
        )
    )
    assert abandoned["ok"] is True
    assert abandoned["status"] == store.SNAPSHOT_STATUS_ABANDONED


def test_graph_governance_semantic_feedback_and_enrich_api(conn, tmp_path):
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    docs = project / "docs" / "dev" / "proposal.md"
    docs.parent.mkdir(parents=True, exist_ok=True)
    docs.write_text("# Proposal\n", encoding="utf-8")
    tests = project / "agent" / "tests" / "test_graph_governance_api.py"
    tests.parent.mkdir(parents=True, exist_ok=True)
    tests.write_text("def test_api():\n    assert True\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    feedback = server.handle_graph_governance_snapshot_semantic_feedback(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-api"},
            method="POST",
            body={
                "actor": "observer",
                "feedback_items": {
                    "feedback_id": "fb-api-1",
                    "target_type": "node",
                    "target_id": "L7.1",
                    "issue": "Name should mention API governance.",
                },
            },
        )
    )
    assert feedback["ok"] is True
    assert feedback["feedback_count"] == 1

    enriched = server.handle_graph_governance_snapshot_semantic_enrich(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-api"},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "use_ai": False,
            },
        )
    )

    assert enriched["ok"] is True
    assert enriched["summary"]["feature_count"] == 1
    assert enriched["semantic_index"]["features"][0]["feedback_count"] == 1
    assert enriched["semantic_index"]["features"][0]["enrichment_status"] == "heuristic"
    assert Path(enriched["semantic_index_path"]).exists()


def test_graph_governance_semantic_review_queue_waits_for_ai_semantics(conn, tmp_path):
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-review-gate-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    enriched = server.handle_graph_governance_snapshot_semantic_enrich(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-review-gate-api"},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "semantic_mode": "manual",
                "use_ai": False,
                "feedback_review_mode": "auto",
            },
        )
    )

    assert enriched["ok"] is True
    assert enriched["summary"]["semantic_run_status"] == "ai_pending"
    assert enriched["summary"]["ai_complete_count"] == 0
    assert enriched["feedback_queue"]["blocked"] is True
    assert enriched["feedback_queue"]["gate"]["reason"] == "semantic_ai_not_complete"


def test_graph_governance_semantic_enrich_can_run_full_global_review_after_semantic(conn, tmp_path):
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-global-review-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    enriched = server.handle_graph_governance_snapshot_semantic_enrich(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "semantic_mode": "manual",
                "use_ai": False,
                "feedback_review_mode": "manual",
                "run_global_review_after_semantic": True,
                "run_id": "dogfood-health-picture",
            },
        )
    )

    assert enriched["ok"] is True
    assert enriched["global_review"]["ok"] is True
    assert enriched["global_review"]["run_id"] == "dogfood-health-picture"
    assert enriched["global_review"]["health_picture"]["project_health_score"] >= 0
    assert Path(enriched["global_review"]["latest_report_path"]).exists()


def test_graph_governance_status_observation_requires_explicit_backlog_action(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-status-observation-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "coverage_review",
                "summary": "missing_test_binding flag: this node has no direct test binding.",
                "type": "",
            }
        ],
    )
    item = classified["items"][0]
    assert item["feedback_kind"] == "status_observation"

    with pytest.raises(ValidationError):
        server.handle_graph_governance_snapshot_feedback_file_backlog(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={"feedback_id": item["feedback_id"], "bug_id": "OPT-STATUS-NO-AUTO"},
            )
        )

    filed = server.handle_graph_governance_snapshot_feedback_file_backlog(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": item["feedback_id"],
                "bug_id": "OPT-STATUS-USER-FILED",
                "allow_status_observation": True,
            },
        )
    )

    assert filed["bug_id"] == "OPT-STATUS-USER-FILED"
    assert filed["feedback"]["status"] == "backlog_filed"
    row = conn.execute(
        "SELECT chain_trigger_json FROM backlog_bugs WHERE bug_id=?",
        ("OPT-STATUS-USER-FILED",),
    ).fetchone()
    trigger = json.loads(row["chain_trigger_json"])
    assert trigger["feedback_kind"] == "status_observation"


def test_graph_governance_feedback_submit_creates_queue_item_and_graph_event(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-submit-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    status, submitted = server.handle_graph_governance_snapshot_feedback_submit(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            method="POST",
            body={
                "feedback_kind": "graph_correction",
                "source_node_ids": ["L7.1"],
                "target_type": "edge",
                "target_id": "L7.1->L3.1:contains",
                "issue_type": "add_relation",
                "summary": "User thinks this edge needs semantic review.",
                "actor": "dashboard-user",
            },
        )
    )

    assert status == 201
    assert submitted["feedback"]["feedback_kind"] == "graph_correction"
    assert submitted["event"]["event_type"] == "graph_correction_proposed"
    assert submitted["event"]["payload"]["feedback_id"] == submitted["feedback"]["feedback_id"]

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            query={"lane": "graph_patch_candidate"},
        )
    )
    assert queue["group_count"] == 1
    assert queue["action_catalog"]["lanes"]["graph_patch_candidate"]["primary_actions"]

    lane_queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            query={"group_by": "lane"},
        )
    )
    assert lane_queue["group_by"] == "lane"
    assert lane_queue["groups"][0]["group_by"] == "lane"
    assert lane_queue["groups"][0]["target_type"] == "feedback_lane"


def test_graph_governance_feedback_file_backlog_allows_dashboard_overrides(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-backlog-override",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    submitted_status, submitted = server.handle_graph_governance_snapshot_feedback_submit(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_kind": "project_improvement",
                "source_node_ids": ["L7.1"],
                "summary": "User wants a targeted test coverage backlog.",
                "paths": ["agent/governance/server.py"],
                "actor": "dashboard-user",
            },
        )
    )
    assert submitted_status == 201

    filed = server.handle_graph_governance_snapshot_feedback_file_backlog(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": submitted["feedback"]["feedback_id"],
                "bug_id": "OPT-FEEDBACK-OVERRIDE",
                "overrides": {
                    "title": "Dashboard edited backlog title",
                    "priority": "P1",
                    "target_files": ["agent/governance/server.py"],
                    "acceptance_criteria": ["Dashboard override is persisted."],
                },
            },
        )
    )

    assert filed["bug_id"] == "OPT-FEEDBACK-OVERRIDE"
    row = conn.execute(
        "SELECT title, priority, target_files, acceptance_criteria FROM backlog_bugs WHERE bug_id=?",
        ("OPT-FEEDBACK-OVERRIDE",),
    ).fetchone()
    assert row["title"] == "Dashboard edited backlog title"
    assert row["priority"] == "P1"
    assert json.loads(row["target_files"]) == ["agent/governance/server.py"]
    assert json.loads(row["acceptance_criteria"]) == ["Dashboard override is persisted."]


def test_graph_governance_feedback_review_use_reviewer_ai_enables_ai(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-reviewer-ai-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Doc binding should be attached to the feedback router.",
                "target": "docs/governance/reconcile-workflow.md",
                "type": "add_doc_binding",
            }
        ],
    )
    item = classified["items"][0]
    calls = []

    def fake_builder(**kwargs):
        assert kwargs["semantic_config"].model == "claude-opus-4-7"

        def fake_call(stage, payload):
            calls.append({
                "stage": stage,
                "feedback_id": payload["feedback"]["feedback_id"],
                "has_review_context": bool(payload.get("review_context")),
                "has_read_tools": bool((payload.get("review_context") or {}).get("read_tools")),
            })
            return {
                "decision": "graph_correction",
                "rationale": "AI reviewer confirms this is graph metadata only.",
                "confidence": 0.91,
                "_ai_route": {"provider": "anthropic", "model": "claude-opus-4-7"},
            }

        return fake_call

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        fake_builder,
    )

    reviewed = server.handle_graph_governance_snapshot_feedback_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "feedback_id": item["feedback_id"],
                "use_reviewer_ai": True,
                "semantic_ai_provider": "anthropic",
                "semantic_ai_model": "claude-opus-4-7",
            },
        )
    )

    assert calls == [{
        "stage": "reconcile_feedback_review",
        "feedback_id": item["feedback_id"],
        "has_review_context": True,
        "has_read_tools": True,
    }]
    reviewed_item = reviewed["items"][0]
    assert reviewed_item["reviewer_decision"] == "graph_correction"
    assert reviewed_item["reviewer_rationale"] == "AI reviewer confirms this is graph metadata only."
    assert reviewed_item["reviewer_confidence"] == 0.91


def test_graph_governance_feedback_review_queue_uses_reviewer_ai(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-reviewer-ai-queue-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the feedback router.",
                "target": "agent.governance.reconcile_feedback",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the event service.",
                "target": "agent.governance.event_service",
                "type": "add_typed_relation",
            },
        ],
    )
    assert classified["count"] == 2
    calls = []

    def fake_builder(**kwargs):
        def fake_call(stage, payload):
            calls.append({
                "stage": stage,
                "feedback_id": payload["feedback"]["feedback_id"],
                "has_review_context": bool(payload.get("review_context")),
            })
            return {
                "decision": "graph_correction",
                "rationale": "AI reviewer confirms graph-only correction.",
                "confidence": 0.88,
                "_ai_route": {"provider": "anthropic", "model": "claude-opus-4-7"},
            }

        return fake_call

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        fake_builder,
    )

    reviewed = server.handle_graph_governance_snapshot_feedback_review_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "use_reviewer_ai": True,
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "group_by": "feature",
                "limit_groups": 10,
                "max_items": 2,
                "semantic_ai_provider": "anthropic",
                "semantic_ai_model": "claude-opus-4-7",
            },
        )
    )

    assert reviewed["ok"] is True
    assert reviewed["selected_count"] == 2
    assert reviewed["reviewed_count"] == 2
    assert [call["stage"] for call in calls] == ["reconcile_feedback_review", "reconcile_feedback_review"]
    assert all(call["has_review_context"] for call in calls)
    assert {item["reviewer_decision"] for item in reviewed["reviewed"]} == {"graph_correction"}
    assert reviewed["summary"]["by_status"] == {"reviewed": 2}


def test_graph_governance_feedback_review_queue_can_require_current_semantics(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    graph = _graph("L7.1")
    graph["deps_graph"]["nodes"].append({
        "id": "L7.2",
        "layer": "L7",
        "title": "Pending Feature Node",
        "kind": "service_runtime",
        "primary": ["agent/governance/reconcile_feedback.py"],
        "secondary": [],
        "test": [],
        "metadata": {"subsystem": "governance"},
    })
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-review-current-semantics-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    conn.commit()
    state_path = reconcile_feedback.semantic_graph_state_path(PID, snapshot["snapshot_id"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({
            "node_semantics": {
                "L7.1": {
                    "status": "ai_complete",
                    "feature_hash": "hash-current",
                    "file_hashes": {"agent/governance/server.py": "a"},
                    "updated_at": "2026-05-09T00:00:00Z",
                },
                "L7.2": {
                    "status": "ai_failed",
                    "feature_hash": "hash-pending",
                    "updated_at": "2026-05-09T00:00:00Z",
                },
            }
        }),
        encoding="utf-8",
    )
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the current feature.",
                "target": "agent.governance.reconcile_feedback",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the pending feature.",
                "target": "agent.governance.server",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "coverage_gap",
                "summary": "missing doc binding on the pending feature.",
                "type": "missing_doc_binding",
            },
        ],
    )

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "group_by": "feature",
                "require_current_semantic": "true",
            },
        )
    )

    assert queue["summary"]["require_current_semantic"] is True
    assert queue["summary"]["hidden_semantic_pending_count"] == 1
    assert queue["summary"]["by_lane_all_items"]["status_only"] == 1
    assert queue["summary"]["visible_item_count"] == 1
    assert queue["groups"][0]["source_node_ids"] == ["L7.1"]
    assert queue["groups"][0]["semantic_review_ready"] is True


def test_graph_governance_feedback_review_queue_can_batch_reviewer_ai(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-reviewer-ai-batch-queue-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the feedback router.",
                "target": "agent.governance.reconcile_feedback",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the event service.",
                "target": "agent.governance.event_service",
                "type": "add_typed_relation",
            },
        ],
    )
    assert classified["count"] == 2
    calls = []

    def fake_builder(**kwargs):
        def fake_call(stage, payload):
            calls.append({
                "stage": stage,
                "count": len(payload["feedback_items"]),
                "context_count": len(payload["review_contexts"]),
            })
            return {
                "items": [
                    {
                        "feedback_id": item["feedback_id"],
                        "decision": "graph_correction",
                        "rationale": "Batch reviewer confirms graph-only correction.",
                        "confidence": 0.86,
                    }
                    for item in payload["feedback_items"]
                ],
                "_ai_route": {"provider": "anthropic", "model": "claude-opus-4-7"},
            }

        return fake_call

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        fake_builder,
    )

    reviewed = server.handle_graph_governance_snapshot_feedback_review_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "use_reviewer_ai": True,
                "batch_review": True,
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "group_by": "feature",
                "limit_groups": 10,
                "max_items": 2,
                "semantic_ai_provider": "anthropic",
                "semantic_ai_model": "claude-opus-4-7",
            },
        )
    )

    assert reviewed["ok"] is True
    assert reviewed["selected_count"] == 2
    assert reviewed["reviewed_count"] == 2
    assert calls == [{"stage": "reconcile_feedback_review_batch", "count": 2, "context_count": 2}]
    assert {item["reviewer_decision"] for item in reviewed["reviewed"]} == {"graph_correction"}
    assert reviewed["summary"]["by_status"] == {"reviewed": 2}


def test_graph_governance_feedback_retrieval_tools_are_project_scoped(conn, tmp_path):
    project = tmp_path / "project"
    source = project / "agent" / "governance" / "server.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "def feedback_router():\n    return 'graph retrieval evidence'\n",
        encoding="utf-8",
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-retrieval-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "feedback_router should be linked to graph retrieval.",
            "type": "add_relation",
        }],
    )
    item = classified["items"][0]

    result = server.handle_graph_governance_snapshot_feedback_retrieval(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "feedback_id": item["feedback_id"],
                "operations": [
                    {"tool": "graph_query", "node_ids": ["L7.1"], "depth": 1},
                    {"tool": "grep_in_scope", "pattern": "feedback_router", "node_ids": ["L7.1"]},
                    {"tool": "read_excerpt", "path": "agent/governance/server.py", "line_start": 1, "line_end": 1},
                    {"tool": "read_excerpt", "path": "../outside.txt", "line_start": 1},
                ],
            },
        )
    )

    assert result["ok"] is True
    assert result["count"] == 4
    assert result["results"][0]["result"]["nodes"][0]["id"] == "L7.1"
    assert result["results"][1]["result"]["matches"][0]["line_no"] == 1
    assert "feedback_router" in result["results"][2]["result"]["excerpt"]
    assert result["results"][3]["result"]["ok"] is False
    assert result["results"][3]["result"]["error"] == "invalid_path"


def test_graph_governance_feedback_queue_claim_lease_blocks_duplicate_workers(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-claim-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add typed relation to review queue.",
            "target": "agent.governance.reconcile_feedback",
            "type": "add_typed_relation",
        }],
    )
    assert classified["count"] == 1

    first = server.handle_graph_governance_snapshot_feedback_queue_claim(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "worker_id": "reviewer-a",
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "limit_groups": 1,
                "max_items": 1,
            },
        )
    )
    second = server.handle_graph_governance_snapshot_feedback_queue_claim(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "worker_id": "reviewer-b",
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "limit_groups": 1,
                "max_items": 1,
            },
        )
    )

    assert first["claimed_count"] == 1
    assert second["claimed_count"] == 0
    state = reconcile_feedback.load_feedback_state(PID, snapshot["snapshot_id"])
    item = next(iter(state["items"].values()))
    assert item["review_claim"]["worker_id"] == "reviewer-a"


def test_feedback_review_state_carries_forward_by_fingerprint(conn):
    base = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-base-feedback",
        commit_sha="base",
        snapshot_kind="scope",
        graph_json=_graph(),
    )
    current = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-current-feedback",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=_graph(),
    )
    conn.commit()
    issue = {
        "node_id": "L7.1",
        "reason": "dependency_patch_suggestions",
        "summary": "Add typed relation to feedback router.",
        "target": "agent.governance.reconcile_feedback",
        "type": "add_typed_relation",
    }
    base_classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        base["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[issue],
    )
    base_item = base_classified["items"][0]
    reconcile_feedback.review_feedback_item(
        PID,
        base["snapshot_id"],
        base_item["feedback_id"],
        decision="graph_correction",
        rationale="Reviewed on base snapshot.",
        confidence=0.82,
        actor="reviewer-a",
    )

    current_classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        current["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[issue],
        base_snapshot_id=base["snapshot_id"],
    )

    carried = current_classified["items"][0]
    assert current_classified["carry_forward"]["carried_forward_count"] == 1
    assert carried["status"] == "reviewed"
    assert carried["reviewer_decision"] == "graph_correction"
    assert carried["reviewer_rationale"] == "Reviewed on base snapshot."
    assert carried["carried_from_snapshot_id"] == base["snapshot_id"]


def test_graph_governance_feedback_decision_marks_user_state(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-decision",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add typed relation to the feedback router.",
            "target": "agent.governance.reconcile_feedback",
            "type": "add_typed_relation",
        }],
    )
    item = classified["items"][0]
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            query={"lane": "graph_patch_candidate"},
        )
    )
    assert queue["summary"]["raw_count"] == 1

    decided = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            method="POST",
            body={
                "feedback_id": item["feedback_id"],
                "action": "accept_graph_correction",
                "actor": "dashboard-user",
                "rationale": "User accepts graph-only correction.",
            },
        )
    )

    assert decided["ok"] is True
    assert decided["decided_count"] == 1
    decided_item = decided["items"][0]
    assert decided_item["status"] == "accepted"
    assert decided_item["final_feedback_kind"] == "graph_correction"
    assert decided_item["accepted_by"] == "dashboard-user"


def test_graph_governance_dashboard_review_bundle_exposes_two_graphs(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    graph = {
        "deps_graph": {
            "nodes": [
                {"id": "L1.1", "layer": "L1", "title": "Project", "kind": "system", "primary": []},
                {"id": "L2.1", "layer": "L2", "title": "Governance", "kind": "subsystem", "primary": []},
                {"id": "L7.1", "layer": "L7", "title": "Feedback Router", "kind": "service_runtime", "primary": ["agent/governance/reconcile_feedback.py"]},
                {"id": "L7.2", "layer": "L7", "title": "Server API", "kind": "service_runtime", "primary": ["agent/governance/server.py"]},
            ],
            "edges": [
                {"source": "L1.1", "target": "L2.1", "edge_type": "contains", "direction": "hierarchy"},
                {"source": "L2.1", "target": "L7.1", "edge_type": "contains", "direction": "hierarchy"},
                {"source": "L7.2", "target": "L7.1", "edge_type": "calls", "direction": "dependency"},
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-dashboard-review",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=[
            {"path": "agent/governance/reconcile_feedback.py", "file_kind": "source", "scan_status": "clustered", "graph_status": "mapped"},
            {"path": "docs/reconcile.md", "file_kind": "doc", "scan_status": "orphan", "graph_status": "unmapped"},
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "coverage_review",
            "summary": "missing_doc_binding flag: this node has no direct doc binding.",
        }],
    )

    bundle = server.handle_graph_governance_snapshot_dashboard_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"persist": "true", "node_limit": "20", "edge_limit": "20"},
        )
    )

    assert bundle["ok"] is True
    assert bundle["status"]["node_count"] == 4
    assert bundle["graphs"]["architecture_hierarchy"]["node_count"] == 2
    assert "graph TD" in bundle["graphs"]["architecture_hierarchy"]["mermaid"]
    assert bundle["graphs"]["feature_dependency"]["edge_count"] == 1
    assert bundle["ai_review"]["feedback_summary"]["count"] == 1
    assert Path(bundle["artifact_path"]).exists()


def test_graph_governance_status_observation_detector_classifies_graph_candidates(conn):
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Service Feature",
                    "kind": "service_runtime",
                    "primary": ["agent/service.py"],
                    "secondary": ["docs/service.md"],
                    "test": ["tests/test_service.py"],
                    "metadata": {"subsystem": "service"},
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Uncovered Feature",
                    "kind": "service_runtime",
                    "primary": ["agent/uncovered.py"],
                    "secondary": [],
                    "test": [],
                    "metadata": {"subsystem": "service"},
                },
            ],
            "edges": [],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-status-detector",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {
                "path": "agent/service.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
                "attached_node_ids": ["L7.1"],
                "mapped_node_ids": ["L7.1"],
            },
            {
                "path": "docs/service.md",
                "file_kind": "doc",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "decision": "attach_to_node",
                "attached_node_ids": ["L7.1"],
            },
            {
                "path": "tests/test_service.py",
                "file_kind": "test",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "decision": "attach_to_node",
                "attached_node_ids": ["L7.1"],
            },
            {
                "path": "docs/legacy.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
            },
        ],
        notes=json.dumps({
            "pending_scope_reconcile": {
                "scope_file_delta": {
                    "changed_files": ["agent/service.py"],
                    "impacted_files": ["agent/service.py"],
                }
            }
        }),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()

    result = server.handle_graph_governance_snapshot_feedback_status_observations(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "test_failures": [
                    {
                        "path": "tests/test_service.py",
                        "nodeid": "tests/test_service.py::test_service_contract",
                        "message": "expected old status",
                    }
                ],
            },
        )
    )

    assert result["ok"] is True
    assert result["detector"]["classified_count"] >= 5
    items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    assert {item["feedback_kind"] for item in items} == {"status_observation"}
    by_type = {item["issue_type"]: item for item in items}
    assert by_type["missing_doc_binding"]["feedback_kind"] == "status_observation"
    assert by_type["missing_test_binding"]["feedback_kind"] == "status_observation"
    assert by_type["orphan_file"]["paths"] == ["docs/legacy.md"]
    assert by_type["doc_drift_candidate"]["source_node_ids"] == ["L7.1"]
    assert by_type["stale_test_expectation_candidate"]["source_node_ids"] == ["L7.1"]
    assert by_type["failed_test_candidate"]["target_id"] == "tests/test_service.py"
    assert by_type["stale_test_expectation_candidate"]["status_observation_category"] == "stale_test_expectation"

    reviewed = server.handle_graph_governance_snapshot_feedback_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": by_type["stale_test_expectation_candidate"]["feedback_id"],
                "decision": "status_observation",
                "status_observation_category": "stale_test_expectation",
                "rationale": "Keep visible for user approval before filing backlog.",
            },
        )
    )
    assert reviewed["items"][0]["reviewed_status_observation_category"] == "stale_test_expectation"


def test_graph_governance_drift_api_records_and_lists_rows(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-drift-api",
        commit_sha="head",
        snapshot_kind="full",
    )
    conn.commit()

    code, recorded = server.handle_graph_governance_drift_record(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "commit_sha": "head",
                "path": "agent/service.py",
                "drift_type": "missing_doc",
                "target_symbol": "agent.service.create",
                "evidence": {"source": "unit-test"},
            },
        )
    )
    assert code == 201
    assert recorded["ok"] is True

    listed = server.handle_graph_governance_drift_list(
        _ctx(
            {"project_id": PID},
            query={"snapshot_id": snapshot["snapshot_id"], "drift_type": "missing_doc"},
        )
    )

    assert listed["ok"] is True
    assert listed["count"] == 1
    assert listed["drift"][0]["target_symbol"] == "agent.service.create"
    assert listed["drift"][0]["evidence"]["source"] == "unit-test"


def test_graph_governance_snapshot_files_api_reads_companion_inventory(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-files-api",
        commit_sha="head",
        snapshot_kind="full",
        file_inventory=[
            {
                "path": "agent/service.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
                "mapped_node_ids": ["L7.1"],
            },
            {
                "path": "docs/missing.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
                "mapped_node_ids": [],
            },
            {
                "path": ".coverage",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 512,
            },
            {
                "path": "dbservice/package-lock.json",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 4096,
            },
            {
                "path": "agent/.coverage",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 1024,
            },
        ],
    )
    conn.commit()

    files = server.handle_graph_governance_snapshot_files(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"scan_status": "orphan"},
        )
    )

    assert files["ok"] is True
    assert files["summary"]["by_scan_status"]["orphan"] == 1
    assert files["filtered_count"] == 1
    assert files["files"][0]["path"] == "docs/missing.md"

    cleanup = server.handle_graph_governance_snapshot_files(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"file_kind": "generated", "sort": "size_desc"},
        )
    )
    assert cleanup["sort"] == "size_desc"
    assert [item["path"] for item in cleanup["files"]] == [
        "dbservice/package-lock.json",
        "agent/.coverage",
        ".coverage",
    ]

    with pytest.raises(ValidationError, match="unsupported file inventory sort"):
        server.handle_graph_governance_snapshot_files(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                query={"sort": "unknown"},
            )
        )


def test_graph_governance_snapshot_export_cache_writes_non_authoritative_files(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-export-cache",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()

    code, result = server.handle_graph_governance_snapshot_export_cache(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"project_root": str(project)},
        )
    )

    assert code == 201
    assert result["ok"] is True
    graph_path = Path(result["graph_path"])
    manifest_path = Path(result["manifest_path"])
    assert graph_path.exists()
    assert manifest_path.exists()
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert graph["deps_graph"]["nodes"][0]["id"] == "L7.1"
    assert manifest["snapshot_id"] == snapshot["snapshot_id"]
    assert manifest["non_authoritative"] is True


def test_graph_governance_drift_file_backlog_files_bug_and_updates_drift(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-drift-backlog",
        commit_sha="head",
        snapshot_kind="full",
    )
    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="head",
        path="README.md",
        drift_type="missing_test",
        target_symbol="doc:index",
        evidence={"source": "unit-test"},
    )
    conn.commit()

    code, result = server.handle_graph_governance_drift_file_backlog(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "path": "README.md",
                "drift_type": "missing_test",
                "target_symbol": "doc:index",
                "bug_id": "GRAPH-DRIFT-UNIT-1",
                "actor": "unit-test",
            },
        )
    )

    assert code == 201
    assert result["bug_id"] == "GRAPH-DRIFT-UNIT-1"
    assert result["drift"]["status"] == "backlog_filed"
    row = conn.execute(
        "SELECT bug_id, status, target_files FROM backlog_bugs WHERE bug_id = ?",
        ("GRAPH-DRIFT-UNIT-1",),
    ).fetchone()
    assert row is not None
    assert row["status"] == "OPEN"
    assert "README.md" in row["target_files"]


def test_graph_governance_index_and_full_reconcile_api_call_helpers(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Demo\n", encoding="utf-8")

    def fake_index(conn_arg, project_id, project_root, **kwargs):
        assert conn_arg is not None
        assert project_id == PID
        assert Path(project_root) == project
        return {
            "run_id": "idx",
            "commit_sha": "head",
            "active_snapshot": {},
            "file_inventory_summary": {"total": 1},
            "symbol_index": {"symbol_count": 0},
            "doc_index": {"heading_count": 1},
            "coverage_state": {"schema_version": 1},
            "persist_summary": {"summary_path": "summary.json"},
        }

    def fake_full(conn_arg, project_id, project_root, **kwargs):
        assert conn_arg is not None
        assert project_id == PID
        assert Path(project_root) == project
        assert kwargs["semantic_enrich"] is True
        assert kwargs["semantic_use_ai"] is None
        return {
            "ok": True,
            "snapshot_id": "full-head",
            "commit_sha": kwargs["commit_sha"],
            "graph_stats": {"nodes": 1, "edges": 0},
            "semantic_enrichment": {"feature_count": 1},
        }

    def fake_backfill(conn_arg, project_id, project_root, **kwargs):
        assert conn_arg is not None
        assert project_id == PID
        assert Path(project_root) == project
        assert kwargs["reason"] == "scope blocked"
        return {
            "ok": True,
            "snapshot_id": "full-head-escape",
            "pending_scope_waiver": {"waived_count": 2},
        }

    monkeypatch.setattr(
        "agent.governance.governance_index.build_and_persist_governance_index",
        fake_index,
    )
    monkeypatch.setattr(
        "agent.governance.state_reconcile.run_state_only_full_reconcile",
        fake_full,
    )
    monkeypatch.setattr(
        "agent.governance.state_reconcile.run_backfill_escape_hatch",
        fake_backfill,
    )

    index = server.handle_graph_governance_index_build(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"project_root": str(project), "run_id": "idx"},
        )
    )
    assert index["ok"] is True
    assert index["doc_heading_count"] == 1
    assert index["persist_summary"]["summary_path"] == "summary.json"

    code, full = server.handle_graph_governance_full_reconcile(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"project_root": str(project), "commit_sha": "head"},
        )
    )
    assert code == 201
    assert full["ok"] is True
    assert full["snapshot_id"] == "full-head"

    code2, backfill = server.handle_graph_governance_backfill_escape(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"project_root": str(project), "reason": "scope blocked"},
        )
    )
    assert code2 == 201
    assert backfill["ok"] is True
    assert backfill["pending_scope_waiver"]["waived_count"] == 2


def test_graph_governance_backfill_input_errors_are_validation_errors(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()

    def fake_backfill(*_args, **_kwargs):
        raise ValueError("target_commit_sha must equal HEAD")

    monkeypatch.setattr(
        "agent.governance.state_reconcile.run_backfill_escape_hatch",
        fake_backfill,
    )

    from agent.governance.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        server.handle_graph_governance_backfill_escape(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={"project_root": str(project), "target_commit_sha": "not-head"},
            )
        )
    assert "target_commit_sha must equal HEAD" in str(exc.value)


def test_semantic_projection_uses_indexed_hash_metadata(conn):
    graph = _graph("L7.1")
    merge = merge_feature_hashes_into_graph_nodes(
        graph,
        {
            "feature_index": {
                "features": [
                    {
                        "node_id": "L7.1",
                        "feature_hash": "sha256:indexed-feature",
                        "file_hashes": {"agent/governance/server.py": "sha256:file-a"},
                    }
                ]
            }
        },
    )
    assert merge["nodes_updated"] == 1
    node = graph["deps_graph"]["nodes"][0]
    assert node["metadata"]["feature_hash"] == "sha256:indexed-feature"
    assert node["metadata"]["hash_scheme"] == "indexed_sha256"

    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-hash-current",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="semantic_node_enriched",
        event_kind="semantic",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_OBSERVED,
        baseline_commit="old",
        target_commit="old",
        stable_node_key=graph_events.stable_node_key_for_node(node),
        feature_hash="sha256:old-indexed-feature",
        file_hashes={"agent/governance/server.py": "sha256:file-a"},
        payload={"semantic_payload": {"summary": "ok", "open_issues": []}},
        created_by="test",
    )
    conn.commit()

    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )
    health = projection["health"]
    assert health["semantic_current_count"] == 1
    assert health["semantic_unverified_hash_count"] == 0
    validity = projection["projection"]["node_semantics"]["L7.1"]["validity"]
    assert validity["status"] == "semantic_carried_forward_current"
    assert validity["current_hash_scheme"] == "indexed_sha256"
    assert validity["feature_hash_match"] is False
    assert validity["hash_validation"] == "file_hash_matched"


def test_operations_queue_unifies_jobs_and_edge_not_queued(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="ops-active",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash,
           file_hashes_json, updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID,
            snapshot["snapshot_id"],
            "L7.1",
            "ai_pending",
            "sha256:indexed-feature",
            json.dumps({"agent/governance/server.py": "sha256:file-a"}),
            "2026-05-10T00:00:00Z",
            "2026-05-10T00:00:00Z",
        ),
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(
        _ctx({"project_id": PID}, query={"require_current_semantic": "true"})
    )

    assert queue["ok"] is True
    assert queue["snapshot_id"] == "ops-active"
    assert queue["summary"]["node_semantic_jobs"]["by_status"] == {"ai_pending": 1}
    assert queue["summary"]["feedback_queue"]["visible_item_count"] == 0
    operations = {row["operation_id"]: row for row in queue["operations"]}
    assert operations["node-semantic:L7.1"]["status"] == "ai_pending"
    edge_row = operations["edge-semantic:not-queued"]
    assert edge_row["status"] == "not_queued"
    assert edge_row["progress"] == {"done": 0, "total": 1}
    assert "1 edge semantics missing, 0 queued" == edge_row["last_result"]
    assert "queue_edge_semantics" in edge_row["supported_actions"]
    assert "run_edge_semantics" in edge_row["supported_actions"]


def test_operations_queue_includes_pending_scope_reconcile(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-active",
        commit_sha="old",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    assert queue["summary"]["pending_scope_reconcile_count"] == 1
    assert queue["summary"]["by_type"]["scope_reconcile"] == 1
    row = next(item for item in queue["operations"] if item["operation_type"] == "scope_reconcile")
    assert row["target_id"] == "head"
    assert row["status"] == "queued"


def test_operations_queue_synthesizes_stale_scope_reconcile(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "head-commit")
    monkeypatch.setattr(
        server,
        "_git_changed_paths_between",
        lambda _root, _base, _head, limit=25: ["docs/governance/manual-fix-sop.md"],
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-stale-active",
        commit_sha="old-commit",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    row = next(item for item in queue["operations"] if item["operation_id"] == "scope-reconcile:stale:head-commit")
    assert row["operation_type"] == "scope_reconcile"
    assert row["status"] == "not_queued"
    assert row["target_id"] == "head-commit"
    assert row["active_graph_commit"] == "old-commit"
    assert row["changed_files"] == ["docs/governance/manual-fix-sop.md"]
    assert queue["summary"]["graph_stale"]["is_stale"] is True
    assert queue["summary"]["graph_stale"]["changed_file_count"] == 1
