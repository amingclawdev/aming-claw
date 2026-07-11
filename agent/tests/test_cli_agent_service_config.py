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


def test_selected_profile_wins_and_explains_every_public_field():
    from cli_agent_service.config import resolve_agent_config
    from cli_agent_service.models import PUBLIC_CONFIGURATION_FIELDS

    run = resolve_agent_config(
        run_id="run-profile",
        role="dev",
        project_id="aming-claw",
        profile=_profile(),
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
    assert run.config.model == "gpt-5.4-codex"
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
        "provider",
        "model",
        "backend_mode",
        "auth_mode",
        "output_policy",
    ):
        assert run.config.resolution_for(field_name).source == "agent_profile"


def test_project_role_precedes_pipeline_and_keeps_routing_tuple_coherent():
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

    assert run.config.provider == "openai"
    assert run.config.model == "gpt-5.4-codex"
    assert run.config.backend_mode == "codex_cli"
    assert run.config.auth_mode == "cli_auth"
    assert run.config.output_policy == "redact_all"
    assert run.config.resolution_for("provider").source == (
        "project_config.ai.routing.dev"
    )
    assert run.config.resolution_for("backend_mode").source.endswith(
        ":derived_backend_mode"
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
        governance_refs={"runtime_context_id": "mfrctx-example"},
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
    assert adapter["raw_credential_material_exposed"] is False
    assert request.to_evidence()["schema_version"] == "ai_invocation_request.v1"
