from __future__ import annotations

import json
from pathlib import Path

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


def _runtime_write_from(record, *, actor_role: str, stage_id: str, line_id: str):
    state = record["execution_state"]
    guide = record["runtime_guide"]
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
