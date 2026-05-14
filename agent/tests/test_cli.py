"""Tests for agent.cli — AC1, AC8."""

import os
import pytest

try:
    from click.testing import CliRunner
    from agent.cli import main
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
