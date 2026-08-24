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
from agent.governance.contracts.registry import (
    ContractCommonRuleApplicabilityError,
    ContractDependencyUnresolvedError,
    UnknownContractDefinitionError,
)
from agent.governance.contract_template_registry import (
    get_contract_template,
    list_contract_templates,
    resolve_contract_template,
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


EXPECTED_COMMON_SAFETY_RULE_IDS = [
    "AC-COMMON-IDENTITY-PROJECT-BACKLOG-TASK",
    "AC-COMMON-IDENTITY-ROUTE",
    "AC-COMMON-SCOPE-OWNED-FILES",
    "AC-COMMON-WORKTREE-EXACT-IDENTITY",
    "AC-COMMON-CREDENTIAL-NO-RAW-PERSISTENCE",
    "AC-COMMON-COMMIT-IMMUTABLE-WORKER",
    "AC-COMMON-QA-ROLE-SEPARATION",
    "AC-COMMON-MERGE-ORDERED",
    "AC-COMMON-GRAPH-EXACT-ACTIVE-PREFLIGHT",
    "AC-COMMON-CLOSE-INTEGRITY",
]


def _resolved_common_rule_join(registry, *, rule_ids=None, **overrides):
    package = registry.common_rule_package()
    join = {
        "schema_version": "contract_common_rule_applicability.v1",
        "join_state": "resolved",
        "package_id": package["package_id"],
        "package_version": package["package_version"],
        "package_digest": package["package_digest"],
        "rule_ids": list(rule_ids or package["rule_ids"]),
        "scopes": ["direct_main"],
        "omitted_rules_apply": False,
        "server_inference_allowed": False,
        "activation_allowed": True,
    }
    join.update(overrides)
    return join


def test_common_safety_rule_package_is_source_backed_versioned_and_digestable():
    registry = ContractDefinitionRegistry()
    package = registry.common_rule_package()

    assert package["schema_version"] == "contract_common_rule_package.v1"
    assert package["package_id"] == "aming-claw.common-safety"
    assert package["package_version"] == "v1"
    assert package["package_digest"].startswith("sha256:")
    assert package["rule_ids"] == EXPECTED_COMMON_SAFETY_RULE_IDS
    assert all(rule["statement"] for rule in package["rules"])
    assert all(rule["admitted_fact_selectors"] for rule in package["rules"])


def test_missing_common_rule_join_never_infers_authority_from_contract_type():
    registry = ContractDefinitionRegistry()
    projection = registry.resolve_common_rule_applicability(
        _definition(contract_type="mf_parallel")
    )

    assert projection["declared"] is False
    assert projection["join_state"] == "not_declared"
    assert projection["authoritative"] is False
    assert projection["rule_ids"] == []
    assert projection["omitted_rules_apply"] is False
    assert projection["server_inference_allowed"] is False


def test_explicit_common_rule_join_resolves_only_named_rules_and_scopes():
    registry = ContractDefinitionRegistry()
    selected_rule_ids = EXPECTED_COMMON_SAFETY_RULE_IDS[:3]
    definition = _definition(
        metadata={
            "common_rule_applicability": _resolved_common_rule_join(
                registry,
                rule_ids=selected_rule_ids,
            )
        }
    )

    projection = registry.resolve_common_rule_applicability(definition)

    assert projection["declared"] is True
    assert projection["join_state"] == "resolved"
    assert projection["authoritative"] is True
    assert projection["rule_ids"] == selected_rule_ids
    assert [rule["rule_id"] for rule in projection["rules"]] == selected_rule_ids
    assert projection["scopes"] == ["direct_main"]
    assert projection["authority_hash"].startswith("sha256:")


@pytest.mark.parametrize(
    ("join_patch", "code", "field"),
    [
        (
            {"package_digest": ""},
            "common_rule_applicability_field_missing",
            "metadata.common_rule_applicability.package_digest",
        ),
        (
            {"package_digest": "sha256:wrong"},
            "common_rule_applicability_digest_mismatch",
            "metadata.common_rule_applicability.package_digest",
        ),
        (
            {"rule_ids": ["AC-COMMON-UNKNOWN"]},
            "common_rule_applicability_rule_unknown",
            "metadata.common_rule_applicability.rule_ids",
        ),
    ],
)
def test_invalid_explicit_common_rule_join_fails_with_expected_actual(
    join_patch,
    code,
    field,
):
    registry = ContractDefinitionRegistry()
    join = _resolved_common_rule_join(registry)
    join.update(join_patch)
    definition = _definition(
        metadata={"common_rule_applicability": join}
    )

    with pytest.raises(ContractCommonRuleApplicabilityError) as raised:
        registry.resolve_common_rule_applicability(definition)

    error = raised.value.to_dict()
    assert error["code"] == code
    assert error["field"] == field
    assert "expected" in error
    assert "actual" in error
    assert error["authorizes_write"] is False
    assert error["mutation_performed"] is False


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
    parallel_rev6 = registry.get("mf_parallel.v2", version="v2", revision="rev6")
    parallel_rev8 = registry.get("mf_parallel.v2", version="v2", revision="rev8")
    parallel_rev9 = registry.get("mf_parallel.v2", version="v2", revision="rev9")

    assert registry.get("direct_fix", version="v1")["revision"] == "rev4"
    parallel_latest = registry.get("mf_parallel.v2", version="v2")
    assert parallel_latest["revision"] == "rev9"
    assert parallel_latest["definition_hash"] == parallel_rev9["definition_hash"]
    assert parallel_rev6["revision"] == "rev6"
    assert parallel_rev8["revision"] == "rev8"
    assert parallel_rev9["metadata"]["previous_revision"] == "mf_parallel.v2.rev8"
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


def test_direct_fix_terminal_retirement_separates_new_selection_from_history():
    registry = ContractDefinitionRegistry()
    exact = {
        revision: registry.get("direct_fix", version="v1", revision=revision)
        for revision in ("rev1", "rev2", "rev3", "rev4")
    }

    for contract_id in ("direct_fix", "direct_fix.v1", "observer_direct_fix.v1"):
        assert registry.get(contract_id, version="v1")["revision"] == "rev4"
        for revision in ("rev1", "rev2", "rev3", "rev4"):
            resolved = registry.resolve_for_new_execution(
                contract_id,
                version="v1",
                requested_revision=revision,
            )
            assert resolved["revision"] == "rev4"

    assert exact["rev4"]["status"] == "deprecated"
    assert exact["rev4"]["metadata"]["lifecycle"]["terminal_retirement"] is True
    assert [
        definition
        for definition in registry.list_definitions(include_deprecated=False)
        if definition["contract_id"] == "direct_fix"
    ] == []
    for revision in ("rev1", "rev2", "rev3"):
        assert registry.get(
            "direct_fix",
            version="v1",
            revision=revision,
        )["definition_hash"] == exact[revision]["definition_hash"]
    for contract_id in ("direct_fix", "direct_fix.v1", "observer_direct_fix.v1"):
        with pytest.raises(UnknownContractDefinitionError):
            registry.get(
                contract_id,
                version="v1",
                include_deprecated=False,
            )
        for revision in ("rev1", "rev2", "rev3", "rev4"):
            with pytest.raises(UnknownContractDefinitionError):
                registry.get(
                    contract_id,
                    version="v1",
                    revision=revision,
                    include_deprecated=False,
                )


def _terminal_retirement_definition(
    *,
    contract_id: str,
    version: str,
    revision: str,
    supersedes: list[str],
) -> dict:
    return _definition(
        contract_id=contract_id,
        version=version,
        revision=revision,
        status="deprecated",
        compat_aliases=[],
        successors=[],
        system_layer={
            "entrypoint_policy": {
                "allow_root_start": False,
                "allow_entry": False,
            },
            "successor_policy": {"allow_successor_start": False},
            "write_authority_policy": {
                "agent_facing_generic_crud_allowed": False,
            },
            "graph_binding_policy": {"new_graph_context_allowed": False},
            "route_policy": {
                "start_allowed": False,
                "new_route_allowed": False,
            },
            "lifecycle_policy": {
                "entry_allowed": False,
                "resume_allowed": False,
                "retry_allowed": False,
                "reentry_allowed": False,
                "successor_allowed": False,
            },
        },
        rule_layer={
            "stages": [
                {
                    "stage_id": "terminal_retirement",
                    "lines": [
                        {
                            "line_id": "terminal_retirement",
                            "owner_role": "system",
                            "allowed_writer_roles": ["system"],
                            "evidence_kind": "contract_terminal_retirement",
                            "required": False,
                        }
                    ],
                }
            ],
            "transitions": [],
        },
        metadata={
            "lifecycle": {
                "state": "terminal_retired",
                "terminal_retirement": True,
                "supersedes_revisions": supersedes,
                "new_execution_allowed": False,
                "entry_allowed": False,
                "resume_allowed": False,
                "retry_allowed": False,
                "reentry_allowed": False,
                "successor_allowed": False,
                "historical_pinned_read_allowed": True,
                "result": {
                    "schema_version": "test_contract_retired.v1",
                    "code": "test_contract_retired",
                    "error": "test_contract_retired",
                    "status": "rejected",
                    "classification": "contract_retirement",
                    "retryable": False,
                    "message": "test contract is terminally retired",
                    "historical_evidence_readable": True,
                    "historical_execution_scheduler_eligible": False,
                    "authorizes_write": False,
                    "next_legal_action": {
                        "id": "file_fresh_bounded_row",
                        "action": "select_or_create_backlog",
                        "next_step": "file a fresh bounded row",
                    },
                    "forbidden_backedges": ["resume_original_contract"],
                },
            }
        },
    )


def test_terminal_retirement_dominates_later_same_version_revision(tmp_path):
    for revision in ("rev1", "rev3"):
        payload = _definition(
            contract_id="terminal_dominance",
            version="v1",
            revision=revision,
            status="active",
            compat_aliases=[],
        )
        (tmp_path / f"terminal_dominance.v1.{revision}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    retirement = _terminal_retirement_definition(
        contract_id="terminal_dominance",
        version="v1",
        revision="rev2",
        supersedes=["rev1"],
    )
    (tmp_path / "terminal_dominance.v1.rev2.json").write_text(
        json.dumps(retirement),
        encoding="utf-8",
    )

    registry = ContractDefinitionRegistry(tmp_path)
    assert registry.get(
        "terminal_dominance",
        version="v1",
        revision="rev3",
    )["revision"] == "rev3"
    assert registry.resolve_for_new_execution(
        "terminal_dominance",
        version="v1",
        requested_revision="rev3",
    )["revision"] == "rev2"


def test_terminal_retirement_masks_only_its_exact_version_chain(tmp_path):
    for version in ("v1", "v2"):
        payload = _definition(
            contract_id="version_scoped_retirement",
            version=version,
            revision="rev1",
            status="active",
            compat_aliases=[],
        )
        (tmp_path / f"version_scoped_retirement.{version}.rev1.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    retirement = _terminal_retirement_definition(
        contract_id="version_scoped_retirement",
        version="v1",
        revision="rev2",
        supersedes=["rev1"],
    )
    (tmp_path / "version_scoped_retirement.v1.rev2.json").write_text(
        json.dumps(retirement),
        encoding="utf-8",
    )

    registry = ContractDefinitionRegistry(tmp_path)
    assert registry.get(
        "version_scoped_retirement",
        version="v1",
    )["revision"] == "rev2"
    assert registry.get(
        "version_scoped_retirement",
        version="v2",
    )["revision"] == "rev1"
    with pytest.raises(ContractDefinitionError, match="ambiguous"):
        registry.get("version_scoped_retirement")


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (("state", ""), "lifecycle.state"),
        (("supersedes_revisions", []), "supersedes_revisions"),
        (("resume_allowed", True), "resume_allowed"),
        (("historical_pinned_read_allowed", False), "historical pinned reads"),
        (("result.authorizes_write", True), "result.authorizes_write"),
        (("result.error", "different_error"), "result.error"),
        (("result.forbidden_backedges", []), "forbidden_backedges"),
        (("required_line", True), "required rule lines"),
        (("line_owner", "observer"), "owner_role must be system"),
        (("line_writers", ["observer"]), "allow only system"),
        (("extra_line", True), "exactly one non-executable declaration line"),
        (("system_entry", True), "entrypoint_policy.allow_entry"),
        (("system_entry_missing", None), "entrypoint_policy.allow_entry"),
        (("route_start_missing", None), "route_policy.start_allowed"),
        (("route_start", True), "route_policy.start_allowed"),
        (("lifecycle_policy_missing", None), "requires system_layer.lifecycle_policy"),
    ],
)
def test_terminal_retirement_requires_complete_coherent_schema(mutation, error):
    payload = json.loads(
        (registry_module.DEFAULT_DEFINITION_DIR / "direct_fix.v1.rev4.json").read_text(
            encoding="utf-8"
        )
    )
    field, value = mutation
    if field.startswith("result."):
        payload["metadata"]["lifecycle"]["result"][field.split(".", 1)[1]] = value
    elif field == "required_line":
        payload["rule_layer"]["stages"][0]["lines"][0]["required"] = value
    elif field == "line_owner":
        payload["rule_layer"]["stages"][0]["lines"][0]["owner_role"] = value
    elif field == "line_writers":
        payload["rule_layer"]["stages"][0]["lines"][0]["allowed_writer_roles"] = value
    elif field == "extra_line":
        payload["rule_layer"]["stages"][0]["lines"].append(
            {
                "line_id": "resume_old_world",
                "owner_role": "observer",
                "allowed_writer_roles": ["observer"],
                "evidence_kind": "resume",
                "required": False,
            }
        )
    elif field == "system_entry":
        payload["system_layer"]["entrypoint_policy"]["allow_entry"] = value
    elif field == "system_entry_missing":
        payload["system_layer"]["entrypoint_policy"].pop("allow_entry")
    elif field == "lifecycle_policy_missing":
        payload["system_layer"].pop("lifecycle_policy")
    elif field == "route_start_missing":
        payload["system_layer"]["route_policy"].pop("start_allowed")
    elif field == "route_start":
        payload["system_layer"]["route_policy"]["start_allowed"] = value
    else:
        payload["metadata"]["lifecycle"][field] = value

    with pytest.raises(ContractDefinitionError, match=error):
        ContractDefinitionRegistry().validate_payload(payload)


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
        assert policy["exact_candidate_escalation_stage"] == (
            "server_persisted_overlay_failure_classification"
        )
        assert policy["exact_candidate_acceptance_stage"] == (
            "graph_query_trace_persisted_basis_decision"
        )
        assert policy["server_trigger_escalation_ref_required"] is True
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


@pytest.mark.parametrize(
    "trigger",
    [
        "graph_algorithm_or_graph_config_change",
        "governance_semantic_or_structure_hint_change",
        "broad_or_unbounded_candidate_change",
        "deterministic_overlay_or_one_hop_dependency_failure",
        "qa_explicit_exact_snapshot_request",
    ],
)
def test_latest_qa_gate_accepts_each_pinned_exact_upgrade_trigger(trigger: str) -> None:
    definition = ContractDefinitionRegistry().get(
        "direct_fix",
        revision="rev3",
    )
    state = build_execution_state(
        definition,
        project_id="aming-claw",
        backlog_id="AC-QA-EXACT-TRIGGER-MATRIX",
        contract_execution_id=f"cex-exact-trigger-{trigger}",
        actor_role="qa",
        instruction_bundle_hash="sha256:exact-trigger-matrix",
    )
    decision = {
        "schema_version": "qa_review_graph.basis_decision.v1",
        "decision_source": "server_bounded_qa_graph_basis",
        "default_graph_basis": "canonical_base_plus_candidate_diff",
        "selected_graph_basis": "exact_candidate_snapshot",
        "selection_reason": (
            "qa_explicit_exact_candidate_snapshot"
            if trigger == "qa_explicit_exact_snapshot_request"
            else "server_classified_exact_candidate_snapshot"
        ),
        "candidate_change_classification": f"server_or_qa:{trigger}",
        "exact_candidate_upgrade_trigger": trigger,
        "exact_candidate_upgrade_ref": (
            "" if trigger == "qa_explicit_exact_snapshot_request" else f"qage-{trigger}"
        ),
        "exact_candidate_upgrade_policy": "server_classified_or_qa_explicit",
        "canonical_head_policy": "base_or_candidate",
        "canonical_head_relation": "candidate",
        "overlay_failure_policy": "fail_closed",
        "one_hop_dependency_failure_policy": "fail_closed",
    }
    commit = "b" * 40
    authority = {
        "source": "graph_query_traces",
        "db_verified": True,
        "trace_ids": ["gqt-exact-trigger"],
        "verified_trace_ids": ["gqt-exact-trigger"],
        "missing_trace_ids": [],
        "identity_mismatches": [],
        "query_source": "qa",
        "query_purpose": "independent_verification",
        "project_id": "aming-claw",
        "backlog_id": "AC-QA-EXACT-TRIGGER-MATRIX",
        "task_id": "worker-exact-trigger",
        "qa_session_id": "ses-exact-trigger",
        "qa_principal": "qa-exact-trigger",
        "target_project_root": "/tmp/exact-trigger",
        "graph_basis": "exact_candidate_snapshot",
        "graph_basis_decision": decision,
        "graph_basis_decision_hash": stable_sha256(decision),
        "canonical_base_snapshot_id": "full-exact-trigger",
        "base_commit_sha": commit,
        "candidate_commit_sha": commit,
        "changed_files": [],
        "candidate_diff_hash": (
            "sha256:e3b0c44298fc1c149afbf4c8996fb924"
            "27ae41e4649b934ca495991b7852b855"
        ),
        "changed_files_source": "server_exact_candidate_snapshot",
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
        "task_id": "worker-exact-trigger",
        "graph_trace_ids": ["gqt-exact-trigger"],
        "payload": {"graph_trace_evidence": authority},
    }

    assert validate_contract_write(
        definition,
        state,
        write,
        require_next_action=False,
    ).ok is True


def test_direct_main_rev1_is_exact_auditable_and_dependency_blocked() -> None:
    registry = ContractDefinitionRegistry()
    definition = registry.get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev1",
    )

    assert definition["contract_id"] == "operator_supervised_direct_main"
    assert definition["version"] == "v1"
    assert definition["revision"] == "rev1"
    assert definition["status"] == "draft"
    assert definition["definition_hash"] == (
        "sha256:3b0da25cf3ab87f7e4bd59b65dca81882798f13fa92558def8707a4f174a21bb"
    )
    source_path = (
        registry_module.DEFAULT_DEFINITION_DIR
        / "operator_supervised_direct_main.v1.rev1.json"
    )
    assert definition["source_sha256"] == file_sha256(source_path)
    assert registry.get("direct_main", revision="rev1")["definition_hash"] == (
        definition["definition_hash"]
    )
    assert registry.get("direct_main.v1", revision="rev1")["definition_hash"] == (
        definition["definition_hash"]
    )
    assert is_new_execution_allowed(definition) is False

    activation = definition["metadata"]["activation"]
    assert activation["state"] == "dependency_unresolved"
    assert activation["activation_ready"] is False
    assert activation["new_execution_allowed"] is False
    assert activation["template_selection_allowed"] is False
    assert [
        item["dependency_id"] for item in activation["unresolved_dependencies"]
    ] == ["AC-CONTRACT-COMMON-SAFETY-RULE-PACKAGE-P0-20260815"]

    with pytest.raises(ContractDependencyUnresolvedError) as raised:
        registry.resolve_for_new_execution(
            "operator_supervised_direct_main",
            version="v1",
            requested_revision="rev1",
        )
    error = raised.value.to_dict()
    assert error["schema_version"] == "contract_dependency_unresolved.v1"
    assert error["code"] == "contract_dependency_unresolved"
    assert error["error"] == "contract_dependency_unresolved"
    assert error["status"] == "rejected"
    assert error["classification"] == "external_dependency"
    assert error["retryable"] is False
    assert error["authorizes_write"] is False
    assert error["activation_ready"] is False
    assert error["contract_id"] == "operator_supervised_direct_main"
    assert error["version"] == "v1"
    assert error["revision"] == "rev1"
    assert error["next_legal_action"]["id"] == (
        "complete_common_rule_package_then_create_direct_revision"
    )


def test_direct_main_rev3_template_and_task_selection_are_source_backed() -> None:
    registry = ContractDefinitionRegistry()
    definition = registry.resolve_for_new_execution(
        "operator_supervised_direct_main",
        version="v1",
    )
    rev2 = registry.get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev2",
    )
    template = get_contract_template("operator_supervised_direct_main")

    assert template["template_id"] == "operator_supervised_direct_main.v1"
    assert template["source"] == {
        "type": "source_controlled",
        "path": "contract_definitions/operator_supervised_direct_main.v1.rev3.json",
        "authority": "contract_definition",
        "template_is_authoritative": False,
        "contract_id": "operator_supervised_direct_main",
        "version": "v1",
        "revision": "rev3",
        "definition_hash": (
            "sha256:39789c8180777162b3d1fd3aac401f0cfe58aa5e30c3a0c046ccc5ed1d9ba1a4"
        ),
        "definition_source_sha256": (
            "sha256:38ba51f29e3d2a1cde67cd7a34dafa4ee0018ac760f97c40a0805725fd34c2e7"
        ),
    }
    assert definition["revision"] == "rev3"
    assert definition["status"] == "active"
    assert registry.get("direct_main")["revision"] == "rev3"
    assert registry.get("direct_main.v1")["revision"] == "rev3"
    assert rev2["source_sha256"] == (
        "sha256:a98b860023b33cebaabc3d76d30c3a2c779532bd642bc8eb1a4ae7917540bef0"
    )
    assert "AC-COMMON-MERGE-ORDERED" in rev2["metadata"][
        "common_rule_applicability"
    ]["rule_ids"]
    assert "AC-COMMON-MERGE-ORDERED" not in definition["metadata"][
        "common_rule_applicability"
    ]["rule_ids"]
    assert definition["metadata"]["previous_revision"] == (
        "operator_supervised_direct_main.v1.rev2"
    )
    assert is_new_execution_allowed(definition) is True
    assert template["source"]["definition_hash"] == definition["definition_hash"]
    assert template["source"]["definition_source_sha256"] == definition[
        "source_sha256"
    ]
    assert template["common_rule_applicability"] == definition["metadata"][
        "common_rule_applicability"
    ]
    assert template["activation_policy"] == definition["metadata"]["activation"]
    assert [
        item["template_id"]
        for item in list_contract_templates(task_type="direct_main")
    ] == ["operator_supervised_direct_main.v1"]

    for query in (
        {"template_id": "direct_main"},
        {"template_id": "operator_supervised_direct_main"},
        {"task_type": "direct_main"},
        {"task_type": "operator_supervised_direct_main"},
    ):
        resolved = resolve_contract_template(**query)
        assert resolved["source"]["revision"] == "rev3"
        assert resolved["activation_policy"]["activation_ready"] is True
        assert resolved["activation_policy"]["new_execution_allowed"] is True
