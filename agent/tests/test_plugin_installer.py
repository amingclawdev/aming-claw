"""Tests for Git URL plugin bootstrap helpers."""

import json
from pathlib import Path

import pytest

from agent.plugin_installer import (
    AI_CLI_REQUIREMENTS,
    CODEX_PLUGIN_ID,
    PluginInstallError,
    _check_ai_cli,
    configure_codex_plugin,
    doctor_plugin,
    format_result,
    format_doctor_result,
    install_codex_marketplace,
    install_codex_plugin_cache,
    install_from_git,
    plugin_root_for,
    slug_from_repo_url,
    validate_plugin_root,
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
        ".claude-plugin/plugin.json": {"name": "aming-claw"},
        ".claude-plugin/marketplace.json": {"name": "aming-claw-local", "plugins": []},
        ".mcp.json": {"mcpServers": {"aming-claw": {"command": "python"}}},
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(text), encoding="utf-8")

    for rel in ("skills/aming-claw/SKILL.md", "skills/aming-claw-launcher/SKILL.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: test\n---\n", encoding="utf-8")


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

    target = install_codex_plugin_cache(tmp_path, codex_home=codex_home)

    assert target == codex_home / "plugins" / "cache" / "aming-claw-local" / "aming-claw" / "0.1.0"
    assert (target / ".codex-plugin" / "plugin.json").is_file()
    assert (target / "skills" / "aming-claw" / "SKILL.md").is_file()


def test_configure_codex_plugin_enables_plugin_and_valid_marketplace(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace_root = install_codex_marketplace(tmp_path, marketplace_root=tmp_path / "marketplace-root")
    config_path = configure_codex_plugin(
        codex_config=tmp_path / "config.toml",
        marketplace_root=marketplace_root,
    )
    text = config_path.read_text(encoding="utf-8")

    assert f'[plugins."{CODEX_PLUGIN_ID}"]' in text
    assert "enabled = true" in text
    assert str(marketplace_root).replace("\\", "\\\\") in text


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
