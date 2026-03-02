"""Tests for pipeline_config.py - multi-provider pipeline configuration."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pipeline_config import (  # noqa: E402
    PROVIDER_ALIASES,
    VALID_PROVIDERS,
    _normalize_provider,
    apply_config_to_stages,
    load_pipeline_config,
    log_role_routing,
    resolve_role_config,
    validate_pipeline_config,
    validate_provider_availability,
    get_effective_pipeline_config,
    _apply_env_overrides,
)


class TestNormalizeProvider(unittest.TestCase):
    def test_canonical_names(self):
        self.assertEqual(_normalize_provider("anthropic"), "anthropic")
        self.assertEqual(_normalize_provider("openai"), "openai")

    def test_aliases(self):
        self.assertEqual(_normalize_provider("opus"), "anthropic")
        self.assertEqual(_normalize_provider("claude"), "anthropic")
        self.assertEqual(_normalize_provider("codex"), "openai")
        self.assertEqual(_normalize_provider("gpt"), "openai")

    def test_case_insensitive(self):
        self.assertEqual(_normalize_provider("OPUS"), "anthropic")
        self.assertEqual(_normalize_provider("Codex"), "openai")
        self.assertEqual(_normalize_provider("ANTHROPIC"), "anthropic")

    def test_empty(self):
        self.assertEqual(_normalize_provider(""), "")
        self.assertEqual(_normalize_provider(None), "")

    def test_unknown(self):
        self.assertEqual(_normalize_provider("unknown"), "unknown")


class TestLoadPipelineConfigYAML(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_load_yaml_config(self):
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "pipeline_config.yaml"
        config_path.write_text(
            "pipeline:\n"
            "  default:\n"
            "    provider: anthropic\n"
            "    model: claude-sonnet-4-6\n"
            "  roles:\n"
            "    pm:\n"
            "      provider: opus\n"
            "      model: claude-opus-4-6\n"
            "    dev:\n"
            "      provider: openai\n"
            "      model: gpt-4o\n",
            encoding="utf-8",
        )
        config = load_pipeline_config(str(config_path))
        self.assertIn("default", config)
        self.assertEqual(config["default"]["provider"], "anthropic")
        self.assertEqual(config["default"]["model"], "claude-sonnet-4-6")
        self.assertIn("roles", config)
        # "opus" should be normalized to "anthropic"
        self.assertEqual(config["roles"]["pm"]["provider"], "anthropic")
        self.assertEqual(config["roles"]["pm"]["model"], "claude-opus-4-6")
        self.assertEqual(config["roles"]["dev"]["provider"], "openai")

    def test_load_json_config(self):
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "pipeline_config.json"
        config_path.write_text(json.dumps({
            "pipeline": {
                "default": {"provider": "openai", "model": "gpt-4.1"},
                "roles": {
                    "qa": {"provider": "codex", "model": "gpt-4.1"},
                },
            }
        }), encoding="utf-8")
        config = load_pipeline_config(str(config_path))
        self.assertEqual(config["default"]["provider"], "openai")
        # "codex" alias → "openai"
        self.assertEqual(config["roles"]["qa"]["provider"], "openai")

    def test_load_no_file(self):
        config = load_pipeline_config("/nonexistent/path.yaml")
        self.assertEqual(config, {})

    def test_load_auto_discover(self):
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "pipeline_config.yaml").write_text(
            "pipeline:\n  default:\n    provider: anthropic\n    model: claude-opus-4-6\n",
            encoding="utf-8",
        )
        config = load_pipeline_config()
        self.assertIn("default", config)
        self.assertEqual(config["default"]["provider"], "anthropic")

    def test_invalid_yaml_raises(self):
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        bad_path = config_dir / "pipeline_config.yaml"
        bad_path.write_text("{{{{invalid yaml", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_pipeline_config(str(bad_path))
        self.assertIn("加载失败", str(cm.exception))

    def test_provider_aliases_in_yaml(self):
        """Test that provider aliases (opus/codex/claude/gpt) are resolved."""
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "pipeline_config.yaml"
        config_path.write_text(
            "pipeline:\n"
            "  default:\n"
            "    provider: codex\n"
            "    model: gpt-4.1\n"
            "  roles:\n"
            "    pm:\n"
            "      provider: opus\n"
            "      model: claude-opus-4-6\n"
            "    test:\n"
            "      provider: gpt\n"
            "      model: gpt-4o\n",
            encoding="utf-8",
        )
        config = load_pipeline_config(str(config_path))
        self.assertEqual(config["default"]["provider"], "openai")
        self.assertEqual(config["roles"]["pm"]["provider"], "anthropic")
        self.assertEqual(config["roles"]["test"]["provider"], "openai")


class TestEnvOverrides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        for key in list(os.environ.keys()):
            if key.startswith("PIPELINE_"):
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_default_override(self):
        os.environ["PIPELINE_DEFAULT_PROVIDER"] = "openai"
        os.environ["PIPELINE_DEFAULT_MODEL"] = "gpt-4o"
        config = _apply_env_overrides({})
        self.assertEqual(config["default"]["provider"], "openai")
        self.assertEqual(config["default"]["model"], "gpt-4o")

    def test_role_override(self):
        os.environ["PIPELINE_ROLE_PM_PROVIDER"] = "anthropic"
        os.environ["PIPELINE_ROLE_PM_MODEL"] = "claude-opus-4-6"
        config = _apply_env_overrides({})
        self.assertEqual(config["roles"]["pm"]["provider"], "anthropic")
        self.assertEqual(config["roles"]["pm"]["model"], "claude-opus-4-6")

    def test_env_overrides_yaml(self):
        """Env vars should override YAML config."""
        base_config = {
            "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "roles": {
                "pm": {"provider": "anthropic", "model": "claude-opus-4-6"},
            },
        }
        os.environ["PIPELINE_ROLE_PM_MODEL"] = "gpt-4o"
        os.environ["PIPELINE_ROLE_PM_PROVIDER"] = "openai"
        result = _apply_env_overrides(base_config)
        self.assertEqual(result["roles"]["pm"]["model"], "gpt-4o")
        self.assertEqual(result["roles"]["pm"]["provider"], "openai")
        # Default unchanged
        self.assertEqual(result["default"]["provider"], "anthropic")

    def test_alias_in_env(self):
        os.environ["PIPELINE_DEFAULT_PROVIDER"] = "opus"
        config = _apply_env_overrides({})
        self.assertEqual(config["default"]["provider"], "anthropic")


class TestResolveRoleConfig(unittest.TestCase):
    def test_role_hit(self):
        """Role with explicit config should use its own provider/model."""
        config = {
            "default": {"provider": "openai", "model": "gpt-4.1"},
            "roles": {
                "pm": {"provider": "anthropic", "model": "claude-opus-4-6"},
            },
        }
        result = resolve_role_config("pm", config)
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["model"], "claude-opus-4-6")

    def test_role_fallback_to_default(self):
        """Role without config should fall back to default."""
        config = {
            "default": {"provider": "openai", "model": "gpt-4.1"},
            "roles": {
                "pm": {"provider": "anthropic", "model": "claude-opus-4-6"},
            },
        }
        result = resolve_role_config("qa", config)
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "gpt-4.1")

    def test_no_config(self):
        """Empty config should return empty."""
        result = resolve_role_config("pm", {})
        self.assertEqual(result["provider"], "")
        self.assertEqual(result["model"], "")

    def test_partial_role_config_inherits_default(self):
        """Role with only model should inherit provider from default."""
        config = {
            "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "roles": {
                "dev": {"provider": "", "model": ""},
            },
        }
        result = resolve_role_config("dev", config)
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["model"], "claude-sonnet-4-6")


class TestValidatePipelineConfig(unittest.TestCase):
    def test_valid_config(self):
        config = {
            "default": {"provider": "anthropic", "model": "claude-opus-4-6"},
            "roles": {
                "pm": {"provider": "anthropic", "model": "claude-opus-4-6"},
                "dev": {"provider": "openai", "model": "gpt-4o"},
            },
        }
        errors = validate_pipeline_config(config)
        self.assertEqual(errors, [])

    def test_invalid_provider(self):
        config = {
            "default": {"provider": "invalid_provider", "model": "some-model"},
        }
        errors = validate_pipeline_config(config)
        self.assertTrue(any("无效" in e for e in errors))

    def test_provider_without_model(self):
        config = {
            "roles": {
                "pm": {"provider": "anthropic", "model": ""},
            },
        }
        errors = validate_pipeline_config(config)
        self.assertTrue(any("未指定 model" in e for e in errors))

    def test_unknown_role(self):
        config = {
            "roles": {
                "unknown_role": {"provider": "anthropic", "model": "claude-opus-4-6"},
            },
        }
        errors = validate_pipeline_config(config)
        self.assertTrue(any("未知角色" in e for e in errors))

    def test_empty_config_valid(self):
        errors = validate_pipeline_config({})
        self.assertEqual(errors, [])

    def test_model_without_provider_inferred(self):
        """Model that can be inferred should not produce error."""
        config = {
            "roles": {
                "pm": {"provider": "", "model": "claude-opus-4-6"},
            },
        }
        errors = validate_pipeline_config(config)
        self.assertEqual(errors, [])

    def test_model_without_provider_unknown(self):
        """Unknown model without provider should produce error."""
        config = {
            "roles": {
                "pm": {"provider": "", "model": "unknown-model-xyz"},
            },
        }
        errors = validate_pipeline_config(config)
        self.assertTrue(any("无法推断" in e for e in errors))


class TestValidateProviderAvailability(unittest.TestCase):
    def setUp(self):
        self._orig_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
        self._orig_openai = os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self):
        if self._orig_anthropic is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._orig_anthropic
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        if self._orig_openai is not None:
            os.environ["OPENAI_API_KEY"] = self._orig_openai
        else:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_no_keys_warns(self):
        config = {
            "default": {"provider": "anthropic", "model": "claude-opus-4-6"},
            "roles": {"pm": {"provider": "openai", "model": "gpt-4o"}},
        }
        warnings = validate_provider_availability(config)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("anthropic" in w.lower() for w in warnings))
        self.assertTrue(any("openai" in w.lower() for w in warnings))

    def test_with_keys_no_warn(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        os.environ["OPENAI_API_KEY"] = "test-key"
        config = {
            "default": {"provider": "anthropic", "model": "claude-opus-4-6"},
            "roles": {"pm": {"provider": "openai", "model": "gpt-4o"}},
        }
        warnings = validate_provider_availability(config)
        self.assertEqual(warnings, [])

    def test_empty_config_no_warn(self):
        warnings = validate_provider_availability({})
        self.assertEqual(warnings, [])


class TestGetEffectivePipelineConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        for key in list(os.environ.keys()):
            if key.startswith("PIPELINE_"):
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_no_config_returns_empty(self):
        config = get_effective_pipeline_config()
        self.assertEqual(config, {})

    def test_valid_yaml_config(self):
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "pipeline_config.yaml").write_text(
            "pipeline:\n"
            "  default:\n"
            "    provider: anthropic\n"
            "    model: claude-opus-4-6\n",
            encoding="utf-8",
        )
        config = get_effective_pipeline_config()
        self.assertEqual(config["default"]["provider"], "anthropic")

    def test_invalid_config_raises(self):
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "pipeline_config.yaml").write_text(
            "pipeline:\n"
            "  default:\n"
            "    provider: invalid_xyz\n"
            "    model: some-model\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError) as cm:
            get_effective_pipeline_config()
        self.assertIn("校验失败", str(cm.exception))

    def test_env_only_config(self):
        os.environ["PIPELINE_DEFAULT_PROVIDER"] = "openai"
        os.environ["PIPELINE_DEFAULT_MODEL"] = "gpt-4o"
        config = get_effective_pipeline_config()
        self.assertEqual(config["default"]["provider"], "openai")
        self.assertEqual(config["default"]["model"], "gpt-4o")


class TestApplyConfigToStages(unittest.TestCase):
    def test_apply_role_config(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "", "provider": ""},
            {"name": "dev", "backend": "claude", "model": "", "provider": ""},
            {"name": "test", "backend": "claude", "model": "", "provider": ""},
            {"name": "qa", "backend": "claude", "model": "", "provider": ""},
        ]
        config = {
            "default": {"provider": "openai", "model": "gpt-4.1"},
            "roles": {
                "pm": {"provider": "anthropic", "model": "claude-opus-4-6"},
                "dev": {"provider": "anthropic", "model": "claude-opus-4-6"},
            },
        }
        result = apply_config_to_stages(stages, config)
        self.assertEqual(result[0]["model"], "claude-opus-4-6")
        self.assertEqual(result[0]["provider"], "anthropic")
        self.assertEqual(result[1]["model"], "claude-opus-4-6")
        self.assertEqual(result[1]["provider"], "anthropic")
        # test/qa should use default
        self.assertEqual(result[2]["model"], "gpt-4.1")
        self.assertEqual(result[2]["provider"], "openai")
        self.assertEqual(result[3]["model"], "gpt-4.1")
        self.assertEqual(result[3]["provider"], "openai")

    def test_empty_config_no_change(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "", "provider": ""},
        ]
        result = apply_config_to_stages(stages, {})
        self.assertEqual(result[0]["model"], "")

    def test_only_default_config(self):
        stages = [
            {"name": "pm", "backend": "claude", "model": "", "provider": ""},
            {"name": "dev", "backend": "claude", "model": "", "provider": ""},
        ]
        config = {
            "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        }
        result = apply_config_to_stages(stages, config)
        self.assertEqual(result[0]["model"], "claude-sonnet-4-6")
        self.assertEqual(result[1]["model"], "claude-sonnet-4-6")


class TestLogRoleRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_routing_with_mixed_config(self):
        config = {
            "default": {"provider": "openai", "model": "gpt-4.1"},
            "roles": {
                "pm": {"provider": "anthropic", "model": "claude-opus-4-6"},
            },
        }
        routing = log_role_routing([], config)
        self.assertEqual(len(routing), 4)
        pm_route = next(r for r in routing if r["role"] == "pm")
        self.assertEqual(pm_route["provider"], "anthropic")
        self.assertEqual(pm_route["model"], "claude-opus-4-6")
        self.assertEqual(pm_route["source"], "config_role")

        # qa should fall back to default
        qa_route = next(r for r in routing if r["role"] == "qa")
        self.assertEqual(qa_route["provider"], "openai")
        self.assertEqual(qa_route["model"], "gpt-4.1")
        self.assertEqual(qa_route["source"], "config_default")

    def test_routing_no_config_uses_global(self):
        config = {}
        routing = log_role_routing([], config)
        self.assertEqual(len(routing), 4)
        for r in routing:
            self.assertEqual(r["source"], "global")


class TestLoadPipelineProviderConfigIntegration(unittest.TestCase):
    """Integration test: _load_pipeline_provider_config in backends.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        for key in list(os.environ.keys()):
            if key.startswith("PIPELINE_"):
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_no_config_returns_empty(self):
        from backends import _load_pipeline_provider_config
        config = _load_pipeline_provider_config()
        self.assertEqual(config, {})

    def test_with_yaml_config(self):
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "pipeline_config.yaml").write_text(
            "pipeline:\n"
            "  default:\n"
            "    provider: anthropic\n"
            "    model: claude-opus-4-6\n"
            "  roles:\n"
            "    pm:\n"
            "      provider: anthropic\n"
            "      model: claude-opus-4-6\n",
            encoding="utf-8",
        )
        from backends import _load_pipeline_provider_config
        config = _load_pipeline_provider_config()
        self.assertIn("default", config)
        self.assertIn("roles", config)

    def test_invalid_config_returns_empty(self):
        """Invalid config should log error and return empty dict (no crash)."""
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "pipeline_config.yaml").write_text(
            "pipeline:\n"
            "  default:\n"
            "    provider: bad_provider\n"
            "    model: bad-model\n",
            encoding="utf-8",
        )
        from backends import _load_pipeline_provider_config
        config = _load_pipeline_provider_config()
        self.assertEqual(config, {})


class TestEndToEndConfigResolution(unittest.TestCase):
    """End-to-end test: YAML + env + fallback chain."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        for key in list(os.environ.keys()):
            if key.startswith("PIPELINE_"):
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_full_chain(self):
        """YAML config + env override for one role."""
        config_dir = Path(self.tmp.name) / "codex-tasks" / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "pipeline_config.yaml").write_text(
            "pipeline:\n"
            "  default:\n"
            "    provider: openai\n"
            "    model: gpt-4.1\n"
            "  roles:\n"
            "    pm:\n"
            "      provider: anthropic\n"
            "      model: claude-opus-4-6\n",
            encoding="utf-8",
        )
        # Override dev via env
        os.environ["PIPELINE_ROLE_DEV_PROVIDER"] = "anthropic"
        os.environ["PIPELINE_ROLE_DEV_MODEL"] = "claude-sonnet-4-6"

        config = get_effective_pipeline_config()

        # PM: from YAML
        pm = resolve_role_config("pm", config)
        self.assertEqual(pm["provider"], "anthropic")
        self.assertEqual(pm["model"], "claude-opus-4-6")

        # Dev: from env override
        dev = resolve_role_config("dev", config)
        self.assertEqual(dev["provider"], "anthropic")
        self.assertEqual(dev["model"], "claude-sonnet-4-6")

        # Test: fallback to default
        test = resolve_role_config("test", config)
        self.assertEqual(test["provider"], "openai")
        self.assertEqual(test["model"], "gpt-4.1")

        # QA: fallback to default
        qa = resolve_role_config("qa", config)
        self.assertEqual(qa["provider"], "openai")
        self.assertEqual(qa["model"], "gpt-4.1")


if __name__ == "__main__":
    unittest.main()
