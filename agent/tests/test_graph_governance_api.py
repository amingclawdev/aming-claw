from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.ai_invocation import RoutePromptContract
from agent.tests.fixtures.parallel_project import create_parallel_fixture_project
from agent.tests.fixtures.rule_fingerprint_project import (
    create_rule_fingerprint_git_fixture_project,
    rule_fingerprint_mismatch_pair,
)
from agent.governance import asset_impact
from agent.governance import batch_jobs
from agent.governance import graph_correction_patches
from agent.governance import graph_events
from agent.governance import graph_query_trace
from agent.governance import observer_session
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import reconcile_semantic_enrichment as semantic_enrichment
from agent.governance import server
from agent.governance import state_reconcile
from agent.governance import task_timeline
from agent.governance.db import _ensure_schema
from agent.governance.errors import GovernanceError, PermissionDeniedError, ValidationError
from agent.governance.governance_index import merge_feature_hashes_into_graph_nodes
from agent.governance.mf_subagent_contract import (
    MfSubagentContractError,
    validate_mf_subagent_finish_gate,
)
from agent.observer_runtime import (
    ObserverRuntimeTextPrepareRequest,
    build_observer_runtime_text_context,
)
from agent.governance.parallel_branch_runtime import (
    BATCH_STATE_OPEN,
    BranchRuntimeFenceError,
    BranchTaskRuntimeContext,
    BatchMergeItem,
    BatchMergeRuntime,
    MergeQueueItem,
    STATE_MERGE_FAILED,
    STATE_WORKTREE_READY,
    append_branch_contract_revision,
    get_branch_context,
    get_latest_branch_contract_revision,
    mf_subagent_session_token_hash,
    runtime_context_id_for_branch_context,
    upsert_batch_merge_runtime,
    upsert_branch_context,
    upsert_merge_queue_items,
)


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


def _ctx_with_role(
    path_params: dict,
    role: str,
    *,
    method: str = "GET",
    query: dict | None = None,
    body: dict | None = None,
):
    ctx = _ctx(path_params, method=method, query=query, body=body)
    ctx._session = {
        "session_id": f"ses-{role}",
        "principal_id": f"{role}-principal",
        "project_id": path_params.get("project_id", PID),
        "role": role,
        "scope": [],
    }
    return ctx


def _mf_sub_run_id(task_id: str, fence_token: str) -> str:
    fence_hash = hashlib.sha256(fence_token.encode("utf-8")).hexdigest()[:16]
    return f"mf_subagent:{task_id}:fence:{fence_hash}"


def _insert_mf_sub_graph_query_trace(
    conn,
    *,
    trace_id: str,
    parent_task_id: str,
    snapshot_id: str = "scope-test",
    runtime_context_id: str = "",
    task_id: str = "",
    worker_role: str = "",
    fence_token: str = "",
    run_id: str = "",
    created_at: str = "2026-06-06T10:00:00Z",
) -> None:
    graph_query_trace.ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_query_traces
          (trace_id, project_id, snapshot_id, actor, query_source, query_purpose,
           run_id, parent_task_id, runtime_context_id, task_id, worker_role,
           fence_token, status, budget_json, usage_json, artifact_path,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            PID,
            snapshot_id,
            "mcp",
            "mf_subagent",
            "subagent_context_build",
            run_id,
            parent_task_id,
            runtime_context_id,
            task_id,
            worker_role,
            fence_token,
            "complete",
            "{}",
            "{}",
            "",
            created_at,
            created_at,
        ),
    )


def _route_waiver(action: str, *, task_id: str = "", backlog_id: str = "") -> dict:
    waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "allowed_action": action,
        "project_id": PID,
        "route_context_hash": f"sha256:test-route-context-{action}",
        "prompt_contract_id": f"prompt-contract-{action}",
        "caller_role": "observer",
        "reason": "Unit test supplies explicit route gate waiver evidence.",
        "timeline_evidence": {"event_id": f"test-route-gate-{action}"},
    }
    if task_id:
        waiver["task_id"] = task_id
    if backlog_id:
        waiver["backlog_id"] = backlog_id
    return waiver


def _route_token(
    action: str,
    *,
    project_id: str = PID,
    task_id: str = "",
    backlog_id: str = "",
) -> dict:
    scope = {"project_id": project_id}
    if task_id:
        scope["task_id"] = task_id
    if backlog_id:
        scope["backlog_id"] = backlog_id
    return {
        "route_context_hash": f"sha256:test-route-context-{action}",
        "prompt_contract_id": f"prompt-contract-{action}",
        "prompt_contract_hash": f"sha256:test-prompt-contract-{action}",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": scope,
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": [f"timeline:test-route-token-{action}"],
    }


def _server_issued_route_token(
    conn,
    action: str,
    *,
    project_id: str = PID,
    task_id: str = "route-token-test-task",
    backlog_id: str = "route-token-test-backlog",
) -> dict:
    from agent.governance import observer_route_context

    issued = observer_route_context.issue_observer_write_route_context(
        project_id=project_id,
        backlog_id=backlog_id,
        task_id=task_id,
        target_files=["agent/governance/server.py"],
        allowed_actions=[action],
        evidence_refs=[f"timeline:test-route-token-{action}"],
    )
    observer_route_context.persist_route_token_ref(
        conn,
        project_id=project_id,
        route_token_ref=issued["route_token_ref"],
        token=issued["route_token"],
    )
    return issued["route_token"]


def _finish_gate_evidence(
    *,
    fence_token: str,
    worktree_path: str,
    branch_ref: str,
    head_commit: str,
    nested_key: str = "startup_evidence",
) -> dict:
    return {
        nested_key: {
            "schema_version": "mf_subagent_startup_gate.v1",
            "gate_kind": "mf_subagent.startup",
            "status": "passed",
            "ok": True,
            "allowed": True,
            "bounded": True,
            "started": True,
            "startup_complete": True,
            "actual_startup_recorded": True,
            "worker_role": "mf_sub",
            "worker_id": "codex-subagent-api",
            "fence_token": fence_token,
            "actual_cwd": worktree_path,
            "actual_git_root": worktree_path,
            "worktree_path": worktree_path,
            "branch_ref": branch_ref,
            "head_commit": head_commit,
            "route_token_ref": f"rtok-{fence_token}",
            "observer_command_id": f"cmd-{fence_token}",
            "read_receipt_event_id": f"rr-{fence_token}",
            "worker_session_id": f"session-{fence_token}",
            "filer_principal": f"session-{fence_token}",
            "worker_transcript_path": f"/tmp/transcript-{fence_token}.jsonl",
            "harness_type": "codex",
            "worker_self_attesting": True,
            "self_attesting": True,
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "status": "passed",
                "worker_self_attesting": True,
                "worker_session_id": f"session-{fence_token}",
                "worker_transcript_path": f"/tmp/transcript-{fence_token}.jsonl",
                "harness_type": "codex",
                "blockers": [],
            },
        },
        "read_receipt_hash": f"sha256:read-{fence_token}",
        "read_receipt_event_id": f"rr-{fence_token}",
    }


def _record_finish_startup_event(
    conn,
    *,
    task_id: str,
    backlog_id: str,
    fence_token: str,
    worktree_path: str,
    branch_ref: str,
    head_commit: str,
    nested_key: str = "startup_evidence",
) -> dict:
    startup_gate = _finish_gate_evidence(
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
        head_commit=head_commit,
        nested_key=nested_key,
    )[nested_key]
    task_timeline.ensure_schema(conn)
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="mf_subagent.startup",
        event_kind="mf_subagent_startup",
        phase="startup_gate",
        status="passed",
        actor=str(startup_gate.get("worker_session_id") or "mf_sub"),
        payload={"mf_subagent_startup_gate": startup_gate},
    )
    conn.commit()
    return startup_gate


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


def _git_repo(tmp_path):
    return create_parallel_fixture_project(tmp_path).root


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _NoCloseConn(c))
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


def _write_dashboard_dist(root: Path, asset_name: str) -> Path:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        f'<script type="module" src="/dashboard/assets/{asset_name}"></script>',
        encoding="utf-8",
    )
    (root / "assets" / asset_name).write_text("console.log('dashboard');", encoding="utf-8")
    return root


def test_dashboard_dist_dir_installed_runtime_prefers_packaged_over_stale_repo_dist(
    tmp_path, monkeypatch
):
    stale_repo_dist = _write_dashboard_dist(
        tmp_path / "runtime-checkout" / "frontend" / "dashboard" / "dist",
        "index-stale.js",
    )
    packaged_dist = _write_dashboard_dist(
        tmp_path / "runtime-checkout" / "agent" / "governance" / "dashboard_dist",
        "index-current.js",
    )
    monkeypatch.delenv("GOVERNANCE_DASHBOARD_DIST", raising=False)
    monkeypatch.setattr(server, "_repo_dashboard_dist_dir", lambda: stale_repo_dist)
    monkeypatch.setattr(server, "_packaged_dashboard_dist_dir", lambda: packaged_dist)

    assert server._dashboard_dist_dir() == packaged_dist


def test_dashboard_dist_dir_explicit_override_wins_over_packaged_and_repo_dist(
    tmp_path, monkeypatch
):
    stale_repo_dist = _write_dashboard_dist(
        tmp_path / "runtime-checkout" / "frontend" / "dashboard" / "dist",
        "index-stale.js",
    )
    packaged_dist = _write_dashboard_dist(
        tmp_path / "runtime-checkout" / "agent" / "governance" / "dashboard_dist",
        "index-current.js",
    )
    override_dist = _write_dashboard_dist(tmp_path / "override-dist", "index-override.js")
    monkeypatch.setenv("GOVERNANCE_DASHBOARD_DIST", str(override_dist))
    monkeypatch.setattr(server, "_repo_dashboard_dist_dir", lambda: stale_repo_dist)
    monkeypatch.setattr(server, "_packaged_dashboard_dist_dir", lambda: packaged_dist)

    assert server._dashboard_dist_dir() == override_dist.resolve()


def test_project_bootstrap_first_run_mints_server_binding_and_records_gate(conn, monkeypatch):
    observed = {}

    def fake_bootstrap_project(**kwargs):
        observed.update(kwargs)
        return {
            "project_id": "bootstrap-demo",
            "graph_stats": {"node_count": 1, "edge_count": 0, "layers": {}},
        }

    monkeypatch.setattr(server.project_service, "project_exists", lambda _project_id: False)
    monkeypatch.setattr(server.project_service, "bootstrap_project", fake_bootstrap_project)

    status, payload = server.handle_project_bootstrap(
        _ctx(
            {},
            method="POST",
            body={
                "workspace_path": "/tmp/bootstrap-demo",
                "project_id": "bootstrap-demo",
                "language": "python",
            },
        )
    )

    assert status == 200
    assert observed["workspace_path"] == "/tmp/bootstrap-demo"
    assert observed["config_override"]["project_id"] == "bootstrap-demo"
    assert observed["config_override"]["language"] == "python"
    gate = payload["route_token_gate"]
    assert gate["decision"] == "route_token"
    assert gate["server_minted"] is True
    assert gate["bootstrap_gate_decision"] == "server_minted_first_run_binding"
    assert gate["server_issued_binding"] is True
    assert gate["binding_source"] == "observer_route_token_refs"
    assert gate["scope"]["project_id"] == "bootstrap-demo"
    assert gate["first_run_bootstrap"]["raw_route_token_persisted"] is False
    assert payload["route_bootstrap_handoff"]["first_run"] is True

    ref_row = conn.execute(
        "SELECT route_token_ref, status FROM observer_route_token_refs WHERE project_id = ?",
        ("bootstrap-demo",),
    ).fetchone()
    assert ref_row is not None
    assert ref_row["route_token_ref"] == gate["route_token_ref"]
    assert ref_row["status"] == "active"

    event = conn.execute(
        "SELECT event_type, project_id, payload_json FROM task_timeline_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "route_token_gate.project_bootstrap"
    assert event["project_id"] == "bootstrap-demo"
    event_payload = json.loads(event["payload_json"])
    assert event_payload["route_token_gate"]["first_run_bootstrap"]["project_id"] == "bootstrap-demo"


def test_project_bootstrap_tokenless_non_first_run_rejected(conn, monkeypatch):
    def fail_bootstrap(**_kwargs):
        raise AssertionError("bootstrap_project must not run without route gate evidence")

    monkeypatch.setattr(server.project_service, "project_exists", lambda _project_id: True)
    monkeypatch.setattr(server.project_service, "bootstrap_project", fail_bootstrap)

    for _ in range(2):
        with pytest.raises(GovernanceError, match="route_token") as raised:
            server.handle_project_bootstrap(
                _ctx(
                    {},
                    method="POST",
                    body={
                        "workspace_path": "/tmp/bootstrap-demo",
                        "project_id": "bootstrap-demo",
                    },
                )
            )
        assert raised.value.code == "route_token_required"

    rows = conn.execute(
        """
        SELECT event_type, event_kind, decision, status, payload_json, verification_json
        FROM task_timeline_events
        WHERE project_id = ? AND event_type = ?
        """,
        ("bootstrap-demo", "route_token_gate.project_bootstrap_refusal"),
    ).fetchall()
    assert len(rows) == 1
    event = rows[0]
    assert event["event_kind"] == "refusal"
    assert event["decision"] == "route_token_required"
    assert event["status"] == "rejected"
    event_payload = json.loads(event["payload_json"])
    refusal = event_payload["route_token_gate_refusal"]
    assert refusal["gate_decision"] == "route_token_required"
    assert refusal["fault_domain"] == "caller_missing_route_evidence"
    assert refusal["details"]["fault_domain"] == "caller_missing_route_evidence"
    verification = json.loads(event["verification_json"])
    assert verification["gate_decision"] == "route_token_required"
    assert verification["fault_domain"] == "caller_missing_route_evidence"


def test_project_bootstrap_route_waiver_allows_and_records_gate(conn, monkeypatch):
    observed = {}

    def fake_bootstrap_project(**kwargs):
        observed.update(kwargs)
        return {
            "project_id": "bootstrap-demo",
            "graph_stats": {"node_count": 1, "edge_count": 0, "layers": {}},
        }

    monkeypatch.setattr(server.project_service, "bootstrap_project", fake_bootstrap_project)

    status, payload = server.handle_project_bootstrap(
        _ctx(
            {},
            method="POST",
            body={
                "workspace_path": "/tmp/bootstrap-demo",
                "project_id": "bootstrap-demo",
                "route_waiver": {
                    **_route_waiver("project_bootstrap"),
                    "project_id": "bootstrap-demo",
                },
            },
        )
    )

    assert status == 200
    assert observed["workspace_path"] == "/tmp/bootstrap-demo"
    assert payload["route_token_gate"]["decision"] == "route_waiver"
    event = conn.execute(
        "SELECT event_type, project_id FROM task_timeline_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "route_token_gate.project_bootstrap"
    assert event["project_id"] == "bootstrap-demo"


def test_project_bootstrap_route_token_allows_project_scope(conn, monkeypatch):
    monkeypatch.setattr(
        server.project_service,
        "bootstrap_project",
        lambda **_kwargs: {"project_id": "bootstrap-token-demo", "graph_stats": {}},
    )

    status, payload = server.handle_project_bootstrap(
        _ctx(
            {},
            method="POST",
            body={
                "workspace_path": "/tmp/bootstrap-token-demo",
                "project_id": "bootstrap-token-demo",
                "route_token": _server_issued_route_token(
                    conn,
                    "project_bootstrap",
                    project_id="bootstrap-token-demo",
                ),
            },
        )
    )

    assert status == 200
    assert payload["route_token_gate"]["decision"] == "route_token"
    assert payload["route_token_gate"]["scope"]["project_id"] == "bootstrap-token-demo"


def test_bootstrap_project_persists_top_level_exclude_patterns(
    conn,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "exclude-demo"
    workspace.mkdir()
    captured = {}

    def fake_reconcile(_conn, pid, ws, **kwargs):
        captured.update({"project_id": pid, "workspace": ws, **kwargs})
        return {
            "ok": True,
            "snapshot_id": "snap-exclude-demo",
            "activation": {"projection_status": "current"},
            "graph_stats": {"node_count": 0, "edge_count": 0, "layers": {}},
            "index_counts": {"nodes": 0, "edges": 0},
        }

    monkeypatch.setattr(server.project_service, "_governance_root", lambda: tmp_path / "state")
    monkeypatch.setattr(server.project_service, "get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        server.project_service,
        "_ensure_clean_git_worktree_for_graph",
        lambda _workspace: {"is_git_repo": False, "dirty": False},
    )
    monkeypatch.setattr(state_reconcile, "run_state_only_full_reconcile", fake_reconcile)

    result = server.project_service.bootstrap_project(
        workspace_path=str(workspace),
        project_name="Exclude Demo",
        config_override={
            "project_id": "exclude-demo",
            "graph": {"exclude_paths": ["dist"]},
        },
        exclude_patterns=["node_modules"],
    )

    persisted = server.project_service.get_project_config_metadata("exclude-demo")
    assert persisted["graph"]["exclude_paths"] == ["dist", "node_modules"]
    assert persisted["graph"]["effective_exclude_roots"] == ["dist", "node_modules"]
    assert result["config"]["graph"]["exclude_paths"] == ["dist", "node_modules"]
    assert result["config"]["graph"]["effective_exclude_roots"] == ["dist", "node_modules"]
    assert result["effective_exclude_roots"] == ["dist", "node_modules"]
    assert captured["graph_exclude_paths"] == ["dist", "node_modules"]


def _activate_basic_graph(
    conn,
    snapshot_id: str = "full-query-test",
    *,
    project_id: str = PID,
) -> None:
    snapshot = store.create_graph_snapshot(
        conn,
        project_id,
        snapshot_id=snapshot_id,
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        project_id,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, project_id, snapshot["snapshot_id"])
    conn.commit()


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


def test_graph_governance_asset_impact_reminders_api_lists_events_and_resolves(conn):
    first = asset_impact.record_asset_impact_detected(
        conn,
        project_id=PID,
        asset_kind="doc",
        asset_path="docs/dev/proposal.md",
        node_id="L7.1",
        node_title="Feature Node",
        commit_sha="c1",
        snapshot_id="s1",
        actor="scope-reconcile",
    )
    second = asset_impact.record_asset_impact_detected(
        conn,
        project_id=PID,
        asset_kind="doc",
        asset_path="docs/dev/proposal.md",
        node_id="L7.1",
        node_title="Feature Node",
        commit_sha="c2",
        snapshot_id="s2",
        actor="scope-reconcile",
    )
    conn.commit()

    queue = server.handle_graph_governance_asset_impact_reminders(
        _ctx(
            {"project_id": PID},
            query={"asset_kind": "doc", "status": "pending"},
        )
    )

    assert queue["ok"] is True
    assert queue["count"] == 1
    assert queue["summary"]["open_event_count"] == 2
    assert "waived" in queue["action_catalog"]["resolution_kinds"]
    reminder = queue["reminders"][0]
    assert reminder["open_event_ids"] == [
        first["event"]["id"],
        second["event"]["id"],
    ]

    history = server.handle_graph_governance_asset_impact_reminder_events(
        _ctx({"project_id": PID, "reminder_id": reminder["reminder_id"]})
    )
    assert history["ok"] is True
    assert history["reminder"]["reminder_id"] == reminder["reminder_id"]
    assert [event["event_type"] for event in history["events"]] == [
        asset_impact.EVENT_IMPACT_DETECTED,
        asset_impact.EVENT_IMPACT_DETECTED,
    ]

    resolved = server.handle_graph_governance_asset_impact_reminder_resolve(
        _ctx(
            {"project_id": PID, "reminder_id": reminder["reminder_id"]},
            method="POST",
            body={
                "resolution_kind": "keep_unchanged",
                "note": "Reviewed in Review Queue.",
                "actor": "dashboard-user",
            },
        )
    )

    assert resolved["ok"] is True
    assert resolved["resolution"]["covers_event_ids"] == reminder["open_event_ids"]
    assert resolved["reminder"]["status"] == asset_impact.STATUS_RECORDED
    resolution_events = [
        event for event in resolved["events"]
        if event["event_type"] == asset_impact.EVENT_RESOLUTION_RECORDED
    ]
    assert len(resolution_events) == 1
    assert resolution_events[0]["actor"] == "dashboard-user"
    assert resolution_events[0]["evidence"]["note"] == "Reviewed in Review Queue."

    resolved_history = server.handle_graph_governance_asset_impact_reminder_events(
        _ctx({"project_id": PID, "reminder_id": reminder["reminder_id"]})
    )
    assert resolved_history["reminder"]["status"] == asset_impact.STATUS_RECORDED
    assert [
        event["event_type"] for event in resolved_history["events"]
    ] == [
        asset_impact.EVENT_IMPACT_DETECTED,
        asset_impact.EVENT_IMPACT_DETECTED,
        asset_impact.EVENT_RESOLUTION_RECORDED,
    ]

    after = server.handle_graph_governance_asset_impact_reminders(
        _ctx(
            {"project_id": PID},
            query={"asset_kind": "doc", "status": "pending"},
        )
    )
    assert after["count"] == 0


def test_graph_governance_asset_inbox_relation_drift_and_proposal_contract(conn):
    graph = _graph("L7.runtime")
    graph["deps_graph"]["nodes"][0]["title"] = "Runtime Service"
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="asset-inbox-drift",
        commit_sha="c-drift",
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {
                "path": "docs/runtime.md",
                "file_kind": "doc",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "attached_node_ids": ["L7.runtime"],
                "mapped_node_ids": ["L7.runtime"],
                "file_hash": "sha256:doc",
            },
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "file_hash": "sha256:orphan",
            },
            {
                "path": "config/runtime.yaml",
                "file_kind": "config",
                "scan_status": "pending_decision",
                "graph_status": "pending_decision",
                "candidate_node_id": "L7.runtime",
                "file_hash": "sha256:config",
            },
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    asset_impact.record_asset_impact_detected(
        conn,
        project_id=PID,
        asset_kind="doc",
        asset_path="docs/runtime.md",
        node_id="L7.runtime",
        node_title="Runtime Service",
        commit_sha="c-drift",
        snapshot_id=snapshot["snapshot_id"],
        actor="scope-reconcile",
    )
    conn.commit()

    inbox = server.handle_graph_governance_snapshot_asset_inbox(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    by_path = {item["path"]: item for item in inbox["items"]}
    assert inbox["ok"] is True
    runtime_doc = by_path["docs/runtime.md"]
    assert runtime_doc["asset_status"] == "impact_pending"
    assert runtime_doc["drift"]["state"] == "suspected"
    assert runtime_doc["drift"]["source"] == "asset_impact_reminder"
    assert runtime_doc["mount_relations"][0]["status"] == "impact_pending"
    assert runtime_doc["mount_relations"][0]["impact_reminder_id"]
    assert "resolve_drift" in runtime_doc["recommended_actions"]

    orphan = by_path["docs/orphan.md"]
    assert orphan["asset_status"] == "doc_unbound"
    assert orphan["mount_relations"][0]["status"] == "unbound"

    config = by_path["config/runtime.yaml"]
    assert config["asset_status"] == "config_pending_decision"
    assert config["mount_relations"][0]["status"] == "candidate"
    assert "write_governance_hint" not in config["batch_eligible_actions"]


def test_backlog_close_response_includes_asset_drift_summary_for_changed_orphan_doc(conn, monkeypatch):
    graph = _graph("L7.external_protocol")
    graph["deps_graph"]["nodes"][0]["title"] = "External Protocol MCP"
    graph["deps_graph"]["nodes"][0]["primary"] = ["scripts/external_protocol_mcp.py"]
    graph["deps_graph"]["nodes"][0]["secondary"] = []
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="close-impact-external-protocol",
        commit_sha="c-ext-base",
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {
                "path": "scripts/external_protocol_mcp.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "attached_node_ids": ["L7.external_protocol"],
                "mapped_node_ids": ["L7.external_protocol"],
            },
            {
                "path": "skills/external-protocol/SKILL.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "file_hash": "sha256:skill",
            },
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    store.activate_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="test",
        auto_rebuild_projection=False,
    )
    conn.execute(
        """INSERT INTO backlog_bugs
           (bug_id, title, status, mf_type, bypass_policy_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "BUG-CLOSE-ASSET",
            "Close gate asset summary",
            "MF_IN_PROGRESS",
            "chain_rescue",
            '{"mf_type":"chain_rescue"}',
            "now",
            "now",
        ),
    )
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    close_timeline = _record_close_timeline(
        conn,
        backlog_id="BUG-CLOSE-ASSET",
        task_id="close-asset-task",
        suffix="close-asset",
        same_owner_startup=True,
    )

    result = server.handle_backlog_close(
        _ctx(
            {"project_id": PID, "bug_id": "BUG-CLOSE-ASSET"},
            method="POST",
            body={
                "commit": "c-close",
                "actor": "test",
                "route_waiver": {
                    "accepted": True,
                    "waiver_type": "manual_fix",
                    "allowed_action": "backlog_close",
                    "project_id": PID,
                    "backlog_id": "BUG-CLOSE-ASSET",
                    "caller_role": "observer",
                    "reason": "Unit test supplies explicit route gate waiver evidence.",
                    "timeline_evidence": {"event_id": "test-route-gate"},
                    **close_timeline["route_identity"],
                },
                "changed_files": [
                    "scripts/external_protocol_mcp.py",
                    "skills/external-protocol/SKILL.md",
                ],
            },
        )
    )

    check = result["close_impact_check"]
    assert result["ok"] is True
    assert check["changed_file_count"] == 2
    assert check["impacted_node_count"] == 1
    assert check["changed_untrusted_asset_counts_by_kind"]["doc"] == 1
    assert check["coverage_claim_allowed_by_kind"]["doc"] is False
    assert check["changed_untrusted_assets_sample"][0]["path"] == "skills/external-protocol/SKILL.md"
    assert "not trusted impact-scope coverage" in " ".join(check["required_actions"])


def test_backlog_list_compact_includes_observer_command_terminal_projection(conn):
    observer_session.ensure_schema(conn)
    backlog_id = "AC-OBSERVER-COMMAND-TERMINAL-PROJECTION-FROM-CONTRACT-20260604"
    conn.execute(
        """INSERT INTO backlog_bugs
           (bug_id, title, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            backlog_id,
            "Project command terminal status",
            "FIXED",
            "2026-06-04T00:00:00Z",
            "2026-06-04T00:00:00Z",
        ),
    )
    terminal_projection = {
        "schema_version": "observer_command_terminal_projection.v1",
        "source_of_truth": "Contract/Revision/Event",
        "passed": True,
        "canonical_contract_state": "closed",
        "command_projection_status": "completed",
        "divergence_reason": "superseded_route_identity_reconciled",
        "canonical_route_identity": {"route_id": "route-repair-e97d980211e2dc1c"},
        "superseded_route_identity": {"route_id": "route-repair-01c5a0404ba10777"},
        "terminal_evidence_refs": [{"request_id": "req-97cd668efd14"}],
    }
    conn.execute(
        """INSERT INTO observer_command_queue (
               command_id, project_id, command_type, payload_json, status,
               target_session_id, claimed_by_session_id, created_by, created_at,
               notified_at, claimed_at, completed_at, result_json, error
           ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, '')""",
        (
            "cmd-d0e3e3bf7893",
            PID,
            observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
            observer_session._json_dumps({
                "backlog_id": backlog_id,
                "route_id": "route-repair-01c5a0404ba10777",
            }),
            observer_session.COMMAND_STATUS_COMPLETED,
            "observer",
            "2026-06-04T00:00:00Z",
            "2026-06-04T00:00:01Z",
            "2026-06-04T00:00:02Z",
            "2026-06-04T00:00:03Z",
            observer_session._json_dumps({
                "ok": True,
                "terminal_contract_projection": terminal_projection,
            }),
        ),
    )
    conn.commit()

    result = server.handle_backlog_list(
        _ctx({"project_id": PID}, query={"view": "compact", "include_closed": "true"})
    )

    bug = result["bugs"][0]
    projection = bug["observer_command_projection"]
    assert projection["source_of_truth"] == "Contract/Revision/Event"
    assert projection["command_id"] == "cmd-d0e3e3bf7893"
    assert projection["command_projection_status"] == "completed"
    assert projection["divergence_reason"] == "superseded_route_identity_reconciled"
    assert projection["canonical_route_identity"]["route_id"] == "route-repair-e97d980211e2dc1c"
    assert projection["superseded_route_identity"]["route_id"] == "route-repair-01c5a0404ba10777"


def test_graph_governance_asset_drift_state_and_proposal_api_are_auditable(conn):
    code, recorded = server.handle_graph_governance_asset_drift_state_record(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "asset_kind": "doc",
                "asset_path": "docs/runtime.md",
                "snapshot_id": "asset-inbox-drift",
                "commit_sha": "c-drift",
                "drift_state": "confirmed",
                "actor": "unit-test",
                "evidence": {"source": "fixture"},
            },
        )
    )
    assert code == 201
    assert recorded["drift_state"]["drift_state"] == "confirmed"
    assert recorded["drift_state"]["evidence"]["source"] == "fixture"

    code, proposal = server.handle_graph_governance_asset_drift_proposal_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "asset_kind": "doc",
                "asset_path": "docs/runtime.md",
                "snapshot_id": "asset-inbox-drift",
                "commit_sha": "c-drift",
                "node_id": "L7.runtime",
                "actor": "unit-test",
            },
        )
    )
    assert code == 201
    assert proposal["proposal"]["status"] == "blocked"
    assert proposal["proposal"]["self_precheck"]["ok"] is False
    assert proposal["proposal"]["self_precheck"]["required_gate"] == "local_precheck_before_review_queue"


def test_graph_structure_hint_projection_api_returns_snapshot_notes(conn):
    notes = {
        "graph_structure_hint_projection": {
            "status": "conflict",
            "hint_count": 2,
            "materialized_count": 1,
            "conflict_count": 1,
            "hint_states": {
                "hint-ok": {"status": "materialized"},
                "hint-stale": {"status": "conflict", "last_error": "target_node_missing"},
            },
            "suppressed_edges": [
                {"src": "tests/test_service.py", "dst": "L7.old", "edge_type": "tests"}
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="hint-projection-api",
        commit_sha="hintcommit",
        snapshot_kind="scope",
        graph_json=_graph(),
        notes=json.dumps(notes, sort_keys=True),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])

    report = server.handle_graph_governance_snapshot_graph_structure_hints(
        _ctx({"project_id": PID, "snapshot_id": "active"})
    )

    assert report["ok"] is True
    assert report["snapshot_id"] == "hint-projection-api"
    assert report["commit_sha"] == "hintcommit"
    assert report["status"] == "conflict"
    assert report["hint_count"] == 2
    assert report["materialized_count"] == 1
    assert report["conflict_count"] == 1
    assert report["hint_states"]["hint-stale"]["last_error"] == "target_node_missing"
    assert report["suppressed_edges"] == [
        {"src": "tests/test_service.py", "dst": "L7.old", "edge_type": "tests"}
    ]

    bundle = server.handle_graph_governance_dashboard_active_bundle(
        _ctx({"project_id": PID}, query={"snapshot_id": "active"})
    )
    assert bundle["graph_structure_hints"]["status"] == "conflict"
    assert bundle["endpoints"]["graph_structure_hints"].endswith(
        "/snapshots/hint-projection-api/graph-structure-hints"
    )


def test_graph_structure_hint_projection_api_defaults_when_notes_are_absent(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="hint-projection-empty",
        commit_sha="emptycommit",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])

    report = server.handle_graph_governance_snapshot_graph_structure_hints(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    assert report["ok"] is True
    assert report["status"] == "ok"
    assert report["hint_count"] == 0
    assert report["materialized_count"] == 0
    assert report["conflict_count"] == 0
    assert report["projection"]["has_projection_notes"] is False


def test_parallel_branch_read_model_route_returns_durable_runtime_state(conn):
    batch_id = "PB-010-api"
    queue_id = "mergeq-PB010-api"
    target_ref = "refs/heads/main"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id=batch_id,
            task_id="T1",
            backlog_id="OPT-PB010-API",
            branch_ref="refs/heads/codex/PB010-api-T1",
            status="running",
            merge_queue_id=queue_id,
            checkpoint_id="checkpoint-T1",
            snapshot_id="scope-T1",
            projection_id="semproj-T1",
        ),
        now_iso="2026-05-17T06:20:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB010-api-T1",
                queue_index=1,
                status="merge_ready",
                target_ref=target_ref,
                merge_preview_id="preview-T1",
            )
        ],
        now_iso="2026-05-17T06:20:00Z",
    )
    upsert_batch_merge_runtime(
        conn,
        BatchMergeRuntime(
            project_id=PID,
            batch_id=batch_id,
            target_ref=target_ref,
            batch_base_commit="B0",
            current_target_head="B0",
            batch_status=BATCH_STATE_OPEN,
            items=(
                BatchMergeItem(
                    task_id="T1",
                    branch_ref="refs/heads/codex/PB010-api-T1",
                    worktree_path="/tmp/worktrees/PB010-api-T1",
                    queue_index=1,
                    status="merge_ready",
                    branch_head="H1",
                    base_commit="B0",
                    snapshot_id="scope-T1",
                    projection_id="semproj-T1",
                ),
            ),
        ),
        now_iso="2026-05-17T06:20:00Z",
    )

    result = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": batch_id,
                "merge_queue_id": queue_id,
                "target_ref": target_ref,
                "limit": "5",
            },
        )
    )

    assert result["ok"] is True
    payload = result["read_model"]
    assert payload["project_id"] == PID
    assert payload["batch_id"] == batch_id
    assert payload["summary"]["lane_count"] == 1
    assert payload["summary"]["mergeable_count"] == 1
    assert payload["branch_lanes"][0]["task_id"] == "T1"
    assert payload["merge_queue"]["rows"][0]["merge_preview_id"] == "preview-T1"
    assert payload["rollback"]["cleanup_allowed"] is False
    assert payload["truncated"] == {
        "branch_lanes": False,
        "merge_queue_rows": False,
        "rollback_rows": False,
    }


def test_parallel_branch_read_model_route_marks_supplied_target_head_drift(conn):
    queue_id = "mergeq-api-stale-target"
    target_ref = "refs/heads/main"
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-stale-target",
                task_id="stale-target-task",
                branch_ref="refs/heads/codex/stale-target-task",
                queue_index=1,
                status="merge_ready",
                target_ref=target_ref,
                branch_head="branch-head",
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-before",
            )
        ],
        now_iso="2026-05-17T09:10:00Z",
    )

    result = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "merge_queue_id": queue_id,
                "target_ref": target_ref,
                "current_target_head": "target-after",
                "limit": "5",
            },
        )
    )
    payload = result["read_model"]
    row = payload["merge_queue"]["rows"][0]

    assert result["ok"] is True
    assert payload["summary"]["mergeable_count"] == 0
    assert payload["summary"]["stale_count"] == 1
    assert payload["merge_queue"]["stale_task_ids"] == ["stale-target-task"]
    assert row["stale_target_head"] is True
    assert row["queue_state"] == "stale_after_dependency_merge"


def test_parallel_branch_read_model_route_can_resolve_actual_target_head(conn, tmp_path):
    repo = _git_repo(tmp_path)
    target_before = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "target.txt").write_text("target moved\n", encoding="utf-8")
    subprocess.run(["git", "add", "target.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "move target"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    queue_id = "mergeq-api-resolve-target"
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-resolve-target",
                task_id="resolve-target-task",
                branch_ref="refs/heads/codex/resolve-target-task",
                queue_index=1,
                status="merge_ready",
                target_ref="main",
                branch_head="branch-head",
                validated_target_head=target_before,
                current_target_head=target_before,
            )
        ],
        now_iso="2026-05-17T09:11:00Z",
    )

    result = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "merge_queue_id": queue_id,
                "target_ref": "main",
                "workspace_path": str(repo),
                "resolve_current_target_head": "true",
                "limit": "5",
            },
        )
    )

    assert result["ok"] is True
    assert result["read_model"]["merge_queue"]["stale_task_ids"] == ["resolve-target-task"]
    assert result["read_model"]["merge_queue"]["rows"][0]["stale_target_head"] is True


def test_parallel_branch_allocate_route_materializes_worktree_and_updates_read_model(conn, tmp_path):
    repo = _git_repo(tmp_path)

    status, created = server.handle_graph_governance_parallel_branch_allocate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "API Branch Task",
                "batch_id": "PB-api-alloc",
                "backlog_id": "ARCH-PB-ALLOC",
                "workspace_root": str(repo),
                "worker_id": "worker api",
                "merge_queue_id": "mergeq-api-alloc",
                "create_worktree": True,
                "now_iso": "2026-05-17T07:10:00Z",
            },
        )
    )

    assert status == 201
    assert created["ok"] is True
    context = created["context"]
    assert context["status"] == "worktree_ready"
    assert context["branch_ref"] == "refs/heads/codex/api-branch-task"
    assert context["fence_token"].startswith("fence-")
    assert context["worktree_path"] == str(repo / ".worktrees" / "worker-api" / "api-branch-task")
    allocation_evidence = created["branch_runtime_evidence"]
    assert allocation_evidence["schema_version"] == "mf_subagent_branch_runtime.v1"
    assert allocation_evidence["registered"] is True
    assert allocation_evidence["source_ref"].endswith("/parallel-branches/allocate")
    assert allocation_evidence["runtime_context_id"] == context["runtime_context_id"]
    assert allocation_evidence["context"]["worktree_path"] == context["worktree_path"]
    assert created["worktree"]["created"] is True
    assert created["worktree"]["branch_graph"]["status"] == "ready"

    checkpoint = server.handle_graph_governance_parallel_branch_checkpoint(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "API Branch Task",
                "checkpoint_id": "checkpoint-api-alloc",
                "fence_token": context["fence_token"],
                "now_iso": "2026-05-17T07:11:00Z",
            },
        )
    )
    assert checkpoint["context"]["checkpoint_id"] == "checkpoint-api-alloc"

    read = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": "PB-api-alloc",
                "merge_queue_id": "mergeq-api-alloc",
                "limit": "5",
            },
        )
    )
    lanes = read["read_model"]["branch_lanes"]
    assert len(lanes) == 1
    assert lanes[0]["task_id"] == "API Branch Task"
    assert lanes[0]["status"] == "worktree_ready"
    assert lanes[0]["worktree_path"] == context["worktree_path"]
    assert lanes[0]["graph_epoch"]["base_commit"]


def test_parallel_branch_allocate_issues_same_owner_scoped_session_token(conn, tmp_path):
    repo = _git_repo(tmp_path)

    status, created = server.handle_graph_governance_parallel_branch_allocate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "same-owner-token-task",
                "batch_id": "PB-same-owner-token",
                "backlog_id": "AC-SAME-OWNER-WORKER-SESSION-TOKEN-ISSUANCE-20260613",
                "workspace_root": str(repo),
                "worker_id": "same-owner-worker",
                "agent_id": "same-owner-agent",
                "allocation_owner": "same-owner-agent",
                "merge_queue_id": "mergeq-same-owner-token",
                "fence_token": "fence-same-owner-token",
                "create_worktree": True,
                "now_iso": "2026-06-13T03:40:00Z",
            },
        )
    )

    assert status == 201
    session = created["same_owner_worker_session"]
    raw_token = session["session_token"]
    token_hash = mf_subagent_session_token_hash(raw_token)
    context = created["context"]
    assert session["issued"] is True
    assert session["scope"]["task_id"] == "same-owner-token-task"
    assert session["scope"]["runtime_context_id"] == context["runtime_context_id"]
    assert session["session_token_hash"] == token_hash
    assert context["session_token_hash"] == token_hash
    assert raw_token not in str(context)
    assert raw_token not in str(created["branch_runtime_evidence"])

    started = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "same-owner-token-task",
                "parent_task_id": "same-owner-token-task",
                "worker_role": "mf_sub",
                "worker_id": "same-owner-worker",
                "worker_slot_id": "same-owner-worker",
                "agent_id": "same-owner-agent",
                "session_token": raw_token,
                "runtime_context_id": context["runtime_context_id"],
                "fence_token": "fence-same-owner-token",
                "actual_cwd": context["worktree_path"],
                "actual_git_root": context["worktree_path"],
                "branch": context["branch_ref"],
                "head_commit": "head-same-owner-token",
                "base_commit": context["base_commit"],
                "target_head_commit": context["target_head_commit"],
                "merge_queue_id": "mergeq-same-owner-token",
                "owned_files": ["agent/governance/server.py"],
                "route_id": "route-same-owner-token",
                "route_context_hash": "sha256:route-same-owner-token",
                "prompt_contract_id": "rprompt-same-owner-token",
                "prompt_contract_hash": "sha256:prompt-same-owner-token",
                "route_token_ref": "rtok-same-owner-token",
                "visible_injection_manifest_hash": "sha256:visible-same-owner-token",
                "observer_command_id": "cmd-same-owner-token",
                "read_receipt_hash": "sha256:read-same-owner-token",
                "read_receipt_event_id": "4308",
            },
        )
    )

    assert started["ok"] is True
    gate = started["startup_gate"]
    assert gate["agent_id_match_mode"] == "same_as_allocation_owner"
    assert gate["session_token_evidence_type"] == "server_verified"
    assert gate["server_issued_session_token_verified"] is True
    assert raw_token not in str(started)


def test_runtime_text_prepare_accepts_parallel_branch_allocate_evidence(conn, tmp_path):
    repo = _git_repo(tmp_path)
    main = tmp_path / "main"
    main.mkdir()

    status, allocated = server.handle_graph_governance_parallel_branch_allocate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "Runtime Text Allocate Task",
                "backlog_id": "AC-RUNTIME-TEXT",
                "parent_task_id": "AC-RUNTIME-TEXT",
                "workspace_root": str(repo),
                "worker_id": "worker api",
                "merge_queue_id": "mq-runtime-text-api",
                "create_worktree": True,
                "now_iso": "2026-05-17T07:12:00Z",
            },
        )
    )
    assert status == 201
    allocation_evidence = allocated["branch_runtime_evidence"]
    assert allocation_evidence["status"] == STATE_WORKTREE_READY
    assert allocation_evidence["registered"] is True

    prepared = build_observer_runtime_text_context(
        ObserverRuntimeTextPrepareRequest(
            project_id=PID,
            backlog_id="AC-RUNTIME-TEXT",
            route=RoutePromptContract(
                route_context_hash="sha256:route-api",
                prompt_contract_id="rprompt-api",
                prompt_contract_hash="sha256:prompt-api",
                route_token_ref="rtok-api",
            ),
            main_worktree=str(main),
            owned_files=("agent/observer_runtime.py",),
            observer_command_id="cmd-runtime-api",
            task_id=allocated["context"]["task_id"],
            parent_task_id=allocated["context"]["root_task_id"],
            worker_id=allocated["context"]["worker_id"],
            graph_trace_ids=("gqt-runtime-api",),
            branch_runtime_evidence=allocation_evidence,
            route_id="route-api",
            visible_injection_manifest_hash="sha256:visible-api",
        )
    )

    assert prepared["ok"] is True
    assert prepared["status"] == "prepared"
    assert prepared["runtime_context_id"] == allocation_evidence["runtime_context_id"]
    assert prepared["observer_command_id"] == "cmd-runtime-api"
    assert prepared["runtime_context"]["worktree_path"] == allocated["context"]["worktree_path"]
    assert prepared["branch_runtime_evidence"]["status"] == STATE_WORKTREE_READY
    assert prepared["branch_runtime_evidence"]["registered"] is True
    assert prepared["dispatch_gate_validation"]["startup_intent_event_generated"] is True


@pytest.mark.parametrize("path_field", ["worktree_root", "worktree_path"])
def test_parallel_branch_allocate_accepts_final_absolute_worktree_path(
    conn,
    tmp_path,
    path_field,
):
    repo = _git_repo(tmp_path)
    final_worktree = repo / ".worktrees" / "worker-api" / "api-branch-task"

    status, allocated = server.handle_graph_governance_parallel_branch_allocate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "API Branch Task",
                "batch_id": "PB-api-final-path",
                "backlog_id": "ARCH-PB-ALLOC",
                "worker_id": "worker api",
                "workspace_root": str(repo),
                "base_commit": "base-api",
                "target_head_commit": "target-api",
                "merge_queue_id": "mergeq-api-alloc",
                "create_worktree": False,
                path_field: str(final_worktree),
            },
        )
    )

    assert status == 201
    assert allocated["context"]["worktree_path"] == str(final_worktree)
    assert allocated["branch_runtime_evidence"]["context"]["worktree_path"] == (
        str(final_worktree)
    )


def test_parallel_branch_allocate_rejects_ambiguous_absolute_worktree_root(conn, tmp_path):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValidationError, match="worktree_root appears to include"):
        server.handle_graph_governance_parallel_branch_allocate(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "API Branch Task",
                    "backlog_id": "ARCH-PB-ALLOC",
                    "worker_id": "worker api",
                    "workspace_root": str(repo),
                    "worktree_root": str(repo / ".worktrees" / "worker-api"),
                    "base_commit": "base-api",
                    "target_head_commit": "target-api",
                    "merge_queue_id": "mergeq-api-alloc",
                    "create_worktree": False,
                },
            )
        )


def test_parallel_branch_allocate_without_worktree_preserves_materialized_runtime_context(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="API Branch Task",
            runtime_context_id="mfrctx-api-branch-task",
            batch_id="PB-api-alloc",
            backlog_id="ARCH-PB-ALLOC",
            root_task_id="root-api-alloc",
            stage_task_id="API Branch Task",
            stage_type="mf_sub",
            agent_id="agent-api",
            worker_id="worker api",
            attempt=3,
            lease_id="lease-existing",
            lease_expires_at="2026-05-17T08:00:00Z",
            fence_token="fence-existing",
            branch_ref="refs/heads/codex/api-branch-task",
            ref_name="main",
            worktree_id="wt-api-branch-task",
            worktree_path="/repo/.worktrees/worker-api/api-branch-task",
            base_commit="base-existing",
            head_commit="head-existing",
            target_head_commit="target-existing",
            snapshot_id="scope-existing",
            projection_id="semproj-existing",
            merge_queue_id="mergeq-existing",
            merge_preview_id="preview-existing",
            rollback_epoch="rollback-existing",
            replay_epoch="replay-existing",
            status=STATE_WORKTREE_READY,
            checkpoint_id="checkpoint-existing",
            replay_source="mf_sub_finish_gate",
            last_recovery_action="finish_gate_recorded",
        ),
        now_iso="2026-05-17T07:10:00Z",
    )

    status, allocated = server.handle_graph_governance_parallel_branch_allocate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "API Branch Task",
                "batch_id": "PB-api-alloc",
                "backlog_id": "ARCH-PB-ALLOC",
                "worker_id": "worker api",
                "workspace_root": "/repo",
                "base_commit": "base-new",
                "target_head_commit": "target-new",
                "fence_token": "fence-new",
                "create_worktree": False,
                "now_iso": "2026-05-17T07:12:00Z",
            },
        )
    )

    assert status == 201
    context = allocated["context"]
    assert context["status"] == "worktree_ready"
    assert context["fence_token"] == "fence-existing"
    assert context["worktree_id"] == "wt-api-branch-task"
    assert context["worktree_path"] == "/repo/.worktrees/worker-api/api-branch-task"
    assert context["base_commit"] == "base-existing"
    assert context["head_commit"] == "head-existing"
    assert context["target_head_commit"] == "target-existing"
    assert context["snapshot_id"] == "scope-existing"
    assert context["projection_id"] == "semproj-existing"
    assert context["merge_queue_id"] == "mergeq-existing"
    assert context["merge_preview_id"] == "preview-existing"
    assert context["checkpoint_id"] == "checkpoint-existing"
    assert context["replay_source"] == "mf_sub_finish_gate"

    reloaded = get_branch_context(conn, PID, "API Branch Task")
    assert reloaded is not None
    assert reloaded.status == STATE_WORKTREE_READY
    assert reloaded.head_commit == "head-existing"
    assert reloaded.checkpoint_id == "checkpoint-existing"


def test_observer_runtime_text_prepare_resolves_persisted_runtime_context_id(conn, tmp_path):
    worktree = tmp_path / "worker"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-text-task",
            runtime_context_id="mfrctx-runtime-text-api",
            backlog_id="AC-RUNTIME-TEXT",
            root_task_id="AC-RUNTIME-TEXT",
            stage_task_id="runtime-text-task",
            stage_type="mf_sub",
            worker_id="worker-api",
            attempt=1,
            fence_token="fence-runtime-text-api",
            branch_ref="refs/heads/codex/runtime-text-task",
            worktree_id="wt-runtime-text-task",
            worktree_path=str(worktree),
            base_commit="base-api",
            target_head_commit="target-api",
            merge_queue_id="mq-runtime-text-api",
            status=STATE_WORKTREE_READY,
        ),
    )
    main = tmp_path / "main"
    main.mkdir()

    prepared = server.handle_observer_runtime_text_prepare(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "backlog_id": "AC-RUNTIME-TEXT",
                "observer_command_id": "cmd-runtime-text-api",
                "task_id": "runtime-text-task",
                "parent_task_id": "AC-RUNTIME-TEXT",
                "runtime_context_id": "mfrctx-runtime-text-api",
                "fence_token": "fence-runtime-text-api",
                "worktree_path": str(worktree),
                "base_commit": "base-api",
                "target_head_commit": "target-api",
                "merge_queue_id": "mq-runtime-text-api",
                "route_context_hash": "sha256:route-api",
                "route_id": "route-api",
                "prompt_contract_id": "rprompt-api",
                "prompt_contract_hash": "sha256:prompt-api",
                "route_token_ref": "rtok-api",
                "visible_injection_manifest_hash": "sha256:visible-api",
                "main_worktree": str(main),
                "owned_files": ["agent/observer_runtime.py"],
                "graph_trace_ids": ["gqt-runtime-api"],
            },
        )
    )

    assert prepared["ok"] is True
    assert prepared["status"] == "prepared"
    assert prepared["runtime_context_id"] == "mfrctx-runtime-text-api"
    assert prepared["observer_command_id"] == "cmd-runtime-text-api"
    assert prepared["runtime_context"]["worktree_path"] == str(worktree)
    evidence = prepared["branch_runtime_evidence"]
    assert evidence["registered"] is True
    assert evidence["registration_source"] == "persisted_branch_runtime_context"
    assert evidence["context"]["worktree_path"] == str(worktree)


def test_observer_runtime_text_prepare_resolves_runtime_context_registration_ref(conn, tmp_path):
    workspace = tmp_path / "workers"
    main = tmp_path / "main"
    main.mkdir()
    status_code, allocated = server.handle_graph_governance_parallel_branch_allocate(
        _ctx_with_role(
            {"project_id": PID},
            "observer",
            method="POST",
            body={
                "task_id": "runtime-text-task",
                "parent_task_id": "AC-RUNTIME-TEXT",
                "backlog_id": "AC-RUNTIME-TEXT",
                "worker_id": "worker-api",
                "workspace_root": str(workspace),
                "fence_token": "fence-runtime-text-api",
                "base_commit": "base-api",
                "target_head_commit": "target-api",
                "merge_queue_id": "mq-runtime-text-api",
                "create_worktree": False,
            },
        )
    )
    assert status_code == 201
    context = allocated["context"]

    prepared = server.handle_observer_runtime_text_prepare(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "backlog_id": "AC-RUNTIME-TEXT",
                "observer_command_id": "cmd-runtime-text-api",
                "task_id": context["task_id"],
                "parent_task_id": context["root_task_id"],
                "branch_runtime_registration_ref": context["runtime_context_id"],
                "fence_token": context["fence_token"],
                "worktree_path": context["worktree_path"],
                "base_commit": context["base_commit"],
                "target_head_commit": context["target_head_commit"],
                "merge_queue_id": context["merge_queue_id"],
                "route_context_hash": "sha256:route-api",
                "route_id": "route-api",
                "prompt_contract_id": "rprompt-api",
                "prompt_contract_hash": "sha256:prompt-api",
                "route_token_ref": "rtok-api",
                "visible_injection_manifest_hash": "sha256:visible-api",
                "main_worktree": str(main),
                "owned_files": ["agent/observer_runtime.py"],
                "graph_trace_ids": ["gqt-runtime-api"],
            },
        )
    )

    assert prepared["ok"] is True
    assert prepared["status"] == "prepared"
    assert prepared["runtime_context_id"] == context["runtime_context_id"]
    assert prepared["observer_command_id"] == "cmd-runtime-text-api"
    assert prepared["runtime_context"]["worktree_path"] == context["worktree_path"]
    evidence = prepared["branch_runtime_evidence"]
    assert evidence["registered"] is True
    assert evidence["registration_ref"].endswith("/parallel-branches/allocate")
    assert evidence["registration_source"] == "persisted_branch_runtime_context"
    revision = prepared["runtime_contract_revision"]
    assert revision["route_identity"]["route_context_hash"] == "sha256:route-api"
    assert revision["route_identity"]["prompt_contract_id"] == "rprompt-api"
    assert revision["route_identity"]["prompt_contract_hash"] == "sha256:prompt-api"
    assert revision["route_identity"]["visible_injection_manifest_hash"] == (
        "sha256:visible-api"
    )
    assert revision["payload"]["target_files"] == ["agent/observer_runtime.py"]
    assert prepared["persistent_evidence"]["contract_revision_persisted"] is True

    current_state = server.handle_graph_governance_parallel_branch_runtime_context_current_state(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context["runtime_context_id"],
            },
            "observer",
            query={"view": "all"},
        )
    )
    views = current_state["runtime_context_service"]["views"]
    current = views["current"]
    gate_inputs = views["gate_inputs"]
    worker_view = views["worker_view"]
    assert current["route_identity"]["route_context_hash"] == "sha256:route-api"
    assert current["route_identity"]["prompt_contract_id"] == "rprompt-api"
    assert current["route_identity"]["prompt_contract_hash"] == "sha256:prompt-api"
    assert current["route_identity"]["visible_injection_manifest_hash"] == (
        "sha256:visible-api"
    )
    assert current["identity"]["observer_command_id"] == "cmd-runtime-text-api"
    assert current["work"]["target_files"] == ["agent/observer_runtime.py"]
    assert gate_inputs["route_context_hash"] == "sha256:route-api"
    assert gate_inputs["observer_command_id"] == "cmd-runtime-text-api"
    assert gate_inputs["prompt_contract_id"] == "rprompt-api"
    assert gate_inputs["prompt_contract_hash"] == "sha256:prompt-api"
    assert gate_inputs["visible_injection_manifest_hash"] == "sha256:visible-api"
    assert gate_inputs["target_files"] == ["agent/observer_runtime.py"]
    assert worker_view["route_context_hash"] == "sha256:route-api"
    assert worker_view["observer_command_id"] == "cmd-runtime-text-api"
    assert worker_view["prompt_contract_id"] == "rprompt-api"
    assert worker_view["prompt_contract_hash"] == "sha256:prompt-api"
    assert worker_view["visible_injection_manifest_hash"] == "sha256:visible-api"
    assert worker_view["target_files"] == ["agent/observer_runtime.py"]

    contract = server.handle_graph_governance_parallel_branch_runtime_contract_by_context(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context["runtime_context_id"],
            },
            "mf_sub",
            query={
                "observer_command_id": "cmd-runtime-text-api",
                "parent_task_id": context["root_task_id"],
                "fence_token": context["fence_token"],
            },
        )
    )["runtime_contract"]
    assert contract["route_context_hash"] == "sha256:route-api"
    assert contract["observer_command_id"] == "cmd-runtime-text-api"
    assert contract["runtime_context"]["observer_command_id"] == (
        "cmd-runtime-text-api"
    )
    assert contract["contract"]["observer_command"]["observer_command_id"] == (
        "cmd-runtime-text-api"
    )
    assert contract["prompt_contract_id"] == "rprompt-api"
    assert contract["prompt_contract_hash"] == "sha256:prompt-api"
    assert contract["visible_injection_manifest_hash"] == "sha256:visible-api"
    assert contract["target_files"] == ["agent/observer_runtime.py"]


def test_observer_runtime_text_prepare_persists_registered_host_identity_for_startup(
    conn,
    tmp_path,
):
    worktree = tmp_path / "worker-prepare-startup"
    worktree.mkdir()
    main = tmp_path / "main"
    main.mkdir()
    runtime_context = BranchTaskRuntimeContext(
        project_id=PID,
        task_id="prepare-startup-task",
        runtime_context_id="mfrctx-prepare-startup",
        root_task_id="AC-RUNTIME-PREPARE-STARTUP",
        stage_task_id="prepare-startup-task",
        backlog_id="AC-RUNTIME-PREPARE-STARTUP",
        worker_id="prepare-worker-slot",
        worker_slot_id="prepare-worker-slot",
        agent_id="observer-allocation-owner",
        allocation_owner="observer-allocation-owner",
        branch_ref="refs/heads/codex/prepare-startup-task",
        status=STATE_WORKTREE_READY,
        fence_token="fence-prepare-startup",
        worktree_path=str(worktree),
        base_commit="base-prepare-startup",
        target_head_commit="target-prepare-startup",
        merge_queue_id="mq-prepare-startup",
    )
    upsert_branch_context(conn, runtime_context, now_iso="2026-06-12T10:00:00Z")

    prepared = server.handle_observer_runtime_text_prepare(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "backlog_id": "AC-RUNTIME-PREPARE-STARTUP",
                "observer_command_id": "cmd-prepare-startup",
                "task_id": "prepare-startup-task",
                "parent_task_id": "AC-RUNTIME-PREPARE-STARTUP",
                "runtime_context_id": "mfrctx-prepare-startup",
                "branch_runtime_registration_ref": "mfrctx-prepare-startup",
                "fence_token": "fence-prepare-startup",
                "worktree_path": str(worktree),
                "base_commit": "base-prepare-startup",
                "target_head_commit": "target-prepare-startup",
                "merge_queue_id": "mq-prepare-startup",
                "route_id": "route-prepare-startup",
                "route_context_hash": "sha256:route-prepare-startup",
                "prompt_contract_id": "rprompt-prepare-startup",
                "prompt_contract_hash": "sha256:prompt-prepare-startup",
                "route_token_ref": "rtok-prepare-startup",
                "visible_injection_manifest_hash": "sha256:visible-prepare-startup",
                "main_worktree": str(main),
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "graph_trace_ids": ["gqt-prepare-startup"],
                "backend_mode": "codex_cli_exec",
                "startup_source": "codex_cli_exec",
                "host_adapter_agent_id": "prepare-host-agent",
                "actual_host_worker_id": "prepare-host-worker",
                "host_startup_id": "host-startup-prepare",
                "host_session_id": "host-session-prepare",
                "session_token_surrogate": "host-adapter:prepare",
            },
        )
    )

    assert prepared["ok"] is True
    latest_revision = get_latest_branch_contract_revision(
        conn,
        PID,
        "mfrctx-prepare-startup",
    )
    assert latest_revision is not None
    registered = latest_revision.payload["registered_host_adapter_spawn"]
    assert registered["runtime_context_id"] == "mfrctx-prepare-startup"
    assert registered["observer_command_id"] == "cmd-prepare-startup"
    assert registered["launch_text_hash"] == prepared["launch_text_hash"]
    assert registered["task_id"] == "prepare-startup-task"
    assert registered["worker_slot_id"] == "prepare-worker-slot"
    assert registered["agent_id"] == "prepare-host-agent"
    assert registered["actual_host_worker_id"] == "prepare-host-worker"
    assert registered["host_startup_id"] == "host-startup-prepare"
    assert registered["host_session_id"] == "host-session-prepare"
    assert registered["session_token_surrogate"] == "host-adapter:prepare"

    started = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "prepare-startup-task",
                "parent_task_id": "AC-RUNTIME-PREPARE-STARTUP",
                "worker_role": "mf_sub",
                "worker_id": "prepare-worker-slot",
                "agent_id": registered["agent_id"],
                "actual_host_worker_id": registered["actual_host_worker_id"],
                "runtime_context_id": registered["runtime_context_id"],
                "launch_text_hash": registered["launch_text_hash"],
                "host_startup_id": registered["host_startup_id"],
                "host_session_id": registered["host_session_id"],
                "session_token_surrogate": registered["session_token_surrogate"],
                "startup_source": registered["startup_source"],
                "fence_token": "fence-prepare-startup",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/prepare-startup-task",
                "head_commit": "head-prepare-startup",
                "base_commit": "base-prepare-startup",
                "target_head_commit": "target-prepare-startup",
                "merge_queue_id": "mq-prepare-startup",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-prepare-startup",
                "route_context_hash": "sha256:route-prepare-startup",
                "prompt_contract_id": "rprompt-prepare-startup",
                "prompt_contract_hash": "sha256:prompt-prepare-startup",
                "route_token_ref": "rtok-prepare-startup",
                "visible_injection_manifest_hash": "sha256:visible-prepare-startup",
                "observer_command_id": "cmd-prepare-startup",
                "read_receipt_hash": "sha256:read-prepare-startup",
                "read_receipt_event_id": "read-prepare-startup",
            },
        )
    )

    assert started["ok"] is True
    gate = started["startup_gate"]
    assert gate["host_adapter_startup_token_accepted"] is True
    assert gate["agent_id_match_mode"] == "host_adapter_startup_token_surrogate"
    assert gate["agent_id"] == "prepare-host-agent"
    assert gate["actual_host_worker_id"] == "prepare-host-worker"
    assert gate["host_startup_id"] == "host-startup-prepare"
    assert gate["session_token_surrogate"] == "host-adapter:prepare"


def test_observer_runtime_text_prepare_rejects_unpersisted_runtime_context_id(conn, tmp_path):
    main = tmp_path / "main"
    main.mkdir()

    prepared = server.handle_observer_runtime_text_prepare(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "backlog_id": "AC-RUNTIME-TEXT",
                "observer_command_id": "cmd-runtime-text-api",
                "task_id": "runtime-text-task",
                "parent_task_id": "AC-RUNTIME-TEXT",
                "runtime_context_id": "mfrctx-missing",
                "fence_token": "fence-runtime-text-api",
                "base_commit": "base-api",
                "target_head_commit": "target-api",
                "merge_queue_id": "mq-runtime-text-api",
                "route_context_hash": "sha256:route-api",
                "route_id": "route-api",
                "prompt_contract_id": "rprompt-api",
                "route_token_ref": "rtok-api",
                "visible_injection_manifest_hash": "sha256:visible-api",
                "main_worktree": str(main),
                "owned_files": ["agent/observer_runtime.py"],
                "graph_trace_ids": ["gqt-runtime-api"],
            },
        )
    )

    assert prepared["ok"] is False
    assert prepared["status"] == "allocation_required"
    assert prepared["branch_runtime_evidence"]["registered"] is False
    assert "not found" in prepared["branch_runtime_evidence"]["message"]


def test_observer_runtime_text_prepare_rejects_runtime_context_identity_mismatch(conn, tmp_path):
    worktree = tmp_path / "worker"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-text-task",
            runtime_context_id="mfrctx-runtime-text-api",
            backlog_id="AC-RUNTIME-TEXT",
            root_task_id="AC-RUNTIME-TEXT",
            stage_task_id="runtime-text-task",
            fence_token="fence-runtime-text-api",
            branch_ref="refs/heads/codex/runtime-text-task",
            worktree_path=str(worktree),
            base_commit="base-api",
            target_head_commit="target-api",
            merge_queue_id="mq-runtime-text-api",
            status=STATE_WORKTREE_READY,
        ),
    )
    main = tmp_path / "main"
    main.mkdir()

    prepared = server.handle_observer_runtime_text_prepare(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "backlog_id": "AC-RUNTIME-TEXT",
                "task_id": "runtime-text-task",
                "parent_task_id": "AC-RUNTIME-TEXT",
                "runtime_context_id": "mfrctx-runtime-text-api",
                "fence_token": "wrong-fence",
                "worktree_path": str(worktree),
                "base_commit": "base-api",
                "target_head_commit": "target-api",
                "merge_queue_id": "mq-runtime-text-api",
                "route_context_hash": "sha256:route-api",
                "route_id": "route-api",
                "prompt_contract_id": "rprompt-api",
                "route_token_ref": "rtok-api",
                "visible_injection_manifest_hash": "sha256:visible-api",
                "main_worktree": str(main),
                "owned_files": ["agent/observer_runtime.py"],
                "graph_trace_ids": ["gqt-runtime-api"],
            },
        )
    )

    assert prepared["ok"] is False
    assert prepared["status"] == "allocation_required"
    assert prepared["branch_runtime_evidence"]["registered"] is False
    assert prepared["branch_runtime_evidence"]["mismatches"][0]["field"] == "fence_token"


@pytest.mark.parametrize(
    ("body_field", "expected_field", "wrong_value"),
    [
        ("task_id", "task_id", "wrong-runtime-text-task"),
        ("parent_task_id", "parent_task_id", "wrong-parent"),
        ("worktree_path", "worktree_path", "/wrong/worktree"),
        ("base_commit", "base_commit", "wrong-base"),
        ("target_head_commit", "target_head_commit", "wrong-target"),
        ("merge_queue_id", "merge_queue_id", "wrong-merge-queue"),
    ],
)
def test_observer_runtime_text_prepare_rejects_persisted_runtime_context_mismatches(
    conn,
    tmp_path,
    body_field,
    expected_field,
    wrong_value,
):
    worktree = tmp_path / "worker"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-text-task",
            runtime_context_id="mfrctx-runtime-text-api",
            backlog_id="AC-RUNTIME-TEXT",
            root_task_id="AC-RUNTIME-TEXT",
            stage_task_id="runtime-text-task",
            fence_token="fence-runtime-text-api",
            branch_ref="refs/heads/codex/runtime-text-task",
            worktree_path=str(worktree),
            base_commit="base-api",
            target_head_commit="target-api",
            merge_queue_id="mq-runtime-text-api",
            status=STATE_WORKTREE_READY,
        ),
    )
    main = tmp_path / "main"
    main.mkdir()
    body = {
        "backlog_id": "AC-RUNTIME-TEXT",
        "observer_command_id": "cmd-runtime-text-api",
        "task_id": "runtime-text-task",
        "parent_task_id": "AC-RUNTIME-TEXT",
        "branch_runtime_registration_ref": "mfrctx-runtime-text-api",
        "fence_token": "fence-runtime-text-api",
        "worktree_path": str(worktree),
        "base_commit": "base-api",
        "target_head_commit": "target-api",
        "merge_queue_id": "mq-runtime-text-api",
        "route_context_hash": "sha256:route-api",
        "route_id": "route-api",
        "prompt_contract_id": "rprompt-api",
        "route_token_ref": "rtok-api",
        "visible_injection_manifest_hash": "sha256:visible-api",
        "main_worktree": str(main),
        "owned_files": ["agent/observer_runtime.py"],
        "graph_trace_ids": ["gqt-runtime-api"],
    }
    body[body_field] = wrong_value

    prepared = server.handle_observer_runtime_text_prepare(
        _ctx(
            {"project_id": PID},
            method="POST",
            body=body,
        )
    )

    assert prepared["ok"] is False
    assert prepared["status"] == "allocation_required"
    assert prepared["branch_runtime_evidence"]["registered"] is False
    assert any(
        mismatch["field"] == expected_field
        for mismatch in prepared["branch_runtime_evidence"]["mismatches"]
    )


def test_parallel_branch_runtime_contract_route_returns_worker_scoped_view(conn):
    raw_fence = "synthetic-raw-fence-runtime-contract-secret"
    raw_session = "synthetic-raw-session-runtime-contract-secret"
    raw_private = "synthetic-private-runtime-contract-secret"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-contract-task",
            root_task_id="runtime-contract-parent",
            backlog_id="AC-CONTRACT-RUNTIME-SERVICE-SHARED-CONTEXT-20260603",
            worker_id="worker-runtime",
            attempt=1,
            branch_ref="refs/heads/codex/runtime-contract-task",
            ref_name="main",
            worktree_path="/repo/.worktrees/runtime-contract-task",
            base_commit="base123",
            head_commit="head123",
            target_head_commit="target123",
            snapshot_id="scope-1",
            projection_id="semproj-1",
            merge_queue_id="mq-runtime",
            fence_token=raw_fence,
            session_token_hash=mf_subagent_session_token_hash(raw_session),
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-03T10:00:00Z",
    )

    with pytest.raises(GovernanceError) as exc_info:
        server.handle_graph_governance_parallel_branch_runtime_contract(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "task_id": "runtime-contract-task",
                },
                "mf_sub",
                query={
                    "parent_task_id": "runtime-contract-parent",
                    "fence_token": raw_fence,
                    "session_token": "wrong-runtime-contract-session",
                },
            )
        )
    assert exc_info.value.status == 403
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM parallel_branch_runtime_access_audit"
    ).fetchone()[0]
    assert audit_count == 0

    result = server.handle_graph_governance_parallel_branch_runtime_contract(
        _ctx_with_role(
            {
                "project_id": PID,
                "task_id": "runtime-contract-task",
            },
                "mf_sub",
                query={
                    "parent_task_id": "runtime-contract-parent",
                    "fence_token": raw_fence,
                    "session_token": raw_session,
                    "route_context_hash": "sha256:route",
                    "prompt_contract_id": "rprompt-runtime",
                    "visible_injection_manifest_hash": "sha256:visible",
                    "raw_private_context": raw_private,
                },
            )
    )

    view = result["runtime_contract"]
    assert result["ok"] is True
    assert view["schema_version"] == "mf_subagent_runtime_contract_view.v1"
    assert view["role_scope"] == "worker"
    assert view["latest_revision_id"] == ""
    assert view["known_revision_id"] == ""
    assert view["contract_changed"] is False
    assert view["must_ack_revision"] is False
    assert view["poll_after_sec"] == 15
    assert view["runtime_context"]["task_id"] == "runtime-contract-task"
    assert view["runtime_context"]["parent_task_id"] == "runtime-contract-parent"
    fence_hash = "sha256:" + hashlib.sha256(raw_fence.encode("utf-8")).hexdigest()
    assert view["runtime_context"]["fence_token"] == "redacted"
    assert view["runtime_context"]["fence_token_hash"] == fence_hash
    assert view["runtime_context"]["fence_token_redacted"] is True
    assert view["agent_task_contract"]["target_fences"] == ["redacted"]
    assert view["contract"]["protected_timeline_append"]["task_scoped_route_waiver"][
        "scope"
    ]["fence_token"] == "redacted"
    assert view["contract"]["contract_change_policy"]["source_of_truth"] == "contract_service"
    assert view["route_identity"]["route_context_hash"] == "sha256:route"
    assert view["route_identity"]["prompt_contract_id"] == "rprompt-runtime"
    assert view["route_identity"]["raw_private_context_exposed"] is False
    assert "raw_private_context" not in view["route_identity"]
    audit = result["access_audit"]
    assert audit["schema_version"] == "runtime_context.access_audit.v1"
    assert audit["projection_hash"].startswith("sha256:")
    assert audit["nodes_read"][0]["view"] == "runtime_contract"
    assert audit["nodes_read"][0]["hash"].startswith("sha256:")
    audit_row = conn.execute(
        """
        SELECT principal_id, role, view_name, projection_hash, nodes_read_json,
               metadata_json, created_at
        FROM parallel_branch_runtime_access_audit
        WHERE audit_id = ?
        """,
        (audit["audit_id"],),
    ).fetchone()
    assert audit_row is not None
    assert audit_row["principal_id"] == "mf_sub-principal"
    assert audit_row["role"] == "mf_sub"
    assert audit_row["view_name"] == "runtime_contract"
    assert audit_row["projection_hash"] == audit["projection_hash"]
    assert audit_row["created_at"]
    assert json.loads(audit_row["nodes_read_json"])[0]["view"] == "runtime_contract"
    assert json.loads(audit_row["metadata_json"])["endpoint"] == (
        "parallel-branches.runtime-contract"
    )
    response_json = json.dumps(result, sort_keys=True)
    audit_nodes_json = audit_row["nodes_read_json"]
    audit_metadata_json = audit_row["metadata_json"]
    for secret in (raw_fence, raw_session, raw_private):
        assert secret not in response_json
        assert secret not in audit_nodes_json
        assert secret not in audit_metadata_json


def test_parallel_branch_runtime_contract_revision_append_and_runtime_context_poll(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-contract-revision-task",
            root_task_id="runtime-contract-revision-parent",
            backlog_id="AC-CONTRACT-RUNTIME-REVISION-POLLING-DOGFOOD-20260603",
            branch_ref="refs/heads/codex/runtime-contract-revision-task",
            worktree_path="/repo/.worktrees/runtime-contract-revision-task",
            base_commit="base123",
            target_head_commit="target123",
            merge_queue_id="mq-runtime",
            fence_token="fence-revision",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-03T10:00:00Z",
    )

    with pytest.raises(GovernanceError) as missing_gate:
        server.handle_graph_governance_parallel_branch_runtime_contract_revision_append(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "task_id": "runtime-contract-revision-task",
                },
                "observer",
                method="POST",
                body={
                    "revision_id": "crev-missing-gate",
                    "runtime_context_id": context.runtime_context_id,
                    "contract_revision": {"summary": "missing route evidence"},
                },
            )
        )
    assert missing_gate.value.code == "route_token_required"

    route_token = _server_issued_route_token(
        conn,
        "append_contract_revision",
        task_id="runtime-contract-revision-task",
        backlog_id="AC-CONTRACT-RUNTIME-REVISION-POLLING-DOGFOOD-20260603",
    )
    status, appended = (
        server.handle_graph_governance_parallel_branch_runtime_contract_revision_append(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "task_id": "runtime-contract-revision-task",
                },
                "observer",
                method="POST",
                body={
                    "revision_id": "crev-1",
                    "runtime_context_id": context.runtime_context_id,
                    "contract_version": "mf_parallel.v1",
                    "contract_revision": {
                        "summary": "worker should poll by runtime_context_id",
                        "raw_private_context": "must not persist",
                        "nested": {
                            "visible": "kept",
                            "hidden_context": "must not persist",
                        },
                    },
                    "route_token": route_token,
                    "raw_private_context": "must-not-leak",
                    "now_iso": "2026-06-03T10:01:00Z",
                },
            )
        )
    )

    assert status == 201
    revision = appended["revision"]
    assert revision["revision_id"] == "crev-1"
    assert revision["runtime_context_id"] == context.runtime_context_id
    assert revision["payload"]["summary"] == "worker should poll by runtime_context_id"
    assert "raw_private_context" not in revision["payload"]
    assert "hidden_context" not in revision["payload"]["nested"]
    assert revision["route_identity"]["route_context_hash"].startswith("sha256:")
    assert revision["route_identity"]["prompt_contract_id"] == route_token["prompt_contract_id"]
    assert revision["route_identity"]["raw_private_context_exposed"] is False
    serialized_revision = json.dumps(revision, sort_keys=True)
    assert "must-not-leak" not in serialized_revision
    assert "must not persist" not in serialized_revision

    changed = server.handle_graph_governance_parallel_branch_runtime_contract_by_context(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context.runtime_context_id,
            },
            "mf_sub",
            query={
                "parent_task_id": "runtime-contract-revision-parent",
                "fence_token": "fence-revision",
            },
        )
    )
    changed_view = changed["runtime_contract"]
    assert changed_view["runtime_context"]["task_id"] == "runtime-contract-revision-task"
    assert changed_view["latest_revision_id"] == "crev-1"
    assert changed_view["known_revision_id"] == ""
    assert changed_view["contract_changed"] is True
    assert changed_view["must_ack_revision"] is True
    assert changed_view["latest_revision"]["payload"]["nested"]["visible"] == "kept"

    no_change = server.handle_graph_governance_parallel_branch_runtime_contract_by_context(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context.runtime_context_id,
            },
            "mf_sub",
            query={
                "parent_task_id": "runtime-contract-revision-parent",
                "fence_token": "fence-revision",
                "known_revision_id": "crev-1",
                "poll_after_sec": "9",
            },
        )
    )
    no_change_view = no_change["runtime_contract"]
    assert no_change_view["latest_revision_id"] == "crev-1"
    assert no_change_view["known_revision_id"] == "crev-1"
    assert no_change_view["contract_changed"] is False
    assert no_change_view["must_ack_revision"] is False
    assert no_change_view["poll_after_sec"] == 9

    status, appended_waiver = (
        server.handle_graph_governance_parallel_branch_runtime_contract_revision_append(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "task_id": "runtime-contract-revision-task",
                },
                "observer",
                method="POST",
                body={
                    "revision_id": "crev-2",
                    "runtime_context_id": context.runtime_context_id,
                    "contract_revision": {"summary": "explicit waiver redirect"},
                    "route_waiver": _route_waiver(
                        "append_contract_revision",
                        task_id="runtime-contract-revision-task",
                        backlog_id="AC-CONTRACT-RUNTIME-REVISION-POLLING-DOGFOOD-20260603",
                    ),
                    "now_iso": "2026-06-03T10:02:00Z",
                },
            )
        )
    )
    assert status == 201
    assert appended_waiver["revision"]["revision_id"] == "crev-2"
    assert appended_waiver["revision"]["route_evidence_type"] == "route_waiver"

    changed_again = server.handle_graph_governance_parallel_branch_runtime_contract_by_context(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context.runtime_context_id,
            },
            "mf_sub",
            query={
                "parent_task_id": "runtime-contract-revision-parent",
                "fence_token": "fence-revision",
                "known_revision_id": "crev-1",
            },
        )
    )
    changed_again_view = changed_again["runtime_contract"]
    assert changed_again_view["latest_revision_id"] == "crev-2"
    assert changed_again_view["known_revision_id"] == "crev-1"
    assert changed_again_view["contract_changed"] is True


def test_parallel_branch_runtime_context_contract_route_rejects_wrong_fence(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-context-wrong-fence-task",
            root_task_id="runtime-context-wrong-fence-parent",
            branch_ref="refs/heads/codex/runtime-context-wrong-fence-task",
            worktree_path="/repo/.worktrees/runtime-context-wrong-fence-task",
            base_commit="base123",
            target_head_commit="target123",
            merge_queue_id="mq-runtime",
            fence_token="fence-current",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-03T10:00:00Z",
    )

    with pytest.raises(GovernanceError) as exc_info:
        server.handle_graph_governance_parallel_branch_runtime_contract_by_context(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "runtime_context_id": context.runtime_context_id,
                },
                "mf_sub",
                query={
                    "parent_task_id": "runtime-context-wrong-fence-parent",
                    "fence_token": "fence-stale",
                },
            )
        )
    assert exc_info.value.code == "fence_invalidated_or_unknown"
    assert exc_info.value.status == 403


def test_runtime_context_canonical_read_routes_use_runtime_context_facade(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-canonical-read-task",
            root_task_id="runtime-canonical-read-parent",
            backlog_id="AC-RUNTIME-CANONICAL-READ",
            branch_ref="refs/heads/codex/runtime-canonical-read-task",
            worktree_path="/repo/.worktrees/runtime-canonical-read-task",
            base_commit="base-canonical",
            target_head_commit="target-canonical",
            merge_queue_id="mq-canonical",
            fence_token="fence-canonical",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-15T00:00:00Z",
    )

    def resolve(path: str):
        handler_obj = object.__new__(server.GovernanceHandler)
        handler_obj.path = path
        route_handler, params, _unused = server.GovernanceHandler._find_handler(
            handler_obj,
            "GET",
        )
        assert route_handler is not None
        return route_handler, params

    current_handler, current_params = resolve(
        f"/api/graph-governance/{PID}/runtime-contexts/"
        f"{context.runtime_context_id}/current-state"
    )
    assert (
        current_handler
        is server.handle_graph_governance_parallel_branch_runtime_context_current_state
    )
    current = current_handler(_ctx_with_role(current_params, "observer"))
    assert current["ok"] is True
    assert current["runtime_context_id"] == context.runtime_context_id
    assert current["task_id"] == "runtime-canonical-read-task"

    contract_handler, contract_params = resolve(
        f"/api/graph-governance/{PID}/runtime-contexts/"
        f"{context.runtime_context_id}/runtime-contract"
    )
    assert (
        contract_handler
        is server.handle_graph_governance_parallel_branch_runtime_contract_by_context
    )
    contract = contract_handler(_ctx_with_role(contract_params, "observer"))
    assert contract["ok"] is True
    assert contract["runtime_contract"]["runtime_context"]["task_id"] == (
        "runtime-canonical-read-task"
    )

    guide_handler, guide_params = resolve(
        f"/api/graph-governance/{PID}/runtime-contexts/"
        f"{context.runtime_context_id}/worker-guide"
    )
    assert (
        guide_handler
        is server.handle_graph_governance_parallel_branch_runtime_context_worker_guide
    )
    guide = guide_handler(_ctx_with_role(guide_params, "observer"))
    assert guide["ok"] is True
    assert guide["schema_version"] == "runtime_context.worker_guide_response.v1"
    assert guide["worker_guide"]["runtime_context_id"] == context.runtime_context_id


def test_runtime_context_current_state_route_role_filters_worker_view(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-current-task",
            root_task_id="runtime-current-parent",
            backlog_id="AC-RUNTIME-CURRENT",
            worker_id="worker-runtime-current",
            worker_slot_id="worker-runtime-current",
            actual_host_worker_id="worker-runtime-current",
            agent_id="agent-runtime-current",
            allocation_owner="agent-runtime-current",
            governance_project_id=PID,
            target_project_id=PID,
            branch_ref="refs/heads/codex/runtime-current-task",
            worktree_path="/repo/.worktrees/runtime-current-task",
            base_commit="base-current",
            head_commit="head-current",
            target_head_commit="target-current",
            snapshot_id="scope-current",
            projection_id="semproj-current",
            merge_queue_id="mq-current",
            fence_token="fence-current",
            session_token_hash=mf_subagent_session_token_hash(
                "runtime-current-session"
            ),
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-06T10:00:00Z",
    )
    append_branch_contract_revision(
        conn,
        context,
        revision_id="crev-current",
        contract_version="mf_parallel.v1",
        payload={
            "target_files": ["agent/governance/server.py"],
            "acceptance_criteria": ["runtime context current state is exposed"],
            "raw_private_context": "must-not-leak",
        },
        route_identity={
            "route_id": "route-current",
            "route_context_hash": "sha256:route-current",
            "prompt_contract_id": "rprompt-current",
            "prompt_contract_hash": "sha256:prompt-current",
            "visible_injection_manifest_hash": "sha256:visible-current",
            "route_token_ref": "rtok-current",
            "route_token": "raw-route-token-current",
            "raw_private_context": "must-not-leak",
        },
        now_iso="2026-06-06T10:01:00Z",
    )
    startup = task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-current-task",
        backlog_id="AC-RUNTIME-CURRENT",
        event_type="mf_subagent.startup",
        event_kind="mf_subagent_startup",
        phase="startup_gate",
        status="passed",
        payload={
            "mf_subagent_startup_gate": {
                "runtime_context_id": context.runtime_context_id,
                "fence_token": "fence-current",
                "raw_private_context": "must-not-leak",
            }
        },
    )
    read_receipt = task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-current-task",
        backlog_id="AC-RUNTIME-CURRENT",
        event_type="mf_subagent_read_receipt",
        event_kind="mf_subagent_read_receipt",
        phase="startup_read_receipt",
        status="ok",
        payload={"read_receipt_hash": "sha256:read-current"},
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id="gqt-runtime-current",
        parent_task_id="runtime-current-parent",
        runtime_context_id=context.runtime_context_id,
        task_id="runtime-current-task",
        worker_role="mf_sub",
        fence_token="fence-current",
        run_id=_mf_sub_run_id("runtime-current-task", "fence-current"),
    )
    conn.commit()

    observer_result = (
        server.handle_graph_governance_parallel_branch_runtime_context_current_state(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "runtime_context_id": context.runtime_context_id,
                },
                "observer",
                query={"graph_trace_id": "gqt-runtime-current", "view": "all"},
            )
        )
    )

    assert observer_result["ok"] is True
    assert observer_result["role_scope"] == "observer"
    assert "fence-current" not in json.dumps(observer_result, sort_keys=True)
    observer_views = observer_result["runtime_context_service"]["views"]
    assert {
        "current",
        "gate_inputs",
        "action_plan",
        "control_plane",
        "capability_boundary",
        "worker_view",
        "close_gate_view",
    }.issubset(observer_views)
    current = observer_views["current"]
    assert current["route_identity"]["route_context_hash"] == "sha256:route-current"
    assert current["route_identity"]["prompt_contract_hash"] == "sha256:prompt-current"
    assert current["timeline_refs"]["startup_event_ref"] == f"timeline:{startup['id']}"
    assert current["timeline_refs"]["read_receipt_event_ref"] == (
        f"timeline:{read_receipt['id']}"
    )
    assert current["graph_trace_refs"]["trace_ids"] == ["gqt-runtime-current"]
    control_plane = observer_views["control_plane"]
    capability_boundary = observer_views["capability_boundary"]
    assert control_plane["schema_version"] == "runtime_context.control_plane.v1"
    assert control_plane["next_legal_action"] == "record_finish_gate"
    assert capability_boundary["schema_version"] == "runtime_context.capability_boundary.v1"
    assert capability_boundary["owned_files"] == ["agent/governance/server.py"]
    assert capability_boundary["fence_token_hash"] == (
        "sha256:" + hashlib.sha256(b"fence-current").hexdigest()
    )
    assert capability_boundary["raw_fence_token_exposed"] is False
    assert control_plane["capability_boundary_hash"] == (
        capability_boundary["capability_boundary_hash"]
    )
    assert control_plane["route_token_action"]["status"] == "present"
    assert control_plane["route_token_action"]["next_action"] == "none"
    assert control_plane["route_token_action"]["source_event_lineage"][
        "route_token_ref"
    ] == "rtok-current"
    assert control_plane["route_token_action"]["source_event_lineage"][
        "raw_route_token_required"
    ] is False
    assert control_plane["route_token_action"]["source_event_lineage"][
        "next_action"
    ] == "append_protected_evidence_with_route_token_ref_or_route_owned_source_event"
    assert control_plane["route_token_action"]["entrypoint"]["path"] == (
        "/api/projects/{project_id}/observer/route-context/issue"
    )
    assert control_plane["route_token_action"]["entrypoint"][
        "required_public_fields"
    ] == ["backlog_id", "task_id", "target_files", "caller_role"]
    assert control_plane["read_receipt_hash_action"]["status"] == "present"
    assert "must-not-leak" not in json.dumps(observer_result, sort_keys=True)
    assert "raw-route-token-current" not in json.dumps(observer_result, sort_keys=True)
    observer_content_address = observer_result["runtime_context_service"][
        "content_address"
    ]
    assert observer_content_address["schema_version"] == (
        "runtime_context.content_address.v1"
    )
    assert observer_content_address["projection_hash"].startswith("sha256:")
    assert set(observer_content_address["nodes"]) == {
        "action_plan",
        "capability_boundary",
        "control_plane",
        "current",
        "gate_inputs",
        "worker_view",
        "close_gate_view",
        "observer_view",
        "qa_view",
        "judge_view",
    }
    observer_audit = observer_result["access_audit"]
    assert observer_audit["schema_version"] == "runtime_context.access_audit.v1"
    assert observer_audit["projection_hash"] == observer_content_address[
        "projection_hash"
    ]
    assert {node["view"] for node in observer_audit["nodes_read"]} == set(
        observer_content_address["nodes"]
    )
    observer_audit_row = conn.execute(
        """
        SELECT principal_id, role, view_name, projection_hash, nodes_read_json,
               metadata_json
        FROM parallel_branch_runtime_access_audit
        WHERE audit_id = ?
        """,
        (observer_audit["audit_id"],),
    ).fetchone()
    assert observer_audit_row is not None
    assert observer_audit_row["principal_id"] == "observer-principal"
    assert observer_audit_row["role"] == "observer"
    assert observer_audit_row["view_name"] == "all"
    assert observer_audit_row["projection_hash"] == observer_audit["projection_hash"]
    assert "must-not-leak" not in observer_audit_row["nodes_read_json"]
    assert "must-not-leak" not in observer_audit_row["metadata_json"]

    audit_count_before_denied = conn.execute(
        "SELECT COUNT(*) FROM parallel_branch_runtime_access_audit"
    ).fetchone()[0]
    with pytest.raises(GovernanceError) as exc_info:
        server.handle_graph_governance_parallel_branch_runtime_context_current_state(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "runtime_context_id": context.runtime_context_id,
                },
                "mf_sub",
                query={
                    "parent_task_id": "runtime-current-parent",
                    "fence_token": "fence-current",
                    "session_token": "wrong-runtime-current-session",
                    "graph_trace_id": "gqt-runtime-current",
                },
            )
        )
    assert exc_info.value.status == 403
    audit_count_after_denied = conn.execute(
        "SELECT COUNT(*) FROM parallel_branch_runtime_access_audit"
    ).fetchone()[0]
    assert audit_count_after_denied == audit_count_before_denied

    worker_result = (
        server.handle_graph_governance_parallel_branch_runtime_context_current_state(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "runtime_context_id": context.runtime_context_id,
                },
                "mf_sub",
                query={
                    "parent_task_id": "runtime-current-parent",
                    "fence_token": "fence-current",
                    "session_token": "runtime-current-session",
                    "graph_trace_id": "gqt-runtime-current",
                    "view": "all",
                },
            )
        )
    )

    assert worker_result["ok"] is True
    assert worker_result["role_scope"] == "worker"
    worker_views = worker_result["runtime_context_service"]["views"]
    assert set(worker_views) == {"worker_view"}
    worker_view = worker_views["worker_view"]
    assert worker_view["schema_version"] == "runtime_context.worker_view.v1"
    assert worker_view["task"]["task_id"] == "runtime-current-task"
    fence_hash = "sha256:" + hashlib.sha256(b"fence-current").hexdigest()
    assert "fence_token" not in worker_view["task"]
    assert worker_view["task"]["fence_token_hash"] == fence_hash
    assert worker_view["task"]["fence_token_redacted"] is True
    assert "fence_token" not in worker_view["graph_query_identity"]
    assert worker_view["graph_query_identity"]["fence_token_hash"] == fence_hash
    assert worker_view["graph_query_identity"]["fence_token_redacted"] is True
    assert worker_view["gate_inputs"]["gates"]["dispatch"]["fields"]["fence_token"][
        "value"
    ] == "redacted"
    assert worker_view["gate_inputs"]["gates"]["dispatch"]["fields"]["fence_token"][
        "fence_token_hash"
    ] == fence_hash
    assert worker_view["role_filter_policy"]["raw_private_context_exposed"] is False
    assert worker_view["privacy_boundary"]["other_worker_contexts_exposed"] is False
    assert worker_view["action_plan"]["schema_version"] == "runtime_context.action_plan.v1"
    assert worker_view["control_plane"]["schema_version"] == (
        "runtime_context.control_plane.v1"
    )
    assert worker_view["control_plane"]["route_token_action"]["status"] == "present"
    assert worker_view["control_plane"]["route_token_action"]["next_action"] == "none"
    assert worker_view["control_plane"]["route_token_action"]["source_event_lineage"][
        "route_token_ref"
    ] == "rtok-current"
    assert worker_view["control_plane"]["route_token_action"]["source_event_lineage"][
        "raw_route_token_required"
    ] is False
    assert worker_view["control_plane"]["read_receipt_hash_action"]["status"] == (
        "present"
    )
    next_required = worker_view["next_required_evidence"]
    assert worker_view["action_plan"]["next_required_evidence"] == next_required
    assert worker_view["control_plane"]["next_required_evidence"] == next_required
    assert [item["id"] for item in next_required[:2]] == [
        "worker_self_attestation",
        "finish_gate",
    ]
    assert next_required[0]["next_action"] == "record_worker_self_attestation"
    assert next_required[1]["runtime_context_id"] == context.runtime_context_id
    assert "current_values" not in worker_view
    assert "fence-current" not in json.dumps(worker_result, sort_keys=True)
    assert "runtime-current-session" not in json.dumps(worker_result, sort_keys=True)
    assert "raw-route-token-current" not in json.dumps(worker_result, sort_keys=True)
    assert "must-not-leak" not in json.dumps(worker_result, sort_keys=True)
    worker_guide_result = (
        server.handle_graph_governance_parallel_branch_runtime_context_worker_guide(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "runtime_context_id": context.runtime_context_id,
                },
                "mf_sub",
                query={
                    "parent_task_id": "runtime-current-parent",
                    "fence_token": "fence-current",
                    "session_token": "runtime-current-session",
                    "graph_trace_id": "gqt-runtime-current",
                    "view": "current",
                },
            )
        )
    )
    assert worker_guide_result["ok"] is True
    assert worker_guide_result["role_scope"] == "worker"
    assert worker_guide_result["schema_version"] == (
        "runtime_context.worker_guide_response.v1"
    )
    worker_guide = worker_guide_result["worker_guide"]
    assert worker_guide["schema_version"] == "runtime_context.worker_guide.v1"
    assert worker_guide["next_legal_action"] == "record_finish_gate"
    assert worker_guide["read_endpoints"]["current_state"]["path"] == (
        "/api/graph-governance/{project_id}/runtime-contexts/"
        "{runtime_context_id}/current-state"
    )
    assert worker_guide["read_endpoints"]["graph_query"]["path"] == (
        "/api/graph-governance/{project_id}/query"
    )
    assert worker_guide["read_endpoints"]["graph_query"]["query_source"] == (
        "mf_subagent"
    )
    assert worker_guide["read_endpoints"]["route_context"]["source_view_path"] == (
        "runtime_context_service.views.worker_view.route_identity"
    )
    assert worker_guide["write_guides"]["startup"]["legacy_bridge"]["path"] == (
        "/api/graph-governance/{project_id}/parallel-branches/startup"
    )
    assert worker_guide["write_guides"]["startup"]["canonical_facade_status"] == (
        "planned"
    )
    assert worker_guide["write_guides"]["finish_gate"]["legacy_bridge"]["path"] == (
        "/api/graph-governance/{project_id}/parallel-branches/finish-gate"
    )
    assert worker_guide["graph_query_identity"]["fence_token_hash"] == fence_hash
    assert worker_guide["graph_query_identity"]["fence_token_redacted"] is True
    assert "surrogate_startup" in worker_guide["blocked_actions"]
    assert "bypass_timeline_gate" in worker_guide["blocked_actions"]
    worker_guide_json = json.dumps(worker_guide_result, sort_keys=True)
    assert "fence-current" not in worker_guide_json
    assert "runtime-current-session" not in worker_guide_json
    assert "raw-route-token-current" not in worker_guide_json
    assert "must-not-leak" not in worker_guide_json
    worker_content_address = worker_result["runtime_context_service"][
        "content_address"
    ]
    assert worker_content_address["projection_hash"].startswith("sha256:")
    assert set(worker_content_address["nodes"]) == {
        "capability_boundary",
        "worker_view",
    }
    worker_audit = worker_result["access_audit"]
    assert worker_audit["projection_hash"].startswith("sha256:")
    assert {node["view"] for node in worker_audit["nodes_read"]} == {
        "capability_boundary",
        "worker_view",
    }
    worker_audit_row = conn.execute(
        """
        SELECT principal_id, role, view_name, projection_hash, nodes_read_json,
               metadata_json
        FROM parallel_branch_runtime_access_audit
        WHERE audit_id = ?
        """,
        (worker_audit["audit_id"],),
    ).fetchone()
    assert worker_audit_row is not None
    assert worker_audit_row["principal_id"] == "mf_sub-principal"
    assert worker_audit_row["role"] == "mf_sub"
    assert worker_audit_row["view_name"] == "worker_view"
    assert {
        node["view"] for node in json.loads(worker_audit_row["nodes_read_json"])
    } == {"capability_boundary", "worker_view"}
    assert "runtime-current-session" not in worker_audit_row["metadata_json"]
    assert "fence-current" not in worker_audit_row["metadata_json"]
    assert "must-not-leak" not in worker_audit_row["metadata_json"]


def test_runtime_context_current_state_route_folds_lane_plan_from_timeline_events(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-lane-plan-task",
            root_task_id="AC-RUNTIME-LANE-PLAN",
            backlog_id="AC-RUNTIME-LANE-PLAN",
            worker_id="worker-runtime-lane-plan",
            worker_slot_id="worker-runtime-lane-plan",
            actual_host_worker_id="worker-runtime-lane-plan",
            agent_id="agent-runtime-lane-plan",
            allocation_owner="agent-runtime-lane-plan",
            governance_project_id=PID,
            target_project_id=PID,
            branch_ref="refs/heads/codex/runtime-lane-plan-task",
            worktree_path="/repo/.worktrees/runtime-lane-plan-task",
            base_commit="base-lane-plan",
            head_commit="head-lane-plan",
            target_head_commit="target-lane-plan",
            snapshot_id="scope-lane-plan",
            projection_id="semproj-lane-plan",
            merge_queue_id="mq-lane-plan",
            fence_token="fence-lane-plan",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-06T10:00:00Z",
    )
    append_branch_contract_revision(
        conn,
        context,
        revision_id="crev-lane-plan",
        contract_version="mf_parallel.v1",
        payload={
            "target_files": ["agent/governance/server.py"],
            "acceptance_criteria": ["lane plan is folded from stored events"],
        },
        route_identity={
            "route_id": "route-lane-plan",
            "route_context_hash": "sha256:route-lane-plan",
            "prompt_contract_id": "rprompt-lane-plan",
            "prompt_contract_hash": "sha256:prompt-lane-plan",
        },
        now_iso="2026-06-06T10:01:00Z",
    )
    recorded_events = []
    for event_kind in (
        "route_context",
        "route_action_precheck",
        "bounded_implementation_worker_dispatch",
        "mf_subagent_startup",
        "mf_subagent_read_receipt",
        "verification",
    ):
        payload = {"runtime_context_id": context.runtime_context_id}
        if event_kind == "mf_subagent_read_receipt":
            payload["required_evidence"] = ["runtime_context_read_receipt"]
        recorded_events.append(
            task_timeline.record_event(
                conn,
                project_id=PID,
                task_id="runtime-lane-plan-task",
                backlog_id="AC-RUNTIME-LANE-PLAN",
                event_type=event_kind,
                event_kind=event_kind,
                phase=event_kind,
                status="passed",
                payload=payload,
            )
        )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-lane-plan-sibling",
        backlog_id="AC-RUNTIME-LANE-PLAN",
        event_type="close_ready",
        event_kind="close_ready",
        phase="close_ready",
        status="ready",
        payload={"runtime_context_id": "mfrctx-sibling"},
    )
    conn.commit()

    result = server.handle_graph_governance_parallel_branch_runtime_context_current_state(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context.runtime_context_id,
            },
            "observer",
            query={"view": "current"},
        )
    )

    current = result["runtime_context_service"]["views"]["current"]
    lane_plan = current["lane_plan"]
    fulfilled = {item["clause"]: item for item in lane_plan["fulfilled"]}
    missing = {item["clause"] for item in lane_plan["missing"]}

    assert lane_plan["schema_version"] == "runtime_context.lane_fold.v1"
    assert lane_plan["lane_id"] == "runtime-lane-plan-task"
    assert lane_plan["current_state"]["fulfilled_count"] == 6
    assert lane_plan["current_state"]["missing_count"] == 1
    assert lane_plan["current_state"]["status"] == "missing_required_clauses"
    assert missing == {"close_ready"}
    assert fulfilled["route_context"]["event_ref"] == str(recorded_events[0]["id"])
    assert fulfilled["independent_verification"]["event_ref"] == str(
        recorded_events[-1]["id"]
    )
    assert "runtime-lane-plan-sibling" not in json.dumps(lane_plan, sort_keys=True)


def test_runtime_context_close_gate_projects_a4_lineage_graph_traces(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-a4-task",
            root_task_id="AC-RUNTIME-A4",
            backlog_id="AC-RUNTIME-A4",
            worker_id="worker-runtime-a4",
            worker_slot_id="worker-runtime-a4",
            actual_host_worker_id="worker-runtime-a4",
            agent_id="agent-runtime-a4",
            allocation_owner="agent-runtime-a4",
            governance_project_id=PID,
            target_project_id=PID,
            branch_ref="refs/heads/codex/runtime-a4-task",
            worktree_path="/repo/.worktrees/runtime-a4-task",
            base_commit="base-a4",
            head_commit="head-a4",
            target_head_commit="target-a4",
            snapshot_id="scope-a4",
            projection_id="semproj-a4",
            merge_queue_id="mq-a4",
            checkpoint_id="ckpt-a4",
            fence_token="fence-a4",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-06T10:00:00Z",
    )
    append_branch_contract_revision(
        conn,
        context,
        revision_id="crev-a4",
        contract_version="mf_parallel.v1",
        payload={
            "target_files": ["agent/governance/server.py"],
            "acceptance_criteria": ["close gate projection is ready"],
        },
            route_identity={
                "route_id": "route-a4",
                "route_context_hash": "sha256:route-a4",
                "prompt_contract_id": "rprompt-a4",
                "prompt_contract_hash": "sha256:prompt-a4",
                "route_token_ref": "rtok-a4",
                "visible_injection_manifest_hash": "sha256:visible-a4",
            },
        now_iso="2026-06-06T10:01:00Z",
    )
    read_receipt = task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-a4-task",
        backlog_id="AC-RUNTIME-A4",
        event_type="mf_subagent_read_receipt",
        event_kind="mf_subagent_read_receipt",
        phase="startup_read_receipt",
        status="ok",
        payload={"read_receipt_hash": "sha256:read-a4"},
    )
    startup = task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-a4-task",
        backlog_id="AC-RUNTIME-A4",
        event_type="mf_subagent.startup",
        event_kind="mf_subagent_startup",
        phase="startup_gate",
        status="passed",
        payload={
            "mf_subagent_startup_gate": {
                "schema_version": "mf_subagent_startup_gate.v1",
                "gate_kind": "mf_subagent.startup",
                "status": "passed",
                "bounded": True,
                "close_satisfying": True,
                "runtime_context_id": context.runtime_context_id,
                "task_id": "runtime-a4-task",
                "worker_slot_id": "worker-runtime-a4",
                "fence_token": "fence-a4",
                "observer_command_id": "cmd-runtime-a4",
                "agent_id": "agent-runtime-a4",
                "allocation_owner": "agent-runtime-a4",
                "agent_id_match_mode": "same_as_allocation_owner",
                "session_token_evidence_type": "hash",
                    "session_token_hash": "sha256:runtime-a4-session",
                    "session_token_present": True,
                    "worker_session_id": "codex-session-runtime-a4",
                    "filer_principal": "codex-session-runtime-a4",
                    "worker_transcript_path": "/tmp/runtime-a4-transcript.jsonl",
                "harness_type": "codex",
                "worker_self_attesting": True,
                "self_attesting": True,
                "worker_self_attestation": {
                    "schema_version": "worker_transcript_self_attestation.v1",
                    "status": "passed",
                    "worker_self_attesting": True,
                    "worker_session_id": "codex-session-runtime-a4",
                    "worker_transcript_path": "/tmp/runtime-a4-transcript.jsonl",
                    "harness_type": "codex",
                    "blockers": [],
                },
            }
        },
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-a4-task",
        backlog_id="AC-RUNTIME-A4",
        event_type="mf_subagent.finish_gate",
        event_kind="mf_subagent_finish_gate",
        phase="finish_gate",
        status="passed",
        payload={"graph_trace_ids": ["gqt-finish-payload"]},
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-a4-task",
        backlog_id="AC-RUNTIME-A4",
        event_type="verification",
        event_kind="verification",
        phase="verification",
        status="passed",
        payload={"graph_query_trace_ids": ["gqt-verification"]},
    )
    close_ready = task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-a4-task",
        backlog_id="AC-RUNTIME-A4",
        event_type="close_ready",
        event_kind="close_ready",
        phase="close_ready",
        status="ready",
        payload={"graph_trace_ids": ["gqt-close-ready"]},
    )
    for trace_id, created_at in (
        ("gqt-finish-payload", "2026-06-06T10:05:00Z"),
        ("gqt-verification", "2026-06-06T10:04:00Z"),
        ("gqt-close-ready", "2026-06-06T10:03:00Z"),
        ("gqt-from-db", "2026-06-06T10:02:00Z"),
    ):
        _insert_mf_sub_graph_query_trace(
            conn,
            trace_id=trace_id,
            parent_task_id="AC-RUNTIME-A4",
            snapshot_id="scope-a4",
            runtime_context_id=context.runtime_context_id,
            task_id="runtime-a4-task",
            worker_role="mf_sub",
            fence_token="fence-a4",
            run_id=_mf_sub_run_id("runtime-a4-task", "fence-a4"),
            created_at=created_at,
        )
    conn.commit()

    result = server.handle_graph_governance_parallel_branch_runtime_context_current_state(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context.runtime_context_id,
            },
            "observer",
            query={"view": "all"},
        )
    )

    views = result["runtime_context_service"]["views"]
    current = views["current"]
    close_gate = views["close_gate_view"]
    gate_inputs = views["gate_inputs"]

    assert current["timeline_refs"]["startup_event_ref"] == f"timeline:{startup['id']}"
    assert current["timeline_refs"]["read_receipt_event_ref"] == (
        f"timeline:{read_receipt['id']}"
    )
    assert current["timeline_refs"]["startup_event_ref"] != (
        current["timeline_refs"]["read_receipt_event_ref"]
    )
    assert current["timeline_refs"]["close_ready_event_ref"] == (
        f"timeline:{close_ready['id']}"
    )
    assert current["graph_trace_refs"]["trace_ids"] == [
        "gqt-finish-payload",
        "gqt-verification",
        "gqt-close-ready",
        "gqt-from-db",
    ]
    assert close_gate["ready"] is True, close_gate
    assert close_gate["graph_trace_ids"] == current["graph_trace_refs"]["trace_ids"]
    assert close_gate["evidence_refs"]["route_identity"]["route_id"] == "route-a4"
    assert close_gate["evidence_refs"]["close_evidence"]["payload"]["event_id"] == (
        f"timeline:{close_ready['id']}"
    )
    assert (
        gate_inputs["gates"]["close"]["fields"]["graph_trace_ids"]["producer"]
        == "graph_query_trace"
    )
    assert gate_inputs["evidence_refs"]["graph_trace"]["source"] == (
        "graph_query_traces"
    )


def test_runtime_context_graph_trace_projection_excludes_sibling_mf_sub_rows(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-task-a",
            root_task_id="AC-RUNTIME-SHARED",
            backlog_id="AC-RUNTIME-SHARED",
            worker_id="worker-task-a",
            worker_slot_id="worker-task-a",
            actual_host_worker_id="worker-task-a",
            agent_id="agent-task-a",
            allocation_owner="agent-task-a",
            governance_project_id=PID,
            target_project_id=PID,
            branch_ref="refs/heads/codex/runtime-task-a",
            worktree_path="/repo/.worktrees/runtime-task-a",
            base_commit="base-a",
            head_commit="head-a",
            target_head_commit="target-a",
            snapshot_id="scope-shared",
            projection_id="semproj-shared",
            merge_queue_id="mq-shared",
            checkpoint_id="ckpt-a",
            fence_token="fence-task-a",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-06T10:00:00Z",
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id="gqt-current-runtime",
        parent_task_id="AC-RUNTIME-SHARED",
        runtime_context_id=context.runtime_context_id,
        task_id="runtime-task-a",
        worker_role="mf_sub",
        fence_token="fence-task-a",
        run_id=_mf_sub_run_id("runtime-task-a", "fence-task-a"),
        created_at="2026-06-06T10:01:00Z",
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id="gqt-current-task",
        parent_task_id="AC-RUNTIME-SHARED",
        runtime_context_id=context.runtime_context_id,
        task_id="runtime-task-a",
        worker_role="mf_sub",
        fence_token="fence-task-a",
        run_id=_mf_sub_run_id("runtime-task-a", "fence-task-a"),
        created_at="2026-06-06T10:02:00Z",
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id="gqt-current-fence",
        parent_task_id="AC-RUNTIME-SHARED",
        runtime_context_id=context.runtime_context_id,
        task_id="runtime-task-a",
        worker_role="mf_sub",
        fence_token="fence-task-a",
        run_id=_mf_sub_run_id("runtime-task-a", "fence-task-a"),
        created_at="2026-06-06T10:03:00Z",
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id="gqt-current-run",
        parent_task_id="AC-RUNTIME-SHARED",
        runtime_context_id=context.runtime_context_id,
        task_id="runtime-task-a",
        worker_role="mf_sub",
        fence_token="fence-task-a",
        run_id=_mf_sub_run_id("runtime-task-a", "fence-task-a"),
        created_at="2026-06-06T10:04:00Z",
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id="gqt-sibling",
        parent_task_id="AC-RUNTIME-SHARED",
        runtime_context_id="mfrctx-sibling",
        task_id="runtime-task-b",
        worker_role="mf_sub",
        fence_token="fence-task-b",
        run_id=_mf_sub_run_id("runtime-task-b", "fence-task-b"),
        created_at="2026-06-06T10:05:00Z",
    )
    conn.commit()

    result = server.handle_graph_governance_parallel_branch_runtime_context_current_state(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context.runtime_context_id,
            },
            "observer",
            query={"view": "all"},
        )
    )

    trace_ids = result["runtime_context_service"]["views"]["current"][
        "graph_trace_refs"
    ]["trace_ids"]
    assert trace_ids == [
        "gqt-current-run",
        "gqt-current-fence",
        "gqt-current-task",
        "gqt-current-runtime",
    ]
    assert "gqt-sibling" not in trace_ids


def test_runtime_context_timeline_fallback_excludes_sibling_graph_trace_ids(conn):
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-task-a",
            root_task_id="AC-RUNTIME-SHARED",
            backlog_id="AC-RUNTIME-SHARED",
            worker_id="worker-task-a",
            worker_slot_id="worker-task-a",
            actual_host_worker_id="worker-task-a",
            agent_id="agent-task-a",
            allocation_owner="agent-task-a",
            governance_project_id=PID,
            target_project_id=PID,
            branch_ref="refs/heads/codex/runtime-task-a",
            worktree_path="/repo/.worktrees/runtime-task-a",
            base_commit="base-a",
            head_commit="head-a",
            target_head_commit="target-a",
            snapshot_id="scope-shared",
            projection_id="semproj-shared",
            merge_queue_id="mq-shared",
            checkpoint_id="ckpt-a",
            fence_token="fence-task-a",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-06T10:00:00Z",
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id="runtime-task-b",
        backlog_id="AC-RUNTIME-SHARED",
        event_type="verification",
        event_kind="verification",
        phase="verification",
        status="passed",
        payload={"graph_trace_ids": ["gqt-sibling-timeline"]},
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        backlog_id="AC-RUNTIME-SHARED",
        event_type="verification",
        event_kind="verification",
        phase="verification",
        status="passed",
        payload={"graph_trace_ids": ["gqt-backlog-unscoped"]},
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id="gqt-backlog-unscoped",
        parent_task_id="AC-RUNTIME-SHARED",
        runtime_context_id=context.runtime_context_id,
        task_id="runtime-task-a",
        worker_role="mf_sub",
        fence_token="fence-task-a",
        run_id=_mf_sub_run_id("runtime-task-a", "fence-task-a"),
    )
    conn.commit()

    result = server.handle_graph_governance_parallel_branch_runtime_context_current_state(
        _ctx_with_role(
            {
                "project_id": PID,
                "runtime_context_id": context.runtime_context_id,
            },
            "observer",
            query={"view": "all"},
        )
    )

    current = result["runtime_context_service"]["views"]["current"]
    assert result["source_refs"]["timeline"]["graph_trace_ids"] == [
        "gqt-backlog-unscoped"
    ]
    assert current["graph_trace_refs"]["trace_ids"] == ["gqt-backlog-unscoped"]
    assert "gqt-sibling-timeline" not in json.dumps(current, sort_keys=True)


def test_parallel_branch_runtime_contract_route_rejects_wrong_worker_fence(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="runtime-contract-fence-task",
            root_task_id="runtime-contract-parent",
            branch_ref="refs/heads/codex/runtime-contract-fence-task",
            worktree_path="/repo/.worktrees/runtime-contract-fence-task",
            base_commit="base123",
            target_head_commit="target123",
            merge_queue_id="mq-runtime",
            fence_token="fence-good",
            status="running",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-06-03T10:00:00Z",
    )

    with pytest.raises(GovernanceError) as exc_info:
        server.handle_graph_governance_parallel_branch_runtime_contract(
            _ctx_with_role(
                {
                    "project_id": PID,
                    "task_id": "runtime-contract-fence-task",
                },
                "mf_sub",
                query={
                    "parent_task_id": "runtime-contract-parent",
                    "fence_token": "fence-bad",
                },
            )
    )
    assert exc_info.value.code == "fence_invalidated_or_unknown"
    assert exc_info.value.status == 403


def test_parallel_branch_recover_and_checkpoint_routes_enforce_fence(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-recover",
            task_id="recover-task",
            branch_ref="refs/heads/codex/recover-task",
            status="running",
            lease_id="lease-old",
            lease_expires_at="2026-05-17T07:00:00Z",
            fence_token="fence-old",
            checkpoint_id="checkpoint-old",
            replay_source="checkpoint",
        ),
        now_iso="2026-05-17T07:00:00Z",
    )

    recovered = server.handle_graph_governance_parallel_branch_recover_expired(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "now_iso": "2026-05-17T07:10:00Z",
                "actor": "observer-test",
            },
        )
    )

    assert recovered["recovered_count"] == 1
    context = recovered["contexts"][0]
    assert context["status"] == "reclaimable"
    assert context["attempt"] == 2
    assert context["fence_token"] != "fence-old"

    with pytest.raises(BranchRuntimeFenceError):
        server.handle_graph_governance_parallel_branch_checkpoint(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "recover-task",
                    "checkpoint_id": "checkpoint-stale",
                    "fence_token": "fence-old",
                },
            )
        )

    checkpointed = server.handle_graph_governance_parallel_branch_checkpoint(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "recover-task",
                "checkpoint_id": "checkpoint-after-reclaim",
                "fence_token": context["fence_token"],
                "now_iso": "2026-05-17T07:11:00Z",
            },
        )
    )

    assert checkpointed["ok"] is True
    assert checkpointed["context"]["checkpoint_id"] == "checkpoint-after-reclaim"


def test_parallel_branch_merge_queue_route_enforces_fence_and_returns_decision(conn):
    queue_id = "mergeq-api-fenced"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-queue",
            task_id="queue-task",
            branch_ref="refs/heads/codex/queue-task",
            status="worktree_ready",
            fence_token="fence-queue-current",
            base_commit="base-queue",
            head_commit="head-queue",
            target_head_commit="target-queue",
            snapshot_id="scope-queue",
            projection_id="semproj-queue",
        ),
        now_iso="2026-05-17T07:20:00Z",
    )

    with pytest.raises(BranchRuntimeFenceError):
        server.handle_graph_governance_parallel_branch_merge_queue(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "queue-task",
                    "merge_queue_id": queue_id,
                    "fence_token": "fence-stale",
                    "route_waiver": _route_waiver("merge_queue", task_id="queue-task"),
                },
            )
        )

    queued = server.handle_graph_governance_parallel_branch_merge_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "queue-task",
                "merge_queue_id": queue_id,
                "queue_index": 1,
                "fence_token": "fence-queue-current",
                "route_waiver": _route_waiver("merge_queue", task_id="queue-task"),
                "hard_depends_on": ["foundation-task"],
                "merge_preview_id": "preview-queue",
                "now_iso": "2026-05-17T07:21:00Z",
            },
        )
    )

    assert queued["ok"] is True
    assert queued["context"]["status"] == "queued_for_merge"
    assert queued["queue_item"]["merge_preview_id"] == "preview-queue"
    assert queued["decision"]["blocked_task_ids"] == ["queue-task"]
    row = queued["decision"]["rows"][0]
    assert row["dependency_blockers"] == ["foundation-task"]
    assert row["dependency_blocker_types"] == {"foundation-task": ["hard_depends_on"]}

    read = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": "PB-api-queue",
                "merge_queue_id": queue_id,
                "limit": "5",
            },
        )
    )
    assert read["read_model"]["merge_queue"]["blocked_task_ids"] == ["queue-task"]
    assert read["read_model"]["branch_lanes"][0]["merge_queue_id"] == queue_id


def test_parallel_branch_merge_queue_requires_route_token_or_waiver(conn):
    queue_id = "mergeq-api-route-token"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-route-token",
            task_id="route-token-task",
            branch_ref="refs/heads/codex/route-token-task",
            status="worktree_ready",
            fence_token="fence-route-token",
            base_commit="base-route-token",
            head_commit="head-route-token",
            target_head_commit="target-route-token",
        ),
        now_iso="2026-05-17T07:21:30Z",
    )

    with pytest.raises(GovernanceError, match="route_token"):
        server.handle_graph_governance_parallel_branch_merge_queue(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "route-token-task",
                    "merge_queue_id": queue_id,
                    "fence_token": "fence-route-token",
                },
            )
        )


def test_parallel_branch_checkpoint_refreshes_worktree_head_before_merge_queue(conn, tmp_path):
    repo = _git_repo(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "README.md").write_text("# worker change\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "worker change"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    queue_id = "mergeq-api-refresh-head"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-refresh-head",
            task_id="refresh-head-task",
            branch_ref="refs/heads/codex/refresh-head-task",
            status="worktree_ready",
            fence_token="fence-refresh-head",
            worktree_path=str(repo),
            base_commit=base,
            head_commit=base,
            target_head_commit=base,
        ),
        now_iso="2026-05-17T07:22:00Z",
    )

    checkpointed = server.handle_graph_governance_parallel_branch_checkpoint(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "refresh-head-task",
                "checkpoint_id": "ckpt-refresh-head",
                "fence_token": "fence-refresh-head",
                "refresh_head_from_worktree": True,
            },
        )
    )

    assert checkpointed["context"]["head_commit"] == head

    queued = server.handle_graph_governance_parallel_branch_merge_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "refresh-head-task",
                "merge_queue_id": queue_id,
                "fence_token": "fence-refresh-head",
                "route_waiver": _route_waiver("merge_queue", task_id="refresh-head-task"),
            },
        )
    )

    assert queued["ok"] is True
    assert queued["queue_item"]["branch_head"] == head


def test_parallel_branch_finish_gate_records_validated_checkpoint(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-finish",
            task_id="finish-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/finish-task",
            status="worktree_ready",
            fence_token="fence-finish-current",
            worktree_path="/tmp/nonexistent-finish-task",
            base_commit="base-finish",
            head_commit="base-finish",
            target_head_commit="target-finish",
            merge_queue_id="mergeq-api-finish",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )
    _record_finish_startup_event(
        conn,
        task_id="finish-task",
        backlog_id="FEAT-FINISH-GATE",
        fence_token="fence-finish-current",
        worktree_path="/tmp/nonexistent-finish-task",
        branch_ref="refs/heads/codex/finish-task",
        head_commit="head-finish",
    )

    finished = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "project_id": PID,
                "task_id": "finish-task",
                "backlog_id": "FEAT-FINISH-GATE",
                "branch_ref": "refs/heads/codex/finish-task",
                "worktree_path": "/tmp/nonexistent-finish-task",
                "base_commit": "base-finish",
                "target_head_commit": "target-finish",
                "head_commit": "head-finish",
                "status": "succeeded",
                "changed_files": ["agent/governance/server.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-gate",
                "fence_token": "fence-finish-current",
                "agent_id": "codex-subagent-1",
                "now_iso": "2026-05-17T07:31:00Z",
                "evidence": _finish_gate_evidence(
                    fence_token="fence-finish-current",
                    worktree_path="/tmp/nonexistent-finish-task",
                    branch_ref="refs/heads/codex/finish-task",
                    head_commit="head-finish",
                ),
            },
        )
    )

    assert finished["ok"] is True
    assert finished["gate"]["checkpoint_id"] == "ckpt-finish-gate"
    assert finished["gate"]["validated_head_commit"] == "head-finish"
    assert finished["context"]["checkpoint_id"] == "ckpt-finish-gate"
    assert finished["context"]["replay_source"] == "mf_sub_finish_gate"
    assert finished["context"]["status"] == "validated"
    assert finished["context"]["head_commit"] == "head-finish"


@pytest.mark.parametrize("worker_status", ["succeeded", "review_ready"])
def test_parallel_branch_finish_gate_accepts_mf_sub_session(conn, worker_status):
    suffix = worker_status.replace("_", "-")
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id=f"PB-api-finish-mf-sub-{suffix}",
            task_id=f"finish-mf-sub-task-{suffix}",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref=f"refs/heads/codex/finish-mf-sub-task-{suffix}",
            status="worktree_ready",
            fence_token=f"fence-finish-mf-sub-{suffix}",
            worktree_path=f"/tmp/nonexistent-finish-mf-sub-task-{suffix}",
            base_commit="base-finish",
            head_commit="base-finish",
            target_head_commit="target-finish",
            merge_queue_id=f"mergeq-api-finish-mf-sub-{suffix}",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )
    _record_finish_startup_event(
        conn,
        task_id=f"finish-mf-sub-task-{suffix}",
        backlog_id="FEAT-FINISH-GATE",
        fence_token=f"fence-finish-mf-sub-{suffix}",
        worktree_path=f"/tmp/nonexistent-finish-mf-sub-task-{suffix}",
        branch_ref=f"refs/heads/codex/finish-mf-sub-task-{suffix}",
        head_commit=f"head-finish-mf-sub-{suffix}",
        nested_key="bounded_startup_evidence",
    )

    finished = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "project_id": PID,
                "task_id": f"finish-mf-sub-task-{suffix}",
                "status": worker_status,
                "changed_files": ["agent/governance/server.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": f"ckpt-finish-mf-sub-{suffix}",
                "fence_token": f"fence-finish-mf-sub-{suffix}",
                "head_commit": f"head-finish-mf-sub-{suffix}",
                "agent_id": "codex-subagent-mf-sub",
                "evidence": _finish_gate_evidence(
                    fence_token=f"fence-finish-mf-sub-{suffix}",
                    worktree_path=f"/tmp/nonexistent-finish-mf-sub-task-{suffix}",
                    branch_ref=f"refs/heads/codex/finish-mf-sub-task-{suffix}",
                    head_commit=f"head-finish-mf-sub-{suffix}",
                    nested_key="bounded_startup_evidence",
                ),
            },
        )
    )

    assert finished["ok"] is True
    assert finished["gate"]["merge_queue_ready"] is True
    assert finished["context"]["checkpoint_id"] == f"ckpt-finish-mf-sub-{suffix}"
    assert finished["context"]["replay_source"] == "mf_sub_finish_gate"


def test_parallel_branch_startup_records_timeline_and_running_context(conn, tmp_path):
    worktree = tmp_path / "worker-startup"
    worktree.mkdir()
    runtime_context = BranchTaskRuntimeContext(
        project_id=PID,
        batch_id="PB-api-startup",
        task_id="startup-mf-sub-task",
        root_task_id="startup-parent",
        stage_task_id="startup-mf-sub-task",
        backlog_id="FEAT-STARTUP-GATE",
        branch_ref="refs/heads/codex/startup-mf-sub-task",
        status="worktree_ready",
        worker_id="startup-worker",
        agent_id="startup-agent",
        fence_token="fence-startup-mf-sub",
        worktree_path=str(worktree),
        base_commit="base-startup",
        head_commit="base-startup",
        target_head_commit="target-startup",
        merge_queue_id="mergeq-api-startup",
        session_token_hash=mf_subagent_session_token_hash("api-startup-token"),
    )
    upsert_branch_context(
        conn,
        runtime_context,
        now_iso="2026-06-03T07:30:00Z",
    )

    started = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "startup-mf-sub-task",
                "parent_task_id": "startup-parent",
                "worker_role": "mf_sub",
                "worker_id": "startup-worker",
                "agent_id": "startup-agent",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "session_token": "api-startup-token",
                "fence_token": "fence-startup-mf-sub",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/startup-mf-sub-task",
                "head_commit": "head-startup",
                "base_commit": "base-startup",
                "target_head_commit": "target-startup",
                "merge_queue_id": "mergeq-api-startup",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-startup",
                "route_context_hash": "sha256:route-startup",
                "prompt_contract_id": "rprompt-startup",
                "prompt_contract_hash": "sha256:prompt-startup",
                "route_token_ref": "rtok-startup",
                "visible_injection_manifest_hash": "sha256:visible-startup",
                "observer_command_id": "cmd-startup",
                "read_receipt_hash": "sha256:read-startup",
                "read_receipt_event_id": "2873",
            },
        )
    )

    events = task_timeline.list_events(
        conn,
        PID,
        backlog_id="FEAT-STARTUP-GATE",
        event_kind="mf_subagent_startup",
    )
    assert started["ok"] is True
    assert started["context"]["status"] == "running"
    assert started["startup_gate"]["actual_startup_recorded"] is True
    assert started["startup_gate"]["session_token_evidence_type"] == "server_verified"
    assert started["startup_gate"]["server_issued_session_token_verified"] is True
    assert started["timeline_event_recorded"]["event_kind"] == "mf_subagent_startup"
    assert len(events) == 1
    assert events[0]["payload"]["mf_subagent_startup_gate"]["worker_role"] == "mf_sub"


def test_parallel_branch_startup_blocks_missing_command_read_receipt_lineage(
    conn, tmp_path
):
    worktree = tmp_path / "worker-startup-missing-lineage"
    worktree.mkdir()
    runtime_context = BranchTaskRuntimeContext(
        project_id=PID,
        batch_id="PB-api-startup-missing-lineage",
        task_id="startup-missing-lineage-task",
        root_task_id="startup-missing-lineage-parent",
        stage_task_id="startup-missing-lineage-task",
        backlog_id="FEAT-STARTUP-LINEAGE-GATE",
        branch_ref="refs/heads/codex/startup-missing-lineage-task",
        status="worktree_ready",
        worker_id="startup-lineage-worker",
        agent_id="startup-lineage-agent",
        fence_token="fence-startup-lineage",
        worktree_path=str(worktree),
        base_commit="base-startup-lineage",
        head_commit="base-startup-lineage",
        target_head_commit="target-startup-lineage",
        merge_queue_id="mergeq-startup-lineage",
    )
    upsert_branch_context(
        conn,
        runtime_context,
        now_iso="2026-06-03T07:30:00Z",
    )

    blocked = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "startup-missing-lineage-task",
                "parent_task_id": "startup-missing-lineage-parent",
                "worker_role": "mf_sub",
                "worker_id": "startup-lineage-worker",
                "agent_id": "startup-lineage-agent",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "session_token_surrogate": "host-session:lineage",
                "fence_token": "fence-startup-lineage",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/startup-missing-lineage-task",
                "head_commit": "head-startup-lineage",
                "base_commit": "base-startup-lineage",
                "target_head_commit": "target-startup-lineage",
                "merge_queue_id": "mergeq-startup-lineage",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-startup-lineage",
                "route_context_hash": "sha256:route-startup-lineage",
                "prompt_contract_id": "rprompt-startup-lineage",
                "prompt_contract_hash": "sha256:prompt-startup-lineage",
                "route_token_ref": "rtok-startup-lineage",
                "visible_injection_manifest_hash": "sha256:visible-startup-lineage",
            },
        )
    )

    events = task_timeline.list_events(
        conn,
        PID,
        backlog_id="FEAT-STARTUP-LINEAGE-GATE",
        event_kind="mf_subagent_startup_refusal",
    )
    assert blocked["ok"] is False
    assert blocked["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )
    assert "observer_command_id" in blocked["missing_required_fields"]
    assert "read_receipt_hash" in blocked["missing_required_fields"]
    assert "read_receipt_event_id" in blocked["missing_required_fields"]
    assert blocked["next_legal_action"]["tool"] == (
        "observer_read_receipt_then_startup"
    )
    assert len(events) == 1
    refusal = events[0]["payload"]["mf_subagent_startup_refusal"]
    assert refusal["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )
    assert refusal["missing_required_fields"] == [
        "observer_command_id",
        "read_receipt_hash",
        "read_receipt_event_id",
    ]


def test_parallel_branch_startup_accepts_host_worker_surrogate_for_observer_allocation(
    conn, tmp_path
):
    worktree = tmp_path / "worker-host-startup"
    worktree.mkdir()
    runtime_context = BranchTaskRuntimeContext(
        project_id=PID,
        batch_id="PB-api-host-startup",
        task_id="host-startup-mf-sub-task",
        root_task_id="host-startup-parent",
        stage_task_id="host-startup-mf-sub-task",
        backlog_id="FEAT-HOST-STARTUP-GATE",
        branch_ref="refs/heads/codex/host-startup-mf-sub-task",
        status="worktree_ready",
        worker_id="host-startup-worker-slot",
        agent_id="observer-allocation-owner",
        allocation_owner="observer-allocation-owner",
        worker_slot_id="host-startup-worker-slot",
        fence_token="fence-host-startup-mf-sub",
        worktree_path=str(worktree),
        base_commit="base-host-startup",
        head_commit="base-host-startup",
        target_head_commit="target-host-startup",
        merge_queue_id="mergeq-api-host-startup",
    )
    upsert_branch_context(
        conn,
        runtime_context,
        now_iso="2026-06-05T04:30:00Z",
    )
    append_branch_contract_revision(
        conn,
        runtime_context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "test_registered_host_adapter_spawn",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "task_id": "host-startup-mf-sub-task",
                "worker_slot_id": "host-startup-worker-slot",
                "agent_id": "019e95fd-cec4-7c12-8abe-8acc849cd9c4",
                "actual_host_worker_id": (
                    "019e95fd-cec4-7c12-8abe-8acc849cd9c4"
                ),
                "host_startup_id": (
                    "multi_agent_v1.spawn_agent:"
                    "019e95fd-cec4-7c12-8abe-8acc849cd9c4"
                ),
                "host_session_id": (
                    "multi_agent_v1.spawn_agent:"
                    "019e95fd-cec4-7c12-8abe-8acc849cd9c4"
                ),
                "session_token_surrogate": (
                    "codex_desktop_multi_agent_v1:"
                    "019e95fd-cec4-7c12-8abe-8acc849cd9c4"
                ),
            }
        },
        route_identity={
            "route_id": "route-host-startup",
            "route_context_hash": "sha256:route-host-startup",
            "prompt_contract_id": "rprompt-host-startup",
            "prompt_contract_hash": "sha256:prompt-host-startup",
            "route_token_ref": "rtok-host-startup",
            "visible_injection_manifest_hash": "sha256:visible-host-startup",
        },
        now_iso="2026-06-05T04:31:00Z",
    )

    started = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "host-startup-mf-sub-task",
                "parent_task_id": "host-startup-parent",
                "worker_role": "mf_sub",
                "worker_id": "host-startup-worker-slot",
                "agent_id": "019e95fd-cec4-7c12-8abe-8acc849cd9c4",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "host_startup_id": (
                    "multi_agent_v1.spawn_agent:"
                    "019e95fd-cec4-7c12-8abe-8acc849cd9c4"
                ),
                "session_token_surrogate": (
                    "codex_desktop_multi_agent_v1:"
                    "019e95fd-cec4-7c12-8abe-8acc849cd9c4"
                ),
                "startup_source": "codex_desktop_multi_agent_v1.spawn_agent",
                "fence_token": "fence-host-startup-mf-sub",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/host-startup-mf-sub-task",
                "head_commit": "head-host-startup",
                "base_commit": "base-host-startup",
                "target_head_commit": "target-host-startup",
                "merge_queue_id": "mergeq-api-host-startup",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-host-startup",
                "route_context_hash": "sha256:route-host-startup",
                "prompt_contract_id": "rprompt-host-startup",
                "prompt_contract_hash": "sha256:prompt-host-startup",
                "route_token_ref": "rtok-host-startup",
                "visible_injection_manifest_hash": "sha256:visible-host-startup",
                "observer_command_id": "cmd-host-startup",
                "read_receipt_hash": "sha256:read-host-startup",
                "read_receipt_event_id": "2873",
            },
        )
    )

    assert started["ok"] is True
    assert started["startup_gate"]["allocation_owner"] == "observer-allocation-owner"
    assert (
        started["startup_gate"]["actual_host_worker_id"]
        == "019e95fd-cec4-7c12-8abe-8acc849cd9c4"
    )
    assert started["startup_gate"]["agent_id_match_mode"] == (
        "host_adapter_startup_token_surrogate"
    )
    assert started["startup_gate"]["host_adapter_startup_token_accepted"] is True


def test_parallel_branch_startup_accepts_codex_cli_host_startup_id_for_observer_allocation(
    conn, tmp_path
):
    worktree = tmp_path / "worker-codex-cli-startup"
    worktree.mkdir()
    runtime_context = BranchTaskRuntimeContext(
        project_id=PID,
        batch_id="PB-api-codex-cli-startup",
        task_id="codex-cli-startup-mf-sub-task",
        root_task_id="codex-cli-startup-parent",
        stage_task_id="codex-cli-startup-mf-sub-task",
        backlog_id="FEAT-HOST-STARTUP-GATE",
        branch_ref="refs/heads/codex/codex-cli-startup-mf-sub-task",
        status="worktree_ready",
        worker_id="codex-cli-worker-slot",
        agent_id="codex_observer_subagent",
        allocation_owner="codex_observer_subagent",
        worker_slot_id="codex-cli-worker-slot",
        fence_token="fence-codex-cli-startup-mf-sub",
        worktree_path=str(worktree),
        base_commit="base-codex-cli-startup",
        head_commit="base-codex-cli-startup",
        target_head_commit="target-codex-cli-startup",
        merge_queue_id="mergeq-api-codex-cli-startup",
    )
    upsert_branch_context(
        conn,
        runtime_context,
        now_iso="2026-06-05T20:45:00Z",
    )
    append_branch_contract_revision(
        conn,
        runtime_context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "test_registered_host_adapter_spawn",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "task_id": "codex-cli-startup-mf-sub-task",
                "worker_slot_id": "codex-cli-worker-slot",
                "agent_id": "codex-cli-mfsub-doc-bootstrap-progress-20260605-a2",
                "actual_host_worker_id": (
                    "codex-cli-mfsub-doc-bootstrap-progress-20260605-a2"
                ),
                "host_startup_id": (
                    "codex-cli-thread:"
                    "019e995e-d14d-79f2-8fcb-5af3ec083251"
                ),
                "host_session_id": (
                    "codex-cli-thread:"
                    "019e995e-d14d-79f2-8fcb-5af3ec083251"
                ),
            }
        },
        route_identity={
            "route_id": "route-codex-cli-startup",
            "route_context_hash": "sha256:route-codex-cli-startup",
            "prompt_contract_id": "rprompt-codex-cli-startup",
            "prompt_contract_hash": "sha256:prompt-codex-cli-startup",
            "route_token_ref": "rtok-codex-cli-startup",
            "visible_injection_manifest_hash": "sha256:visible-codex-cli-startup",
        },
        now_iso="2026-06-05T20:46:00Z",
    )

    started = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "codex-cli-startup-mf-sub-task",
                "parent_task_id": "codex-cli-startup-parent",
                "worker_role": "mf_sub",
                "worker_id": "codex-cli-worker-slot",
                "agent_id": "codex-cli-mfsub-doc-bootstrap-progress-20260605-a2",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "host_startup_id": (
                    "codex-cli-thread:"
                    "019e995e-d14d-79f2-8fcb-5af3ec083251"
                ),
                "startup_source": "codex_cli_exec",
                "fence_token": "fence-codex-cli-startup-mf-sub",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/codex-cli-startup-mf-sub-task",
                "head_commit": "head-codex-cli-startup",
                "base_commit": "base-codex-cli-startup",
                "target_head_commit": "target-codex-cli-startup",
                "merge_queue_id": "mergeq-api-codex-cli-startup",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-codex-cli-startup",
                "route_context_hash": "sha256:route-codex-cli-startup",
                "prompt_contract_id": "rprompt-codex-cli-startup",
                "prompt_contract_hash": "sha256:prompt-codex-cli-startup",
                "route_token_ref": "rtok-codex-cli-startup",
                "visible_injection_manifest_hash": "sha256:visible-codex-cli-startup",
                "observer_command_id": "cmd-codex-cli-startup",
                "read_receipt_hash": "sha256:read-codex-cli-startup",
                "read_receipt_event_id": "2873",
            },
        )
    )

    assert started["ok"] is True
    assert started["startup_gate"]["allocation_owner"] == "codex_observer_subagent"
    assert (
        started["startup_gate"]["actual_host_worker_id"]
        == "codex-cli-mfsub-doc-bootstrap-progress-20260605-a2"
    )
    assert started["startup_gate"]["agent_id_match_mode"] == (
        "host_adapter_startup_token_surrogate"
    )
    assert started["startup_gate"]["host_adapter_startup_token_accepted"] is True
    assert started["startup_gate"]["host_startup_id"].startswith("codex-cli-thread:")


def test_parallel_branch_startup_rejects_host_worker_mismatch_without_surrogate(
    conn, tmp_path
):
    worktree = tmp_path / "worker-host-startup-mismatch"
    worktree.mkdir()
    runtime_context = BranchTaskRuntimeContext(
        project_id=PID,
        task_id="host-startup-mismatch-task",
        root_task_id="host-startup-parent",
        stage_task_id="host-startup-mismatch-task",
        backlog_id="FEAT-HOST-STARTUP-GATE",
        branch_ref="refs/heads/codex/host-startup-mismatch-task",
        status="worktree_ready",
        worker_id="host-startup-worker-slot",
        agent_id="observer-allocation-owner",
        allocation_owner="observer-allocation-owner",
        worker_slot_id="host-startup-worker-slot",
        fence_token="fence-host-startup-mismatch",
        worktree_path=str(worktree),
        base_commit="base-host-startup",
        target_head_commit="target-host-startup",
        merge_queue_id="mergeq-api-host-startup",
    )
    upsert_branch_context(
        conn,
        runtime_context,
    )

    blocked = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "host-startup-mismatch-task",
                "parent_task_id": "host-startup-parent",
                "worker_role": "mf_sub",
                "worker_id": "host-startup-worker-slot",
                "agent_id": "019e95fd-cec4-7c12-8abe-8acc849cd9c4",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "session_token_surrogate": "plain-host-session",
                "fence_token": "fence-host-startup-mismatch",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/host-startup-mismatch-task",
                "head_commit": "head-host-startup",
                "base_commit": "base-host-startup",
                "target_head_commit": "target-host-startup",
                "merge_queue_id": "mergeq-api-host-startup",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-host-startup",
                "route_context_hash": "sha256:route-host-startup",
                "prompt_contract_id": "rprompt-host-startup",
                "prompt_contract_hash": "sha256:prompt-host-startup",
                "route_token_ref": "rtok-host-startup",
                "visible_injection_manifest_hash": "sha256:visible-host-startup",
                "observer_command_id": "cmd-host-startup",
                "read_receipt_hash": "sha256:read-host-startup",
                "read_receipt_event_id": "2873",
            },
        )
    )

    assert blocked["ok"] is False
    assert blocked["blocker_id"] == "agent_id_mismatch"

    blocked_with_source = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "host-startup-mismatch-task",
                "parent_task_id": "host-startup-parent",
                "worker_role": "mf_sub",
                "worker_id": "host-startup-worker-slot",
                "agent_id": "019e95fd-cec4-7c12-8abe-8acc849cd9c4",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "session_token_surrogate": "plain-host-session",
                "startup_source": "codex_cli_exec",
                "fence_token": "fence-host-startup-mismatch",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/host-startup-mismatch-task",
                "head_commit": "head-host-startup",
                "base_commit": "base-host-startup",
                "target_head_commit": "target-host-startup",
                "merge_queue_id": "mergeq-api-host-startup",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-host-startup",
                "route_context_hash": "sha256:route-host-startup",
                "prompt_contract_id": "rprompt-host-startup",
                "prompt_contract_hash": "sha256:prompt-host-startup",
                "route_token_ref": "rtok-host-startup",
                "visible_injection_manifest_hash": "sha256:visible-host-startup",
                "observer_command_id": "cmd-host-startup",
                "read_receipt_hash": "sha256:read-host-startup",
                "read_receipt_event_id": "2873",
            },
        )
    )
    assert blocked_with_source["ok"] is False
    assert blocked_with_source["blocker_id"] == "agent_id_mismatch"

    blocked_with_incidental_marker = (
        server.handle_graph_governance_parallel_branch_startup(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "task_id": "host-startup-mismatch-task",
                    "parent_task_id": "host-startup-parent",
                    "worker_role": "mf_sub",
                    "worker_id": "host-startup-worker-slot",
                    "agent_id": "019e95fd-cec4-7c12-8abe-8acc849cd9c4",
                    "runtime_context_id": runtime_context_id_for_branch_context(
                        runtime_context
                    ),
                    "host_startup_id": "opaque-codex-cli-thread:123",
                    "startup_source": "codex_cli_exec",
                    "fence_token": "fence-host-startup-mismatch",
                    "actual_cwd": str(worktree),
                    "actual_git_root": str(worktree),
                    "branch": "refs/heads/codex/host-startup-mismatch-task",
                    "head_commit": "head-host-startup",
                    "base_commit": "base-host-startup",
                    "target_head_commit": "target-host-startup",
                    "merge_queue_id": "mergeq-api-host-startup",
                    "owned_files": [
                        "agent/governance/parallel_branch_runtime.py"
                    ],
                    "route_id": "route-host-startup",
                    "route_context_hash": "sha256:route-host-startup",
                    "prompt_contract_id": "rprompt-host-startup",
                    "prompt_contract_hash": "sha256:prompt-host-startup",
                    "route_token_ref": "rtok-host-startup",
                    "visible_injection_manifest_hash": (
                        "sha256:visible-host-startup"
                    ),
                    "observer_command_id": "cmd-host-startup",
                    "read_receipt_hash": "sha256:read-host-startup",
                    "read_receipt_event_id": "2873",
                },
            )
        )
    )
    assert blocked_with_incidental_marker["ok"] is False
    assert blocked_with_incidental_marker["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )

    blocked_with_flag = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "host-startup-mismatch-task",
                "parent_task_id": "host-startup-parent",
                "worker_role": "mf_sub",
                "worker_id": "host-startup-worker-slot",
                "agent_id": "019e95fd-cec4-7c12-8abe-8acc849cd9c4",
                "runtime_context_id": runtime_context_id_for_branch_context(
                    runtime_context
                ),
                "host_startup_id": "opaque-thread:123",
                "host_adapter_startup": True,
                "fence_token": "fence-host-startup-mismatch",
                "actual_cwd": str(worktree),
                "actual_git_root": str(worktree),
                "branch": "refs/heads/codex/host-startup-mismatch-task",
                "head_commit": "head-host-startup",
                "base_commit": "base-host-startup",
                "target_head_commit": "target-host-startup",
                "merge_queue_id": "mergeq-api-host-startup",
                "owned_files": ["agent/governance/parallel_branch_runtime.py"],
                "route_id": "route-host-startup",
                "route_context_hash": "sha256:route-host-startup",
                "prompt_contract_id": "rprompt-host-startup",
                "prompt_contract_hash": "sha256:prompt-host-startup",
                "route_token_ref": "rtok-host-startup",
                "visible_injection_manifest_hash": "sha256:visible-host-startup",
                "observer_command_id": "cmd-host-startup",
                "read_receipt_hash": "sha256:read-host-startup",
                "read_receipt_event_id": "2873",
            },
        )
    )
    assert blocked_with_flag["ok"] is False
    assert blocked_with_flag["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )


def test_parallel_branch_startup_rejects_event_4178_multi_agent_prefix_replay(
    conn,
    tmp_path,
):
    worktree = tmp_path / "worker-event-4178-startup"
    worktree.mkdir()
    runtime_context = BranchTaskRuntimeContext(
        project_id=PID,
        task_id="event-4178-startup-task",
        root_task_id="event-4178-parent",
        stage_task_id="event-4178-startup-task",
        backlog_id="FEAT-HOST-STARTUP-GATE",
        branch_ref="refs/heads/codex/event-4178-startup-task",
        status="worktree_ready",
        worker_id="event-4178-worker-slot",
        agent_id="observer-allocation-owner",
        allocation_owner="observer-allocation-owner",
        worker_slot_id="event-4178-worker-slot",
        fence_token="fence-event-4178-startup",
        worktree_path=str(worktree),
        base_commit="base-event-4178",
        target_head_commit="target-event-4178",
        merge_queue_id="mergeq-event-4178",
    )
    upsert_branch_context(conn, runtime_context)

    def _body(host_startup_id: str) -> dict:
        return {
            "task_id": "event-4178-startup-task",
            "parent_task_id": "event-4178-parent",
            "worker_role": "mf_sub",
            "worker_id": "event-4178-worker-slot",
            "agent_id": "codex-multi-agent-4178",
            "session_token": "same-event-4178-session-token",
            "runtime_context_id": runtime_context_id_for_branch_context(
                runtime_context
            ),
            "host_startup_id": host_startup_id,
            "fence_token": "fence-event-4178-startup",
            "actual_cwd": str(worktree),
            "actual_git_root": str(worktree),
            "branch": "refs/heads/codex/event-4178-startup-task",
            "head_commit": "head-event-4178",
            "base_commit": "base-event-4178",
            "target_head_commit": "target-event-4178",
            "merge_queue_id": "mergeq-event-4178",
            "owned_files": ["agent/governance/parallel_branch_runtime.py"],
            "route_id": "route-event-4178",
            "route_context_hash": "sha256:route-event-4178",
            "prompt_contract_id": "rprompt-event-4178",
            "prompt_contract_hash": "sha256:prompt-event-4178",
            "route_token_ref": "rtok-event-4178",
            "visible_injection_manifest_hash": "sha256:visible-event-4178",
            "observer_command_id": "cmd-event-4178",
            "read_receipt_hash": "sha256:read-event-4178",
            "read_receipt_event_id": "4178",
        }

    attempts = (
        _body("codex-multi-agent-4178-a"),
        _body("codex-multi-agent-4178-b"),
        _body("multi_agent_v1:4178-b"),
    )
    results = [
        server.handle_graph_governance_parallel_branch_startup(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body=body,
            )
        )
        for body in attempts
    ]

    for result in results:
        assert result["ok"] is False
        assert result["blocker_id"] == "agent_id_mismatch"
        assert result["timeline_event_recorded"]["event_kind"] == (
            "mf_subagent_startup_refusal"
        )

    events = task_timeline.list_events(
        conn,
        PID,
        backlog_id="FEAT-HOST-STARTUP-GATE",
        event_kind="mf_subagent_startup_refusal",
        limit=10,
    )
    event_4178_events = [
        event
        for event in events
        if event["task_id"] == "event-4178-startup-task"
    ]
    assert len(event_4178_events) == 3
    refusals = [
        event["payload"]["mf_subagent_startup_refusal"]
        for event in event_4178_events
    ]
    assert [refusal["host_startup_id"] for refusal in refusals] == [
        "codex-multi-agent-4178-a",
        "codex-multi-agent-4178-b",
        "multi_agent_v1:4178-b",
    ]
    for refusal in refusals:
        assert refusal["blocker_id"] == "agent_id_mismatch"
        assert refusal["agent_id"] == "codex-multi-agent-4178"
        assert refusal["allocation_owner"] == "observer-allocation-owner"
        assert refusal["runtime_context_id"] == runtime_context_id_for_branch_context(
            runtime_context
        )
        assert refusal["route_id"] == "route-event-4178"
        assert refusal["route_context_hash"] == "sha256:route-event-4178"
        assert refusal["prompt_contract_id"] == "rprompt-event-4178"
        assert refusal["prompt_contract_hash"] == "sha256:prompt-event-4178"
    assert "same-event-4178-session-token" not in json.dumps(
        event_4178_events,
        sort_keys=True,
    )


def test_parallel_branch_startup_returns_blocker_without_actual_startup(conn, tmp_path):
    worktree = tmp_path / "worker-startup-blocked"
    worktree.mkdir()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="startup-blocked-task",
            root_task_id="startup-parent",
            stage_task_id="startup-blocked-task",
            backlog_id="FEAT-STARTUP-GATE",
            branch_ref="refs/heads/codex/startup-blocked-task",
            status="worktree_ready",
            worker_id="startup-worker",
            agent_id="startup-agent",
            fence_token="fence-startup-blocked",
            worktree_path=str(worktree),
            base_commit="base-startup",
            target_head_commit="target-startup",
            merge_queue_id="mergeq-api-startup",
        ),
    )

    blocked = server.handle_graph_governance_parallel_branch_startup(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "task_id": "startup-blocked-task",
                "branch_runtime_evidence": {"registered": True},
                "startup_intent_event": {"event_kind": "mf_subagent_startup_intent"},
            },
        )
    )

    events = task_timeline.list_events(
        conn,
        PID,
        backlog_id="FEAT-STARTUP-GATE",
        event_kind="mf_subagent_startup_refusal",
    )
    assert blocked["ok"] is False
    assert blocked["blocker_id"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert blocked["terminal_dispatch_blocker"] is True
    assert len(events) == 1
    assert events[0]["payload"]["mf_subagent_startup_refusal"]["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )


def test_parallel_branch_finish_gate_rejects_stale_fence(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="finish-stale-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/finish-stale-task",
            status="worktree_ready",
            fence_token="fence-current",
            worktree_path="/tmp/nonexistent-finish-stale-task",
            base_commit="base",
            target_head_commit="target",
            merge_queue_id="mergeq-api-finish-stale",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )

    with pytest.raises(MfSubagentContractError, match="stale"):
        server.handle_graph_governance_parallel_branch_finish_gate(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "finish-stale-task",
                    "status": "succeeded",
                    "changed_files": ["agent/governance/server.py"],
                    "test_results": {"status": "passed"},
                    "checkpoint_id": "ckpt-stale",
                    "fence_token": "fence-old",
                },
            )
        )


def test_parallel_branch_finish_gate_validates_worktree_changed_files(conn, tmp_path):
    repo = _git_repo(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "worker change"], cwd=repo, check=True, capture_output=True, text=True)
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-finish-diff",
            task_id="finish-diff-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/finish-diff-task",
            status="worktree_ready",
            fence_token="fence-finish-diff",
            worktree_path=str(repo),
            base_commit=base,
            head_commit=base,
            target_head_commit=base,
            merge_queue_id="mergeq-api-finish-diff",
        ),
        now_iso="2026-05-17T07:33:00Z",
    )
    _record_finish_startup_event(
        conn,
        task_id="finish-diff-task",
        backlog_id="FEAT-FINISH-GATE",
        fence_token="fence-finish-diff",
        worktree_path=str(repo),
        branch_ref="refs/heads/codex/finish-diff-task",
        head_commit=head,
        nested_key="mf_subagent_startup_gate",
    )

    with pytest.raises(ValidationError, match="changed_files do not match assigned worktree diff"):
        server.handle_graph_governance_parallel_branch_finish_gate(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "finish-diff-task",
                    "status": "succeeded",
                    "changed_files": ["agent/governance/server.py"],
                    "test_results": {"status": "passed"},
                    "checkpoint_id": "ckpt-finish-diff-bad",
                    "fence_token": "fence-finish-diff",
                    "head_commit": head,
                    "evidence": _finish_gate_evidence(
                        fence_token="fence-finish-diff",
                        worktree_path=str(repo),
                        branch_ref="refs/heads/codex/finish-diff-task",
                        head_commit=head,
                        nested_key="mf_subagent_startup_gate",
                    ),
                },
            )
        )

    finished = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "finish-diff-task",
                "status": "succeeded",
                "changed_files": ["README.md"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-finish-diff",
                "fence_token": "fence-finish-diff",
                "head_commit": head,
                "evidence": _finish_gate_evidence(
                    fence_token="fence-finish-diff",
                    worktree_path=str(repo),
                    branch_ref="refs/heads/codex/finish-diff-task",
                    head_commit=head,
                    nested_key="mf_subagent_startup_gate",
                ),
            },
        )
    )

    assert finished["ok"] is True
    assert finished["gate"]["validated_changed_files"] == ["README.md"]
    assert finished["context"]["head_commit"] == head


def test_mf_sub_merge_queue_requires_finish_gate_checkpoint(conn):
    queue_id = "mergeq-api-finish-required"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="mf-sub-queue-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/mf-sub-queue-task",
            status="worktree_ready",
            fence_token="fence-mf-sub",
            worktree_path="/tmp/nonexistent-mf-sub-queue-task",
            base_commit="base",
            head_commit="head",
            target_head_commit="target",
            merge_queue_id=queue_id,
        ),
        now_iso="2026-05-17T07:32:00Z",
    )
    _record_finish_startup_event(
        conn,
        task_id="mf-sub-queue-task",
        backlog_id="FEAT-FINISH-GATE",
        fence_token="fence-mf-sub",
        worktree_path="/tmp/nonexistent-mf-sub-queue-task",
        branch_ref="refs/heads/codex/mf-sub-queue-task",
        head_commit="head",
    )

    with pytest.raises(ValueError, match="checkpoint_id is required"):
        server.handle_graph_governance_parallel_branch_merge_queue(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "mf-sub-queue-task",
                    "merge_queue_id": queue_id,
                    "worker_role": "mf_sub",
                    "fence_token": "fence-mf-sub",
                    "route_waiver": _route_waiver("merge_queue", task_id="mf-sub-queue-task"),
                },
            )
        )

    server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "mf-sub-queue-task",
                "status": "succeeded",
                "changed_files": ["agent/governance/server.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-mf-sub",
                "fence_token": "fence-mf-sub",
                "head_commit": "head",
                "evidence": _finish_gate_evidence(
                    fence_token="fence-mf-sub",
                    worktree_path="/tmp/nonexistent-mf-sub-queue-task",
                    branch_ref="refs/heads/codex/mf-sub-queue-task",
                    head_commit="head",
                ),
            },
        )
    )

    queued = server.handle_graph_governance_parallel_branch_merge_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "mf-sub-queue-task",
                "merge_queue_id": queue_id,
                "worker_role": "mf_sub",
                "checkpoint_id": "ckpt-mf-sub",
                "fence_token": "fence-mf-sub",
                "route_waiver": _route_waiver("merge_queue", task_id="mf-sub-queue-task"),
            },
        )
    )

    assert queued["ok"] is True
    assert queued["context"]["status"] == "queued_for_merge"
    assert queued["context"]["checkpoint_id"] == "ckpt-mf-sub"


def test_mf_sub_session_cannot_enqueue_or_execute_merge(conn):
    queue_id = "mergeq-api-mf-sub-denied"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="mf-sub-denied-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/mf-sub-denied-task",
            status="validated",
            fence_token="fence-mf-sub-denied",
            worktree_path="/tmp/nonexistent-mf-sub-denied-task",
            base_commit="base",
            head_commit="head",
            target_head_commit="target",
            checkpoint_id="ckpt-mf-sub-denied",
            merge_queue_id=queue_id,
            replay_source="mf_sub_finish_gate",
        ),
        now_iso="2026-05-17T07:32:00Z",
    )

    with pytest.raises(PermissionDeniedError, match="merge-queue"):
        server.handle_graph_governance_parallel_branch_merge_queue(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "task_id": "mf-sub-denied-task",
                    "merge_queue_id": queue_id,
                    "worker_role": "mf_sub",
                    "checkpoint_id": "ckpt-mf-sub-denied",
                    "fence_token": "fence-mf-sub-denied",
                },
            )
        )

    with pytest.raises(PermissionDeniedError, match="merge-execute"):
        server.handle_graph_governance_parallel_branch_merge_execute(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "merge_queue_id": queue_id,
                    "target_ref": "main",
                    "task_id": "mf-sub-denied-task",
                    "evidence": {},
                    "dry_run": True,
                },
            )
        )


def test_parallel_branch_merge_gate_route_returns_dry_run_plan(conn):
    queue_id = "mergeq-api-gate"
    evidence = {
        "git_conflict_check": {"status": "pass", "evidence_id": "preview-api-gate"},
        "dirty_worktree_check": {"status": "pass"},
        "test_evidence": {"status": "pass"},
        "graph_currentness": {"status": "current"},
        "scope_reconcile": {"status": "pass"},
        "semantic_projection": {"status": "pass"},
        "backlog_acceptance": {"status": "satisfied"},
    }
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-gate-task",
                task_id="gate-task",
                branch_ref="refs/heads/codex/gate-task",
                queue_index=1,
                status="merge_ready",
                target_ref="refs/heads/main",
                branch_head="head-gate",
                validated_target_head="target-gate",
                current_target_head="target-gate",
                merge_preview_id="preview-api-gate",
                snapshot_id="scope-gate",
                projection_id="semproj-gate",
            )
        ],
        now_iso="2026-05-17T08:10:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_merge_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "merge_queue_id": queue_id,
                "task_id": "gate-task",
                "evidence": evidence,
            },
        )
    )

    assert result["ok"] is True
    plan = result["plan"]
    assert plan["dry_run"] is True
    assert plan["merge_gate_passed"] is True
    assert plan["merge_allowed"] is True
    assert plan["target_branch_mutation_allowed"] is False
    assert plan["target_graph_activation_allowed"] is False
    assert plan["next_actions"] == ["operator_approve_live_merge"]
    assert plan["merge_preview_id"] == "preview-api-gate"
    assert plan["evidence"][0]["key"] == "git_conflict_check"


def test_parallel_branch_merge_gate_route_blocks_batch_rollback(conn):
    queue_id = "mergeq-api-gate-blocked"
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-gate-blocked",
                task_id="gate-blocked",
                branch_ref="refs/heads/codex/gate-blocked",
                queue_index=1,
                status="merge_ready",
                target_ref="refs/heads/main",
                branch_head="head-gate-blocked",
                validated_target_head="target-gate",
                current_target_head="target-gate",
            )
        ],
        now_iso="2026-05-17T08:15:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_merge_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "merge_queue_id": queue_id,
                "task_id": "gate-blocked",
                "batch_status": "rollback_required",
            },
        )
    )

    assert result["plan"]["merge_gate_passed"] is False
    assert "batch_rollback_required" in result["plan"]["blocker_codes"]
    assert "missing_evidence:git_conflict_check" in result["plan"]["blocker_codes"]
    assert result["plan"]["target_branch_mutation_allowed"] is False


def test_parallel_branch_merge_preview_route_builds_gate_evidence(conn, tmp_path):
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature-preview"], cwd=repo, check=True)
    (repo / "preview.txt").write_text("preview\n", encoding="utf-8")
    subprocess.run(["git", "add", "preview.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "preview branch"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)
    main_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    queue_id = "mergeq-api-preview"
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-preview-task",
                task_id="preview-task",
                branch_ref="feature-preview",
                queue_index=1,
                status="merge_ready",
                target_ref="main",
                branch_head="feature-preview",
                validated_target_head=main_head,
                current_target_head=main_head,
                merge_preview_id="preview-route",
                snapshot_id="scope-preview",
                projection_id="semproj-preview",
            )
        ],
        now_iso="2026-05-17T08:24:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_merge_preview(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "repo_root_path": str(repo),
                "merge_queue_id": queue_id,
                "target_ref": "main",
                "task_id": "preview-task",
                "include_gate_plan": True,
                "evidence": {
                    "dirty_worktree_check": {"status": "pass"},
                    "test_evidence": {"status": "pass"},
                    "graph_currentness": {"status": "current"},
                    "scope_reconcile": {"status": "pass"},
                    "semantic_projection": {"status": "pass"},
                    "backlog_acceptance": {"status": "satisfied"},
                },
            },
        )
    )

    assert result["ok"] is True
    assert result["preview"]["status"] == "pass"
    assert result["preview"]["target_commit"] == main_head
    assert result["gate_plan"]["merge_gate_passed"] is True
    assert result["gate_plan"]["dry_run"] is True
    assert result["gate_plan"]["target_branch_mutation_allowed"] is False


def test_parallel_branch_merge_execute_route_dry_run_then_live_merge(conn, tmp_path):
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature-live"], cwd=repo, check=True)
    (repo / "live.txt").write_text("live\n", encoding="utf-8")
    subprocess.run(["git", "add", "live.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "live branch"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)
    main_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    queue_id = "mergeq-api-execute"
    evidence = {
        "dirty_worktree_check": {"status": "pass"},
        "test_evidence": {"status": "pass"},
        "graph_currentness": {"status": "current"},
        "scope_reconcile": {"status": "pass"},
        "semantic_projection": {"status": "pass"},
        "backlog_acceptance": {"status": "satisfied"},
    }
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-execute",
            task_id="execute-task",
            branch_ref="feature-live",
            status="merge_ready",
            fence_token="fence-execute-current",
            target_head_commit=main_head,
            merge_queue_id=queue_id,
        ),
        now_iso="2026-05-17T08:28:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-execute-task",
                task_id="execute-task",
                branch_ref="feature-live",
                queue_index=1,
                status="merge_ready",
                target_ref="main",
                branch_head="feature-live",
                validated_target_head=main_head,
                current_target_head=main_head,
                snapshot_id="scope-execute",
                projection_id="semproj-execute",
            )
        ],
        now_iso="2026-05-17T08:28:00Z",
    )

    dry_run = server.handle_graph_governance_parallel_branch_merge_execute(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "repo_root_path": str(repo),
                "merge_queue_id": queue_id,
                "target_ref": "main",
                "task_id": "execute-task",
                "evidence": evidence,
                "dry_run": True,
            },
        )
    )

    assert dry_run["ok"] is True
    assert dry_run["executed"] is False
    assert dry_run["gate_plan"]["merge_gate_passed"] is True
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == main_head

    with pytest.raises(GovernanceError, match="route_token"):
        server.handle_graph_governance_parallel_branch_merge_execute(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "repo_root_path": str(repo),
                    "merge_queue_id": queue_id,
                    "target_ref": "main",
                    "task_id": "execute-task",
                    "evidence": evidence,
                    "dry_run": False,
                    "allow_target_ref_mutation": True,
                    "fence_token": "fence-execute-current",
                    "message": "merge feature-live",
                    "bug_id": "ARCH-PARALLEL-AGENT-MULTIBRANCH-EXECUTION",
                },
            )
        )

    live = server.handle_graph_governance_parallel_branch_merge_execute(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "repo_root_path": str(repo),
                "merge_queue_id": queue_id,
                "target_ref": "main",
                "task_id": "execute-task",
                "evidence": evidence,
                "dry_run": False,
                "allow_target_ref_mutation": True,
                "fence_token": "fence-execute-current",
                "route_waiver": _route_waiver("merge_execute", task_id="execute-task"),
                "message": "merge feature-live",
                "bug_id": "ARCH-PARALLEL-AGENT-MULTIBRANCH-EXECUTION",
                "now_iso": "2026-05-17T08:29:00Z",
            },
        )
    )

    assert live["ok"] is True
    assert live["executed"] is True
    assert live["merge_commit"]
    assert live["recorded"]["queue_item"]["status"] == "merged"
    assert live["recorded"]["context"]["status"] == "merged"
    assert live["decision"]["rows"][0]["queue_state"] == "merged"
    assert live["decision"]["rows"][0]["target_graph_activation_allowed"] is True
    assert (repo / "live.txt").read_text(encoding="utf-8") == "live\n"
    assert subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.find("Chain-Source-Stage: merge") != -1


def test_parallel_branch_merge_result_route_records_with_fence(conn):
    queue_id = "mergeq-api-result"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-result",
            task_id="result-task",
            branch_ref="refs/heads/codex/result-task",
            status="merge_ready",
            fence_token="fence-result-current",
            target_head_commit="target-before",
            merge_queue_id=queue_id,
            merge_preview_id="preview-result",
        ),
        now_iso="2026-05-17T08:25:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-result-task",
                task_id="result-task",
                branch_ref="refs/heads/codex/result-task",
                queue_index=1,
                status="merge_ready",
                target_ref="refs/heads/main",
                branch_head="head-result",
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-result",
                snapshot_id="scope-result",
                projection_id="semproj-result",
            )
        ],
        now_iso="2026-05-17T08:25:00Z",
    )

    with pytest.raises(BranchRuntimeFenceError):
        server.handle_graph_governance_parallel_branch_merge_result(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "merge_queue_id": queue_id,
                    "task_id": "result-task",
                    "status": "merged",
                    "merge_commit": "merge-result",
                    "target_head_after_merge": "target-after",
                    "fence_token": "fence-stale",
                    "route_waiver": _route_waiver("merge_result", task_id="result-task"),
                },
            )
        )

    result = server.handle_graph_governance_parallel_branch_merge_result(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "merge_queue_id": queue_id,
                "task_id": "result-task",
                "status": "merged",
                "merge_commit": "merge-result",
                "target_head_before_merge": "target-before",
                "target_head_after_merge": "target-after",
                "fence_token": "fence-result-current",
                "route_waiver": _route_waiver("merge_result", task_id="result-task"),
                "now_iso": "2026-05-17T08:26:00Z",
            },
        )
    )

    assert result["ok"] is True
    assert result["queue_item"]["status"] == "merged"
    assert result["queue_item"]["merge_commit"] == "merge-result"
    assert result["context"]["status"] == "merged"
    assert result["context"]["target_head_commit"] == "target-after"
    row = result["decision"]["rows"][0]
    assert row["queue_state"] == "merged"
    assert row["target_graph_activation_allowed"] is True


def test_parallel_branch_batch_runtime_route_returns_rollback_plan(conn):
    batch_id = "PB-api-batch"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id=batch_id,
            task_id="T1",
            branch_ref="refs/heads/codex/batch-t1",
            worktree_path="/repo/.worktrees/batch-t1",
            status="merged",
            base_commit="base-batch",
            head_commit="head-T1",
            snapshot_id="scope-T1",
            projection_id="semproj-T1",
            merge_queue_id="mergeq-batch",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id=batch_id,
            task_id="T2",
            branch_ref="refs/heads/codex/batch-t2",
            worktree_path="/repo/.worktrees/batch-t2",
            status="merge_failed",
            base_commit="base-batch",
            head_commit="head-T2",
            snapshot_id="scope-T2",
            projection_id="semproj-T2",
            merge_queue_id="mergeq-batch",
            depends_on=("T1",),
        ),
        now_iso="2026-05-17T07:30:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_batch_runtime(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "batch_id": batch_id,
                "target_ref": "refs/heads/main",
                "batch_base_commit": "base-batch",
                "current_target_head": "bad-target",
                "severe_integration_failure": True,
                "corrected_replay_order": ["T2", "T1"],
                "failure_reason": "wrong merge order",
                "items": [
                    {"task_id": "T1", "queue_index": 1, "merge_commit": "merge-T1"},
                    {"task_id": "T2", "queue_index": 2},
                ],
                "now_iso": "2026-05-17T07:31:00Z",
            },
        )
    )

    assert result["ok"] is True
    assert result["runtime"]["batch_status"] == "rollback_required"
    assert result["plan"]["rollback_required"] is True
    assert result["plan"]["rollback_target_commit"] == "base-batch"
    assert result["plan"]["retained_branch_refs"] == [
        "refs/heads/codex/batch-t1",
        "refs/heads/codex/batch-t2",
    ]
    assert result["plan"]["replay_task_ids"] == ["T2", "T1"]
    assert result["plan"]["cleanup_allowed"] is False
    assert result["plan"]["cleanup_blockers"] == ["T1", "T2"]

    read = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": batch_id,
                "merge_queue_id": "mergeq-batch",
                "corrected_replay_order": "T2,T1",
                "limit": "5",
            },
        )
    )

    assert read["read_model"]["rollback"]["rollback_required"] is True
    assert read["read_model"]["rollback"]["replay_task_ids"] == ["T2", "T1"]
    assert read["read_model"]["rollback"]["cleanup_blockers"] == ["T1", "T2"]


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


def test_feedback_queue_surfaces_graph_structure_lifecycle_files(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="graph-structure-review",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()

    reconcile_feedback.submit_feedback_item(
        PID,
        snapshot["snapshot_id"],
        feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
        issue={
            "type": "governance_hint_attach",
            "reason": "operator candidate binding",
            "summary": "Attach doc with governance hint.",
            "target": "L7.1",
            "target_type": "doc",
            "paths": ["docs/runtime.md"],
            "changed_files": ["docs/runtime.md"],
            "intent": "bind_candidate_doc",
        },
        actor="observer",
        source_round="graph_structure_lifecycle",
    )

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    group = queue["groups"][0]
    lifecycle = group["graph_structure_lifecycle"]
    assert group["category"] in {"asset_binding", "doc_binding", "graph_structure"}
    assert lifecycle["subtype"] == "governance_hint"
    assert lifecycle["changed_files"] == ["docs/runtime.md"]
    assert lifecycle["requires_commit"] is True
    assert lifecycle["update_graph_after_commit"] is True
    assert lifecycle["semantic_lifecycle"] == "separate"


def test_graph_structure_cancel_discards_only_operation_files(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "docs").mkdir()
    (repo / "docs/runtime.md").write_text("before\n", encoding="utf-8")
    (repo / "other.txt").write_text("keep\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "docs/runtime.md").write_text("after\n", encoding="utf-8")
    (repo / "other.txt").write_text("keep dirty\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="graph-structure-cancel",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    submitted = reconcile_feedback.submit_feedback_item(
        PID,
        snapshot["snapshot_id"],
        feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
        issue={
            "type": "governance_hint_attach",
            "reason": "operator candidate binding",
            "summary": "Attach doc with governance hint.",
            "target": "L7.1",
            "target_type": "doc",
            "paths": ["docs/runtime.md"],
            "changed_files": ["docs/runtime.md"],
        },
        actor="observer",
        source_round="graph_structure_lifecycle",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    result = server.handle_graph_governance_snapshot_feedback_graph_structure_cancel(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"project_root": str(repo), "feedback_ids": [feedback_id]},
        )
    )

    assert result["status"] == "cancelled"
    assert result["discarded_files"] == ["docs/runtime.md"]
    assert (repo / "docs/runtime.md").read_text(encoding="utf-8") == "before\n"
    assert (repo / "other.txt").read_text(encoding="utf-8") == "keep dirty\n"


def test_graph_structure_cancel_refuses_unsafe_overlap(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "docs").mkdir()
    (repo / "docs/runtime.md").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "docs/runtime.md").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/runtime.md"], cwd=repo, check=True)
    (repo / "docs/runtime.md").write_text("unstaged\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="graph-structure-overlap",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    submitted = reconcile_feedback.submit_feedback_item(
        PID,
        snapshot["snapshot_id"],
        feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
        issue={
            "type": "governance_hint_attach",
            "summary": "Attach doc with governance hint.",
            "target": "L7.1",
            "target_type": "doc",
            "paths": ["docs/runtime.md"],
            "changed_files": ["docs/runtime.md"],
        },
        actor="observer",
        source_round="graph_structure_lifecycle",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    status, result = server.handle_graph_governance_snapshot_feedback_graph_structure_cancel(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"project_root": str(repo), "feedback_ids": [feedback_id]},
        )
    )

    assert status == 409
    assert result["status"] == "blocked_dirty_overlap"
    assert result["dirty_guard"]["unsafe_overlap"] == {"docs/runtime.md": "MM"}


def test_graph_structure_commit_stages_only_operation_files(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "docs").mkdir()
    (repo / "docs/runtime.md").write_text("before\n", encoding="utf-8")
    (repo / "other.txt").write_text("keep\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "docs/runtime.md").write_text("after\n", encoding="utf-8")
    (repo / "other.txt").write_text("keep dirty\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="graph-structure-commit",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    submitted = reconcile_feedback.submit_feedback_item(
        PID,
        snapshot["snapshot_id"],
        feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
        issue={
            "type": "governance_hint_attach",
            "summary": "Attach doc with governance hint.",
            "target": "L7.1",
            "target_type": "doc",
            "paths": ["docs/runtime.md"],
            "changed_files": ["docs/runtime.md"],
        },
        actor="observer",
        source_round="graph_structure_lifecycle",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    result = server.handle_graph_governance_snapshot_feedback_graph_structure_commit(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"project_root": str(repo), "feedback_ids": [feedback_id], "actor": "observer"},
        )
    )

    assert result["status"] == "committed"
    assert result["commit"]["commit_sha"]
    show = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    assert show.stdout.strip().splitlines() == ["docs/runtime.md"]
    status = subprocess.run(["git", "status", "--short"], cwd=repo, check=True, capture_output=True, text=True)
    assert status.stdout.strip() == "M other.txt"
    assert result["requires_update_graph"] is True


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


def test_graph_governance_snapshot_nodes_normalize_pending_review_overlay(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-pending-review-overlay",
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
        "feature_name": "Pending Review Feature",
        "semantic_summary": "Generated by AI but not approved yet.",
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'pending_review', 'sha256:feature',
                '{"agent/governance/server.py":"sha256:file"}', ?, 2, 7, '2026-05-09T20:31:24Z')
        """,
        (PID, snapshot["snapshot_id"], json.dumps(semantic_payload)),
    )
    conn.commit()

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    semantic = nodes["nodes"][0]["semantic"]
    assert semantic["status"] == "review_pending"
    assert semantic["node_status"] == "pending_review"
    assert semantic["hash_state"] == "pending"
    assert semantic["has_semantic_payload"] is True


def test_graph_governance_snapshot_nodes_do_not_treat_completed_job_as_semantic(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-job-only-overlay",
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
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:job-only',
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
    assert semantic["status"] == "structure_only"
    assert semantic["node_status"] == ""
    assert semantic["job_status"] == "ai_complete"
    assert semantic["feature_hash"] == ""
    assert semantic["hash_state"] == "unknown"
    assert semantic["has_semantic_payload"] is False
    assert semantic["job"]["feature_hash"] == "sha256:job-only"


def test_graph_governance_semantic_jobs_endpoint_enqueues_existing_semantic_jobs(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    from agent.governance import event_bus

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_bus, "publish", lambda event, payload: published.append((event, payload)))
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
    assert published == [
        (
            "semantic_job.enqueued",
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "queued_count": 1,
                "target_scope": "node",
                "source": "semantic_jobs_create_api",
            },
        )
    ]

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


def test_semantic_jobs_explicit_node_ids_do_not_expand_by_inferred_layer(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr("agent.governance.event_bus.publish", lambda *_args, **_kwargs: None)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-jobs-explicit-node-scope",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph_with_dependency(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph_with_dependency()["deps_graph"]["nodes"],
        edges=_graph_with_dependency()["deps_graph"]["edges"],
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "semantic_node_ids": ["L7.1"],
                "semantic_selector_match": "any",
                "options": {"skip_current": False},
                "created_by": "dashboard_user",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["summary"]["by_status"] == {"ai_pending": 1}
    rows = conn.execute(
        """
        SELECT node_id, status
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchall()
    assert [dict(row) for row in rows] == [{"node_id": "L7.1", "status": "ai_pending"}]


def test_semantic_jobs_stale_scope_uses_projection_stale_nodes(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr("agent.governance.event_bus.publish", lambda *_args, **_kwargs: None)
    base_graph = _graph_with_dependency()
    base_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-jobs-stale-base",
        commit_sha="commit-a",
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
    for node in base_graph["deps_graph"]["nodes"]:
        node_id = str(node["id"])
        graph_events.create_event(
            conn,
            PID,
            base_snapshot["snapshot_id"],
            event_type="semantic_node_enriched",
            event_kind="semantic_job",
            target_type="node",
            target_id=node_id,
            status=graph_events.EVENT_STATUS_ACCEPTED,
            stable_node_key=graph_events.stable_node_key_for_node(node),
            feature_hash=graph_events.feature_hash_for_node(node),
            payload={"semantic_payload": {"feature_name": f"Base {node_id}"}},
            created_by="test",
        )
    graph_events.build_semantic_projection(
        conn,
        PID,
        base_snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-stale-base",
    )

    current_graph = json.loads(json.dumps(base_graph))
    current_graph["deps_graph"]["nodes"][1]["title"] = "Dependency Node Renamed"
    current_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-jobs-stale-current",
        commit_sha="commit-b",
        snapshot_kind="scope",
        parent_snapshot_id=base_snapshot["snapshot_id"],
        graph_json=current_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        current_snapshot["snapshot_id"],
        nodes=current_graph["deps_graph"]["nodes"],
        edges=current_graph["deps_graph"]["edges"],
    )
    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        current_snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-stale-current",
        backfill_existing=False,
    )
    assert projection["health"]["semantic_stale_count"] == 1
    assert (
        projection["projection"]["node_semantics"]["L7.2"]["validity"]["status"]
        == "semantic_stale_feature_hash"
    )
    assert (
        projection["projection"]["node_semantics"]["L7.1"]["validity"]["status"]
        == "semantic_carried_forward_current"
    )

    status, dry_run = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": current_snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "snapshot",
                "options": {"scope": "stale", "dry_run": True, "retry_stale_failed": True},
                "created_by": "dashboard_user",
            },
        )
    )
    assert status == 202
    assert dry_run["planned_count"] == 1
    assert dry_run["batch_plan"]["selector"]["semantic_node_ids"] == ["L7.2"]

    status, queued = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": current_snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "snapshot",
                "options": {"scope": "stale", "retry_stale_failed": True},
                "created_by": "dashboard_user",
            },
        )
    )
    assert status == 202
    assert queued["queued_count"] == 1
    assert queued["batch_plan"]["selector"]["node_ids"] == ["L7.2"]
    rows = conn.execute(
        """
        SELECT node_id, status
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (PID, current_snapshot["snapshot_id"]),
    ).fetchall()
    assert [dict(row) for row in rows] == [{"node_id": "L7.2", "status": "ai_pending"}]


def test_semantic_jobs_operator_request_uses_project_ai_routing(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr(
        server.project_service,
        "get_project_config_metadata",
        lambda project_id: {
            "project_id": project_id,
            "ai": {
                "routing": {
                    "semantic": {"provider": "openai", "model": "gpt-5.5"}
                }
            },
        },
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-jobs-project-routing",
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

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
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

    assert status == 202
    analyzer = payload["operator_request"]["analyzer"]
    assert analyzer["provider"] == "openai"
    assert analyzer["model"] == "gpt-5.5"
    assert "ai.routing.semantic" in analyzer["override_path"]


def test_semantic_jobs_requires_project_route_when_registry_config_exists(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr(
        server.project_service,
        "get_project_config_metadata",
        lambda project_id: {"project_id": project_id, "ai": {"routing": {}}},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-jobs-missing-project-routing",
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

    with pytest.raises(ValidationError, match="AI enrich blocked"):
        server.handle_graph_governance_snapshot_semantic_jobs_create(
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


def test_semantic_jobs_edge_targets_hydrates_edge_dict_when_only_target_ids_given(conn):
    """Regression for MF 2026-05-11 / BACKLOG-EDGE-AI-ENRICH-BROKEN bug 1.

    Dashboard sends `target_ids: ["<src>|<dst>|<type>"]` with no `edges` array
    and no `all_eligible: true`. Previously the backend created
    {"edge_id": ..., "edge": {}} — an empty edge dict — and the downstream
    event payload's `edge_context.src/dst/edge_type/evidence` were all empty
    strings, causing the AI to reply risk=insufficient_context. The fix is
    to look up the matching edge in the snapshot and hydrate the dict.
    """
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="edge-targets-hydration",
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

    # Dashboard sends the pipe-form edge_id. Arrow-form should also work.
    rows = server._semantic_jobs_edge_targets(
        {"target_ids": ["L7.1|L7.2|depends_on"]},
        conn=conn,
        project_id=PID,
        snapshot_id=snapshot["snapshot_id"],
    )
    assert len(rows) == 1
    edge = rows[0]["edge"]
    # Snapshot edges normalize src/dst into `src`/`dst` keys (not source/target).
    assert (edge.get("src") or edge.get("source")) == "L7.1"
    assert (edge.get("dst") or edge.get("target")) == "L7.2"
    assert (edge.get("edge_type") or edge.get("type")) == "depends_on"

    rows_arrow = server._semantic_jobs_edge_targets(
        {"target_ids": ["L7.1->L7.2:depends_on"]},
        conn=conn,
        project_id=PID,
        snapshot_id=snapshot["snapshot_id"],
    )
    assert len(rows_arrow) == 1
    assert (rows_arrow[0]["edge"].get("src") or rows_arrow[0]["edge"].get("source")) == "L7.1"

    # Unknown edge_id should fall through to {} (graceful, not a crash).
    rows_missing = server._semantic_jobs_edge_targets(
        {"target_ids": ["L7.99|L7.999|nonexistent"]},
        conn=conn,
        project_id=PID,
        snapshot_id=snapshot["snapshot_id"],
    )
    assert rows_missing == [{"edge_id": "L7.99|L7.999|nonexistent", "edge": {}}]


def test_semantic_jobs_endpoint_populates_edge_context_when_only_target_ids_given(conn, tmp_path, monkeypatch):
    """End-to-end version of the bug-1 fix: confirm the graph_events row
    emitted by /semantic/jobs has a non-empty edge_context."""
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="edge-context-hydrated",
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
                "target_ids": ["L7.1|L7.2|depends_on"],
            },
        )
    )
    assert status == 202
    assert payload["queued_count"] == 1
    edge_context = payload["events"][0]["payload"]["edge_context"]
    assert edge_context["src"] == "L7.1"
    assert edge_context["dst"] == "L7.2"
    assert edge_context["edge_type"] == "depends_on"
    # evidence should also flow through from the snapshot edge row when
    # present (the fixture sets it to {"source": "test-dependency"}).
    assert edge_context["evidence"] == {"source": "test-dependency"}


def test_semantic_job_cancel_routes_edge_job_to_graph_events(conn, tmp_path, monkeypatch):
    """Regression for MF 2026-05-11 / BACKLOG-EDGE-AI-ENRICH-BROKEN bug 3.

    Edge jobs live in graph_events, not graph_semantic_jobs. The cancel
    endpoint used to look up the job_id in graph_semantic_jobs only and
    raise ValidationError when not found — operator clicks Cancel on an
    edge job and gets 500. Now the endpoint detects edge-shaped job_id
    (parseable as `<src>|<dst>|<type>` or arrow form) and updates the
    matching graph_events row to status=stale.
    """
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="edge-cancel-test",
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
    # Submit the edge job via the public endpoint first.
    status, _payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "target_ids": ["L7.1|L7.2|depends_on"],
            },
        )
    )
    assert status == 202
    # Now cancel it — dashboard passes the edge_id as job_id.
    result = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "job_id": "L7.1|L7.2|depends_on",
            },
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert result["ok"] is True
    assert result["job"]["target_scope"] == "edge"
    assert result["job"]["edge_id"] == "L7.1|L7.2|depends_on"
    # dashboard-facing status comes from _edge_semantic_job_status, which
    # surfaces 'rejected' for an operator-cancelled edge event (main MF
    # 2026-05-10-011 split stale=auto-supersede from rejected=manual cancel;
    # the dashboard test was originally written against the older 'stale'
    # contract and is updated here to match main's semantics).
    assert result["job"]["status"] == "rejected"
    assert result["event"]["status"] == graph_events.EVENT_STATUS_REJECTED
    # And the counts now reflect the cancellation.
    assert result["summary"]["by_status"].get("rejected", 0) >= 1


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
    assert enriched["events"][0]["status"] == graph_events.EVENT_STATUS_PROPOSED

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-enriched"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_requested"
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_coverage_ratio"] == 0.0
    edge_jobs = server.handle_graph_governance_snapshot_semantic_jobs_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"target_scope": "edge"},
        )
    )
    assert edge_jobs["count"] == 1
    assert edge_jobs["jobs"][0]["status"] == "pending_review"
    assert edge_jobs["jobs"][0]["semantic"]["relation_purpose"] == "Feature Node calls Dependency Node."

    feedback_items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    edge_id_variants = set(server._semantic_edge_id_variants("L7.1|L7.2|depends_on"))
    edge_feedback = [
        item for item in feedback_items
        if item.get("target_type") == "edge" and item.get("target_id") in edge_id_variants
    ]
    assert edge_feedback, "inline edge semantic proposal must create review feedback"
    decision = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "feedback_ids": [edge_feedback[0]["feedback_id"]],
                "action": "accept_semantic_enrichment",
            },
        )
    )
    assert decision["semantic_enrichment_accepted"]["edge_ids_flipped"] == ["L7.1->L7.2:depends_on"]

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-accepted"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "Feature Node calls Dependency Node."
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_coverage_ratio"] == 1.0

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )
    semantic_health = summary["health"]["semantic_health"]
    assert semantic_health["edge_semantic_eligible_count"] == 1
    assert semantic_health["edge_semantic_current_count"] == 1
    assert semantic_health["edge_semantic_requested_count"] == 0
    assert semantic_health["edge_semantic_missing_count"] == 0
    assert semantic_health["edge_semantic_coverage_ratio"] == 1.0


def test_graph_governance_edge_semantic_inline_reject_stays_unprojected(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-inline-reject",
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
                        "relation_purpose": "Rejected payload must not become current.",
                        "confidence": 0.9,
                    }
                ],
                "actor": "semantic-ai",
            },
        )
    )
    assert status == 202
    event_id = enriched["events"][0]["event_id"]
    assert enriched["events"][0]["status"] == graph_events.EVENT_STATUS_PROPOSED

    feedback_items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    edge_feedback = [
        item for item in feedback_items
        if item.get("target_type") == "edge" and item.get("target_id") == "L7.1->L7.2:depends_on"
    ]
    assert edge_feedback
    decision = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "feedback_ids": [edge_feedback[0]["feedback_id"]],
                "action": "reject_false_positive",
            },
        )
    )
    assert decision["semantic_enrichment_rejected"]["edge_ids_cleared"] == ["L7.1->L7.2:depends_on"]
    event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], event_id)
    assert event["status"] == graph_events.EVENT_STATUS_REJECTED
    pending_edges = conn.execute(
        """
        SELECT COUNT(*) AS count FROM graph_semantic_edges
        WHERE project_id=? AND snapshot_id=? AND edge_id=?
        """,
        (PID, snapshot["snapshot_id"], "L7.1->L7.2:depends_on"),
    ).fetchone()
    assert pending_edges["count"] == 0

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-inline-reject"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_missing"
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_missing_count"] == 1


def test_edge_semantic_projection_accepts_dashboard_pipe_edge_ids(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-pipe-id",
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

    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id="L7.1|L7.2|depends_on",
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={
            "semantic_payload": {
                "relation_purpose": "Dashboard pipe id enriches the dependency.",
                "confidence": 0.9,
                "evidence": {"source": "semantic_ai"},
            }
        },
        created_by="dashboard",
    )

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-pipe-id"},
        )
    )

    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "Dashboard pipe id enriches the dependency."
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 0


def test_edge_semantic_projection_prefers_same_snapshot_pipe_ai_over_carried_rule(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    prev = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-carried-rule",
        commit_sha="prev",
        snapshot_kind="full",
        graph_json=graph,
    )
    current = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-current-ai",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    for snapshot in (prev, current):
        store.index_graph_snapshot(
            conn,
            PID,
            snapshot["snapshot_id"],
            nodes=graph["deps_graph"]["nodes"],
            edges=graph["deps_graph"]["edges"],
        )
    nodes_by_id = {node["id"]: node for node in graph["deps_graph"]["nodes"]}
    edge = graph["deps_graph"]["edges"][0]
    stable_edge_key = graph_events.stable_edge_key_for_edge(
        edge,
        nodes_by_id["L7.1"],
        nodes_by_id["L7.2"],
    )
    graph_events.create_event(
        conn,
        PID,
        prev["snapshot_id"],
        event_type="edge_semantic_enriched",
        event_kind="imported_semantic_cache",
        target_type="edge",
        target_id="L7.1->L7.2:depends_on",
        status=graph_events.EVENT_STATUS_OBSERVED,
        stable_node_key=stable_edge_key,
        payload={
            "semantic_payload": {
                "relation_purpose": "Rule fallback should not beat same-snapshot AI.",
                "confidence": 0.55,
                "evidence": {"source": "edge_semantic_rule"},
            }
        },
        created_by="carry-forward",
    )
    graph_events.create_event(
        conn,
        PID,
        current["snapshot_id"],
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id="L7.1|L7.2|depends_on",
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={
            "semantic_payload": {
                "relation_purpose": "Same snapshot pipe AI wins.",
                "confidence": 0.95,
                "evidence": {"source": "semantic_ai"},
            }
        },
        created_by="dashboard",
    )

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": current["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-current-ai"},
        )
    )

    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "Same snapshot pipe AI wins."
    assert edge_semantic["source_event"]["snapshot_id"] == current["snapshot_id"]
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_rule_count"] == 0


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


def test_edge_semantic_auto_enrich_ai_response_projects_current(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    auto_ai_body = server._edge_semantic_ai_body(
        {"options": {"auto_enrich": True}},
        "sid",
        auto_enrich=True,
    )
    assert auto_ai_body["semantic_use_ai"] is True
    assert "semantic_use_ai" not in server._edge_semantic_ai_body(
        {"semantic_mode": "auto"},
        "sid",
        auto_enrich=True,
    )
    assert server._edge_semantic_ai_body(
        {"semantic_use_ai": False},
        "sid",
        auto_enrich=True,
    )["semantic_use_ai"] is False

    ai_body = {}

    def fake_ai_call(_project_id, _root, _body):
        ai_body.update(_body)
        return lambda _stage, _payload: {
            "relation_purpose": "AI confirms the feature depends on the dependency.",
            "confidence": 0.93,
        }

    monkeypatch.setattr(server, "_semantic_ai_call_from_body", fake_ai_call)

    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-auto-ai",
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
                "target_ids": ["L7.1|L7.2|depends_on"],
                "options": {"mode": "auto_enrich", "auto_enrich": True},
                "actor": "dashboard_user",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["enriched_count"] == 1
    assert payload["ai_error_count"] == 0
    assert ai_body["semantic_use_ai"] is True
    assert payload["events"][-1]["status"] == graph_events.EVENT_STATUS_PROPOSED
    assert payload["jobs"][0]["semantic_source"] == "semantic_ai"
    assert payload["jobs"][0]["status"] == "pending_review"

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-auto-ai"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_requested"
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_missing_count"] == 1

    feedback_items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    edge_id_variants = set(server._semantic_edge_id_variants("L7.1|L7.2|depends_on"))
    edge_feedback = [
        item for item in feedback_items
        if item.get("target_type") == "edge" and item.get("target_id") in edge_id_variants
    ]
    assert edge_feedback
    server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "feedback_ids": [edge_feedback[0]["feedback_id"]],
                "action": "accept_semantic_enrichment",
            },
        )
    )
    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-auto-ai-accepted"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "AI confirms the feature depends on the dependency."
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 0


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


def test_projection_api_builds_and_fetches_branch_ref_specific_projection(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="api-branch-projection",
        commit_sha="commit-api-branch",
        snapshot_kind="scope",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    branch_ref = "refs/heads/codex/api-branch"
    node = graph["deps_graph"]["nodes"][0]
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_id="api-branch-semantic",
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_ACCEPTED,
        branch_ref=branch_ref,
        operation_type="accept",
        stable_node_key=graph_events.stable_node_key_for_node(node),
        feature_hash=graph_events.feature_hash_for_node(node),
        payload={"semantic_payload": {"feature_name": "API branch semantic"}},
        created_by="test",
    )

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "projection_id": "semproj-api-branch",
                "ref_name": branch_ref,
                "branch_ref": branch_ref,
                "backfill_existing": False,
            },
        )
    )
    fetched = server.handle_graph_governance_snapshot_semantic_projection_get(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"ref_name": branch_ref, "branch_ref": branch_ref},
        )
    )

    assert projected["ref_name"] == branch_ref
    assert projected["branch_ref"] == branch_ref
    assert fetched["projection_id"] == "semproj-api-branch"
    assert fetched["ref_name"] == branch_ref
    assert fetched["branch_ref"] == branch_ref
    assert fetched["projection"]["node_semantics"]["L7.1"]["semantic"]["feature_name"] == "API branch semantic"


def test_semantic_projection_rejects_target_id_fallback_when_lid_is_reused(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    old_graph = _graph("L7.1")
    old_node = old_graph["deps_graph"]["nodes"][0]
    old_node["title"] = "gateway.executors.plan_task"
    old_node["primary"] = ["gateway/executors/plan_task.py"]
    old_node["metadata"] = {"module": "gateway.executors.plan_task"}
    old_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-old-lid-owner",
        commit_sha="commit-old",
        snapshot_kind="scope",
        graph_json=old_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        old_snapshot["snapshot_id"],
        nodes=old_graph["deps_graph"]["nodes"],
        edges=old_graph["deps_graph"]["edges"],
    )
    graph_events.create_event(
        conn,
        PID,
        old_snapshot["snapshot_id"],
        event_id="old-lid-semantic",
        event_type="semantic_node_enriched",
        event_kind="semantic",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_OBSERVED,
        baseline_commit="commit-old",
        target_commit="commit-old",
        stable_node_key=graph_events.stable_node_key_for_node(old_node),
        feature_hash=graph_events.feature_hash_for_node(old_node),
        payload={
            "semantic_payload": {
                "feature_name": "plan_task executor",
                "primary": ["gateway/executors/plan_task.py"],
                "source_title": "gateway.executors.plan_task",
            }
        },
        created_by="test",
    )

    new_graph = _graph("L7.1")
    new_node = new_graph["deps_graph"]["nodes"][0]
    new_node["title"] = "frontend.dashboard.scripts.verify-acceptance"
    new_node["primary"] = ["frontend/dashboard/scripts/verify-acceptance.mjs"]
    new_node["metadata"] = {"module": "frontend.dashboard.scripts.verify-acceptance"}
    assert graph_events.stable_node_key_for_node(old_node) != graph_events.stable_node_key_for_node(new_node)

    new_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-new-lid-owner",
        commit_sha="commit-new",
        snapshot_kind="scope",
        parent_snapshot_id=old_snapshot["snapshot_id"],
        graph_json=new_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        new_snapshot["snapshot_id"],
        nodes=new_graph["deps_graph"]["nodes"],
        edges=new_graph["deps_graph"]["edges"],
    )
    conn.commit()

    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        new_snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )

    node_semantic = projection["projection"]["node_semantics"]["L7.1"]
    assert node_semantic["validity"]["status"] == "semantic_missing"
    assert node_semantic["semantic"] == {}
    assert node_semantic["source_event"]["event_id"] == ""


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


def test_graph_governance_summary_project_health_prefers_structure_when_no_legacy_score(conn):
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-health-taxonomy",
        commit_sha="health-taxonomy",
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
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-health-taxonomy",
        backfill_existing=False,
    )

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    health = summary["health"]
    assert health["structure_health_score"] > health["semantic_health_score"]
    assert health["project_health_score"] == health["structure_health_score"]


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
    assert attached["review_queue"]["queued"] is True
    assert attached["review_queue"]["operation_type"] == "graph_structure"
    assert attached["review_queue"]["subtype"] == "asset_binding"
    assert attached["review_queue"]["feedback"]["issue_type"] == "doc_binding_addition"

    status, remove_binding = server.handle_graph_governance_snapshot_file_hygiene_action(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "action": "remove_binding",
                "path": "docs/orphan.md",
                "target_node_id": "L7.1",
                "actor": "dashboard-user",
                "reason": "Proposal-safe binding removal test.",
            },
        )
    )
    assert status == 201
    assert remove_binding["event"]["event_type"] == "asset_binding_remove_requested"
    assert remove_binding["event"]["target_type"] == "file"
    assert remove_binding["event"]["target_id"] == "docs/orphan.md"
    assert remove_binding["event"]["risk_level"] == "high"
    assert remove_binding["event"]["payload"]["target_node_id"] == "L7.1"
    assert remove_binding["event"]["payload"]["destructive_mutation_performed"] is False
    assert remove_binding["review_queue"]["queued"] is True
    assert remove_binding["review_queue"]["feedback"]["requires_human_signoff"] is True
    assert remove_binding["review_queue"]["feedback"]["evidence"]["raw_issue"]["paths"] == ["docs/orphan.md"]
    assert "changed_files" not in remove_binding["review_queue"]["feedback"]["evidence"]["raw_issue"]

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"include_status_observations": "true"},
        )
    )
    binding_groups = [
        group for group in queue["groups"] if group["category"] in {"doc_binding", "asset_binding"}
    ]
    assert binding_groups
    assert any(
        "docs/orphan.md" in str(group.get("target_id") or group.get("representative_issue") or "")
        or "docs/orphan.md" in str(group.get("feedback_ids") or "")
        for group in binding_groups
    )
    assert all("graph_structure_lifecycle" not in group for group in binding_groups)

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


def test_graph_governance_file_hygiene_hint_attach_writes_source_hint(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    project = tmp_path / "project"
    doc = project / "docs" / "orphan.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Orphan\n\nNeeds binding.\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-hint-attach",
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
                "attached_node_ids": [],
                "size_bytes": 123,
            },
        ],
    )
    conn.commit()

    result = server.handle_graph_governance_snapshot_file_hygiene_hint_attach(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/orphan.md",
                "target_node_id": "L7.1",
                "project_root": str(project),
                "actor": "dashboard-user",
            },
        )
    )

    assert result["ok"] is True
    assert result["state"] == "written_uncommitted"
    assert result["requires_commit"] is True
    assert result["update_graph_after_commit"] is True
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("<!-- governance-hint ")
    assert '"target_node_id": "L7.1"' in text
    assert '"target_title": "Feature Node"' in text
    assert '"path": "docs/orphan.md"' in text

    second = server.handle_graph_governance_snapshot_file_hygiene_hint_attach(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/orphan.md",
                "target_node_id": "L7.1",
                "project_root": str(project),
                "actor": "dashboard-user",
            },
        )
    )
    assert second["already_present"] is True
    assert doc.read_text(encoding="utf-8").count("governance-hint") == 1


def test_graph_governance_file_hygiene_hint_unbind_appends_source_event(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    project = tmp_path / "project"
    doc = project / "docs" / "bound.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/bound.md","role":"doc","target_node_id":"L7.1"}}'
        " -->\n\n# Bound\n",
        encoding="utf-8",
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-hint-unbind",
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
                "path": "docs/bound.md",
                "file_kind": "doc",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "decision": "attach_to_node",
                "attached_node_ids": ["L7.1"],
                "size_bytes": 123,
            },
        ],
    )
    conn.commit()

    result = server.handle_graph_governance_snapshot_file_hygiene_hint_unbind(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/bound.md",
                "target_node_id": "L7.1",
                "project_root": str(project),
                "actor": "dashboard-user",
                "reason": "Wrong feature binding.",
            },
        )
    )

    assert result["ok"] is True
    assert result["state"] == "written_uncommitted"
    assert result["requires_commit"] is True
    assert result["update_graph_after_commit"] is True
    assert result["review_queue"]["queued"] is True
    text = doc.read_text(encoding="utf-8")
    assert '"attach_to_node"' in text
    assert '"asset_binding_event"' in text
    assert '"operation": "unbind"' in text
    assert text.index('"attach_to_node"') < text.index('"asset_binding_event"')

    second = server.handle_graph_governance_snapshot_file_hygiene_hint_unbind(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/bound.md",
                "target_node_id": "L7.1",
                "project_root": str(project),
                "actor": "dashboard-user",
                "reason": "Wrong feature binding.",
            },
        )
    )
    assert second["already_present"] is True
    assert doc.read_text(encoding="utf-8").count("governance-hint") == 2


def test_graph_governance_file_hygiene_hint_unbind_requires_current_binding(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    project = tmp_path / "project"
    doc = project / "docs" / "bound.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Bound\n", encoding="utf-8")
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-hint-unbind-guard",
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
    store.write_companion_files(
        PID,
        snapshot["snapshot_id"],
        graph_json=graph,
        file_inventory=[
            {
                "path": "docs/bound.md",
                "file_kind": "doc",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "decision": "attach_to_node",
                "attached_node_ids": ["L7.2"],
                "size_bytes": 123,
            },
        ],
    )
    conn.commit()

    with pytest.raises(ValidationError, match="existing accepted binding"):
        server.handle_graph_governance_snapshot_file_hygiene_hint_unbind(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={
                    "path": "docs/bound.md",
                    "target_node_id": "L7.1",
                    "project_root": str(project),
                    "actor": "dashboard-user",
                    "reason": "Wrong feature binding.",
                },
            )
        )
    assert "asset_binding_event" not in doc.read_text(encoding="utf-8")


def test_graph_governance_file_hygiene_hint_repair_stabilizes_and_withdraws_source_hint(
    conn,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    project = tmp_path / "project"
    doc = project / "docs" / "orphan.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "<!-- governance-hint "
        '{"attach_to_node":{"path":"docs/orphan.md","role":"doc","target_node_id":"L7.1"}}'
        " -->\n\n# Orphan\n",
        encoding="utf-8",
    )
    graph = _graph()
    graph["deps_graph"]["nodes"][0]["metadata"]["module"] = "agent.governance.server"
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-hint-repair",
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

    repaired = server.handle_graph_governance_snapshot_file_hygiene_hint_repair(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/orphan.md",
                "action": "stabilize",
                "project_root": str(project),
                "actor": "dashboard-user",
            },
        )
    )

    assert repaired["ok"] is True
    assert repaired["state"] == "written_uncommitted"
    assert repaired["changed"] is True
    text = doc.read_text(encoding="utf-8")
    assert '"target_module": "agent.governance.server"' in text
    assert '"target_title": "Feature Node"' in text

    withdrawn = server.handle_graph_governance_snapshot_file_hygiene_hint_repair(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/orphan.md",
                "action": "withdraw",
                "project_root": str(project),
                "actor": "dashboard-user",
            },
        )
    )

    assert withdrawn["ok"] is True
    assert withdrawn["withdrawn_count"] == 1
    assert "governance-hint" not in doc.read_text(encoding="utf-8")


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
    assert result["review_queue"]["queued"] is True
    assert len(result["review_queue"]["feedback"]) == 2
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
    assert fetched["trace"]["status"] == "running"

    finished = server.handle_graph_governance_query_trace_finish(
        _ctx(
            {"project_id": PID, "trace_id": trace_id},
            method="POST",
            body={"status": "complete"},
        )
    )
    assert finished["trace"]["status"] == "complete"

    one_shot = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "get_node",
                "args": {"node_id": "L7.1"},
            },
        )
    )
    assert one_shot["ok"] is True
    one_shot_trace = server.handle_graph_governance_query_trace_get(
        _ctx({"project_id": PID, "trace_id": one_shot["trace_id"]})
    )
    assert one_shot_trace["trace"]["status"] == "complete"
    assert one_shot_trace["trace"]["event_count"] == 1


def test_mf_sub_graph_query_requires_task_scope_and_uses_bounded_source(conn):
    _activate_basic_graph(conn, "full-query-mf-sub")

    with pytest.raises(ValidationError, match="task_id is required"):
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "subagent_context_build",
                    "parent_task_id": "subtask-1",
                    "worker_role": "mf_sub",
                    "fence_token": "fence-subtask-1",
                },
            )
        )

    with pytest.raises(ValidationError, match="parent_task_id is required"):
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "subagent_context_build",
                    "task_id": "subtask-1",
                    "worker_role": "mf_sub",
                    "fence_token": "fence-subtask-1",
                },
            )
        )

    with pytest.raises(ValidationError, match="worker_role=mf_sub is required"):
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "subagent_context_build",
                    "task_id": "subtask-1",
                    "parent_task_id": "parent-task-1",
                    "fence_token": "fence-subtask-1",
                },
            )
        )

    with pytest.raises(ValidationError, match="fence_token is required"):
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "subagent_context_build",
                    "task_id": "subtask-1",
                    "parent_task_id": "parent-task-1",
                    "worker_role": "mf_sub",
                },
            )
        )

    with pytest.raises(server.GovernanceError) as unsupported_purpose:
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "observer_private_context",
                    "task_id": "subtask-1",
                    "parent_task_id": "parent-task-1",
                    "worker_role": "mf_sub",
                    "fence_token": "fence-subtask-1",
                },
            )
        )
    assert unsupported_purpose.value.code == (
        "unsupported_mf_subagent_graph_query_purpose"
    )

    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="subtask-1",
            root_task_id="parent-task-1",
            stage_task_id="subtask-1",
            worker_id="mf-worker-1",
            branch_ref="refs/heads/codex/subtask-1",
            status="worktree_ready",
            fence_token="fence-subtask-1",
        ),
    )
    conn.commit()

    queried = server.handle_graph_governance_query(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "query_schema",
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "task_id": "subtask-1",
                "parent_task_id": "parent-task-1",
                "worker_role": "mf_sub",
                "fence_token": "fence-subtask-1",
            },
        )
    )

    assert queried["ok"] is True
    assert "mf_subagent" in queried["result"]["query_sources"]
    fetched = server.handle_graph_governance_query_trace_get(
        _ctx_with_role(
            {"project_id": PID, "trace_id": queried["trace_id"]},
            "mf_sub",
        )
    )
    assert fetched["trace"]["query_source"] == "mf_subagent"
    assert fetched["trace"]["parent_task_id"] == "parent-task-1"


def test_mf_sub_graph_query_rejects_unknown_task_id_and_fake_fence(conn):
    _activate_basic_graph(conn, "full-query-mf-sub-unknown")

    with pytest.raises(GovernanceError) as exc_info:
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "subagent_context_build",
                    "task_id": "ghost-subtask",
                    "parent_task_id": "ghost-parent",
                    "worker_role": "mf_sub",
                    "fence_token": "fake-fence-token",
                },
            )
        )

    assert exc_info.value.code == "fence_invalidated_or_unknown"
    assert exc_info.value.status == 403


def test_mf_sub_graph_query_rejects_cross_fence_mismatch(conn):
    _activate_basic_graph(conn, "full-query-mf-sub-cross-fence")
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="worker-a",
            root_task_id="parent-a",
            stage_task_id="worker-a",
            worker_id="worker-a",
            branch_ref="refs/heads/codex/worker-a",
            status="worktree_ready",
            fence_token="fence-worker-a",
        ),
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="worker-b",
            root_task_id="parent-b",
            stage_task_id="worker-b",
            worker_id="worker-b",
            branch_ref="refs/heads/codex/worker-b",
            status="worktree_ready",
            fence_token="fence-worker-b",
        ),
    )
    conn.commit()

    with pytest.raises(GovernanceError) as exc_info:
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "subagent_context_build",
                    "task_id": "worker-a",
                    "parent_task_id": "parent-a",
                    "worker_role": "mf_sub",
                    "fence_token": "fence-worker-b",
                },
            )
        )

    assert exc_info.value.code == "fence_invalidated_or_unknown"
    assert exc_info.value.status == 403


def test_mf_sub_graph_query_valid_active_identity_succeeds_and_records_trace_context(conn):
    _activate_basic_graph(conn, "full-query-mf-sub-valid-context")
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="worker-valid",
            root_task_id="parent-valid",
            stage_task_id="worker-valid",
            worker_id="worker-valid",
            branch_ref="refs/heads/codex/worker-valid",
            status="worktree_ready",
            fence_token="fence-worker-valid",
            merge_queue_id="mergeq-valid",
        ),
    )
    conn.commit()

    queried = server.handle_graph_governance_query(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "query_schema",
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "task_id": "worker-valid",
                "parent_task_id": "parent-valid",
                "worker_role": "mf_sub",
                "fence_token": "fence-worker-valid",
            },
        )
    )

    assert queried["ok"] is True
    fetched = server.handle_graph_governance_query_trace_get(
        _ctx_with_role(
            {"project_id": PID, "trace_id": queried["trace_id"]},
            "mf_sub",
        )
    )
    trace = fetched["trace"]
    assert trace["query_source"] == "mf_subagent"
    assert trace["parent_task_id"] == "parent-valid"
    assert trace["run_id"].startswith("mf_subagent:worker-valid:")
    assert trace["fence_token_redacted"] is True
    assert trace["graph_query_identity"]["fence_token_redacted"] is True
    assert trace["fence_token_hash"]
    assert "fence-worker-valid" not in json.dumps(trace, sort_keys=True)


def test_mf_sub_graph_query_accepts_target_project_with_governance_fence(conn, tmp_path):
    target_project_id = "target-graph-project"
    target_root = tmp_path / "target-graph-project"
    target_root.mkdir()
    _activate_basic_graph(
        conn,
        "full-query-mf-sub-target-project",
        project_id=target_project_id,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            governance_project_id=PID,
            target_project_id=target_project_id,
            target_project_root=str(target_root),
            task_id="worker-target-project",
            root_task_id="parent-target-project",
            stage_task_id="worker-target-project",
            worker_id="worker-target-project",
            worker_slot_id="worker-target-project",
            branch_ref="refs/heads/codex/worker-target-project",
            status="worktree_ready",
            fence_token="fence-target-project",
        ),
    )
    conn.commit()

    queried = server.handle_graph_governance_query(
        _ctx_with_role(
            {"project_id": target_project_id},
            "mf_sub",
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "query_schema",
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "task_id": "worker-target-project",
                "parent_task_id": "parent-target-project",
                "worker_role": "mf_sub",
                "fence_token": "fence-target-project",
                "governance_project_id": PID,
                "target_project_id": target_project_id,
                "target_project_root": str(target_root),
            },
        )
    )

    assert queried["ok"] is True
    fetched = server.handle_graph_governance_query_trace_get(
        _ctx_with_role(
            {"project_id": target_project_id, "trace_id": queried["trace_id"]},
            "mf_sub",
        )
    )
    trace = fetched["trace"]
    assert trace["query_source"] == "mf_subagent"
    assert trace["parent_task_id"] == "parent-target-project"
    assert trace["run_id"].startswith("mf_subagent:worker-target-project:")


def test_mf_sub_graph_query_accepts_context_allocated_by_parallel_branch_api(conn):
    _activate_basic_graph(conn, "full-query-mf-sub-allocated-context")

    status_code, allocated = server.handle_graph_governance_parallel_branch_allocate(
        _ctx_with_role(
            {"project_id": PID},
            "observer",
            method="POST",
            body={
                "task_id": "worker-allocated",
                "parent_task_id": "parent-allocated",
                "worker_id": "worker-allocated",
                "fence_token": "fence-worker-allocated",
                "base_commit": "base-sha",
                "target_head_commit": "target-sha",
                "merge_queue_id": "mergeq-allocated",
                "create_worktree": False,
            },
        )
    )

    assert status_code == 201
    assert allocated["ok"] is True
    assert allocated["context"]["task_id"] == "worker-allocated"
    assert allocated["context"]["root_task_id"] == "parent-allocated"

    queried = server.handle_graph_governance_query(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "query_schema",
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "task_id": "worker-allocated",
                "parent_task_id": "parent-allocated",
                "worker_role": "mf_sub",
                "fence_token": "fence-worker-allocated",
            },
        )
    )

    assert queried["ok"] is True


def test_mf_sub_graph_query_rejects_inactive_runtime_context(conn):
    _activate_basic_graph(conn, "full-query-mf-sub-inactive-context")
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="worker-failed",
            root_task_id="parent-failed",
            stage_task_id="worker-failed",
            worker_id="worker-failed",
            branch_ref="refs/heads/codex/worker-failed",
            status=STATE_MERGE_FAILED,
            fence_token="fence-worker-failed",
        ),
    )
    conn.commit()

    with pytest.raises(GovernanceError) as exc_info:
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "query_purpose": "subagent_context_build",
                    "task_id": "worker-failed",
                    "parent_task_id": "parent-failed",
                    "worker_role": "mf_sub",
                    "fence_token": "fence-worker-failed",
                },
            )
        )

    assert exc_info.value.code == "fence_invalidated_or_unknown"
    assert exc_info.value.status == 403


def test_graph_governance_query_api_exposes_graph_native_discovery(conn):
    graph = _graph()
    graph["deps_graph"]["nodes"][0]["metadata"]["functions"] = [
        "agent.governance.server::serve"
    ]
    graph["deps_graph"]["nodes"][0]["metadata"]["function_lines"] = {"serve": [5, 9]}
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-query-native-api",
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

    schema = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"snapshot_id": "active", "tool": "query_schema"},
        )
    )
    assert "find_node_by_path" in schema["result"]["tool_names"]

    by_path = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "find_node_by_path",
                "args": {"path": "agent/governance/server.py"},
            },
        )
    )
    assert by_path["result"]["matches"][0]["node"]["node_id"] == "L7.1"

    functions = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "function_index",
                "args": {"query": "serve"},
            },
        )
    )
    assert functions["result"]["matches"][0]["line_start"] == 5


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


def test_pending_scope_materialize_auto_creates_running_row(conn, tmp_path, monkeypatch):
    """Direct Update graph should not require a prior /pending-scope call."""
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)

    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-old",
        commit_sha="old",
        snapshot_kind="scope",
        graph_json=_graph("L7.1"),
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"])
    conn.commit()

    captured = {}

    def fake_run_pending_scope(conn_arg, project_id, root, **kwargs):
        rows = store.list_pending_scope_reconcile(
            conn_arg,
            project_id,
            commit_shas=["head"],
            statuses=[store.PENDING_STATUS_RUNNING],
        )
        captured["rows"] = rows
        captured["kwargs"] = kwargs
        conn_arg.execute(
            """
            UPDATE pending_scope_reconcile
            SET status = ?, snapshot_id = ?
            WHERE project_id = ? AND commit_sha = ?
            """,
            (store.PENDING_STATUS_MATERIALIZED, "scope-head", project_id, "head"),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": "scope-head",
            "covered_pending_count": 1,
            "pending_rows_bound": 1,
        }

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fake_run_pending_scope)

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "target_commit_sha": "head",
                "parent_commit_sha": "old",
                "actor": "dashboard",
                "activate": True,
            },
        )
    )

    assert code == 201
    assert result["ok"] is True
    assert captured["kwargs"]["target_commit_sha"] == "head"
    assert captured["rows"], "handler should create a running row before materializing"
    assert captured["rows"][0]["status"] == store.PENDING_STATUS_RUNNING

    final_rows = store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"])
    assert final_rows[0]["status"] == store.PENDING_STATUS_MATERIALIZED


def test_pending_scope_materialize_infers_head_when_target_commit_missing(conn, tmp_path, monkeypatch):
    """New-user Update graph calls should not dead-end when target_commit_sha is omitted."""
    from agent.governance import batch_jobs, state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)
    monkeypatch.setattr(batch_jobs, "git_commit", lambda root: "head")

    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-old-missing-target",
        commit_sha="old",
        snapshot_kind="scope",
        graph_json=_graph("L7.1"),
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"])
    conn.commit()

    captured = {}

    def fake_run_pending_scope(conn_arg, project_id, root, **kwargs):
        rows = store.list_pending_scope_reconcile(
            conn_arg,
            project_id,
            commit_shas=["head"],
            statuses=[store.PENDING_STATUS_RUNNING],
        )
        captured["rows"] = rows
        captured["kwargs"] = kwargs
        conn_arg.execute(
            """
            UPDATE pending_scope_reconcile
            SET status = ?, snapshot_id = ?
            WHERE project_id = ? AND commit_sha = ?
            """,
            (store.PENDING_STATUS_MATERIALIZED, "scope-head-inferred", project_id, "head"),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": "scope-head-inferred",
            "covered_pending_count": 1,
            "pending_rows_bound": 1,
        }

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fake_run_pending_scope)

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "parent_commit_sha": "old",
                "actor": "dashboard",
                "activate": True,
            },
        )
    )

    assert code == 201
    assert result["ok"] is True
    assert captured["kwargs"]["target_commit_sha"] == "head"
    assert captured["rows"], "handler should infer HEAD and create a running pending row"
    assert captured["rows"][0]["status"] == store.PENDING_STATUS_RUNNING


def test_pending_scope_materialize_missing_target_commit_returns_actionable_error(
    conn, tmp_path, monkeypatch
):
    from agent.governance import batch_jobs

    project_root = tmp_path / "not-git"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)

    def fail_git_commit(_root):
        raise batch_jobs.BatchJobError("not a git repository")

    monkeypatch.setattr(batch_jobs, "git_commit", fail_git_commit)

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"actor": "dashboard", "activate": True},
        )
    )

    assert code == 400
    assert result["ok"] is False
    assert result["reason"] == "target_commit_sha_required"
    assert result["recommended_body"]["target_commit_sha"] == "<git HEAD commit sha>"
    assert result["recommended_body"]["activate"] is True


def test_pending_scope_queue_allows_same_commit_on_different_refs(conn):
    code_main, main = server.handle_graph_governance_pending_scope_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "commit_sha": "same-head",
                "parent_commit_sha": "base",
                "branch_ref": "refs/heads/main",
            },
        )
    )
    code_feature, feature = server.handle_graph_governance_pending_scope_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "commit_sha": "same-head",
                "parent_commit_sha": "base",
                "branch_ref": "refs/heads/feature",
            },
        )
    )

    assert code_main == 201
    assert code_feature == 201
    assert main["pending_scope_reconcile"]["ref_name"] == "refs/heads/main"
    assert feature["pending_scope_reconcile"]["ref_name"] == "refs/heads/feature"

    rows = store.list_pending_scope_reconcile(conn, PID, commit_shas=["same-head"])
    assert {row["ref_name"] for row in rows} == {"refs/heads/main", "refs/heads/feature"}

    feature_rows = store.list_pending_scope_reconcile(
        conn,
        PID,
        commit_shas=["same-head"],
        branch_ref="refs/heads/feature",
    )
    assert len(feature_rows) == 1
    assert feature_rows[0]["ref_name"] == "refs/heads/feature"


def test_pending_scope_schema_migrates_commit_only_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE pending_scope_reconcile (
          project_id TEXT NOT NULL,
          commit_sha TEXT NOT NULL,
          parent_commit_sha TEXT NOT NULL DEFAULT '',
          queued_at TEXT NOT NULL,
          status TEXT NOT NULL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          snapshot_id TEXT NOT NULL DEFAULT '',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(project_id, commit_sha)
        )
        """
    )
    c.execute(
        """
        INSERT INTO pending_scope_reconcile
          (project_id, commit_sha, parent_commit_sha, queued_at, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (PID, "same-head", "base", "2026-05-17T00:00:00Z", store.PENDING_STATUS_QUEUED),
    )

    store.ensure_schema(c)
    store.queue_pending_scope_reconcile(
        c,
        PID,
        commit_sha="same-head",
        parent_commit_sha="base",
        branch_ref="refs/heads/feature",
    )

    rows = store.list_pending_scope_reconcile(c, PID, commit_shas=["same-head"])
    assert {row["ref_name"] for row in rows} == {"active", "refs/heads/feature"}
    c.close()


def test_pending_scope_materialize_selects_branch_worktree_identity(conn, tmp_path, monkeypatch):
    from agent.governance import state_reconcile

    worktree = tmp_path / "feature-worktree"
    worktree.mkdir()
    active_identity = store.normalize_pending_scope_identity()
    feature_identity = store.normalize_pending_scope_identity(
        branch_ref="codex/feature",
        worktree_path=str(worktree),
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="same-head",
        parent_commit_sha="base",
        ref_name=active_identity["ref_name"],
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="same-head",
        parent_commit_sha="base",
        branch_ref=feature_identity["branch_ref"],
        worktree_id=feature_identity["worktree_id"],
        worktree_path=feature_identity["worktree_path"],
    )
    conn.commit()

    captured = {}

    def fake_run_pending_scope(conn_arg, project_id, root, **kwargs):
        captured["root"] = Path(root)
        captured["kwargs"] = kwargs
        rows = store.list_pending_scope_reconcile(
            conn_arg,
            project_id,
            commit_shas=["same-head"],
            ref_name=kwargs["ref_name"],
            branch_ref=kwargs["branch_ref"],
            worktree_id=kwargs["worktree_id"],
            worktree_path=kwargs["worktree_path"],
            statuses=[store.PENDING_STATUS_QUEUED],
        )
        assert len(rows) == 1
        assert rows[0]["ref_name"] == "codex/feature"
        conn_arg.execute(
            """
            UPDATE pending_scope_reconcile
            SET status = ?, snapshot_id = ?
            WHERE project_id = ? AND ref_name = ? AND worktree_id = ? AND commit_sha = ?
            """,
            (
                store.PENDING_STATUS_MATERIALIZED,
                "scope-feature",
                project_id,
                kwargs["ref_name"],
                kwargs["worktree_id"],
                "same-head",
            ),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": "scope-feature",
            "covered_pending_count": 1,
            "pending_rows_bound": 1,
            "ref_name": kwargs["ref_name"],
            "worktree_id": kwargs["worktree_id"],
        }

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fake_run_pending_scope)

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "worktree_path": str(worktree),
                "target_commit_sha": "same-head",
                "branch_ref": "codex/feature",
                "actor": "dashboard",
            },
        )
    )

    assert code == 201
    assert result["ok"] is True
    assert captured["root"] == worktree.resolve()
    assert captured["kwargs"]["ref_name"] == "codex/feature"
    assert captured["kwargs"]["worktree_id"] == feature_identity["worktree_id"]

    active_rows = store.list_pending_scope_reconcile(
        conn,
        PID,
        commit_shas=["same-head"],
        ref_name=active_identity["ref_name"],
    )
    feature_rows = store.list_pending_scope_reconcile(
        conn,
        PID,
        commit_shas=["same-head"],
        ref_name=feature_identity["ref_name"],
        worktree_id=feature_identity["worktree_id"],
    )
    assert active_rows[0]["status"] == store.PENDING_STATUS_QUEUED
    assert feature_rows[0]["status"] == store.PENDING_STATUS_MATERIALIZED


def test_pending_scope_materialize_already_current_is_idempotent(conn, tmp_path, monkeypatch):
    """Direct Update graph should treat an active target commit as a no-op success."""
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)

    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-head",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=_graph("L7.1"),
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"])
    conn.commit()

    def fail_if_materializer_runs(*_args, **_kwargs):
        raise AssertionError("already-current direct update should not rematerialize")

    monkeypatch.setattr(
        state_reconcile,
        "run_pending_scope_reconcile_candidate",
        fail_if_materializer_runs,
    )

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "target_commit_sha": "head",
                "parent_commit_sha": "head",
                "actor": "dashboard",
                "activate": True,
            },
        )
    )

    assert code == 200
    assert result["ok"] is True
    assert result["status"] == "already_current"
    assert result["snapshot_id"] == "scope-head"
    assert store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"]) == []


def test_pending_scope_materialize_existing_running_failure_becomes_recoverable(conn, tmp_path, monkeypatch):
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "previous_direct_update"},
    )
    conn.commit()

    def fail_materialize(*_args, **_kwargs):
        raise RuntimeError("client disconnected during materialize")

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fail_materialize)

    with pytest.raises(RuntimeError):
        server.handle_graph_governance_pending_scope_materialize(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "target_commit_sha": "head",
                    "parent_commit_sha": "old",
                    "actor": "dashboard",
                    "activate": True,
                },
            )
        )

    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED
    evidence = json.loads(row["evidence_json"])
    assert evidence["recoverable"] is True
    assert evidence["recovery_action"] == "force_requeue_pending_scope"
    assert "client disconnected" in evidence["reason"]


def test_pending_scope_catch_up_queues_range_and_materializes_head(conn, tmp_path, monkeypatch):
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "c3")
    monkeypatch.setattr(server, "_git_commit_range", lambda _root, _base, _target: ["c1", "c2", "c3"])
    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-base",
        commit_sha="base",
        snapshot_kind="scope",
        graph_json=_graph("L7.1"),
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"])
    conn.commit()

    captured = {}

    def fake_materialize(conn_arg, project_id, root, **kwargs):
        rows = store.list_pending_scope_reconcile(conn_arg, project_id)
        captured["rows"] = rows
        captured["kwargs"] = kwargs
        for row in rows:
            conn_arg.execute(
                """
                UPDATE pending_scope_reconcile
                SET status=?, snapshot_id=?
                WHERE project_id=? AND commit_sha=?
                """,
                (store.PENDING_STATUS_MATERIALIZED, "scope-c3", project_id, row["commit_sha"]),
            )
        return {
            "ok": True,
            "snapshot_id": "scope-c3",
            "covered_commit_shas": ["c1", "c2", "c3"],
        }

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fake_materialize)

    code, result = server.handle_graph_governance_pending_scope_catch_up(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"target_commit_sha": "c3", "activate": True, "actor": "dashboard"},
        )
    )

    assert code == 201
    assert result["commit_count"] == 3
    assert result["progress"] == {"done": 3, "total": 3}
    assert captured["kwargs"]["target_commit_sha"] == "c3"
    assert [row["commit_sha"] for row in captured["rows"]] == ["c1", "c2", "c3"]
    assert [item["covered"] for item in result["commits"]] == [True, True, True]


def test_reconcile_metrics_endpoint_reports_speedup(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="fast",
        snapshot_id="scope-fast",
        strategy="incremental_graph_delta",
        graph_delta_mode="metadata_only",
        status="ok",
        elapsed_ms=5000,
    )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="full",
        snapshot_id="scope-full",
        strategy="full_rebuild_fallback",
        graph_delta_mode="full_rebuild",
        status="ok",
        elapsed_ms=40000,
    )
    conn.commit()

    result = server.handle_graph_governance_reconcile_metrics(
        _ctx({"project_id": PID}, query={"backfill": "false"})
    )

    assert result["ok"] is True
    assert result["summary"]["speedup"]["speedup_x"] == 8
    assert result["summary"]["speedup"]["elapsed_reduction_pct"] == 87.5
    assert {row["run_id"] for row in result["metrics"]} == {"fast", "full"}


def test_pending_scope_recover_stale_endpoint_marks_running_failed(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="old-running",
        parent_commit_sha="base",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "direct_update_graph"},
    )
    conn.execute(
        """
        UPDATE pending_scope_reconcile
        SET queued_at='2026-01-01T00:00:00Z'
        WHERE project_id=? AND commit_sha=?
        """,
        (PID, "old-running"),
    )
    conn.commit()

    code, result = server.handle_graph_governance_pending_scope_recover_stale(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"max_running_seconds": 1, "actor": "dashboard"},
        )
    )

    assert code == 200
    assert result["recovered_count"] == 1
    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["old-running"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED


def test_stale_artifact_cleanup_api_dry_run_and_apply(conn, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)

    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    created = batch_jobs.create_batch_task(
        conn,
        PID,
        "cleanup api",
        repo_root_path=repo,
        batch_id="cleanup-api",
        base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    batch_jobs.create_worktree(strategy, repo_root_path=repo)
    batch_jobs.record_task_batch_state(conn, created["task_id"], "abandoned")
    conn.commit()

    dry_run = server.handle_graph_governance_stale_artifact_cleanup(_ctx({"project_id": PID}))

    candidate = next(item for item in dry_run["candidates"] if item["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is True

    applied = server.handle_graph_governance_stale_artifact_cleanup_apply(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "candidate_ids": [candidate["candidate_id"]],
                "actor": "test",
                "reason": "api cleanup",
            },
        )
    )

    assert applied["ok"] is True
    assert applied["applied_count"] == 1
    assert not (repo / ".worktrees" / "batch-cleanup-api").exists()


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


def test_graph_governance_semantic_chunk_fix_replay_api(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    functions = [
        {
            "name": f"generated_{idx}",
            "path": "agent/governance/large_replay.py",
            "lineno": idx + 1,
        }
        for idx in range(4)
    ]
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.replay",
                    "layer": "L7",
                    "title": "Large Replay Node",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/large_replay.py"],
                    "metadata": {
                        "subsystem": "semantic",
                        "function_count": len(functions),
                        "functions": functions,
                    },
                }
            ],
            "edges": [],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-chunk-replay-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    source_trace = project / "semantic-source-trace"

    def write_trace(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    write_trace(
        source_trace / "feature-inputs" / "L7.replay.json",
        {
            "graph_query_audit": {
                "status": "complete",
                "trace_id": "gqt-api-replay",
            }
        },
    )
    self_check = {
        "required": True,
        "valid": True,
        "status": "passed",
        "checked_rules": [
            "required_fields_present",
            "source_payload_only",
            "no_project_mutation",
            "review_feedback_accounted_for",
            "graph_suggestions_contract_checked",
        ],
        "checked_rules_count": 5,
        "repair_attempts": 0,
        "max_repair_attempts": 1,
        "known_risks": [],
    }
    for idx in range(2):
        write_trace(
            source_trace / "chunk-outputs" / f"L7.replay-slice-{idx:03d}.json",
            {
                "node_id": "L7.replay",
                "slice_id": f"L7.replay-slice-{idx:03d}",
                "slice_index": idx,
                "ai_response_present": True,
                "ai_error": "",
                "ai_response": {
                    "feature_name": f"Large Replay Node slice {idx}",
                    "semantic_summary": f"Persisted slice {idx}.",
                    "intent": f"slice-{idx}",
                    "domain_label": "semantic.slice",
                    "self_check": self_check,
                },
            },
        )
    write_trace(
        source_trace / "feature-outputs" / "L7.replay.json",
        {
            "node_id": "L7.replay",
            "ai_response_present": True,
            "ai_response": {
                "node_id": "L7.replay",
                "feature_name": "Large Replay Node slice 0",
                "semantic_summary": "Persisted aggregate still slice scoped.",
                "intent": "slice-0",
                "domain_label": "semantic.slice",
                "self_check": self_check,
                "semantic_chunking": {
                    "mode": "function_slices",
                    "status": "complete",
                    "slice_count": 2,
                    "completed_slice_count": 2,
                },
            },
        },
    )
    stages: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        stages.append(stage)
        assert stage == "reconcile_semantic_chunk_fix"
        assert payload["instructions"]["job_type"] == "chunk_fix"
        return {
            "feature_name": "Large Replay Runtime",
            "semantic_summary": "API replay repaired completed chunk outputs.",
            "intent": "coordinate replayed chunks",
            "domain_label": "semantic.large_node",
            "self_check": self_check,
            "_ai_route": {"provider": "openai", "model": "gpt-5.5"},
        }

    monkeypatch.setattr(
        server,
        "_semantic_ai_call_from_body",
        lambda *_args, **_kwargs: fake_ai,
    )

    code, result = server.handle_graph_governance_snapshot_semantic_chunk_fix_replay(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-chunk-replay-api"},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "node_id": "L7.replay",
                "source_trace_dir": str(source_trace),
                "trace_dir": str(project / "semantic-replay-trace"),
                "semantic_ai_chunk_context_mode": "function_index",
                "semantic_ai_chunk_max_functions_per_slice": 2,
            },
        )
    )

    assert code == 200
    assert result["ok"] is True
    assert result["complete_count"] == 1
    assert stages == ["reconcile_semantic_chunk_fix"]
    assert (
        project / "semantic-replay-trace" / "chunk-fix-inputs" / "L7.replay.json"
    ).exists()
    row = conn.execute(
        """
        SELECT status, semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-chunk-replay-api", "L7.replay"),
    ).fetchone()
    assert row["status"] == "pending_review"
    semantic_json = json.loads(row["semantic_json"])
    assert semantic_json["feature_name"] == "Large Replay Runtime"
    event = conn.execute(
        """
        SELECT status FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND event_type='semantic_node_enriched'
          AND target_id=?
        """,
        (PID, "full-semantic-chunk-replay-api", "L7.replay"),
    ).fetchone()
    assert event["status"] == "proposed"


def test_graph_governance_semantic_chunk_fix_replay_api_dry_run_not_needed_no_db_write(
    conn,
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.replay",
                    "layer": "L7",
                    "title": "Large Replay Node",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/large_replay.py"],
                    "metadata": {
                        "subsystem": "semantic",
                        "function_count": 1,
                        "functions": [
                            {
                                "name": "generated",
                                "path": "agent/governance/large_replay.py",
                                "lineno": 1,
                            }
                        ],
                    },
                }
            ],
            "edges": [],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-chunk-replay-api-dry-run",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    source_trace = project / "semantic-source-trace"

    def write_trace(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    self_check = {
        "required": True,
        "valid": True,
        "status": "passed",
        "checked_rules": [
            "required_fields_present",
            "source_payload_only",
            "no_project_mutation",
            "review_feedback_accounted_for",
            "graph_suggestions_contract_checked",
        ],
        "checked_rules_count": 5,
        "repair_attempts": 0,
        "max_repair_attempts": 1,
        "known_risks": [],
    }
    write_trace(
        source_trace / "chunk-outputs" / "L7.replay-slice-000.json",
        {
            "node_id": "L7.replay",
            "slice_id": "L7.replay-slice-000",
            "slice_index": 0,
            "ai_response_present": True,
            "ai_error": "",
            "ai_response": {
                "feature_name": "Large Replay Runtime Part",
                "semantic_summary": "Persisted output for replay.",
                "intent": "describe replay runtime behavior",
                "domain_label": "semantic.large_node",
                "self_check": self_check,
            },
        },
    )
    write_trace(
        source_trace / "feature-outputs" / "L7.replay.json",
        {
            "node_id": "L7.replay",
            "ai_response_present": True,
            "ai_response": {
                "node_id": "L7.replay",
                "feature_name": "Large Replay Runtime",
                "semantic_summary": "Persisted aggregate is already node scoped.",
                "intent": "coordinate replayed chunk outputs",
                "domain_label": "semantic.large_node",
                "self_check": self_check,
                "semantic_chunking": {
                    "mode": "function_slices",
                    "status": "complete",
                    "slice_count": 1,
                    "completed_slice_count": 1,
                },
            },
        },
    )
    monkeypatch.setattr(
        server,
        "_semantic_ai_call_from_body",
        lambda *_args, **_kwargs: pytest.fail("dry-run replay must not create an AI call"),
    )

    code, result = server.handle_graph_governance_snapshot_semantic_chunk_fix_replay(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-chunk-replay-api-dry-run"},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "node_id": "L7.replay",
                "source_trace_dir": str(source_trace),
                "dry_run": True,
                "persist_feature_payloads": False,
            },
        )
    )

    assert code == 200
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["results"][0]["status"] == "not_needed"
    row = conn.execute(
        """
        SELECT status FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-chunk-replay-api-dry-run", "L7.replay"),
    ).fetchone()
    assert row is None


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
    rows = conn.execute(
        "SELECT COUNT(*) AS count FROM graph_semantic_jobs WHERE project_id=? AND snapshot_id=?",
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert rows["count"] == 0


def test_graph_governance_semantic_enrich_enqueue_stale_publishes_worker_event(
    conn, tmp_path, monkeypatch
):
    from agent.governance import event_bus

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_bus, "publish", lambda event, payload: published.append((event, payload)))
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-enqueue-stale-api",
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
                "enqueue_stale": True,
            },
        )
    )

    assert enriched["ok"] is True
    rows = conn.execute(
        "SELECT node_id, status FROM graph_semantic_jobs WHERE project_id=? AND snapshot_id=?",
        (PID, snapshot["snapshot_id"]),
    ).fetchall()
    assert [dict(row) for row in rows] == [{"node_id": "L7.1", "status": "ai_pending"}]
    assert published == [
        (
            "semantic_job.enqueued",
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "queued_count": 1,
                "target_scope": "node",
                "source": "semantic_enrich_api",
            },
        )
    ]


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


def test_graph_governance_feedback_queue_exposes_category_metadata(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-category-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    cases = [
        ("graph_correction", "add_typed_relation", "dependency_patch_suggestions", "Add relation.", "graph_structure"),
        ("graph_correction", "add_doc_binding", "asset_binding_proposal", "Bind docs/runbook.md.", "doc_binding"),
        ("graph_correction", "test_binding_realign", "asset_binding_proposal", "Bind tests/test_router.py.", "test_binding"),
        ("graph_correction", "config_binding_addition", "asset_binding_proposal", "Bind config/router.yml.", "config_binding"),
        ("graph_correction", "asset_binding", "asset_binding_proposal", "Bind generated asset.", "asset_binding"),
        ("needs_observer_decision", "semantic_memory_update", "ai_enrich", "Review semantic memory.", "semantic"),
        ("needs_observer_decision", "graph_enrich_config", "registered_action_needed", "Add enricher predicate.", "graph_enrich_config"),
        ("project_improvement", "unit_test_gap", "dashboard feedback", "Add focused tests.", "backlog"),
        ("status_observation", "stale_test_expectation", "status_observation", "Stale test expectation.", "status_observation"),
    ]
    for index, (kind, issue_type, reason, summary, _category) in enumerate(cases):
        reconcile_feedback.submit_feedback_item(
            PID,
            snapshot["snapshot_id"],
            feedback_kind=kind,
            source_round="round-category",
            actor="observer",
            issue={
                "node_id": "L7.1",
                "type": issue_type,
                "reason": reason,
                "summary": summary,
                "target": f"target-{index}",
            },
        )

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={
                "source_round": "round-category",
                "include_status_observations": "true",
                "group_by": "target",
            },
        )
    )

    expected_categories = {category for *_prefix, category in cases}
    assert queue["group_count"] == len(cases)
    assert {group["category"] for group in queue["groups"]} == expected_categories
    assert all(group["category_label"] for group in queue["groups"])
    assert queue["summary"]["by_category_all_items"] == {
        category: 1 for category in sorted(expected_categories)
    }
    assert queue["summary"]["by_category_visible_groups"] == {
        category: 1 for category in sorted(expected_categories)
    }
    assert set(queue["action_catalog"]["categories"]) >= expected_categories
    assert queue["action_catalog"]["categories"]["graph_structure"]["label"] == "Graph structure"
    assert "category_order" in queue["action_catalog"]


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


def test_graph_governance_feedback_queue_merges_db_semantics_when_state_artifact_partial(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    graph = _graph("L7.1")
    graph["deps_graph"]["nodes"].append({
        "id": "L7.2",
        "layer": "L7",
        "title": "DB Backed Feature Node",
        "kind": "service_runtime",
        "primary": ["agent/governance/reconcile_feedback.py"],
        "secondary": [],
        "test": [],
        "metadata": {"subsystem": "governance"},
    })
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-review-current-semantics-db-fallback-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    for node_id in ("L7.1", "L7.2"):
        conn.execute(
            """
            INSERT INTO graph_semantic_nodes
              (project_id, snapshot_id, node_id, status, feature_hash,
               file_hashes_json, semantic_json, updated_at)
            VALUES (?, ?, ?, 'ai_complete', ?, '{"agent/governance/server.py":"sha256:file"}',
                    ?, '2026-05-09T00:00:00Z')
            """,
            (
                PID,
                snapshot["snapshot_id"],
                node_id,
                f"hash-{node_id}",
                json.dumps({"feature_name": f"Feature {node_id}"}),
            ),
        )
    conn.commit()
    state_path = reconcile_feedback.semantic_graph_state_path(PID, snapshot["snapshot_id"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({
            "node_semantics": {
                "L7.1": {
                    "status": "ai_complete",
                    "feature_hash": "hash-L7.1",
                    "file_hashes": {"agent/governance/server.py": "sha256:file"},
                    "updated_at": "2026-05-09T00:00:00Z",
                }
            }
        }),
        encoding="utf-8",
    )
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-db-fallback",
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Review current artifact-backed feature.",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "dependency_patch_suggestions",
                "summary": "Review current DB-backed feature.",
                "type": "add_typed_relation",
            },
        ],
    )

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={
                "source_round": "round-db-fallback",
                "lane": "graph_patch_candidate",
                "group_by": "feature",
                "require_current_semantic": "true",
            },
        )
    )

    assert queue["summary"]["require_current_semantic"] is True
    assert queue["summary"]["hidden_semantic_pending_count"] == 0
    assert queue["summary"]["visible_item_count"] == 2
    assert sorted(group["source_node_ids"][0] for group in queue["groups"]) == ["L7.1", "L7.2"]
    assert all(group["semantic_review_ready"] is True for group in queue["groups"])


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


def test_git_diff_changed_line_ranges_map_to_node_functions(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    src = project / "src"
    src.mkdir()
    service = src / "service.py"
    service.write_text(
        "def keep():\n"
        "    return 'stable'\n\n"
        "def serve():\n"
        "    value = 'old'\n"
        "    return value\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=project, check=True, capture_output=True, text=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    service.write_text(
        "def keep():\n"
        "    return 'stable'\n\n"
        "def serve():\n"
        "    value = 'new'\n"
        "    return value\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "change serve"], cwd=project, check=True, capture_output=True, text=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    ranges = state_reconcile._git_changed_line_ranges(project, base, head, ["src/service.py"])
    node = {
        "id": "L7.service",
        "primary": ["src/service.py"],
        "metadata": {
            "functions": ["src.service::keep", "src.service::serve"],
            "function_lines": {"keep": [1, 2], "serve": [4, 6]},
        },
    }
    function_delta = state_reconcile._changed_functions_for_line_ranges([node], ranges)

    assert ranges == {"src/service.py": [[5, 5]]}
    assert function_delta["changed_function_ids"] == ["src.service::serve"]
    assert function_delta["changed_functions_by_node"] == {"L7.service": ["src.service::serve"]}


def test_git_diff_changed_line_ranges_map_to_node_test_functions(tmp_path):
    project = tmp_path / "test-function-delta"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    tests = project / "tests"
    tests.mkdir()
    test_service = tests / "test_service.py"
    test_service.write_text(
        "def test_keep():\n"
        "    assert 'stable'\n\n"
        "def test_serve():\n"
        "    value = 'old'\n"
        "    assert value == 'old'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=project, check=True, capture_output=True, text=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    test_service.write_text(
        "def test_keep():\n"
        "    assert 'stable'\n\n"
        "def test_serve():\n"
        "    value = 'new'\n"
        "    assert value == 'new'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "change test serve"], cwd=project, check=True, capture_output=True, text=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    ranges = state_reconcile._git_changed_line_ranges(project, base, head, ["tests/test_service.py"])
    node = {
        "id": "L7.service",
        "test": ["tests/test_service.py"],
        "metadata": {
            "test_functions": [
                "tests.test_service::test_keep",
                "tests.test_service::test_serve",
            ],
            "test_function_lines": {"test_keep": [1, 2], "test_serve": [4, 6]},
        },
    }
    test_delta = state_reconcile._changed_test_functions_for_line_ranges([node], ranges)

    assert ranges == {"tests/test_service.py": [[5, 6]]}
    assert test_delta["changed_test_function_ids"] == ["tests.test_service::test_serve"]
    assert test_delta["changed_test_functions_by_node"] == {"L7.service": ["tests.test_service::test_serve"]}


def test_semantic_projection_reports_changed_function_hashes_for_stale_node(conn):
    base_graph = _graph("L7.1")
    base_node = base_graph["deps_graph"]["nodes"][0]
    base_node["metadata"].update({
        "module": "agent.governance.server",
        "feature_hash": "sha256:feature-v1",
        "functions": [
            "agent.governance.server::keep",
            "agent.governance.server::serve",
        ],
        "function_lines": {"keep": [1, 2], "serve": [4, 6]},
        "function_hashes": {
            "agent.governance.server::keep": "sha256:keep-v1",
            "agent.governance.server::serve": "sha256:serve-v1",
        },
    })
    base_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-function-hash-base",
        commit_sha="commit-a",
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
    graph_events.create_event(
        conn,
        PID,
        base_snapshot["snapshot_id"],
        event_type="semantic_node_enriched",
        event_kind="semantic",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_OBSERVED,
        stable_node_key=graph_events.stable_node_key_for_node(base_node),
        feature_hash=graph_events.feature_hash_for_node(base_node),
        file_hashes={"agent/governance/server.py": "sha256:file-v1"},
        payload={
            "semantic_payload": {"summary": "ok", "open_issues": []},
            "function_hashes": base_node["metadata"]["function_hashes"],
        },
        created_by="test",
    )

    current_graph = json.loads(json.dumps(base_graph))
    current_node = current_graph["deps_graph"]["nodes"][0]
    current_node["metadata"]["feature_hash"] = "sha256:feature-v2"
    current_node["metadata"]["function_hashes"]["agent.governance.server::serve"] = "sha256:serve-v2"
    current_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-function-hash-current",
        commit_sha="commit-b",
        snapshot_kind="scope",
        parent_snapshot_id=base_snapshot["snapshot_id"],
        graph_json=current_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        current_snapshot["snapshot_id"],
        nodes=current_graph["deps_graph"]["nodes"],
        edges=current_graph["deps_graph"]["edges"],
    )

    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        current_snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )

    validity = projection["projection"]["node_semantics"]["L7.1"]["validity"]
    assert validity["status"] == "semantic_stale_feature_hash"
    assert validity["hash_validation"] == "function_hash_mismatch"
    assert validity["function_hash_status"] == "mismatch"
    assert validity["function_hash_match"] is False
    assert validity["changed_function_ids"] == ["agent.governance.server::serve"]
    assert projection["health"]["semantic_stale_count"] == 1


def test_semantic_projection_reports_changed_test_function_hashes_for_stale_node(conn):
    base_graph = _graph("L7.1")
    base_node = base_graph["deps_graph"]["nodes"][0]
    base_node["metadata"].update({
        "module": "agent.governance.server",
        "feature_hash": "sha256:feature-v1",
        "functions": ["agent.governance.server::serve"],
        "function_hashes": {
            "agent.governance.server::serve": "sha256:serve-v1",
        },
        "test_functions": [
            "agent.tests.test_graph_governance_api::test_serve",
        ],
        "test_function_hashes": {
            "agent.tests.test_graph_governance_api::test_serve": "sha256:test-v1",
        },
    })
    base_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-test-function-hash-base",
        commit_sha="commit-a",
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
    graph_events.create_event(
        conn,
        PID,
        base_snapshot["snapshot_id"],
        event_type="semantic_node_enriched",
        event_kind="semantic",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_OBSERVED,
        stable_node_key=graph_events.stable_node_key_for_node(base_node),
        feature_hash=graph_events.feature_hash_for_node(base_node),
        payload={
            "semantic_payload": {"summary": "ok", "open_issues": []},
            "function_hashes": base_node["metadata"]["function_hashes"],
            "test_function_hashes": base_node["metadata"]["test_function_hashes"],
        },
        created_by="test",
    )

    current_graph = json.loads(json.dumps(base_graph))
    current_node = current_graph["deps_graph"]["nodes"][0]
    current_node["metadata"]["feature_hash"] = "sha256:feature-v2"
    current_node["metadata"]["test_function_hashes"][
        "agent.tests.test_graph_governance_api::test_serve"
    ] = "sha256:test-v2"
    current_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-test-function-hash-current",
        commit_sha="commit-b",
        snapshot_kind="scope",
        parent_snapshot_id=base_snapshot["snapshot_id"],
        graph_json=current_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        current_snapshot["snapshot_id"],
        nodes=current_graph["deps_graph"]["nodes"],
        edges=current_graph["deps_graph"]["edges"],
    )

    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        current_snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )

    validity = projection["projection"]["node_semantics"]["L7.1"]["validity"]
    assert validity["status"] == "semantic_stale_feature_hash"
    assert validity["hash_validation"] == "test_function_hash_mismatch"
    assert validity["function_hash_status"] == "match"
    assert validity["test_function_hash_status"] == "mismatch"
    assert validity["test_function_hash_match"] is False
    assert validity["changed_test_function_ids"] == [
        "agent.tests.test_graph_governance_api::test_serve",
    ]
    assert projection["health"]["semantic_stale_count"] == 1


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


def test_operations_queue_reports_pending_scope_branch_identity(conn, monkeypatch, tmp_path):
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
    worktree = tmp_path / "feature-worktree"
    worktree.mkdir()
    identity = store.normalize_pending_scope_identity(
        branch_ref="codex/feature",
        worktree_path=str(worktree),
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        branch_ref=identity["branch_ref"],
        worktree_id=identity["worktree_id"],
        worktree_path=identity["worktree_path"],
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    row = next(item for item in queue["operations"] if item["operation_type"] == "scope_reconcile")
    assert row["target_id"] == "head"
    assert row["ref_name"] == "codex/feature"
    assert row["branch_ref"] == "codex/feature"
    assert row["worktree_id"] == identity["worktree_id"]
    assert row["worktree_path"] == identity["worktree_path"]
    assert row["target_label"] == "head @ codex/feature"


def test_operations_queue_surfaces_pending_scope_recovery_evidence(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "direct_update_graph"},
    )
    store.mark_pending_scope_reconcile_failed(
        conn,
        PID,
        commit_sha="head",
        actor="test",
        reason="timeout",
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    row = next(item for item in queue["operations"] if item["operation_id"] == "scope-reconcile:head")
    assert row["status"] == "failed"
    assert row["last_error"] == "timeout"
    assert row["last_result"] == "force_requeue_pending_scope"
    assert row["evidence"]["recoverable"] is True
    assert "retry_scope_reconcile" in row["supported_actions"]


def test_operations_queue_synthesizes_stale_scope_reconcile(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "head-commit")
    changed_paths = [f"docs/governance/manual-fix-{i}.md" for i in range(30)]
    monkeypatch.setattr(
        server,
        "_git_changed_paths_between",
        lambda _root, _base, _head, limit=25: changed_paths if limit is None else changed_paths[:limit],
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
    assert row["changed_files"] == changed_paths[:25]
    assert "30 changed files" in row["last_result"]
    assert queue["summary"]["graph_stale"]["is_stale"] is True
    assert queue["summary"]["graph_stale"]["changed_file_count"] == 30


def test_operations_queue_surfaces_rule_fingerprint_rebuild_action(conn, monkeypatch, tmp_path):
    fixture = create_rule_fingerprint_git_fixture_project(tmp_path)
    old_rule, current_rule = rule_fingerprint_mismatch_pair()

    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda *_args, **_kwargs: fixture.root)
    monkeypatch.setattr(server, "_current_graph_rule_fingerprint", lambda _root: current_rule)
    monkeypatch.setattr(server, "_git_changed_paths_between", lambda *_args, **_kwargs: [])

    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-rule-anchor",
        commit_sha=fixture.head_commit,
        snapshot_kind="full",
        graph_json=_graph(),
        notes=json.dumps({
            "graph_rule_fingerprint": old_rule,
            "full_reconcile_anchor": {
                "anchor_commit": fixture.head_commit,
                "snapshot_id": "full-rule-anchor",
                "structure_rule_fingerprint": old_rule["fingerprint"],
                "reconcile_mode": "full",
            },
        }),
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

    status = server.handle_graph_governance_status(_ctx({"project_id": PID}))
    graph_stale = status["current_state"]["graph_stale"]
    assert graph_stale["is_stale"] is True
    assert graph_stale["stale_reason"] == "rule_fingerprint_mismatch"
    assert graph_stale["recommended_action"] == "run_full_reconcile"
    assert graph_stale["rule_fingerprint"]["snapshot_fingerprint"] == "sha256:anchor-before-rollback"
    assert graph_stale["rule_fingerprint"]["current_fingerprint"] == "sha256:current-after-rollback"

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))
    row = next(item for item in queue["operations"] if item["operation_id"].startswith("scope-reconcile:rule-fingerprint:"))
    assert row["operation_type"] == "scope_reconcile"
    assert row["status"] == "not_queued"
    assert row["target_id"] == fixture.head_commit
    assert row["last_result"] == "graph rule fingerprint changed; run full reconcile before trusting active graph"
    assert row["supported_actions"] == ["run_full_reconcile", "view_trace", "file_backlog"]


def test_operations_queue_surfaces_suspect_snapshot_root_warning(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "head-commit")
    notes = {
        "checkout_provenance": {
            "execution_root": "/private/tmp/aming-claw-scope/repo",
            "execution_root_role": "execution_root",
            "execution_root_is_ephemeral": True,
            "canonical_project_identity": {"type": "git", "project_id": PID},
            "warnings": [
                {
                    "code": "ephemeral_execution_root",
                    "message": "graph snapshot was materialized from a temporary execution root",
                }
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-suspect-root-active",
        commit_sha="head-commit",
        snapshot_kind="scope",
        graph_json=_graph(),
        notes=json.dumps(notes, sort_keys=True),
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

    row = next(
        item for item in queue["operations"]
        if item["operation_id"] == "scope-reconcile:suspect-root:head-commit"
    )
    assert row["status"] == "not_queued"
    assert row["warnings"][0]["code"] == "ephemeral_execution_root"
    assert queue["summary"]["graph_stale"]["active_snapshot_warnings"][0]["code"] == "ephemeral_execution_root"


def test_managed_ref_api_tracks_existing_long_lived_branch_without_new_project(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )

    status, created = server.handle_graph_governance_managed_ref_upsert(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/release/1.x",
                "target_ref": "refs/heads/main",
                "merge_base_commit": "B0",
                "ref_head_commit": "R1",
                "target_head_commit": "M0",
                "status": "imported",
                "evidence": {"source": "project_import"},
                "now_iso": "2026-05-17T10:10:00Z",
            },
        )
    )

    assert status == 201
    assert created["project_id"] == PID
    assert created["ref"]["project_id"] == PID
    assert created["ref"]["ref_name"] == "refs/heads/release/1.x"
    assert created["decision"]["action"] == "materialize_ref_graph"

    listed = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"current_target_head": "M0"},
        )
    )

    assert listed["ok"] is True
    assert listed["refs"][0]["project_id"] == PID
    assert listed["refs"][0]["evidence"]["source"] == "project_import"
    assert listed["deletion_guard"]["allowed"] is False
    assert listed["deletion_guard"]["required_action"] == "archive_or_abandon_managed_refs"


def test_managed_ref_api_surfaces_stale_target_movement(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    server.handle_graph_governance_managed_ref_upsert(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/long-lived",
                "target_ref": "refs/heads/main",
                "merge_base_commit": "B0",
                "ref_head_commit": "F4",
                "target_head_commit": "M0",
                "validated_target_head": "M0",
                "snapshot_id": "scope-feature-F4",
                "projection_id": "semproj-feature-F4",
                "merge_preview_id": "preview-F4-into-M0",
                "status": "merge_candidate",
                "now_iso": "2026-05-17T10:20:00Z",
            },
        )
    )

    listed = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"current_target_head": "M1"},
        )
    )

    decision = listed["decisions"][0]
    assert decision["decision_state"] == "stale"
    assert decision["action"] == "recompute_ref_context"
    assert decision["target_moved"] is True
    assert decision["blockers"] == ["target_ref_moved"]
    assert decision["merge_ready"] is False


def test_managed_ref_api_records_merge_then_archives_ref_context(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    server.handle_graph_governance_managed_ref_upsert(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/large-refactor",
                "target_ref": "refs/heads/main",
                "merge_base_commit": "B0",
                "ref_head_commit": "F9",
                "target_head_commit": "M8",
                "validated_target_head": "M8",
                "snapshot_id": "scope-feature-F9",
                "projection_id": "semproj-feature-F9",
                "merge_preview_id": "preview-F9-into-M8",
                "status": "merge_candidate",
                "now_iso": "2026-05-17T10:30:00Z",
            },
        )
    )

    listed = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"current_target_head": "M8"},
        )
    )
    assert listed["decisions"][0]["merge_ready"] is True
    assert listed["decisions"][0]["action"] == "queue_merge_gate"

    merged = server.handle_graph_governance_managed_ref_merged(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/large-refactor",
                "merge_commit": "M9",
                "target_head_commit": "M9",
                "merge_queue_id": "mergeq-long-ref",
                "now_iso": "2026-05-17T10:31:00Z",
            },
        )
    )

    assert merged["ref"]["status"] == "merged"
    assert merged["decision"]["action"] == "archive_ref_context"
    assert merged["decision"]["archive_allowed"] is True

    archived = server.handle_graph_governance_managed_ref_archive(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/large-refactor",
                "evidence": {"reason": "merged_to_target_and_retained"},
                "now_iso": "2026-05-17T10:32:00Z",
            },
        )
    )

    assert archived["ref"]["status"] == "archived"
    assert archived["deletion_guard"]["allowed"] is True
    visible = server.handle_graph_governance_managed_refs(_ctx({"project_id": PID}))
    assert visible["refs"] == []
    retained = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"include_archived": "true"},
        )
    )
    assert retained["refs"][0]["status"] == "archived"


def test_managed_ref_bootstrap_api_dry_run_discovers_git_branches(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "release/1.x"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "release.txt").write_text("release\n", encoding="utf-8")
    subprocess.run(["git", "add", "release.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "release work"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", "codex/task-1"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)

    result = server.handle_graph_governance_managed_ref_bootstrap_dry_run(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "project_root": str(repo),
                "target_ref": "refs/heads/main",
            },
        )
    )

    assert result["ok"] is True
    assert result["discovery"]["source"] == "git_for_each_ref"
    by_ref = {candidate["ref_name"]: candidate for candidate in result["candidates"]}
    assert by_ref["refs/heads/main"]["classification"] == "target_ref"
    assert by_ref["refs/heads/codex/task-1"]["classification"] == "short_lived_agent_ref"
    release = by_ref["refs/heads/release/1.x"]
    assert release["classification"] == "managed_ref"
    assert release["action"] == "import"
    assert release["ahead_count"] == 1
    assert release["behind_count"] == 0
    listed = server.handle_graph_governance_managed_refs(_ctx({"project_id": PID}))
    assert listed["refs"] == []


def test_managed_ref_bootstrap_api_applies_supplied_refs(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )

    with pytest.raises(GovernanceError, match="route_token"):
        server.handle_graph_governance_managed_ref_bootstrap(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "target_ref": "refs/heads/main",
                    "target_head_commit": "M0",
                    "refs": [
                        {"ref_name": "refs/heads/main", "ref_head_commit": "M0"},
                    ],
                    "evidence": {"source": "operator_dry_run_accept"},
                },
            )
        )

    status, payload = server.handle_graph_governance_managed_ref_bootstrap(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "target_ref": "refs/heads/main",
                "target_head_commit": "M0",
                "refs": [
                    {"ref_name": "refs/heads/main", "ref_head_commit": "M0"},
                    {
                        "ref_name": "refs/heads/release/1.x",
                        "ref_head_commit": "R1",
                        "target_head_commit": "M0",
                        "merge_base_commit": "B0",
                    },
                    {"ref_name": "refs/heads/codex/task-1", "ref_head_commit": "C1"},
                ],
                "evidence": {"source": "operator_dry_run_accept"},
                "route_waiver": _route_waiver("managed_ref_bootstrap"),
                "now_iso": "2026-05-17T11:20:00Z",
            },
        )
    )

    assert status == 201
    assert payload["route_token_gate"]["decision"] == "route_waiver"
    assert payload["applied_count"] == 1
    assert payload["skipped_count"] == 2
    assert payload["refs"][0]["ref_name"] == "refs/heads/release/1.x"
    event = conn.execute(
        "SELECT event_type FROM task_timeline_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "route_token_gate.managed_ref_bootstrap"
    listed = server.handle_graph_governance_managed_refs(_ctx({"project_id": PID}))
    assert listed["refs"][0]["ref_name"] == "refs/heads/release/1.x"
    assert listed["refs"][0]["evidence"]["source"] == "operator_dry_run_accept"
    assert listed["decisions"][0]["action"] == "materialize_ref_graph"


# ---------------------------------------------------------------------------
# F2 security fix: server finish-gate handler must IGNORE caller-supplied
# real_startup_events and source them exclusively from the governance DB.
# (QA block #3516 finding F2-CRITICAL)
# ---------------------------------------------------------------------------

def _surrogate_finish_gate_body(
    *,
    task_id: str,
    fence_token: str,
    worktree_path: str,
    branch_ref: str,
    real_startup_events: object = None,
) -> dict:
    """Build a finish-gate request body whose startup_evidence is a surrogate."""
    surrogate_startup = {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "status": "passed",
        "ok": True,
        "allowed": True,
        "bounded": True,
        "started": True,
        "startup_complete": True,
        "actual_startup_recorded": True,
        "agent_id_match_mode": "host_adapter_startup_token_surrogate",
        "session_token_evidence_type": "surrogate",
        "worker_role": "mf_sub",
        "fence_token": fence_token,
        "actual_cwd": worktree_path,
        "actual_git_root": worktree_path,
        "worktree_path": worktree_path,
        "branch_ref": branch_ref,
        "task_id": task_id,
        "worker_slot_id": f"wslot-{task_id}",
        "runtime_context_id": f"mfrctx-{task_id}",
        "head_commit": f"head-{task_id}",
        "observer_command_id": f"cmd-{fence_token}",
        "read_receipt_event_id": f"rr-{fence_token}",
        "route_token_ref": f"rtok-{fence_token}",
    }
    body = {
        "project_id": PID,
        "task_id": task_id,
        "status": "review_ready",
        "changed_files": [],
        "test_results": {"status": "passed"},
        "checkpoint_id": f"ckpt-{task_id}",
        "fence_token": fence_token,
        "head_commit": f"head-{task_id}",
        "agent_id": "codex-subagent-api",
        "startup_evidence": surrogate_startup,
        "read_receipt_hash": f"sha256:rr-{fence_token}",
        "read_receipt_event_id": f"rr-{fence_token}",
        "observer_command_id": f"cmd-{fence_token}",
    }
    if real_startup_events is not None:
        body["real_startup_events"] = real_startup_events
    return body


def _make_real_startup_timeline_event(
    *,
    task_id: str,
    fence_token: str,
    worktree_path: str,
    branch_ref: str,
) -> dict:
    """A fabricated real worker startup timeline event with matching lineage."""
    return {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,
        # NOT a surrogate — real token present.
        "session_token_evidence_type": "hash",
        "session_token_hash": "sha256:real-token-abc",
        "session_token_present": True,
        "agent_id_match_mode": "same_as_allocation_owner",
        "agent_id": "allocated-mf-sub-worker",
        "allocation_owner": "allocated-mf-sub-worker",
        "host_adapter_startup_token_accepted": False,
        "worker_role": "mf_sub",
        "task_id": task_id,
        "worker_slot_id": f"wslot-{task_id}",
        "runtime_context_id": f"mfrctx-{task_id}",
        "fence_token": fence_token,
        "actual_cwd": worktree_path,
        "actual_git_root": worktree_path,
        "worktree_path": worktree_path,
        "branch_ref": branch_ref,
        "head_commit": f"head-{task_id}",
        "observer_command_id": f"cmd-{fence_token}",
        "read_receipt_event_id": f"rr-{fence_token}",
        "worker_session_id": f"session-{task_id}",
        "filer_principal": f"session-{task_id}",
        "worker_transcript_path": f"/tmp/transcript-{task_id}.jsonl",
        "harness_type": "codex",
        "worker_self_attesting": True,
        "self_attesting": True,
        "worker_self_attestation": {
            "schema_version": "worker_transcript_self_attestation.v1",
            "status": "passed",
            "worker_self_attesting": True,
            "worker_session_id": f"session-{task_id}",
            "worker_transcript_path": f"/tmp/transcript-{task_id}.jsonl",
            "harness_type": "codex",
            "blockers": [],
        },
    }


def _close_timeline_route_identity(suffix: str) -> dict:
    return {
        "route_id": f"route-close-{suffix}",
        "route_context_hash": f"sha256:route-close-{suffix}",
        "prompt_contract_id": f"rprompt-close-{suffix}",
        "prompt_contract_hash": f"sha256:prompt-close-{suffix}",
        "visible_injection_manifest_hash": f"sha256:visible-close-{suffix}",
        "route_token_ref": f"rtok-close-{suffix}",
    }


def _insert_close_timeline_backlog(conn, *, backlog_id: str, suffix: str) -> None:
    contract = {
        "template_id": "mf_parallel.v1",
        "project_id": PID,
        "target_files": ["agent/governance/server.py"],
        "acceptance_criteria": ["close timeline startup evidence is truthful"],
        "governance_policy": {
            "profile": "third-party-public",
            "requirements": {
                "close_timeline": True,
                "worker_graph_trace": False,
                "independent_qa": False,
            },
        },
        "route_topology_policy": {
            "selected_topology": "observer_led_parallel_lanes",
            "recommended_topology": "mf_parallel.v1",
        },
    }
    conn.execute(
        """INSERT INTO backlog_bugs
           (bug_id, title, status, mf_type, bypass_policy_json, chain_trigger_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            backlog_id,
            "Close timeline startup gate",
            "MF_IN_PROGRESS",
            "chain_rescue",
            '{"mf_type":"chain_rescue"}',
            json.dumps(contract),
            "2026-06-12T00:00:00Z",
            "2026-06-12T00:00:00Z",
        ),
    )


def _close_timeline_startup_gate(
    *,
    task_id: str,
    fence_token: str,
    worktree_path: str,
    branch_ref: str,
    route_identity: dict,
    same_owner: bool,
) -> dict:
    owner = "allocated-mf-sub-worker"
    startup = {
        **route_identity,
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "status": "passed",
        "ok": True,
        "allowed": True,
        "bounded": True,
        "started": True,
        "startup_complete": True,
        "actual_startup_recorded": True,
        "worker_role": "mf_sub",
        "task_id": task_id,
        "worker_slot_id": f"wslot-{task_id}",
        "runtime_context_id": f"mfrctx-{task_id}",
        "fence_token": fence_token,
        "actual_cwd": worktree_path,
        "actual_git_root": worktree_path,
        "worktree_path": worktree_path,
        "branch_ref": branch_ref,
        "branch": branch_ref,
        "head_commit": f"head-{task_id}",
        "observer_command_id": f"cmd-{task_id}",
        "read_receipt_event_id": f"rr-{task_id}",
        "close_satisfying": True,
        "worker_session_id": f"session-{task_id}",
        "filer_principal": f"session-{task_id}",
        "worker_transcript_path": f"/tmp/transcript-{task_id}.jsonl",
        "harness_type": "codex",
    }
    if same_owner:
        startup.update(
            {
                "agent_id": owner,
                "allocation_owner": owner,
                "agent_id_match_mode": "same_as_allocation_owner",
                "session_token_evidence_type": "server_verified",
                "session_token_hash": f"sha256:real-{task_id}",
                "session_token_present": True,
                "host_adapter_startup_token_accepted": False,
                "worker_self_attesting": True,
                "self_attesting": True,
                "worker_self_attestation": {
                    "schema_version": "worker_transcript_self_attestation.v1",
                    "status": "passed",
                    "worker_self_attesting": True,
                    "worker_session_id": f"session-{task_id}",
                    "worker_transcript_path": f"/tmp/transcript-{task_id}.jsonl",
                    "harness_type": "codex",
                    "blockers": [],
                },
            }
        )
    else:
        startup.update(
            {
                "agent_id": "codex-cli-thread:event-4178",
                "allocation_owner": owner,
                "agent_id_match_mode": "host_adapter_startup_token_surrogate",
                "session_token_evidence_type": "server_verified",
                "session_token_hash": f"sha256:host-{task_id}",
                "session_token_present": True,
                "host_adapter_startup_token_accepted": True,
                "known_bad_playback_4178": True,
                "worker_self_attesting": False,
                "self_attesting": False,
                "worker_self_attestation": {
                    "schema_version": "worker_transcript_self_attestation.v1",
                    "status": "blocked",
                    "worker_self_attesting": False,
                    "worker_session_id": f"session-{task_id}",
                    "worker_transcript_path": f"/tmp/transcript-{task_id}.jsonl",
                    "harness_type": "codex",
                    "known_bad_playback_4178": True,
                    "blockers": ["known_bad_playback_4178_shape"],
                },
            }
        )
    return startup


def _record_close_timeline(
    conn,
    *,
    backlog_id: str,
    task_id: str,
    suffix: str,
    same_owner_startup: bool,
    startup_overrides: dict | None = None,
) -> dict:
    route_identity = _close_timeline_route_identity(suffix)
    fence_token = f"fence-close-{suffix}"
    worktree_path = f"/tmp/close-{suffix}"
    branch_ref = f"refs/heads/close/{suffix}"
    common_payload = {
        **route_identity,
        "task_id": task_id,
        "runtime_context_id": f"mfrctx-{task_id}",
        "worker_slot_id": f"wslot-{task_id}",
        "fence_token": fence_token,
        "worktree_path": worktree_path,
        "branch": branch_ref,
        "head_commit": f"head-{task_id}",
    }
    task_timeline.ensure_schema(conn)
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="route.context",
        event_kind="route_context",
        phase="route_context",
        status="passed",
        payload=common_payload,
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="route.action_precheck",
        event_kind="route_action_precheck",
        phase="route_action_precheck",
        status="passed",
        payload=common_payload,
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="mf_subagent.dispatch",
        event_kind="bounded_implementation_worker_dispatch",
        phase="dispatch",
        status="passed",
        payload=common_payload,
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="mf_subagent_read_receipt",
        event_kind="mf_subagent_read_receipt",
        phase="startup_read_receipt",
        status="passed",
        payload={**common_payload, "read_receipt_hash": f"sha256:rr-{task_id}"},
    )
    startup_gate = _close_timeline_startup_gate(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
        route_identity=route_identity,
        same_owner=same_owner_startup,
    )
    if startup_overrides:
        startup_gate.update(startup_overrides)
    startup = task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="mf_subagent.startup",
        event_kind="mf_subagent_startup",
        phase="startup_gate",
        status="passed",
        payload={"mf_subagent_startup_gate": startup_gate},
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="implementation",
        event_kind="implementation",
        phase="implementation",
        status="passed",
        payload={"graph_trace_ids": [f"gqt-close-{suffix}"]},
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="independent_verification",
        event_kind="independent_verification",
        phase="verification",
        status="passed",
        actor="qa-reviewer",
        payload={**route_identity, "reviewer": "qa-reviewer"},
    )
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id=backlog_id,
        event_type="close_ready",
        event_kind="close_ready",
        phase="close_ready",
        status="passed",
        payload=route_identity,
    )
    conn.commit()
    return {"startup_event": startup, "route_identity": route_identity}


def _insert_simple_mf_close_backlog(conn, backlog_id: str) -> None:
    conn.execute(
        """INSERT INTO backlog_bugs
           (bug_id, title, status, mf_type, bypass_policy_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            backlog_id,
            "Simple MF close backlog",
            "MF_IN_PROGRESS",
            "chain_rescue",
            '{"mf_type":"chain_rescue"}',
            "2026-06-12T00:00:00Z",
            "2026-06-12T00:00:00Z",
        ),
    )
    conn.commit()


def test_timeline_append_meta_contract_rejects_observer_forbidden_action(conn):
    backlog_id = "AC-META-CONTRACT-OBSERVER-FORBIDDEN"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    with pytest.raises(GovernanceError) as exc:
        server.handle_task_timeline_append(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "backlog_id": backlog_id,
                    "event_type": "observer.blocker",
                    "event_kind": "record_blocker",
                    "phase": "implementation",
                    "actor": "observer",
                    "status": "passed",
                    "payload": {
                        "bypass_timeline_gate": True,
                        "meta_contract_gate": {
                            "allowed": True,
                            "role": "observer",
                            "action": "record_blocker",
                        },
                    },
                    "route_waiver": _route_waiver(
                        "task_timeline_append",
                        backlog_id=backlog_id,
                    ),
                },
            )
        )

    assert exc.value.code == "meta_contract_whitelist_rejected"
    events = task_timeline.list_events(conn, PID, backlog_id=backlog_id)
    assert events == []


def test_task_timeline_record_event_centrally_rejects_meta_contract_bypass(conn):
    backlog_id = "AC-META-CONTRACT-CENTRAL-REJECT"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    with pytest.raises(MfSubagentContractError, match="bypass_timeline_gate"):
        task_timeline.record_event(
            conn,
            project_id=PID,
            backlog_id=backlog_id,
            event_type="observer.blocker",
            event_kind="record_blocker",
            phase="blocker",
            actor="observer",
            status="passed",
            payload={
                "bypass_timeline_gate": True,
                "meta_contract_gate": {
                    "allowed": True,
                    "role": "observer",
                    "action": "record_blocker",
                },
            },
        )

    assert task_timeline.list_events(conn, PID, backlog_id=backlog_id) == []


def test_task_timeline_record_event_rejects_forged_meta_contract_gate(conn):
    backlog_id = "AC-META-CONTRACT-FORGED-GATE-DIRECT"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    with pytest.raises(MfSubagentContractError, match="unknown timeline action"):
        task_timeline.record_event(
            conn,
            project_id=PID,
            backlog_id=backlog_id,
            event_type="observer.invented",
            event_kind="invented_action",
            phase="invented",
            actor="observer",
            status="passed",
            payload={
                "meta_contract_gate": {
                    "schema_version": "meta_contract_timeline_gate.v1",
                    "allowed": True,
                    "role": "observer",
                    "action": "record_blocker",
                }
            },
        )

    assert task_timeline.list_events(conn, PID, backlog_id=backlog_id) == []


def test_task_timeline_record_event_meta_contract_allows_explicit_internal_system_event(conn):
    event = task_timeline.record_event(
        conn,
        project_id=PID,
        event_type="service.route.completed",
        event_kind="service_route",
        phase="service_router",
        actor="service-router",
        status="allowed",
        payload={"service_router_suppress": True},
    )

    gate = event["payload"]["meta_contract_gate"]
    assert gate["role"] == "system"
    assert gate["action"] == "service_route"
    assert gate["allowed"] is True


def test_timeline_append_rejects_forged_meta_contract_gate(conn):
    backlog_id = "AC-META-CONTRACT-FORGED-GATE-API"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    with pytest.raises(GovernanceError) as exc:
        server.handle_task_timeline_append(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "backlog_id": backlog_id,
                    "event_type": "observer.invented",
                    "event_kind": "invented_action",
                    "phase": "invented",
                    "actor": "observer",
                    "status": "passed",
                    "payload": {
                        "meta_contract_gate": {
                            "schema_version": "meta_contract_timeline_gate.v1",
                            "allowed": True,
                            "role": "observer",
                            "action": "record_blocker",
                        }
                    },
                },
            )
        )

    assert exc.value.code == "meta_contract_whitelist_rejected"
    assert "unknown timeline action" in str(exc.value)
    assert task_timeline.list_events(conn, PID, backlog_id=backlog_id) == []


def test_timeline_append_meta_contract_rejects_observer_authoring_worker_evidence(conn):
    backlog_id = "AC-META-CONTRACT-OBSERVER-WORKER-EVIDENCE"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    with pytest.raises(GovernanceError) as exc:
        server.handle_task_timeline_append(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "backlog_id": backlog_id,
                    "event_type": "implementation",
                    "event_kind": "implementation",
                    "phase": "implementation",
                    "actor": "observer",
                    "status": "passed",
                    "payload": {"changed_files": ["agent/governance/server.py"]},
                    "route_waiver": _route_waiver(
                        "task_timeline_append",
                        backlog_id=backlog_id,
                    ),
                },
            )
        )

    assert exc.value.code == "meta_contract_whitelist_rejected"
    assert "author_worker_evidence" in exc.value.message
    assert task_timeline.list_events(
        conn,
        PID,
        backlog_id=backlog_id,
        event_kind="implementation",
    ) == []


def test_timeline_append_meta_contract_allows_observer_on_behalf_worker_evidence(conn):
    backlog_id = "AC-META-CONTRACT-OBSERVER-ON-BEHALF"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    result = server.handle_task_timeline_append(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "backlog_id": backlog_id,
                "event_type": "implementation",
                "event_kind": "implementation",
                "phase": "implementation",
                "actor": "observer-on-behalf-of:mf-sub-worker-1",
                "status": "passed",
                "payload": {
                    "on_behalf_of": "mf-sub-worker-1",
                    "changed_files": ["agent/governance/server.py"],
                    "worker_self_attesting": False,
                },
                "route_waiver": _route_waiver(
                    "task_timeline_append",
                    backlog_id=backlog_id,
                ),
            },
        )
    )

    assert result["event_kind"] == "implementation"
    gate = result["meta_contract_gate"]
    assert gate["observer_event_validated"] is True
    assert gate["observer_worker_transport"] is True
    event = task_timeline.list_events(
        conn,
        PID,
        backlog_id=backlog_id,
        event_kind="implementation",
    )[0]
    assert event["payload"]["meta_contract_gate"]["observer_worker_transport"] is True


@pytest.mark.parametrize(
    "event_kind",
    [
        "bounded_implementation_worker_dispatch",
        "merge",
        "merge_preview",
        "merge_queue_entry",
        "live_merge",
        "reconcile",
        "record_blocker",
        "close_after_clauses",
    ],
)
def test_timeline_append_meta_contract_allows_legal_observer_actions(
    conn,
    event_kind,
):
    backlog_id = f"AC-META-CONTRACT-LEGAL-{event_kind.upper()}"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    result = server.handle_task_timeline_append(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "backlog_id": backlog_id,
                "event_type": event_kind.replace("_", "."),
                "event_kind": event_kind,
                "phase": event_kind,
                "actor": "observer",
                "status": "passed",
                "payload": {"reason": "legal observer coordination event"},
                "route_waiver": _route_waiver(
                    "task_timeline_append",
                    backlog_id=backlog_id,
                ),
            },
        )
    )

    assert result["meta_contract_gate"]["role"] == "observer"
    assert result["meta_contract_gate"]["observer_event_validated"] is True
    assert result["meta_contract_gate"]["allowed"] is True


@pytest.mark.parametrize("role", [None, "observer", "coordinator"])
def test_backlog_close_bypass_timeline_gate_is_rejected_for_ai_reachable_callers(
    conn,
    role,
):
    role_label = role or "anonymous"
    backlog_id = f"AC-BYPASS-TIMELINE-REJECTED-{role_label}"
    _insert_simple_mf_close_backlog(conn, backlog_id)
    body = {
        "actor": role_label,
        "bypass_timeline_gate": True,
        "timeline_bypass_reason": "This long reason used to skip the entire close gate.",
        "route_waiver": _route_waiver("backlog_close", backlog_id=backlog_id),
    }
    ctx = (
        _ctx_with_role(
            {"project_id": PID, "bug_id": backlog_id},
            role,
            method="POST",
            body=body,
        )
        if role
        else _ctx(
            {"project_id": PID, "bug_id": backlog_id},
            method="POST",
            body=body,
        )
    )

    with pytest.raises(GovernanceError) as exc:
        server.handle_backlog_close(ctx)

    assert exc.value.code == "mf_timeline_bypass_forbidden"
    row = conn.execute(
        "SELECT status FROM backlog_bugs WHERE bug_id = ?", (backlog_id,)
    ).fetchone()
    assert row["status"] == "MF_IN_PROGRESS"
    events = task_timeline.list_events(
        conn,
        PID,
        backlog_id=backlog_id,
        event_kind="mf_timeline_gate_bypass_rejected",
    )
    assert len(events) == 1
    assert events[0]["payload"]["bypass_timeline_gate"] is True


def test_hotfix_enter_records_timeline_event(conn):
    result = server.handle_project_hotfix_enter(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "actor": "operator",
                "reason": "Human approved emergency repair for close gate exposure.",
                "backlog_id": "AC-HOTFIX-ENTER",
                "task_id": "hotfix-enter-task",
            },
        )
    )

    assert result["ok"] is True
    assert result["profile"] == "HOTFIX"
    assert result["event"]["event_type"] == "hotfix.entered"
    assert result["event"]["event_kind"] == "hotfix_entered"
    assert result["event"]["payload"]["reason"].startswith("Human approved")
    assert result["event"]["payload"]["meta_contract_gate"]["action"] == (
        "hotfix_entered"
    )
    assert result["event"]["payload"]["meta_contract_gate"]["allowed"] is True


def test_hotfix_usage_view_includes_entered_and_under_hotfix_close_action(conn):
    entered = server.handle_project_hotfix_enter(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "actor": "operator",
                "reason": "Human approved emergency close path audit marker.",
                "backlog_id": "AC-HOTFIX-USAGE",
            },
        )
    )
    backlog_id = "AC-HOTFIX-USAGE"
    task_id = "hotfix-usage-task"
    suffix = "hotfix-usage"
    _insert_close_timeline_backlog(conn, backlog_id=backlog_id, suffix=suffix)
    recorded = _record_close_timeline(
        conn,
        backlog_id=backlog_id,
        task_id=task_id,
        suffix=suffix,
        same_owner_startup=True,
    )
    route_identity = recorded["route_identity"]

    close_result = server.handle_backlog_close(
        _ctx(
            {"project_id": PID, "bug_id": backlog_id},
            method="POST",
            body={
                "actor": "operator",
                "under_hotfix": True,
                "hotfix_ref": entered["hotfix_ref"],
                "hotfix_reason": "Human confirmed this close action is under the HOTFIX entry.",
                "task_id": task_id,
                "route_waiver": {
                    "accepted": True,
                    "waiver_type": "manual_fix",
                    "allowed_action": "backlog_close",
                    "project_id": PID,
                    "backlog_id": backlog_id,
                    "caller_role": "observer",
                    "reason": "Unit test supplies explicit route gate waiver evidence.",
                    "timeline_evidence": {"event_id": entered["hotfix_ref"]},
                    **route_identity,
                },
            },
        )
    )

    assert close_result["ok"] is True
    usage = server.handle_project_hotfix_usage(_ctx({"project_id": PID}))
    assert usage["ok"] is True
    entered_ids = {event["id"] for event in usage["hotfix_entered"]}
    under_hotfix_ids = {event["id"] for event in usage["under_hotfix_events"]}
    assert entered["event"]["id"] in entered_ids
    assert close_result["hotfix_audit_event"]["id"] in under_hotfix_ids
    assert any(
        event["payload"].get("under_hotfix") is True
        for event in usage["under_hotfix_events"]
    )
    assert close_result["hotfix_audit_event"]["payload"]["meta_contract_gate"][
        "action"
    ] == "hotfix_under_action"


def test_under_hotfix_close_tag_does_not_bypass_timeline_gate(conn):
    backlog_id = "AC-HOTFIX-DOES-NOT-BYPASS-CLOSE-GATE"
    _insert_simple_mf_close_backlog(conn, backlog_id)
    entered = server.handle_project_hotfix_enter(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "actor": "operator",
                "reason": "Human approved emergency close gate marker.",
                "backlog_id": backlog_id,
            },
        )
    )

    with pytest.raises(GovernanceError) as exc:
        server.handle_backlog_close(
            _ctx(
                {"project_id": PID, "bug_id": backlog_id},
                method="POST",
                body={
                    "actor": "operator",
                    "under_hotfix": True,
                    "hotfix_ref": entered["hotfix_ref"],
                    "hotfix_reason": "Human confirmed under-hotfix action reason.",
                    "route_waiver": _route_waiver(
                        "backlog_close",
                        backlog_id=backlog_id,
                    ),
                },
            )
        )

    assert exc.value.code == "mf_timeline_gate_failed"
    row = conn.execute(
        "SELECT status FROM backlog_bugs WHERE bug_id = ?", (backlog_id,)
    ).fetchone()
    assert row["status"] == "MF_IN_PROGRESS"
    events = task_timeline.list_events(
        conn,
        PID,
        backlog_id=backlog_id,
        event_kind="hotfix_backlog_close",
    )
    assert len(events) == 1
    assert events[0]["payload"]["under_hotfix"] is True
    assert events[0]["artifact_refs"]["under_hotfix"] is True
    assert events[0]["payload"]["meta_contract_gate"]["action"] == (
        "hotfix_under_action"
    )


@pytest.mark.parametrize(
    ("body_overrides", "message"),
    [
        ({"hotfix_reason": "Human reason but no ref."}, "hotfix_ref"),
        (
            {
                "hotfix_ref": "timeline:999999",
                "hotfix_reason": "Human reason with mismatched ref.",
            },
            "matching hotfix.entered",
        ),
        ({"hotfix_ref": "timeline:999999"}, "human reason"),
    ],
)
def test_under_hotfix_close_requires_entry_ref_and_reason(
    conn,
    body_overrides,
    message,
):
    backlog_id = "AC-HOTFIX-ENTRY-REF-REQUIRED"
    _insert_simple_mf_close_backlog(conn, backlog_id)

    with pytest.raises(ValidationError, match=message):
        server.handle_backlog_close(
            _ctx(
                {"project_id": PID, "bug_id": backlog_id},
                method="POST",
                body={
                    "actor": "operator",
                    "under_hotfix": True,
                    "route_waiver": _route_waiver(
                        "backlog_close",
                        backlog_id=backlog_id,
                    ),
                    **body_overrides,
                },
            )
        )

    events = task_timeline.list_events(
        conn,
        PID,
        backlog_id=backlog_id,
        event_kind="hotfix_backlog_close",
    )
    assert events == []


def test_finish_gate_flags_known_bad_4178_startup_non_self_attesting():
    task_id = "finish-event-4178-task"
    suffix = "finish-event-4178"
    fence_token = f"fence-close-{suffix}"
    worktree_path = f"/tmp/close-{suffix}"
    branch_ref = f"refs/heads/close/{suffix}"
    startup = _close_timeline_startup_gate(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
        route_identity=_close_timeline_route_identity(suffix),
        same_owner=True,
    )
    startup.update(
        {
            "agent_id": "codex-cli-thread:event-4178",
            "known_bad_playback_4178": True,
            "worker_self_attesting": False,
            "self_attesting": False,
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "status": "blocked",
                "worker_self_attesting": False,
                "worker_session_id": f"session-{task_id}",
                "worker_transcript_path": f"/tmp/transcript-{task_id}.jsonl",
                "harness_type": "codex",
                "known_bad_playback_4178": True,
                "blockers": ["known_bad_playback_4178_shape"],
            },
        }
    )
    context = BranchTaskRuntimeContext(
        project_id=PID,
        task_id=task_id,
        backlog_id="AC-WORKER-SELF-ATTESTATION-VERIFIABLE-TRANSCRIPT-20260612",
        branch_ref=branch_ref,
        status=STATE_WORKTREE_READY,
        fence_token=fence_token,
        worktree_path=worktree_path,
        base_commit="base-4178",
        head_commit=f"head-{task_id}",
        target_head_commit="target-4178",
        merge_queue_id="mq-4178",
    )
    with pytest.raises(
        MfSubagentContractError,
        match="known_bad_playback_4178_shape",
    ):
        validate_mf_subagent_finish_gate(
            {
                "task_id": task_id,
                "status": "review_ready",
                "changed_files": ["agent/governance/server.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-4178",
                "fence_token": fence_token,
                "head_commit": f"head-{task_id}",
                "real_startup_events": [
                    {
                        "event_kind": "mf_subagent_startup",
                        "event_type": "mf_subagent.startup",
                        "phase": "startup_gate",
                        "status": "passed",
                        "payload": {"mf_subagent_startup_gate": startup},
                    }
                ],
                "read_receipt_hash": f"sha256:rr-{task_id}",
                "read_receipt_event_id": f"rr-{task_id}",
                "observer_command_id": f"cmd-{task_id}",
            },
            context=context,
        )


def test_timeline_gate_blocks_event_4178_surrogate_startup_without_real_join(conn):
    backlog_id = "AC-CLOSE-TIMELINE-EVENT-4178-SURROGATE"
    task_id = "close-event-4178-task"
    _insert_close_timeline_backlog(conn, backlog_id=backlog_id, suffix="event-4178")
    recorded = _record_close_timeline(
        conn,
        backlog_id=backlog_id,
        task_id=task_id,
        suffix="event-4178",
        same_owner_startup=False,
    )

    result = server.handle_backlog_timeline_gate(
        _ctx({"project_id": PID, "bug_id": backlog_id})
    )

    assert result["can_close"] is False
    gate = result["timeline_gate"]
    startup_gate = gate["close_timeline_startup_gate"]
    assert startup_gate["demoted_startup_events"][0]["id"] == str(
        recorded["startup_event"]["id"]
    )
    assert "mf_subagent_startup" in gate["route_context_gate"]["missing_requirement_ids"]
    assert gate["route_context_gate"]["checks"]["mf_subagent_startup_present"] is False


def test_worker_transcript_timeline_gate_allows_same_owner_passed_attestation(conn):
    backlog_id = "AC-CLOSE-TIMELINE-SAME-OWNER-STARTUP"
    task_id = "close-same-owner-task"
    _insert_close_timeline_backlog(conn, backlog_id=backlog_id, suffix="same-owner")
    _record_close_timeline(
        conn,
        backlog_id=backlog_id,
        task_id=task_id,
        suffix="same-owner",
        same_owner_startup=True,
    )

    result = server.handle_backlog_timeline_gate(
        _ctx({"project_id": PID, "bug_id": backlog_id})
    )

    assert result["can_close"] is True
    gate = result["timeline_gate"]
    assert gate.get("close_timeline_startup_gate", {}).get("demoted_startup_events", []) == []
    assert gate["route_context_gate"]["checks"]["mf_subagent_startup_present"] is True


def test_worker_transcript_timeline_gate_blocks_same_owner_failed_attestation(conn):
    backlog_id = "AC-CLOSE-TIMELINE-WORKER-TRANSCRIPT-BLOCKED"
    task_id = "close-worker-transcript-blocked-task"
    _insert_close_timeline_backlog(
        conn,
        backlog_id=backlog_id,
        suffix="worker-transcript-blocked",
    )
    recorded = _record_close_timeline(
        conn,
        backlog_id=backlog_id,
        task_id=task_id,
        suffix="worker-transcript-blocked",
        same_owner_startup=True,
        startup_overrides={
            "close_satisfying": False,
            "worker_self_attesting": False,
            "self_attesting": False,
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "status": "blocked",
                "worker_self_attesting": False,
                "worker_session_id": f"session-{task_id}",
                "worker_transcript_path": f"/tmp/transcript-{task_id}.jsonl",
                "harness_type": "codex",
                "blockers": ["worker_transcript_attestation_failed"],
            },
        },
    )

    result = server.handle_backlog_timeline_gate(
        _ctx({"project_id": PID, "bug_id": backlog_id})
    )

    assert result["can_close"] is False
    gate = result["timeline_gate"]
    startup_gate = gate["close_timeline_startup_gate"]
    assert startup_gate["demoted_startup_events"][0]["id"] == str(
        recorded["startup_event"]["id"]
    )
    assert startup_gate["demoted_startup_events"][0]["reason"] == (
        "worker_self_attestation_not_close_satisfying"
    )
    assert startup_gate["demoted_startup_events"][0][
        "worker_self_attestation_blockers"
    ] == ["worker_self_attestation_not_passed"]
    assert "mf_subagent_startup" in gate["route_context_gate"]["missing_requirement_ids"]
    assert gate["route_context_gate"]["checks"]["mf_subagent_startup_present"] is False


def test_finish_gate_server_ignores_caller_supplied_real_startup_events(conn):
    """F2 bypass regression: fabricated real_startup_events in the request body
    must be IGNORED.  A surrogate-only lane (DB has no real startup events for
    the task) must be refused even when the body carries a perfectly-matching
    fabricated event.
    """
    task_id = "f2-bypass-test-01"
    fence_token = "fence-f2-bypass"
    worktree_path = "/tmp/nonexistent-f2-bypass"
    branch_ref = "refs/heads/f2/bypass-test"

    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id=task_id,
            backlog_id="AC-PARALLEL-BRANCH-STARTUP-HOST-SURROGATE-JOIN-GAP-20260605",
            branch_ref=branch_ref,
            status="worktree_ready",
            fence_token=fence_token,
            worktree_path=worktree_path,
            base_commit="base-f2",
            head_commit="base-f2",
            target_head_commit="target-f2",
            merge_queue_id="mergeq-f2-bypass",
        ),
        now_iso="2026-06-10T00:00:00Z",
    )

    # DB has NO real mf_subagent_startup events for this task.
    # Fabricate a matching real startup event and pass it in the body.
    fabricated_event = _make_real_startup_timeline_event(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
    )

    body = _surrogate_finish_gate_body(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
        real_startup_events=[fabricated_event],
    )

    # Must be refused because DB has no real startup; caller-supplied events ignored.
    with pytest.raises(MfSubagentContractError, match="actual mf_subagent_startup evidence"):
        server.handle_graph_governance_parallel_branch_finish_gate(
            _ctx({"project_id": PID}, method="POST", body=body)
        )


def test_finish_gate_server_accepts_db_sourced_real_startup_events(conn):
    """F2 positive path: when the governance DB contains a real mf_subagent_startup
    for the lane, finish-gate must succeed even if the request body carries NO
    real_startup_events key (events come from DB only).
    """
    task_id = "f2-db-source-test-01"
    fence_token = "fence-f2-db"
    worktree_path = "/tmp/nonexistent-f2-db"
    branch_ref = "refs/heads/f2/db-test"

    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id=task_id,
            backlog_id="AC-PARALLEL-BRANCH-STARTUP-HOST-SURROGATE-JOIN-GAP-20260605",
            branch_ref=branch_ref,
            status="worktree_ready",
            fence_token=fence_token,
            worktree_path=worktree_path,
            base_commit="base-f2-db",
            head_commit="base-f2-db",
            target_head_commit="target-f2-db",
            merge_queue_id="mergeq-f2-db",
        ),
        now_iso="2026-06-10T00:01:00Z",
    )

    # Insert a real mf_subagent_startup event into the DB.
    real_event_payload = _make_real_startup_timeline_event(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
    )
    task_timeline.ensure_schema(conn)
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id="AC-PARALLEL-BRANCH-STARTUP-HOST-SURROGATE-JOIN-GAP-20260605",
        event_type="mf_subagent.startup",
        event_kind="mf_subagent_startup",
        phase="startup_gate",
        status="passed",
        actor="mf_sub",
        payload={"mf_subagent_startup_gate": real_event_payload},
    )
    conn.commit()

    # Body has NO real_startup_events — server must use DB exclusively.
    body = _surrogate_finish_gate_body(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
        # no real_startup_events key
    )

    result = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx({"project_id": PID}, method="POST", body=body)
    )
    assert result["ok"] is True, f"Expected ok=True but got: {result}"
    # The gate should not flag caller-supplied events ignored (none were supplied).
    assert result["gate"].get("caller_supplied_real_startup_events_ignored") is not True


def test_finish_gate_db_graph_trace_evidence_carries_fence_token(conn):
    task_id = "fence-token-db-trace-task"
    parent_task_id = "fence-token-db-trace-parent"
    fence_token = "fence-db-graph-trace"
    worktree_path = "/tmp/nonexistent-fence-token-db-trace"
    branch_ref = "refs/heads/f2/fence-token-db-trace"
    trace_id = "gqt-fence-token-db-trace"

    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id=task_id,
            root_task_id=parent_task_id,
            backlog_id="AC-RUNTIME-CONTEXT-GRAPH-TRACE-EVIDENCE-FENCE-TOKEN-20260614",
            branch_ref=branch_ref,
            status="worktree_ready",
            fence_token=fence_token,
            worktree_path=worktree_path,
            base_commit="base-fence-token-db",
            head_commit="base-fence-token-db",
            target_head_commit="target-fence-token-db",
            merge_queue_id="mergeq-fence-token-db",
        ),
        now_iso="2026-06-14T09:30:00Z",
    )
    real_event_payload = _make_real_startup_timeline_event(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
    )
    task_timeline.ensure_schema(conn)
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id="AC-RUNTIME-CONTEXT-GRAPH-TRACE-EVIDENCE-FENCE-TOKEN-20260614",
        event_type="mf_subagent.startup",
        event_kind="mf_subagent_startup",
        phase="startup_gate",
        status="passed",
        actor="mf_sub",
        payload={"mf_subagent_startup_gate": real_event_payload},
    )
    _insert_mf_sub_graph_query_trace(
        conn,
        trace_id=trace_id,
        parent_task_id=parent_task_id,
        runtime_context_id=runtime_context_id_for_branch_context(context),
        task_id=task_id,
        worker_role="mf_sub",
        fence_token=fence_token,
        run_id=_mf_sub_run_id(task_id, fence_token),
    )
    conn.commit()

    body = _surrogate_finish_gate_body(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
    )
    body.update(
        {
            "parent_task_id": parent_task_id,
            "changed_files": ["agent/governance/server.py"],
            "graph_trace_ids": [trace_id],
        }
    )

    result = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx({"project_id": PID}, method="POST", body=body)
    )

    graph_trace = result["gate"]["graph_trace_evidence"]
    assert graph_trace["db_verified"] is True
    assert graph_trace["trace_ids"] == [trace_id]
    assert graph_trace["fence_token"] == fence_token
    assert graph_trace["task_id"] == task_id
    assert graph_trace["parent_task_id"] == parent_task_id
    assert graph_trace["worker_role"] == "mf_sub"
    assert graph_trace["missing_trace_ids"] == []
    assert graph_trace["identity_mismatches"] == []


def test_finish_gate_server_flags_caller_supplied_ignored(conn):
    """F2 transparency: when body does contain real_startup_events,
    the response should include caller_supplied_real_startup_events_ignored=True,
    even if the call ultimately succeeds because DB has a matching real startup.
    """
    task_id = "f2-flag-test-01"
    fence_token = "fence-f2-flag"
    worktree_path = "/tmp/nonexistent-f2-flag"
    branch_ref = "refs/heads/f2/flag-test"

    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id=task_id,
            backlog_id="AC-PARALLEL-BRANCH-STARTUP-HOST-SURROGATE-JOIN-GAP-20260605",
            branch_ref=branch_ref,
            status="worktree_ready",
            fence_token=fence_token,
            worktree_path=worktree_path,
            base_commit="base-f2-flag",
            head_commit="base-f2-flag",
            target_head_commit="target-f2-flag",
            merge_queue_id="mergeq-f2-flag",
        ),
        now_iso="2026-06-10T00:02:00Z",
    )

    # Insert a real startup into the DB.
    real_event_payload = _make_real_startup_timeline_event(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
    )
    task_timeline.ensure_schema(conn)
    task_timeline.record_event(
        conn,
        project_id=PID,
        task_id=task_id,
        backlog_id="AC-PARALLEL-BRANCH-STARTUP-HOST-SURROGATE-JOIN-GAP-20260605",
        event_type="mf_subagent.startup",
        event_kind="mf_subagent_startup",
        phase="startup_gate",
        status="passed",
        actor="mf_sub",
        payload={"mf_subagent_startup_gate": real_event_payload},
    )
    conn.commit()

    # Pass a (now-ignored) real_startup_events in the body.
    body = _surrogate_finish_gate_body(
        task_id=task_id,
        fence_token=fence_token,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
        real_startup_events=[{"fabricated": True}],
    )

    result = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx({"project_id": PID}, method="POST", body=body)
    )
    assert result["ok"] is True
    # Transparency flag must be present since caller did supply the key.
    assert result["gate"].get("caller_supplied_real_startup_events_ignored") is True
