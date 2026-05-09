from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.errors import ValidationError


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
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
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
