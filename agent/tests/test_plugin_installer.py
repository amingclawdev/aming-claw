"""Tests for Git URL plugin bootstrap helpers."""

import json
from pathlib import Path

import pytest

from agent.plugin_installer import (
    PluginInstallError,
    format_result,
    install_from_git,
    plugin_root_for,
    slug_from_repo_url,
    validate_plugin_root,
)


def _write_plugin_fixture(root: Path) -> None:
    for rel, text in {
        ".codex-plugin/plugin.json": {"name": "aming-claw"},
        ".agents/plugins/marketplace.json": {"name": "aming-claw-local", "plugins": []},
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
