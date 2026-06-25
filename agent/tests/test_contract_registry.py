from __future__ import annotations

import json

import pytest

from agent.governance.contracts import (
    ContractDefinitionError,
    ContractDefinitionRegistry,
    ContractLifecycleError,
    build_execution_state,
    compile_runtime_guide,
    is_new_execution_allowed,
    resolve_instruction_bundle,
    validate_contract_write,
)
from agent.governance.contracts.hash import file_sha256
from agent.governance.contracts.registry import UnknownContractDefinitionError


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


def _write_definition(root, payload, name="observer_hotfix.v1.rev1.json"):
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_registry_loads_definition_with_hash_and_alias(tmp_path):
    _write_definition(tmp_path, _definition())
    (tmp_path / "legacy-template.v1.json").write_text(
        json.dumps({"schema_version": "observer_onboard_contract_template.v1"}),
        encoding="utf-8",
    )
    (tmp_path / "ignored.schema.json").write_text("{}", encoding="utf-8")

    registry = ContractDefinitionRegistry(tmp_path)
    definitions = registry.list_definitions()
    by_alias = registry.get("observer_hotfix_direct_mutation.v1")

    assert [item["contract_id"] for item in definitions] == ["observer_hotfix"]
    assert by_alias["definition_hash"].startswith("sha256:")
    assert by_alias["rule_layer"]["stages"][1]["lines"][0]["allowed_writer_roles"] == ["qa"]
    assert is_new_execution_allowed(by_alias) is True


def test_registry_exposes_system_layer_legacy_default_read_model(tmp_path):
    _write_definition(tmp_path, _definition())

    definition = ContractDefinitionRegistry(tmp_path).get("observer_hotfix")
    read_model = definition["read_model"]

    assert read_model["system_layer_policy_status"] == {
        "schema_version": "contract_system_layer_policy_status.v1",
        "status": "legacy_default_deny",
        "explicit": False,
        "defaulted": True,
        "deny_by_default": True,
        "missing_policies": [
            "entrypoint_policy",
            "successor_policy",
            "write_authority_policy",
            "next_action_policy",
            "projection_policy",
            "route_policy",
            "authority_policy",
            "graph_binding_policy",
        ],
        "defaulted_policies": [
            "entrypoint_policy",
            "successor_policy",
            "write_authority_policy",
            "next_action_policy",
            "projection_policy",
            "route_policy",
            "authority_policy",
            "graph_binding_policy",
        ],
        "explicit_policies": [],
    }
    assert read_model["system_layer"]["entrypoint_policy"] == {
        "schema_version": "contract_system_policy.v1",
        "policy_name": "entrypoint_policy",
        "policy_status": "legacy_default_deny",
        "defaulted": True,
        "deny_by_default": True,
        "allowed": False,
    }


def test_registry_normalizes_explicit_system_layer_read_model(tmp_path):
    payload = _definition(
        system_layer={
            "entrypoint_policy": {
                "allow_root_start": True,
                "allowed_entrypoints": ["observer_hotfix"],
            },
            "route_policy": {
                "route_token_ref_required": True,
            },
        }
    )

    definition = ContractDefinitionRegistry(tmp_path).validate_payload(payload)
    system_layer = definition["read_model"]["system_layer"]

    assert system_layer["entrypoint_policy"]["policy_status"] == "explicit"
    assert system_layer["entrypoint_policy"]["allow_root_start"] is True
    assert system_layer["route_policy"]["route_token_ref_required"] is True
    assert definition["read_model"]["system_layer_policy_status"]["status"] == (
        "partial_default_deny"
    )
    assert "successor_policy" in definition["read_model"]["system_layer_policy_status"][
        "defaulted_policies"
    ]


def test_default_registry_migrated_definitions_expose_explicit_system_layer():
    registry = ContractDefinitionRegistry()

    expected_root_policy = {
        "onboard_contract": True,
        "observer_hotfix": False,
        "contract_add": False,
        "mf_parallel": False,
    }
    for contract_id, allow_root_start in expected_root_policy.items():
        definition = registry.get(contract_id)
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
        assert (
            system_layer["write_authority_policy"]["body_supplied_role_claims_trusted"]
            is False
        )

    onboard = registry.get("onboard_contract")["read_model"]["system_layer"]
    assert onboard["successor_policy"]["allowed_successors"] == [
        {"contract_id": "observer_hotfix", "version": "v1"}
    ]
    hotfix = registry.get("observer_hotfix")["read_model"]["system_layer"]
    assert hotfix["successor_policy"]["allowed_parent_contracts"] == [
        {"contract_id": "onboard_contract", "version": "v1"}
    ]


def test_registry_rejects_non_object_system_layer(tmp_path):
    with pytest.raises(ContractDefinitionError, match="system_layer must be an object"):
        ContractDefinitionRegistry(tmp_path).validate_payload(
            _definition(system_layer=["not-an-object"])
        )


def test_registry_exposes_source_sha_and_load_record(tmp_path):
    path = _write_definition(tmp_path, _definition())

    registry = ContractDefinitionRegistry(tmp_path, loaded_at="2026-06-25T07:12:00Z")
    definition = registry.get("observer_hotfix")
    load_record = definition["definition_load_record"]

    assert definition["source_sha256"] == file_sha256(path)
    assert load_record["load_record_id"].startswith("cdlr-")
    assert load_record["source_path"] == str(path)
    assert load_record["contract_id"] == "observer_hotfix"
    assert load_record["version"] == "v1"
    assert load_record["revision"] == "rev1"
    assert load_record["status"] == "loaded"
    assert load_record["source_sha256"] == definition["source_sha256"]
    assert load_record["definition_hash"] == definition["definition_hash"]
    assert load_record["loaded_at"] == "2026-06-25T07:12:00Z"
    assert load_record["runtime_version"] == "contract_registry.v1"
    assert load_record["drift_status"] == "current"
    assert definition["read_model"]["source_sha256"] == definition["source_sha256"]
    assert definition["read_model"]["definition_load_record"] == load_record
    listed_load_record = registry.list_definitions()[0]["definition_load_record"]
    assert listed_load_record["load_record_id"] == load_record["load_record_id"]
    assert listed_load_record["loaded_at"] == load_record["loaded_at"]


def test_registry_rejects_unsafe_instruction_ref_path(tmp_path):
    payload = _definition(
        instruction_layer={
            "refs": [{"id": "bad", "path": "../outside.md"}],
        }
    )

    with pytest.raises(ContractDefinitionError, match="instruction ref paths"):
        ContractDefinitionRegistry(tmp_path).validate_payload(payload)


def test_registry_create_update_deprecate_and_hard_delete_lifecycle(tmp_path):
    registry = ContractDefinitionRegistry(tmp_path)
    registry.create_definition(_definition())
    existing = registry.get("observer_hotfix")

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
    with pytest.raises(ContractLifecycleError, match="new revision"):
        registry.update_definition(
            changed_same_revision,
            expected_previous_hash=existing["definition_hash"],
        )

    registry.update_definition(_definition(revision="rev2"))
    deprecated_path = registry.deprecate_definition(
        "observer_hotfix",
        version="v1",
        revision="rev1",
        reason="replaced by rev2",
    )
    deprecated = registry.get("observer_hotfix", version="v1", revision="rev1")
    assert deprecated["status"] == "deprecated"
    assert deprecated["definition_hash"] == existing["definition_hash"]
    assert is_new_execution_allowed(deprecated) is False

    with pytest.raises(ContractLifecycleError, match="hard delete"):
        registry.hard_delete_definition(
            "observer_hotfix",
            version="v1",
            revision="rev1",
            references=["timeline:1"],
        )

    deprecated_path.write_text(
        json.dumps({**_definition(status="draft"), "revision": "rev3"}),
        encoding="utf-8",
    )
    assert registry.hard_delete_definition(
        "observer_hotfix",
        version="v1",
        revision="rev3",
        references=["draft-ref-ok"],
    ).name == deprecated_path.name


def test_registry_reports_unknown_contract(tmp_path):
    with pytest.raises(UnknownContractDefinitionError):
        ContractDefinitionRegistry(tmp_path).get("missing")


def test_runtime_guide_and_write_gate_reject_wrong_role_or_stale_hash(tmp_path):
    instruction_dir = tmp_path / "instructions"
    instruction_dir.mkdir()
    prompt = instruction_dir / "hotfix.md"
    prompt.write_text("Read the runtime guide and write only your owned line.\n", encoding="utf-8")
    payload = _definition(
        instruction_layer={
            "refs": [
                {
                    "id": "hotfix",
                    "path": "instructions/hotfix.md",
                    "sha256": file_sha256(prompt),
                    "visible_to_roles": ["observer"],
                    "stage_ids": ["pre_mutation"],
                }
            ]
        }
    )
    _write_definition(tmp_path, payload)
    definition = ContractDefinitionRegistry(tmp_path).get("observer_hotfix")
    bundle = resolve_instruction_bundle(definition, root=tmp_path)
    state = build_execution_state(
        definition,
        project_id="aming-claw",
        backlog_id="AC-CONTRACT",
        contract_execution_id="cex-1",
        actor_role="observer",
        route_token_ref="rtok-1",
        instruction_bundle_hash=bundle["instruction_bundle_hash"],
    )
    guide = compile_runtime_guide(definition, state, instruction_bundle=bundle)
    valid_write = {
        "project_id": "aming-claw",
        "backlog_id": "AC-CONTRACT",
        "contract_execution_id": "cex-1",
        "definition_hash": definition["definition_hash"],
        "instruction_bundle_hash": bundle["instruction_bundle_hash"],
        "execution_state_revision": 1,
        "runtime_guide_hash": guide["runtime_guide_hash"],
        "stage_id": "pre_mutation",
        "line_id": "reason",
        "evidence_kind": "contract_state_changed",
        "actor_role": "observer",
    }

    assert validate_contract_write(definition, state, valid_write, runtime_guide=guide).ok is True

    stale = {**valid_write, "runtime_guide_hash": "sha256:" + "0" * 64}
    assert validate_contract_write(definition, state, stale, runtime_guide=guide).ok is False

    observer_writes_qa = {
        **valid_write,
        "stage_id": "qa",
        "line_id": "independent_qa",
        "actor_role": "observer",
    }
    decision = validate_contract_write(
        definition,
        state,
        observer_writes_qa,
        runtime_guide=guide,
        require_next_action=False,
    )
    assert decision.ok is False
    assert "cannot write line" in decision.errors[0]
