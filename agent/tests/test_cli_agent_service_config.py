import json
import os
import sys
from dataclasses import FrozenInstanceError

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _profile():
    from cli_agent_service.models import (
        AgentProfile,
        CredentialRef,
        HarnessRuntime,
        InferenceEndpoint,
        LauncherAdapter,
        RolePolicy,
    )

    return AgentProfile(
        profile_id="profile-codex-dev",
        version="3",
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-codex",
            version="2",
            kind="codex_cli",
            executable_ref="managed:codex",
            capabilities=("stdio", "worktree"),
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-openai",
            version="4",
            provider="openai",
            model="gpt-5.4-codex",
            backend_mode="codex_cli",
            auth_mode="cli_auth",
            endpoint_kind="subscription",
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:dev",
            version="5",
            provider="openai",
            ref_kind="provider_home",
        ),
        launcher_adapter=LauncherAdapter(
            launcher_id="launcher-codex-exec",
            version="2",
            environment_keys=("CODEX_HOME",),
        ),
        role_policy=RolePolicy(
            policy_id="policy-dev",
            version="7",
            roles=("dev",),
            project_ids=("aming-claw",),
            timeout_sec=300,
        ),
    )


def test_models_are_versioned_immutable_and_public_safe():
    profile = _profile()

    with pytest.raises(FrozenInstanceError):
        profile.profile_id = "replacement"
    with pytest.raises(FrozenInstanceError):
        profile.harness_runtime.capabilities = ("replacement",)

    public = profile.to_public_dict()
    assert public["schema_version"] == "cli_agent_service.agent_profile.v1"
    assert public["credential_ref"]["credential_ref"] == (
        "credential:codex-home:dev"
    )
    assert public["credential_ref"]["raw_credential_material_exposed"] is False
    assert "credential_value" not in json.dumps(public)


def test_request_role_project_defaults_precedence_is_explicit():
    from cli_agent_service.config import resolve_agent_config
    from cli_agent_service.models import PUBLIC_CONFIGURATION_FIELDS

    run = resolve_agent_config(
        run_id="run-profile",
        role="dev",
        project_id="aming-claw",
        profile=_profile(),
        request_overrides={
            "provider": "openai",
            "model": "gpt-5.5-codex",
            "backend_mode": "codex_cli",
            "auth_mode": "cli_auth",
            "output_policy": "hash_and_summary_only",
        },
        project_config={
            "ai": {
                "routing": {
                    "dev": {"provider": "anthropic", "model": "claude-opus-4-6"}
                }
            }
        },
        pipeline_config={
            "roles": {
                "dev": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "backend_mode": "claude_cli",
                    "auth_mode": "cli_auth",
                }
            }
        },
    )

    assert run.config.provider == "openai"
    assert run.config.model == "gpt-5.5-codex"
    assert run.config.profile_id == "profile-codex-dev"
    assert {item.field_name for item in run.config.resolutions} == set(
        PUBLIC_CONFIGURATION_FIELDS
    )
    for field_name in (
        "profile_id",
        "runtime_id",
        "endpoint_id",
        "credential_ref",
        "launcher_id",
        "role_policy_id",
    ):
        assert run.config.resolution_for(field_name).source == "agent_profile"
    assert run.config.resolution_for("model").source == "request_overrides"
    provider_resolution = run.config.resolution_for("provider")
    assert provider_resolution.source == "request_overrides"
    assert [candidate.source for candidate in provider_resolution.candidates[:4]] == [
        "request_overrides",
        "pipeline_config.roles.dev",
        "project_config.ai.routing.dev",
        "agent_profile",
    ]


def test_role_precedes_project_and_keeps_routing_tuple_coherent():
    from cli_agent_service.config import resolve_agent_config

    run = resolve_agent_config(
        run_id="run-project-routing",
        role="dev",
        project_id="aming-claw",
        project_config={
            "ai": {
                "routing": {
                    "dev": {"provider": "openai", "model": "gpt-5.4-codex"}
                }
            }
        },
        pipeline_config={
            "default": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "backend_mode": "claude_cli",
                "auth_mode": "cli_auth",
            },
            "roles": {
                "dev": {
                    "provider": "anthropic",
                    "model": "claude-opus-4-6",
                    "backend_mode": "claude_cli",
                    "auth_mode": "cli_auth",
                    "output_policy": "redact_all",
                }
            },
        },
    )

    assert run.config.provider == "anthropic"
    assert run.config.model == "claude-opus-4-6"
    assert run.config.backend_mode == "claude_cli"
    assert run.config.auth_mode == "cli_auth"
    assert run.config.output_policy == "redact_all"
    assert run.config.resolution_for("provider").source == "pipeline_config.roles.dev"
    assert run.config.resolution_for("backend_mode").source == (
        "pipeline_config.roles.dev"
    )


def test_pipeline_role_precedes_default_and_compatibility_defaults():
    from cli_agent_service.config import resolve_agent_config

    run = resolve_agent_config(
        run_id="run-pipeline",
        role="qa",
        project_id="aming-claw",
        pipeline_config={
            "default": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
            },
            "roles": {"qa": {"provider": "openai", "model": "gpt-5.4-codex"}},
        },
        compatibility_defaults={
            "provider": "fixture",
            "model": "fixture-model",
            "output_policy": "redact_all",
        },
    )

    assert run.config.provider == "openai"
    assert run.config.model == "gpt-5.4-codex"
    assert run.config.backend_mode == "codex_cli"
    assert run.config.output_policy == "redact_all"
    assert run.config.resolution_for("provider").source == "pipeline_config.roles.qa"


def test_existing_run_cannot_be_overridden_by_legacy_or_another_profile():
    from cli_agent_service.config import resolve_agent_config

    original = resolve_agent_config(
        run_id="run-pinned",
        role="dev",
        project_id="aming-claw",
        profile=_profile(),
    )
    resolved_again = resolve_agent_config(
        run_id="run-pinned",
        role="dev",
        project_id="aming-claw",
        existing_run=original,
        pipeline_config={
            "roles": {
                "dev": {"provider": "anthropic", "model": "claude-opus-4-6"}
            }
        },
        compatibility_defaults={"provider": "fixture", "model": "fixture-model"},
    )

    assert resolved_again is original
    assert resolved_again.config.profile_id == "profile-codex-dev"
    assert resolved_again.config.provider == "openai"


def test_existing_run_reuse_pins_project_and_role_identity():
    from cli_agent_service.config import resolve_agent_config

    original = resolve_agent_config(
        run_id="run-pinned-identity",
        role="dev",
        project_id="aming-claw",
        profile=_profile(),
    )

    with pytest.raises(ValueError, match="existing project_id"):
        resolve_agent_config(
            run_id=original.run_id,
            role="dev",
            project_id="other-project",
            existing_run=original,
        )
    with pytest.raises(ValueError, match="existing role"):
        resolve_agent_config(
            run_id=original.run_id,
            role="qa",
            project_id="aming-claw",
            existing_run=original,
        )


def test_missing_routing_fails_closed_and_fixture_must_be_explicit():
    from cli_agent_service.config import resolve_agent_config

    with pytest.raises(ValueError, match="missing routing fields"):
        resolve_agent_config(
            run_id="run-unresolved",
            role="dev",
            project_id="aming-claw",
        )
    with pytest.raises(ValueError, match="model"):
        resolve_agent_config(
            run_id="run-missing-model",
            role="dev",
            project_id="aming-claw",
            compatibility_defaults={"provider": "openai"},
        )

    fixture_run = resolve_agent_config(
        run_id="run-explicit-fixture",
        role="test",
        project_id="aming-claw",
        compatibility_defaults={"provider": "fixture"},
    )
    assert fixture_run.config.provider == "fixture"
    assert fixture_run.config.backend_mode == "fixture"
    assert fixture_run.config.resolution_for("provider").source == (
        "compatibility_defaults"
    )


def test_governance_refs_are_allowlisted_and_value_validated():
    from cli_agent_service.config import resolve_agent_config

    run = resolve_agent_config(
        run_id="run-safe-refs",
        role="dev",
        project_id="aming-claw",
        profile=_profile(),
        governance_refs={
            "runtime_context_id": "mfrctx-example",
            "timeline_ref": "timeline:123",
        },
    )
    assert run.to_public_dict()["governance_refs"] == {
        "runtime_context_id": "mfrctx-example",
        "timeline_ref": "timeline:123",
    }

    unsafe_refs = (
        {"session_token": "raw-secret-value"},
        {"credential": "sk-secret-value"},
        {"runtime_context_id": "raw-secret-value"},
        {"session_token_ref": "raw-secret-value"},
    )
    for governance_refs in unsafe_refs:
        with pytest.raises(ValueError, match="governance reference"):
            resolve_agent_config(
                run_id="run-unsafe-ref",
                role="dev",
                project_id="aming-claw",
                profile=_profile(),
                governance_refs=governance_refs,
            )


def test_credential_refs_reject_raw_or_secret_shaped_values():
    from cli_agent_service.config import resolve_agent_config
    from cli_agent_service.models import CredentialRef

    for unsafe_ref in (
        "sk-proj-raw-secret-value",
        "credential:raw:provider-value",
        "credential:openai:sk-secret-value",
        "plain-reference",
    ):
        with pytest.raises(ValueError, match="credential reference"):
            CredentialRef(ref_id=unsafe_ref)

    with pytest.raises(ValueError, match="credential reference"):
        resolve_agent_config(
            run_id="run-raw-compat-credential",
            role="dev",
            project_id="aming-claw",
            profile=_profile(),
            compatibility_defaults={
                "credential_ref": "sk-proj-raw-secret-value",
            },
        )

    with pytest.raises(ValueError, match="unsupported public routing fields"):
        resolve_agent_config(
            run_id="run-raw-request-credential",
            role="dev",
            project_id="aming-claw",
            profile=_profile(),
            request_overrides={"credential_ref": "credential:provider-home:other"},
        )


def test_profile_role_and_project_policy_fail_closed():
    from cli_agent_service.config import resolve_agent_config

    with pytest.raises(ValueError, match="role 'qa' is not allowed"):
        resolve_agent_config(
            run_id="run-wrong-role",
            role="qa",
            project_id="aming-claw",
            profile=_profile(),
        )
    with pytest.raises(ValueError, match="project 'other' is not allowed"):
        resolve_agent_config(
            run_id="run-wrong-project",
            role="dev",
            project_id="other",
            profile=_profile(),
        )


def test_agent_run_adapts_to_existing_ai_invocation_request_contract():
    from ai_invocation import AIInvocationRequest
    from cli_agent_service.config import resolve_agent_config

    run = resolve_agent_config(
        run_id="run-adapter",
        role="dev",
        project_id="aming-claw",
        profile=_profile(),
        governance_refs={
            "runtime_context_id": "mfrctx-example",
            "contract_execution_id": "cex-example",
            "route_id": "route-example",
            "route_context_hash": "sha256:" + "1" * 64,
            "prompt_contract_id": "rprompt-example",
            "prompt_contract_hash": "sha256:" + "2" * 64,
            "route_token_ref": "rtok-" + "3" * 32,
            "visible_injection_manifest_hash": "sha256:" + "4" * 64,
            "snapshot_id": "qa-candidate-example",
        },
    )
    request = AIInvocationRequest.from_agent_run(
        run,
        prompt="private prompt",
        cwd="/repo",
        metadata={"caller": "test"},
    )

    assert type(request) is AIInvocationRequest
    assert request.provider == "openai"
    assert request.model == "gpt-5.4-codex"
    assert request.backend_mode == "codex_cli"
    assert request.auth_mode == "cli_auth"
    assert request.timeout_sec == 300
    assert request.metadata["caller"] == "test"
    adapter = request.metadata["cli_agent_service"]
    assert adapter["run_id"] == "run-adapter"
    assert adapter["credential_ref"] == "credential:codex-home:dev"
    assert adapter["governance_refs"]["runtime_context_id"] == "mfrctx-example"
    assert adapter["governance_refs"]["snapshot_id"] == "qa-candidate-example"
    assert adapter["raw_credential_material_exposed"] is False
    assert request.route.route_id == "route-example"
    assert request.route.route_context_hash == "sha256:" + "1" * 64
    assert request.route.prompt_contract_id == "rprompt-example"
    assert request.route.prompt_contract_hash == "sha256:" + "2" * 64
    assert request.route.route_token_ref == "rtok-" + "3" * 32
    assert request.route.visible_injection_manifest_hash == "sha256:" + "4" * 64
    assert request.to_evidence()["schema_version"] == "ai_invocation_request.v1"
