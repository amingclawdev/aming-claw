from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from agent.governance.contract_template_registry import (
    get_contract_template,
    list_contract_templates,
)
from agent.governance.contracts import (
    ContractCrudService,
    ContractDefinitionRegistry,
    ContractRuntime,
    run_contract_runtime_precheck,
)
from agent.governance.contracts.runtime import (
    ContractRetirementError,
    ContractRuntimeError,
    MF_PARALLEL_ATOMIC_LANE_WRITER_BINDING_FIELDS,
    SQLiteContractExecutionStore,
    StalePinnedContractExecutionError,
    mf_parallel_precommit_correction_writer_safe_copy,
    read_backlog_contract_chain_current,
)
from agent.governance.contracts.hash import stable_sha256
from agent.governance.server import (
    _contract_runtime_precommit_correction_intent_requested,
    _contract_runtime_direct_fix_close_authority_gate,
    _contract_runtime_mf_parallel_concurrent_sibling_writer_rebase,
    _contract_runtime_writer_line_guide_hash,
)


_SYSTEM_LAYER_POLICY_NAMES = [
    "entrypoint_policy",
    "successor_policy",
    "write_authority_policy",
    "next_action_policy",
    "projection_policy",
    "route_policy",
    "authority_policy",
    "graph_binding_policy",
]


def _definition(**overrides):
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": "observer_hotfix",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "implementation",
        "status": "active",
        "compat_aliases": ["observer_hotfix_direct_mutation.v1"],
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "pre_mutation",
                    "lines": [
                        {
                            "line_id": "reason",
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
                            "line_id": "independent_qa",
                            "owner_role": "qa",
                            "allowed_writer_roles": ["qa"],
                            "evidence_kind": "qa_verification",
                        }
                    ],
                },
            ]
        },
        "instruction_layer": {"inline": ["Use runtime guide only."], "refs": []},
        "successors": [{"contract_id": "qa_onboard", "version": "v1"}],
    }
    payload.update(overrides)
    return payload


def _assert_explicit_system_layer(definition, *, allow_root_start: bool):
    read_model = definition["read_model"]
    assert read_model["system_layer_policy_status"] == {
        "schema_version": "contract_system_layer_policy_status.v1",
        "status": "explicit",
        "explicit": True,
        "defaulted": False,
        "deny_by_default": True,
        "missing_policies": [],
        "defaulted_policies": [],
        "explicit_policies": _SYSTEM_LAYER_POLICY_NAMES,
    }
    system_layer = read_model["system_layer"]
    assert system_layer["entrypoint_policy"]["policy_status"] == "explicit"
    assert system_layer["entrypoint_policy"]["allow_root_start"] is allow_root_start
    assert system_layer["route_policy"]["route_token_ref_required"] is True
    assert system_layer["projection_policy"]["mutable_completed_lines_trust_root"] is False


def _runtime_write_from(record, *, actor_role: str, stage_id: str, line_id: str):
    state = record["execution_state"]
    guide = record["runtime_guide"]
    next_action = guide.get("next_legal_action") or {}
    write = {
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
        "evidence_kind": next_action.get("evidence_kind") or "",
    }
    if actor_role == "qa" and line_id == "qa_independent_verification":
        write["status"] = "passed"
    return write


def _runtime_next_write(record, *, actor_role: str):
    next_action = record["runtime_guide"].get("next_legal_action") or {}
    return _runtime_write_from(
        record,
        actor_role=actor_role,
        stage_id=next_action["stage_id"],
        line_id=next_action["line_id"],
    )


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


def _direct_fix_graph_write_identity(actor_role: str) -> dict:
    if actor_role != "mf_sub":
        return {}
    return {
        "runtime_context_id": "mfrctx-test",
        "task_id": "direct-fix-worker-test",
        "parent_task_id": "cex-direct-fix-parent-test",
    }


def _is_direct_fix_graph_checkpoint(write: dict) -> bool:
    return str(write.get("line_id") or "") in {
        "direct_fix_observer_graph_scope",
        "direct_fix_worker_graph_context",
        "direct_fix_qa_graph_context",
    }


def _submit_next_runtime_line(
    runtime: ContractRuntime,
    contract_execution_id: str,
    *,
    actor_role: str,
    **overrides,
):
    runtime.current_guide(contract_execution_id, actor_role=actor_role)
    record = runtime.store.get(contract_execution_id)
    write = _runtime_next_write(record, actor_role=actor_role)
    if _is_direct_fix_graph_checkpoint(write):
        trace_id = f"gqt-{actor_role}-{contract_execution_id}"
        write.update(_direct_fix_graph_write_identity(actor_role))
        write["payload"] = _direct_fix_graph_payload(
            actor_role=actor_role,
            trace_id=trace_id,
        )
        graph_result = runtime.submit_line_write(contract_execution_id, write)
        if not graph_result["ok"]:
            return graph_result
        runtime.current_guide(contract_execution_id, actor_role=actor_role)
        record = runtime.store.get(contract_execution_id)
        write = _runtime_next_write(record, actor_role=actor_role)
    write.update(overrides)
    return runtime.submit_line_write(contract_execution_id, write)


def _start_mf_parallel_successor(
    runtime: ContractRuntime,
    *,
    project_id: str,
    backlog_id: str,
    contract_execution_id: str,
    route_token_ref: str,
) -> dict:
    parent_execution_id = f"cex-onboard-parent-for-{contract_execution_id}"
    chain_id = f"cchain-for-{contract_execution_id}"
    runtime.start_execution(
        "onboard_contract",
        project_id=project_id,
        backlog_id=backlog_id,
        contract_execution_id=parent_execution_id,
        actor_role="observer",
        route_token_ref=route_token_ref,
        contract_chain_id=chain_id,
    )
    return runtime.start_execution(
        "mf_parallel.v1",
        project_id=project_id,
        backlog_id=backlog_id,
        contract_execution_id=contract_execution_id,
        actor_role="observer",
        parent_contract_execution_id=parent_execution_id,
        root_contract_execution_id=parent_execution_id,
        contract_chain_id=chain_id,
        route_token_ref=route_token_ref,
    )


def _start_direct_fix_successor(
    runtime: ContractRuntime,
    *,
    project_id: str,
    backlog_id: str,
    contract_execution_id: str,
    route_token_ref: str,
) -> dict:
    parent_execution_id = f"cex-onboard-parent-for-{contract_execution_id}"
    chain_id = f"cchain-for-{contract_execution_id}"
    runtime.start_execution(
        "onboard_contract",
        project_id=project_id,
        backlog_id=backlog_id,
        contract_execution_id=parent_execution_id,
        actor_role="observer",
        route_token_ref=route_token_ref,
        contract_chain_id=chain_id,
    )
    return runtime.start_execution(
        "direct_fix.v1",
        project_id=project_id,
        backlog_id=backlog_id,
        contract_execution_id=contract_execution_id,
        actor_role="observer",
        parent_contract_execution_id=parent_execution_id,
        root_contract_execution_id=parent_execution_id,
        contract_chain_id=chain_id,
        route_token_ref=route_token_ref,
    )


def test_contract_runtime_effective_actor_role_accepts_qa_session(monkeypatch):
    from agent.governance import server as server_module

    class QaContext:
        body = {}

        def get_project_id(self):
            return "proj"

        def require_auth(self, conn):
            return {
                "session_id": "qa-session",
                "principal_id": "qa:test",
                "project_id": "proj",
                "role": "qa",
            }

    def fail_if_called(*args, **kwargs):
        raise AssertionError("QA role should not be downgraded to observer or mf_sub proof")

    monkeypatch.setattr(
        server_module,
        "_resolve_contract_runtime_mf_sub_proof",
        fail_if_called,
    )
    monkeypatch.setattr(
        server_module,
        "_resolve_contract_runtime_observer_proof",
        fail_if_called,
    )

    assert (
        server_module._contract_runtime_effective_actor_role(
            QaContext(),
            object(),
            action="contract_runtime_submit_line",
            backlog_id="BUG-QA",
            contract_execution_id="cex-qa",
            record={"contract_execution_id": "cex-qa"},
        )
        == "qa"
    )


def test_crud_service_lists_reads_and_validates_definitions(tmp_path: Path):
    service = ContractCrudService(tmp_path)

    empty = service.list()
    assert empty["ok"] is True
    assert empty["data"] == {"definitions": [], "count": 0}

    validated = service.validate(_definition())
    assert validated["ok"] is True
    assert validated["operation"] == "validate"
    definition = validated["data"]["definition"]
    assert definition["definition_hash"].startswith("sha256:")
    read_model = definition["read_model"]
    assert read_model["role"] == "observer"
    assert read_model["contract_type"] == "implementation"
    assert read_model["system_layer_policy_status"]["status"] == "legacy_default_deny"
    assert read_model["system_layer"]["entrypoint_policy"]["allowed"] is False
    assert read_model["compat_aliases"] == ["observer_hotfix_direct_mutation.v1"]
    assert read_model["successors"] == [{"contract_id": "qa_onboard", "version": "v1"}]
    assert read_model["allowed_writer_roles"] == ["observer", "qa"]
    assert read_model["rule_lines"] == [
        {
            "stage_id": "pre_mutation",
            "stage_description": "",
            "line_id": "reason",
            "owner_role": "observer",
            "allowed_writer_roles": ["observer"],
            "evidence_kind": "contract_state_changed",
            "required": True,
            "description": "",
        },
        {
            "stage_id": "qa",
            "stage_description": "",
            "line_id": "independent_qa",
            "owner_role": "qa",
            "allowed_writer_roles": ["qa"],
            "evidence_kind": "qa_verification",
            "required": True,
            "description": "",
        },
    ]
    assert read_model["required_evidence"] == [
        {
            "stage_id": "pre_mutation",
            "line_id": "reason",
            "owner_role": "observer",
            "allowed_writer_roles": ["observer"],
            "evidence_kind": "contract_state_changed",
            "required": True,
        },
        {
            "stage_id": "qa",
            "line_id": "independent_qa",
            "owner_role": "qa",
            "allowed_writer_roles": ["qa"],
            "evidence_kind": "qa_verification",
            "required": True,
        },
    ]

    created = service.create(_definition())
    assert created["ok"] is True
    assert created["status"] == "created"
    assert Path(created["data"]["path"]).exists()

    listed = service.list()
    assert listed["data"]["count"] == 1
    assert listed["data"]["definitions"][0]["contract_id"] == "observer_hotfix"
    assert listed["data"]["definitions"][0]["source_sha256"].startswith("sha256:")
    load_record = listed["data"]["definitions"][0]["definition_load_record"]
    assert load_record["source_sha256"] == listed["data"]["definitions"][0]["source_sha256"]
    assert load_record["definition_hash"] == listed["data"]["definitions"][0]["definition_hash"]
    assert load_record["drift_status"] == "current"

    by_alias = service.read("observer_hotfix_direct_mutation.v1")
    assert by_alias["ok"] is True
    assert by_alias["data"]["definition"]["contract_id"] == "observer_hotfix"


def test_crud_service_preserves_graph_only_envelope_across_all_lifecycle_reads(
    tmp_path: Path,
):
    service = ContractCrudService(tmp_path)
    envelope = {
        "schema_version": "governance_hints.v1",
        "asset_binding_events": [{
            "schema_version": "asset_binding_event.v1",
            "operation": "bind",
            "path": ".",
            "role": "config",
            "target_module": "agent.governance.contracts.registry",
        }],
    }
    created = service.create(_definition(governance_hints=envelope))
    definition_hash = created["data"]["definition"]["definition_hash"]
    updated_envelope = {
        **envelope,
        "asset_binding_events": [{
            **envelope["asset_binding_events"][0],
            "operation": "unbind",
        }],
    }

    updated = service.update(
        _definition(governance_hints=updated_envelope),
        expected_previous_hash=definition_hash,
    )
    read = service.read("observer_hotfix")
    listed = service.list()
    deprecated = service.deprecate(
        "observer_hotfix",
        version="v1",
        revision="rev1",
        reason="lifecycle preservation",
    )

    assert updated["ok"] is True
    assert updated["data"]["definition"]["definition_hash"] == definition_hash
    assert read["data"]["definition"]["governance_hints"] == updated_envelope
    assert listed["data"]["definitions"][0]["governance_hints"] == updated_envelope
    assert deprecated["data"]["definition"]["governance_hints"] == updated_envelope
    assert "governance_hints" not in read["data"]["definition"]["read_model"]


def test_crud_service_preserves_registry_lifecycle_rules(tmp_path: Path):
    service = ContractCrudService(tmp_path)
    created = service.create(_definition())
    existing_hash = created["data"]["definition"]["definition_hash"]

    changed_same_revision = _definition(
        rule_layer={
            "stages": [
                {
                    "stage_id": "pre_mutation",
                    "lines": [
                        {
                            "line_id": "reason",
                            "owner_role": "observer",
                            "allowed_writer_roles": ["observer"],
                            "description": "semantic change",
                        }
                    ],
                }
            ]
        }
    )
    blocked_update = service.update(
        changed_same_revision,
        expected_previous_hash=existing_hash,
    )
    assert blocked_update["ok"] is False
    assert blocked_update["error"]["type"] == "ContractLifecycleError"
    assert "new revision" in blocked_update["error"]["message"]

    updated = service.update(_definition(revision="rev2"))
    assert updated["ok"] is True
    assert updated["data"]["definition"]["revision"] == "rev2"

    deprecated = service.deprecate(
        "observer_hotfix",
        version="v1",
        revision="rev1",
        reason="replaced by rev2",
    )
    assert deprecated["ok"] is True
    assert deprecated["data"]["definition"]["status"] == "deprecated"

    active_only = service.list(include_deprecated=False)
    assert [item["revision"] for item in active_only["data"]["definitions"]] == ["rev2"]

    hash_mismatch = service.update(
        _definition(revision="rev2"),
        expected_previous_hash="sha256:" + "0" * 64,
    )
    assert hash_mismatch["ok"] is False
    assert hash_mismatch["error"]["type"] == "ContractLifecycleError"
    assert "expected_previous_hash mismatch" in hash_mismatch["error"]["message"]

    blocked_delete = service.hard_delete(
        "observer_hotfix",
        version="v1",
        revision="rev1",
        references=["timeline:1"],
    )
    assert blocked_delete["ok"] is False
    assert blocked_delete["error"]["type"] == "ContractLifecycleError"
    assert "hard delete" in blocked_delete["error"]["message"]

    draft = service.create(_definition(revision="rev3", status="draft"))
    deleted = service.delete(
        "observer_hotfix",
        version="v1",
        revision="rev3",
        references=["draft-ref-ok"],
    )
    assert deleted["ok"] is True
    assert deleted["operation"] == "delete"
    assert deleted["data"]["deleted"] == {
        "contract_id": "observer_hotfix",
        "version": "v1",
        "revision": "rev3",
        "status": "draft",
    }
    assert not Path(draft["data"]["path"]).exists()


def test_registry_resolves_latest_active_revision_and_runtime_pins_source_hash(
    tmp_path: Path,
):
    service = ContractCrudService(tmp_path)
    created = service.create(_definition())
    rev1_hash = created["data"]["definition"]["definition_hash"]

    rev2 = service.update(
        _definition(
            revision="rev2",
            metadata={"revision_reason": "add legal successor graph edge"},
        ),
        expected_previous_hash=None,
    )
    assert rev2["ok"] is True

    latest = service.read("observer_hotfix_direct_mutation.v1")
    assert latest["ok"] is True
    assert latest["data"]["definition"]["revision"] == "rev2"

    explicit_rev1 = service.read(
        "observer_hotfix_direct_mutation.v1",
        revision="rev1",
    )
    assert explicit_rev1["ok"] is True
    assert explicit_rev1["data"]["definition"]["definition_hash"] == rev1_hash

    runtime = ContractRuntime(service.registry)
    old_record = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-UPDATE-SOURCE-HASH",
        contract_execution_id="cex-observer-hotfix-rev1-pinned",
        actor_role="observer",
        revision="rev1",
        route_token_ref="rtok-source-hash",
    )
    assert old_record["revision"] == "rev1"
    assert old_record["definition_hash"] == rev1_hash
    assert old_record["definition_source_sha256"].startswith("sha256:")

    latest_record = runtime.start_execution(
        "observer_hotfix_direct_mutation.v1",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-UPDATE-LATEST-REVISION",
        contract_execution_id="cex-observer-hotfix-latest-revision",
        actor_role="observer",
        route_token_ref="rtok-latest-revision",
    )
    assert latest_record["revision"] == "rev2"
    assert latest_record["definition_source_sha256"].startswith("sha256:")

    source_path = Path(explicit_rev1["data"]["definition"]["_source_path"])
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(StalePinnedContractExecutionError) as exc:
        runtime.current_guide(
            "cex-observer-hotfix-rev1-pinned",
            actor_role="observer",
        )
    assert exc.value.field == "definition_source_sha256"


def test_envelope_only_source_change_keeps_active_runtime_projection_current(
    tmp_path: Path,
):
    envelope = {
        "schema_version": "governance_hints.v1",
        "asset_binding_events": [{
            "schema_version": "asset_binding_event.v1",
            "operation": "bind",
            "path": ".",
            "role": "config",
            "target_module": "agent.governance.contracts.registry",
        }],
    }
    service = ContractCrudService(tmp_path)
    created = service.create(_definition(governance_hints=envelope))
    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-ENVELOPE-ONLY-RUNTIME",
        contract_execution_id="cex-envelope-only-runtime",
        actor_role="observer",
        route_token_ref="rtok-envelope-only-runtime",
    )
    original_guide = record["runtime_guide"]
    source_path = Path(created["data"]["path"])
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    payload["governance_hints"]["asset_binding_events"].append({
        **envelope["asset_binding_events"][0],
        "operation": "unbind",
    })
    source_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    refreshed_guide = runtime.current_guide(
        "cex-envelope-only-runtime",
        actor_role="observer",
    )
    refreshed_definition = service.registry.get("observer_hotfix")

    assert record["definition_source_sha256"] != refreshed_definition["source_sha256"]
    assert record["definition_governance_hints_sha256"] != (
        refreshed_definition["governance_hints_sha256"]
    )
    assert refreshed_definition["definition_hash"] == record["definition_hash"]
    assert refreshed_guide["runtime_guide_hash"] == original_guide["runtime_guide_hash"]
    assert refreshed_guide["next_legal_action"] == original_guide["next_legal_action"]


def test_historical_runtime_without_envelope_hash_accepts_only_root_envelope_change(
    tmp_path: Path,
):
    service = ContractCrudService(tmp_path)
    created = service.create(_definition())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        service.registry,
        store=SQLiteContractExecutionStore(conn),
    )
    execution_id = "cex-historical-envelope-only-runtime"
    record = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-HISTORICAL-ENVELOPE-ONLY-RUNTIME",
        contract_execution_id=execution_id,
        actor_role="observer",
        route_token_ref="rtok-historical-envelope-only-runtime",
    )
    original_guide = record["runtime_guide"]
    historical_record = runtime.store.get(execution_id)
    historical_record.pop("definition_governance_hints_sha256")
    runtime.store.update(execution_id, historical_record)

    source_path = Path(created["data"]["path"])
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    payload["governance_hints"] = {
        "schema_version": "governance_hints.v1",
        "asset_binding_events": [{
            "schema_version": "asset_binding_event.v1",
            "operation": "bind",
            "path": ".",
            "role": "config",
            "target_module": "agent.governance.contracts.registry",
        }],
    }
    source_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    envelope_definition = service.registry.get("observer_hotfix")
    assert envelope_definition["definition_hash"] == record["definition_hash"]
    assert envelope_definition["source_sha256"] != record["definition_source_sha256"]
    refreshed_guide = runtime.current_guide(execution_id, actor_role="observer")
    assert refreshed_guide["runtime_guide_hash"] == original_guide["runtime_guide_hash"]
    assert refreshed_guide["next_legal_action"] == original_guide["next_legal_action"]

    payload["instruction_layer"]["inline"] = ["Business behavior changed."]
    source_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    changed_definition = service.registry.get("observer_hotfix")
    assert changed_definition["definition_hash"] != record["definition_hash"]
    with pytest.raises(StalePinnedContractExecutionError) as exc:
        runtime.current_guide(execution_id, actor_role="observer")
    assert exc.value.field == "definition_hash"


def test_direct_source_edit_warns_without_blocking_runtime_until_contract_update(
    tmp_path: Path,
):
    try:
        subprocess.run(
            ["git", "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git binary is required for source-control integrity warning")

    service = ContractCrudService(tmp_path)
    created = service.create(_definition())
    source_path = Path(created["data"]["path"])

    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "contracts@example.test"],
        ["git", "config", "user.name", "Contract Tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "baseline contract source"],
    ):
        subprocess.run(
            args,
            cwd=tmp_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    payload["instruction_layer"]["inline"].append(
        "Direct source edit without contract_update."
    )
    source_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    read = service.read("observer_hotfix")
    assert read["ok"] is True
    definition = read["data"]["definition"]
    integrity = definition["source_control_integrity"]
    assert integrity["drift_status"] == "source_control_drift"
    assert integrity["status"] == "changed_since_head"
    assert integrity["severity"] == "warning"
    assert integrity["requires_contract_update"] is True
    assert integrity["legal_status"] == "illegal_without_contract_update"
    assert integrity["gate_enforcement"] == (
        "warn_only_until_contract_update_runtime_exists"
    )
    assert integrity["blocks_runtime"] is False
    assert integrity["git_head_source_sha256"].startswith("sha256:")
    assert integrity["git_head_source_sha256"] != definition["source_sha256"]

    load_record = definition["definition_load_record"]
    assert load_record["drift_status"] == "source_control_drift"
    assert load_record["next_operator_action"] == (
        "run_contract_update_or_revert_direct_source_edit"
    )
    assert load_record["source_control_integrity"]["blocks_runtime"] is False
    assert definition["read_model"]["source_control_integrity"] == integrity

    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-SOURCE-DRIFT-WARN-ONLY",
        contract_execution_id="cex-source-drift-warn-only",
        actor_role="observer",
        route_token_ref="rtok-source-drift-warning",
    )
    assert record["contract_execution_id"] == "cex-source-drift-warn-only"
    assert record["definition_source_sha256"] == definition["source_sha256"]


def test_envelope_only_source_drift_routes_to_graph_reconcile_not_contract_update(
    tmp_path: Path,
):
    envelope = {
        "schema_version": "governance_hints.v1",
        "asset_binding_events": [{
            "schema_version": "asset_binding_event.v1",
            "operation": "bind",
            "path": ".",
            "role": "config",
            "target_module": "agent.governance.contracts.registry",
        }],
    }
    service = ContractCrudService(tmp_path)
    created = service.create(_definition(governance_hints=envelope))
    source_path = Path(created["data"]["path"])
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "contracts@example.test"],
        ["git", "config", "user.name", "Contract Tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "baseline contract source"],
    ):
        subprocess.run(
            args,
            cwd=tmp_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    payload["governance_hints"]["asset_binding_events"].append({
        **envelope["asset_binding_events"][0],
        "operation": "unbind",
    })
    source_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    definition = service.read("observer_hotfix")["data"]["definition"]
    integrity = definition["source_control_integrity"]

    assert integrity["status"] == "governance_hints_changed"
    assert integrity["drift_status"] == "graph_source_metadata_changed"
    assert integrity["requires_contract_update"] is False
    assert integrity["legal_status"] == "legal"
    assert integrity["blocks_runtime"] is False
    assert integrity["next_operator_action"] == "run_graph_reconcile"


def test_crud_service_returns_structured_failure_results(tmp_path: Path):
    service = ContractCrudService(tmp_path)

    missing = service.read("missing")
    assert missing == {
        "ok": False,
        "operation": "read",
        "status": "failed",
        "data": {},
        "error": {
            "type": "UnknownContractDefinitionError",
            "message": "unknown contract definition: missing",
        },
    }

    invalid = service.validate({"schema_version": "contract_definition.v1"})
    assert invalid["ok"] is False
    assert invalid["operation"] == "validate"
    assert invalid["error"]["type"] == "ContractDefinitionError"
    assert "contract_id" in invalid["error"]["message"]

    assert service.create(_definition())["ok"] is True
    duplicate = service.create(_definition())
    assert duplicate["ok"] is False
    assert duplicate["operation"] == "create"
    assert duplicate["error"]["type"] == "ContractLifecycleError"


def test_crud_service_accepts_registry_injection(tmp_path: Path):
    registry = ContractDefinitionRegistry(tmp_path)
    service = ContractCrudService(registry=registry)

    assert service.create(_definition())["ok"] is True
    assert service.read("observer_hotfix")["ok"] is True

    with pytest.raises(ValueError, match="either root or registry"):
        ContractCrudService(tmp_path, registry=registry)


def test_default_crud_service_uses_source_definition_root_without_legacy_cutover():
    service = ContractCrudService()

    listed = service.list(include_deprecated=False)
    assert listed["ok"] is True
    definitions = listed["data"]["definitions"]
    dogfood = next(
        item
        for item in definitions
        if item["contract_id"] == "contract_crud_runtime_integration"
    )
    source_path = Path(dogfood["_source_path"])
    assert source_path.parent.name == "contract_definitions"
    assert dogfood["definition_hash"].startswith("sha256:")
    assert dogfood["role"] == "observer"
    assert dogfood["compat_aliases"] == [
        "contract_crud_runtime_integration.v1",
        "contract_crud_runtime_min_path.v1",
    ]
    read_model = dogfood["read_model"]
    assert read_model["definition_hash"] == dogfood["definition_hash"]
    assert dogfood["source_sha256"].startswith("sha256:")
    assert dogfood["definition_load_record"]["definition_hash"] == dogfood["definition_hash"]
    assert read_model["definition_load_record"]["load_record_id"] == dogfood[
        "definition_load_record"
    ]["load_record_id"]
    assert read_model["system_layer_policy_status"]["deny_by_default"] is True
    assert read_model["successors"] == [
        {
            "contract_id": "contract_runtime_route_context_integration",
            "reason": (
                "Wire the source-backed contract read model into route-context "
                "runtime after the min path is accepted."
            ),
            "version": "v1",
        }
    ]
    assert read_model["allowed_writer_roles"] == ["observer", "mf_sub", "qa"]
    assert [
        (line["stage_id"], line["line_id"], line["owner_role"], line["evidence_kind"])
        for line in read_model["rule_lines"]
    ] == [
        (
            "context",
            "read_contract_definition_source",
            "observer",
            "route_context",
        ),
        ("startup", "worker_startup", "mf_sub", "mf_subagent_startup"),
        ("implementation", "crud_runtime_proof", "mf_sub", "implementation"),
        (
            "verification",
            "qa_contract_runtime_min_path",
            "qa",
            "independent_verification",
        ),
    ]
    assert [
        item["evidence_kind"] for item in read_model["required_evidence"]
    ] == [
        "route_context",
        "mf_subagent_startup",
        "implementation",
        "independent_verification",
    ]

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    assert "read_model" not in payload
    validated = service.validate(payload)
    assert validated["ok"] is True
    assert (
        validated["data"]["definition"]["definition_hash"]
        == dogfood["definition_hash"]
    )
    assert (
        service.read("contract_crud_runtime_min_path.v1")["data"]["definition"][
            "contract_id"
        ]
        == "contract_crud_runtime_integration"
    )

    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "contract_crud_runtime_integration",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-SYSTEM-CRUD-REGISTRY-MIN-PATH-20260623",
        contract_execution_id="cex-contract-crud-runtime-integration-test",
        actor_role="observer",
        route_token_ref="rtok-test",
    )
    assert record["runtime_guide"]["next_legal_action"] == {
        "stage_id": "context",
        "line_id": "read_contract_definition_source",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "evidence_kind": "route_context",
        "required": True,
    }
    assert (
        "Observer records route/source context"
        in record["runtime_guide"]["instructions"]["inline"][2]
    )

    legacy_template = get_contract_template("mf_workflow_runtime.v1")
    assert legacy_template["schema_version"] != "contract_definition.v1"
    legacy_ids = {template["template_id"] for template in list_contract_templates()}
    assert "mf_workflow_runtime.v1" in legacy_ids
    assert "contract_crud_runtime_integration" not in legacy_ids


def test_default_registry_exposes_mf_parallel_contract_definition_and_runtime_path():
    service = ContractCrudService()

    result = service.read("mf_parallel.v1")
    assert result["ok"] is True
    definition = result["data"]["definition"]
    assert definition["contract_id"] == "mf_parallel"
    assert definition["role"] == "observer"
    assert definition["contract_type"] == "parallel_worker"
    assert definition["compat_aliases"] == ["mf_parallel.v1", "parallel_worker.v1"]

    read_model = definition["read_model"]
    _assert_explicit_system_layer(definition, allow_root_start=False)
    successor_policy = definition["system_layer"]["successor_policy"]
    assert successor_policy["allow_successor_start"] is True
    assert successor_policy["allowed_parent_contracts"] == [
        {"contract_id": "onboard_contract", "version": "v1"},
        {"contract_id": "onboard_route_guide", "version": "service"},
    ]
    assert "observer_work_mode_transition" in successor_policy[
        "requires_handoff_evidence"
    ]
    assert read_model["allowed_writer_roles"] == ["observer", "mf_sub", "qa"]
    assert [
        (line["stage_id"], line["line_id"], line["owner_role"], line["evidence_kind"])
        for line in read_model["rule_lines"]
    ] == [
        (
            "orchestration",
            "observer_prefill_child_contracts",
            "observer",
            "contract_binding",
        ),
        (
            "dispatch",
            "observer_dispatch_bounded_workers",
            "observer",
            "dispatch_bounded_worker",
        ),
        ("worker_read", "worker_read_runtime_guide", "mf_sub", "read_receipt"),
        ("worker_startup", "worker_startup", "mf_sub", "mf_subagent_startup"),
        ("worker_context", "worker_graph_context", "mf_sub", "graph_trace"),
        (
            "worker_implementation",
            "worker_implementation",
            "mf_sub",
            "implementation",
        ),
        (
            "worker_attestation",
            "worker_finish_time_attestation",
            "mf_sub",
            "record_finish_time_worker_attestation",
        ),
        (
            "worker_finish",
            "worker_finish_gate",
            "mf_sub",
            "mf_subagent_finish_gate",
        ),
        ("qa_handoff", "worker_review_ready_handoff", "mf_sub", "review_ready"),
        (
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
        ),
        ("observer_integration", "observer_merge", "observer", "merge"),
        ("observer_integration", "observer_reconcile", "observer", "reconcile"),
        ("observer_integration", "observer_close_ready", "observer", "close_ready"),
    ]

    runtime = ContractRuntime(service.registry)
    with pytest.raises(ContractRuntimeError, match="contract_root_start_denied"):
        runtime.start_execution(
            "mf_parallel.v1",
            project_id="aming-claw",
            backlog_id="AC-CLAUDE-PARALLEL-CLOSE-RECONCILE-GUIDE-GAP-20260623",
            contract_execution_id="cex-mf-parallel-root-start-blocked-test",
            actor_role="observer",
            route_token_ref="rtok-test",
        )
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-CLAUDE-PARALLEL-CLOSE-RECONCILE-GUIDE-GAP-20260623",
        contract_execution_id="cex-mf-parallel-runtime-path-test",
        route_token_ref="rtok-test",
    )
    assert record["precheck_decision"]["schema_version"] == "contract_gate_decision.v1"
    assert record["precheck_decision"]["decision"] == "allow"
    assert record["parent_contract_execution_id"] == (
        "cex-onboard-parent-for-cex-mf-parallel-runtime-path-test"
    )
    assert record["runtime_guide"]["next_legal_action"] == {
        "stage_id": "orchestration",
        "line_id": "observer_prefill_child_contracts",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "evidence_kind": "contract_binding",
        "required": True,
    }
    assert (
        "mf_sub workers own read receipt"
        in record["runtime_guide"]["instructions"]["inline"][2]
    )

    record = runtime.submit_line_write(
        "cex-mf-parallel-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="orchestration",
            line_id="observer_prefill_child_contracts",
        ),
    )["record"]
    record = runtime.submit_line_write(
        "cex-mf-parallel-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="dispatch",
            line_id="observer_dispatch_bounded_workers",
        ),
    )["record"]

    rejected_observer_worker_evidence = runtime.submit_line_write(
        "cex-mf-parallel-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="worker_read",
            line_id="worker_read_runtime_guide",
        ),
    )
    assert rejected_observer_worker_evidence["ok"] is False
    assert "cannot write line" in rejected_observer_worker_evidence["decision"]["errors"][0]

    for stage_id, line_id in [
        ("worker_read", "worker_read_runtime_guide"),
        ("worker_startup", "worker_startup"),
        ("worker_context", "worker_graph_context"),
        ("worker_implementation", "worker_implementation"),
        ("worker_attestation", "worker_finish_time_attestation"),
        ("worker_finish", "worker_finish_gate"),
    ]:
        runtime.current_guide("cex-mf-parallel-runtime-path-test", actor_role="mf_sub")
        record = runtime.store.get("cex-mf-parallel-runtime-path-test")
        accepted = runtime.submit_line_write(
            "cex-mf-parallel-runtime-path-test",
            _runtime_write_from(
                record,
                actor_role="mf_sub",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
        record = accepted["record"]

    runtime.current_guide("cex-mf-parallel-runtime-path-test", actor_role="qa")
    record = runtime.store.get("cex-mf-parallel-runtime-path-test")
    rejected_qa_before_handoff = runtime.submit_line_write(
        "cex-mf-parallel-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )
    assert rejected_qa_before_handoff["ok"] is False
    assert any(
        "write does not match next legal action" in error
        for error in rejected_qa_before_handoff["decision"]["errors"]
    )

    runtime.current_guide("cex-mf-parallel-runtime-path-test", actor_role="mf_sub")
    record = runtime.store.get("cex-mf-parallel-runtime-path-test")
    accepted_handoff = runtime.submit_line_write(
        "cex-mf-parallel-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="mf_sub",
            stage_id="qa_handoff",
            line_id="worker_review_ready_handoff",
        ),
    )
    assert accepted_handoff["ok"] is True
    record = accepted_handoff["record"]

    runtime.current_guide("cex-mf-parallel-runtime-path-test", actor_role="observer")
    record = runtime.store.get("cex-mf-parallel-runtime-path-test")
    rejected_observer_qa_evidence = runtime.submit_line_write(
        "cex-mf-parallel-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )
    assert rejected_observer_qa_evidence["ok"] is False
    assert "cannot write line" in rejected_observer_qa_evidence["decision"]["errors"][0]

    runtime.current_guide("cex-mf-parallel-runtime-path-test", actor_role="qa")
    record = runtime.store.get("cex-mf-parallel-runtime-path-test")
    accepted_qa = runtime.submit_line_write(
        "cex-mf-parallel-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )
    assert accepted_qa["ok"] is True
    record = accepted_qa["record"]

    for stage_id, line_id in [
        ("observer_integration", "observer_merge"),
        ("observer_integration", "observer_reconcile"),
        ("observer_integration", "observer_close_ready"),
    ]:
        runtime.current_guide("cex-mf-parallel-runtime-path-test", actor_role="observer")
        record = runtime.store.get("cex-mf-parallel-runtime-path-test")
        accepted = runtime.submit_line_write(
            "cex-mf-parallel-runtime-path-test",
            _runtime_write_from(
                record,
                actor_role="observer",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
        record = accepted["record"]

    assert record["runtime_guide"]["next_legal_action"] is None


def test_contract_runtime_writer_role_safe_copy_payload_for_observer_to_mf_sub():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    execution_id = "cex-mf-parallel-writer-role-copy-observer-worker-test"
    _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-WRITER-ROLE-GUIDE-HASH-MISMATCH-20260701",
        contract_execution_id=execution_id,
        route_token_ref="rtok-writer-role-copy",
    )
    _submit_next_runtime_line(runtime, execution_id, actor_role="observer")
    _submit_next_runtime_line(runtime, execution_id, actor_role="observer")

    observer_guide = runtime.current_guide(execution_id, actor_role="observer")
    copy_payload = observer_guide["writer_role_safe_copy_payload"]["copy_payload"]
    assert observer_guide["next_legal_action"]["line_id"] == "worker_read_runtime_guide"
    assert copy_payload["actor_role"] == "mf_sub"
    assert copy_payload["runtime_guide_hash"] != observer_guide["runtime_guide_hash"]

    stale_reader_payload = dict(copy_payload)
    stale_reader_payload["runtime_guide_hash"] = observer_guide["runtime_guide_hash"]
    rejected = runtime.submit_line_write(execution_id, stale_reader_payload)

    assert rejected["ok"] is False
    mismatch = "\n".join(rejected["decision"]["errors"])
    assert "reader-role guide hash for role 'observer'" in mismatch
    assert "owner/writer-role guide hash for role 'mf_sub'" in mismatch
    assert "writer_role_safe_copy_payload.copy_payload.runtime_guide_hash" in mismatch

    accepted = runtime.submit_line_write(execution_id, copy_payload)
    assert accepted["ok"] is True
    assert accepted["record"]["completed_lines"][-1]["actor_role"] == "mf_sub"


def test_precommit_correction_rebinds_exact_current_atomic_lane_writer_copy():
    runtime_context_id = "mfrctx-precommit-writer-binding"
    contract_execution_id = "cex-precommit-writer-binding"
    current_hash = stable_sha256({"line": "worker_commit", "revision": 17})
    current_copy = {
        "project_id": "aming-claw",
        "backlog_id": "AC-PRECOMMIT-WRITER-BINDING",
        "contract_execution_id": contract_execution_id,
        "definition_hash": stable_sha256({"definition": "mf_parallel.v2"}),
        "instruction_bundle_hash": stable_sha256({"instructions": "worker"}),
        "execution_state_revision": 17,
        "runtime_guide_hash": current_hash,
        "stage_id": "worker_commit",
        "line_id": "worker_commit",
        "actor_role": "mf_sub",
        "evidence_kind": "worker_commit",
        "line_instance_id": f"runtime_context:{runtime_context_id}",
        "runtime_context_id": runtime_context_id,
        "task_id": "task-precommit-writer-binding",
        "parent_task_id": "parent-precommit-writer-binding",
        "worker_role": "mf_sub",
        "lane_id": "source",
        "worker_slot_id": "source",
        "worker_id": "source-repair",
    }
    source = {
        "stage_id": "worker_commit",
        "line_id": "worker_commit",
        "evidence_kind": "worker_commit",
        "writer_role_safe_copy_payload": {
            "schema_version": "contract_runtime.writer_role_safe_copy_payload.v1",
            "copy_payload": dict(current_copy),
            "hash_alignment": {
                "required_writer_runtime_guide_hash": current_hash,
            },
        },
    }
    intent = {
        "schema_version": (
            "runtime_context.precommit_implementation_correction_intent.v1"
        ),
        "action": "revise_precommit_worker_implementation",
        "contract_execution_id": contract_execution_id,
        "runtime_context_id": runtime_context_id,
        "task_id": current_copy["task_id"],
        "prior_implementation_lineage_ref": "contract-runtime:implementation:prior",
    }

    safe_copy = mf_parallel_precommit_correction_writer_safe_copy(
        source,
        correction_intent=intent,
    )
    correction_copy = safe_copy["copy_payload"]
    assert source["writer_role_safe_copy_payload"]["copy_payload"] == current_copy
    assert {
        field: correction_copy[field]
        for field in MF_PARALLEL_ATOMIC_LANE_WRITER_BINDING_FIELDS
        if field not in {"runtime_guide_hash", "stage_id", "line_id", "evidence_kind"}
    } == {
        field: current_copy[field]
        for field in MF_PARALLEL_ATOMIC_LANE_WRITER_BINDING_FIELDS
        if field not in {"runtime_guide_hash", "stage_id", "line_id", "evidence_kind"}
    }
    assert correction_copy["stage_id"] == "worker_implementation"
    assert correction_copy["line_id"] == "worker_implementation"
    assert correction_copy["evidence_kind"] == "implementation"
    assert correction_copy["runtime_guide_hash"] != current_hash
    binding = safe_copy["precommit_correction_writer_binding"]
    assert binding["bound"] is True
    assert binding["contract_position_mutated"] is False
    assert binding["authority_hash"] == correction_copy["runtime_guide_hash"]
    assert stable_sha256(binding["authority"]) == binding["authority_hash"]
    correction_guide = {"writer_role_safe_copy_payload": safe_copy}
    assert _contract_runtime_writer_line_guide_hash(
        correction_guide,
        actor_role="mf_sub",
        stage_id="worker_implementation",
        line_id="worker_implementation",
        evidence_kind="implementation",
    ) == correction_copy["runtime_guide_hash"]
    assert not _contract_runtime_writer_line_guide_hash(
        correction_guide,
        actor_role="mf_sub",
        stage_id="worker_commit",
        line_id="worker_commit",
        evidence_kind="worker_commit",
    )

    mismatched = dict(intent)
    mismatched["runtime_context_id"] = "mfrctx-another-lane"
    assert mf_parallel_precommit_correction_writer_safe_copy(
        source,
        correction_intent=mismatched,
    ) == {}


def test_precommit_correction_intent_bypasses_completed_line_projection():
    intent = {
        "schema_version": (
            "runtime_context.precommit_implementation_correction_intent.v1"
        ),
        "action": "revise_precommit_worker_implementation",
        "contract_execution_id": "cex-precommit-projection-bypass",
        "runtime_context_id": "mfrctx-precommit-projection-bypass",
        "task_id": "task-precommit-projection-bypass",
        "prior_implementation_lineage_ref": (
            "contract-runtime:implementation:prior"
        ),
    }

    assert _contract_runtime_precommit_correction_intent_requested(
        {
            "event_kind": "implementation",
            "payload": {
                "precommit_implementation_correction_intent": intent,
            },
        }
    ) is True
    assert _contract_runtime_precommit_correction_intent_requested(
        {
            "event_kind": "implementation",
            "payload": {"changed_files": ["agent/governance/server.py"]},
        }
    ) is False
    assert _contract_runtime_precommit_correction_intent_requested(
        {
            "event_kind": "implementation",
            "payload": {
                "precommit_implementation_correction_intent": None,
            },
        }
    ) is True


def test_contract_runtime_writer_role_safe_copy_payload_for_mf_sub_to_qa():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    execution_id = "cex-mf-parallel-writer-role-copy-worker-qa-test"
    _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-WRITER-ROLE-GUIDE-HASH-MISMATCH-20260701",
        contract_execution_id=execution_id,
        route_token_ref="rtok-writer-role-copy",
    )
    _submit_next_runtime_line(runtime, execution_id, actor_role="observer")
    _submit_next_runtime_line(runtime, execution_id, actor_role="observer")
    for _ in range(7):
        _submit_next_runtime_line(runtime, execution_id, actor_role="mf_sub")

    mf_sub_guide = runtime.current_guide(execution_id, actor_role="mf_sub")
    copy_payload = mf_sub_guide["writer_role_safe_copy_payload"]["copy_payload"]
    assert mf_sub_guide["next_legal_action"]["line_id"] == "qa_independent_verification"
    assert copy_payload["actor_role"] == "qa"
    assert copy_payload["runtime_guide_hash"] != mf_sub_guide["runtime_guide_hash"]

    stale_reader_payload = dict(copy_payload)
    stale_reader_payload["runtime_guide_hash"] = mf_sub_guide["runtime_guide_hash"]
    rejected = runtime.submit_line_write(execution_id, stale_reader_payload)

    assert rejected["ok"] is False
    mismatch = "\n".join(rejected["decision"]["errors"])
    assert "reader-role guide hash for role 'mf_sub'" in mismatch
    assert "owner/writer-role guide hash for role 'qa'" in mismatch
    assert "writer_role_safe_copy_payload.copy_payload.runtime_guide_hash" in mismatch

    accepted = runtime.submit_line_write(execution_id, copy_payload)
    assert accepted["ok"] is True
    assert accepted["record"]["completed_lines"][-1]["actor_role"] == "qa"


@pytest.mark.parametrize("qa_status", ["failed", "blocked", "rejected"])
def test_mf_parallel_qa_failed_blocked_or_rejected_does_not_unlock_observer_merge(
    qa_status,
):
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-CLAUDE-PARALLEL-QA-FAILED-BLOCKS-MERGE-20260629",
        contract_execution_id=f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
        route_token_ref="rtok-test",
    )

    for stage_id, line_id, actor_role in [
        ("orchestration", "observer_prefill_child_contracts", "observer"),
        ("dispatch", "observer_dispatch_bounded_workers", "observer"),
        ("worker_read", "worker_read_runtime_guide", "mf_sub"),
        ("worker_startup", "worker_startup", "mf_sub"),
        ("worker_context", "worker_graph_context", "mf_sub"),
        ("worker_implementation", "worker_implementation", "mf_sub"),
        ("worker_attestation", "worker_finish_time_attestation", "mf_sub"),
        ("worker_finish", "worker_finish_gate", "mf_sub"),
        ("qa_handoff", "worker_review_ready_handoff", "mf_sub"),
    ]:
        runtime.current_guide(
            f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
            actor_role=actor_role,
        )
        record = runtime.store.get(
            f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test"
        )
        accepted = runtime.submit_line_write(
            f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
            _runtime_write_from(
                record,
                actor_role=actor_role,
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
        record = accepted["record"]

    runtime.current_guide(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
        actor_role="qa",
    )
    record = runtime.store.get(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test"
    )
    failed_qa_write = _runtime_write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
    )
    failed_qa_write["status"] = qa_status
    failed_qa_write["payload"] = {"status": qa_status}
    if qa_status == "failed":
        failed_qa_write["verification"] = {
            "verdict": "fail",
            "summary": "Independent QA failed the worker commit due to scope drift.",
        }
    accepted_failed_qa = runtime.submit_line_write(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
        failed_qa_write,
    )
    assert accepted_failed_qa["ok"] is True
    record = accepted_failed_qa["record"]
    next_action = record["runtime_guide"]["next_legal_action"]
    assert next_action["line_id"] == "worker_read_runtime_guide"
    assert next_action["owner_role"] == "mf_sub"
    assert next_action["line_id"] != "observer_merge"
    assert next_action["semantic_next_action"] == "revise_after_failed_independent_qa"
    assert next_action["failed_qa_blocker"]["status"] == (
        "blocked_by_failed_independent_qa"
    )
    assert next_action["failed_qa_blocker"]["failed_qa_status"] == qa_status

    runtime.current_guide(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
        actor_role="observer",
    )
    record = runtime.store.get(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test"
    )
    rejected_merge = runtime.submit_line_write(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="observer_integration",
            line_id="observer_merge",
        ),
    )
    assert rejected_merge["ok"] is False
    assert any(
        "write does not match next legal action" in error
        or "cannot write line" in error
        for error in rejected_merge["decision"]["errors"]
    )

    for stage_id, line_id in [
        ("worker_read", "worker_read_runtime_guide"),
        ("worker_startup", "worker_startup"),
        ("worker_context", "worker_graph_context"),
        ("worker_implementation", "worker_implementation"),
        ("worker_attestation", "worker_finish_time_attestation"),
        ("worker_finish", "worker_finish_gate"),
        ("qa_handoff", "worker_review_ready_handoff"),
    ]:
        runtime.current_guide(
            f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
            actor_role="mf_sub",
        )
        record = runtime.store.get(
            f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test"
        )
        accepted_retry = runtime.submit_line_write(
            f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
            _runtime_write_from(
                record,
                actor_role="mf_sub",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted_retry["ok"] is True
        record = accepted_retry["record"]

    runtime.current_guide(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
        actor_role="qa",
    )
    record = runtime.store.get(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test"
    )
    accepted_passed_qa = runtime.submit_line_write(
        f"cex-mf-parallel-qa-{qa_status}-blocks-merge-test",
        _runtime_write_from(
            record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )
    assert accepted_passed_qa["ok"] is True
    record = accepted_passed_qa["record"]
    assert record["runtime_guide"]["next_legal_action"]["line_id"] == "observer_merge"


def test_mf_parallel_repeated_failed_qa_payload_outcome_does_not_unlock_observer_merge():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    execution_id = "cex-mf-parallel-qa-outcome-failed-blocks-merge-test"
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-CLAUDE-PARALLEL-QA-OUTCOME-FAILED-BLOCKS-MERGE-20260629",
        contract_execution_id=execution_id,
        route_token_ref="rtok-test",
    )

    for stage_id, line_id, actor_role in [
        ("orchestration", "observer_prefill_child_contracts", "observer"),
        ("dispatch", "observer_dispatch_bounded_workers", "observer"),
        ("worker_read", "worker_read_runtime_guide", "mf_sub"),
        ("worker_startup", "worker_startup", "mf_sub"),
        ("worker_context", "worker_graph_context", "mf_sub"),
        ("worker_implementation", "worker_implementation", "mf_sub"),
        ("worker_attestation", "worker_finish_time_attestation", "mf_sub"),
        ("worker_finish", "worker_finish_gate", "mf_sub"),
        ("qa_handoff", "worker_review_ready_handoff", "mf_sub"),
    ]:
        runtime.current_guide(execution_id, actor_role=actor_role)
        record = runtime.store.get(execution_id)
        accepted = runtime.submit_line_write(
            execution_id,
            _runtime_write_from(
                record,
                actor_role=actor_role,
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
        record = accepted["record"]

    runtime.current_guide(execution_id, actor_role="qa")
    record = runtime.store.get(execution_id)
    failed_verdict_write = _runtime_write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
    )
    failed_verdict_write["payload"] = {"verdict": "failed"}
    accepted_failed_verdict = runtime.submit_line_write(
        execution_id,
        failed_verdict_write,
    )
    assert accepted_failed_verdict["ok"] is True
    assert (
        accepted_failed_verdict["record"]["runtime_guide"]["next_legal_action"][
            "line_id"
        ]
        == "worker_read_runtime_guide"
    )

    for stage_id, line_id in [
        ("worker_read", "worker_read_runtime_guide"),
        ("worker_startup", "worker_startup"),
        ("worker_context", "worker_graph_context"),
        ("worker_implementation", "worker_implementation"),
        ("worker_attestation", "worker_finish_time_attestation"),
        ("worker_finish", "worker_finish_gate"),
        ("qa_handoff", "worker_review_ready_handoff"),
    ]:
        runtime.current_guide(execution_id, actor_role="mf_sub")
        record = runtime.store.get(execution_id)
        accepted_retry = runtime.submit_line_write(
            execution_id,
            _runtime_write_from(
                record,
                actor_role="mf_sub",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted_retry["ok"] is True

    runtime.current_guide(execution_id, actor_role="qa")
    record = runtime.store.get(execution_id)
    failed_outcome_write = _runtime_write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
    )
    failed_outcome_write["payload"] = {"outcome": "failed"}
    accepted_failed_outcome = runtime.submit_line_write(
        execution_id,
        failed_outcome_write,
    )
    assert accepted_failed_outcome["ok"] is True
    next_action = accepted_failed_outcome["record"]["runtime_guide"][
        "next_legal_action"
    ]
    assert next_action["line_id"] == "worker_read_runtime_guide"
    assert next_action["owner_role"] == "mf_sub"
    assert next_action["line_id"] != "observer_merge"


@pytest.mark.parametrize(
    "failed_evidence",
    [
        {"payload": {"verdict": "failed"}},
        {"payload": {"outcome": "blocked"}},
        {"payload": {"decision": "rejected"}},
        {"test_results": {"tests_summary": {"failures": 1}}},
        {"test_results": {"tests_summary": "Independent QA failed"}},
        {"tests": [{"command": {"status": "failed"}}]},
        {"verification": {"decision": "rejected"}},
        {
            "artifact_refs": {
                "qa_report": {
                    "command": {"status": "blocked"},
                }
            }
        },
    ],
    ids=[
        "payload-verdict",
        "payload-outcome",
        "payload-decision",
        "tests-summary-count",
        "tests-summary-text",
        "command-status",
        "verification",
        "nested-artifact-refs",
    ],
)
def test_mf_parallel_persisted_nested_failed_qa_evidence_blocks_observer_merge(
    failed_evidence,
):
    service = ContractCrudService()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        service.registry,
        store=SQLiteContractExecutionStore(conn),
    )
    execution_id = "cex-mf-parallel-nested-failed-qa-blocks-merge-test"
    _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-NESTED-FAILED-QA-BLOCKS-MERGE-20260721",
        contract_execution_id=execution_id,
        route_token_ref="rtok-test",
    )

    for stage_id, line_id, actor_role in [
        ("orchestration", "observer_prefill_child_contracts", "observer"),
        ("dispatch", "observer_dispatch_bounded_workers", "observer"),
        ("worker_read", "worker_read_runtime_guide", "mf_sub"),
        ("worker_startup", "worker_startup", "mf_sub"),
        ("worker_context", "worker_graph_context", "mf_sub"),
        ("worker_implementation", "worker_implementation", "mf_sub"),
        ("worker_attestation", "worker_finish_time_attestation", "mf_sub"),
        ("worker_finish", "worker_finish_gate", "mf_sub"),
        ("qa_handoff", "worker_review_ready_handoff", "mf_sub"),
    ]:
        runtime.current_guide(execution_id, actor_role=actor_role)
        record = runtime.store.get(execution_id)
        accepted = runtime.submit_line_write(
            execution_id,
            _runtime_write_from(
                record,
                actor_role=actor_role,
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True

    runtime.current_guide(execution_id, actor_role="qa")
    record = runtime.store.get(execution_id)
    failed_qa_write = _runtime_write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
    )
    failed_qa_write["status"] = "accepted"
    failed_qa_write.update(failed_evidence)
    accepted_failed_qa = runtime.submit_line_write(execution_id, failed_qa_write)
    assert accepted_failed_qa["ok"] is True

    persisted = runtime.store.get(execution_id)
    next_action = persisted["runtime_guide"]["next_legal_action"]
    assert next_action["line_id"] == "worker_read_runtime_guide"
    assert next_action["owner_role"] == "mf_sub"
    assert next_action["line_id"] != "observer_merge"
    assert next_action["semantic_next_action"] == "revise_after_failed_independent_qa"


def test_mf_parallel_failed_qa_retry_ignores_malformed_worker_implementation_payload():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    execution_id = "cex-mf-parallel-malformed-retry-implementation-test"
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-MALFORMED-RETRY-IMPLEMENTATION-20260629",
        contract_execution_id=execution_id,
        route_token_ref="rtok-test",
    )

    for stage_id, line_id, actor_role in [
        ("orchestration", "observer_prefill_child_contracts", "observer"),
        ("dispatch", "observer_dispatch_bounded_workers", "observer"),
        ("worker_read", "worker_read_runtime_guide", "mf_sub"),
        ("worker_startup", "worker_startup", "mf_sub"),
        ("worker_context", "worker_graph_context", "mf_sub"),
        ("worker_implementation", "worker_implementation", "mf_sub"),
        ("worker_attestation", "worker_finish_time_attestation", "mf_sub"),
        ("worker_finish", "worker_finish_gate", "mf_sub"),
        ("qa_handoff", "worker_review_ready_handoff", "mf_sub"),
    ]:
        runtime.current_guide(execution_id, actor_role=actor_role)
        record = runtime.store.get(execution_id)
        accepted = runtime.submit_line_write(
            execution_id,
            _runtime_write_from(
                record,
                actor_role=actor_role,
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True

    runtime.current_guide(execution_id, actor_role="qa")
    record = runtime.store.get(execution_id)
    failed_qa = _runtime_write_from(
        record,
        actor_role="qa",
        stage_id="qa",
        line_id="qa_independent_verification",
    )
    failed_qa["payload"] = {"outcome": "failed"}
    accepted_failed_qa = runtime.submit_line_write(execution_id, failed_qa)
    assert accepted_failed_qa["ok"] is True

    record = runtime.store.get(execution_id)
    malformed = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_implementation",
        line_id="worker_implementation",
    )
    malformed["payload"] = {
        "schema_version": "worker_runtime_guide_read_after_failed_qa_revision.v1",
        "summary": "This is read/continuation evidence, not implementation.",
    }
    record["completed_lines"].append(malformed)
    runtime.store.update(execution_id, record)

    runtime.current_guide(execution_id, actor_role="mf_sub")
    record = runtime.store.get(execution_id)
    next_action = record["runtime_guide"]["next_legal_action"]
    assert next_action["line_id"] == "worker_read_runtime_guide"
    assert next_action["owner_role"] == "mf_sub"


def test_direct_fix_historical_qa_reentry_is_terminally_retired():
    service = ContractCrudService()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    runtime = ContractRuntime(
        service.registry,
        store=SQLiteContractExecutionStore(conn),
    )
    backlog_id = "AC-DIRECT-FIX-QA-FAILED-BLOCKS-PROGRESSION-20260629"
    child_id = "cex-direct-fix-qa-failed-blocks-progression-test"
    with pytest.raises(ContractRetirementError) as raised:
        _start_direct_fix_successor(
            runtime,
            project_id="aming-claw",
            backlog_id=backlog_id,
            contract_execution_id=child_id,
            route_token_ref="rtok-test",
        )
    result = raised.value.to_dict()
    assert result["code"] == "direct_fix_retired"
    assert result["authorizes_write"] is False
    assert result["historical_evidence_readable"] is True
    assert result["forbidden_backedges"] == [
        "direct_fix_enter",
        "parent_to_resume",
        "return_to_parent",
        "resume_original_contract",
        "retry_source_backlog_close_after_repair",
    ]


def test_direct_fix_repair_diagnostic_cannot_reenter_retired_contract():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    child_id = "cex-direct-fix-repair-diagnostic-blocked-status-test"
    with pytest.raises(ContractRetirementError) as raised:
        _start_direct_fix_successor(
            runtime,
            project_id="aming-claw",
            backlog_id="AC-DIRECT-FIX-DIAGNOSTIC-BLOCKED-STATUS-20260630",
            contract_execution_id=child_id,
            route_token_ref="rtok-test",
        )
    assert raised.value.to_dict()["next_legal_action"]["id"] == (
        "file_fresh_bounded_row"
    )


def test_direct_fix_close_authority_gate_rejects_failed_qa_provenance():
    projection = {
        "projection_source": "backlog_contract_chain_current",
        "source_of_proof": "contract_runtime_executions.completed_lines",
        "active_chain": {"execution_ids": ["cex-parent", "cex-child"]},
    }
    child_record = {
        "contract_id": "direct_fix",
        "contract_execution_id": "cex-child",
        "parent_contract_execution_id": "cex-parent",
        "completed_lines": [
            {
                "stage_id": "candidate_repair",
                "line_id": "direct_fix_candidate_repair",
                "actor_role": "mf_sub",
                "evidence_kind": "direct_fix_repair_evidence",
            },
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "qa_evidence_provenance": {"status": "failed"},
            },
            {
                "stage_id": "return_to_parent",
                "line_id": "direct_fix_return_to_parent",
                "actor_role": "observer",
                "evidence_kind": "direct_fix_return_to_parent",
            },
        ],
    }
    parent_record = {
        "contract_id": "onboard_contract",
        "contract_execution_id": "cex-parent",
        "completed_lines": [
            {
                "stage_id": "successor_return",
                "line_id": "resume_parent_after_successor_return",
                "actor_role": "observer",
                "evidence_kind": "successor_return_acknowledgement",
                "payload": {
                    "parent_contract_execution_id": "cex-parent",
                    "successor_contract_execution_id": "cex-child",
                },
            }
        ],
    }

    rejected = _contract_runtime_direct_fix_close_authority_gate(
        [parent_record, child_record],
        chain_projection=projection,
        close_commit="",
    )
    assert rejected["passed"] is False
    assert "independent_qa" in rejected["missing_requirement_ids"]
    assert rejected["checks"]["has_independent_qa"] is False

    child_record["completed_lines"][1]["qa_evidence_provenance"] = {
        "status": "passed"
    }
    accepted = _contract_runtime_direct_fix_close_authority_gate(
        [parent_record, child_record],
        chain_projection=projection,
        close_commit="",
    )
    assert accepted["passed"] is True
    assert accepted["checks"]["has_independent_qa"] is True


def test_direct_fix_close_authority_requires_ack_for_each_returned_child():
    def returned_direct_fix_record(child_id: str):
        return {
            "contract_id": "direct_fix",
            "contract_execution_id": child_id,
            "parent_contract_execution_id": "cex-parent",
            "completed_lines": [
                {
                    "stage_id": "candidate_repair",
                    "line_id": "direct_fix_candidate_repair",
                    "actor_role": "mf_sub",
                    "evidence_kind": "direct_fix_repair_evidence",
                },
                {
                    "stage_id": "qa",
                    "line_id": "qa_independent_verification",
                    "actor_role": "qa",
                    "evidence_kind": "independent_verification",
                    "qa_evidence_provenance": {"status": "passed"},
                },
                {
                    "stage_id": "return_to_parent",
                    "line_id": "direct_fix_return_to_parent",
                    "actor_role": "observer",
                    "evidence_kind": "direct_fix_return_to_parent",
                },
            ],
        }

    def parent_ack_line(child_id: str):
        return {
            "stage_id": "successor_return",
            "line_id": "resume_parent_after_successor_return",
            "actor_role": "observer",
            "evidence_kind": "successor_return_acknowledgement",
            "payload": {
                "parent_contract_execution_id": "cex-parent",
                "successor_contract_execution_id": child_id,
                "successor_contract_id": "direct_fix",
            },
        }

    projection = {
        "projection_source": "backlog_contract_chain_current",
        "source_of_proof": "contract_runtime_executions.completed_lines",
        "active_chain": {
            "execution_ids": ["cex-parent", "cex-child-a", "cex-child-b"]
        },
    }
    parent_record = {
        "contract_id": "onboard_contract",
        "contract_execution_id": "cex-parent",
        "completed_lines": [parent_ack_line("cex-child-b")],
    }
    child_a = returned_direct_fix_record("cex-child-a")
    child_b = returned_direct_fix_record("cex-child-b")

    rejected = _contract_runtime_direct_fix_close_authority_gate(
        [parent_record, child_a, child_b],
        chain_projection=projection,
        close_commit="",
    )
    assert rejected["passed"] is False
    assert rejected["missing_requirement_ids"] == ["parent_return_acknowledgement"]
    assert rejected["returned_successor_contract_execution_ids"] == [
        "cex-child-a",
        "cex-child-b",
    ]
    assert rejected["acknowledged_successor_contract_execution_ids"] == [
        "cex-child-b"
    ]
    assert rejected["unacknowledged_successor_contract_execution_ids"] == [
        "cex-child-a"
    ]
    assert rejected["checks"]["all_returned_children_acknowledged"] is False

    parent_record["completed_lines"].append(parent_ack_line("cex-child-a"))
    accepted = _contract_runtime_direct_fix_close_authority_gate(
        [parent_record, child_a, child_b],
        chain_projection=projection,
        close_commit="",
    )
    assert accepted["passed"] is True
    assert accepted["missing_requirement_ids"] == []
    assert accepted["unacknowledged_successor_contract_execution_ids"] == []
    assert accepted["checks"]["all_returned_children_acknowledged"] is True


def test_mf_parallel_gate_precheck_requires_onboard_parent():
    service = ContractCrudService()
    definition = service.read("mf_parallel.v1")["data"]["definition"]

    root_decision = run_contract_runtime_precheck(
        registry=service.registry,
        definition=definition,
        action="start_execution",
        actor_role="observer",
        subject={
            "project_id": "aming-claw",
            "backlog_id": "AC-MF-PARALLEL-GATE-PRECHECK",
        },
    )
    assert root_decision["schema_version"] == "contract_gate_decision.v1"
    assert root_decision["decision"] == "block"
    assert "contract_root_start_denied" in root_decision["errors"]
    assert root_decision["next_move"]["contract_id"] == "onboard_contract"

    runtime = ContractRuntime(service.registry)
    onboard = runtime.start_execution(
        "onboard_contract",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-GATE-PRECHECK",
        contract_execution_id="cex-mf-parallel-gate-onboard-parent",
        actor_role="observer",
        route_token_ref="rtok-mf-parallel-gate",
        contract_chain_id="cchain-mf-parallel-gate",
    )
    hotfix = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-GATE-PRECHECK",
        contract_execution_id="cex-mf-parallel-gate-hotfix-parent",
        actor_role="observer",
        parent_contract_execution_id=onboard["contract_execution_id"],
        root_contract_execution_id=onboard["contract_execution_id"],
        contract_chain_id=onboard["contract_chain_id"],
        route_token_ref="rtok-mf-parallel-gate",
    )

    with pytest.raises(ContractRuntimeError, match="parent_contract_not_allowed"):
        runtime.start_execution(
            "mf_parallel.v1",
            project_id="aming-claw",
            backlog_id="AC-MF-PARALLEL-GATE-PRECHECK",
            contract_execution_id="cex-mf-parallel-gate-wrong-parent",
            actor_role="observer",
            parent_contract_execution_id=hotfix["contract_execution_id"],
            root_contract_execution_id=onboard["contract_execution_id"],
            contract_chain_id=onboard["contract_chain_id"],
            route_token_ref="rtok-mf-parallel-gate",
        )


def test_mf_parallel_runtime_binds_worker_lines_to_runtime_context_instances():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-LANE-BOUND-LINES-BLOCK-20260625",
        contract_execution_id="cex-mf-parallel-lane-bound-test",
        route_token_ref="rtok-lane-bound-test",
    )
    record = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="orchestration",
            line_id="observer_prefill_child_contracts",
        ),
    )["record"]
    dispatch_write = _runtime_write_from(
        record,
        actor_role="observer",
        stage_id="dispatch",
        line_id="observer_dispatch_bounded_workers",
    )
    dispatch_write["payload"] = {
        "worker_count": 2,
        "required_worker_count": 2,
        "atomic_dispatch": True,
        "all_or_nothing": True,
        "workers": [
            {
                "runtime_context_id": "mfrctx-impl-core",
                "task_id": "mfsub-impl-core",
                "parent_task_id": "cex-mf-parallel-lane-bound-test",
                "lane_id": "impl-core",
                "worker_slot_id": "impl-core",
                "worker_id": "impl-core",
            },
            {
                "runtime_context_id": "mfrctx-trace-observability",
                "task_id": "mfsub-trace-observability",
                "parent_task_id": "cex-mf-parallel-lane-bound-test",
                "lane_id": "trace-observability",
                "worker_slot_id": "trace-observability",
                "worker_id": "trace-observability",
            },
        ]
    }
    dispatch = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        dispatch_write,
    )
    assert dispatch["ok"] is True
    runtime.current_guide("cex-mf-parallel-lane-bound-test", actor_role="mf_sub")
    record = runtime.store.get("cex-mf-parallel-lane-bound-test")
    next_action = record["runtime_guide"]["next_legal_action"]
    assert next_action["line_id"] == "worker_read_runtime_guide"
    assert next_action["runtime_context_id"] == "mfrctx-impl-core"
    assert next_action["line_instance_id"] == "runtime_context:mfrctx-impl-core"

    lanes = {
        "a": {
            "runtime_context_id": "mfrctx-impl-core",
            "task_id": "mfsub-impl-core",
            "lane_id": "impl-core",
            "worker_id": "impl-core",
        },
        "b": {
            "runtime_context_id": "mfrctx-trace-observability",
            "task_id": "mfsub-trace-observability",
            "lane_id": "trace-observability",
            "worker_id": "trace-observability",
        },
    }

    def lane_write(
        current,
        lane,
        *,
        stage_id: str,
        line_id: str,
        evidence_kind: str,
    ):
        identity = lanes[lane]
        write = _runtime_write_from(
            current,
            actor_role="mf_sub",
            stage_id=stage_id,
            line_id=line_id,
        )
        write.update(
            {
                **identity,
                "parent_task_id": "cex-mf-parallel-lane-bound-test",
                "worker_role": "mf_sub",
                "worker_slot_id": identity["lane_id"],
                "line_instance_id": (
                    f"runtime_context:{identity['runtime_context_id']}"
                ),
                "evidence_kind": evidence_kind,
            }
        )
        if line_id == "worker_read_runtime_guide":
            write["payload"] = {
                **identity,
                "parent_task_id": "cex-mf-parallel-lane-bound-test",
                "worker_slot_id": identity["lane_id"],
                "read_receipt_hash": f"sha256:{lane}-read",
            }
        return write

    lane_read_hashes = {}
    for lane in ("a", "b"):
        probe = lane_write(
            record,
            lane,
            stage_id="worker_read",
            line_id="worker_read_runtime_guide",
            evidence_kind="read_receipt",
        )
        _lane_state, lane_guide = runtime.mf_parallel_atomic_lane_gate_view(
            record,
            record["runtime_guide"],
            probe,
            source_record=record,
        )
        lane_read_hashes[lane] = lane_guide["runtime_guide_hash"]
    assert lane_read_hashes["a"] != lane_read_hashes["b"]

    for invalid_hash, public_actual in (
        ("sha256:" + "f" * 64, "sha256:" + "f" * 64),
        (lane_read_hashes["a"], lane_read_hashes["a"]),
        (
            {"session_token": "must-not-be-reflected"},
            "<invalid-runtime-guide-hash>",
        ),
        (["must-not-be-reflected"], "<invalid-runtime-guide-hash>"),
        (None, "<invalid-runtime-guide-hash>"),
        ("", "<invalid-runtime-guide-hash>"),
    ):
        invalid_b_read = lane_write(
            record,
            "b",
            stage_id="worker_read",
            line_id="worker_read_runtime_guide",
            evidence_kind="read_receipt",
        )
        invalid_b_read["runtime_guide_hash"] = invalid_hash
        before_invalid_hash = runtime.store.get(
            "cex-mf-parallel-lane-bound-test"
        )
        rejected_invalid_hash = runtime.submit_line_write(
            "cex-mf-parallel-lane-bound-test",
            invalid_b_read,
        )
        assert rejected_invalid_hash["ok"] is False
        hash_gate = rejected_invalid_hash["decision"][
            "imported_legacy_checks"
        ][0]
        assert {
            "field": "runtime_guide_hash",
            "expected": lane_read_hashes["b"],
            "actual": public_actual,
        } in hash_gate["identity_mismatches"]
        assert any(
            error.startswith("runtime_guide_hash mismatch:")
            for error in hash_gate["errors"]
        )
        assert "must-not-be-reflected" not in repr(hash_gate)
        assert runtime.store.get("cex-mf-parallel-lane-bound-test") == (
            before_invalid_hash
        )

    missing_hash_b_read = lane_write(
        record,
        "b",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
        evidence_kind="read_receipt",
    )
    missing_hash_b_read.pop("runtime_guide_hash")
    before_missing_hash = runtime.store.get("cex-mf-parallel-lane-bound-test")
    rejected_missing_hash = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        missing_hash_b_read,
    )
    assert rejected_missing_hash["ok"] is False
    assert {
        "field": "runtime_guide_hash",
        "expected": lane_read_hashes["b"],
        "actual": "<missing>",
    } in rejected_missing_hash["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ]
    assert runtime.store.get("cex-mf-parallel-lane-bound-test") == (
        before_missing_hash
    )

    before = runtime.store.get("cex-mf-parallel-lane-bound-test")
    skipped_a_startup = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        lane_write(
            record,
            "a",
            stage_id="worker_startup",
            line_id="worker_startup",
            evidence_kind="mf_subagent_startup",
        ),
    )
    assert skipped_a_startup["ok"] is False
    skipped_fields = {
        item["field"]
        for item in skipped_a_startup["decision"]["imported_legacy_checks"][0][
            "identity_mismatches"
        ]
    }
    assert {"stage_id", "line_id"}.issubset(skipped_fields)
    assert runtime.store.get("cex-mf-parallel-lane-bound-test") == before

    conflicting_nested_b = lane_write(
        record,
        "b",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
        evidence_kind="read_receipt",
    )
    conflicting_nested_b["payload"].update(
        {
            "runtime_context_id": lanes["a"]["runtime_context_id"],
            "task_id": lanes["a"]["task_id"],
            "lane_id": lanes["a"]["lane_id"],
            "worker_slot_id": lanes["a"]["lane_id"],
            "worker_id": lanes["a"]["worker_id"],
        }
    )
    rejected_nested_conflict = runtime.precheck_line_write(
        "cex-mf-parallel-lane-bound-test",
        conflicting_nested_b,
    )
    assert rejected_nested_conflict["ok"] is False
    nested_fields = {
        item["field"]
        for item in rejected_nested_conflict["decision"][
            "imported_legacy_checks"
        ][0]["identity_mismatches"]
    }
    assert {
        "payload.runtime_context_id",
        "payload.task_id",
        "payload.lane_id",
        "payload.worker_slot_id",
        "payload.worker_id",
    }.issubset(nested_fields)
    assert runtime.store.get("cex-mf-parallel-lane-bound-test") == before

    cross_wired_b = lane_write(
        record,
        "b",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
        evidence_kind="read_receipt",
    )
    cross_wired_b["task_id"] = lanes["a"]["task_id"]
    cross_wired_b["payload"]["task_id"] = lanes["a"]["task_id"]
    cross_wired_b["runtime_guide_hash"] = lane_read_hashes["a"]
    rejected_cross_wire = runtime.precheck_line_write(
        "cex-mf-parallel-lane-bound-test",
        cross_wired_b,
    )
    assert rejected_cross_wire["ok"] is False
    assert {
        "field": "task_id",
        "expected": lanes["b"]["task_id"],
        "actual": lanes["a"]["task_id"],
    } in rejected_cross_wire["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ]
    assert {
        "field": "runtime_guide_hash",
        "expected": lane_read_hashes["b"],
        "actual": lane_read_hashes["a"],
    } in rejected_cross_wire["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ]
    assert runtime.store.get("cex-mf-parallel-lane-bound-test") == before

    missing_slot_b = lane_write(
        record,
        "b",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
        evidence_kind="read_receipt",
    )
    missing_slot_b.pop("worker_slot_id")
    missing_slot_b["payload"].pop("worker_slot_id")
    rejected_missing_slot = runtime.precheck_line_write(
        "cex-mf-parallel-lane-bound-test",
        missing_slot_b,
    )
    assert rejected_missing_slot["ok"] is False
    assert {
        "field": "worker_slot_id",
        "expected": lanes["b"]["lane_id"],
        "actual": "<missing>",
    } in rejected_missing_slot["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ]
    assert runtime.store.get("cex-mf-parallel-lane-bound-test") == before

    failed_b_read = lane_write(
        record,
        "b",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
        evidence_kind="read_receipt",
    )
    failed_b_read["status"] = "failed"
    lane_state, lane_guide = runtime.mf_parallel_atomic_lane_gate_view(
        record,
        record["runtime_guide"],
        failed_b_read,
        source_record=record,
    )
    lane_scope = lane_guide["runtime_guide_hash_scope"]
    assert lane_scope["hash_input"] == "authority"
    assert lane_scope["source_global_runtime_guide_hash"] == (
        record["runtime_guide"]["runtime_guide_hash"]
    )
    assert lane_guide["runtime_guide_hash"] == stable_sha256(
        lane_scope["authority"]
    )
    assert lane_guide["writer_role_safe_copy_payload"]["copy_payload"][
        "runtime_guide_hash"
    ] == lane_guide["runtime_guide_hash"]
    assert lane_state["next_action"] == lane_guide["next_legal_action"]
    lane_precheck = runtime.precheck_line_write(
        "cex-mf-parallel-lane-bound-test",
        failed_b_read,
    )
    assert lane_precheck["ok"] is True
    assert lane_precheck["write"]["runtime_guide_hash"] == (
        lane_guide["runtime_guide_hash"]
    )
    recorded_failed_b_read = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        failed_b_read,
    )
    assert recorded_failed_b_read["ok"] is True
    record = recorded_failed_b_read["record"]
    assert record["runtime_guide"]["next_legal_action"] == next_action

    accepted_b_read = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        lane_write(
            record,
            "b",
            stage_id="worker_read",
            line_id="worker_read_runtime_guide",
            evidence_kind="read_receipt",
        ),
    )
    assert accepted_b_read["ok"] is True
    record = accepted_b_read["record"]
    assert record["runtime_guide"]["next_legal_action"] == next_action

    stale_b_startup = lane_write(
        before,
        "b",
        stage_id="worker_startup",
        line_id="worker_startup",
        evidence_kind="mf_subagent_startup",
    )
    before_duplicate = runtime.store.get("cex-mf-parallel-lane-bound-test")
    duplicate_b_read = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        lane_write(
            record,
            "b",
            stage_id="worker_read",
            line_id="worker_read_runtime_guide",
            evidence_kind="read_receipt",
        ),
    )
    assert duplicate_b_read["ok"] is False
    assert runtime.store.get("cex-mf-parallel-lane-bound-test") == before_duplicate

    rejected_stale = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        stale_b_startup,
    )
    assert rejected_stale["ok"] is False
    _lane_state, current_b_startup_guide = (
        runtime.mf_parallel_atomic_lane_gate_view(
            record,
            record["runtime_guide"],
            lane_write(
                record,
                "b",
                stage_id="worker_startup",
                line_id="worker_startup",
                evidence_kind="mf_subagent_startup",
            ),
            source_record=record,
        )
    )
    assert {
        "field": "runtime_guide_hash",
        "expected": current_b_startup_guide["runtime_guide_hash"],
        "actual": stale_b_startup["runtime_guide_hash"],
    } in rejected_stale["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ]
    assert runtime.store.get("cex-mf-parallel-lane-bound-test") == before_duplicate

    accepted_b_startup = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        lane_write(
            record,
            "b",
            stage_id="worker_startup",
            line_id="worker_startup",
            evidence_kind="mf_subagent_startup",
        ),
    )
    assert accepted_b_startup["ok"] is True
    record = accepted_b_startup["record"]
    assert record["runtime_guide"]["next_legal_action"] == next_action

    observer_merge_before_a = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="observer_integration",
            line_id="observer_merge",
        ),
    )
    assert observer_merge_before_a["ok"] is False
    after_observer_merge = runtime.store.get("cex-mf-parallel-lane-bound-test")
    assert after_observer_merge["execution_state_revision"] == record[
        "execution_state_revision"
    ]
    assert after_observer_merge["completed_lines"] == record["completed_lines"]
    runtime.current_guide(
        "cex-mf-parallel-lane-bound-test",
        actor_role="mf_sub",
    )
    record = runtime.store.get("cex-mf-parallel-lane-bound-test")

    accepted_a_read = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        lane_write(
            record,
            "a",
            stage_id="worker_read",
            line_id="worker_read_runtime_guide",
            evidence_kind="read_receipt",
        ),
    )
    assert accepted_a_read["ok"] is True
    record = accepted_a_read["record"]
    accepted_a_startup = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        lane_write(
            record,
            "a",
            stage_id="worker_startup",
            line_id="worker_startup",
            evidence_kind="mf_subagent_startup",
        ),
    )
    assert accepted_a_startup["ok"] is True
    completed = accepted_a_startup["record"]["completed_lines"]
    assert {
        (line["line_id"], line.get("runtime_context_id"))
        for line in completed
        if line["line_id"] in {"worker_read_runtime_guide", "worker_startup"}
    } == {
        ("worker_read_runtime_guide", lanes["a"]["runtime_context_id"]),
        ("worker_startup", lanes["a"]["runtime_context_id"]),
        ("worker_read_runtime_guide", lanes["b"]["runtime_context_id"]),
        ("worker_startup", lanes["b"]["runtime_context_id"]),
    }

    record = accepted_a_startup["record"]
    for lane in ("b", "a"):
        accepted_graph = runtime.submit_line_write(
            "cex-mf-parallel-lane-bound-test",
            lane_write(
                record,
                lane,
                stage_id="worker_context",
                line_id="worker_graph_context",
                evidence_kind="graph_trace",
            ),
        )
        assert accepted_graph["ok"] is True
        record = accepted_graph["record"]

    stale_b_implementation = lane_write(
        record,
        "b",
        stage_id="worker_implementation",
        line_id="worker_implementation",
        evidence_kind="implementation",
    )
    _lane_state, stale_b_guide = runtime.mf_parallel_atomic_lane_gate_view(
        record,
        record["runtime_guide"],
        stale_b_implementation,
        source_record=record,
    )
    stale_b_copy = stale_b_guide["writer_role_safe_copy_payload"][
        "copy_payload"
    ]
    stale_b_binding = {
        field: stale_b_copy[field]
        for field in (
            "backlog_id",
            "definition_hash",
            "instruction_bundle_hash",
            "execution_state_revision",
            "runtime_guide_hash",
            "stage_id",
            "line_id",
            "evidence_kind",
            "line_instance_id",
        )
    }

    accepted_a_implementation = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        lane_write(
            record,
            "a",
            stage_id="worker_implementation",
            line_id="worker_implementation",
            evidence_kind="implementation",
        ),
    )
    assert accepted_a_implementation["ok"] is True
    current = runtime.current_record(
        "cex-mf-parallel-lane-bound-test",
        actor_role="mf_sub",
    )
    current_b_write = lane_write(
        current,
        "b",
        stage_id="worker_implementation",
        line_id="worker_implementation",
        evidence_kind="implementation",
    )
    _lane_state, current_b_guide = runtime.mf_parallel_atomic_lane_gate_view(
        current,
        current["runtime_guide"],
        current_b_write,
        source_record=current,
    )
    current_b_copy = current_b_guide["writer_role_safe_copy_payload"][
        "copy_payload"
    ]

    rebased = _contract_runtime_mf_parallel_concurrent_sibling_writer_rebase(
        runtime,
        stored_record=current,
        actor_role="mf_sub",
        write=stale_b_implementation,
        submitted_writer_binding=stale_b_binding,
        current_lane_writer_copy=current_b_copy,
    )
    assert rebased["copy_payload"]["execution_state_revision"] == current[
        "execution_state_revision"
    ]
    assert rebased["copy_payload"]["runtime_guide_hash"] == current_b_copy[
        "runtime_guide_hash"
    ]
    assert rebased["evidence"]["intervening_line_count"] == 1
    assert rebased["evidence"][
        "intervening_lines_all_from_dispatched_sibling"
    ] is True

    forged_b_binding = dict(stale_b_binding)
    forged_b_binding["runtime_guide_hash"] = "sha256:" + "f" * 64
    assert not _contract_runtime_mf_parallel_concurrent_sibling_writer_rebase(
        runtime,
        stored_record=current,
        actor_role="mf_sub",
        write=stale_b_implementation,
        submitted_writer_binding=forged_b_binding,
        current_lane_writer_copy=current_b_copy,
    )


def test_mf_parallel_one_worker_dispatch_binds_exact_private_lane():
    runtime = ContractRuntime(ContractCrudService().registry)
    execution_id = "cex-mf-parallel-one-worker-lane-bound-test"
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-ONE-WORKER-LANE-BOUND-TEST",
        contract_execution_id=execution_id,
        route_token_ref="rtok-one-worker-lane-bound",
    )
    record = runtime.submit_line_write(
        execution_id,
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="orchestration",
            line_id="observer_prefill_child_contracts",
        ),
    )["record"]
    dispatch = _runtime_write_from(
        record,
        actor_role="observer",
        stage_id="dispatch",
        line_id="observer_dispatch_bounded_workers",
    )
    worker = {
        "runtime_context_id": "mfrctx-one-worker-lane-bound",
        "task_id": "one-worker-lane-bound",
        "parent_task_id": execution_id,
        "lane_id": "one-worker-lane-bound",
        "worker_slot_id": "one-worker-lane-bound",
        "worker_id": "one-worker-lane-bound",
    }
    dispatch["payload"] = {
        "worker_count": 1,
        "required_worker_count": 1,
        "atomic_dispatch": False,
        "workers": [worker],
    }
    dispatched = runtime.submit_line_write(execution_id, dispatch)
    assert dispatched["ok"] is True
    runtime.current_guide(execution_id, actor_role="mf_sub")
    record = runtime.store.get(execution_id)

    lane_read = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
    )
    lane_read.update(
        {
            **worker,
            "worker_role": "mf_sub",
            "line_instance_id": (
                f"runtime_context:{worker['runtime_context_id']}"
            ),
            "payload": {
                **worker,
                "worker_role": "mf_sub",
                "read_receipt_hash": "sha256:one-worker-read",
            },
        }
    )
    _lane_state, lane_guide = runtime.mf_parallel_atomic_lane_gate_view(
        record,
        record["runtime_guide"],
        lane_read,
        source_record=record,
    )
    assert lane_guide["atomic_lane_gate_binding"]["bound"] is True
    assert lane_guide["atomic_lane_gate_binding"][
        "runtime_context_id"
    ] == worker["runtime_context_id"]
    accepted = runtime.submit_line_write(execution_id, lane_read)
    assert accepted["ok"] is True

    current = accepted["record"]
    forged = _runtime_write_from(
        current,
        actor_role="mf_sub",
        stage_id="worker_startup",
        line_id="worker_startup",
    )
    forged.update(
        {
            **worker,
            "runtime_context_id": "mfrctx-forged-second-lane",
            "worker_role": "mf_sub",
            "line_instance_id": "runtime_context:mfrctx-forged-second-lane",
        }
    )
    before = runtime.store.get(execution_id)
    rejected = runtime.precheck_line_write(execution_id, forged)
    assert rejected["ok"] is False
    assert rejected["record"]["runtime_guide"].get(
        "atomic_lane_gate_binding"
    ) is None
    assert runtime.store.get(execution_id) == before

    for stage_id, line_id, evidence_kind in (
        ("worker_startup", "worker_startup", "mf_subagent_startup"),
        ("worker_context", "worker_graph_context", "graph_trace"),
        (
            "worker_implementation",
            "worker_implementation",
            "implementation",
        ),
    ):
        runtime.current_guide(execution_id, actor_role="mf_sub")
        current = runtime.store.get(execution_id)
        lane_write = _runtime_write_from(
            current,
            actor_role="mf_sub",
            stage_id=stage_id,
            line_id=line_id,
        )
        lane_write.update(
            {
                **worker,
                "worker_role": "mf_sub",
                "line_instance_id": (
                    f"runtime_context:{worker['runtime_context_id']}"
                ),
                "evidence_kind": evidence_kind,
            }
        )
        _lane_state, lane_guide = runtime.mf_parallel_atomic_lane_gate_view(
            current,
            current["runtime_guide"],
            lane_write,
            source_record=current,
        )
        assert lane_guide["atomic_lane_gate_binding"]["bound"] is True
        assert lane_guide["next_legal_action"]["line_id"] == line_id
        accepted = runtime.submit_line_write(execution_id, lane_write)
        assert accepted["ok"] is True


def test_mf_parallel_non_atomic_dispatch_keeps_global_lane_order():
    runtime = ContractRuntime(ContractCrudService().registry)
    execution_id = "cex-mf-parallel-non-atomic-lane-order-test"
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-NON-ATOMIC-LANE-ORDER-TEST",
        contract_execution_id=execution_id,
        route_token_ref="rtok-non-atomic-lane-order",
    )
    record = runtime.submit_line_write(
        execution_id,
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="orchestration",
            line_id="observer_prefill_child_contracts",
        ),
    )["record"]
    dispatch = _runtime_write_from(
        record,
        actor_role="observer",
        stage_id="dispatch",
        line_id="observer_dispatch_bounded_workers",
    )
    dispatch["payload"] = {
        "worker_count": 2,
        "required_worker_count": 2,
        "workers": [
            {
                "runtime_context_id": "mfrctx-non-atomic-a",
                "task_id": "non-atomic-a",
                "parent_task_id": execution_id,
                "lane_id": "non-atomic-a",
                "worker_slot_id": "non-atomic-a",
            },
            {
                "runtime_context_id": "mfrctx-non-atomic-b",
                "task_id": "non-atomic-b",
                "parent_task_id": execution_id,
                "lane_id": "non-atomic-b",
                "worker_slot_id": "non-atomic-b",
            },
        ],
    }
    assert runtime.submit_line_write(execution_id, dispatch)["ok"] is True
    runtime.current_guide(execution_id, actor_role="mf_sub")
    record = runtime.store.get(execution_id)
    sibling_b = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
    )
    sibling_b.update(
        {
            "runtime_context_id": "mfrctx-non-atomic-b",
            "task_id": "non-atomic-b",
            "parent_task_id": execution_id,
            "lane_id": "non-atomic-b",
            "worker_slot_id": "non-atomic-b",
            "line_instance_id": "runtime_context:mfrctx-non-atomic-b",
            "payload": {
                "runtime_context_id": "mfrctx-non-atomic-b",
                "task_id": "non-atomic-b",
                "read_receipt_hash": "sha256:non-atomic-b-read",
            },
        }
    )
    before = runtime.store.get(execution_id)
    rejected = runtime.submit_line_write(execution_id, sibling_b)
    assert rejected["ok"] is False
    assert {
        "field": "runtime_context_id",
        "expected": "mfrctx-non-atomic-a",
        "actual": "mfrctx-non-atomic-b",
    } in rejected["decision"]["imported_legacy_checks"][0][
        "identity_mismatches"
    ]
    assert runtime.store.get(execution_id) == before


@pytest.mark.parametrize(
    ("workers", "requested_index"),
    [
        (
            [
                {
                    "runtime_context_id": "mfrctx-missing-worker-id-a",
                    "task_id": "missing-worker-id-a",
                    "parent_task_id": "cex-malformed-atomic-dispatch",
                    "lane_id": "missing-worker-id-a",
                    "worker_slot_id": "missing-worker-id-a",
                },
                {
                    "runtime_context_id": "mfrctx-missing-worker-id-b",
                    "task_id": "missing-worker-id-b",
                    "parent_task_id": "cex-malformed-atomic-dispatch",
                    "lane_id": "missing-worker-id-b",
                    "worker_slot_id": "missing-worker-id-b",
                },
            ],
            1,
        ),
        (
            [
                {
                    "runtime_context_id": f"mfrctx-three-worker-{index}",
                    "task_id": f"three-worker-{index}",
                    "parent_task_id": "cex-malformed-atomic-dispatch",
                    "lane_id": f"three-worker-{index}",
                    "worker_slot_id": f"three-worker-{index}",
                    "worker_id": f"three-worker-{index}",
                }
                for index in range(3)
            ],
            2,
        ),
    ],
)
def test_mf_parallel_malformed_atomic_dispatch_keeps_global_lane_order(
    workers,
    requested_index,
):
    runtime = ContractRuntime(ContractCrudService().registry)
    execution_id = "cex-malformed-atomic-dispatch"
    record = _start_mf_parallel_successor(
        runtime,
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-MALFORMED-ATOMIC-DISPATCH",
        contract_execution_id=execution_id,
        route_token_ref="rtok-malformed-atomic-dispatch",
    )
    record = runtime.submit_line_write(
        execution_id,
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="orchestration",
            line_id="observer_prefill_child_contracts",
        ),
    )["record"]
    dispatch = _runtime_write_from(
        record,
        actor_role="observer",
        stage_id="dispatch",
        line_id="observer_dispatch_bounded_workers",
    )
    dispatch["payload"] = {
        "worker_count": len(workers),
        "required_worker_count": len(workers),
        "atomic_dispatch": True,
        "all_or_nothing": True,
        "workers": workers,
    }
    dispatched = runtime.submit_line_write(execution_id, dispatch)
    assert dispatched["ok"] is True
    record = dispatched["record"]
    requested = workers[requested_index]
    sibling_write = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
    )
    sibling_write.update(
        {
            **requested,
            "worker_role": "mf_sub",
            "line_instance_id": (
                f"runtime_context:{requested['runtime_context_id']}"
            ),
            "payload": {
                **requested,
                "worker_role": "mf_sub",
                "read_receipt_hash": "sha256:malformed-atomic-sibling",
            },
        }
    )
    before = runtime.store.get(execution_id)
    rejected = runtime.precheck_line_write(execution_id, sibling_write)
    assert rejected["ok"] is False
    assert rejected["record"]["runtime_guide"].get(
        "atomic_lane_gate_binding"
    ) is None
    after = runtime.store.get(execution_id)
    assert after["execution_state_revision"] == before[
        "execution_state_revision"
    ]
    assert after["completed_lines"] == before["completed_lines"]


def test_default_registry_exposes_contract_add_definition_and_runtime_path():
    service = ContractCrudService()

    result = service.read("contract_add.v1")
    assert result["ok"] is True
    definition = result["data"]["definition"]
    assert definition["contract_id"] == "contract_add"
    assert definition["role"] == "observer"
    assert definition["contract_type"] == "contract_add"
    assert definition["compat_aliases"] == ["contract_add.v1", "add_contract.v1"]

    read_model = definition["read_model"]
    _assert_explicit_system_layer(definition, allow_root_start=False)
    assert read_model["allowed_writer_roles"] == ["observer", "mf_sub", "qa"]
    assert [
        (line["stage_id"], line["line_id"], line["owner_role"], line["evidence_kind"])
        for line in read_model["rule_lines"]
    ] == [
        (
            "observer_request",
            "observer_request_contract_add",
            "observer",
            "contract_add_request",
        ),
        (
            "worker_precheck",
            "worker_draft_precheck",
            "mf_sub",
            "contract_draft_precheck",
        ),
        (
            "worker_source",
            "worker_source_or_adoption_proof",
            "mf_sub",
            "contract_source_or_adoption_proof",
        ),
        (
            "worker_runtime_visibility",
            "worker_runtime_visibility_proof",
            "mf_sub",
            "contract_runtime_visibility",
        ),
        (
            "worker_asset_binding",
            "worker_asset_binding_proposal_or_waiver",
            "mf_sub",
            "asset_binding_proposal_or_waiver",
        ),
        ("qa", "qa_independent_verification", "qa", "independent_verification"),
        (
            "observer_accept",
            "observer_accept_contract_add",
            "observer",
            "contract_add_accept",
        ),
        ("observer_accept", "observer_close_ready", "observer", "close_ready"),
    ]
    assert "generic CRUD remains internal" in definition["instruction_layer"]["inline"][1]

    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "contract_add",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-ADD-PARALLEL-DOGFOOD-20260624",
        contract_execution_id="cex-contract-add-runtime-path-test",
        actor_role="observer",
        route_token_ref="rtok-contract-add-test",
    )
    assert record["runtime_guide"]["next_legal_action"] == {
        "stage_id": "observer_request",
        "line_id": "observer_request_contract_add",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "evidence_kind": "contract_add_request",
        "required": True,
    }

    record = runtime.submit_line_write(
        "cex-contract-add-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="observer_request",
            line_id="observer_request_contract_add",
        ),
    )["record"]

    rejected_observer_worker_evidence = runtime.submit_line_write(
        "cex-contract-add-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="worker_precheck",
            line_id="worker_draft_precheck",
        ),
    )
    assert rejected_observer_worker_evidence["ok"] is False
    assert "cannot write line" in rejected_observer_worker_evidence["decision"]["errors"][0]

    for stage_id, line_id in [
        ("worker_precheck", "worker_draft_precheck"),
        ("worker_source", "worker_source_or_adoption_proof"),
        ("worker_runtime_visibility", "worker_runtime_visibility_proof"),
        ("worker_asset_binding", "worker_asset_binding_proposal_or_waiver"),
    ]:
        runtime.current_guide("cex-contract-add-runtime-path-test", actor_role="mf_sub")
        record = runtime.store.get("cex-contract-add-runtime-path-test")
        accepted = runtime.submit_line_write(
            "cex-contract-add-runtime-path-test",
            _runtime_write_from(
                record,
                actor_role="mf_sub",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
        record = accepted["record"]

    runtime.current_guide("cex-contract-add-runtime-path-test", actor_role="observer")
    record = runtime.store.get("cex-contract-add-runtime-path-test")
    rejected_observer_qa_evidence = runtime.submit_line_write(
        "cex-contract-add-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )
    assert rejected_observer_qa_evidence["ok"] is False
    assert "cannot write line" in rejected_observer_qa_evidence["decision"]["errors"][0]

    runtime.current_guide("cex-contract-add-runtime-path-test", actor_role="qa")
    record = runtime.store.get("cex-contract-add-runtime-path-test")
    record = runtime.submit_line_write(
        "cex-contract-add-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )["record"]

    for stage_id, line_id in [
        ("observer_accept", "observer_accept_contract_add"),
        ("observer_accept", "observer_close_ready"),
    ]:
        runtime.current_guide("cex-contract-add-runtime-path-test", actor_role="observer")
        record = runtime.store.get("cex-contract-add-runtime-path-test")
        accepted = runtime.submit_line_write(
            "cex-contract-add-runtime-path-test",
            _runtime_write_from(
                record,
                actor_role="observer",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
        record = accepted["record"]

    assert record["runtime_guide"]["next_legal_action"] is None


def test_default_registry_exposes_contract_update_definition_and_runtime_path():
    service = ContractCrudService()

    result = service.read("contract_update.v1")
    assert result["ok"] is True
    definition = result["data"]["definition"]
    assert definition["contract_id"] == "contract_update"
    assert definition["role"] == "observer"
    assert definition["contract_type"] == "contract_update"
    assert definition["revision"] == "rev2"
    assert definition["compat_aliases"] == ["contract_update.v1", "update_contract.v1"]

    read_model = definition["read_model"]
    _assert_explicit_system_layer(definition, allow_root_start=False)
    assert read_model["allowed_writer_roles"] == ["observer", "mf_sub", "qa"]
    assert [
        (line["stage_id"], line["line_id"], line["owner_role"], line["evidence_kind"])
        for line in read_model["rule_lines"]
    ] == [
        (
            "observer_request",
            "observer_request_contract_update",
            "observer",
            "contract_update_request",
        ),
        (
            "dispatch",
            "observer_dispatch_bounded_workers",
            "observer",
            "dispatch_bounded_worker",
        ),
        (
            "worker_read",
            "worker_read_runtime_guide",
            "mf_sub",
            "read_receipt",
        ),
        (
            "worker_startup",
            "worker_startup",
            "mf_sub",
            "mf_subagent_startup",
        ),
        (
            "worker_context",
            "worker_graph_context",
            "mf_sub",
            "graph_trace",
        ),
        (
            "worker_previous_source",
            "worker_previous_source_proof",
            "mf_sub",
            "contract_previous_source_proof",
        ),
        (
            "worker_precheck",
            "worker_revision_precheck",
            "mf_sub",
            "contract_revision_precheck",
        ),
        (
            "worker_source",
            "worker_revision_source_proof",
            "mf_sub",
            "contract_revision_source_proof",
        ),
        (
            "worker_runtime_visibility",
            "worker_runtime_visibility_proof",
            "mf_sub",
            "contract_update_runtime_visibility",
        ),
        (
            "worker_asset_binding",
            "worker_asset_binding_proposal_or_waiver",
            "mf_sub",
            "asset_binding_proposal_or_waiver",
        ),
        ("qa", "qa_independent_verification", "qa", "independent_verification"),
        (
            "observer_accept",
            "observer_accept_contract_update",
            "observer",
            "contract_update_accept",
        ),
        ("observer_accept", "observer_close_ready", "observer", "close_ready"),
    ]
    assert "same-revision active semantic mutation is invalid" in (
        definition["instruction_layer"]["inline"][5]
    )
    assert "query the graph with its bounded identity" in (
        definition["instruction_layer"]["inline"][6]
    )

    explicit_rev1 = service.read("contract_update.v1", revision="rev1")
    assert explicit_rev1["ok"] is True
    rev1_lines = explicit_rev1["data"]["definition"]["read_model"]["rule_lines"]
    assert rev1_lines[1]["line_id"] == "worker_previous_source_proof"
    assert all(
        line["line_id"] != "observer_dispatch_bounded_workers"
        for line in rev1_lines
    )

    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "contract_update",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-SYSTEM-CRUD-REGISTRY-MIN-PATH-20260623",
        contract_execution_id="cex-contract-update-runtime-path-test",
        actor_role="observer",
        route_token_ref="rtok-contract-update-test",
    )
    assert record["definition_source_sha256"].startswith("sha256:")
    assert record["runtime_guide"]["next_legal_action"] == {
        "stage_id": "observer_request",
        "line_id": "observer_request_contract_update",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "evidence_kind": "contract_update_request",
        "required": True,
    }

    record = runtime.submit_line_write(
        "cex-contract-update-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="observer_request",
            line_id="observer_request_contract_update",
        ),
    )["record"]

    record = runtime.submit_line_write(
        "cex-contract-update-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="dispatch",
            line_id="observer_dispatch_bounded_workers",
        ),
    )["record"]

    rejected_observer_worker_evidence = runtime.submit_line_write(
        "cex-contract-update-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="worker_read",
            line_id="worker_read_runtime_guide",
        ),
    )
    assert rejected_observer_worker_evidence["ok"] is False
    assert "cannot write line" in rejected_observer_worker_evidence["decision"]["errors"][0]

    for stage_id, line_id in [
        ("worker_read", "worker_read_runtime_guide"),
        ("worker_startup", "worker_startup"),
        ("worker_context", "worker_graph_context"),
        ("worker_previous_source", "worker_previous_source_proof"),
        ("worker_precheck", "worker_revision_precheck"),
        ("worker_source", "worker_revision_source_proof"),
        ("worker_runtime_visibility", "worker_runtime_visibility_proof"),
        ("worker_asset_binding", "worker_asset_binding_proposal_or_waiver"),
    ]:
        runtime.current_guide(
            "cex-contract-update-runtime-path-test",
            actor_role="mf_sub",
        )
        record = runtime.store.get("cex-contract-update-runtime-path-test")
        write = _runtime_write_from(
            record,
            actor_role="mf_sub",
            stage_id=stage_id,
            line_id=line_id,
        )
        if line_id == "worker_read_runtime_guide":
            write["read_receipt_hash"] = "sha256:" + ("a" * 64)
        if line_id == "worker_graph_context":
            graph_trace_id = "gqt-contract-update-runtime-path"
            graph_identity = {
                "runtime_context_id": "mfrctx-contract-update-runtime-path",
                "task_id": "contract-update-runtime-path-worker",
                "parent_task_id": "cex-contract-update-runtime-path-test",
                "target_project_root": "/tmp/contract-update-runtime-path",
                "worker_role": "mf_sub",
            }
            write.update(graph_identity)
            write["graph_trace_ids"] = [graph_trace_id]
            write["graph_trace_evidence"] = {
                **graph_identity,
                "db_verified": True,
                "graph_trace_ids": [graph_trace_id],
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
            }
        accepted = runtime.submit_line_write(
            "cex-contract-update-runtime-path-test",
            write,
        )
        assert accepted["ok"] is True, (
            stage_id,
            line_id,
            accepted["decision"]["errors"],
        )
        record = accepted["record"]

    runtime.current_guide("cex-contract-update-runtime-path-test", actor_role="observer")
    record = runtime.store.get("cex-contract-update-runtime-path-test")
    rejected_observer_qa_evidence = runtime.submit_line_write(
        "cex-contract-update-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )
    assert rejected_observer_qa_evidence["ok"] is False
    assert "cannot write line" in rejected_observer_qa_evidence["decision"]["errors"][0]

    runtime.current_guide("cex-contract-update-runtime-path-test", actor_role="qa")
    record = runtime.store.get("cex-contract-update-runtime-path-test")
    record = runtime.submit_line_write(
        "cex-contract-update-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_independent_verification",
        ),
    )["record"]

    for stage_id, line_id in [
        ("observer_accept", "observer_accept_contract_update"),
        ("observer_accept", "observer_close_ready"),
    ]:
        runtime.current_guide(
            "cex-contract-update-runtime-path-test",
            actor_role="observer",
        )
        record = runtime.store.get("cex-contract-update-runtime-path-test")
        accepted = runtime.submit_line_write(
            "cex-contract-update-runtime-path-test",
            _runtime_write_from(
                record,
                actor_role="observer",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
        record = accepted["record"]

    assert record["runtime_guide"]["next_legal_action"] is None


def test_contract_add_worker_asset_binding_payload_is_visible_to_qa():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "contract_add",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-LINE-WRITE-EVIDENCE-PERSISTENCE-20260625",
        contract_execution_id="cex-contract-add-evidence-payload-test",
        actor_role="observer",
        route_token_ref="rtok-contract-add-evidence-test",
    )

    record = runtime.submit_line_write(
        "cex-contract-add-evidence-payload-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="observer_request",
            line_id="observer_request_contract_add",
        ),
    )["record"]

    for stage_id, line_id in [
        ("worker_precheck", "worker_draft_precheck"),
        ("worker_source", "worker_source_or_adoption_proof"),
        ("worker_runtime_visibility", "worker_runtime_visibility_proof"),
    ]:
        runtime.current_guide("cex-contract-add-evidence-payload-test", actor_role="mf_sub")
        record = runtime.store.get("cex-contract-add-evidence-payload-test")
        record = runtime.submit_line_write(
            "cex-contract-add-evidence-payload-test",
            _runtime_write_from(
                record,
                actor_role="mf_sub",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )["record"]

    runtime.current_guide("cex-contract-add-evidence-payload-test", actor_role="mf_sub")
    record = runtime.store.get("cex-contract-add-evidence-payload-test")
    asset_binding_write = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_asset_binding",
        line_id="worker_asset_binding_proposal_or_waiver",
    )
    asset_binding_write.update(
        {
            "payload": {
                "binding_status": "waived",
                "proposed_bindings": [],
                "waiver": {"direct_trusted_graph_db_write": False},
                "route_token_ref": "rtok-copy-safe-ref",
            },
            "artifact_refs": [],
            "trace_id": "gqt-20260625-contract-add-evidence",
            "commit_sha": "dd6a55b55e88338fd0586d3e75ed66c5100cf1e1",
        }
    )
    accepted = runtime.submit_line_write(
        "cex-contract-add-evidence-payload-test",
        asset_binding_write,
    )

    assert accepted["ok"] is True
    record = accepted["record"]
    asset_line = next(
        line
        for line in record["completed_lines"]
        if line["line_id"] == "worker_asset_binding_proposal_or_waiver"
    )
    assert asset_line["payload"] == {
        "binding_status": "waived",
        "proposed_bindings": [],
        "waiver": {"direct_trusted_graph_db_write": False},
        "route_token_ref": "rtok-copy-safe-ref",
    }
    assert asset_line["artifact_refs"] == []
    assert asset_line["trace_id"] == "gqt-20260625-contract-add-evidence"
    assert asset_line["commit_sha"] == "dd6a55b55e88338fd0586d3e75ed66c5100cf1e1"

    qa_guide = runtime.current_guide(
        "cex-contract-add-evidence-payload-test",
        actor_role="qa",
    )
    qa_asset_line = next(
        line
        for line in qa_guide["completed_lines"]
        if line["line_id"] == "worker_asset_binding_proposal_or_waiver"
    )
    assert qa_asset_line["payload"] == asset_line["payload"]
    assert qa_asset_line["artifact_refs"] == asset_line["artifact_refs"]
    assert qa_asset_line["payload"]["proposed_bindings"] == []
    assert qa_asset_line["payload"]["waiver"] == {
        "direct_trusted_graph_db_write": False
    }
    assert qa_guide["next_legal_action"]["line_id"] == "qa_independent_verification"


def test_contract_runtime_line_write_sanitizes_raw_token_evidence():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "contract_add",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-LINE-WRITE-EVIDENCE-PERSISTENCE-20260625",
        contract_execution_id="cex-contract-add-token-sanitization-test",
        actor_role="observer",
        route_token_ref="rtok-contract-add-sanitization-test",
    )
    write = _runtime_write_from(
        record,
        actor_role="observer",
        stage_id="observer_request",
        line_id="observer_request_contract_add",
    )
    write.update(
        {
            "payload": {
                "request": "add contract_add fixture",
                "route_token_ref": "rtok-safe-ref",
                "token_ref": "role-assignment-token-ref",
                "route_token": {"raw": "raw-route-token-secret"},
                "session_token": "raw-session-token-secret",
                "token": "raw-generic-token-secret",
                "tokens": ["raw-generic-token-list-secret"],
                "nested": {
                    "governance_token": "raw-governance-token-secret",
                    "keep": "visible",
                },
            },
            "artifact_refs": [
                {
                    "path": "agent/governance/contracts/contract_add.v1.rev1.json",
                    "session_token": "artifact-session-token-secret",
                }
            ],
            "trace_id": "gqt-20260625-token-sanitization",
            "commit_sha": "dd6a55b55e88338fd0586d3e75ed66c5100cf1e1",
            "session_token": "top-level-session-token-secret",
            "route_token": {"raw": "top-level-route-token-secret"},
        }
    )

    result = runtime.submit_line_write(
        "cex-contract-add-token-sanitization-test",
        write,
    )

    assert result["ok"] is True
    line = result["record"]["completed_lines"][0]
    assert line["payload"]["route_token_ref"] == "rtok-safe-ref"
    assert line["payload"]["token_ref"] == "role-assignment-token-ref"
    assert line["payload"]["nested"] == {"keep": "visible"}
    assert line["artifact_refs"] == [
        {"path": "agent/governance/contracts/contract_add.v1.rev1.json"}
    ]
    serialized_line = json.dumps(line, sort_keys=True)
    assert "raw-route-token-secret" not in serialized_line
    assert "raw-session-token-secret" not in serialized_line
    assert "raw-governance-token-secret" not in serialized_line
    assert "raw-generic-token-secret" not in serialized_line
    assert "raw-generic-token-list-secret" not in serialized_line
    assert "artifact-session-token-secret" not in serialized_line
    assert "top-level-session-token-secret" not in serialized_line
    assert "top-level-route-token-secret" not in serialized_line
    assert "route_token" not in line["payload"]
    assert "session_token" not in line["payload"]
    assert "token" not in line["payload"]
    assert "tokens" not in line["payload"]
    assert "governance_token" not in line["payload"]["nested"]


def test_contract_runtime_line_write_precheck_does_not_append_evidence():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-LINE-WRITE-DRY-RUN-PRECHECK-20260625",
        contract_execution_id="cex-hotfix-line-write-precheck-test",
        actor_role="observer",
        route_token_ref="rtok-hotfix-line-write-precheck-test",
    )
    pre_reason = _runtime_write_from(
        record,
        actor_role="observer",
        stage_id="pre_mutation",
        line_id="hotfix_pre_reason",
    )

    precheck = runtime.precheck_line_write(
        "cex-hotfix-line-write-precheck-test",
        pre_reason,
    )

    assert precheck["ok"] is True
    assert precheck["would_mutate_completed_lines"] is False
    assert precheck["completed_lines_count"] == 0
    assert precheck["record"]["completed_lines"] == []
    assert runtime.store.get("cex-hotfix-line-write-precheck-test")[
        "completed_lines"
    ] == []

    result = runtime.submit_line_write(
        "cex-hotfix-line-write-precheck-test",
        pre_reason,
    )
    assert result["ok"] is True
    assert len(result["record"]["completed_lines"]) == 1


def test_contract_runtime_submit_line_rejects_no_mutation_expected_payload():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-LINE-WRITE-DRY-RUN-PRECHECK-20260625",
        contract_execution_id="cex-hotfix-no-mutation-submit-test",
        actor_role="observer",
        route_token_ref="rtok-hotfix-no-mutation-submit-test",
    )
    write = _runtime_write_from(
        record,
        actor_role="observer",
        stage_id="pre_mutation",
        line_id="hotfix_pre_reason",
    )
    write["payload"] = {"status": "diagnostic_probe_no_mutation_expected"}

    result = runtime.submit_line_write(
        "cex-hotfix-no-mutation-submit-test",
        write,
    )

    assert result["ok"] is False
    assert result["decision"]["errors"] == [
        "line_write_declares_no_mutation_expected"
    ]
    assert result["record"]["completed_lines"] == []
    assert runtime.store.get("cex-hotfix-no-mutation-submit-test")[
        "completed_lines"
    ] == []


def test_mf_parallel_read_only_adoption_proves_source_hash_and_duplicate_rejected():
    service = ContractCrudService()
    before_paths = service.registry.definition_paths()
    before_count = len(before_paths)

    adopted = service.read("mf_parallel.v1")["data"]["definition"]
    source_path = Path(adopted["_source_path"])
    raw_sha256 = "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()

    assert source_path.name == "mf_parallel.v1.rev2.json"
    assert adopted["compat_aliases"] == ["mf_parallel.v1", "parallel_worker.v1"]
    assert adopted["definition_hash"].startswith("sha256:")

    duplicate = service.create(json.loads(source_path.read_text(encoding="utf-8")))

    assert duplicate["ok"] is False
    assert duplicate["error"]["type"] == "ContractLifecycleError"
    assert "already exists" in duplicate["error"]["message"]
    assert len(service.registry.definition_paths()) == before_count
    assert source_path in before_paths
    assert raw_sha256 == "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()


def test_contract_runtime_blocks_legacy_meta_contract_as_primary_root():
    runtime = ContractRuntime(ContractCrudService().registry)

    with pytest.raises(ContractRuntimeError, match="legacy_contract_route_blocked"):
        runtime.start_execution(
            "meta_contract.v1",
            project_id="aming-claw",
            backlog_id="AC-CONTRACT-RUNTIME-LEGACY-ENTRY-BLOCK-20260625",
            actor_role="observer",
            contract_execution_id="cex-meta-contract-should-not-start",
        )


def test_default_registry_exposes_onboarding_and_hotfix_successor_contracts():
    service = ContractCrudService()

    onboarding = service.read("onboard_contract.v1")
    assert onboarding["ok"] is True
    onboard_definition = onboarding["data"]["definition"]
    assert onboard_definition["contract_id"] == "onboard_contract"
    assert onboard_definition["revision"] == "rev3"
    assert onboard_definition["contract_type"] == "observer_onboarding"
    _assert_explicit_system_layer(onboard_definition, allow_root_start=True)
    assert onboard_definition["successors"] == [
        {
            "contract_id": "observer_hotfix",
            "reason": "Successor hotfix execution after observer onboarding is complete.",
            "version": "v1",
        },
        {
            "contract_id": "mf_parallel",
            "reason": (
                "Observer-owned parallel worker orchestration successor after "
                "observer onboarding is complete."
            ),
            "version": "v1",
        },
        {
            "contract_id": "contract_update",
            "reason": (
                "Source-backed contract revision successor after observer "
                "onboarding is complete."
            ),
            "version": "v1",
        }
    ]
    explicit_rev1 = service.read("onboard_contract.v1", revision="rev1")
    assert explicit_rev1["ok"] is True
    assert explicit_rev1["data"]["definition"]["successors"] == [
        {
            "contract_id": "observer_hotfix",
            "reason": "Successor hotfix execution after observer onboarding is complete.",
            "version": "v1",
        }
    ]
    assert [
        (line["stage_id"], line["line_id"], line["owner_role"], line["evidence_kind"])
        for line in onboard_definition["read_model"]["rule_lines"]
    ] == [
        (
            "graph_context",
            "graph_query_schema_trace",
            "observer",
            "graph_query_schema_trace",
        ),
        (
            "backlog_review",
            "related_backlog_review",
            "observer",
            "related_backlog_review",
        ),
        (
            "runtime_state",
            "observer_root_route_context_read",
            "observer",
            "observer_root_route_context_read",
        ),
        (
            "runtime_state",
            "contract_state_projection_read",
            "observer",
            "contract_state_projection_read",
        ),
        ("route_binding", "route_context", "observer", "route_context"),
        (
            "route_binding",
            "route_action_precheck",
            "observer",
            "route_action_precheck",
        ),
        (
            "work_mode",
            "observer_work_mode_transition",
            "observer",
            "observer_work_mode_transition",
        ),
    ]

    runtime = ContractRuntime(service.registry)
    onboard_record = runtime.start_execution(
        "onboard_contract",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-HOTFIX-CUTOVER-HANDOFF-20260624",
        contract_execution_id="cex-onboard-default-registry-test",
        actor_role="observer",
        route_token_ref="rtok-onboard-test",
    )
    assert onboard_record["runtime_guide"]["next_legal_action"]["line_id"] == (
        "graph_query_schema_trace"
    )
    assert onboard_record["revision"] == "rev3"
    assert onboard_record["definition_source_sha256"].startswith("sha256:")

    hotfix = service.read("observer_hotfix_direct_mutation.v1")
    assert hotfix["ok"] is True
    hotfix_definition = hotfix["data"]["definition"]
    assert hotfix_definition["contract_id"] == "observer_hotfix"
    _assert_explicit_system_layer(hotfix_definition, allow_root_start=False)
    assert [
        (line["stage_id"], line["line_id"], line["owner_role"], line["evidence_kind"])
        for line in hotfix_definition["read_model"]["rule_lines"]
    ] == [
        ("pre_mutation", "hotfix_pre_reason", "observer", "hotfix_entered"),
        (
            "mutation",
            "hotfix_post_action_summary",
            "observer",
            "hotfix_under_action",
        ),
        (
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
        ),
        ("observer_close", "observer_close_ready", "observer", "close_ready"),
    ]

    hotfix_record = runtime.start_execution(
        "observer_hotfix",
        project_id="aming-claw",
        backlog_id="AC-CONTRACT-RUNTIME-HOTFIX-CUTOVER-HANDOFF-20260624",
        contract_execution_id="cex-hotfix-default-registry-test",
        actor_role="observer",
        parent_contract_execution_id="cex-onboard-default-registry-test",
        root_contract_execution_id="cex-onboard-default-registry-test",
        contract_chain_id="cchain-default-registry-test",
        route_token_ref="rtok-onboard-test",
    )
    assert hotfix_record["parent_contract_execution_id"] == (
        "cex-onboard-default-registry-test"
    )
    assert hotfix_record["contract_chain_id"] == "cchain-default-registry-test"
    assert hotfix_record["runtime_guide"]["next_legal_action"] == {
        "stage_id": "pre_mutation",
        "line_id": "hotfix_pre_reason",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "evidence_kind": "hotfix_entered",
        "required": True,
    }
