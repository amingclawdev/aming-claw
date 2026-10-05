from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from agent.governance import parallel_branch_runtime
from agent.governance import graph_snapshot_store
from agent.governance import server
from agent.governance.contracts import ContractDefinitionRegistry, ContractRuntime
from agent.governance.contracts.write_gate import (
    _validate_worker_receipt_hash_evidence,
)


def _native_reconcile_proof_fixture(tmp_path, monkeypatch, *, lookalike=False):
    """Real capability gate, native finalizer, canonical writer and SQLite read.

    The small active graph is fixture input; no graph builder or live service runs.
    """
    import sqlite3
    from contextlib import contextmanager
    from agent.governance import task_timeline
    from agent.governance.contracts.runtime import SQLiteContractExecutionStore

    project, backlog, execution = "native-fixture", "AC-NATIVE-FIXTURE", "cex-native-fixture"
    definition = {
        "schema_version": "contract_definition.v1", "contract_id": "native_fixture",
        "version": "v1", "revision": "rev1", "role": "observer",
        "contract_type": "native_fixture", "status": "active",
        "rule_layer": {"stages": [{"stage_id": "record", "lines": [
            {"line_id": line, "owner_role": "observer", "allowed_writer_roles": ["observer"],
             "evidence_kind": "reconcile" if n == 0 else "recorded_fixture"}
            for n, line in enumerate(("observer_reconcile", "later_report"))
        ]}]}, "instruction_layer": {"inline": [], "refs": []},
    }
    (tmp_path / "native_fixture.v1.rev1.json").write_text(json.dumps(definition))
    conn = sqlite3.connect(tmp_path / "native.sqlite")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path),
        store=SQLiteContractExecutionStore(conn), instruction_root=tmp_path)
    runtime.start_execution("native_fixture", project_id=project, backlog_id=backlog,
        contract_execution_id=execution, actor_role="observer")
    graph_snapshot_store.ensure_schema(conn)
    task_timeline.ensure_schema(conn)
    parallel_branch_runtime.upsert_branch_context(conn,
        parallel_branch_runtime.BranchTaskRuntimeContext(project_id=project,
            backlog_id=backlog, task_id=execution, runtime_context_id="mfrctx-native-fixture",
            branch_ref="refs/heads/codex/native-fixture", parent_task_id="parent-native-fixture",
            status="merged", target_head_commit="d" * 40))
    graph_snapshot_store.create_graph_snapshot(conn, project, snapshot_id="full-native-fixture",
        commit_sha="d" * 40, snapshot_kind="full", status="active",
        graph_json={"nodes": [], "edges": []})
    session = {"role": "coordinator", "principal_id": "fixture-coordinator", "session_id": "ses-fixture"}
    body = {"backlog_id": backlog, "task_id": execution}
    ctx = SimpleNamespace(token="", body=body, query={}, require_auth=lambda actual: session,
        get_project_id=lambda: project)
    auth = server._require_current_full_reconcile_auth(ctx, conn, "graph_current_full_reconcile")
    scope = server._current_full_reconcile_runtime_context_scope(conn, project_id=project,
        body=body, auth=auth, target_commit_sha="d" * 40)
    route = server._current_full_reconcile_route_evidence(auth, runtime_context_scope=scope)
    result = {"ok": True, "snapshot_id": "full-native-fixture", "current_full_reconcile": True, "activated": True,
        "active_graph_commit": "d" * 40,
        "activation_verification": {"verified": True, "active_graph_commit": "d" * 40}}
    event, provenance = server._record_current_full_atomic_evidence(conn, graph_snapshot_store,
        project_id=project, body=body, result=result, run_id="native-fixture-run",
        snapshot_id="full-native-fixture", target_commit="d" * 40, route_evidence=route,
        runtime_context_scope=scope, request_id="req-111111111111",
        request_started_at=server._utc_now(), graph_delta_mode="full",
        declared_actor_role="observer", route_bound=False)
    if lookalike:
        event = task_timeline.record_event(conn, project_id=event["project_id"],
            backlog_id=event["backlog_id"], task_id=event["task_id"], event_type=event["event_type"],
            event_kind=event["event_kind"], phase=event["phase"], status=event["status"],
            actor=event["actor"], commit_sha=event["commit_sha"], payload=copy.deepcopy(event["payload"]),
            post_commit_hooks=False)

    def write(line_id, evidence_kind, payload):
        record = runtime.current_record(execution, actor_role="observer")
        guide = runtime.current_guide(execution, actor_role="observer")
        result = runtime.submit_line_write(execution, {
            "project_id": project, "backlog_id": backlog, "contract_execution_id": execution,
            "definition_hash": record["definition_hash"], "instruction_bundle_hash": record["instruction_bundle_hash"],
            "stage_id": "record", "line_id": line_id, "actor_role": "observer", "evidence_kind": evidence_kind,
            "execution_state_revision": record["execution_state_revision"], "runtime_guide_hash": guide["runtime_guide_hash"],
            "payload": payload,
        }, actor_role="observer")
        assert result["ok"] is True, result
        conn.commit()
        return result

    write("observer_reconcile", "reconcile", {"status": "passed", "reconcile_authority": {
        **{key: scope[key] for key in ("project_id", "backlog_id", "task_id", "parent_task_id", "runtime_context_id", "merge_queue_id")},
        "contract_execution_id": execution, "reconcile_event_id": event["id"],
        "reconcile_event_created_at": event["created_at"], "reconcile_source_ref": f"timeline:{event['id']}",
    }})

    @contextmanager
    def database(requested_project):
        assert requested_project == project
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    monkeypatch.setattr(server, "DBContext", database)
    monkeypatch.setattr(server, "_contract_runtime", lambda actual: runtime if actual is conn else None)
    def request(query, role="observer"):
        return SimpleNamespace(path_params={"contract_execution_id": execution}, query=query,
            request_id="req-222222222222", get_project_id=lambda: project,
            require_auth=lambda actual: {"role": role})
    current = server.handle_project_contract_runtime_current_state(request({"response_view": "coordinator_current"}))
    selector = {"response_view": "native_event", "backlog_id": backlog,
        "contract_revision_id": current["contract_revision_id"], "contract_hash": current["contract_hash"],
        "execution_state_revision": str(current["execution_state_revision"]), "execution_state_hash": current["execution_state_hash"],
        "source_completed_line_index": "latest", "source_event_id": str(event["id"])}
    return conn, runtime, write, request, selector, event, provenance


def test_native_event_actual_writer_current_to_proof(tmp_path, monkeypatch):
    conn, runtime, _write, request, selector, event, _ = _native_reconcile_proof_fixture(tmp_path, monkeypatch)
    try:
        proof = server.handle_project_contract_runtime_current_state(request(selector))
        assert proof.get("response_view") == "native_event", proof
        assert proof["ok"] is True, proof
        original = dict(conn.execute("SELECT * FROM task_timeline_events WHERE id=?", (event["id"],)).fetchone())
        assert proof["native_event"]["source_event_hash"] == server.stable_sha256(original)
        line = runtime.store.get("cex-native-fixture")["completed_lines"][0]
        assert proof["recorded_line"]["source_line_hash"] == server.stable_sha256(line)
        assert proof["native_event"]["source_event_hash"] != proof["recorded_line"]["source_line_hash"]
        assert proof["native_event"]["source_event_hash"] != server.stable_sha256(proof["native_event"])
        assert proof["formal_position"] == "UNKNOWN"
        assert proof["formal_world"] == "UNKNOWN"
        assert proof["direction_authority"] is False
        assert proof["effect_authority"] is False
        import os
        from pathlib import Path
        export = os.environ.get("AC_NATIVE_EVENT_FIXTURE_EXPORT")
        if export:
            current = server.handle_project_contract_runtime_current_state(request({"response_view": "coordinator_current"}))
            Path(export).write_text(json.dumps({"coordinator_current": current, "selector": selector,
                "source_event": original, "source_line": line, "native_event_proof": proof}, indent=2) + "\n")
    finally:
        conn.close()


def test_native_event_later_canonical_write_refuses_stale_selector(tmp_path, monkeypatch):
    conn, _runtime, write, request, selector, _event, _ = _native_reconcile_proof_fixture(tmp_path, monkeypatch)
    try:
        write("later_report", "recorded_fixture", {"status": "blocked", "reason": "ordinary later observation"})
        proof = server.handle_project_contract_runtime_current_state(request(selector))
        assert proof["ok"] is False
        assert proof["error"] == "recorded_line_currentness_mismatch"
    finally:
        conn.close()


def test_native_event_caller_lookalike_has_no_native_seal(tmp_path, monkeypatch):
    conn, _runtime, _write, request, selector, _event, _ = _native_reconcile_proof_fixture(tmp_path, monkeypatch, lookalike=True)
    try:
        # An actual canonical stored claim cannot lend the separate native seal.
        proof = server.handle_project_contract_runtime_current_state(request(selector))
        assert proof["error"] == "native_event_native_binding_missing"
        assert proof["native_event"] is None
    finally:
        conn.close()


@pytest.mark.parametrize("changes,expected", [
    ({"backlog_id": "OTHER"}, "recorded_line_scope_mismatch"),
    ({"contract_revision_id": "rev-other"}, "native_event_scope_mismatch"),
    ({"event_family": "worker.finish"}, "native_event_family_unsupported"),
    ({"source_event_hash": "sha256:" + "0" * 64}, "native_event_digest_mismatch"),
    ({"source_event_id": "0"}, "native_event_selector_invalid"),
    ({"source_event_id": "9999"}, "native_event_line_binding_mismatch"),
])
def test_native_event_exact_selector_refusals(tmp_path, monkeypatch, changes, expected):
    conn, _runtime, _write, request, selector, _event, _ = _native_reconcile_proof_fixture(tmp_path, monkeypatch)
    try:
        proof = server.handle_project_contract_runtime_current_state(request({**selector, **changes}))
        assert proof["error"] == expected
        assert proof["native_event"] is None
    finally:
        conn.close()


def test_native_event_wrong_read_role_refuses(tmp_path, monkeypatch):
    from agent.governance import native_event_provenance
    conn, runtime, _write, request, selector, _event, _ = _native_reconcile_proof_fixture(tmp_path, monkeypatch)
    try:
        ordinary = server.handle_project_contract_runtime_current_state(request({**selector, "response_view": "recorded_line"}))
        assert native_event_provenance.read_native_event(conn, source_record=runtime.store.get("cex-native-fixture"),
            recorded=ordinary, selector=selector, actor_role="mf_sub")["error"] == "native_event_role_unsupported"
    finally:
        conn.close()


@pytest.mark.parametrize("change,expected", [
    ("dev_force", "native_event_native_binding_invalid"),
    ("operator_no_backlog", "native_event_native_binding_invalid"),
    ("context", "native_event_runtime_context_mismatch"),
    ("ambiguous", "native_event_binding_ambiguous"),
])
def test_native_event_native_seal_and_context_controls(tmp_path, monkeypatch, change, expected):
    conn, _runtime, _write, request, selector, event, provenance = _native_reconcile_proof_fixture(tmp_path, monkeypatch)
    try:
        if change == "context":
            conn.execute("UPDATE parallel_branch_runtime_contexts SET parent_task_id='other' WHERE runtime_context_id=?", ("mfrctx-native-fixture",))
        elif change == "ambiguous":
            columns = [row[1] for row in conn.execute("PRAGMA table_info(graph_current_full_reconcile_provenance)")]
            row = dict(conn.execute("SELECT * FROM graph_current_full_reconcile_provenance").fetchone())
            row["provenance_id"] = "cfrp-second-fixture"
            conn.execute("INSERT INTO graph_current_full_reconcile_provenance (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(row[key] for key in columns))
        else:
            marker = copy.deepcopy(provenance["marker"])
            if change == "dev_force":
                marker.update(normal_update_path=False, dev_force_graph_only=True)
            else:
                marker["operator_event_id"] = "gref-operator-no-backlog"
            marker["provenance_hash"] = server.stable_sha256({key: value for key, value in marker.items() if key != "provenance_hash"})
            conn.execute("UPDATE graph_current_full_reconcile_provenance SET marker_json=?,provenance_hash=?", (json.dumps(marker), marker["provenance_hash"]))
        conn.commit()
        proof = server.handle_project_contract_runtime_current_state(request(selector))
        assert proof["error"] == expected
        assert proof["native_event"] is None
    finally:
        conn.close()


@pytest.mark.parametrize("marker_event_id", [True, 1.0, "1", 2], ids=["bool", "float", "string", "wrong-int"])
def test_native_event_sealed_event_id_requires_exact_integer(tmp_path, monkeypatch, marker_event_id):
    conn, _runtime, _write, request, selector, event, provenance = _native_reconcile_proof_fixture(tmp_path, monkeypatch)
    try:
        assert event["id"] == 1
        marker = copy.deepcopy(provenance["marker"])
        marker["reconcile_event_id"] = marker_event_id
        marker["provenance_hash"] = server.stable_sha256({
            key: value for key, value in marker.items() if key != "provenance_hash"})
        conn.execute("UPDATE graph_current_full_reconcile_provenance SET marker_json=?,provenance_hash=?",
            (json.dumps(marker), marker["provenance_hash"]))
        conn.execute("UPDATE graph_snapshots SET notes=?",
            (json.dumps({"current_full_reconcile": marker}),))
        conn.commit()
        proof = server.handle_project_contract_runtime_current_state(request(selector))
        (tmp_path / "actual-marker-proof.json").write_text(json.dumps({
            "marker": marker, "original_native_event_id": event["id"], "proof": proof}, indent=2) + "\n")
        assert proof["ok"] is False, proof
        assert proof["error"] == "native_event_event_binding_mismatch"
        assert proof["native_event"] is None
    finally:
        conn.close()


def test_native_event_http_final_wire_cap():
    import io
    from agent.governance import native_event_provenance
    handler = SimpleNamespace(wfile=io.BytesIO(), CORS_HEADERS=server.GovernanceHandler.CORS_HEADERS,
        send_response=lambda _code: None, send_header=lambda *_args: None, end_headers=lambda: None)
    server.GovernanceHandler._respond(handler, 200, {**native_event_provenance.refusal("native_event_missing"),
        "handler_extension": "汉" * 6000})
    assert len(handler.wfile.getvalue()) <= 16384
    assert json.loads(handler.wfile.getvalue())["error"] == "native_event_wire_unbounded"


def test_contract_write_gate_rejects_nested_worker_receipt_placeholders():
    errors: list[str] = []

    _validate_worker_receipt_hash_evidence(
        errors,
        {
            "evidence_kind": "read_receipt",
            "payload": {
                "read_receipt_hash": "sha256:valid-top-level",
                "contract_context_read_receipt": {
                    "schema_version": "contract_context_read_receipt.v1",
                    "event_kind": "contract_context_read_receipt",
                    "receipt_hash": "<worker-computed-read-receipt-hash>",
                },
            },
        },
    )

    assert errors == [
        (
            "payload.contract_context_read_receipt.receipt_hash requires a "
            "worker-computed non-placeholder sha256: value"
        )
    ]


def test_contract_write_gate_rejects_missing_or_empty_read_receipt_hashes():
    missing_errors: list[str] = []
    _validate_worker_receipt_hash_evidence(
        missing_errors,
        {
            "evidence_kind": "read_receipt",
            "payload": {
                "schema_version": "contract_context_read_receipt.v1",
            },
        },
    )
    assert missing_errors == [
        (
            "read_receipt evidence requires at least one worker-computed "
            "non-placeholder sha256: value (missing)"
        )
    ]

    empty_errors: list[str] = []
    _validate_worker_receipt_hash_evidence(
        empty_errors,
        {
            "evidence_kind": "read_receipt",
            "read_receipt_hash": "",
            "payload": {
                "contract_context_read_receipt": {
                    "schema_version": "contract_context_read_receipt.v1",
                    "receipt_hash": "",
                },
            },
        },
    )
    assert empty_errors == [
        (
            "read_receipt_hash requires a worker-computed non-placeholder "
            "sha256: value"
        ),
        (
            "payload.contract_context_read_receipt.receipt_hash requires a "
            "worker-computed non-placeholder sha256: value"
        ),
        (
            "read_receipt evidence requires at least one worker-computed "
            "non-placeholder sha256: value (supplied but invalid)"
        ),
    ]


def test_contract_runtime_rejects_missing_or_empty_receipt_before_persistence(
    tmp_path,
):
    contract_id = "read_receipt_persistence_gate"
    definition = {
        "schema_version": "contract_definition.v1",
        "contract_id": contract_id,
        "version": "v1",
        "revision": "rev1",
        "role": "mf_sub",
        "contract_type": contract_id,
        "status": "active",
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "worker_read",
                    "lines": [
                        {
                            "line_id": "worker_read_runtime_guide",
                            "owner_role": "mf_sub",
                            "allowed_writer_roles": ["mf_sub"],
                            "evidence_kind": "read_receipt",
                        }
                    ],
                }
            ]
        },
        "instruction_layer": {"inline": [], "refs": []},
    }
    (tmp_path / f"{contract_id}.v1.rev1.json").write_text(
        json.dumps(definition),
        encoding="utf-8",
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    execution_id = "cex-read-receipt-persistence-gate"
    record = runtime.start_execution(
        contract_id,
        project_id="aming-claw",
        backlog_id="AC-READ-RECEIPT-PERSISTENCE-GATE",
        contract_execution_id=execution_id,
        actor_role="observer",
    )
    writer_guide = runtime.current_guide(execution_id, actor_role="mf_sub")
    common = {
        "project_id": record["project_id"],
        "backlog_id": record["backlog_id"],
        "contract_execution_id": execution_id,
        "definition_hash": record["definition_hash"],
        "instruction_bundle_hash": record["instruction_bundle_hash"],
        "stage_id": "worker_read",
        "line_id": "worker_read_runtime_guide",
        "actor_role": "mf_sub",
        "evidence_kind": "read_receipt",
        "execution_state_revision": record["execution_state_revision"],
        "runtime_guide_hash": writer_guide["runtime_guide_hash"],
    }

    for payload in (
        {},
        {"read_receipt_hash": ""},
        {
            "contract_context_read_receipt": {
                "schema_version": "contract_context_read_receipt.v1",
                "receipt_hash": "",
            }
        },
    ):
        rejected = runtime.submit_line_write(
            execution_id,
            {**common, "payload": payload},
            actor_role="mf_sub",
        )
        assert rejected["ok"] is False
        persisted = runtime.store.get(execution_id)
        assert persisted["execution_state_revision"] == 1
        assert persisted["completed_lines"] == []

    accepted = runtime.submit_line_write(
        execution_id,
        {
            **common,
            "payload": {"read_receipt_hash": "sha256:worker-computed"},
        },
        actor_role="mf_sub",
    )
    assert accepted["ok"] is True, json.dumps(accepted["decision"], indent=2)
    persisted = runtime.store.get(execution_id)
    assert persisted["execution_state_revision"] == 2
    assert len(persisted["completed_lines"]) == 1


def _audit_only_merge_line(*, status: str | None = None):
    candidate_commit = "a" * 40
    merged_commit = "b" * 40
    diagnostic_id = "AC-AUDIT-ONLY-DIAGNOSTIC"
    audit_authority = {
        "schema_version": (
            "contract_runtime.audit_only_no_pass_bypass_round_authority.v1"
        ),
        "server_derived": True,
        "db_verified": True,
        "qa_contract_runtime_verified": False,
        "canonical_contract_acceptance_recorded": False,
        "authoritative_pass_synthesized": False,
        "close_satisfying": False,
        "no_pass_claim": True,
        "overall_release_pass_claimed": False,
        "candidate_commit_sha": candidate_commit,
        "diagnostic_backlog_id": diagnostic_id,
        "diagnostic_status": "OPEN",
        "bypass_completed_line_index": 2,
        "qa_event_ref": "timeline:4",
    }
    durable = {
        "schema_version": (
            "contract_runtime.observer_merge_durable_authority.v1"
        ),
        "source": (
            "parallel_branch_merge_queue+task_timeline_merge+"
            "audit_only_no_pass_bypass_round"
        ),
        "server_derived": True,
        "db_verified": True,
        "project_id": "contract-runtime-api-test",
        "backlog_id": "AC-AUDIT-ONLY",
        "runtime_context_id": "mfrctx-audit-only",
        "task_id": "worker-audit-only",
        "parent_task_id": "cex-audit-only",
        "branch_head": candidate_commit,
        "merge_commit": merged_commit,
        "target_head_after_merge": merged_commit,
        "merge_gate_passed": True,
        "merge_queue_id": "mq-audit-only",
        "queue_item_id": "mqitem-audit-only",
        "queue_item_status": "merged",
        "qa_contract_runtime_verified": False,
        "qa_audit_only_no_pass_authority": audit_authority,
        "timeline_event_refs": ["timeline:5"],
        "merge_event_ref": "timeline:5",
    }
    line = {
        "stage_id": "observer_integration",
        "line_id": "observer_merge",
        "actor_role": "observer",
        "evidence_kind": "merge",
        "commit_sha": merged_commit,
        "payload": {
            "schema_version": "observer_merge.no_pass_exception.v1",
            "disposition": "audit_only_no_pass_bypass_recovery",
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
            "authoritative_pass_synthesized": False,
            "close_satisfying": False,
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "independent_qa_status": (
                "accepted_in_scope_no_pass_system_blocked"
            ),
            "system_diagnostics": [
                {"backlog_id": diagnostic_id, "status": "OPEN"}
            ],
            "system_diagnostics_open": [diagnostic_id],
            "qa_audit_only_no_pass_authority": audit_authority,
            "durable_merge_authority": durable,
        },
    }
    if status is not None:
        line["status"] = status
    return line, audit_authority


def test_audit_only_merge_missing_status_requires_trusted_recovery_mode():
    line, _audit = _audit_only_merge_line()

    assert not server._contract_runtime_server_derived_observer_merge_no_pass_line(
        line
    )
    assert server._contract_runtime_server_derived_observer_merge_no_pass_line(
        line,
        allow_missing_top_level_status=True,
    )

    for rejected_status in ("waived", "bypassed", "failed"):
        rejected = copy.deepcopy(line)
        rejected["status"] = rejected_status
        assert not (
            server._contract_runtime_server_derived_observer_merge_no_pass_line(
                rejected,
                allow_missing_top_level_status=True,
            )
        )

    forged = copy.deepcopy(line)
    forged["payload"]["durable_merge_authority"]["server_derived"] = False
    assert not server._contract_runtime_server_derived_observer_merge_no_pass_line(
        forged,
        allow_missing_top_level_status=True,
    )


def test_completed_merge_recovers_missing_status_only_after_canonical_acceptance(
    monkeypatch,
):
    project_id = "contract-runtime-api-test"
    line, audit_authority = _audit_only_merge_line()
    candidate_commit = "a" * 40
    merged_commit = "b" * 40
    qa_graph = {
        "stage_id": "qa",
        "line_id": "qa_graph_context",
        "actor_role": "qa",
        "evidence_kind": "graph_trace",
        "status": "accepted",
        "commit_sha": candidate_commit,
        "payload": {
            "graph_trace_evidence": {
                "db_verified": True,
                "verified_trace_ids": ["gqt-audit-only"],
                "candidate_commit_sha": candidate_commit,
            }
        },
    }
    bypass = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "contract_line_bypass",
        "status": "bypassed",
    }
    record = {
        "project_id": project_id,
        "backlog_id": "AC-AUDIT-ONLY",
        "contract_execution_id": "cex-audit-only",
        "contract_id": "mf_parallel.v2",
        "completed_lines": [
            {
                "stage_id": "dispatch",
                "line_id": "observer_dispatch_bounded_workers",
                "actor_role": "observer",
                "evidence_kind": "dispatch_bounded_worker",
            },
            qa_graph,
            bypass,
            line,
        ],
    }
    context = SimpleNamespace(
        runtime_context_id="mfrctx-audit-only",
        task_id="worker-audit-only",
        parent_task_id="cex-audit-only",
        backlog_id="AC-AUDIT-ONLY",
    )
    timeline_events = [
        {
            "id": 5,
            "event_kind": "live_merge",
            "phase": "merge",
            "commit_sha": merged_commit,
            "created_at": "2026-07-22T16:00:00Z",
            "payload": {},
        }
    ]
    durable_item = SimpleNamespace(
        task_id=context.task_id,
        status="merged",
        merge_commit=merged_commit,
        target_head_after_merge=merged_commit,
    )
    monkeypatch.setattr(
        parallel_branch_runtime,
        "get_merge_queue_item",
        lambda *_args, **_kwargs: durable_item,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_observer_merge_completed_round",
        lambda *_args, **_kwargs: {
            "qa_contract_runtime_verified": False,
            "qa_graph_completed_line_index": 1,
            "qa_audit_only_no_pass_authority": audit_authority,
        },
    )
    acceptance_calls = []

    def accepted(*_args, **kwargs):
        acceptance_calls.append(kwargs)
        return {"db_verified": True}

    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_line_acceptance",
        accepted,
    )

    authority = server._contract_runtime_completed_merge_authority(
        object(),
        project_id=project_id,
        record=record,
        context=context,
        timeline_events=timeline_events,
    )

    assert authority["authority_verified"] is True
    assert authority["no_pass_claim"] is True
    assert authority["overall_release_pass_claimed"] is False
    assert acceptance_calls == [
        {
            "project_id": project_id,
            "record": record,
            "completed_line_index": 3,
            "expected_line": line,
            "allow_missing_observer_merge_status": True,
        }
    ]

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: timeline_events,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        lambda *_args, merge, **_kwargs: dict(merge),
    )
    trusted_projection = server._contract_runtime_trusted_merge_projection(
        object(),
        project_id=project_id,
        record=record,
    )
    assert trusted_projection["authority_verified"] is True
    assert trusted_projection["no_pass_claim"] is True
    assert trusted_projection["overall_release_pass_claimed"] is False

    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_line_acceptance",
        lambda *_args, **_kwargs: {},
    )
    assert server._contract_runtime_completed_merge_authority(
        object(),
        project_id=project_id,
        record=record,
        context=context,
        timeline_events=timeline_events,
    ) == {}

    forged = copy.deepcopy(record)
    forged["completed_lines"][3]["payload"]["durable_merge_authority"][
        "db_verified"
    ] = False
    assert server._contract_runtime_completed_merge_authority(
        object(),
        project_id=project_id,
        record=forged,
        context=context,
        timeline_events=timeline_events,
    ) == {}


def test_trusted_merge_uses_one_coherent_worker_identity_after_close_ready(
    monkeypatch,
):
    project_id = "contract-runtime-api-test"
    contract_execution_id = "cex-f711"
    runtime_context_id = "mfrctx-f711"
    task_id = "worker-f711"
    root_task_id = "onboard-root-f711"
    context = SimpleNamespace(
        runtime_context_id=runtime_context_id,
        task_id=task_id,
        parent_task_id=contract_execution_id,
        backlog_id="AC-F711",
    )
    record = {
        "project_id": project_id,
        "backlog_id": context.backlog_id,
        "contract_execution_id": contract_execution_id,
        "runtime_guide": {
            # A contract-scoped close action is intentionally incomplete as a
            # worker identity and must not seed fields from completed lines.
            "next_legal_action": {
                "line_id": "observer_close_ready",
                "task_id": contract_execution_id,
            }
        },
        "completed_lines": [
            {
                "line_id": "observer_dispatch_bounded_workers",
                "payload": {"worker_count": 1},
            },
            {
                "line_id": "observer_merge",
                "actor_role": "observer",
                "evidence_kind": "merge",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": contract_execution_id,
                "payload": {
                    "durable_merge_authority": {
                        "schema_version": (
                            "contract_runtime."
                            "observer_merge_durable_authority.v1"
                        ),
                        "server_derived": True,
                        "db_verified": True,
                        "runtime_context_id": runtime_context_id,
                        "task_id": task_id,
                        "parent_task_id": contract_execution_id,
                    }
                },
            },
            {
                "line_id": "observer_reconcile",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "parent_task_id": contract_execution_id,
                },
            },
            {
                "line_id": "observer_close_ready",
                "actor_role": "observer",
                "evidence_kind": "close_ready",
                "runtime_context_id": runtime_context_id,
                "task_id": contract_execution_id,
                "parent_task_id": root_task_id,
                "payload": {
                    "status": "passed",
                    "runtime_context_id": runtime_context_id,
                    "worker_task_id": task_id,
                },
            },
        ],
    }

    identity = server._contract_runtime_server_line_identity(record)
    assert identity == {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": contract_execution_id,
        "identity_status": "resolved",
        "identity_source_line_id": "observer_merge",
    }

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_authority",
        lambda *_args, **_kwargs: {
            "timeline_verified": True,
            "authority_verified": True,
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "parent_task_id": contract_execution_id,
            "merged_commit_sha": "b" * 40,
        },
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        lambda *_args, merge, **_kwargs: dict(merge),
    )

    projection = server._contract_runtime_trusted_merge_projection(
        object(),
        project_id=project_id,
        record=record,
    )
    assert projection["timeline_verified"] is True
    assert projection["authority_verified"] is True
    assert projection["runtime_context_id"] == runtime_context_id
    assert projection["task_id"] == task_id

    conflicting_close_ready = copy.deepcopy(record)
    conflicting_close_ready["completed_lines"][-1]["payload"][
        "worker_task_id"
    ] = "worker-f711-conflict"
    conflicting_identity = server._contract_runtime_server_line_identity(
        conflicting_close_ready
    )
    assert conflicting_identity == {
        "runtime_context_id": "",
        "task_id": "",
        "parent_task_id": "",
        "identity_status": "ambiguous",
        "identity_source_line_id": "observer_close_ready",
    }
    conflicting_projection = server._contract_runtime_trusted_merge_projection(
        object(),
        project_id=project_id,
        record=conflicting_close_ready,
    )
    assert conflicting_projection["timeline_verified"] is False
    assert conflicting_projection["identity_mismatches"] == [
        {
            "field": "server_line_identity",
            "expected": "one coherent runtime_context_id/task_id tuple",
            "actual": "ambiguous",
            "source_line_id": "observer_close_ready",
        }
    ]


def test_server_line_identity_rejects_direct_worker_shaped_close_ready():
    runtime_context_id = "mfrctx-f711"
    task_id = "worker-f711"
    contract_execution_id = "cex-f711"
    record = {
        "contract_execution_id": contract_execution_id,
        "completed_lines": [
            {
                "line_id": "observer_merge",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": contract_execution_id,
                "payload": {
                    "durable_merge_authority": {
                        "schema_version": (
                            "contract_runtime."
                            "observer_merge_durable_authority.v1"
                        ),
                        "server_derived": True,
                        "db_verified": True,
                        "runtime_context_id": runtime_context_id,
                        "task_id": task_id,
                        "parent_task_id": contract_execution_id,
                    }
                },
            },
            {
                "line_id": "observer_close_ready",
                "actor_role": "observer",
                "evidence_kind": "close_ready",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": contract_execution_id,
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "worker_task_id": task_id,
                },
            },
        ],
    }

    assert server._contract_runtime_server_line_identity(record) == {
        "runtime_context_id": "",
        "task_id": "",
        "parent_task_id": "",
        "identity_status": "ambiguous",
        "identity_source_line_id": "observer_close_ready",
    }


def test_close_ready_submit_precheck_rejects_direct_worker_before_revision(
    monkeypatch,
):
    project_id = "contract-runtime-api-test"
    contract_execution_id = "cex-close-ready-precheck"
    runtime_context_id = "mfrctx-close-ready-precheck"
    worker_task_id = "worker-close-ready-precheck"
    root_task_id = "onboard-root-close-ready-precheck"
    initial_revision = 7
    record = {
        "project_id": project_id,
        "backlog_id": "AC-CLOSE-READY-PRECHECK",
        "contract_id": "mf_parallel.v2",
        "contract_execution_id": contract_execution_id,
        "definition_hash": "sha256:definition",
        "instruction_bundle_hash": "sha256:instructions",
        "execution_state_revision": initial_revision,
        "execution_state": {
            "execution_state_revision": initial_revision,
            "execution_state_hash": "sha256:state",
        },
        "runtime_guide": {
            "runtime_guide_hash": "sha256:guide",
            "next_legal_action": {
                "stage_id": "observer_integration",
                "line_id": "observer_close_ready",
                "evidence_kind": "close_ready",
            },
        },
        "completed_lines": [
            {
                "line_id": "observer_merge",
                "actor_role": "observer",
                "evidence_kind": "merge",
                "runtime_context_id": runtime_context_id,
                "task_id": worker_task_id,
                "parent_task_id": contract_execution_id,
                "payload": {
                    "durable_merge_authority": {
                        "schema_version": (
                            "contract_runtime."
                            "observer_merge_durable_authority.v1"
                        ),
                        "server_derived": True,
                        "db_verified": True,
                        "runtime_context_id": runtime_context_id,
                        "task_id": worker_task_id,
                        "parent_task_id": contract_execution_id,
                    }
                },
            }
        ],
    }

    class FakeConnection:
        def commit(self):
            return None

        def rollback(self):
            return None

    class FakeDBContext:
        def __init__(self, _project_id):
            self.conn = FakeConnection()

        def __enter__(self):
            return self.conn

        def __exit__(self, *_args):
            return False

    class FakeStore:
        def get(self, _execution_id):
            return record

    class FakeRuntime:
        def __init__(self):
            self.store = FakeStore()
            self.submit_calls = 0

        def current_guide(self, _execution_id, *, actor_role):
            assert actor_role == "observer"
            return record["runtime_guide"]

        def precheck_line_write(self, _execution_id, write, **_kwargs):
            return {
                "ok": True,
                "record": copy.deepcopy(record),
                "write": dict(write),
                "decision": {"ok": True, "errors": []},
                "completed_lines_count": len(record["completed_lines"]),
                "execution_state_revision": record[
                    "execution_state_revision"
                ],
                "runtime_guide_hash": record["runtime_guide"][
                    "runtime_guide_hash"
                ],
            }

        def submit_line_write(self, _execution_id, write, **_kwargs):
            self.submit_calls += 1
            record["completed_lines"].append(copy.deepcopy(write))
            record["execution_state_revision"] += 1
            record["execution_state"]["execution_state_revision"] = record[
                "execution_state_revision"
            ]
            return {
                "ok": True,
                "record": copy.deepcopy(record),
                "decision": {"ok": True, "errors": []},
            }

    runtime = FakeRuntime()
    monkeypatch.setattr(server, "DBContext", FakeDBContext)
    monkeypatch.setattr(server, "_contract_runtime", lambda _conn: runtime)
    monkeypatch.setattr(
        server,
        "_contract_runtime_effective_actor_role",
        lambda *_args, **_kwargs: "observer",
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_apply_mf_parallel_context_projection",
        lambda _conn, **kwargs: (
            copy.deepcopy(kwargs["record"]),
            {"projected_completed_lines": []},
        ),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_bind_server_line_authority",
        lambda *_args, **kwargs: dict(kwargs["write"]),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_bind_mf_parallel_dispatch_authority",
        lambda _conn, **kwargs: (dict(kwargs["write"]), []),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_bind_close_reconcile_authority",
        lambda _conn, **kwargs: dict(kwargs["record"]),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_mf_parallel_close_authority_gate",
        lambda *_args, **_kwargs: {
            "accepted": True,
            "passed": True,
            "status": "passed",
            "primary_decision_source": True,
            "missing_requirement_ids": [],
        },
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_observer_reconcile_idempotency",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        server,
        "_direct_fix_materialize_dispatch_runtime_context",
        lambda _conn, **kwargs: dict(kwargs["write"]),
    )
    monkeypatch.setattr(
        server,
        "_record_contract_runtime_mf_parallel_dispatch_event",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        server,
        "_publish_accepted_contract_runtime_line_write",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_response",
        lambda current, **_kwargs: {
            "execution_state_revision": current[
                "execution_state_revision"
            ],
        },
    )

    def request(body):
        return server.RequestContext(
            None,
            "POST",
            {
                "project_id": project_id,
                "contract_execution_id": contract_execution_id,
            },
            {},
            body,
            "req-close-ready-precheck",
            "",
            "",
        )

    retained_envelope = {
        "stage_id": "observer_integration",
        "line_id": "observer_close_ready",
        "evidence_kind": "close_ready",
        "runtime_context_id": runtime_context_id,
        "task_id": contract_execution_id,
        "parent_task_id": root_task_id,
        "payload": {
            "runtime_context_id": runtime_context_id,
            "worker_task_id": worker_task_id,
            "parent_task_id": contract_execution_id,
        },
    }
    poison_shapes = {
        "direct_worker_task": {
            **retained_envelope,
            "task_id": worker_task_id,
        },
        "missing_contract_task": {
            key: value
            for key, value in retained_envelope.items()
            if key != "task_id"
        },
        "top_level_runtime_conflict": {
            **retained_envelope,
            "runtime_context_id": "mfrctx-conflict",
        },
        "payload_runtime_conflict": {
            **retained_envelope,
            "payload": {
                **retained_envelope["payload"],
                "runtime_context_id": "mfrctx-conflict",
            },
        },
        "payload_worker_conflict": {
            **retained_envelope,
            "payload": {
                **retained_envelope["payload"],
                "worker_task_id": "worker-conflict",
            },
        },
        "payload_task_conflict": {
            **retained_envelope,
            "payload": {
                **retained_envelope["payload"],
                "task_id": "worker-conflict",
            },
        },
        "payload_parent_conflict": {
            **retained_envelope,
            "payload": {
                **retained_envelope["payload"],
                "parent_task_id": "cex-conflict",
            },
        },
    }
    expected_identity_mismatch = [
        {
            "field": "server_line_identity",
            "expected": "one coherent runtime_context_id/task_id tuple",
            "actual": "ambiguous",
            "source_line_id": "observer_close_ready",
        }
    ]
    for shape, body in poison_shapes.items():
        rejected_precheck = (
            server.handle_project_contract_runtime_line_write_precheck(
                request(body)
            )
        )
        assert rejected_precheck["ok"] is False, shape
        assert rejected_precheck["execution_state_revision"] == (
            initial_revision
        ), shape
        assert record["execution_state_revision"] == initial_revision, shape
        assert rejected_precheck["close_authority_precheck"][
            "identity_mismatches"
        ] == expected_identity_mismatch, shape

        rejected_submit = server.handle_project_contract_runtime_line_write(
            request(body)
        )
        assert rejected_submit["ok"] is False, shape
        assert record["execution_state_revision"] == initial_revision, shape
        assert runtime.submit_calls == 0, shape

    top_level_worker_conflict = {
        **server._contract_runtime_line_write_body(
            record,
            actor_role="observer",
            body=retained_envelope,
        ),
        "worker_task_id": "worker-conflict",
    }
    direct_gate = server._contract_runtime_mf_parallel_close_ready_precheck(
        record,
        top_level_worker_conflict,
        conn=FakeConnection(),
        project_id=project_id,
    )
    assert direct_gate["passed"] is False
    assert direct_gate["identity_mismatches"] == expected_identity_mismatch

    accepted_precheck = (
        server.handle_project_contract_runtime_line_write_precheck(
            request(retained_envelope)
        )
    )
    assert accepted_precheck["ok"] is True
    assert record["execution_state_revision"] == initial_revision

    accepted_submit = server.handle_project_contract_runtime_line_write(
        request(retained_envelope)
    )
    assert accepted_submit["ok"] is True
    assert record["execution_state_revision"] == initial_revision + 1
    assert runtime.submit_calls == 1
    assert server._contract_runtime_server_line_identity(record) == {
        "runtime_context_id": runtime_context_id,
        "task_id": worker_task_id,
        "parent_task_id": contract_execution_id,
        "identity_status": "resolved",
        "identity_source_line_id": "observer_merge",
    }


def test_server_line_identity_rejects_ambiguous_single_line_scope(
    monkeypatch,
):
    record = {
        "project_id": "contract-runtime-api-test",
        "backlog_id": "AC-AMBIGUOUS-IDENTITY",
        "contract_execution_id": "cex-ambiguous",
        "completed_lines": [
            {
                "line_id": "observer_dispatch_bounded_workers",
                "payload": {"worker_count": 2},
            },
            {
                "line_id": "observer_merge",
                "payload": {
                    "primary": {
                        "runtime_context_id": "mfrctx-primary",
                        "task_id": "worker-primary",
                        "parent_task_id": "cex-ambiguous",
                    },
                    "conflicting": {
                        "runtime_context_id": "mfrctx-conflict",
                        "task_id": "worker-conflict",
                        "parent_task_id": "cex-ambiguous",
                    },
                },
            },
        ],
    }

    identity = server._contract_runtime_server_line_identity(record)
    assert identity["identity_status"] == "ambiguous"
    assert identity["runtime_context_id"] == ""
    assert identity["task_id"] == ""
    assert identity["identity_source_line_id"] == "observer_merge"

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [],
    )
    projection = server._contract_runtime_trusted_merge_projection(
        object(),
        project_id=record["project_id"],
        record=record,
    )
    assert projection["timeline_verified"] is False
    assert projection["identity_mismatches"] == [
        {
            "field": "server_line_identity",
            "expected": "one coherent runtime_context_id/task_id tuple",
            "actual": "ambiguous",
            "source_line_id": "observer_merge",
        }
    ]


def test_current_full_authority_uses_current_canonical_reconcile_target(
    monkeypatch,
):
    historical_merge_commit = "a" * 40
    current_canonical_commit = "b" * 40
    observed = {}

    def current_full_state(
        _conn,
        _project_id,
        merged_commit_sha,
        **kwargs,
    ):
        observed["merged_commit_sha"] = merged_commit_sha
        observed.update(kwargs)
        return {
            "db_verified": True,
            "active_snapshot_commit": current_canonical_commit,
            "active_snapshot_status": "active",
            "active_snapshot_verified": True,
            "reconcile_snapshot_verified": True,
            "current_canonical_commit_sha": current_canonical_commit,
            "reconciled_commit_sha": current_canonical_commit,
        }

    monkeypatch.setattr(
        graph_snapshot_store,
        "current_full_reconcile_state",
        current_full_state,
    )
    monkeypatch.setattr(
        server.project_service,
        "resolve_project_root",
        lambda *_args, **_kwargs: "/canonical/project",
    )
    monkeypatch.setattr(
        server,
        "_git_head_commit",
        lambda _root: current_canonical_commit,
    )
    monkeypatch.setattr(
        server,
        "_git_commit_is_ancestor",
        lambda _root, ancestor, descendant: (
            ancestor == historical_merge_commit
            and descendant == current_canonical_commit
        ),
    )
    merge = {
        "timeline_verified": True,
        "merged_commit_sha": historical_merge_commit,
        "runtime_context_id": "mfrctx-current-canonical",
        "task_id": "worker-current-canonical",
        "parent_task_id": "cex-current-canonical",
        "merge_source_ref": "timeline:11",
        "merge_event_id": 11,
        "merge_event_created_at": "2026-07-22T10:01:00Z",
    }
    record = {
        "project_id": "contract-runtime-api-test",
        "backlog_id": "AC-CURRENT-CANONICAL",
        "contract_execution_id": "cex-current-canonical",
    }

    authority = server._contract_runtime_current_full_reconcile_authority_from_merge(
        object(),
        project_id="contract-runtime-api-test",
        record=record,
        merge=merge,
        reconcile={
            "reconcile_event_id": 12,
            "reconcile_event_created_at": "2026-07-22T10:02:00Z",
            "reconcile_task_id": "worker-current-canonical",
            "reconcile_runtime_context_id": "mfrctx-current-canonical",
        },
    )

    assert observed["merged_commit_sha"] == historical_merge_commit
    assert observed["current_canonical_commit_sha"] == current_canonical_commit
    assert authority["merged_commit_sha"] == historical_merge_commit
    assert authority["reconciled_commit_sha"] == current_canonical_commit
    assert authority["canonical_head_equals_merged_commit"] is False
    assert authority["reconciled_commit_is_ancestor_of_canonical_head"] is True
    assert authority["active_snapshot_matches_canonical_head"] is True
    assert authority["active_snapshot_verified"] is True
    assert authority["graph_reconciled"] is True


def test_current_full_authority_keeps_completed_reconcile_after_later_head(
    monkeypatch,
):
    historical_merge_commit = "a" * 40
    reconciled_commit = "b" * 40
    current_canonical_commit = "c" * 40
    ancestry_calls = []

    def current_full_state(
        _conn,
        _project_id,
        merged_commit_sha,
        **kwargs,
    ):
        assert merged_commit_sha == historical_merge_commit
        assert (
            kwargs["current_canonical_commit_sha"] == current_canonical_commit
        )
        return {
            "db_verified": True,
            "active_snapshot_commit": current_canonical_commit,
            "active_snapshot_status": "active",
            "active_snapshot_verified": True,
            "reconcile_snapshot_verified": True,
            "current_canonical_commit_sha": current_canonical_commit,
            "reconciled_commit_sha": reconciled_commit,
        }

    monkeypatch.setattr(
        graph_snapshot_store,
        "current_full_reconcile_state",
        current_full_state,
    )
    monkeypatch.setattr(
        server.project_service,
        "resolve_project_root",
        lambda *_args, **_kwargs: "/canonical/project",
    )
    monkeypatch.setattr(
        server,
        "_git_head_commit",
        lambda _root: current_canonical_commit,
    )

    def is_ancestor(_root, ancestor, descendant):
        ancestry_calls.append((ancestor, descendant))
        return (
            ancestor == reconciled_commit
            and descendant == current_canonical_commit
        )

    monkeypatch.setattr(server, "_git_commit_is_ancestor", is_ancestor)
    authority = (
        server._contract_runtime_current_full_reconcile_authority_from_merge(
            object(),
            project_id="contract-runtime-api-test",
            record={
                "backlog_id": "AC-COMPLETED-RECONCILE",
                "contract_execution_id": "cex-completed-reconcile",
            },
            merge={
                "timeline_verified": True,
                "merged_commit_sha": historical_merge_commit,
                "runtime_context_id": "mfrctx-completed-reconcile",
                "task_id": "worker-completed-reconcile",
                "parent_task_id": "cex-completed-reconcile",
                "merge_event_id": 11,
                "merge_event_created_at": "2026-07-22T10:01:00Z",
            },
            reconcile={
                "reconcile_event_id": 12,
                "reconcile_event_created_at": "2026-07-22T10:02:00Z",
                "reconcile_task_id": "worker-completed-reconcile",
                "reconcile_runtime_context_id": "mfrctx-completed-reconcile",
            },
        )
    )

    assert ancestry_calls == [(reconciled_commit, current_canonical_commit)]
    assert authority["merged_commit_sha"] == historical_merge_commit
    assert authority["reconciled_commit_sha"] == reconciled_commit
    assert authority["canonical_head_commit"] == current_canonical_commit
    assert authority["canonical_head_equals_merged_commit"] is False
    assert authority["canonical_head_equals_reconciled_commit"] is False
    assert authority["reconciled_commit_is_ancestor_of_canonical_head"] is True
    assert authority["graph_reconciled"] is True


def test_batch_postmerge_generation_authority_is_server_derived(
    monkeypatch,
    tmp_path,
) -> None:
    execution_id = "cex-batch-row-2"
    runtime_context_id = "mfrctx-batch-row-2"
    task_id = "batch-row-2"
    backlog_id = "AC-BATCH-ROW-2"
    generation_id = "bypassgen-root2"
    diagnostic_id = "AC-CONTRACT-LINE-BYPASS-TEST"
    candidate_commit = "a" * 40
    owned_files = [
        "agent/governance/dashboard_dist/",
        "frontend/dashboard/src/views/BacklogView.tsx",
    ]
    changed_files = [
        "agent/governance/dashboard_dist/index.html",
        "agent/governance/dashboard_dist/assets/index.js",
        "frontend/dashboard/src/views/BacklogView.tsx",
    ]
    root_line = {
        "stage_id": "worker_finish",
        "line_id": "worker_finish_gate",
        "line_instance_id": f"runtime_context:{runtime_context_id}",
        "evidence_kind": "contract_line_bypass",
        "status": "waived",
        "no_pass_claim": True,
        "payload": {
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "parent_task_id": execution_id,
            "worker_role": "mf_sub",
            "no_pass_generation": {"generation_id": generation_id},
        },
    }
    record = {
        "project_id": "contract-runtime-api-test",
        "backlog_id": backlog_id,
        "contract_execution_id": execution_id,
        "completed_lines": [root_line],
    }
    runtime = SimpleNamespace(store=SimpleNamespace(get=lambda _execution: record))
    monkeypatch.setattr(server, "_contract_runtime", lambda _conn: runtime)
    monkeypatch.setattr(
        server,
        "_contract_runtime_no_pass_generation",
        lambda _record: {
            "root_generation_persisted": True,
            "generation_id": generation_id,
            "root_line_id": "worker_finish_gate",
            "root_stage_id": "worker_finish",
            "root_line_instance_id": f"runtime_context:{runtime_context_id}",
            "source_backlog_id": backlog_id,
            "contract_execution_id": execution_id,
            "root_diagnostic_backlog_id": diagnostic_id,
            "root_bypass_identity": "bypass:row-2",
            "no_pass_claim": True,
            "authoritative_pass_synthesized": False,
        },
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_strict_no_pass_bypass_audit",
        lambda *_args, **_kwargs: {"db_verified": True},
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_actual_worker_commit_line",
        lambda *_args, **_kwargs: (
            {"commit_sha": candidate_commit},
            {
                "worker_commit_sha": candidate_commit,
                "changed_files": changed_files,
                "owned_files": owned_files,
            },
        ),
    )
    context = SimpleNamespace(
        owned_files=tuple(owned_files),
        target_files=(),
        worktree_path=str(tmp_path),
    )

    authority = server._audited_postmerge_recovery_batch_generation_authority(
        object(),
        project_id="contract-runtime-api-test",
        backlog_id=backlog_id,
        task_id=task_id,
        runtime_context_id=runtime_context_id,
        execution_id=execution_id,
        context=context,
    )

    assert authority["server_derived"] is True
    assert authority["candidate_commit"] == candidate_commit
    assert authority["no_pass_generation_id"] == generation_id
    assert authority["diagnostic_backlog_id"] == diagnostic_id
    assert authority["fence_containment"]["ok"] is True
    assert authority["authoritative_pass_synthesized"] is False

    root_line["payload"]["no_pass_generation"]["generation_id"] = (
        "bypassgen-forged"
    )
    assert server._audited_postmerge_recovery_batch_generation_authority(
        object(),
        project_id="contract-runtime-api-test",
        backlog_id=backlog_id,
        task_id=task_id,
        runtime_context_id=runtime_context_id,
        execution_id=execution_id,
        context=context,
    ) == {}


def _recorded_line_real_fixture(tmp_path, monkeypatch, *, line_count=1,
    project_id="recorded-fixture", backlog_id="AC-RECORDED-FIXTURE", execution="cex-recorded-fixture"):
    """Real canonical writer/SQLite reader with only disposable connection routing."""
    import sqlite3
    from contextlib import contextmanager
    from agent.governance.contracts.runtime import SQLiteContractExecutionStore

    definition = {
        "schema_version": "contract_definition.v1", "contract_id": "recorded_fixture",
        "version": "v1", "revision": "rev1", "role": "observer",
        "contract_type": "recorded_fixture", "status": "active",
        "rule_layer": {"stages": [{"stage_id": "record", "lines": [
            {"line_id": f"record_{n}", "owner_role": "observer",
             "allowed_writer_roles": ["observer"], "evidence_kind": "recorded_fixture"}
            for n in range(3)
        ]}]}, "instruction_layer": {"inline": [], "refs": []},
    }
    (tmp_path / "recorded_fixture.v1.rev1.json").write_text(json.dumps(definition))
    conn = sqlite3.connect(tmp_path / "ordinary-recorded.sqlite")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path),
        store=SQLiteContractExecutionStore(conn), instruction_root=tmp_path)
    runtime.start_execution("recorded_fixture", project_id=project_id,
        backlog_id=backlog_id, contract_execution_id=execution,
        actor_role="observer")

    def write(n, active_runtime=None):
        active_runtime = active_runtime or runtime
        record = active_runtime.current_record(execution, actor_role="observer")
        guide = active_runtime.current_guide(execution, actor_role="observer")
        result = active_runtime.submit_line_write(execution, {
            "project_id": record["project_id"], "backlog_id": record["backlog_id"],
            "contract_execution_id": execution, "definition_hash": record["definition_hash"],
            "instruction_bundle_hash": record["instruction_bundle_hash"],
            "stage_id": "record", "line_id": f"record_{n}", "actor_role": "observer",
            "evidence_kind": "recorded_fixture",
            "execution_state_revision": record["execution_state_revision"],
            "runtime_guide_hash": guide["runtime_guide_hash"],
            "payload": {"status": "blocked", "reason": "ordinary reporter claim", "sequence": n},
        }, actor_role="observer")
        assert result["ok"] is True, result
        return result

    for n in range(line_count):
        write(n)
    conn.commit()
    boundaries = []

    @contextmanager
    def context(requested_project):
        assert requested_project == project_id
        boundaries.append("enter")
        try:
            yield conn
            conn.commit()
            boundaries.append("exit")
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(server, "DBContext", context)
    monkeypatch.setattr(server, "_contract_runtime", lambda actual: runtime if actual is conn else None)
    def request(query):
        return SimpleNamespace(path_params={"contract_execution_id": execution},
            query=query, request_id="req-222222222222", get_project_id=lambda: project_id,
            require_auth=lambda actual: {"role": "observer"} if actual is conn else {})

    current = runtime.current_record(execution, actor_role="observer")
    state = server._runtime_current_state_from_record(current)
    selector = {"response_view": "recorded_line", "backlog_id": current["backlog_id"],
        "contract_hash": current["definition_hash"],
        "execution_state_revision": str(current["execution_state_revision"]),
        "execution_state_hash": state["execution_state_hash"],
        "source_completed_line_index": "latest"}
    return conn, runtime, write, request, selector, boundaries


def test_recorded_line_real_writer_current_handler_and_v1_compatibility(tmp_path, monkeypatch):
    import hashlib
    import os
    from pathlib import Path
    conn, runtime, _write, request, selector, boundaries = _recorded_line_real_fixture(tmp_path, monkeypatch)
    try:
        record = runtime.current_record("cex-recorded-fixture", actor_role="observer")
        native = server._contract_runtime_response(record, actor_role="observer",
            response_view="coordinator_current", request_id="req-222222222222")
        expected_v1 = server._contract_runtime_coordinator_current_response(record, native,
            project_id=record["project_id"], contract_execution_id=record["contract_execution_id"],
            actor_role="observer", request_id="req-222222222222")
        actual_v1 = server.handle_project_contract_runtime_current_state(request({"response_view": "coordinator_current"}))
        assert json.dumps(actual_v1, ensure_ascii=False) == json.dumps(expected_v1, ensure_ascii=False)
        original = copy.deepcopy(runtime.store.get("cex-recorded-fixture")["completed_lines"])
        actual_projector = server._contract_runtime_recorded_line_response
        def assemble(*args, **kwargs):
            assert conn.in_transaction
            boundaries.append("assemble")
            return actual_projector(*args, **kwargs)
        monkeypatch.setattr(server, "_contract_runtime_recorded_line_response", assemble)
        response = server.handle_project_contract_runtime_current_state(request(selector))
        assert boundaries[-3:] == ["enter", "assemble", "exit"]
        assert response["ok"] is True, response
        proof = response["recorded_line"]
        digest = "sha256:" + hashlib.sha256(json.dumps(original[0], sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert proof == {"source_completed_line_index": 0, "source_stage_id": "record",
            "source_line_id": "record_0", "evidence_kind": "recorded_fixture",
            "source_line_hash": digest, "provenance": "accepted_recorded_evidence",
            "currentness": "latest_completed_line"}
        assert runtime.store.get("cex-recorded-fixture")["completed_lines"] == original
        assert "ordinary reporter claim" not in json.dumps(response)
        assert not {"event_id", "occurred_at", "WorldRef", "Position", "payload", "reason", "status"}.intersection(response)
        assert len(json.dumps(response, ensure_ascii=False).encode()) < 16384
        export = os.environ.get("AC_RECORDED_FIXTURE_EXPORT")
        if export:
            destination = Path(export)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "producer-response.json").write_text(json.dumps(response, ensure_ascii=False) + "\n")
            (destination / "producer-selector.json").write_text(json.dumps(selector, ensure_ascii=False) + "\n")
            (destination / "synthetic-original-line.json").write_text(json.dumps(original[0], ensure_ascii=False) + "\n")
            (destination / "fixture-provenance.json").write_text(json.dumps({
                "kind": "synthetic disposable SQLite canonical writer/read/current-state handler roundtrip",
                "writer": "ContractRuntime.submit_line_write", "store": "SQLiteContractExecutionStore",
                "formal_authority": "unverified", "native_event_authority": "unverified",
                "production_observation": False, "original_line_digest": digest,
            }, indent=2) + "\n")
    finally:
        conn.close()


def test_recorded_line_exact_expectations_history_and_advanced_currentness(tmp_path, monkeypatch):
    conn, runtime, write, request, selector, _ = _recorded_line_real_fixture(tmp_path, monkeypatch)
    try:
        first = server.handle_project_contract_runtime_current_state(request(selector))
        exact = {**selector, **{k: str(v) for k, v in first["recorded_line"].items()
            if k in {"source_completed_line_index", "source_stage_id", "source_line_id", "evidence_kind", "source_line_hash"}}}
        assert server.handle_project_contract_runtime_current_state(request(exact))["ok"] is True
        write(1)
        conn.commit()
        advanced = server.handle_project_contract_runtime_current_state(request(exact))
        assert advanced["error"] == "recorded_line_currentness_mismatch"
        record = runtime.current_record("cex-recorded-fixture", actor_role="observer")
        fresh = {**exact, "execution_state_revision": str(record["execution_state_revision"]),
            "execution_state_hash": record["execution_state"]["execution_state_hash"]}
        historical = server.handle_project_contract_runtime_current_state(request(fresh))
        assert historical["ok"] is True
        assert historical["recorded_line"]["currentness"] == "historical_completed_line"
        assert historical["recorded_line"]["source_line_hash"] == first["recorded_line"]["source_line_hash"]
    finally:
        conn.close()


def test_recorded_line_named_absence_and_exact_selector_refusals(tmp_path, monkeypatch):
    conn, runtime, _write, request, selector, _ = _recorded_line_real_fixture(tmp_path, monkeypatch)
    try:
        original = copy.deepcopy(runtime.store.get("cex-recorded-fixture")["completed_lines"])
        cases = [
            ({"backlog_id": "wrong"}, "recorded_line_scope_mismatch"),
            ({"contract_hash": "sha256:" + "0" * 64}, "recorded_line_scope_mismatch"),
            ({"execution_state_revision": "999"}, "recorded_line_currentness_mismatch"),
            ({"execution_state_hash": "sha256:" + "0" * 64}, "recorded_line_currentness_mismatch"),
            ({"source_completed_line_index": "5"}, "recorded_line_missing"),
            ({"source_completed_line_index": "-1"}, "recorded_line_selector_invalid"),
            ({"source_completed_line_index": "00"}, "recorded_line_selector_invalid"),
            ({"source_line_id": "wrong"}, "recorded_line_mismatch"),
            ({"source_stage_id": "wrong"}, "recorded_line_mismatch"),
            ({"evidence_kind": "wrong"}, "recorded_line_mismatch"),
            ({"source_line_hash": "sha256:" + "0" * 64}, "recorded_line_mismatch"),
            ({"source_line_hash": "not-a-digest"}, "recorded_line_selector_invalid"),
            ({"source_stage_id": "x" * 257}, "recorded_line_selector_unbounded"),
            ({"backlog_id": "é" * 513}, "recorded_line_selector_unbounded"),
        ]
        for changes, expected in cases:
            result = server.handle_project_contract_runtime_current_state(request({**selector, **changes}))
            assert result["ok"] is False and result["error"] == expected, result
            assert result["recorded_line"] is None
            assert result["absence"] == (expected if expected == "recorded_line_missing" else None)
        missing = {k: v for k, v in selector.items() if k != "execution_state_hash"}
        assert server.handle_project_contract_runtime_current_state(request(missing))["error"] == "recorded_line_selector_missing"
        assert runtime.store.get("cex-recorded-fixture")["completed_lines"] == original
    finally:
        conn.close()


def test_recorded_line_empty_and_final_wire_cap(tmp_path, monkeypatch):
    conn, _runtime, _write, request, selector, _ = _recorded_line_real_fixture(tmp_path, monkeypatch, line_count=0)
    try:
        absent = server.handle_project_contract_runtime_current_state(request(selector))
        assert absent["error"] == absent["absence"] == "recorded_line_missing"
        small = server._contract_runtime_recorded_line_refusal("recorded_line_missing")
        assert server._contract_runtime_recorded_line_wire_response(small) == small
        oversized = {**small, "fixture_extension": "汉" * 6000}
        assert server._contract_runtime_recorded_line_wire_response(oversized)["error"] == "recorded_line_wire_unbounded"
    finally:
        conn.close()


def test_recorded_line_cold_start_returns_persisted_proof(tmp_path, monkeypatch):
    conn, _runtime, _write, request, selector, _ = _recorded_line_real_fixture(tmp_path, monkeypatch)
    try:
        # Caller knows only bounded current CEX identity and the explicit latest locator.
        assert "source_line_hash" not in selector
        response = server.handle_project_contract_runtime_current_state(request(selector))
        assert response["response_view"] == "recorded_line", response
        assert response["ok"] is True
        assert response["recorded_line"]["source_completed_line_index"] == 0
        assert response["recorded_line"]["provenance"] == "accepted_recorded_evidence"
    finally:
        conn.close()


def test_recorded_line_snapshot_survives_concurrent_later_writer(tmp_path, monkeypatch):
    import sqlite3
    from agent.governance.contracts.runtime import SQLiteContractExecutionStore
    conn, runtime, write, request, selector, _ = _recorded_line_real_fixture(tmp_path, monkeypatch)
    other = None
    try:
        # Warm inherited schema in this disposable fixture, then use actual WAL readers/writers.
        server.handle_project_contract_runtime_current_state(request({"response_view": "coordinator_current"}))
        conn.execute("PRAGMA journal_mode=WAL")
        other = sqlite3.connect(tmp_path / "ordinary-recorded.sqlite")
        other.row_factory = sqlite3.Row
        other_runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path),
            store=SQLiteContractExecutionStore(other), instruction_root=tmp_path)
        original = server._contract_runtime_recorded_line_response
        def assemble(*args, **kwargs):
            assert conn.in_transaction
            write(1, other_runtime)
            other.commit()
            # The underlying SQLite read snapshot still sees the original prefix.
            assert len(runtime.store.get("cex-recorded-fixture")["completed_lines"]) == 1
            return original(*args, **kwargs)
        monkeypatch.setattr(server, "_contract_runtime_recorded_line_response", assemble)
        response = server.handle_project_contract_runtime_current_state(request(selector))
        assert response["ok"] is True
        assert response["execution_state_revision"] == 2
        assert response["recorded_line"]["source_line_id"] == "record_0"
        assert response["recorded_line"]["currentness"] == "latest_completed_line"
        assert len(runtime.store.get("cex-recorded-fixture")["completed_lines"]) == 2
        monkeypatch.setattr(server, "_contract_runtime_recorded_line_response", original)
        assert server.handle_project_contract_runtime_current_state(request(selector))["error"] == "recorded_line_currentness_mismatch"
    finally:
        if other:
            other.close()
        conn.close()


def test_recorded_line_preserves_source_action_and_project_scope_checks(tmp_path, monkeypatch):
    conn, runtime, _write, _request, selector, _ = _recorded_line_real_fixture(tmp_path, monkeypatch)
    try:
        record = runtime.current_record("cex-recorded-fixture", actor_role="observer")
        source = runtime.store.get("cex-recorded-fixture")
        native = server._contract_runtime_response(record, actor_role="observer")
        changed = copy.deepcopy(native)
        changed["next_legal_action"]["contract_execution_id"] = "cex-other-epoch"
        for selected, project, expected in [
            (changed, "recorded-fixture", "coordinator_current_action_source_mismatch"),
            (native, "other-project", "coordinator_current_scope_mismatch"),
        ]:
            result = server._contract_runtime_recorded_line_response(source, record, selected,
                selector=selector, project_id=project, contract_execution_id="cex-recorded-fixture",
                actor_role="observer", request_id="req-222222222222")
            assert result["error"] == expected
            assert result["recorded_line"] is None
    finally:
        conn.close()


def test_recorded_line_http_final_wire_cap_after_handler_additions():
    import io
    headers = []
    handler = SimpleNamespace(wfile=io.BytesIO(), CORS_HEADERS=server.GovernanceHandler.CORS_HEADERS, send_response=lambda _code: None,
        send_header=lambda key, value: headers.append((key, value)), end_headers=lambda: None)
    body = {**server._contract_runtime_recorded_line_refusal("recorded_line_missing"),
        "handler_extension": "汉" * 6000}
    server.GovernanceHandler._respond(handler, 200, body)
    payload = handler.wfile.getvalue()
    assert len(payload) <= 16384
    assert json.loads(payload)["error"] == "recorded_line_wire_unbounded"


def test_recorded_line_registered_consumer_companion_fixture(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    conn, runtime, _write, request, selector, _ = _recorded_line_real_fixture(tmp_path, monkeypatch,
        project_id="judgment-brain", backlog_id="JB-DURABLE-RECEIPT", execution="cex-receipt-12345678")
    try:
        response = server.handle_project_contract_runtime_current_state(request(selector))
        assert response["ok"] is True, response
        assert response["project_id"] == "judgment-brain"
        assert response["backlog_id"] == "JB-DURABLE-RECEIPT"
        assert response["contract_execution_id"] == "cex-receipt-12345678"
        assert response["request_id"] == "req-222222222222"
        line = runtime.store.get("cex-receipt-12345678")["completed_lines"][0]
        assert response["recorded_line"]["source_line_hash"] == server.stable_sha256(line)
        export = os.environ.get("AC_RECORDED_FIXTURE_EXPORT")
        if export:
            destination = Path(export).parent / "registered-consumer-fixture"
            destination.mkdir(parents=True, exist_ok=True)
            for filename, value in [
                ("producer-response.json", response), ("producer-selector.json", selector),
                ("synthetic-original-line.json", line),
                ("fixture-provenance.json", {"kind": "ordinary local SQLite canonical writer/read/handler",
                    "writer": "ContractRuntime.submit_line_write", "store": "SQLiteContractExecutionStore",
                    "consumer_mapping": "judgment-brain/JB-DURABLE-RECEIPT/cex-receipt-12345678",
                    "production_observation": False, "formal_authority": "unverified",
                    "native_event_authority": "unverified"}),
            ]:
                (destination / filename).write_text(json.dumps(value, ensure_ascii=False) + "\n")
    finally:
        conn.close()


def _r5_owned_lane_ledger(base_commit):
    """Saved-shaped eight-test sibling comparison with no private identity."""
    owned_ids = [f"tests/test_owned.py::test_owned_{index}" for index in range(5)]
    sibling_ids = [f"tests/test_sibling.py::test_sibling_{index}" for index in range(3)]
    def raw(argv, exit_code, stdout):
        return {"argv": argv, "command": " ".join(argv), "cwd": "/fixture/worker",
                "exit_code": exit_code, "stdout": stdout, "stderr": ""}
    full_argv = ["python3", "-m", "pytest", "-q"]
    focused = raw(full_argv + ["tests/test_owned.py"], 0, "5 passed")
    focused.update(status="passed", phase="candidate", failed_test_ids=[])
    hashes = {"pkg/owned.py": "1" * 64, "pkg/sibling.py": "2" * 64,
              "tests/test_owned.py": "3" * 64, "tests/test_sibling.py": "4" * 64}
    candidate_hashes = {**hashes, "pkg/owned.py": "5" * 64}
    return {"status": "passed", "passed": True,
        "scope": "owned lane only; no full-suite or release PASS", "commands": [focused],
        "baseline_comparison": {
            "baseline_commit": base_commit, "comparison_scope": "original eight tests",
            "baseline": {"passed": 0, "failed": 8, "failure_ids": owned_ids + sibling_ids,
                         "raw_command": raw(full_argv, 1, "8 failed")},
            "candidate": {"passed": 5, "failed": 3, "failure_ids": sibling_ids,
                          "raw_command": raw(full_argv, 1, "3 failed, 5 passed")},
            "candidate_new_failure_ids": [], "resolved_baseline_failure_ids": owned_ids,
            "known_baseline_failure_ids": sibling_ids,
            "failure_classification": "Unchanged sibling stubs; original owned five pass.",
            "unchanged_files": ["pkg/sibling.py", "tests/test_owned.py", "tests/test_sibling.py"],
            "baseline_file_hashes": hashes, "candidate_file_hashes": candidate_hashes,
            "full_suite_passed": False},
        "full_suite": {"passed": 5, "failed": 3}, "full_suite_passed": False,
        "overall_release_pass_claimed": False, "original_tests_unchanged": True}


def _r6_owned_lane_ledger(base_commit):
    """Anonymous full raw-command shape, including the saved failure status."""
    results = _r5_owned_lane_ledger(base_commit)
    comparison = results["baseline_comparison"]
    for command in (results["commands"][0], comparison["baseline"]["raw_command"],
                    comparison["candidate"]["raw_command"]):
        command.update(started_at_UTC="2026-01-01T00:00:00+00:00",
                       completed_at_UTC="2026-01-01T00:00:01+00:00")
    comparison["baseline"]["raw_command"].update(
        resolved_python="/fixture/env/bin/python",
        environment_overrides={"PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "PATH_prefix": "/fixture/env/bin", "PYTHONPATH": "unset"})
    comparison["candidate"]["raw_command"].update(
        status="failed", failure_ids=list(comparison["candidate"]["failure_ids"]))
    results["commands"][0].update(failure_ids=[], passed=5)
    return results


@pytest.mark.parametrize("ledger", [_r5_owned_lane_ledger, _r6_owned_lane_ledger],
                         ids=["legacy-status-absent", "full-raw-status-failed"])
def test_r5_owned_lane_consumer_preserves_truthful_history(ledger):
    results = ledger("a" * 40)
    original = copy.deepcopy(results)
    original_hash = server.stable_sha256(results)
    accepted = server._contract_runtime_finish_test_results_consumer_acceptance(
        results, expected_baseline_commit="a" * 40)
    assert accepted["accepted"] and accepted["complete_owned_lane"]
    assert accepted["complete_known_baseline"] is False
    assert server._runtime_context_finish_no_pass_producer_accepted(
        results, expected_baseline_commit="a" * 40)
    assert results == original
    assert server.stable_sha256(results) == original_hash
    assert server._runtime_context_finish_attestation_test_results_payload(results) == original
    assert results["baseline_comparison"]["baseline"]["failed"] == 8
    assert results["full_suite"]["failed"] == 3
    assert not server._contract_runtime_value_reports_failed_qa(accepted["failure_scan"])
    # Exact-copy replacement cannot sanitize a different nested canonical hash.
    changed = copy.deepcopy(results)
    changed["full_suite"]["failed"] = 4
    outer = {"test_results": results, "nested": {"test_results": changed}}
    scanned = server._contract_runtime_finish_test_results_failure_scan(
        outer, canonical_test_results=results,
        accepted_failure_scan=accepted["failure_scan"])
    assert scanned["nested"]["test_results"] == changed
    assert server._contract_runtime_value_reports_failed_qa(scanned)


@pytest.mark.parametrize("phase", ["baseline", "candidate"])
@pytest.mark.parametrize("status", ["failed", "passed", "rejected", "unknown", "", None, False])
def test_r6_owned_lane_raw_status_is_exact_failure_observation(phase, status):
    results = _r6_owned_lane_ledger("a" * 40)
    results["baseline_comparison"][phase]["raw_command"]["status"] = status
    original = copy.deepcopy(results)
    accepted = server._contract_runtime_finish_test_results_consumer_acceptance(
        results, expected_baseline_commit="a" * 40)
    assert bool(accepted) is (status == "failed")
    assert server._runtime_context_finish_no_pass_producer_accepted(
        results, expected_baseline_commit="a" * 40) is (status == "failed")
    if accepted:
        raw = accepted["failure_scan"]["baseline_comparison"][phase]["raw_command"]
        assert raw == {**original["baseline_comparison"][phase]["raw_command"],
                       "status": "baseline_observation"}
    assert results == original


@pytest.mark.parametrize("location", ["ledger", "raw_command"])
def test_r6_owned_lane_raw_failure_does_not_hide_unrelated_qa(location):
    results = _r6_owned_lane_ledger("a" * 40)
    target = results if location == "ledger" else results["baseline_comparison"]["candidate"]["raw_command"]
    target["independent_qa"] = {"status": "rejected"}
    original = copy.deepcopy(results)
    assert not server._contract_runtime_finish_test_results_consumer_acceptance(
        results, expected_baseline_commit="a" * 40)
    assert results == original


def test_r5_owned_lane_consumer_refuses_semantic_tampering():
    original = _r6_owned_lane_ledger("a" * 40)
    for change in ("baseline", "new_failure", "owned_as_sibling", "owned_command_failure",
                   "QA_failure", "missing_comparison", "missing_hash", "changed_test",
                   "missing_raw", "release_claim", "duplicate_failure", "malformed_argv"):
        results = copy.deepcopy(original)
        comparison = results["baseline_comparison"]
        if change == "baseline": comparison["baseline_commit"] = "b" * 40
        elif change == "new_failure": comparison["candidate_new_failure_ids"] = ["tests/test_owned.py::new_failure"]
        elif change == "owned_as_sibling":
            forged = "tests/test_owned.py::test_owned_0"
            comparison["candidate"]["failure_ids"][0] = forged
            comparison["known_baseline_failure_ids"][0] = forged
            comparison["resolved_baseline_failure_ids"] = sorted(
                set(comparison["baseline"]["failure_ids"]) - set(comparison["candidate"]["failure_ids"]))
        elif change == "owned_command_failure": results["commands"][0]["exit_code"] = 1
        elif change == "QA_failure": results["independent_QA"] = {"status": "failed"}
        elif change == "missing_comparison": results.pop("baseline_comparison")
        elif change == "missing_hash": comparison.pop("baseline_file_hashes")
        elif change == "changed_test": comparison["candidate_file_hashes"]["tests/test_owned.py"] = "9" * 64
        elif change == "missing_raw": comparison["baseline"].pop("raw_command")
        elif change == "release_claim": results["overall_release_pass_claimed"] = True
        elif change == "duplicate_failure": comparison["baseline"]["failure_ids"][1] = comparison["baseline"]["failure_ids"][0]
        elif change == "malformed_argv": results["commands"][0]["argv"] = [{}]
        assert not server._contract_runtime_finish_test_results_consumer_acceptance(
            results, expected_baseline_commit="a" * 40), change
        assert not server._runtime_context_finish_no_pass_producer_accepted(
            results, expected_baseline_commit="a" * 40), change
    assert not server._contract_runtime_finish_test_results_consumer_acceptance(original)


def _exercise_finish_ledger_real_producer_premerge_and_canonical_guards(
    tmp_path, monkeypatch, request, ledger,
):
    # Reuse the existing owning-producer and narrow no-pass integration helpers.
    # Stop immediately after the real premerge consumer's later predicates;
    # do not replay that fixture's unrelated tamper/close campaign.
    import pytest
    from agent.tests import test_graph_governance_api as api
    class PremergeObserved(Exception):
        pass
    monkeypatch.setattr(api, "_complete_known_baseline_test_results",
        lambda _results, *, runtime_context, tmp_path: ledger(runtime_context.base_commit))
    original_premerge = server._contract_runtime_mf_parallel_rev10_premerge_backlog_acceptance
    def observe(conn, *args, **kwargs):
        protected, authority = original_premerge(conn, *args, **kwargs)
        assert protected is True and authority["status"] == "satisfied", authority
        assert authority["db_verified"] is True and len(authority["workers"]) == 2
        record = server._contract_runtime_store(conn).get(kwargs["source_contract_execution_id"])
        owned_lines = [(index, line) for index, line in enumerate(record["completed_lines"])
            if "baseline_commit" in line.get("payload", {}).get("test_results", {}).get("baseline_comparison", {})]
        assert len(owned_lines) == 3  # implementation, attestation, finish
        before = copy.deepcopy(record)
        for index, line in owned_lines:
            context = api.get_branch_context(conn, kwargs["project_id"], line["task_id"])
            identity = {field: str(getattr(context, field) or "") for field in (
                "runtime_context_id", "task_id", "parent_task_id", "worker_id", "worker_slot_id", "merge_queue_id")}
            options = dict(project_id=kwargs["project_id"], record=record,
                completed_line_index=index, expected_line=line,
                expected_worker_identity=identity, expected_baseline_commit=context.base_commit,
                allow_verified_worker_implementation_known_baseline=line["line_id"] == "worker_implementation",
                allow_verified_worker_finish_attestation=line["line_id"] == "worker_finish_time_attestation",
                allow_statusless_worker_finish_gate=line["line_id"] == "worker_finish_gate")
            assert server._contract_runtime_completed_line_acceptance(conn, **options)["db_verified"]
            mutated = copy.deepcopy(line)
            mutated["payload"]["test_results"]["extra"] = "changed"
            assert not server._contract_runtime_completed_line_acceptance(conn, **{**options, "expected_line": mutated})
            assert not server._contract_runtime_completed_line_acceptance(conn, **{
                **options, "expected_worker_identity": {**identity, "worker_slot_id": "foreign-slot"}})
            # Canonical line/hash is insufficient without unique durable proof.
            rows = conn.execute("SELECT * FROM backlog_contract_chain_bindings").fetchall()
            conn.execute("SAVEPOINT r5_missing_proof")
            conn.execute("DELETE FROM backlog_contract_chain_bindings")
            assert not server._contract_runtime_completed_line_acceptance(conn, **options)
            conn.execute("ROLLBACK TO r5_missing_proof")
            conn.execute("RELEASE r5_missing_proof")
            assert len(conn.execute("SELECT * FROM backlog_contract_chain_bindings").fetchall()) == len(rows)
        assert server._contract_runtime_store(conn).get(record["contract_execution_id"]) == before
        # The public merge handler derives the same acceptance during a dry-run.
        monkeypatch.setattr(server, "_contract_runtime_mf_parallel_rev10_premerge_backlog_acceptance", original_premerge)
        selected = api.get_branch_context(conn, kwargs["project_id"], kwargs["task_id"])
        rows_before = api._fresh_release_fixture_rows(conn, extra_tables=(
            "parallel_branch_runtime_contexts", "parallel_branch_merge_queue_items"))
        response = server.handle_graph_governance_parallel_branch_merge_execute(
            api._ctx({"project_id": kwargs["project_id"]}, method="POST", body={
                "repo_root_path": selected.target_project_root,
                "merge_queue_id": kwargs["merge_queue_id"],
                "queue_item_id": kwargs["queue_item_id"], "task_id": kwargs["task_id"],
                "branch_ref": selected.branch_ref, "target_ref": kwargs["target_ref"],
                "source_contract_execution_id": kwargs["source_contract_execution_id"],
                "observer_route_token_ref": kwargs["observer_route_token_ref"],
                "dry_run": True, "allow_target_ref_mutation": False,
                "evidence": {key: {"status": "pass", "passed": True} for key in (
                    "dirty_worktree_check", "test_evidence", "graph_currentness",
                    "scope_reconcile", "semantic_projection")}}))
        assert response["ok"] and response["dry_run"] and response["executed"] is False, response
        acceptance_row = next(row for row in response["gate_plan"]["evidence"] if row["key"] == "backlog_acceptance")
        assert acceptance_row["passed"] and acceptance_row["detail"]["db_verified"]
        assert api._fresh_release_fixture_rows(conn, extra_tables=(
            "parallel_branch_runtime_contexts", "parallel_branch_merge_queue_items")) == rows_before
        raise PremergeObserved()
    monkeypatch.setattr(server, "_contract_runtime_mf_parallel_rev10_premerge_backlog_acceptance", observe)
    fixture = api._isolated_api_connection(tmp_path, monkeypatch, physical_project_id="aming-claw")
    conn = next(fixture)
    try:
        with pytest.raises(PremergeObserved):
            api.test_rev10_normal_producer_finish_queue_premerge_accepts_known_baseline_and_pass(
                conn, monkeypatch, tmp_path, request, "runtime_drift", "mixed", "canonical")
    finally:
        fixture.close()


def test_r5_owned_lane_real_producer_premerge_and_canonical_guards(tmp_path, monkeypatch, request):
    _exercise_finish_ledger_real_producer_premerge_and_canonical_guards(
        tmp_path, monkeypatch, request, _r6_owned_lane_ledger)


def test_r5_owned_lane_preserves_flat_known_baseline_producer_rule(tmp_path):
    from agent.tests import test_graph_governance_api as api
    api.test_finish_no_pass_producer_matches_existing_consumer_command_rule(tmp_path)


@pytest.mark.parametrize("field", ["passed", "failed"])
@pytest.mark.parametrize("numeric_alias", [True, 1.0], ids=["bool", "float"])
def test_r5_full_suite_count_type_requires_actual_int(field, numeric_alias):
    # Exact independently reproduced owned1/sibling1 comparison shape.
    results = _r5_owned_lane_ledger("a" * 40)
    comparison = results["baseline_comparison"]
    owned_id = comparison["resolved_baseline_failure_ids"][0]
    sibling_id = comparison["known_baseline_failure_ids"][0]
    comparison["baseline"].update(failed=2, failure_ids=[owned_id, sibling_id])
    comparison["baseline"]["raw_command"]["stdout"] = "2 failed"
    comparison["candidate"].update(passed=1, failed=1, failure_ids=[sibling_id])
    comparison["candidate"]["raw_command"]["stdout"] = "1 failed, 1 passed"
    comparison["resolved_baseline_failure_ids"] = [owned_id]
    comparison["known_baseline_failure_ids"] = [sibling_id]
    results["commands"][0]["stdout"] = "1 passed"
    results["full_suite"] = {"passed": 1, "failed": 1}
    assert server._contract_runtime_finish_test_results_consumer_acceptance(
        results, expected_baseline_commit="a" * 40)["complete_owned_lane"]
    results["full_suite"][field] = numeric_alias
    original = copy.deepcopy(results)
    assert not server._contract_runtime_finish_test_results_consumer_acceptance(
        results, expected_baseline_commit="a" * 40)
    assert not server._runtime_context_finish_no_pass_producer_accepted(
        results, expected_baseline_commit="a" * 40)
    assert results == original
    assert type(results["full_suite"][field]) is type(numeric_alias)


def _r7_resolved_green_ledger(base_commit):
    """Anonymous saved R7 shape, including every raw command proof field."""
    ids = [f"tests/test_owned.py::test_owned_{index}" for index in range(3)]
    def raw(phase, argv, passed, failed, failure_ids):
        summary = f"{failed} failed, {passed} passed" if failed else f"{passed} passed"
        stdout = "\n".join(f"FAILED {item} - NotImplementedError" for item in failure_ids)
        stdout += f"\n========================= {summary} in 0.01s =========================\n"
        return {"phase": phase, "cwd": "/fixture/worker", "command": " ".join(argv),
            "argv": argv, "interpreter": "/fixture/env/bin/python",
            "status": "failed" if failed else "passed", "exit_code": 1 if failed else 0,
            "started_at": "2026-01-01T00:00:00+00:00", "completed_at": "2026-01-01T00:00:01+00:00",
            "stdout": stdout, "stderr": "", "stdout_path": f"/fixture/evidence/{phase}.stdout.txt",
            "stderr_path": f"/fixture/evidence/{phase}.stderr.txt", "passed_count": passed,
            "failed_count": failed, "collected_count": passed + failed, "failed_test_ids": failure_ids}
    argv = ["python", "-m", "pytest"]
    baseline = raw("baseline", argv, 5, 3, ids)
    baseline["head"] = base_commit
    return {"status": "passed", "passed": True,
        "commands": [raw("final-focused", argv + ["tests/test_owned.py"], 3, 0, []),
                     raw("final-full", argv, 8, 0, [])],
        "baseline_comparison": {"baseline_commit": base_commit, "baseline_commands": [baseline],
            "baseline_failed_test_ids": ids, "current_failed_test_ids": [], "resolved_test_ids": ids,
            "new_failed_test_ids": [], "inherited_dependency_failures": [],
            "summary": "Original baseline 5 passed/3 stub failures; focused 3 and full 8 now pass."},
        "local_precommit": {"command": "python -m agent.cli mf precommit-check --json-output",
            "interpreter": "/fixture/env/bin/python", "cwd": "/fixture/worker", "exit_code": 0,
            "actual_result": {"ok": True, "checks": {"plugin_update_state": {"ok": True,
                "status": "pass", "update_status": "current", "blockers": []},
                "route_context_consumption": {"ok": True, "status": "skipped"}}},
            "tool_output_chunk_id": "fixture", "result_scope": "Local CLI summary only.",
            "initial_applicability_note_correction": "Available local command executed once."},
        "raw_test_ledgers_complete_before_immutable_commit": True}


def test_r7_resolved_green_producer_consumer_preserves_raw_ledger():
    results = _r7_resolved_green_ledger("a" * 40)
    original = copy.deepcopy(results)
    original_hash = server.stable_sha256(results)
    acceptance = server._contract_runtime_finish_test_results_consumer_acceptance(
        results, expected_baseline_commit="a" * 40)
    assert acceptance["accepted"] and acceptance["complete_resolved_green_baseline"]
    assert not acceptance["complete_owned_lane"]
    assert server._runtime_context_finish_no_pass_producer_accepted(
        results, expected_baseline_commit="a" * 40)
    server._runtime_context_require_finish_no_pass_producer(
        results, expected_baseline_commit="a" * 40, require_complete_known_baseline=True)
    assert not server._contract_runtime_value_reports_failed_qa(acceptance["failure_scan"])
    assert results == original and server.stable_sha256(results) == original_hash
    assert server._runtime_context_finish_attestation_test_results_payload(results) == original
    # A different nested ledger cannot inherit this exact copy's scan authority.
    changed = copy.deepcopy(results)
    changed["independent_QA"] = {"status": "failed"}
    outer = {"test_results": results, "nested": {"test_results": changed}}
    scanned = server._contract_runtime_finish_test_results_failure_scan(
        outer, canonical_test_results=results, accepted_failure_scan=acceptance["failure_scan"])
    assert scanned["nested"]["test_results"] == changed
    assert server._contract_runtime_value_reports_failed_qa(scanned)


@pytest.mark.parametrize("tamper", ["wrong_base", "wrong_head", "current_failure", "new_failure",
    "count", "bool_count", "status", "exit", "raw_ids", "resolution", "raw_summary",
    "changed_full_command", "changed_cwd", "unrelated_qa", "nested_baseline_qa"])
def test_r7_resolved_green_refuses_inconsistent_or_current_negative_evidence(tamper):
    results = _r7_resolved_green_ledger("a" * 40)
    comparison = results["baseline_comparison"]
    baseline = comparison["baseline_commands"][0]
    focused, full = results["commands"]
    if tamper == "wrong_base": comparison["baseline_commit"] = "b" * 40
    elif tamper == "wrong_head": baseline["head"] = "b" * 40
    elif tamper == "current_failure": full.update(status="failed", exit_code=1)
    elif tamper == "new_failure": comparison["new_failed_test_ids"] = ["tests/test_owned.py::new"]
    elif tamper == "count": baseline["failed_count"] = 2
    elif tamper == "bool_count": focused["failed_count"] = False
    elif tamper == "status": baseline["status"] = "passed"
    elif tamper == "exit": full["exit_code"] = 1
    elif tamper == "raw_ids": baseline["failure_ids"] = ["tests/test_owned.py::foreign"]
    elif tamper == "resolution": comparison["resolved_test_ids"] = []
    elif tamper == "raw_summary": baseline["stdout"] = baseline["stdout"].replace("3 failed", "2 failed")
    elif tamper == "changed_full_command": full["argv"] += ["tests/test_owned.py"]
    elif tamper == "changed_cwd": baseline["cwd"] = "/fixture/foreign"
    elif tamper == "unrelated_qa": results["independent_QA"] = {"status": "rejected"}
    elif tamper == "nested_baseline_qa": baseline["independent_QA"] = {"failed_count": 1}
    original = copy.deepcopy(results)
    assert not server._contract_runtime_finish_test_results_consumer_acceptance(
        results, expected_baseline_commit="a" * 40)
    assert not server._runtime_context_finish_no_pass_producer_accepted(
        results, expected_baseline_commit="a" * 40)
    assert results == original


def test_r7_resolved_green_real_producer_finish_premerge_and_canonical_guards(tmp_path, monkeypatch, request):
    _exercise_finish_ledger_real_producer_premerge_and_canonical_guards(
        tmp_path, monkeypatch, request, _r7_resolved_green_ledger)


def test_r7_resolved_green_implementation_alias_is_exact_summary_only():
    results = _r7_resolved_green_ledger("a" * 40)
    identity = dict(runtime_context_id="ctx", task_id="task", parent_task_id="parent",
                    worker_id="worker", worker_slot_id="source", merge_queue_id="queue")
    aliases = [{key: command[key] for key in ("command", "status", "exit_code", "failed_count")}
               for command in results["commands"]]
    line = {**identity, "stage_id": "worker_implementation", "line_id": "worker_implementation",
        "actor_role": "mf_sub", "evidence_kind": "implementation", "line_instance_id": "runtime_context:ctx",
        "payload": {**identity, "test_results": results, "tests": aliases}}
    original = copy.deepcopy(line)
    assert server._contract_runtime_worker_implementation_known_baseline_scan_candidate(
        line, expected_worker_identity=identity, expected_baseline_commit="a" * 40)["accepted"]
    assert line == original
    line["payload"]["tests"][0]["failed_count"] = False
    assert not server._contract_runtime_worker_implementation_known_baseline_scan_candidate(
        line, expected_worker_identity=identity, expected_baseline_commit="a" * 40)
