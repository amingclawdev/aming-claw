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


def _governance_hints(*, operation="bind"):
    return {
        "schema_version": "governance_hints.v1",
        "asset_binding_events": [{
            "schema_version": "asset_binding_event.v1",
            "operation": operation,
            "path": ".",
            "role": "config",
            "target_module": "agent.governance.contracts.registry",
        }],
    }


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
        {"contract_id": "observer_hotfix", "version": "v1"},
        {"contract_id": "mf_parallel", "version": "v1"},
        {"contract_id": "contract_update", "version": "v1"},
    ]
    hotfix = registry.get("observer_hotfix")["read_model"]["system_layer"]
    assert hotfix["successor_policy"]["allowed_parent_contracts"] == [
        {"contract_id": "onboard_contract", "version": "v1"}
    ]


def test_qa_and_reconcile_policy_revision_boundary_is_pinnable_and_policy_driven():
    registry = ContractDefinitionRegistry()
    direct_rev1 = registry.get("direct_fix", version="v1", revision="rev1")
    direct_rev2 = registry.get("direct_fix", version="v1", revision="rev2")
    parallel_rev1 = registry.get("mf_parallel.v2", version="v2", revision="rev1")
    parallel_rev2 = registry.get("mf_parallel.v2", version="v2", revision="rev2")

    assert registry.get("direct_fix", version="v1")["revision"] == "rev2"
    assert registry.get("mf_parallel.v2", version="v2")["revision"] == "rev2"
    assert direct_rev1["definition_hash"] == (
        "sha256:aada5b4fd59b49bdfda85c17839194432e4b4d690d78bfe6a35cff138c96a383"
    )
    assert parallel_rev1["definition_hash"] == (
        "sha256:0fd69197a4e62a6f600e5f746b78b5a57c2519c9b89fc015c0b6bdd0905a9c32"
    )
    assert direct_rev1["definition_hash"] != direct_rev2["definition_hash"]
    assert parallel_rev1["definition_hash"] != parallel_rev2["definition_hash"]
    assert direct_rev2["metadata"]["previous_revision"] == "direct_fix.v1.rev1"
    assert parallel_rev2["metadata"]["previous_revision"] == "mf_parallel.v2.rev1"

    direct_rev1_graph_policy = direct_rev1["system_layer"]["graph_binding_policy"]
    direct_rev2_graph_policy = direct_rev2["system_layer"]["graph_binding_policy"]
    parallel_rev1_graph_policy = parallel_rev1["system_layer"]["graph_binding_policy"]
    parallel_rev2_graph_policy = parallel_rev2["system_layer"]["graph_binding_policy"]
    assert "bounded_qa_review_policy" not in direct_rev1_graph_policy
    assert direct_rev2_graph_policy["bounded_qa_review_policy"]["enabled"] is True
    assert "current_full_reconcile_evidence_policy" not in parallel_rev1_graph_policy
    assert parallel_rev2_graph_policy[
        "current_full_reconcile_evidence_policy"
    ]["enabled"] is True

    def state_and_write(definition, *, stage_id, line_id, actor_role, evidence_kind):
        state = build_execution_state(
            definition,
            project_id="aming-claw",
            backlog_id="AC-REVISION-BOUNDARY",
            contract_execution_id="cex-revision-boundary",
            actor_role=actor_role,
            instruction_bundle_hash="sha256:instruction-boundary",
        )
        write = {
            "project_id": state["project_id"],
            "backlog_id": state["backlog_id"],
            "contract_execution_id": state["contract_execution_id"],
            "definition_hash": state["definition_hash"],
            "instruction_bundle_hash": state["instruction_bundle_hash"],
            "execution_state_revision": state["execution_state_revision"],
            "stage_id": stage_id,
            "line_id": line_id,
            "actor_role": actor_role,
            "evidence_kind": evidence_kind,
        }
        return state, write

    rev1_state, rev1_qa_write = state_and_write(
        direct_rev1,
        stage_id="qa_graph_context",
        line_id="direct_fix_qa_graph_context",
        actor_role="qa",
        evidence_kind="graph_trace",
    )
    rev1_qa_write["payload"] = {
        "graph_trace_ids": ["gqt-revision-boundary"],
        "graph_trace_evidence": {
            "db_verified": True,
            "query_source": "qa",
            "query_purpose": "independent_verification",
            "target_project_root": "/tmp/revision-boundary",
        },
    }
    rev1_qa_write["graph_trace_ids"] = ["gqt-revision-boundary"]
    rev2_state, rev2_qa_write = state_and_write(
        direct_rev2,
        stage_id="qa_graph_context",
        line_id="direct_fix_qa_graph_context",
        actor_role="qa",
        evidence_kind="graph_trace",
    )
    rev2_qa_write["payload"] = dict(rev1_qa_write["payload"])
    rev2_qa_write["graph_trace_ids"] = ["gqt-revision-boundary"]
    assert validate_contract_write(
        direct_rev1,
        rev1_state,
        rev1_qa_write,
        require_next_action=False,
    ).ok is True
    assert validate_contract_write(
        direct_rev2,
        rev2_state,
        rev2_qa_write,
        require_next_action=False,
    ).ok is False

    for graph_basis in (
        "exact_candidate_snapshot",
        "canonical_base_plus_candidate_diff",
    ):
        complete_write = dict(rev2_qa_write)
        authority = {
            "source": "graph_query_traces",
            "db_verified": True,
            "trace_ids": ["gqt-revision-boundary"],
            "verified_trace_ids": ["gqt-revision-boundary"],
            "missing_trace_ids": [],
            "identity_mismatches": [],
            "query_source": "qa",
            "query_purpose": "independent_verification",
            "project_id": "aming-claw",
            "backlog_id": "AC-REVISION-BOUNDARY",
            "task_id": "worker-revision-boundary",
            "qa_session_id": "ses-revision-boundary",
            "qa_principal": "qa-revision-boundary",
            "target_project_root": "/tmp/revision-boundary",
            "graph_basis": graph_basis,
            "canonical_base_snapshot_id": "full-revision-boundary",
            "base_commit_sha": "a" * 40,
            "candidate_commit_sha": (
                "a" * 40
                if graph_basis == "exact_candidate_snapshot"
                else "b" * 40
            ),
            "changed_files": (
                []
                if graph_basis == "exact_candidate_snapshot"
                else ["agent/governance/server.py"]
            ),
            "candidate_diff_hash": (
                "sha256:e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855"
                if graph_basis == "exact_candidate_snapshot"
                else "sha256:" + "1" * 64
            ),
            "changed_files_source": (
                "server_exact_candidate_snapshot"
                if graph_basis == "exact_candidate_snapshot"
                else "server_candidate_diff"
            ),
        }
        if graph_basis == "canonical_base_plus_candidate_diff":
            authority.update(
                {
                    "candidate_overlay_hash": "sha256:" + "2" * 64,
                    "root_identity_hash": "sha256:" + "3" * 64,
                    "query_root_identity_hash": "sha256:" + "4" * 64,
                    "canonical_project_identity_hash": "sha256:" + "5" * 64,
                    "repository_identity_hash": "sha256:" + "6" * 64,
                }
            )
        complete_write["payload"] = {
            "graph_trace_ids": ["gqt-revision-boundary"],
            "graph_trace_evidence": authority,
        }
        assert validate_contract_write(
            direct_rev2,
            rev2_state,
            complete_write,
            require_next_action=False,
        ).ok is True

    rev1_state, rev1_reconcile = state_and_write(
        parallel_rev1,
        stage_id="observer_integration",
        line_id="observer_reconcile",
        actor_role="observer",
        evidence_kind="reconcile",
    )
    rev2_state, rev2_reconcile = state_and_write(
        parallel_rev2,
        stage_id="observer_integration",
        line_id="observer_reconcile",
        actor_role="observer",
        evidence_kind="reconcile",
    )
    assert validate_contract_write(
        parallel_rev1,
        rev1_state,
        rev1_reconcile,
        require_next_action=False,
    ).ok is True
    assert validate_contract_write(
        parallel_rev2,
        rev2_state,
        rev2_reconcile,
        require_next_action=False,
    ).ok is False


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


def test_registry_preserves_only_reserved_root_envelope_and_hashes_nested_business_field():
    registry = ContractDefinitionRegistry()
    payload = _definition(
        governance_hints=_governance_hints(),
        metadata={"governance_hints": {"business_rule": "one"}},
        unrelated_root={"drop": True},
    )

    normalized = registry.validate_payload(payload)
    envelope_hash = normalized["definition_hash"]
    envelope_changed = registry.validate_payload({
        **payload,
        "governance_hints": _governance_hints(operation="unbind"),
    })
    nested_changed = registry.validate_payload({
        **payload,
        "metadata": {"governance_hints": {"business_rule": "two"}},
    })

    assert normalized["governance_hints"] == _governance_hints()
    assert "unrelated_root" not in normalized
    assert "governance_hints" not in normalized["read_model"]
    assert envelope_changed["definition_hash"] == envelope_hash
    assert nested_changed["definition_hash"] != envelope_hash


def test_registry_crud_lifecycle_preserves_governance_hints_envelope(tmp_path):
    registry = ContractDefinitionRegistry(tmp_path)
    registry.create_definition(_definition(governance_hints=_governance_hints()))

    created = registry.get("observer_hotfix")
    deprecated_path = registry.deprecate_definition(
        "observer_hotfix",
        version="v1",
        revision="rev1",
        reason="test lifecycle",
    )
    deprecated_payload = json.loads(deprecated_path.read_text(encoding="utf-8"))
    deprecated = registry.get("observer_hotfix", version="v1", revision="rev1")

    assert created["governance_hints"] == _governance_hints()
    assert deprecated_payload["governance_hints"] == _governance_hints()
    assert deprecated["governance_hints"] == _governance_hints()
    assert deprecated["definition_hash"] == created["definition_hash"]


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


def test_mf_parallel_v2_records_worker_commit_before_finish_attestation():
    definition = ContractDefinitionRegistry().get(
        "mf_parallel.v2",
        version="v2",
        revision="rev1",
    )
    lines = [
        line
        for stage in definition["rule_layer"]["stages"]
        for line in stage.get("lines", [])
    ]
    line_ids = [line["line_id"] for line in lines]

    assert line_ids.index("worker_implementation") < line_ids.index("worker_commit")
    assert line_ids.index("worker_commit") < line_ids.index(
        "worker_finish_time_attestation"
    )
    worker_commit = next(line for line in lines if line["line_id"] == "worker_commit")
    finish_attestation = next(
        line for line in lines if line["line_id"] == "worker_finish_time_attestation"
    )
    assert worker_commit["owner_role"] == "mf_sub"
    assert worker_commit["allowed_writer_roles"] == ["mf_sub"]
    assert worker_commit["requires"] == ["worker_implementation"]
    assert finish_attestation["requires"] == ["worker_commit"]


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
