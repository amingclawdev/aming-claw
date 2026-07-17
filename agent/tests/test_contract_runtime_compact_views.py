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
