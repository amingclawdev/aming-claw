import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR))


def _fake_codex(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _control(tmp_path, *, ready=True):
    from cli_agent_service.auth import ProfileAuthController
    from cli_agent_service.profile_control import ManagedProfileControl
    from cli_agent_service.registry import AgentRegistry

    def runner(command, **_kwargs):
        args = tuple(command[1:])
        if args == ("plugin", "list", "--json"):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "installed": [
                            {
                                "pluginId": "aming-claw@aming-claw-local",
                                "version": "0.1.1+codex.20260713045902",
                                "installed": True,
                                "enabled": True,
                            }
                        ]
                    }
                ),
                "",
            )
        if args == ("mcp", "list", "--json"):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps([{"name": "aming-claw", "enabled": True}]),
                "",
            )
        if ready:
            return subprocess.CompletedProcess(command, 0, "Logged in", "")
        return subprocess.CompletedProcess(command, 1, "", "Not logged in")

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    auth = ProfileAuthController(
        tmp_path / "profiles",
        codex_executable=str(_fake_codex(tmp_path)),
        runner=runner,
    )
    return registry, auth, ManagedProfileControl(
        registry,
        auth,
        plugin_source_root=REPO_ROOT,
        tooling_runner=runner,
    )


def test_fixed_login_status_activate_and_list_register_only_ready_profile(tmp_path):
    from cli_agent_service.adapters.codex_cli import CODEX_CLI_DEFAULT_MODEL
    from cli_agent_service.config import resolve_agent_config

    registry, _auth, control = _control(tmp_path)

    prepared = control.dispatch(
        "profile_login_prepare",
        {"profile_id": "profile-codex-a", "provider": "codex"},
    )
    assert prepared["state"] == "login_in_progress"
    assert [item["action"] for item in prepared["actions"]] == [
        "open_terminal",
        "copy_command",
    ]
    assert all(item["user_triggered"] for item in prepared["actions"])
    assert all(item["auto_execute"] is False for item in prepared["actions"])
    assert registry.list_profiles() == ()

    status = control.dispatch(
        "profile_auth_status",
        {"profile_id": "profile-codex-a", "provider": "openai"},
    )
    assert status["state"] == "ready"
    assert status["profile_registered"] is False
    assert registry.list_profiles() == ()

    activated = control.dispatch(
        "profile_activate",
        {"profile_id": "profile-codex-a", "provider": "codex"},
    )
    profile = registry.get_profile("profile-codex-a")
    assert activated["activated"] is True
    assert activated["profile_registered"] is True
    assert profile is not None
    assert profile == registry.register_profile(profile)
    assert profile.credential_ref.ref_kind == "provider_home"
    assert profile.inference_endpoint.model == CODEX_CLI_DEFAULT_MODEL
    assert profile.launcher_adapter.environment_keys == ("CODEX_HOME",)
    assert profile.role_policy.max_concurrency == 1
    assert {"observer", "mf_sub", "qa"}.issubset(profile.role_policy.roles)
    observer_run = resolve_agent_config(
        run_id="run-managed-observer",
        role="observer",
        project_id="aming-claw",
        profile=profile,
        created_at="2026-07-14T12:00:00Z",
    )
    assert observer_run.config.role == "observer"
    assert observer_run.config.profile_id == profile.profile_id
    assert observer_run.config.model == CODEX_CLI_DEFAULT_MODEL

    listed = control.dispatch("profile_list", {})
    assert listed["profile_count"] == 1
    assert [item["profile_id"] for item in listed["profiles"]] == [
        "profile-codex-a"
    ]


def test_activation_does_not_register_when_auth_is_not_ready(tmp_path):
    registry, _auth, control = _control(tmp_path, ready=False)
    control.prepare_login("profile-codex-a")

    result = control.activate("profile-codex-a")

    assert result["state"] == "login_required"
    assert result["activated"] is False
    assert result["profile_registered"] is False
    assert registry.list_profiles() == ()


def test_legacy_managed_profile_remains_usable_after_default_model_change(tmp_path):
    from cli_agent_service.adapters.codex_cli import (
        CODEX_LEGACY_MANAGED_DEFAULT_MODELS,
    )
    from cli_agent_service.profile_control import _bounded_codex_profile

    registry, _auth, control = _control(tmp_path)
    legacy_model = next(iter(CODEX_LEGACY_MANAGED_DEFAULT_MODELS))
    legacy_profile = _bounded_codex_profile(
        "profile-codex-legacy",
        model=legacy_model,
    )
    registry.register_profile(legacy_profile)
    control.prepare_login(legacy_profile.profile_id)

    activated = control.activate(legacy_profile.profile_id)

    assert activated["profile_registered"] is True
    assert registry.get_profile(legacy_profile.profile_id) == legacy_profile
    assert control.resolve_profile_home(legacy_profile).is_dir()


@pytest.mark.parametrize(
    "field",
    (
        "argv",
        "command",
        "executable",
        "provider_home",
        "profile_home",
        "env",
        "environment",
        "credential",
        "credentials",
    ),
)
def test_profile_protocol_rejects_caller_owned_launch_or_auth_fields(
    tmp_path,
    field,
):
    _registry, _auth, control = _control(tmp_path)

    with pytest.raises(ValueError, match="unsupported fields"):
        control.dispatch(
            "profile_login_prepare",
            {
                "profile_id": "profile-codex-a",
                "provider": "codex",
                field: "caller-owned",
            },
        )


def test_managed_profile_ids_resolve_to_disjoint_server_owned_homes(tmp_path):
    registry, auth, control = _control(tmp_path)
    profiles = []
    homes = []
    for profile_id in ("profile-codex-a", "profile-codex-b"):
        control.prepare_login(profile_id)
        control.activate(profile_id)
        profile = registry.get_profile(profile_id)
        assert profile is not None
        profiles.append(profile)
        homes.append(control.resolve_profile_home(profile))

    assert homes[0] != homes[1]
    assert homes[0] == auth.managed_profile_home("profile-codex-a", "codex")
    assert homes[1] == auth.managed_profile_home("profile-codex-b", "codex")
    assert os.path.commonpath(homes) == str((tmp_path / "profiles" / "codex").resolve())

    forged_cross_selection = replace(profiles[0], profile_id=profiles[1].profile_id)
    with pytest.raises(ValueError, match="registered immutable profile"):
        control.resolve_profile_home(forged_cross_selection)


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is unavailable")
def test_repo_source_tooling_bootstrap_is_idempotent_visible_and_preserves_auth(
    tmp_path,
):
    from cli_agent_service.auth import ProfileAuthController
    from cli_agent_service.profile_control import ManagedProfileControl
    from cli_agent_service.registry import AgentRegistry

    codex = str(shutil.which("codex"))

    def ready_auth_runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, "Logged in", "")

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    auth = ProfileAuthController(
        tmp_path / "profiles",
        codex_executable=codex,
        runner=ready_auth_runner,
    )
    control = ManagedProfileControl(
        registry,
        auth,
        plugin_source_root=REPO_ROOT,
        tooling_runner=subprocess.run,
    )
    control.prepare_login("profile-codex-tooling")
    control.activate("profile-codex-tooling")
    profile = registry.get_profile("profile-codex-tooling")
    assert profile is not None
    home = auth.managed_profile_home(profile.profile_id, "codex")
    auth_path = home / "auth.json"
    auth_bytes = b'{"managed_test_secret":"must-remain-byte-identical"}\n'
    auth_path.write_bytes(auth_bytes)
    os.chmod(auth_path, 0o600)

    first_home = control.resolve_profile_home(profile)
    tracked = (
        home / "config.toml",
        home / "managed-tooling" / "readiness.json",
        home
        / "managed-tooling"
        / "aming-claw-local"
        / ".agents"
        / "plugins"
        / "marketplace.json",
        home
        / "plugins"
        / "cache"
        / "aming-claw-local"
        / "aming-claw"
        / "0.1.1+codex.20260713045902"
        / ".codex-plugin"
        / "plugin.json",
    )
    first_bytes = {path: path.read_bytes() for path in tracked}
    second_home = control.resolve_profile_home(profile)
    second_bytes = {path: path.read_bytes() for path in tracked}

    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    plugin_list = subprocess.run(
        (codex, "plugin", "list", "--json"),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPO_ROOT,
    )
    mcp_list = subprocess.run(
        (codex, "mcp", "list", "--json"),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPO_ROOT,
    )
    plugins = json.loads(plugin_list.stdout)["installed"]
    mcp_servers = json.loads(mcp_list.stdout)

    assert first_home == second_home == home
    assert first_bytes == second_bytes
    assert auth_path.read_bytes() == auth_bytes
    assert any(
        item["pluginId"] == "aming-claw@aming-claw-local"
        and item["enabled"] is True
        for item in plugins
    )
    assert any(
        item["name"] == "aming-claw" and item["enabled"] is True
        for item in mcp_servers
    )
    marker = json.loads((home / "managed-tooling" / "readiness.json").read_text())
    assert marker["desktop_plugin_cache_copied"] is False
    assert marker["raw_credentials_copied"] is False
