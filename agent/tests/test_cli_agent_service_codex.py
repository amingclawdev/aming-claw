import sys
from dataclasses import replace
from pathlib import Path

import pytest


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


def _profile(*, backend_mode="codex_cli"):
    from cli_agent_service.models import (
        AgentProfile,
        CredentialRef,
        HarnessRuntime,
        InferenceEndpoint,
        LauncherAdapter,
        RolePolicy,
    )

    return AgentProfile(
        profile_id="profile-codex-inherited",
        version="1",
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-codex",
            kind="codex_cli",
            executable_ref="managed:codex",
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-openai",
            provider="openai",
            model="gpt-5.4-codex",
            backend_mode=backend_mode,
            auth_mode="cli_auth",
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:inherited",
            provider="openai",
            ref_kind="inherited_current",
        ),
        launcher_adapter=LauncherAdapter(launcher_id="launcher-codex-exec"),
        role_policy=RolePolicy(
            policy_id="policy-dev",
            roles=("dev",),
            project_ids=("aming-claw",),
            max_concurrency=1,
        ),
    )


def _run(*, backend_mode="codex_cli"):
    from cli_agent_service.config import resolve_agent_config

    return resolve_agent_config(
        run_id="run-codex",
        role="dev",
        project_id="aming-claw",
        profile=_profile(backend_mode=backend_mode),
        created_at="2026-07-12T12:00:00Z",
    )


def _fake_codex(tmp_path):
    path = tmp_path / "codex"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "output = args[args.index('-o') + 1]\n"
        "prompt = sys.stdin.read()\n"
        "if prompt.startswith('sleep'):\n"
        "    time.sleep(10)\n"
        "pathlib.Path(output).write_text('completed:' + prompt, encoding='utf-8')\n"
        "print('{\"event\":\"completed\"}')\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _fake_resolver_executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _enable_desktop_fallback(monkeypatch, codex_cli, executable):
    monkeypatch.setattr(codex_cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        codex_cli,
        "MACOS_CODEX_APP_BUNDLE_EXECUTABLES",
        (str(executable),),
    )


def test_codex_executable_explicit_precedes_env_path_and_desktop(
    tmp_path, monkeypatch
):
    from cli_agent_service.adapters import codex_cli

    explicit = _fake_resolver_executable(tmp_path / "explicit" / "codex")
    configured = _fake_resolver_executable(tmp_path / "configured" / "codex")
    path_codex = _fake_resolver_executable(tmp_path / "path" / "codex")
    desktop = _fake_resolver_executable(tmp_path / "desktop" / "codex")
    monkeypatch.setenv("CODEX_BIN", str(configured))
    monkeypatch.setenv("PATH", str(path_codex.parent))
    _enable_desktop_fallback(monkeypatch, codex_cli, desktop)

    assert codex_cli.CodexCliAdapter(
        executable=str(explicit)
    ).resolve_executable() == str(explicit)


def test_codex_executable_env_precedes_path_and_desktop(tmp_path, monkeypatch):
    from cli_agent_service.adapters import codex_cli

    configured = _fake_resolver_executable(tmp_path / "configured" / "codex")
    path_codex = _fake_resolver_executable(tmp_path / "path" / "codex")
    desktop = _fake_resolver_executable(tmp_path / "desktop" / "codex")
    monkeypatch.setenv("CODEX_BIN", str(configured))
    monkeypatch.setenv("PATH", str(path_codex.parent))
    _enable_desktop_fallback(monkeypatch, codex_cli, desktop)

    assert codex_cli.CodexCliAdapter().resolve_executable() == str(configured)


def test_codex_executable_path_precedes_desktop(tmp_path, monkeypatch):
    from cli_agent_service.adapters import codex_cli

    path_codex = _fake_resolver_executable(tmp_path / "path" / "codex")
    desktop = _fake_resolver_executable(tmp_path / "desktop" / "codex")
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", str(path_codex.parent))
    _enable_desktop_fallback(monkeypatch, codex_cli, desktop)

    assert codex_cli.CodexCliAdapter().resolve_executable() == str(path_codex)


def test_invalid_explicit_codex_executable_fails_closed(tmp_path, monkeypatch):
    from cli_agent_service.adapters import codex_cli

    configured = _fake_resolver_executable(tmp_path / "configured" / "codex")
    path_codex = _fake_resolver_executable(tmp_path / "path" / "codex")
    desktop = _fake_resolver_executable(tmp_path / "desktop" / "codex")
    monkeypatch.setenv("CODEX_BIN", str(configured))
    monkeypatch.setenv("PATH", str(path_codex.parent))
    _enable_desktop_fallback(monkeypatch, codex_cli, desktop)

    with pytest.raises(codex_cli.CodexAdapterError, match="configured.*unavailable"):
        codex_cli.CodexCliAdapter(
            executable=str(tmp_path / "missing-codex")
        ).resolve_executable()


def test_codex_managed_launch_uses_chatgpt_desktop_fallback(
    tmp_path, monkeypatch
):
    from cli_agent_service.adapters import codex_cli

    desktop = _fake_resolver_executable(tmp_path / "desktop" / "codex")
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    _enable_desktop_fallback(monkeypatch, codex_cli, desktop)

    launch = codex_cli.CodexCliAdapter().build_launch_spec(
        _run(),
        worktree=tmp_path,
        output_path=tmp_path / "last.txt",
    )

    assert launch.command[0] == str(desktop)


def test_codex_desktop_fallback_is_not_used_off_macos(tmp_path, monkeypatch):
    from cli_agent_service.adapters import codex_cli

    desktop = _fake_resolver_executable(tmp_path / "desktop" / "codex")
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(codex_cli.sys, "platform", "linux")
    monkeypatch.setattr(
        codex_cli,
        "MACOS_CODEX_APP_BUNDLE_EXECUTABLES",
        (str(desktop),),
    )

    with pytest.raises(codex_cli.CodexAdapterError, match="unavailable"):
        codex_cli.CodexCliAdapter().resolve_executable()


def test_codex_adapter_builds_bounded_inherited_profile_command(tmp_path):
    from cli_agent_service.adapters.codex_cli import CodexCliAdapter

    executable = _fake_codex(tmp_path)
    adapter = CodexCliAdapter(executable=str(executable))
    output_path = tmp_path / "last.txt"
    launch = adapter.build_launch_spec(_run(), worktree=tmp_path, output_path=output_path)
    command = list(launch.command)
    assert command[:2] == [str(executable), "exec"]
    assert "--model" in command
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert command[-4:] == ["-C", str(tmp_path.resolve()), "-o", str(output_path)]
    assert launch.environment is None
    assert "credential:codex-home:inherited" not in " ".join(command)
    assert "private prompt" not in " ".join(command)


def test_codex_adapter_rejects_non_codex_route(tmp_path):
    from cli_agent_service.adapters.codex_cli import CodexAdapterError, CodexCliAdapter

    executable = _fake_codex(tmp_path)
    run = _run()
    invalid_run = replace(run, config=replace(run.config, backend_mode="claude_cli"))
    with pytest.raises(CodexAdapterError, match="not routed"):
        CodexCliAdapter(executable=str(executable)).build_launch_spec(
            invalid_run,
            worktree=tmp_path,
            output_path=tmp_path / "last.txt",
        )


def test_adapter_failure_happens_before_registry_lease(tmp_path):
    from cli_agent_service.adapters.codex_cli import CodexAdapterError, CodexCliAdapter
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.supervisor import CodexC0Supervisor

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    supervisor = CodexC0Supervisor(
        registry,
        state_dir=tmp_path / "state",
        adapter=CodexCliAdapter(executable=str(tmp_path / "missing-codex")),
    )
    with pytest.raises(CodexAdapterError, match="unavailable"):
        supervisor.start_run(_run(), prompt="private prompt", worktree=tmp_path)
    assert registry.get_run("run-codex") is None
