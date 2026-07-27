"""Focused coverage for agent-safe ContractRuntime and timeline projections."""

import json
import os

import pytest


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), ConnectionRefusedError("refused")])
def test_optional_judgment_enrichment_is_strict_budget_cached_and_fail_open(
    monkeypatch,
    failure,
):
    from agent.governance.contracts import runtime

    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise failure

    runtime._JUDGMENT_HINT_CACHE.clear()
    monkeypatch.setattr(runtime, "_default_judgment_hints_fetcher", unavailable)

    assert runtime._fetch_judgment_hints(project_id="proj", task_id="task") is None
    assert runtime._fetch_judgment_hints(project_id="proj", task_id="task") is None
    assert len(calls) == 1
    assert calls[0]["timeout"] <= 0.2


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
        "execution_state": {"execution_state_hash": {"raw": "STATE_SECRET"}},
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
                "decision": {
                    "decision": "bypass",
                    "raw_secret": "nested-decision-secret",
                },
                "reason": "continue correct lane while diagnostic is repaired",
                "diagnostic_backlog_id": "AC-DIAGNOSTIC",
                "status": "accepted",
            },
        ],
    }
    record["runtime_guide"]["next_legal_action"]["allowed_writer_roles"] = [
        "qa",
        {"raw": "ROLE_SECRET"},
    ]
    result = build_contract_runtime_visualization(
        project_id="proj",
        backlog={
            "bug_id": "AC-COMPACT-VIEW",
            "title": {"raw": "BACKLOG_SECRET"},
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
                "message": {"raw": "ADVISORY_SECRET"},
                "replacement_authority": [
                    "ContractRuntime",
                    {"raw": "REPLACEMENT_SECRET"},
                ],
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
    assert result["authority"]["source_order"] == [
        "contract_runtime_current",
        "backlog_contract_chain_current",
        "task_timeline_compact_ledger",
        "legacy_diagnostics",
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
        "nested-decision-secret",
        "BACKLOG_SECRET",
        "STATE_SECRET",
        "ROLE_SECRET",
        "ADVISORY_SECRET",
        "REPLACEMENT_SECRET",
    ):
        assert forbidden not in encoded
    assert result["backlog"]["title"] == ""
    assert result["contract_execution_progress"]["execution_state_hash"] == ""
    assert result["contract_execution_progress"]["next_legal_action"][
        "allowed_writer_roles"
    ] == ["qa"]
    assert result["legacy_advisories"][0]["replacement_authority"] == [
        "ContractRuntime"
    ]
    assert "message" not in result["legacy_advisories"][0]
    for forbidden_key in (
        "route_token_ref",
        "session_token_ref",
        "worktree_path",
        "session_token",
    ):
        assert forbidden_key not in encoded


def _lane_visualization_fixture(lane):
    from agent.governance.contract_runtime_visualization import (
        build_contract_runtime_visualization,
    )

    lane_contract_ids = {
        "direct_main": "direct_main.v1",
        "mf_parallel": "mf_parallel.v2",
        "mf_batch_parallel": "mf_batch_parallel.v1",
    }
    root_execution_id = f"cex-{lane}-root"
    current_execution_id = (
        root_execution_id
        if lane == "direct_main"
        else f"cex-{lane}-worker-current"
    )

    def runtime_record(
        execution_id,
        *,
        parent_execution_id="",
        current=False,
    ):
        completed_lines = []
        next_legal_action = None
        if current:
            completed_lines = [
                {
                    "stage_id": "implementation",
                    "line_id": "worker_implementation",
                    "evidence_kind": "implementation",
                    "owner_role": "worker",
                    "status": "accepted",
                    "source_ref": "timeline:41",
                },
                {
                    "stage_id": "recovery",
                    "line_id": "audited_bypass_waiver",
                    "evidence_kind": "audited_bypass",
                    "owner_role": "observer",
                    "status": "PASSED",
                    "decision": "waive",
                    "classification": "system_block",
                    "diagnostic_backlog_id": "AC-LANE-DIAGNOSTIC",
                    "payload": {
                        "session_token": f"session-secret-{lane}",
                        "raw_evidence": f"line-secret-{lane}",
                    },
                    "source_ref": "timeline:42",
                },
            ]
            next_legal_action = {
                "id": "runtime-stale-action",
                "action": "runtime_stale_action",
                "stage_id": "stale",
                "line_id": "runtime_stale_line",
                "owner_role": "worker",
                "allowed_writer_roles": ["worker"],
            }
        return {
            "project_id": "proj",
            "backlog_id": "AC-VISUAL-LANE",
            "contract_execution_id": execution_id,
            "parent_contract_execution_id": parent_execution_id,
            "root_contract_execution_id": root_execution_id,
            "contract_chain_id": f"cchain-{lane}",
            "contract_id": lane_contract_ids[lane],
            "revision": "rev1",
            "definition_hash": "sha256:" + "1" * 64,
            "execution_state_revision": 4,
            "execution_state": {
                "execution_state_hash": "sha256:" + "2" * 64,
            },
            "route_token_ref": f"rtok-secret-{lane}",
            "session_token_ref": f"sesref-secret-{lane}",
            "worktree_path": f"/private/.worktrees/{lane}",
            "runtime_guide": {
                "runtime_guide_hash": "sha256:" + "3" * 64,
                "next_legal_action": next_legal_action,
            },
            "completed_lines": completed_lines,
        }

    records = []
    chain_edges = []
    if lane == "direct_main":
        records.append(runtime_record(root_execution_id, current=True))
    else:
        records.append(runtime_record(root_execution_id))
        records.append(
            runtime_record(
                current_execution_id,
                parent_execution_id=root_execution_id,
                current=True,
            )
        )
        chain_edges.append(
            {
                "parent_contract_execution_id": root_execution_id,
                "child_contract_execution_id": current_execution_id,
                "edge_kind": "worker_dispatch",
                "source_ref": "timeline:40",
            }
        )
        if lane == "mf_batch_parallel":
            sibling_execution_id = f"cex-{lane}-worker-sibling"
            records.append(
                runtime_record(
                    sibling_execution_id,
                    parent_execution_id=root_execution_id,
                )
            )
            chain_edges.append(
                {
                    "parent_contract_execution_id": root_execution_id,
                    "child_contract_execution_id": sibling_execution_id,
                    "edge_kind": "batch_candidate",
                    "source_ref": "timeline:39",
                }
            )

    chain_action = {
        "id": "chain-current-action",
        "action": "observer_merge_candidate",
        "stage_id": "merge",
        "line_id": "observer_merge_candidate",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "contract_execution_id": current_execution_id,
        "execution_state_revision": 5,
    }
    return build_contract_runtime_visualization(
        project_id="proj",
        backlog={
            "bug_id": "AC-VISUAL-LANE",
            "title": "Lane-independent visual read model",
            "status": "OPEN",
            "priority": "P0",
        },
        runtime_records=records,
        chain_current={
            "contract_chain_id": f"cchain-{lane}",
            "root_contract_execution_id": root_execution_id,
            "current_contract_execution_id": current_execution_id,
            "current_contract_id": lane_contract_ids[lane],
            "readiness_state": "contract_active",
            "generation": 5,
            "projection_watermark": 42,
            "projection_hash": "sha256:" + "4" * 64,
            "next_legal_action": chain_action,
            "source_refs": ["timeline:42"],
        },
        chain_edges=chain_edges,
        timeline_events=[
            {
                "id": 42,
                "backlog_id": "AC-VISUAL-LANE",
                "task_id": f"task-{lane}",
                "event_type": "contract_runtime.line_accepted",
                "event_kind": "contract_runtime_line_accepted",
                "phase": "recovery",
                "actor": "observer",
                "status": "accepted",
                "payload": {
                    "route_token_ref": f"rtok-event-secret-{lane}",
                    "raw_secret": f"event-secret-{lane}",
                },
                "payload_ref": {
                    "event_id": 42,
                    "payload_sha256": "sha256:" + "5" * 64,
                    "payload_bytes": 2048,
                },
            }
        ],
        legacy_compatibility_sources=[
            {
                "id": 38,
                "event_kind": "legacy_gate",
                "status": "blocked",
                "payload": {
                    "blocker": "legacy route disagrees with current authority",
                    "diagnostic_backlog_id": "AC-LANE-DIAGNOSTIC",
                    "route_token_ref": f"rtok-legacy-secret-{lane}",
                },
            }
        ],
        compact_ledger_row={
            "backlog_id": "AC-VISUAL-LANE",
            "contract_execution_id": current_execution_id,
            "readiness_state": "contract_active",
            "projection_generation": 5,
            "projection_watermark": 42,
            "projection_hash": "sha256:" + "4" * 64,
        },
        timeline_total=1,
        generated_at="2026-07-27T00:00:00Z",
    )


@pytest.mark.parametrize(
    "lane",
    ["direct_main", "mf_parallel", "mf_batch_parallel"],
)
def test_visualization_lane_fixtures_share_ai_consumable_authority_contract(lane):
    result = _lane_visualization_fixture(lane)
    encoded = json.dumps(result, sort_keys=True)

    assert result["schema_version"] == "contract_runtime.visualization.v1"
    assert result["public_safe"] is True
    assert result["read_only"] is True
    assert result["authority"] == {
        "schema_version": "contract_runtime.visualization.authority.v1",
        "source_order": [
            "contract_runtime_current",
            "backlog_contract_chain_current",
            "task_timeline_compact_ledger",
            "legacy_diagnostics",
        ],
        "source_of_authority": "contract_runtime",
        "authority_decision_source": "backlog_contract_chain_current",
        "axes": [
            "contract_execution_progress",
            "backlog_close_readiness",
            "historical_diagnostics",
        ],
        "legacy_sources_advisory_only": True,
    }

    expected_action = {
        "id": "chain-current-action",
        "action": "observer_merge_candidate",
        "stage_id": "merge",
        "line_id": "observer_merge_candidate",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "contract_execution_id": result["contract_chain"][
            "current_contract_execution_id"
        ],
        "execution_state_revision": 5,
    }
    assert result["contract_execution_progress"]["next_legal_action"] == (
        expected_action
    )
    assert result["contract_chain"]["next_legal_action"] == expected_action
    assert "runtime_stale_action" not in encoded
    assert result["backlog_close_readiness"]["state"] == "open"
    assert result["backlog_close_readiness"][
        "contract_complete_implies_backlog_close"
    ] is False

    bypass_line = next(
        line
        for line in result["contract_execution_progress"]["line_states"]
        if line["bypassed"]
    )
    assert bypass_line["status"] == "bypassed"
    assert bypass_line["bypass"]["no_pass_claim"] is True
    assert result["bypass_records"] == [
        {
            **bypass_line["bypass"],
            "contract_execution_id": bypass_line["contract_execution_id"],
            "stage_id": "recovery",
            "line_id": "audited_bypass_waiver",
            "status": "bypassed",
            "source_ref": "timeline:42",
        }
    ]
    bypass_node = next(
        node
        for node in result["dag"]["nodes"]
        if node["id"] == bypass_line["id"]
    )
    assert bypass_node["status"] == "bypassed"
    assert bypass_node["bypassed"] is True

    assert result["dag"]["schema_version"] == (
        "contract_runtime.visualization.dag.v1"
    )
    assert result["dag"]["typed_edges"] is True
    assert all(
        edge["source"] and edge["target"] and edge["relationship"]
        for edge in result["dag"]["edges"]
    )
    assert {
        "contract_execution_progress",
        "backlog_close_readiness",
        "contract_chain",
        "timeline",
        "dag",
        "bypass_records",
        "raw_compatibility",
        "repair_targets",
        "projection_freshness",
    } <= set(result)
    assert result["raw_compatibility"]["advisory_only"] is True
    assert result["raw_compatibility"]["overrides_current_authority"] is False
    assert result["projection_conflict_count"] == 0

    for secret in (
        f"rtok-secret-{lane}",
        f"sesref-secret-{lane}",
        f"/private/.worktrees/{lane}",
        f"session-secret-{lane}",
        f"line-secret-{lane}",
        f"rtok-event-secret-{lane}",
        f"event-secret-{lane}",
        f"rtok-legacy-secret-{lane}",
    ):
        assert secret not in encoded
    for private_key in (
        "route_token_ref",
        "session_token_ref",
        "worktree_path",
        "session_token",
        "raw_secret",
        "raw_evidence",
    ):
        assert private_key not in encoded


@pytest.mark.parametrize("blocked_value", [False, 0, ""])
def test_visualization_legacy_blocked_false_values_are_non_blocking(blocked_value):
    from agent.governance.contract_runtime_visualization import (
        build_contract_runtime_visualization,
    )

    result = build_contract_runtime_visualization(
        project_id="proj",
        backlog={"bug_id": "AC-COMPAT", "status": "OPEN"},
        runtime_records=[],
        chain_current={},
        chain_edges=[],
        timeline_events=[],
        legacy_compatibility_sources=[
            {
                "id": 15440,
                "status": "blocked",
                "event_kind": "legacy_gate",
                "payload": {
                    "blocked": blocked_value,
                    "diagnostic_backlog_id": "AC-MUST-NOT-BLOCK",
                },
            }
        ],
    )

    assert result["raw_compatibility"]["source_count"] == 1
    assert result["raw_compatibility"]["sources"][0]["blocked"] is False
    assert result["repair_targets"] == []


def test_visualization_projects_event_15441_repair_id_and_missing_id_fallback():
    from agent.governance.contract_runtime_visualization import (
        build_contract_runtime_visualization,
    )

    diagnostic_id = (
        "AC-SYSTEM-PARENTLESS-DIRECT-MAIN-QA-TIMELINE-"
        "ONBOARD-CONTRACT-DEFINITION-R1-20260719"
    )
    result = build_contract_runtime_visualization(
        project_id="proj",
        backlog={"bug_id": "AC-COMPAT", "status": "OPEN"},
        runtime_records=[],
        chain_current={},
        chain_edges=[],
        timeline_events=[],
        legacy_compatibility_sources=[
            {
                "id": 15441,
                "status": "blocked",
                "event_kind": "system_block",
                "payload": {
                    "blocker": "QA append failed with an unknown contract definition",
                    "diagnostic_backlog_id": diagnostic_id,
                    "route_token_ref": "rtok-must-not-leak",
                },
            },
            {
                "id": 15442,
                "status": "blocked",
                "event_kind": "legacy_gate",
                "payload": {
                    "blocker": "A public legacy gate failed without a stable id",
                    "session_token": "session-must-not-leak",
                },
            },
        ],
    )

    sources = result["raw_compatibility"]["sources"]
    event_15441 = next(item for item in sources if item["source_event_id"] == "15441")
    assert event_15441["source_authority"] == "task_timeline_events"
    assert event_15441["advisory_only"] is True
    assert event_15441["overrides_current_authority"] is False
    assert event_15441["raw_fields"]["payload.diagnostic_backlog_id"] == diagnostic_id

    targets_15441 = [
        item for item in result["repair_targets"]
        if item["source_event_id"] == "15441"
    ]
    assert {item["type"] for item in targets_15441} >= {
        "diagnostic_backlog_id",
        "source_event_id",
        "source_authority",
    }
    assert any(item["repair_id"] == diagnostic_id for item in targets_15441)

    targets_15442 = [
        item for item in result["repair_targets"]
        if item["source_event_id"] == "15442"
    ]
    assert any(item["type"] == "source_missing_repair_id" for item in targets_15442)
    encoded = json.dumps(result)
    assert "listed ids" not in encoded.lower()
    assert "route_token_ref" not in encoded
    assert "rtok-must-not-leak" not in encoded
    assert "session_token" not in encoded
    assert "session-must-not-leak" not in encoded


def test_visualization_selects_chain_current_before_runtime_record_cap():
    from agent.governance.contract_runtime_visualization import (
        build_contract_runtime_visualization,
    )

    records = []
    for index in range(51):
        record = _contract_record()
        record["contract_execution_id"] = f"cex-{index}"
        record["root_contract_execution_id"] = "cex-0"
        record["contract_chain_id"] = "cchain-cap"
        records.append(record)

    result = build_contract_runtime_visualization(
        project_id="proj",
        backlog={
            "bug_id": "AC-RUNTIME-CAP",
            "title": "Runtime cap",
            "status": "OPEN",
            "priority": "P1",
        },
        runtime_records=records,
        chain_current={
            "contract_chain_id": "cchain-cap",
            "root_contract_execution_id": "cex-0",
            "current_contract_execution_id": "cex-50",
            "current_contract_id": "mf_parallel.v2",
            "readiness_state": "contract_active",
        },
        chain_edges=[],
        timeline_events=[],
    )

    progress = result["contract_execution_progress"]
    assert progress["contract_execution_id"] == "cex-50"
    assert progress["runtime_record_count"] == 50
    assert progress["runtime_record_total"] == 51
    assert progress["runtime_records_truncated"] is True
    assert not any(
        item["kind"] == "current_execution_mismatch"
        for item in result["projection_conflicts"]
    )


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
        conn.execute(
            """
            INSERT INTO task_timeline_events (
                project_id, backlog_id, task_id, event_type, phase,
                event_kind, actor, status, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proj",
                "AC-VISUAL-ENDPOINT",
                "task-visual-endpoint",
                "system.block.qa_timeline_onboard_contract_definition",
                "qa",
                "system_block",
                "observer:codex",
                "blocked",
                json.dumps(
                    {
                        "blocker": (
                            "QA append failed with an unknown contract definition"
                        ),
                        "diagnostic_backlog_id": "AC-ENDPOINT-DIAGNOSTIC",
                        "route_token_ref": "rtok-endpoint-must-not-leak",
                    }
                ),
                "2026-07-18T00:00:03Z",
            ),
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
    assert response["timeline"]["total_count"] == 3
    assert response["timeline"]["truncated"] is True
    assert response["timeline"]["next_cursor"]
    assert response["dag"]["edge_count"] >= 2
    assert response["raw_compatibility"]["source_count"] == 1
    assert any(
        target["type"] == "diagnostic_backlog_id"
        and target["repair_id"] == "AC-ENDPOINT-DIAGNOSTIC"
        for target in response["repair_targets"]
    )
    assert "rtok-must-not-leak" not in encoded
    assert "rtok-endpoint-must-not-leak" not in encoded
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
