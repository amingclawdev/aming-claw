"""Tests for Git URL plugin bootstrap helpers."""

import json
from pathlib import Path

import pytest

from agent.plugin_installer import (
    AI_CLI_REQUIREMENTS,
    PluginInstallError,
    _check_ai_cli,
    doctor_plugin,
    format_result,
    format_doctor_result,
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
                    "source": {"source": "local", "path": "./"},
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
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        'marketplace = "aming-claw-local"\nplugin = "aming-claw"\n',
        encoding="utf-8",
    )

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        check_governance=False,
    )

    assert result.ok is True
    assert {check.name for check in result.checks} >= {
        "plugin_assets",
        "codex_marketplace",
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
    assert result.ok is False
    assert checks["codex_marketplace"].status == "fail"
    assert ".codex-plugin/plugin.json" in checks["codex_marketplace"].detail
