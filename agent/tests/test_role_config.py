"""Tests for YAML role config loader and migration.

Covers: YAML load, override merge, validation reject, backward compat,
prompt match, turn caps match, task_role_map match, verify_limits match.
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest
import yaml

# Ensure agent package is importable
_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance.role_config import (
    RoleConfig,
    RolePermissions,
    ValidationError,
    load_role_config,
    load_all_role_configs,
    validate_all_configs,
    reset_cache,
    _deep_merge,
    KNOWN_ROLES,
)


# Path to the default config directory
_CONFIG_DIR = _repo_root / "config" / "roles"


@pytest.fixture(autouse=True)
def _reset():
    """Reset config cache before each test."""
    reset_cache()
    yield
    reset_cache()


# --- Test 1: YAML load ---

class TestYAMLLoad:
    """Test that YAML files load correctly."""

    def test_load_pm_config(self):
        """AC1: pm.yaml loads with required keys."""
        config = load_role_config("pm", config_base=_CONFIG_DIR)
        assert config is not None
        assert config.role == "pm"
        assert config.version == "1.0"
        assert config.max_turns == 60
        assert isinstance(config.tools, list)
        assert isinstance(config.permissions, RolePermissions)
        assert config.prompt_template  # non-empty

    def test_load_all_six_roles(self):
        """AC2: All 6 YAML files load (tester archived)."""
        configs = load_all_role_configs(config_base=_CONFIG_DIR)
        assert len(configs) == 6
        for role in KNOWN_ROLES:
            assert role in configs, f"Missing config for role: {role}"

    def test_each_yaml_has_version(self):
        """AC11: Each YAML file has a version field."""
        configs = load_all_role_configs(config_base=_CONFIG_DIR)
        for role, config in configs.items():
            assert config.version, f"Role {role} has empty version"


# --- Test 2: Override merge ---

class TestOverrideMerge:
    """Test project-specific override merging."""

    def test_project_override_merges(self):
        """AC8: Project-specific override merges with default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            # Copy default configs
            default_dir = tmp_path / "default"
            shutil.copytree(_CONFIG_DIR / "default", default_dir)

            # Create project override
            project_dir = tmp_path / "test-project"
            project_dir.mkdir()
            override = {"max_turns": 99, "permissions": {"can": ["generate_prd", "reply_only"]}}
            with open(project_dir / "pm.yaml", "w") as f:
                yaml.dump(override, f)

            config = load_role_config("pm", project_id="test-project", config_base=tmp_path)
            assert config is not None
            assert config.max_turns == 99
            # Should still have version from default
            assert config.version == "1.0"

    def test_deep_merge_preserves_base(self):
        """Deep merge preserves base keys not in override."""
        base = {"a": 1, "b": {"x": 10, "y": 20}}
        override = {"b": {"y": 99}}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": {"x": 10, "y": 99}}


# --- Test 3: Validation reject ---

class TestValidationReject:
    """Test that invalid YAML raises ValidationError."""

    def test_missing_required_field_raises(self):
        """AC7: Missing required field raises ValidationError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            default_dir = tmp_path / "default"
            default_dir.mkdir()
            # Write YAML missing 'version' and 'permissions'
            invalid = {"role": "pm", "max_turns": 10, "prompt_template": "test"}
            with open(default_dir / "pm.yaml", "w") as f:
                yaml.dump(invalid, f)

            with pytest.raises(ValidationError, match="Missing required fields"):
                load_role_config("pm", config_base=tmp_path)

    def test_invalid_max_turns_raises(self):
        """Invalid max_turns type raises ValidationError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            default_dir = tmp_path / "default"
            default_dir.mkdir()
            invalid = {
                "version": "1.0", "role": "pm", "max_turns": "not_a_number",
                "permissions": {"can": []}, "prompt_template": "test"
            }
            with open(default_dir / "pm.yaml", "w") as f:
                yaml.dump(invalid, f)

            with pytest.raises(ValidationError, match="max_turns"):
                load_role_config("pm", config_base=tmp_path)


# --- Test 4: Backward compatibility ---

class TestBackwardCompat:
    """Test that system works identically without YAML files."""

    def test_fallback_to_defaults_when_no_yaml(self):
        """AC5: With no YAML files, role_permissions produces identical dicts."""
        from agent.role_permissions import (
            _DEFAULT_ROLE_PERMISSIONS,
            _DEFAULT_ROLE_PROMPTS,
            _DEFAULT_ROLE_VERIFY_LIMITS,
        )
        # Load with a nonexistent config dir
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "nonexistent"
            config = load_role_config("pm", config_base=tmp_path)
            assert config is None  # No YAML found

        # Verify the default dicts are populated
        assert "pm" in _DEFAULT_ROLE_PERMISSIONS
        assert "coordinator" in _DEFAULT_ROLE_PERMISSIONS
        assert "dev" in _DEFAULT_ROLE_PERMISSIONS
        assert "pm" in _DEFAULT_ROLE_PROMPTS
        assert "tester" in _DEFAULT_ROLE_VERIFY_LIMITS  # Python defaults still have tester for backward compat

    def test_missing_yaml_returns_none(self):
        """load_role_config returns None for missing YAML (fallback path)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            default_dir = tmp_path / "default"
            default_dir.mkdir()
            # No files in default dir
            config = load_role_config("pm", config_base=tmp_path)
            assert config is None


# --- Test 5: Prompt text match ---

class TestPromptMatch:
    """Test that YAML prompt_template matches Python defaults."""

    def test_prompts_match_defaults(self):
        """AC6 (prompts): YAML prompt text matches Python hardcoded values."""
        from agent.role_permissions import _DEFAULT_ROLE_PROMPTS
        configs = load_all_role_configs(config_base=_CONFIG_DIR)

        for role in ("pm", "coordinator", "dev", "qa", "gatekeeper"):
            yaml_prompt = configs[role].prompt_template
            py_prompt = _DEFAULT_ROLE_PROMPTS[role]
            assert yaml_prompt == py_prompt, (
                f"Prompt mismatch for role '{role}':\n"
                f"YAML: {yaml_prompt[:100]}...\n"
                f"Python: {py_prompt[:100]}..."
            )


# --- Test 6: Turn caps match ---

class TestTurnCapsMatch:
    """Test that YAML max_turns matches _CLAUDE_ROLE_TURN_CAPS."""

    def test_turn_caps_match_defaults(self):
        """AC6 (turn caps): YAML max_turns matches Python defaults."""
        from agent.ai_lifecycle import _DEFAULT_CLAUDE_ROLE_TURN_CAPS
        configs = load_all_role_configs(config_base=_CONFIG_DIR)

        for role, expected_cap in _DEFAULT_CLAUDE_ROLE_TURN_CAPS.items():
            assert role in configs, f"Role {role} not in YAML configs"
            assert str(configs[role].max_turns) == expected_cap, (
                f"Turn cap mismatch for '{role}': "
                f"YAML={configs[role].max_turns}, Python={expected_cap}"
            )


# --- Test 7: Task role map match ---

class TestTaskRoleMapMatch:
    """Test that YAML-derived task_role_map matches defaults."""

    def test_task_role_map_match(self):
        """AC6 (task_role_map): YAML-derived map matches Python defaults."""
        from agent.executor_worker import _DEFAULT_TASK_ROLE_MAP, TASK_ROLE_MAP

        # The derived map should have at least all default entries
        for task_type, role in _DEFAULT_TASK_ROLE_MAP.items():
            assert task_type in TASK_ROLE_MAP, f"Missing task type: {task_type}"
            assert TASK_ROLE_MAP[task_type] == role, (
                f"Role mismatch for task type '{task_type}': "
                f"YAML={TASK_ROLE_MAP[task_type]}, default={role}"
            )


# --- Test 8: Verify limits match ---

class TestVerifyLimitsMatch:
    """Test that YAML verify_limits matches ROLE_VERIFY_LIMITS."""

    def test_verify_limits_match_defaults(self):
        """AC6 (verify_limits): YAML verify_limits matches Python defaults."""
        from agent.role_permissions import _DEFAULT_ROLE_VERIFY_LIMITS
        configs = load_all_role_configs(config_base=_CONFIG_DIR)

        for role, expected_limits in _DEFAULT_ROLE_VERIFY_LIMITS.items():
            if role == "tester":
                continue  # tester archived — no YAML config
            assert role in configs, f"Role {role} not in YAML configs"
            yaml_limits = configs[role].verify_limits
            assert yaml_limits == expected_limits, (
                f"Verify limits mismatch for '{role}': "
                f"YAML={yaml_limits}, Python={expected_limits}"
            )


# --- Test 9: Permissions match ---

class TestPermissionsMatch:
    """Test that YAML permissions match Python defaults."""

    def test_permissions_match_defaults(self):
        """AC6 (permissions): YAML allowed/denied sets match Python defaults."""
        from agent.role_permissions import _DEFAULT_ROLE_PERMISSIONS
        configs = load_all_role_configs(config_base=_CONFIG_DIR)

        for role, expected in _DEFAULT_ROLE_PERMISSIONS.items():
            if role == "tester":
                continue  # tester archived — no YAML config
            assert role in configs, f"Role {role} not in YAML configs"
            yaml_allowed = configs[role].permissions.allowed
            yaml_denied = configs[role].permissions.denied
            assert yaml_allowed == expected["allowed"], (
                f"Allowed mismatch for '{role}': "
                f"YAML={sorted(yaml_allowed)}, Python={sorted(expected['allowed'])}"
            )
            assert yaml_denied == expected["denied"], (
                f"Denied mismatch for '{role}': "
                f"YAML={sorted(yaml_denied)}, Python={sorted(expected['denied'])}"
            )


# --- Test 10: Observer config ---

class TestObserverConfig:
    """Test observer role config."""

    def test_observer_empty_allowed(self):
        """AC10: observer.yaml has empty allowed set and full denied set."""
        config = load_role_config("observer", config_base=_CONFIG_DIR)
        assert config is not None
        assert config.role == "observer"
        assert len(config.permissions.allowed) == 0
        assert len(config.permissions.denied) > 0


# --- Test 11: Startup validation ---

class TestStartupValidation:
    """Test startup validation function."""

    def test_validate_all_configs_passes(self):
        """Startup validation succeeds with valid configs."""
        result = validate_all_configs(config_base=_CONFIG_DIR)
        assert result is True

    def test_validate_rejects_invalid(self):
        """Startup validation raises on invalid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            default_dir = tmp_path / "default"
            default_dir.mkdir()
            # Write one invalid file
            invalid = {"role": "pm"}  # missing version, max_turns, etc.
            with open(default_dir / "pm.yaml", "w") as f:
                yaml.dump(invalid, f)

            with pytest.raises(ValidationError):
                validate_all_configs(config_base=tmp_path)


# --- Test 12: RoleConfig.from_dict ---

class TestRoleConfigFromDict:
    """Test dataclass construction."""

    def test_from_dict_valid(self):
        """RoleConfig.from_dict creates valid config."""
        data = {
            "version": "1.0",
            "role": "test_role",
            "max_turns": 10,
            "tools": ["Read"],
            "permissions": {"can": ["read_file"], "cannot": ["modify_code"]},
            "prompt_template": "Test prompt",
        }
        config = RoleConfig.from_dict(data)
        assert config.role == "test_role"
        assert config.max_turns == 10
        assert "read_file" in config.permissions.allowed
        assert "modify_code" in config.permissions.denied
