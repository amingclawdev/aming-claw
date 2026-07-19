from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest
import agent.governance.contracts.registry as registry_module

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
from agent.governance.contracts.hash import file_sha256, stable_sha256
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


def test_registry_snapshot_cache_reuses_normalized_definitions_and_defensive_copies(
    tmp_path,
    monkeypatch,
):
    _write_definition(tmp_path, _definition())
    normalize_calls = 0
    real_normalize = registry_module.normalize_definition

    def counted_normalize(*args, **kwargs):
        nonlocal normalize_calls
        normalize_calls += 1
        return real_normalize(*args, **kwargs)

    monkeypatch.setattr(registry_module, "normalize_definition", counted_normalize)
    registry = ContractDefinitionRegistry(tmp_path)

    first = registry.get("observer_hotfix")
    first["rule_layer"]["stages"][0]["stage_id"] = "caller_mutation"
    second = registry.get("observer_hotfix")

    assert normalize_calls == 1
    assert second["rule_layer"]["stages"][0]["stage_id"] == "pre_mutation"


def test_registry_snapshot_cache_invalidates_on_source_and_head_change(
    tmp_path,
    monkeypatch,
):
    path = _write_definition(tmp_path, _definition())
    head_revision = ["head-a"]
    monkeypatch.setattr(
        registry_module,
        "_git_registry_snapshot",
        lambda root: (None, head_revision[0]),
    )
    normalize_calls = 0
    real_normalize = registry_module.normalize_definition

    def counted_normalize(*args, **kwargs):
        nonlocal normalize_calls
        normalize_calls += 1
        return real_normalize(*args, **kwargs)

    monkeypatch.setattr(registry_module, "normalize_definition", counted_normalize)
    registry = ContractDefinitionRegistry(tmp_path)

    assert registry.get("observer_hotfix")["status"] == "active"
    assert registry.get("observer_hotfix")["status"] == "active"
    assert normalize_calls == 1

    path.write_text(json.dumps(_definition(status="draft")), encoding="utf-8")
    assert registry.get("observer_hotfix")["status"] == "draft"
    assert normalize_calls == 2

    head_revision[0] = "head-b"
    assert registry.get("observer_hotfix")["status"] == "draft"
    assert normalize_calls == 3


def test_registry_snapshot_cache_build_is_thread_safe(tmp_path, monkeypatch):
    _write_definition(tmp_path, _definition())
    normalize_calls = 0
    real_normalize = registry_module.normalize_definition

    def counted_normalize(*args, **kwargs):
        nonlocal normalize_calls
        normalize_calls += 1
        return real_normalize(*args, **kwargs)

    monkeypatch.setattr(registry_module, "normalize_definition", counted_normalize)
    registry = ContractDefinitionRegistry(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        definitions = list(
            executor.map(lambda _: registry.get("observer_hotfix"), range(16))
        )

    assert normalize_calls == 1
    assert {item["definition_hash"] for item in definitions} == {
        definitions[0]["definition_hash"]
    }


@pytest.mark.parametrize("control_character", ["\n", "\r", "\x1f", "\x7f"])
def test_registry_rejects_protocol_control_characters_in_file_name(
    tmp_path,
    control_character,
):
    registry = ContractDefinitionRegistry(tmp_path)

    with pytest.raises(ContractLifecycleError, match="control characters"):
        registry.create_definition(
            _definition(),
            file_name=f"probe{control_character}HEAD:.mcp.json",
        )


def test_git_head_blobs_rejects_protocol_injection_before_subprocess(
    tmp_path,
    monkeypatch,
):
    injected = tmp_path / "probe\nHEAD:.mcp.json"
    injected.write_text("{}", encoding="utf-8")

    def unexpected_subprocess(*args, **kwargs):
        raise AssertionError("unsafe batch input reached subprocess")

    monkeypatch.setattr(registry_module.subprocess, "run", unexpected_subprocess)

    assert registry_module._git_head_blobs(tmp_path, [injected]) is None


def test_registry_retries_unchanged_snapshot_after_transient_batch_failure(
    tmp_path,
    monkeypatch,
):
    path = _write_definition(tmp_path, _definition())
    batch_calls = 0

    monkeypatch.setattr(
        registry_module,
        "_git_registry_snapshot",
        lambda root: (tmp_path, "head-a"),
    )
    monkeypatch.setattr(
        registry_module,
        "_git_head_blob",
        lambda git_root, relative_path: path.read_bytes(),
    )

    def transient_then_success(git_root, paths):
        nonlocal batch_calls
        batch_calls += 1
        if batch_calls == 1:
            return None
        return {path.name: path.read_bytes()}

    monkeypatch.setattr(
        registry_module,
        "_git_head_blobs",
        transient_then_success,
    )
    registry = ContractDefinitionRegistry(tmp_path)

    first = registry.get("observer_hotfix")
    second = registry.get("observer_hotfix")
    third = registry.get("observer_hotfix")

    assert first["definition_load_record"]["source_control_integrity"]["status"] == (
        "current"
    )
    assert second["definition_load_record"]["source_control_integrity"]["status"] == (
        "current"
    )
    assert third["definition_hash"] == second["definition_hash"]
    assert batch_calls == 2


def test_git_head_blobs_rejects_misaligned_or_trailing_batch_output(
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = (
            b"a" * 40
            + b" blob 2\n{}\n"
            + b"b" * 40
            + b" blob 2\n{}\n"
            + b"unexpected trailing response\n"
        )

    monkeypatch.setattr(
        registry_module.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )

    assert registry_module._git_head_blobs(tmp_path, [first, second]) is None


def test_server_contract_runtime_and_pinned_definition_share_registry_snapshot(
    tmp_path,
    monkeypatch,
):
    from agent.governance import server as governance_server

    _write_definition(tmp_path, _definition())
    registry = ContractDefinitionRegistry(tmp_path)
    normalize_calls = 0
    real_normalize = registry_module.normalize_definition

    def counted_normalize(*args, **kwargs):
        nonlocal normalize_calls
        normalize_calls += 1
        return real_normalize(*args, **kwargs)

    monkeypatch.setattr(registry_module, "normalize_definition", counted_normalize)
    monkeypatch.setattr(
        governance_server,
        "_CONTRACT_DEFINITION_REGISTRY",
        registry,
    )
    monkeypatch.setattr(
        governance_server,
        "_contract_runtime_store",
        lambda conn: object(),
    )

    first_runtime = governance_server._contract_runtime(object())
    second_runtime = governance_server._contract_runtime(object())
    first = governance_server._contract_runtime_definition_for_record(
        {"contract_id": "observer_hotfix", "version": "v1", "revision": "rev1"}
    )
    second = governance_server._contract_runtime_definition_for_record(
        {"contract_id": "observer_hotfix", "version": "v1", "revision": "rev1"}
    )

    assert first_runtime.registry is registry
    assert second_runtime.registry is registry
    assert first["definition_hash"] == second["definition_hash"]
    assert normalize_calls == 1


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

    assert registry.get("direct_fix", version="v1")["revision"] == "rev3"
    assert registry.get("mf_parallel.v2", version="v2")["revision"] == "rev6"
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
    assert direct_rev2_graph_policy["bounded_qa_review_policy"][
        "exact_candidate_query_root_clean_required"
    ] is True
    assert direct_rev2_graph_policy["bounded_qa_review_policy"][
        "assigned_target_project_root_required"
    ] is True
    assert "candidate_commit_evidence_policy" not in direct_rev1_graph_policy
    assert direct_rev2_graph_policy["candidate_commit_evidence_policy"][
        "line_ids"
    ] == ["direct_fix_candidate_repair"]
    assert "current_full_reconcile_evidence_policy" not in parallel_rev1_graph_policy
    assert parallel_rev2_graph_policy[
        "current_full_reconcile_evidence_policy"
    ]["enabled"] is True
    reconcile_policy = parallel_rev2_graph_policy[
        "current_full_reconcile_evidence_policy"
    ]
    assert reconcile_policy["required_temporal_fields"][:2] == [
        "merge_event_id",
        "merge_event_created_at",
    ]
    assert reconcile_policy["qa_authority_alternative_mode"] == "exactly_one"
    assert [
        item["id"] for item in reconcile_policy["qa_authority_alternatives"]
    ] == ["timeline_event", "canonical_contract_runtime_acceptance"]
    assert reconcile_policy["explicit_task_id_mismatch_policy"] == "fail_closed"
    assert parallel_rev2_graph_policy["candidate_commit_evidence_policy"][
        "line_ids"
    ] == ["worker_commit"]

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

    rev1_candidate_state, rev1_candidate_write = state_and_write(
        direct_rev1,
        stage_id="candidate_repair",
        line_id="direct_fix_candidate_repair",
        actor_role="mf_sub",
        evidence_kind="direct_fix_repair_evidence",
    )
    rev2_candidate_state, rev2_candidate_write = state_and_write(
        direct_rev2,
        stage_id="candidate_repair",
        line_id="direct_fix_candidate_repair",
        actor_role="mf_sub",
        evidence_kind="direct_fix_repair_evidence",
    )
    assert validate_contract_write(
        direct_rev1,
        rev1_candidate_state,
        rev1_candidate_write,
        require_next_action=False,
    ).ok is True
    assert validate_contract_write(
        direct_rev2,
        rev2_candidate_state,
        rev2_candidate_write,
        require_next_action=False,
    ).ok is False
    rev2_candidate_write["payload"] = {"candidate_commit_sha": "a" * 40}
    assert validate_contract_write(
        direct_rev2,
        rev2_candidate_state,
        rev2_candidate_write,
        require_next_action=False,
    ).ok is False
    rev2_candidate_write["commit_sha"] = "a" * 40
    assert validate_contract_write(
        direct_rev2,
        rev2_candidate_state,
        rev2_candidate_write,
        require_next_action=False,
    ).ok is True

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
            "root_identity_hash": "sha256:" + "3" * 64,
            "query_root_identity_hash": "sha256:" + "4" * 64,
            "canonical_project_identity_hash": "sha256:" + "5" * 64,
            "repository_identity_hash": "sha256:" + "6" * 64,
        }
        if graph_basis == "canonical_base_plus_candidate_diff":
            authority.update(
                {
                    "candidate_overlay_hash": "sha256:" + "2" * 64,
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


def test_mf_parallel_rev5_instructions_cover_replay_friction() -> None:
    definition = ContractDefinitionRegistry().get(
        "mf_parallel.v2",
        revision="rev5",
    )
    instructions = "\n".join(
        definition["instruction_layer"]["inline"]
    )

    assert "active persisted owned_files" in instructions
    assert "activate=false" in instructions
    assert "durable status merge_ready" in instructions
    assert "git_mutation_executed=false" in instructions
    assert "distinct activation snapshot id" in instructions


def test_latest_qa_basis_revisions_default_overlay_without_rewriting_pinned_policy() -> None:
    registry = ContractDefinitionRegistry()
    parallel_rev5 = registry.get("mf_parallel.v2", revision="rev5")
    parallel_rev6 = registry.get("mf_parallel.v2", revision="rev6")
    direct_rev2 = registry.get("direct_fix", revision="rev2")
    direct_rev3 = registry.get("direct_fix", revision="rev3")

    assert parallel_rev6["metadata"]["previous_revision"] == "mf_parallel.v2.rev5"
    assert direct_rev3["metadata"]["previous_revision"] == "direct_fix.v1.rev2"
    assert parallel_rev5["definition_hash"] != parallel_rev6["definition_hash"]
    assert direct_rev2["definition_hash"] != direct_rev3["definition_hash"]
    assert "materialize an exact candidate snapshot" in "\n".join(
        parallel_rev5["instruction_layer"]["inline"]
    )
    assert "default to the active canonical parent graph" in "\n".join(
        parallel_rev6["instruction_layer"]["inline"]
    )

    for definition in (parallel_rev6, direct_rev3):
        policy = definition["system_layer"]["graph_binding_policy"][
            "bounded_qa_review_policy"
        ]
        assert policy["default_graph_basis"] == (
            "canonical_base_plus_candidate_diff"
        )
        assert policy["graph_basis_decision_required"] is True
        assert policy["graph_basis_decision_source"] == (
            "server_bounded_qa_graph_basis"
        )
        assert policy["canonical_head_policy"] == "base_or_candidate"
        assert policy["overlay_failure_policy"] == "fail_closed"
        assert policy["one_hop_dependency_failure_policy"] == "fail_closed"
        assert policy["exact_candidate_upgrade_triggers"] == [
            "graph_algorithm_or_graph_config_change",
            "governance_semantic_or_structure_hint_change",
            "broad_or_unbounded_candidate_change",
            "deterministic_overlay_or_one_hop_dependency_failure",
            "qa_explicit_exact_snapshot_request",
        ]

    state = build_execution_state(
        direct_rev3,
        project_id="aming-claw",
        backlog_id="AC-QA-BASE-GRAPH-DIFF-PREMERGE-20260711",
        contract_execution_id="cex-latest-qa-basis",
        actor_role="qa",
        instruction_bundle_hash="sha256:latest-qa-basis",
    )
    decision = {
        "schema_version": "qa_review_graph.basis_decision.v1",
        "decision_source": "server_bounded_qa_graph_basis",
        "default_graph_basis": "canonical_base_plus_candidate_diff",
        "selected_graph_basis": "canonical_base_plus_candidate_diff",
        "selection_reason": "bounded_source_backed_overlay_safe",
        "candidate_change_classification": "bounded_source_backed_overlay",
        "exact_candidate_upgrade_trigger": "",
        "exact_candidate_upgrade_policy": "server_classified_or_qa_explicit",
        "canonical_head_policy": "base_or_candidate",
        "canonical_head_relation": "candidate",
        "overlay_failure_policy": "fail_closed",
        "one_hop_dependency_failure_policy": "fail_closed",
    }
    authority = {
        "source": "graph_query_traces",
        "db_verified": True,
        "trace_ids": ["gqt-latest-qa-basis"],
        "verified_trace_ids": ["gqt-latest-qa-basis"],
        "missing_trace_ids": [],
        "identity_mismatches": [],
        "query_source": "qa",
        "query_purpose": "independent_verification",
        "project_id": "aming-claw",
        "backlog_id": "AC-QA-BASE-GRAPH-DIFF-PREMERGE-20260711",
        "task_id": "worker-latest-qa-basis",
        "qa_session_id": "ses-latest-qa-basis",
        "qa_principal": "qa-latest-qa-basis",
        "target_project_root": "/tmp/latest-qa-basis",
        "graph_basis": "canonical_base_plus_candidate_diff",
        "graph_basis_decision": decision,
        "graph_basis_decision_hash": stable_sha256(decision),
        "canonical_base_snapshot_id": "full-latest-qa-basis",
        "base_commit_sha": "a" * 40,
        "candidate_commit_sha": "b" * 40,
        "changed_files": ["agent/governance/server.py"],
        "candidate_diff_hash": "sha256:" + "1" * 64,
        "changed_files_source": "server_candidate_diff",
        "candidate_overlay_hash": "sha256:" + "2" * 64,
        "root_identity_hash": "sha256:" + "3" * 64,
        "query_root_identity_hash": "sha256:" + "4" * 64,
        "canonical_project_identity_hash": "sha256:" + "5" * 64,
        "repository_identity_hash": "sha256:" + "6" * 64,
    }
    write = {
        "project_id": state["project_id"],
        "backlog_id": state["backlog_id"],
        "contract_execution_id": state["contract_execution_id"],
        "definition_hash": state["definition_hash"],
        "instruction_bundle_hash": state["instruction_bundle_hash"],
        "execution_state_revision": state["execution_state_revision"],
        "stage_id": "qa_graph_context",
        "line_id": "direct_fix_qa_graph_context",
        "actor_role": "qa",
        "evidence_kind": "graph_trace",
        "task_id": "worker-latest-qa-basis",
        "graph_trace_ids": ["gqt-latest-qa-basis"],
        "payload": {"graph_trace_evidence": authority},
    }
    assert validate_contract_write(
        direct_rev3, state, write, require_next_action=False
    ).ok is True

    forged_decision = {**decision, "overlay_failure_policy": "allow_partial"}
    forged = {
        **write,
        "payload": {
            "graph_trace_evidence": {
                **authority,
                "graph_basis_decision": forged_decision,
                "graph_basis_decision_hash": stable_sha256(forged_decision),
            }
        },
    }
    rejected = validate_contract_write(
        direct_rev3, state, forged, require_next_action=False
    )
    assert rejected.ok is False
    assert any("fail-closed overlay" in item for item in rejected.errors)
