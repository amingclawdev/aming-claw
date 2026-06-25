from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
)
from agent.governance.contracts.runtime import (
    ContractRuntimeError,
    StalePinnedContractExecutionError,
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
        "evidence_kind": next_action.get("evidence_kind") or "",
    }


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
        {"contract_id": "onboard_contract", "version": "v1"}
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
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-CLAUDE-PARALLEL-CLOSE-RECONCILE-GUIDE-GAP-20260623",
        contract_execution_id="cex-mf-parallel-runtime-path-test",
        actor_role="observer",
        route_token_ref="rtok-test",
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


def test_mf_parallel_runtime_binds_worker_lines_to_runtime_context_instances():
    service = ContractCrudService()
    runtime = ContractRuntime(service.registry)
    record = runtime.start_execution(
        "mf_parallel.v1",
        project_id="aming-claw",
        backlog_id="AC-MF-PARALLEL-LANE-BOUND-LINES-BLOCK-20260625",
        contract_execution_id="cex-mf-parallel-lane-bound-test",
        actor_role="observer",
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
        "workers": [
            {
                "runtime_context_id": "mfrctx-impl-core",
                "task_id": "mfsub-impl-core",
                "parent_task_id": "cex-mf-parallel-lane-bound-test",
                "lane_id": "impl-core",
                "worker_slot_id": "impl-core",
            },
            {
                "runtime_context_id": "mfrctx-trace-observability",
                "task_id": "mfsub-trace-observability",
                "parent_task_id": "cex-mf-parallel-lane-bound-test",
                "lane_id": "trace-observability",
                "worker_slot_id": "trace-observability",
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

    first_read = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
    )
    first_read.update(
        {
            "runtime_context_id": "mfrctx-impl-core",
            "task_id": "mfsub-impl-core",
            "parent_task_id": "cex-mf-parallel-lane-bound-test",
            "worker_role": "mf_sub",
            "lane_id": "impl-core",
            "payload": {
                "runtime_context_id": "mfrctx-impl-core",
                "task_id": "mfsub-impl-core",
                "read_receipt_hash": "sha256:impl-read",
            },
        }
    )
    accepted_first_read = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        first_read,
    )
    assert accepted_first_read["ok"] is True
    record = accepted_first_read["record"]
    read_line = next(
        line
        for line in record["completed_lines"]
        if line.get("line_id") == "worker_read_runtime_guide"
    )
    assert read_line["runtime_context_id"] == "mfrctx-impl-core"
    assert read_line["line_instance_id"] == "runtime_context:mfrctx-impl-core"
    next_action = record["runtime_guide"]["next_legal_action"]
    assert next_action["line_id"] == "worker_read_runtime_guide"
    assert next_action["runtime_context_id"] == "mfrctx-trace-observability"

    wrong_startup = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_startup",
        line_id="worker_startup",
    )
    wrong_startup.update(
        {
            "evidence_kind": "mf_subagent_startup",
            "runtime_context_id": "mfrctx-impl-core",
            "task_id": "mfsub-impl-core",
            "parent_task_id": "cex-mf-parallel-lane-bound-test",
            "worker_role": "mf_sub",
        }
    )
    rejected_startup = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        wrong_startup,
    )
    assert rejected_startup["ok"] is False
    assert any(
        "write does not match next legal action" in error
        for error in rejected_startup["decision"]["errors"]
    )
    assert "runtime_context_id does not match next legal action" in (
        rejected_startup["decision"]["errors"]
    )

    second_read = _runtime_write_from(
        record,
        actor_role="mf_sub",
        stage_id="worker_read",
        line_id="worker_read_runtime_guide",
    )
    second_read.update(
        {
            "runtime_context_id": "mfrctx-trace-observability",
            "task_id": "mfsub-trace-observability",
            "parent_task_id": "cex-mf-parallel-lane-bound-test",
            "worker_role": "mf_sub",
            "lane_id": "trace-observability",
        }
    )
    accepted_second_read = runtime.submit_line_write(
        "cex-mf-parallel-lane-bound-test",
        second_read,
    )
    assert accepted_second_read["ok"] is True
    record = accepted_second_read["record"]
    next_action = record["runtime_guide"]["next_legal_action"]
    assert next_action["line_id"] == "worker_startup"
    assert next_action["runtime_context_id"] == "mfrctx-impl-core"


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

    rejected_observer_worker_evidence = runtime.submit_line_write(
        "cex-contract-update-runtime-path-test",
        _runtime_write_from(
            record,
            actor_role="observer",
            stage_id="worker_previous_source",
            line_id="worker_previous_source_proof",
        ),
    )
    assert rejected_observer_worker_evidence["ok"] is False
    assert "cannot write line" in rejected_observer_worker_evidence["decision"]["errors"][0]

    for stage_id, line_id in [
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
        accepted = runtime.submit_line_write(
            "cex-contract-update-runtime-path-test",
            _runtime_write_from(
                record,
                actor_role="mf_sub",
                stage_id=stage_id,
                line_id=line_id,
            ),
        )
        assert accepted["ok"] is True
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
