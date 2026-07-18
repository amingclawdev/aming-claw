"""Focused coverage for agent-safe ContractRuntime and timeline projections."""

import json
import os

import pytest


def _ctx(query=None):
    from agent.governance import server

    return server.RequestContext(
        None,
        "GET",
        {"project_id": "proj"},
        query or {},
        {},
        "req-compact-view",
        "",
        "",
    )


def _contract_record():
    return {
        "project_id": "proj",
        "backlog_id": "AC-COMPACT-VIEW",
        "contract_execution_id": "cex-compact-view",
        "contract_id": "mf_parallel.v2",
        "revision": "rev7",
        "definition_hash": "sha256:" + "1" * 64,
        "execution_state_revision": 7,
        "execution_state": {"execution_state_hash": "sha256:" + "2" * 64},
        "route_token_ref": "rtok-compact-view",
        "runtime_guide": {
            "runtime_guide_hash": "sha256:" + "3" * 64,
            "next_legal_action": {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "owner_role": "qa",
                "allowed_writer_roles": ["qa"],
                "evidence_kind": "independent_verification",
            },
            "completed_lines": [
                {"payload": {"full_completed_line": "x" * 150_000}}
            ],
        },
    }


@pytest.mark.parametrize("actor_role", ["observer", "qa"])
def test_cli_current_is_bounded_for_observer_and_qa(actor_role):
    from agent.governance import server

    record = _contract_record()
    full = server._contract_runtime_response(record, actor_role=actor_role)
    compact = server._contract_runtime_response(
        record,
        actor_role=actor_role,
        response_view="cli_current",
        request_id=f"req-{actor_role}",
    )

    assert "full_completed_line" in json.dumps(full)
    assert compact["schema_version"] == "contract_runtime.compact_cli_response.v1"
    assert compact["response_view"] == "cli_current"
    assert compact["actor_role"] == actor_role
    assert compact["source_of_authority"] == "ContractRuntime"
    assert compact["next_legal_action"]["line_id"] == "qa_independent_verification"
    assert "runtime_guide" not in compact
    assert "contract_runtime_current_state" not in compact
    assert "full_completed_line" not in json.dumps(compact)
    assert len(json.dumps(compact)) < 16_384


def test_recent_agent_and_cli_views_are_compact_while_default_is_full(
    tmp_path, monkeypatch
):
    from agent.governance import server, task_timeline
    from agent.governance.db import get_connection

    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))
    os.makedirs(
        tmp_path / "codex-tasks" / "state" / "governance" / "proj",
        exist_ok=True,
    )
    conn = get_connection("proj")
    try:
        event = task_timeline.record_event(
            conn,
            project_id="proj",
            backlog_id="AC-COMPACT-VIEW",
            task_id="compact-view-task",
            event_type="worker.finish_gate",
            phase="verification",
            event_kind="worker_finish_gate",
            actor="mf_sub",
            status="passed",
            payload={"full_timeline_evidence": "y" * 100_000},
        )
        conn.commit()
    finally:
        conn.close()

    default = server.handle_task_timeline_recent(_ctx({"limit": "10"}))
    agent = server.handle_task_timeline_recent(
        _ctx({"limit": "10", "response_view": "agent"})
    )
    cli = server.handle_task_timeline_recent(
        _ctx({"limit": "10", "response_view": "cli"})
    )

    assert default["response_view"] == "full"
    assert default["raw_event_payloads_omitted"] is False
    assert default["events"][0]["payload"]["full_timeline_evidence"] == "y" * 100_000
    for response in (agent, cli):
        assert response["response_view"] == "compact"
        assert response["raw_event_payloads_omitted"] is True
        assert response["events"][0]["id"] == event["id"]
        assert response["events"][0]["payload"]["raw_payload_omitted"] is True
        assert response["events"][0]["payload_ref"]["payload_bytes"] > 100_000
        assert "full_timeline_evidence" not in json.dumps(response["events"])
        assert len(json.dumps(response)) < 25_000


def test_visualization_builder_is_public_safe_and_keeps_authority_axes_separate():
    from agent.governance.contract_runtime_visualization import (
        build_contract_runtime_visualization,
    )

    record = {
        **_contract_record(),
        "root_contract_execution_id": "cex-compact-view",
        "contract_chain_id": "cchain-compact-view",
        "session_token_ref": "sesref-secret",
        "worktree_path": "/private/worker/worktree",
        "completed_lines": [
            {
                "stage_id": "implementation",
                "line_id": "worker_implementation",
                "evidence_kind": "implementation",
                "status": "accepted",
                "payload": {
                    "route_token_ref": "rtok-secret-nested",
                    "full_completed_line": "private-body",
                },
            },
            {
                "stage_id": "recovery",
                "line_id": "runtime_block_bypass",
                "evidence_kind": "audited_bypass",
                "classification": "system_block",
                "decision": "bypass",
                "reason": "continue correct lane while diagnostic is repaired",
                "diagnostic_backlog_id": "AC-DIAGNOSTIC",
                "status": "accepted",
            },
        ],
    }
    result = build_contract_runtime_visualization(
        project_id="proj",
        backlog={
            "bug_id": "AC-COMPACT-VIEW",
            "title": "Visualization boundary",
            "status": "OPEN",
            "priority": "P0",
        },
        runtime_records=[record],
        chain_current={
            "contract_chain_id": "cchain-compact-view",
            "root_contract_execution_id": "cex-compact-view",
            "current_contract_execution_id": "cex-compact-view",
            "current_contract_id": "mf_parallel.v2",
            "readiness_state": "contract_complete",
            "generation": 7,
            "projection_watermark": 31,
            "projection_hash": "sha256:" + "4" * 64,
            "legacy_route_action_precheck_advisory": {
                "id": "route_action_precheck",
                "legacy": True,
                "advisory_only": True,
                "required": False,
                "route_token_ref": "rtok-advisory-secret",
            },
        },
        chain_edges=[],
        timeline_events=[
            {
                "id": 9,
                "event_type": "worker.finish_gate",
                "event_kind": "worker_finish_gate",
                "status": "passed",
                "payload": {"session_token": "raw-secret"},
                "payload_ref": {
                    "event_id": 9,
                    "payload_sha256": "sha256:" + "5" * 64,
                    "payload_bytes": 1200,
                },
            }
        ],
        timeline_total=4,
        timeline_limit=1,
        timeline_has_more=True,
        next_cursor="9",
    )

    encoded = json.dumps(result)
    assert result["public_safe"] is True
    assert result["read_only"] is True
    assert result["authority"]["axes"] == [
        "contract_execution_progress",
        "backlog_close_readiness",
        "historical_diagnostics",
    ]
    assert result["backlog_close_readiness"]["state"] == "open"
    assert result["backlog_close_readiness"][
        "contract_complete_implies_backlog_close"
    ] is False
    assert result["timeline"]["current_snapshot_in_playback"] is False
    assert result["timeline"]["truncated"] is True
    assert result["timeline"]["next_cursor"] == "9"
    assert result["bypass_records"][0]["status"] == "bypassed"
    assert result["bypass_records"][0]["no_pass_claim"] is True
    assert result["dag"]["typed_edges"] is True
    for forbidden in (
        "rtok-compact-view",
        "rtok-secret-nested",
        "rtok-advisory-secret",
        "sesref-secret",
        "/private/worker/worktree",
        "raw-secret",
        "private-body",
    ):
        assert forbidden not in encoded
    for forbidden_key in (
        "route_token_ref",
        "session_token_ref",
        "worktree_path",
        "session_token",
    ):
        assert forbidden_key not in encoded


def test_visualization_endpoint_returns_cursor_bounded_runtime_projection(
    tmp_path, monkeypatch
):
    from agent.governance import server, task_timeline
    from agent.governance.contracts.runtime import SQLiteContractExecutionStore
    from agent.governance.db import get_connection

    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))
    os.makedirs(
        tmp_path / "codex-tasks" / "state" / "governance" / "proj",
        exist_ok=True,
    )
    conn = get_connection("proj")
    try:
        conn.execute(
            """
            INSERT INTO backlog_bugs (
                bug_id, title, status, priority, bypass_policy_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "AC-VISUAL-ENDPOINT",
                "Visual endpoint",
                "OPEN",
                "P0",
                "{}",
                "2026-07-18T00:00:00Z",
                "2026-07-18T00:00:00Z",
            ),
        )
        store = SQLiteContractExecutionStore(conn)
        store.create(
            {
                "project_id": "proj",
                "backlog_id": "AC-VISUAL-ENDPOINT",
                "contract_execution_id": "cex-visual-endpoint",
                "contract_id": "mf_parallel.v2",
                "version": "v2",
                "revision": "rev1",
                "definition_hash": "sha256:" + "1" * 64,
                "root_contract_execution_id": "cex-visual-endpoint",
                "contract_chain_id": "cchain-visual-endpoint",
                "execution_state_revision": 2,
                "execution_state": {"execution_state_hash": "sha256:" + "2" * 64},
                "route_token_ref": "rtok-must-not-leak",
                "runtime_guide": {
                    "runtime_guide_hash": "sha256:" + "3" * 64,
                    "next_legal_action": None,
                },
                "completed_lines": [
                    {
                        "stage_id": "qa",
                        "line_id": "qa_pass",
                        "evidence_kind": "independent_verification",
                        "owner_role": "qa",
                        "status": "passed",
                    }
                ],
            }
        )
        for index in range(2):
            task_timeline.record_event(
                conn,
                project_id="proj",
                backlog_id="AC-VISUAL-ENDPOINT",
                task_id="task-visual-endpoint",
                event_type="worker.finish_gate",
                phase="verification",
                event_kind="worker_finish_gate",
                actor="mf_sub",
                status="passed",
                payload={"raw_secret": "must-not-leak"},
            )
        conn.commit()
    finally:
        conn.close()

    ctx = server.RequestContext(
        None,
        "GET",
        {"project_id": "proj", "backlog_id": "AC-VISUAL-ENDPOINT"},
        {"limit": "1"},
        {},
        "req-visual-endpoint",
        "",
        "",
    )
    response = server.handle_project_contract_runtime_visualization(ctx)
    encoded = json.dumps(response)

    assert response["schema_version"] == "contract_runtime.visualization.v1"
    assert response["request_id"] == "req-visual-endpoint"
    assert response["contract_execution_progress"]["contract_execution_id"] == (
        "cex-visual-endpoint"
    )
    assert response["contract_execution_progress"]["readiness_state"] == (
        "contract_complete"
    )
    assert response["backlog_close_readiness"]["state"] == "open"
    assert response["timeline"]["returned_count"] == 1
    assert response["timeline"]["total_count"] == 2
    assert response["timeline"]["truncated"] is True
    assert response["timeline"]["next_cursor"]
    assert response["dag"]["edge_count"] >= 2
    assert "rtok-must-not-leak" not in encoded
    assert "must-not-leak" not in encoded
    assert "route_token_ref" not in encoded

    conn = get_connection("proj")
    try:
        conn.execute(
            """
            UPDATE backlog_bugs
            SET bypass_policy_json = ?
            WHERE bug_id = ?
            """,
            ('{"privacy_level":"private","public_safe":false}', "AC-VISUAL-ENDPOINT"),
        )
        conn.commit()
    finally:
        conn.close()
    from agent.governance.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError) as exc_info:
        server.handle_project_contract_runtime_visualization(ctx)
    assert exc_info.value.status == 403
