"""Tests for Git URL plugin bootstrap helpers."""

import json
from pathlib import Path

import pytest

from agent.plugin_installer import (
    AI_CLI_REQUIREMENTS,
    CODEX_PLUGIN_ID,
    PluginInstallError,
    _check_ai_cli,
    _check_claude_manifest,
    _check_claude_marketplace,
    _load_toml_text,
    _upsert_toml_table,
    configure_codex_plugin,
    default_plugin_update_state_path,
    doctor_plugin,
    format_plugin_update_state_status,
    format_result,
    format_doctor_result,
    install_codex_marketplace,
    install_codex_plugin_cache,
    install_from_git,
    plugin_update_state_status,
    plugin_root_for,
    slug_from_repo_url,
    validate_plugin_root,
    write_plugin_update_state,
)


def _write_plugin_fixture(root: Path) -> None:
    for rel, text in {
        ".codex-plugin/plugin.json": {"name": "aming-claw"},
        ".agents/plugins/marketplace.json": {
            "name": "aming-claw-local",
            "plugins": [
                {
                    "name": "aming-claw",
                    "source": {"source": "local", "path": "./."},
                    "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                }
            ],
        },
        ".claude-plugin/plugin.json": {
            "name": "aming-claw",
            "version": "0.1.0",
            "description": "Test plugin.",
            "mcpServers": {
                "aming-claw": {
                    "command": "python",
                    "args": ["-m", "agent.mcp.server"],
                }
            },
        },
        ".claude-plugin/marketplace.json": {
            "name": "aming-claw-local",
            "metadata": {"description": "Test marketplace."},
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": "./", "version": "0.1.0"}
            ],
        },
        ".mcp.json": {
            "mcpServers": {
                "aming-claw": {
                    "command": "python",
                    "args": [
                        "-m",
                        "agent.mcp.server",
                        "--project",
                        "aming-claw",
                        "--workers",
                        "0",
                    ],
                    "cwd": ".",
                    "env": {"PYTHONDONTWRITEBYTECODE": "1"},
                }
            }
        },
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(text), encoding="utf-8")

    for rel in ("skills/aming-claw/SKILL.md", "skills/aming-claw-launcher/SKILL.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: test\n---\n", encoding="utf-8")
    server_path = root / "agent" / "mcp" / "server.py"
    server_path.parent.mkdir(parents=True, exist_ok=True)
    server_path.write_text("# test runtime entrypoint\n", encoding="utf-8")


def test_slug_from_repo_url_handles_https_and_git_suffix():
    assert slug_from_repo_url("https://github.com/amingclawdev/aming-claw.git") == "aming-claw"
    assert slug_from_repo_url("git@github.com:amingclawdev/aming-claw.git") == "aming-claw"


def test_validate_plugin_root_requires_expected_assets(tmp_path):
    with pytest.raises(PluginInstallError, match="plugin root is missing required files"):
        validate_plugin_root(tmp_path)

    _write_plugin_fixture(tmp_path)

    validated = validate_plugin_root(tmp_path)
    assert ".codex-plugin/plugin.json" in validated
    assert "skills/aming-claw/SKILL.md" in validated


def test_default_plugin_update_state_path_uses_user_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AMING_CLAW_PLUGIN_STATE_HOME", str(tmp_path / "state-home"))

    path = default_plugin_update_state_path()

    assert path == tmp_path / "state-home" / "aming-claw-local" / "aming-claw.json"


def test_plugin_update_state_status_warns_when_missing(tmp_path):
    result = plugin_update_state_status(state_path=tmp_path / "missing.json")

    assert result["ok"] is True
    assert result["status"] == "warn"
    assert result["update_status"] == "unknown"
    assert "not found" in result["warnings"][0]


def test_plugin_update_state_status_blocks_pending_restart(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "plugin_id": "aming-claw@aming-claw-local",
        "update_status": "applied_pending_restart",
        "restart_required": {
            "mcp": {
                "required": True,
                "reason": "skills changed",
                "satisfied_by": "open a new session",
            }
        },
    }), encoding="utf-8")

    result = plugin_update_state_status(state_path=state_path)

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert "mcp" in result["blockers"][0]
    assert "required" in format_plugin_update_state_status(result)


def test_plugin_update_state_status_blocks_failed_update(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "plugin_id": "aming-claw@aming-claw-local",
        "update_status": "failed",
    }), encoding="utf-8")

    result = plugin_update_state_status(state_path=state_path)

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert "failed" in result["blockers"][0]


def test_write_plugin_update_state_records_current_install(tmp_path):
    _write_plugin_fixture(tmp_path)
    state_path = write_plugin_update_state(
        plugin_root=tmp_path,
        repo_url="https://github.com/amingclawdev/aming-claw.git",
        state_path=tmp_path / "state" / "plugin.json",
    )

    result = plugin_update_state_status(state_path=state_path)

    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["state"]["installed_version"] == "0.1.0"


def test_install_from_git_dry_run_plans_fresh_clone_without_writing(tmp_path):
    repo_url = "https://github.com/amingclawdev/aming-claw.git"

    result = install_from_git(
        repo_url,
        install_root=tmp_path,
        dry_run=True,
        install_package=False,
    )

    plugin_root = plugin_root_for(repo_url, tmp_path)
    assert not plugin_root.exists()
    assert result.dry_run is True
    assert result.validated_files == []
    assert result.commands[0].args[:2] == ["git", "clone"]
    assert "Claude Code: /plugin marketplace add" in "\n".join(result.next_steps)


def test_install_from_git_validate_only_existing_checkout(tmp_path):
    repo_url = "https://github.com/amingclawdev/aming-claw.git"
    plugin_root = plugin_root_for(repo_url, tmp_path)
    _write_plugin_fixture(plugin_root)

    result = install_from_git(
        repo_url,
        install_root=tmp_path,
        dry_run=True,
        validate_only=True,
        install_package=False,
    )

    assert result.validated_files
    assert result.commands == []
    assert str(plugin_root) in format_result(result)


def test_doctor_plugin_validates_aftercare_without_governance(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"
    install_codex_plugin_cache(tmp_path, codex_home=codex_home)
    install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)
    codex_config = configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_root,
    )

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=codex_home,
        check_governance=False,
    )

    assert result.ok is True
    assert {check.name for check in result.checks} >= {
        "plugin_assets",
        "codex_marketplace",
        "codex_plugin_cache",
        "claude_marketplace",
        "claude_manifest",
        "mcp_config",
        "codex_config",
        "dashboard_static_assets",
        "ai_cli_openai",
        "ai_cli_anthropic",
    }
    assert "Restart/reload Codex" in format_doctor_result(result)
    assert "auth unknown" in format_doctor_result(result) or "missing" in format_doctor_result(result)


def test_ai_cli_check_uses_env_override(monkeypatch, tmp_path):
    fake_codex = tmp_path / "codex-custom"
    monkeypatch.setenv("CODEX_BIN", str(fake_codex))

    class _Proc:
        returncode = 0
        stdout = "codex-cli 9.9.9\n"
        stderr = ""

    def fake_run(args, **_kwargs):
        assert args == [str(fake_codex), "--version"]
        return _Proc()

    monkeypatch.setattr("agent.plugin_installer.subprocess.run", fake_run)

    check = _check_ai_cli("openai", AI_CLI_REQUIREMENTS["openai"])

    assert check.status == "ok"
    assert str(fake_codex) in check.detail
    assert "auth unknown" in check.detail


def test_doctor_plugin_flags_bad_marketplace_path(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    payload["plugins"][0]["source"]["path"] = ".agents/plugins"
    marketplace.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=tmp_path / "missing-config.toml",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert checks["codex_marketplace"].status == "warn"
    assert ".codex-plugin/plugin.json" in checks["codex_marketplace"].detail


def test_doctor_plugin_rejects_empty_root_marketplace_path(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    payload["plugins"][0]["source"]["path"] = "./"
    marketplace.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=tmp_path / "missing-config.toml",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_marketplace"].status == "fail"
    assert "empty local plugin path" in checks["codex_marketplace"].detail


def test_doctor_plugin_fails_when_enabled_cache_is_missing(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"
    install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)
    codex_config = configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_root,
    )

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=codex_home,
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_plugin_cache"].status == "fail"
    assert "missing installed plugin cache" in checks["codex_plugin_cache"].detail


def test_install_codex_plugin_cache_uses_versioned_codex_loader_layout(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"

    target = install_codex_plugin_cache(tmp_path, codex_home=codex_home, python_executable="python3.12")

    assert target == codex_home / "plugins" / "cache" / "aming-claw-local" / "aming-claw" / "0.1.0"
    assert (target / ".codex-plugin" / "plugin.json").is_file()
    assert (target / "skills" / "aming-claw" / "SKILL.md").is_file()
    assert not (target / "agent" / "mcp" / "server.py").exists()

    mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp["mcpServers"]["aming-claw"]
    assert server["command"] == "python3.12"
    assert server["cwd"] == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in server["env"]["PYTHONPATH"].split(":") or str(tmp_path.resolve()) in server["env"]["PYTHONPATH"].split(";")
    assert server["args"][:2] == ["-m", "agent.mcp.server"]


def test_doctor_plugin_fails_when_cache_mcp_cannot_import_runtime(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"
    cache_target = install_codex_plugin_cache(tmp_path, codex_home=codex_home)
    install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)
    codex_config = configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_root,
    )
    mcp_path = cache_target / ".mcp.json"
    payload = json.loads(mcp_path.read_text(encoding="utf-8"))
    payload["mcpServers"]["aming-claw"]["cwd"] = "."
    payload["mcpServers"]["aming-claw"]["env"].pop("PYTHONPATH", None)
    mcp_path.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=codex_home,
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_plugin_cache"].status == "fail"
    assert "cannot import agent.mcp.server" in checks["codex_plugin_cache"].detail


def test_configure_codex_plugin_enables_plugin_and_valid_marketplace(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace_root = install_codex_marketplace(tmp_path, marketplace_root=tmp_path / "marketplace-root")
    config_path = configure_codex_plugin(
        codex_config=tmp_path / "config.toml",
        marketplace_root=marketplace_root,
    )
    text = config_path.read_text(encoding="utf-8")
    parsed = _load_toml_text(text)

    assert f'[plugins."{CODEX_PLUGIN_ID}"]' in text
    assert "enabled = true" in text
    assert parsed["marketplaces"]["aming-claw-local"]["source"] == str(marketplace_root.resolve())
    assert f"source = '{marketplace_root.resolve()}'" in text


def test_upsert_toml_table_replaces_windows_path_without_regex_escape_error():
    old_text = "[marketplaces.aming-claw-local]\nsource = 'old'\n"
    windows_path = "C:" + "\\Users\\z5866\\.aming-claw\\plugins\\aming-claw"

    text = _upsert_toml_table(
        old_text,
        "marketplaces.aming-claw-local",
        f"source_type = \"local\"\nsource = '{windows_path}'",
    )

    parsed = _load_toml_text(text)
    assert parsed["marketplaces"]["aming-claw-local"]["source"] == windows_path


def test_doctor_plugin_fails_on_invalid_codex_config_toml(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_config = tmp_path / "config.toml"
    codex_config.write_text("[plugins.\n", encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=tmp_path / "codex-home",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_config"].status == "fail"
    assert "invalid TOML" in checks["codex_config"].detail


def test_doctor_plugin_rejects_manifest_with_too_many_default_prompts(tmp_path):
    _write_plugin_fixture(tmp_path)
    manifest = tmp_path / ".codex-plugin" / "plugin.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["interface"] = {"defaultPrompt": ["one", "two", "three", "four"]}
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=tmp_path / "missing-config.toml",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_manifest"].status == "fail"
    assert "at most 3" in checks["codex_manifest"].detail


def test_install_from_git_rejects_unsupported_python_before_pip(tmp_path, monkeypatch):
    repo_url = "https://github.com/amingclawdev/aming-claw.git"
    plugin_root = plugin_root_for(repo_url, tmp_path)
    _write_plugin_fixture(plugin_root)

    def fake_run(args, **_kwargs):
        class _Proc:
            returncode = 0
            stdout = "Python 3.8.18\n"
            stderr = ""

        assert args == ["old-python", "--version"]
        return _Proc()

    monkeypatch.setattr("agent.plugin_installer.subprocess.run", fake_run)

    with pytest.raises(PluginInstallError, match="requires Python 3.9"):
        install_from_git(
            repo_url,
            install_root=tmp_path,
            validate_only=True,
            python_executable="old-python",
            install_package=True,
        )


def test_check_claude_marketplace_passes_on_valid_manifest(tmp_path):
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "marketplace.json").write_text(
        json.dumps({
            "name": "aming-claw-local",
            "metadata": {"description": "Test marketplace."},
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": "./", "version": "0.1.0"}
            ],
        }),
        encoding="utf-8",
    )
    check = _check_claude_marketplace(tmp_path)
    assert check.status == "ok"


def test_check_claude_marketplace_fails_on_bare_dot_source(tmp_path):
    """MF #1 P0: claude plugin validate rejects plugins[].source=='.' as Invalid input."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "marketplace.json").write_text(
        json.dumps({
            "name": "aming-claw-local",
            "metadata": {"description": "Test."},
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": ".", "version": "0.1.0"}
            ],
        }),
        encoding="utf-8",
    )
    check = _check_claude_marketplace(tmp_path)
    assert check.status == "fail"
    assert "must start with './'" in check.detail


def test_check_claude_marketplace_warns_on_missing_metadata_description(tmp_path):
    """MF #1 secondary: claude plugin validate warns when metadata.description is missing."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "marketplace.json").write_text(
        json.dumps({
            "name": "aming-claw-local",
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": "./", "version": "0.1.0"}
            ],
        }),
        encoding="utf-8",
    )
    check = _check_claude_marketplace(tmp_path)
    assert check.status == "warn"
    assert "metadata.description" in check.detail


def test_check_claude_manifest_passes_when_mcpservers_declared(tmp_path):
    """MF #2a: declared mcpServers is the manifest-level fix."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "plugin.json").write_text(
        json.dumps({
            "name": "aming-claw",
            "version": "0.1.0",
            "description": "Test plugin.",
            "mcpServers": {
                "aming-claw": {
                    "command": "python",
                    "args": ["-m", "agent.mcp.server"],
                }
            },
        }),
        encoding="utf-8",
    )
    check = _check_claude_manifest(tmp_path)
    assert check.status == "ok"


def test_check_claude_manifest_warns_when_no_mcpservers(tmp_path):
    """Without mcpServers the Claude plugin install will not expose an MCP server."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "plugin.json").write_text(
        json.dumps({
            "name": "aming-claw",
            "version": "0.1.0",
            "description": "Test plugin.",
        }),
        encoding="utf-8",
    )
    check = _check_claude_manifest(tmp_path)
    assert check.status == "warn"
    assert "mcpServers" in check.detail


def test_check_claude_manifest_fails_when_required_field_missing(tmp_path):
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "plugin.json").write_text(
        json.dumps({
            "name": "aming-claw",
            "version": "0.1.0",
            # description intentionally missing
        }),
        encoding="utf-8",
    )
    check = _check_claude_manifest(tmp_path)
    assert check.status == "fail"
    assert "description" in check.detail
