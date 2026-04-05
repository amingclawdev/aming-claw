"""Tests for package installation — AC5, AC7, AC10."""

import importlib
import subprocess
import sys


class TestImportWithoutRedis:
    """AC5: agent package imports without redis installed."""

    def test_agent_import(self):
        import agent
        assert hasattr(agent, "AmingConfig")

    def test_redis_client_has_fallback(self):
        from agent.governance.redis_client import HAS_REDIS
        # HAS_REDIS may be True or False depending on environment,
        # but the import itself must not fail.
        assert isinstance(HAS_REDIS, bool)


class TestPublicAPI:
    """AC7: from aming_claw import AmingConfig works."""

    def test_aming_claw_import(self):
        mod = importlib.import_module("aming_claw")
        assert hasattr(mod, "AmingConfig")

    def test_agent_public_api(self):
        from agent import AmingConfig, bootstrap_project, create_task
        assert callable(bootstrap_project)
        assert callable(create_task)


class TestPyprojectOptionalDeps:
    """AC10: pyproject.toml has optional dependency groups."""

    def test_optional_deps_in_toml(self):
        import os
        toml_path = os.path.join(os.path.dirname(__file__), "..", "..", "pyproject.toml")
        try:
            import tomllib
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
        except ImportError:
            # Python < 3.11 fallback: use pip's vendored tomli (expects str)
            try:
                import pip._vendor.tomli as _tomli
                with open(toml_path, "r", encoding="utf-8") as f:
                    data = _tomli.loads(f.read())
            except (ImportError, AttributeError):
                import tomli as _tomli2
                with open(toml_path, "rb") as f:
                    data = _tomli2.load(f)
        opt = data["project"]["optional-dependencies"]
        assert "redis" in opt
        assert "docker" in opt
        assert "full" in opt
