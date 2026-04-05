"""Tests for agent.config.AmingConfig — AC6."""

import os
import tempfile
import textwrap

from agent.config import AmingConfig


class TestAmingConfigDefaults:
    """Defaults are applied when no env or yaml."""

    def test_default_governance_port(self):
        cfg = AmingConfig()
        assert cfg.governance_port == 40000

    def test_default_notification_backend(self):
        cfg = AmingConfig()
        assert cfg.notification_backend == "telegram"

    def test_default_max_workers(self):
        cfg = AmingConfig()
        assert cfg.max_workers == 4


class TestAmingConfigEnv:
    """AC6a: env vars override everything."""

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("AMING_PROJECT_ID", "env-proj")
        monkeypatch.setenv("AMING_GOVERNANCE_PORT", "9999")
        monkeypatch.setenv("AMING_MAX_WORKERS", "8")
        cfg = AmingConfig.load()
        assert cfg.project_id == "env-proj"
        assert cfg.governance_port == 9999
        assert cfg.max_workers == 8

    def test_env_overrides_yaml(self, monkeypatch, tmp_path):
        yaml_file = tmp_path / ".aming-claw.yaml"
        yaml_file.write_text("project_id: yaml-proj\ngovernance_port: 5000\n")
        monkeypatch.setenv("AMING_PROJECT_ID", "env-proj")
        cfg = AmingConfig.load(yaml_path=str(yaml_file))
        assert cfg.project_id == "env-proj"  # env wins
        assert cfg.governance_port == 5000   # yaml (no env set)


class TestAmingConfigYaml:
    """AC6b: yaml overrides defaults."""

    def test_yaml_loads(self, tmp_path):
        yaml_file = tmp_path / ".aming-claw.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            project_id: yaml-test
            governance_port: 50000
            max_workers: 16
        """))
        cfg = AmingConfig.load(yaml_path=str(yaml_file))
        assert cfg.project_id == "yaml-test"
        assert cfg.governance_port == 50000
        assert cfg.max_workers == 16

    def test_missing_yaml_uses_defaults(self, tmp_path):
        cfg = AmingConfig.load(yaml_path=str(tmp_path / "nonexistent.yaml"))
        assert cfg.governance_port == 40000


class TestAmingConfigPriority:
    """AC6: Full priority chain env > yaml > defaults."""

    def test_full_chain(self, monkeypatch, tmp_path):
        yaml_file = tmp_path / ".aming-claw.yaml"
        yaml_file.write_text("project_id: yaml\nworkspace_path: /yaml\ngovernance_port: 7777\n")
        monkeypatch.setenv("AMING_PROJECT_ID", "env")
        cfg = AmingConfig.load(yaml_path=str(yaml_file))
        assert cfg.project_id == "env"             # env wins
        assert cfg.workspace_path == "/yaml"        # yaml wins (no env)
        assert cfg.governance_port == 7777           # yaml wins (no env)
        assert cfg.max_workers == 4                  # default
