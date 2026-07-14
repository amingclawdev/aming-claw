import sys
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


def _profile(model):
    from cli_agent_service.adapters.codex_cli import CODEX_MANAGED_LAUNCHER_ID
    from cli_agent_service.models import (
        AgentProfile,
        CredentialRef,
        HarnessRuntime,
        InferenceEndpoint,
        LauncherAdapter,
        RolePolicy,
    )

    return AgentProfile(
        profile_id="profile-codex-managed",
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-codex-managed",
            kind="codex_cli",
            executable_ref="managed:codex",
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-openai-managed-codex",
            provider="openai",
            model=model,
            backend_mode="codex_cli",
            auth_mode="cli_auth",
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:managed",
            provider="openai",
            ref_kind="provider_home",
        ),
        launcher_adapter=LauncherAdapter(launcher_id=CODEX_MANAGED_LAUNCHER_ID),
        role_policy=RolePolicy(policy_id="policy-managed", roles=("mf_sub",)),
    )


def _run(model, *, override=""):
    from cli_agent_service.config import resolve_agent_config

    return resolve_agent_config(
        run_id="run-codex-managed",
        role="mf_sub",
        project_id="aming-claw",
        profile=_profile(model),
        request_overrides={"model": override} if override else None,
        created_at="2026-07-14T12:00:00Z",
    )


def _launch_command(tmp_path, run):
    from cli_agent_service.adapters.codex_cli import CodexCliAdapter

    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return CodexCliAdapter(executable=str(executable)).build_launch_spec(
        run,
        worktree=tmp_path,
        output_path=tmp_path / "last.txt",
    ).command


def test_managed_cli_default_model_does_not_add_model_argument(tmp_path):
    from cli_agent_service.adapters.codex_cli import CODEX_CLI_DEFAULT_MODEL

    command = _launch_command(tmp_path, _run(CODEX_CLI_DEFAULT_MODEL))

    assert "--model" not in command


def test_legacy_managed_profile_default_does_not_add_model_argument(tmp_path):
    from cli_agent_service.adapters.codex_cli import (
        CODEX_LEGACY_MANAGED_DEFAULT_MODELS,
    )

    legacy_model = next(iter(CODEX_LEGACY_MANAGED_DEFAULT_MODELS))
    command = _launch_command(tmp_path, _run(legacy_model))

    assert "--model" not in command


def test_explicit_nonempty_model_is_preserved_for_managed_profile(tmp_path):
    from cli_agent_service.adapters.codex_cli import (
        CODEX_LEGACY_MANAGED_DEFAULT_MODELS,
    )

    explicit_model = next(iter(CODEX_LEGACY_MANAGED_DEFAULT_MODELS))
    command = _launch_command(
        tmp_path,
        _run(explicit_model, override=explicit_model),
    )

    model_index = command.index("--model")
    assert command[model_index + 1] == explicit_model
