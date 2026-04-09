"""Role Config — YAML-based role configuration loader.

Loads role configs from config/roles/default/{role}.yaml with optional
project-specific overrides from config/roles/{project_id}/{role}.yaml.

Startup-only loading (no hot reload). Validates schema on load.
Falls back to Python defaults if YAML files not found.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

log = logging.getLogger(__name__)

# Project root: two levels up from agent/governance/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Configurable config base directory
_CONFIG_BASE = Path(os.getenv("ROLE_CONFIG_DIR", str(_PROJECT_ROOT / "config" / "roles")))

# All known roles
KNOWN_ROLES = ("coordinator", "pm", "dev", "qa", "gatekeeper", "observer")

# Required fields in every YAML config
_REQUIRED_FIELDS = {"version", "role", "max_turns", "permissions", "prompt_template"}


class RoleConfigError(Exception):
    """Raised when a YAML role config is invalid."""
    pass


class ValidationError(RoleConfigError):
    """Raised when YAML schema validation fails."""
    pass


@dataclass
class RolePermissions:
    """Parsed permissions block."""
    allowed: Set[str] = field(default_factory=set)
    denied: Set[str] = field(default_factory=set)


@dataclass
class RoleConfig:
    """Validated role configuration loaded from YAML."""
    version: str
    role: str
    max_turns: int
    tools: List[str] = field(default_factory=list)
    permissions: RolePermissions = field(default_factory=RolePermissions)
    prompt_template: str = ""
    verify_limits: Set[str] = field(default_factory=set)
    task_type_alias: Optional[str] = None  # maps task type to this role

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoleConfig":
        """Create RoleConfig from a parsed YAML dict. Validates required fields."""
        missing = _REQUIRED_FIELDS - set(data.keys())
        if missing:
            raise ValidationError(
                f"Missing required fields in role config: {sorted(missing)}"
            )

        version = str(data["version"])
        if not version:
            raise ValidationError("'version' field cannot be empty")

        role = str(data["role"])
        try:
            max_turns = int(data["max_turns"])
        except (ValueError, TypeError):
            raise ValidationError(
                f"'max_turns' must be an integer, got: {data['max_turns']!r}"
            )

        tools = data.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]

        perms_data = data.get("permissions", {})
        if not isinstance(perms_data, dict):
            raise ValidationError("'permissions' must be a mapping with 'can'/'cannot' keys")
        permissions = RolePermissions(
            allowed=set(perms_data.get("can") or []),
            denied=set(perms_data.get("cannot") or []),
        )

        verify_limits = set(data.get("verify_limits") or [])
        task_type_alias = data.get("task_type_alias")

        return cls(
            version=version,
            role=role,
            max_turns=max_turns,
            tools=tools,
            permissions=permissions,
            prompt_template=str(data.get("prompt_template", "")),
            verify_limits=verify_limits,
            task_type_alias=task_type_alias,
        )


def _load_yaml_file(path: Path) -> Optional[Dict[str, Any]]:
    """Load and parse a single YAML file. Returns None if file not found."""
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValidationError(f"YAML file {path} does not contain a mapping at top level")
        return data
    except yaml.YAMLError as e:
        raise ValidationError(f"Invalid YAML in {path}: {e}")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge override into base (shallow for most keys, deep for dicts)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_role_config(
    role: str,
    project_id: Optional[str] = None,
    config_base: Optional[Path] = None,
) -> Optional[RoleConfig]:
    """Load a single role config from YAML.

    Loads from config/roles/default/{role}.yaml first, then merges
    project-specific overrides from config/roles/{project_id}/{role}.yaml.

    Returns None if default YAML not found (caller should use Python defaults).
    Raises ValidationError if YAML exists but is invalid.
    """
    base_dir = config_base or _CONFIG_BASE
    default_path = base_dir / "default" / f"{role}.yaml"

    default_data = _load_yaml_file(default_path)
    if default_data is None:
        return None

    # Apply project-specific overrides if available
    if project_id:
        override_path = base_dir / project_id / f"{role}.yaml"
        override_data = _load_yaml_file(override_path)
        if override_data is not None:
            default_data = _deep_merge(default_data, override_data)

    return RoleConfig.from_dict(default_data)


def load_all_role_configs(
    project_id: Optional[str] = None,
    config_base: Optional[Path] = None,
) -> Dict[str, RoleConfig]:
    """Load all known role configs from YAML.

    Returns dict of role_name -> RoleConfig for roles that have YAML files.
    Logs warnings for missing files, raises on invalid files.
    """
    configs = {}
    for role in KNOWN_ROLES:
        try:
            config = load_role_config(role, project_id=project_id, config_base=config_base)
            if config is not None:
                configs[role] = config
            else:
                log.warning("No YAML config found for role '%s', using Python defaults", role)
        except ValidationError as e:
            raise  # Invalid YAML is a hard error
    return configs


def validate_all_configs(
    project_id: Optional[str] = None,
    config_base: Optional[Path] = None,
) -> bool:
    """Startup validation: load all configs, log warnings for missing, raise on invalid.

    Returns True if all configs loaded successfully.
    """
    try:
        configs = load_all_role_configs(project_id=project_id, config_base=config_base)
        if len(configs) == len(KNOWN_ROLES):
            log.info("All %d role YAML configs loaded successfully", len(configs))
            return True
        else:
            loaded = set(configs.keys())
            missing = set(KNOWN_ROLES) - loaded
            log.warning(
                "Loaded %d/%d role configs from YAML. Missing: %s (using Python defaults)",
                len(configs), len(KNOWN_ROLES), sorted(missing),
            )
            return True  # Still valid — just using defaults for missing
    except ValidationError:
        raise  # Re-raise — invalid YAML is fatal


# --- Cache for loaded configs (startup-only, no hot reload) ---
_cached_configs: Optional[Dict[str, RoleConfig]] = None


def get_all_role_configs(
    project_id: Optional[str] = None,
    config_base: Optional[Path] = None,
    force_reload: bool = False,
) -> Dict[str, RoleConfig]:
    """Get cached role configs. Loads on first call."""
    global _cached_configs
    if _cached_configs is None or force_reload:
        try:
            _cached_configs = load_all_role_configs(
                project_id=project_id, config_base=config_base
            )
        except ValidationError:
            log.error("Failed to load role configs from YAML, using empty cache")
            _cached_configs = {}
    return _cached_configs


def reset_cache():
    """Reset the config cache. Mainly for testing."""
    global _cached_configs
    _cached_configs = None
