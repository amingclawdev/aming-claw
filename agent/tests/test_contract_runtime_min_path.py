from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance.contracts import ContractDefinitionRegistry, ContractRuntime
from agent.governance.contracts.hash import file_sha256
from agent.governance.contracts.runtime import (
    ContractRuntimeError,
    read_backlog_contract_chain_current,
    rebuild_backlog_contract_chain_projection,
    SQLiteContractExecutionStore,
    StalePinnedContractExecutionError,
    upsert_contract_chain_successor_binding,
)


def _write_minimal_contract(tmp_path, *, status: str = "active"):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    guide = prompts / "observer.md"
    guide.write_text("Follow the compiled runtime guide.\n", encoding="utf-8")
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": "observer_onboard",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "onboard",
        "status": status,
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "bootstrap",
                    "lines": [
                        {
                            "line_id": "read_context",
                            "owner_role": "observer",
                            "allowed_writer_roles": ["observer"],
                            "evidence_kind": "contract_state_changed",
                        }
                    ],
                },
                {
                    "stage_id": "qa",
                    "lines": [
                        {
                            "line_id": "qa_verdict",
                            "owner_role": "qa",
                            "allowed_writer_roles": ["qa"],
                            "evidence_kind": "qa_verification",
                        }
                    ],
                },
            ]
        },
        "instruction_layer": {
            "inline": ["Runtime guide is authoritative."],
            "refs": [
                {
                    "id": "observer_prompt",
                    "path": "prompts/observer.md",
                    "sha256": file_sha256(guide),
                    "visible_to_roles": ["observer"],
                    "stage_ids": ["bootstrap"],
                }
            ],
        },
    }
    path = tmp_path / "observer_onboard.v1.rev1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_contract_definition(tmp_path, *, contract_id: str, stages: list[dict]):
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": contract_id,
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": contract_id,
        "status": "active",
        "rule_layer": {"stages": stages},
        "instruction_layer": {
            "inline": ["Runtime guide is authoritative."],
            "refs": [],
        },
    }
    path = tmp_path / f"{contract_id}.v1.rev1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_chain_projection_contracts(tmp_path):
    _write_minimal_contract(tmp_path)
    _write_contract_definition(
        tmp_path,
        contract_id="direct_fix",
        stages=[
            {
                "stage_id": "worker_graph_context",
                "lines": [
                    {
                        "line_id": "direct_fix_worker_graph_context",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "graph_trace",
                    }
                ],
            },
            {
                "stage_id": "candidate_repair",
                "lines": [
                    {
                        "line_id": "direct_fix_candidate_repair",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "direct_fix_repair_evidence",
                        "requires": ["direct_fix_worker_graph_context"],
                    }
                ],
            },
            {
                "stage_id": "qa_graph_context",
                "lines": [
                    {
                        "line_id": "direct_fix_qa_graph_context",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "graph_trace",
                        "requires": ["direct_fix_candidate_repair"],
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                        "requires": ["direct_fix_qa_graph_context"],
                    }
                ],
            },
            {
                "stage_id": "return_to_parent",
                "lines": [
                    {
                        "line_id": "direct_fix_return_to_parent",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "direct_fix_return_to_parent",
                        "requires": ["qa_independent_verification"],
                    }
                ],
            },
        ],
    )
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v1",
        stages=[
            {
                "stage_id": "observer_prefill",
                "lines": [
                    {
                        "line_id": "observer_prefill_child_contracts",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "mf_parallel_prefill",
                    }
                ],
            }
        ],
    )


def _write_from(record, *, actor_role, stage_id, line_id, evidence_kind=None):
    state = record["execution_state"]
    guide = record["runtime_guide"]
    next_action = guide.get("next_legal_action") or {}
    return {
        "project_id": record["project_id"],
        "backlog_id": record["backlog_id"],
        "contract_execution_id": record["contract_execution_id"],
        "definition_hash": record["definition_hash"],
        "instruction_bundle_hash": record["instruction_bundle_hash"],
        "execution_state_revision": state["execution_state_revision"],
        "runtime_guide_hash": guide["runtime_guide_hash"],
        "stage_id": stage_id,
        "line_id": line_id,
        "actor_role": actor_role,
        "evidence_kind": evidence_kind or next_action.get("evidence_kind") or "",
    }


def _direct_fix_graph_payload(*, actor_role: str, trace_id: str):
    query_source = {
        "observer": "observer",
        "mf_sub": "mf_subagent",
        "qa": "qa",
    }[actor_role]
    query_purpose = {
        "observer": "observer_scope_build",
        "mf_sub": "subagent_context_build",
        "qa": "independent_verification",
    }[actor_role]
    evidence = {
        "schema_version": "direct_fix_graph_trace_db_evidence.v1",
        "db_verified": True,
        "trace_ids": [trace_id],
        "verified_trace_ids": [trace_id],
        "query_source": query_source,
        "query_purpose": query_purpose,
        "target_project_root": "/tmp/aming-claw-test",
    }
    if actor_role == "mf_sub":
        evidence.update(
            {
                "worker_role": "mf_sub",
                "runtime_context_id": "mfrctx-test",
                "task_id": "direct-fix-worker-test",
                "parent_task_id": "cex-direct-fix-parent-test",
            }
        )
    return {
        "schema_version": "direct_fix_graph_context.v1",
        "graph_trace_ids": [trace_id],
        "graph_trace_evidence": evidence,
    }


def _start_repaired_direct_fix(runtime, *, backlog_id: str):
    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id=f"cex-root-{backlog_id}",
        actor_role="observer",
        route_token_ref=f"rtok-root-{backlog_id}",
    )
    direct_fix, generation, repair_ref = _start_repaired_direct_fix_child(
        runtime,
        root,
        backlog_id=backlog_id,
        contract_execution_id=f"cex-direct-{backlog_id}",
    )
    return root, direct_fix, generation, repair_ref


def _start_repaired_direct_fix_child(
    runtime,
    root,
    *,
    backlog_id: str,
    contract_execution_id: str,
):
    direct_fix = runtime.start_execution(
        "direct_fix",
        project_id="aming-claw",
        backlog_id=backlog_id,
        contract_execution_id=contract_execution_id,
        actor_role="observer",
        route_token_ref=f"rtok-{contract_execution_id}",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )
    runtime.current_guide(direct_fix["contract_execution_id"], actor_role="mf_sub")
    direct_fix = runtime.store.get(direct_fix["contract_execution_id"])
    worker_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                direct_fix,
                actor_role="mf_sub",
                stage_id="worker_graph_context",
                line_id="direct_fix_worker_graph_context",
                evidence_kind="graph_trace",
            ),
            "runtime_context_id": "mfrctx-test",
            "task_id": "direct-fix-worker-test",
            "parent_task_id": "cex-direct-fix-parent-test",
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id=f"gqt-worker-{contract_execution_id}",
            ),
        },
        actor_role="mf_sub",
    )
    assert worker_graph["ok"] is True
    direct_fix = worker_graph["record"]
    repaired = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            direct_fix,
            actor_role="mf_sub",
            stage_id="candidate_repair",
            line_id="direct_fix_candidate_repair",
            evidence_kind="direct_fix_repair_evidence",
        ),
        actor_role="mf_sub",
    )
    assert repaired["ok"] is True
    record = repaired["record"]
    repair_line_index = len(record["completed_lines"]) - 1
    repair_ref = (
        f"contract_runtime:{record['contract_execution_id']}:"
        f"completed_lines:{repair_line_index}"
    )
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    record = runtime.store.get(record["contract_execution_id"])
    qa_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="direct_fix_qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="qa",
                trace_id=f"gqt-qa-{contract_execution_id}",
            ),
        },
        actor_role="qa",
    )
    assert qa_graph["ok"] is True
    record = qa_graph["record"]
    return record, int(record["execution_state_revision"]), repair_ref


def _direct_fix_qa_write(record, *, generation: int, repair_ref: str):
    write = _write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
        evidence_kind="independent_verification",
    )
    write["payload"] = {
        "status": "pass",
        "direct_fix_contract_execution_id": record["contract_execution_id"],
        "projection_generation": generation,
        "source_refs": [repair_ref],
    }
    write["verification"] = {
        "verdict": "PASS",
        "independent": True,
        "child_contract_execution_id": record["contract_execution_id"],
        "projection_generation": generation,
        "source_ref": repair_ref,
    }
    return write


def _return_direct_fix_to_parent(runtime, record, *, generation: int, repair_ref: str):
    qa = runtime.submit_line_write(
        record["contract_execution_id"],
        _direct_fix_qa_write(
            record,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True
    runtime.current_guide(record["contract_execution_id"], actor_role="observer")
    qa_record = runtime.store.get(record["contract_execution_id"])
    returned = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            qa_record,
            actor_role="observer",
            stage_id="return_to_parent",
            line_id="direct_fix_return_to_parent",
            evidence_kind="direct_fix_return_to_parent",
        ),
        actor_role="observer",
    )
    assert returned["ok"] is True
    return returned["record"]


def test_direct_fix_graph_context_gates_repair_and_qa(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
        contract_execution_id="cex-root-graph-gate",
        actor_role="observer",
        route_token_ref="rtok-root-graph-gate",
    )
    direct_fix = runtime.start_execution(
        "direct_fix",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
        contract_execution_id="cex-direct-graph-gate",
        actor_role="observer",
        route_token_ref="rtok-direct-graph-gate",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    guide = runtime.current_guide(direct_fix["contract_execution_id"], actor_role="mf_sub")
    assert guide["next_legal_action"]["line_id"] == "direct_fix_worker_graph_context"

    missing_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            runtime.store.get(direct_fix["contract_execution_id"]),
            actor_role="mf_sub",
            stage_id="worker_graph_context",
            line_id="direct_fix_worker_graph_context",
            evidence_kind="graph_trace",
        ),
        actor_role="mf_sub",
    )
    assert missing_graph["ok"] is False
    assert "direct_fix_worker_graph_context requires non-empty graph_trace_ids" in (
        missing_graph["decision"]["errors"]
    )

    record = runtime.store.get(direct_fix["contract_execution_id"])
    worker_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="mf_sub",
                stage_id="worker_graph_context",
                line_id="direct_fix_worker_graph_context",
                evidence_kind="graph_trace",
            ),
            "runtime_context_id": "mfrctx-test",
            "task_id": "direct-fix-worker-test",
            "parent_task_id": "cex-direct-fix-parent-test",
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-worker-graph-gate",
            ),
        },
        actor_role="mf_sub",
    )
    assert worker_graph["ok"] is True
    assert (
        worker_graph["record"]["runtime_guide"]["next_legal_action"]["line_id"]
        == "direct_fix_candidate_repair"
    )

    repaired = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            worker_graph["record"],
            actor_role="mf_sub",
            stage_id="candidate_repair",
            line_id="direct_fix_candidate_repair",
            evidence_kind="direct_fix_repair_evidence",
        ),
        actor_role="mf_sub",
    )
    assert repaired["ok"] is True
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
    )
    assert current["readiness_state"] == "direct_fix_complete_awaiting_independent_qa_graph"
    assert current["next_legal_action"]["line_id"] == "direct_fix_qa_graph_context"

    record = runtime.store.get(direct_fix["contract_execution_id"])
    bad_qa_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="direct_fix_qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-wrong-role-for-qa",
            ),
        },
        actor_role="qa",
    )
    assert bad_qa_graph["ok"] is False
    assert any("query_source must be one of ['qa']" in error for error in bad_qa_graph["decision"]["errors"])

    record = runtime.store.get(direct_fix["contract_execution_id"])
    qa_graph = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        {
            **_write_from(
                record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="direct_fix_qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="qa",
                trace_id="gqt-qa-graph-gate",
            ),
        },
        actor_role="qa",
    )
    assert qa_graph["ok"] is True
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-GATE",
    )
    assert current["next_legal_action"]["line_id"] == "qa_independent_verification"


def test_mf_parallel_v2_graph_context_gates_worker_and_qa(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel_graph_gate",
        stages=[
            {
                "stage_id": "worker_context",
                "lines": [
                    {
                        "line_id": "worker_graph_context",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "graph_trace",
                    }
                ],
            },
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                        "requires": ["worker_graph_context"],
                    }
                ],
            },
            {
                "stage_id": "qa_graph_context",
                "lines": [
                    {
                        "line_id": "qa_graph_context",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "graph_trace",
                        "requires": ["worker_implementation"],
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                        "requires": ["qa_graph_context"],
                    }
                ],
            },
        ],
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "mf_parallel_graph_gate",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-V2-GRAPH-GATE",
        contract_execution_id="cex-mf-parallel-v2-graph-gate",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-v2-graph-gate",
    )
    assert record["runtime_guide"]["next_legal_action"]["line_id"] == "worker_graph_context"
    runtime.current_guide(record["contract_execution_id"], actor_role="mf_sub")
    record = runtime.store.get(record["contract_execution_id"])

    missing_worker_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            record,
            actor_role="mf_sub",
            stage_id="worker_context",
            line_id="worker_graph_context",
            evidence_kind="graph_trace",
        ),
        actor_role="mf_sub",
    )
    assert missing_worker_graph["ok"] is False
    assert "worker_graph_context requires non-empty graph_trace_ids" in (
        missing_worker_graph["decision"]["errors"]
    )

    worker_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                runtime.store.get(record["contract_execution_id"]),
                actor_role="mf_sub",
                stage_id="worker_context",
                line_id="worker_graph_context",
                evidence_kind="graph_trace",
            ),
            "runtime_context_id": "mfrctx-mf-parallel-v2-test",
            "task_id": "mf-parallel-v2-worker",
            "parent_task_id": "cex-mf-parallel-v2-parent",
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-mf-parallel-v2-worker",
            ),
        },
        actor_role="mf_sub",
    )
    assert worker_graph["ok"] is True

    implementation = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            worker_graph["record"],
            actor_role="mf_sub",
            stage_id="worker_implementation",
            line_id="worker_implementation",
            evidence_kind="implementation",
        ),
        actor_role="mf_sub",
    )
    assert implementation["ok"] is True
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    bad_qa_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="mf_sub",
                trace_id="gqt-mf-parallel-v2-wrong-source",
            ),
        },
        actor_role="qa",
    )
    assert bad_qa_graph["ok"] is False
    assert any(
        "qa_graph_context query_source must be one of ['qa']" in error
        for error in bad_qa_graph["decision"]["errors"]
    )
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])

    qa_graph = runtime.submit_line_write(
        record["contract_execution_id"],
        {
            **_write_from(
                qa_record,
                actor_role="qa",
                stage_id="qa_graph_context",
                line_id="qa_graph_context",
                evidence_kind="graph_trace",
            ),
            "payload": _direct_fix_graph_payload(
                actor_role="qa",
                trace_id="gqt-mf-parallel-v2-qa",
            ),
        },
        actor_role="qa",
    )
    assert qa_graph["ok"] is True
    assert (
        qa_graph["record"]["runtime_guide"]["next_legal_action"]["line_id"]
        == "qa_independent_verification"
    )


def test_direct_fix_same_revision_graph_gate_migration_is_explicit(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="direct_fix",
        stages=[
            {
                "stage_id": "candidate_repair",
                "lines": [
                    {
                        "line_id": "direct_fix_candidate_repair",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "direct_fix_repair_evidence",
                    }
                ],
            }
        ],
    )
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "direct_fix",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-GRAPH-MIGRATION",
        contract_execution_id="cex-direct-legacy-active",
        actor_role="observer",
        route_token_ref="rtok-legacy-active",
    )
    assert record["runtime_guide"]["next_legal_action"]["line_id"] == "direct_fix_candidate_repair"

    _write_chain_projection_contracts(tmp_path)

    with pytest.raises(StalePinnedContractExecutionError):
        runtime.current_guide(record["contract_execution_id"], actor_role="mf_sub")


def _append_parent_successor_ack(runtime, *, parent_id: str, child_id: str):
    parent = runtime.store.get(parent_id)
    completed_lines = list(parent.get("completed_lines") or [])
    completed_lines.append(
        {
            "stage_id": "successor_return",
            "line_id": "resume_parent_after_successor_return",
            "actor_role": "observer",
            "evidence_kind": "successor_return_acknowledgement",
            "payload": {
                "schema_version": "successor_return_acknowledgement.v1",
                "parent_contract_execution_id": parent_id,
                "successor_contract_execution_id": child_id,
                "successor_contract_id": "direct_fix",
            },
        }
    )
    parent["completed_lines"] = completed_lines
    revision = int(parent.get("execution_state_revision") or 0) + 1
    parent["execution_state_revision"] = revision
    execution_state = parent.get("execution_state")
    if isinstance(execution_state, dict):
        execution_state["execution_state_revision"] = revision
    return runtime.store.update(parent_id, parent)


def test_minimal_contract_runtime_drives_next_action_and_role_gate(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)

    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        contract_execution_id="cex-min-path",
        actor_role="observer",
        route_token_ref="rtok-min-path",
    )

    guide = record["runtime_guide"]
    assert guide["next_legal_action"] == {
        "stage_id": "bootstrap",
        "line_id": "read_context",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "evidence_kind": "contract_state_changed",
        "required": True,
    }
    assert guide["instructions"]["inline"] == ["Runtime guide is authoritative."]
    assert guide["instructions"]["refs"][0]["content"] == "Follow the compiled runtime guide.\n"

    result = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            record,
            actor_role="observer",
            stage_id="bootstrap",
            line_id="read_context",
        ),
    )
    assert result["ok"] is True
    next_record = result["record"]
    assert next_record["execution_state"]["execution_state_revision"] == 2
    assert next_record["runtime_guide"]["next_legal_action"]["stage_id"] == "qa"
    assert next_record["runtime_guide"]["next_legal_action"]["owner_role"] == "qa"

    rejected = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            next_record,
            actor_role="observer",
            stage_id="qa",
            line_id="qa_verdict",
        ),
    )
    assert rejected["ok"] is False
    assert "cannot write line" in rejected["decision"]["errors"][0]

    runtime.current_guide("cex-min-path", actor_role="qa")
    qa_record = runtime.store.get("cex-min-path")
    accepted_qa = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            qa_record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_verdict",
        ),
    )
    assert accepted_qa["ok"] is True
    assert accepted_qa["record"]["runtime_guide"]["next_legal_action"] is None


def test_minimal_runtime_rejects_stale_runtime_guide_hash(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )
    write = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
    )
    write["runtime_guide_hash"] = "sha256:" + "0" * 64

    result = runtime.submit_line_write(record["contract_execution_id"], write)

    assert result["ok"] is False
    assert "runtime_guide_hash mismatch" in result["decision"]["errors"]


def test_runtime_write_gate_rejects_negative_cases(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )

    stale_revision = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
    )
    stale_revision["execution_state_revision"] = 0
    assert runtime.submit_line_write(
        record["contract_execution_id"], stale_revision
    )["decision"]["errors"] == ["execution_state_revision mismatch"]

    wrong_next_action = _write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_verdict",
        evidence_kind="qa_verification",
    )
    wrong_next_errors = runtime.submit_line_write(
        record["contract_execution_id"], wrong_next_action
    )["decision"]["errors"]
    assert any("write does not match next legal action" in item for item in wrong_next_errors)

    wrong_role = _write_from(
        record,
        actor_role="qa",
        stage_id="bootstrap",
        line_id="read_context",
    )
    wrong_role_errors = runtime.submit_line_write(
        record["contract_execution_id"], wrong_role
    )["decision"]["errors"]
    assert any("cannot write line" in item for item in wrong_role_errors)

    wrong_evidence_kind = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
        evidence_kind="qa_verification",
    )
    assert "evidence_kind mismatch" in runtime.submit_line_write(
        record["contract_execution_id"], wrong_evidence_kind
    )["decision"]["errors"]

    forged_role = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
    )
    forged_result = runtime.submit_line_write(
        record["contract_execution_id"],
        forged_role,
        actor_role="mf_sub",
    )
    assert forged_result["ok"] is False
    assert any("cannot write line" in item for item in forged_result["decision"]["errors"])


def test_sqlite_contract_execution_store_persists_rows_and_cas(tmp_path):
    _write_minimal_contract(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = SQLiteContractExecutionStore(conn)
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=store,
    )

    created = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        contract_execution_id="cex-sqlite-min-path",
        actor_role="observer",
        route_token_ref="rtok-sqlite",
    )
    row = conn.execute(
        "SELECT contract_execution_id, execution_state_revision, record_json "
        "FROM contract_runtime_executions WHERE contract_execution_id = ?",
        ("cex-sqlite-min-path",),
    ).fetchone()
    assert dict(row)["contract_execution_id"] == "cex-sqlite-min-path"
    assert row["execution_state_revision"] == 1
    assert json.loads(row["record_json"])["route_token_ref"] == "rtok-sqlite"

    read_back = SQLiteContractExecutionStore(conn).get("cex-sqlite-min-path")
    assert read_back["runtime_guide"]["next_legal_action"]["line_id"] == "read_context"

    write_result = runtime.submit_line_write(
        "cex-sqlite-min-path",
        _write_from(
            created,
            actor_role="observer",
            stage_id="bootstrap",
            line_id="read_context",
        ),
    )
    assert write_result["ok"] is True
    assert write_result["record"]["execution_state_revision"] == 2

    stale = dict(write_result["record"])
    stale["execution_state_revision"] = 3
    with pytest.raises(ContractRuntimeError, match="stale execution_state_revision"):
        store.update("cex-sqlite-min-path", stale, expected_revision=1)


def test_contract_chain_mapping_schema_idempotent_and_rebuilds_projection(tmp_path):
    _write_minimal_contract(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = SQLiteContractExecutionStore(conn)
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=store,
    )

    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
        contract_execution_id="cex-map-root",
        actor_role="observer",
        route_token_ref="rtok-map-root",
    )
    child = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
        contract_execution_id="cex-map-child",
        actor_role="observer",
        route_token_ref="rtok-map-child",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    first = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
    )
    assert first["contract_chain_id"] == root["contract_chain_id"]
    assert first["root_contract_execution_id"] == root["contract_execution_id"]
    assert first["active_child_contract_execution_id"] == child["contract_execution_id"]
    assert first["current_contract_execution_id"] == child["contract_execution_id"]
    assert first["projection_watermark"] >= 2
    first_hash = first["projection_hash"]
    first_watermark = first["projection_watermark"]

    conn.execute("DELETE FROM backlog_contract_chain_current")
    conn.execute("DELETE FROM contract_chain_edges")
    conn.execute("DELETE FROM backlog_contract_chain_bindings")
    rebuilt = rebuild_backlog_contract_chain_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
    )
    rebuilt_again = rebuild_backlog_contract_chain_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MAPPING-MIN-PATH",
    )
    assert rebuilt["current_contract_execution_id"] == child["contract_execution_id"]
    assert rebuilt["projection_hash"] == first_hash
    assert rebuilt["projection_watermark"] > first_watermark
    assert rebuilt_again["projection_hash"] == rebuilt["projection_hash"]
    binding_count = conn.execute(
        "SELECT COUNT(*) FROM backlog_contract_chain_bindings"
    ).fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM contract_chain_edges").fetchone()[0]
    upsert_contract_chain_successor_binding(
        conn,
        parent_record=root,
        child_record=child,
        edge_kind="test_child",
        binding_kind="successor_current",
    )
    upsert_contract_chain_successor_binding(
        conn,
        parent_record=root,
        child_record=child,
        edge_kind="test_child",
        binding_kind="successor_current",
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM backlog_contract_chain_bindings"
    ).fetchone()[0] == binding_count
    assert conn.execute("SELECT COUNT(*) FROM contract_chain_edges").fetchone()[0] == (
        edge_count + 1
    )


def test_direct_fix_qa_without_explicit_binding_counts_after_repair(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, _, _ = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-GENERIC",
    )

    generic_qa_write = _write_from(
        direct_fix,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
        evidence_kind="independent_verification",
    )
    generic_qa_write["payload"] = {
        "status": "pass",
        "qa_summary": "generic QA line without child/projection/source refs",
    }
    generic_qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        generic_qa_write,
        actor_role="qa",
    )
    assert generic_qa["ok"] is True

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-QA-GENERIC",
    )
    assert current["readiness_state"] == "return_to_parent_after_direct_fix_qa"
    assert current["current_contract_execution_id"] == direct_fix["contract_execution_id"]
    assert current["next_legal_action"]["id"] == "return_to_parent_after_direct_fix_qa"


def test_direct_fix_qa_with_child_generation_and_source_refs_is_counted(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-EXPLICIT",
    )

    qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _direct_fix_qa_write(
            direct_fix,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-QA-EXPLICIT",
    )
    assert current["readiness_state"] == "return_to_parent_after_direct_fix_qa"
    assert current["current_contract_execution_id"] == direct_fix["contract_execution_id"]
    assert current["next_legal_action"]["id"] == "return_to_parent_after_direct_fix_qa"
    assert current["next_legal_action"]["qa_evidence_ref"] == (
        f"contract_runtime:{direct_fix['contract_execution_id']}:completed_lines:3"
    )


def test_direct_fix_projection_does_not_resume_parent_when_return_precedes_qa(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-RETURN-BEFORE-QA",
    )

    record = runtime.store.get(direct_fix["contract_execution_id"])
    return_line = _write_from(
        record,
        actor_role="observer",
        stage_id="return_to_parent",
        line_id="direct_fix_return_to_parent",
        evidence_kind="direct_fix_return_to_parent",
    )
    qa_line = _direct_fix_qa_write(
        record,
        generation=generation,
        repair_ref=repair_ref,
    )
    mutated = dict(record)
    mutated["completed_lines"] = list(record["completed_lines"]) + [
        return_line,
        qa_line,
    ]
    runtime.store.update(mutated["contract_execution_id"], mutated)

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-RETURN-BEFORE-QA",
        rebuild_if_missing=True,
    )
    assert current["readiness_state"] == "return_to_parent_after_direct_fix_qa"
    assert current["current_contract_execution_id"] == direct_fix["contract_execution_id"]
    assert current["next_legal_action"]["id"] == "return_to_parent_after_direct_fix_qa"


def test_later_mf_parallel_successor_becomes_current_after_direct_fix_return(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    root, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
    )
    qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _direct_fix_qa_write(
            direct_fix,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True
    runtime.current_guide(direct_fix["contract_execution_id"], actor_role="observer")
    qa_record = runtime.store.get(direct_fix["contract_execution_id"])
    returned = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            qa_record,
            actor_role="observer",
            stage_id="return_to_parent",
            line_id="direct_fix_return_to_parent",
            evidence_kind="direct_fix_return_to_parent",
        ),
        actor_role="observer",
    )
    assert returned["ok"] is True
    current_after_return = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
    )
    assert current_after_return["readiness_state"] == (
        "parent_resume_required_after_direct_fix_qa"
    )
    parent_next = current_after_return["next_legal_action"]
    assert parent_next["id"] == "resume_parent_after_successor_return"
    assert parent_next["stage_id"] == "successor_return"
    assert parent_next["line_id"] == "resume_parent_after_successor_return"
    assert parent_next["evidence_kind"] == "successor_return_acknowledgement"
    assert parent_next["owner_role"] == "observer"

    mf_parallel = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
        contract_execution_id="zz-mf-parallel-after-direct-fix",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-after-direct-fix",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-THEN-MF-PARALLEL",
    )
    assert current["current_contract_execution_id"] == (
        mf_parallel["contract_execution_id"]
    )
    assert current["active_child_contract_execution_id"] == (
        mf_parallel["contract_execution_id"]
    )
    assert current["current_contract_id"] == "mf_parallel.v1"
    assert current["readiness_state"] == "contract_active"


def test_parent_resume_cursor_advances_across_returned_direct_fix_children(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    backlog_id = "AC-DIRECT-FIX-MULTI-RETURN-CURSOR"
    root, first_child, first_generation, first_repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id=backlog_id,
    )
    first_child = _return_direct_fix_to_parent(
        runtime,
        first_child,
        generation=first_generation,
        repair_ref=first_repair_ref,
    )
    second_child, second_generation, second_repair_ref = _start_repaired_direct_fix_child(
        runtime,
        root,
        backlog_id=backlog_id,
        contract_execution_id=f"cex-direct-b-{backlog_id}",
    )
    second_child = _return_direct_fix_to_parent(
        runtime,
        second_child,
        generation=second_generation,
        repair_ref=second_repair_ref,
    )

    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )
    assert current["readiness_state"] == (
        "parent_resume_required_after_direct_fix_qa"
    )
    assert current["next_legal_action"]["successor_contract_execution_id"] == (
        first_child["contract_execution_id"]
    )

    _append_parent_successor_ack(
        runtime,
        parent_id=root["contract_execution_id"],
        child_id=first_child["contract_execution_id"],
    )
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )
    assert current["readiness_state"] == (
        "parent_resume_required_after_direct_fix_qa"
    )
    assert current["next_legal_action"]["successor_contract_execution_id"] == (
        second_child["contract_execution_id"]
    )

    _append_parent_successor_ack(
        runtime,
        parent_id=root["contract_execution_id"],
        child_id=second_child["contract_execution_id"],
    )
    current = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id=backlog_id,
    )
    assert current["current_contract_execution_id"] == root["contract_execution_id"]
    assert current["parent_to_resume_contract_execution_id"] == ""
    assert current["readiness_state"] == "contract_active"
    assert current["next_legal_action"]["line_id"] == "read_context"


def test_server_chain_current_refreshes_stale_next_action_after_finish(tmp_path):
    from agent.governance import server

    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )

    qa = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _direct_fix_qa_write(
            direct_fix,
            generation=generation,
            repair_ref=repair_ref,
        ),
        actor_role="qa",
    )
    assert qa["ok"] is True
    stale_after_qa = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )
    assert stale_after_qa["next_legal_action"]["line_id"] == (
        "direct_fix_return_to_parent"
    )

    runtime.current_guide(direct_fix["contract_execution_id"], actor_role="observer")
    record = runtime.store.get(direct_fix["contract_execution_id"])
    returned = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        _write_from(
            record,
            actor_role="observer",
            stage_id="return_to_parent",
            line_id="direct_fix_return_to_parent",
            evidence_kind="direct_fix_return_to_parent",
        ),
        actor_role="observer",
    )
    assert returned["ok"] is True
    assert returned["record"]["runtime_guide"]["next_legal_action"] is None

    conn.execute(
        """
        UPDATE backlog_contract_chain_current
        SET next_legal_action_json = ?
        WHERE project_id = ? AND backlog_id = ?
        """,
        (
            json.dumps(
                stale_after_qa["next_legal_action"],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "aming-claw",
            "AC-DIRECT-FIX-STALE-AFTER-FINISH",
        ),
    )
    stale_read = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )
    assert stale_read["next_legal_action"]["line_id"] == "direct_fix_return_to_parent"

    refreshed = server._contract_chain_current_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX-STALE-AFTER-FINISH",
    )

    assert refreshed["current_contract_execution_id"].startswith("cex-root-")
    assert refreshed["next_legal_action"]["line_id"] == "read_context"
    assert refreshed["next_legal_action"]["line_id"] != "direct_fix_return_to_parent"
    assert refreshed["contract_runtime_current_state"]["next_legal_action"][
        "line_id"
    ] == "read_context"
    freshness = refreshed["projection_freshness"]
    assert freshness["status"] == "refreshed_from_contract_runtime_current"
    assert freshness["runtime_context_projection_applied"] is False
    assert freshness["stale_next_legal_action"]["line_id"] == (
        "direct_fix_return_to_parent"
    )


def test_server_chain_current_projects_mf_parallel_runtime_context_completion_read_only(
    tmp_path,
    monkeypatch,
):
    from agent.governance import server

    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE",
        contract_execution_id="cex-mf-parallel-runtime-context-projected-complete",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-runtime-context-projected-complete",
    )
    stored_before = runtime.store.get(record["contract_execution_id"])
    assert stored_before.get("completed_lines") == []
    durable = read_backlog_contract_chain_current(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE",
    )
    assert durable["readiness_state"] == "contract_active"
    assert durable["next_legal_action"]["line_id"] == "observer_prefill_child_contracts"

    projected_line = {
        "stage_id": "observer_prefill",
        "line_id": "observer_prefill_child_contracts",
        "actor_role": "observer",
        "evidence_kind": "mf_parallel_prefill",
        "line_instance_id": "runtime_context:mfrctx-projected-complete",
        "payload": {
            "source": "runtime_context_worker_evidence",
            "projection_persists_completed_line": False,
            "observer_authored_worker_backfill": False,
        },
    }
    projection = {
        "schema_version": "contract_runtime.mf_parallel_runtime_context_projection.v1",
        "source": "runtime_context_worker_evidence",
        "projected_completed_lines": [projected_line],
        "projected_line_count": 1,
        "persistence": {
            "mutates_contract_runtime_completed_lines": False,
            "observer_authored_worker_backfill": False,
        },
    }

    def projected_runtime_current(_conn, *, project_id, record, actor_role):
        assert project_id == "aming-claw"
        assert actor_role == "observer"
        projected = dict(record)
        projected["execution_state_revision"] = 2
        projected["execution_state"] = {
            "schema_version": "contract_execution_state.v1",
            "contract_execution_id": record["contract_execution_id"],
            "execution_state_revision": 2,
            "execution_state_hash": "sha256:projected-runtime-context-state",
            "completed_lines": [projected_line],
        }
        projected["runtime_guide"] = {
            "schema_version": "contract_runtime_guide.v1",
            "next_legal_action": None,
            "runtime_guide_hash": "sha256:projected-runtime-context-guide",
            "execution": {
                "project_id": "aming-claw",
                "backlog_id": (
                    "AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE"
                ),
                "contract_execution_id": record["contract_execution_id"],
                "execution_state_revision": 2,
                "execution_state_hash": "sha256:projected-runtime-context-state",
                "route_token_ref": (
                    "rtok-mf-parallel-runtime-context-projected-complete"
                ),
            },
        }
        return projected, projection

    monkeypatch.setattr(
        server,
        "_contract_runtime_apply_mf_parallel_context_projection",
        projected_runtime_current,
    )

    refreshed = server._contract_chain_current_projection(
        conn,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-RUNTIME-CONTEXT-PROJECTED-COMPLETE",
    )

    assert refreshed["readiness_state"] == "contract_complete"
    assert refreshed["next_legal_action"] == {}
    assert refreshed["contract_runtime_current_state"]["readiness_state"] == (
        "contract_complete"
    )
    assert refreshed["contract_runtime_current_state"]["next_legal_action"] == {}
    freshness = refreshed["projection_freshness"]
    assert freshness["status"] == "refreshed_from_contract_runtime_current"
    assert freshness["runtime_context_projection_applied"] is True
    assert freshness["readiness_state_changed"] is True
    assert freshness["stale_readiness_state"] == "contract_active"
    assert freshness["refreshed_readiness_state"] == "contract_complete"
    assert freshness["runtime_context_projection"]["persistence"] == {
        "mutates_contract_runtime_completed_lines": False,
        "observer_authored_worker_backfill": False,
    }

    stored_after = runtime.store.get(record["contract_execution_id"])
    assert stored_after.get("completed_lines") == []


def test_mf_parallel_failed_qa_summary_resets_completion_path(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v1",
        stages=[
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                        "requires": ["worker_implementation"],
                    }
                ],
            },
            {
                "stage_id": "observer_integration",
                "lines": [
                    {
                        "line_id": "observer_merge",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "merge",
                        "requires": ["qa_independent_verification"],
                    }
                ],
            },
        ],
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-FAILED-QA-SUMMARY",
        contract_execution_id="cex-mf-parallel-failed-qa-summary",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-failed-qa-summary",
    )

    runtime.current_guide(record["contract_execution_id"], actor_role="mf_sub")
    worker_record = runtime.store.get(record["contract_execution_id"])
    worker = runtime.submit_line_write(
        record["contract_execution_id"],
        _write_from(
            worker_record,
            actor_role="mf_sub",
            stage_id="worker_implementation",
            line_id="worker_implementation",
            evidence_kind="implementation",
        ),
        actor_role="mf_sub",
    )
    assert worker["ok"] is True
    runtime.current_guide(record["contract_execution_id"], actor_role="qa")
    qa_record = runtime.store.get(record["contract_execution_id"])
    qa_write = _write_from(
        qa_record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
        evidence_kind="independent_verification",
    )
    qa_write["payload"] = {
        "summary": (
            "Independent QA failed the worker commit because legacy route-token "
            "refs cannot renew."
        )
    }

    qa = runtime.submit_line_write(
        record["contract_execution_id"],
        qa_write,
        actor_role="qa",
    )
    assert qa["ok"] is True

    guide = runtime.current_guide(record["contract_execution_id"], actor_role="observer")
    assert guide["next_legal_action"]["line_id"] == "worker_implementation"
    assert guide["next_legal_action"]["owner_role"] == "mf_sub"


def test_mf_parallel_failed_qa_retry_reuses_same_context_setup_lines(tmp_path):
    _write_contract_definition(
        tmp_path,
        contract_id="mf_parallel.v1",
        stages=[
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
            },
            {
                "stage_id": "worker_startup",
                "lines": [
                    {
                        "line_id": "worker_startup",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "mf_subagent_startup",
                    }
                ],
            },
            {
                "stage_id": "worker_context",
                "lines": [
                    {
                        "line_id": "worker_graph_context",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "graph_trace",
                    }
                ],
            },
            {
                "stage_id": "worker_implementation",
                "lines": [
                    {
                        "line_id": "worker_implementation",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "implementation",
                    }
                ],
            },
            {
                "stage_id": "worker_finish",
                "lines": [
                    {
                        "line_id": "worker_finish_gate",
                        "owner_role": "mf_sub",
                        "allowed_writer_roles": ["mf_sub"],
                        "evidence_kind": "mf_subagent_finish_gate",
                    }
                ],
            },
            {
                "stage_id": "qa",
                "lines": [
                    {
                        "line_id": "qa_independent_verification",
                        "owner_role": "qa",
                        "allowed_writer_roles": ["qa"],
                        "evidence_kind": "independent_verification",
                    }
                ],
            },
            {
                "stage_id": "observer_integration",
                "lines": [
                    {
                        "line_id": "observer_close_ready",
                        "owner_role": "observer",
                        "allowed_writer_roles": ["observer"],
                        "evidence_kind": "close_ready",
                    }
                ],
            },
        ],
    )
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
    )
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-FAILED-QA-RETRY-CONTEXT",
        contract_execution_id="cex-mf-parallel-failed-qa-retry-context",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-failed-qa-retry-context",
    )
    context_id = "mfrctx-same-worker"
    instance_id = f"runtime_context:{context_id}"

    def line(stage, line_id, role, evidence, *, payload=None):
        payload = dict(payload or {})
        payload.setdefault("runtime_context_id", context_id)
        return {
            "stage_id": stage,
            "line_id": line_id,
            "actor_role": role,
            "evidence_kind": evidence,
            "line_instance_id": instance_id,
            "runtime_context_id": context_id,
            "payload": payload,
        }

    completed_lines = [
        line(
            "worker_read",
            "worker_read_runtime_guide",
            "mf_sub",
            "read_receipt",
            payload={"schema_version": "contract_context_read_receipt.v1"},
        ),
        line("worker_startup", "worker_startup", "mf_sub", "mf_subagent_startup"),
        line("worker_context", "worker_graph_context", "mf_sub", "graph_trace"),
        line("worker_implementation", "worker_implementation", "mf_sub", "implementation"),
        line(
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
            payload={"summary": "Independent QA failed the worker commit."},
        ),
        line(
            "worker_implementation",
            "worker_implementation",
            "mf_sub",
            "implementation",
            payload={
                "schema_version": "mf_sub.worker_implementation.v1",
                "summary": "Retry implementation fixed the QA blocker.",
            },
        ),
        line(
            "worker_finish",
            "worker_finish_gate",
            "mf_sub",
            "mf_subagent_finish_gate",
            payload={"schema_version": "mf_sub.finish_gate.v1"},
        ),
        line(
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
            payload={
                "schema_version": "qa_independent_verification.retry.v1",
                "qa_result": "pass",
                "pass": True,
            },
        ),
        {
            "stage_id": "observer_integration",
            "line_id": "observer_close_ready",
            "actor_role": "observer",
            "evidence_kind": "close_ready",
            "payload": {"schema_version": "observer_close_ready.retry.v1"},
        },
    ]

    projected = runtime.projected_record(
        record["contract_execution_id"],
        actor_role="observer",
        completed_lines=completed_lines,
    )
    assert projected["runtime_guide"]["next_legal_action"] is None


def test_direct_fix_qa_evidence_preserves_owner_submitter_provenance(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-PROVENANCE",
    )
    write = _direct_fix_qa_write(
        direct_fix,
        generation=generation,
        repair_ref=repair_ref,
    )
    write.update(
        {
            "actor_session_principal": "qa:curie",
            "evidence_owner_actor": "qa:curie",
            "evidence_owner_session_ref": "qstok-curie",
            "submitter_session": "obs-parent-materializer",
            "submitter_principal": "observer:parent",
            "materialized_from": "qa_packet:qapkt-curie",
            "authorization_source": "qa_session_token_ref",
            "qa_session_token_ref": "qstok-curie",
        }
    )

    result = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        write,
        actor_role="qa",
    )

    assert result["ok"] is True
    line = result["record"]["completed_lines"][-1]
    assert line["actor_role"] == "qa"
    assert line["evidence_owner_role"] == "qa"
    assert line["evidence_owner_actor"] == "qa:curie"
    assert line["submitter_session"] == "obs-parent-materializer"
    assert line["submitter_principal"] == "observer:parent"
    assert line["materialized_from"] == "qa_packet:qapkt-curie"
    assert line["authorization_source"] == "qa_session_token_ref"
    assert line["observer_impersonation"] is False
    assert line["qa_evidence_provenance"]["evidence_owner_actor"] == "qa:curie"
    assert line["qa_evidence_provenance"]["submitter_principal"] == "observer:parent"


def test_direct_fix_qa_materialization_defaults_do_not_make_observer_owner(tmp_path):
    _write_chain_projection_contracts(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    _, direct_fix, generation, repair_ref = _start_repaired_direct_fix(
        runtime,
        backlog_id="AC-DIRECT-FIX-QA-MATERIALIZER-NOT-OWNER",
    )
    write = _direct_fix_qa_write(
        direct_fix,
        generation=generation,
        repair_ref=repair_ref,
    )
    write.update(
        {
            "submitter_session": "obs-parent-materializer",
            "submitter_principal": "observer:parent",
            "materialized_from": "qa_packet:qapkt-unowned",
        }
    )

    result = runtime.submit_line_write(
        direct_fix["contract_execution_id"],
        write,
        actor_role="qa",
    )

    assert result["ok"] is True
    line = result["record"]["completed_lines"][-1]
    assert line["actor_role"] == "qa"
    assert line["evidence_owner_role"] == "qa"
    assert line["evidence_owner_actor"] == "qa"
    assert line["submitter_principal"] == "observer:parent"
    assert line["qa_evidence_provenance"]["evidence_owner_actor"] == "qa"
    assert line["qa_evidence_provenance"]["submitter_principal"] == "observer:parent"


def test_upsert_contract_chain_successor_binding_rejects_lineage_mismatch(tmp_path):
    _write_minimal_contract(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(conn),
    )
    root = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-LINEAGE-MISMATCH",
        contract_execution_id="cex-lineage-root",
        actor_role="observer",
    )
    child = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MAPPING-LINEAGE-MISMATCH",
        contract_execution_id="cex-lineage-child",
        actor_role="observer",
        parent_contract_execution_id=root["contract_execution_id"],
        root_contract_execution_id=root["root_contract_execution_id"],
        contract_chain_id=root["contract_chain_id"],
    )

    cases = [
        ("parent_contract_execution_id", "cex-other-parent", "parent_contract"),
        ("root_contract_execution_id", "cex-other-root", "root_contract"),
        ("contract_chain_id", "cchain-other", "contract_chain"),
    ]
    for field, value, message in cases:
        bad_child = dict(child)
        bad_child[field] = value
        with pytest.raises(ContractRuntimeError, match=message):
            upsert_contract_chain_successor_binding(
                conn,
                parent_record=root,
                child_record=bad_child,
            )


def test_deprecated_definition_replays_but_cannot_start_new_execution(tmp_path):
    _write_minimal_contract(tmp_path)
    registry = ContractDefinitionRegistry(tmp_path)
    runtime = ContractRuntime(registry, instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )

    registry.deprecate_definition(
        "observer_onboard",
        version="v1",
        revision="rev1",
        reason="test deprecation",
    )

    with pytest.raises(ContractRuntimeError, match="cannot start new executions"):
        runtime.start_execution(
            "observer_onboard",
            project_id="aming-claw",
            backlog_id="AC-MIN-PATH-2",
            actor_role="observer",
        )

    replay_guide = runtime.current_guide(record["contract_execution_id"])
    assert replay_guide["contract"]["contract_id"] == "observer_onboard"


def test_runtime_raises_structured_stale_pinned_execution_error(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )
    record["definition_hash"] = "sha256:stale-pinned-definition"
    runtime.store.update(record["contract_execution_id"], record)

    with pytest.raises(StalePinnedContractExecutionError) as exc:
        runtime.current_guide(record["contract_execution_id"], actor_role="observer")

    error = exc.value.to_dict()
    assert error["field"] == "definition_hash"
    assert error["contract_execution_id"] == record["contract_execution_id"]
    assert error["pinned_definition_hash"] == "sha256:stale-pinned-definition"
    assert error["current_definition_hash"].startswith("sha256:")
