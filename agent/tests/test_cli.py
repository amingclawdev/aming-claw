"""Tests for agent.cli — AC1, AC8."""

import os
import json
import pytest

try:
    from click.testing import CliRunner
    from agent.cli import main
    from agent.plugin_installer import (
        configure_codex_plugin,
        install_codex_marketplace,
        install_codex_plugin_cache,
    )
    HAS_CLICK = True
except ImportError:
    HAS_CLICK = False

pytestmark = pytest.mark.skipif(not HAS_CLICK, reason="click not installed")


class TestCliHelp:
    """AC1: aming-claw --help contains subcommands."""

    def test_help_output(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        for cmd in ("init", "bootstrap", "scan", "status", "start", "open", "launcher", "run-executor", "plugin"):
            assert cmd in result.output


class TestCliInit:
    """AC8: init creates .aming-claw.yaml."""

    def test_init_creates_yaml(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["init"])
            assert result.exit_code == 0
            assert os.path.exists(".aming-claw.yaml")

    def test_init_idempotent(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            runner.invoke(main, ["init"])
            result = runner.invoke(main, ["init"])
            assert "already exists" in result.output


class TestCliLauncher:
    def test_launcher_writes_local_html(self, tmp_path):
        runner = CliRunner()
        output = tmp_path / "launcher.html"

        result = runner.invoke(main, [
            "launcher",
            "--governance-url",
            "http://127.0.0.1:45555",
            "--output",
            str(output),
        ])

        assert result.exit_code == 0
        text = output.read_text(encoding="utf-8")
        assert "Aming Claw Launcher" in text
        assert "http://127.0.0.1:45555/dashboard" in text
        assert "aming-claw start" in text


class TestCliStart:
    def test_start_exits_when_governance_already_healthy(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        monkeypatch.setattr(
            cli,
            "_probe_governance",
            lambda port: {"status": "ok", "service": "governance", "version": "abc123", "port": port},
        )
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)

        result = runner.invoke(main, ["start", "--workspace", str(tmp_path), "--port", "45555"])

        assert result.exit_code == 0
        assert "Governance already running on port 45555" in result.output
        assert "http://localhost:45555/dashboard" in result.output

    def test_start_reports_non_governance_port_conflict(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: True)
        monkeypatch.setattr(cli, "_port_owner_hint", lambda port: " PID=1234")

        result = runner.invoke(main, ["start", "--workspace", str(tmp_path), "--port", "45555"])

        assert result.exit_code != 0
        assert "Port 45555 is already in use PID=1234" in result.output
        assert "not Aming Claw governance" in result.output


class TestCliPlugin:
    def test_plugin_install_dry_run_prints_plan(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(main, [
            "plugin",
            "install",
            "https://github.com/amingclawdev/aming-claw.git",
            "--install-root",
            str(tmp_path),
            "--dry-run",
            "--no-pip",
        ])

        assert result.exit_code == 0
        assert "Aming Claw plugin bootstrap" in result.output
        assert "git clone" in result.output
        assert "Claude Code: /plugin marketplace add" in result.output

    def test_plugin_doctor_reports_aftercare(self, tmp_path):
        runner = CliRunner()
        for rel, text in {
            ".codex-plugin/plugin.json": {"name": "aming-claw"},
            ".agents/plugins/marketplace.json": {
                "name": "aming-claw-local",
                "plugins": [
                    {
                        "name": "aming-claw",
                        "source": {"source": "local", "path": "./."},
                    }
                ],
            },
            ".claude-plugin/plugin.json": {"name": "aming-claw"},
            ".claude-plugin/marketplace.json": {"name": "aming-claw-local", "plugins": []},
            ".mcp.json": {"mcpServers": {"aming-claw": {"command": "python"}}},
        }.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(text), encoding="utf-8")
        for rel in ("skills/aming-claw/SKILL.md", "skills/aming-claw-launcher/SKILL.md"):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("---\nname: test\n---\n", encoding="utf-8")

        codex_home = tmp_path / "codex-home"
        marketplace_root = install_codex_marketplace(tmp_path, marketplace_root=tmp_path / "marketplace-root")
        install_codex_plugin_cache(tmp_path, codex_home=codex_home)
        config = configure_codex_plugin(
            codex_config=codex_home / "config.toml",
            marketplace_root=marketplace_root,
        )

        result = runner.invoke(main, [
            "plugin",
            "doctor",
            "--plugin-root",
            str(tmp_path),
            "--codex-config",
            str(config),
            "--codex-home",
            str(codex_home),
            "--skip-governance",
        ])

        assert result.exit_code == 0
        assert "Aming Claw plugin doctor" in result.output
        assert "Restart/reload Codex" in result.output
        assert "dashboard_static_assets" in result.output
        assert "ai_cli_openai" in result.output
