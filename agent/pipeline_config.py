"""
pipeline_config.py - Multi-provider pipeline configuration.

Supports per-role provider/model binding via:
  1. Static YAML/JSON config file (pipeline_config.yaml / pipeline_config.json)
  2. Environment variable override (PIPELINE_DEFAULT_PROVIDER, PIPELINE_ROLE_PM_MODEL, etc.)
  3. Runtime config fallback (agent_config.json role_pipeline_stages)

Priority (highest → lowest):
  Environment variable → YAML/JSON config file → Runtime config → Global defaults

Config file format (YAML):
  pipeline:
    default:
      provider: anthropic
      model: claude-sonnet-4-6
    roles:
      pm:
        provider: anthropic
        model: claude-opus-4-6
      dev:
        provider: anthropic
        model: claude-opus-4-6
      test:
        provider: openai
        model: gpt-4.1
      qa:
        provider: openai
        model: gpt-4.1
"""
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Provider aliases: allow shorthand names in config
PROVIDER_ALIASES: Dict[str, str] = {
    "opus": "anthropic",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "codex": "openai",
    "openai": "openai",
    "gpt": "openai",
}

# Valid canonical provider names
VALID_PROVIDERS = {"anthropic", "openai"}
VALID_BACKEND_MODES = {
    "anthropic_api",
    "claude_cli",
    "codex_cli",
    "docker_live_ai",
    "fixture",
    "openai_api",
}
VALID_AUTH_MODES = {"api_key_env", "cli_auth", "external_harness", "not_required"}


def _default_backend_mode(provider: str) -> str:
    return "codex_cli" if provider == "openai" else "claude_cli"


def _default_auth_mode(backend_mode: str) -> str:
    if backend_mode.endswith("_api"):
        return "api_key_env"
    if backend_mode.endswith("_cli"):
        return "cli_auth"
    if backend_mode == "docker_live_ai":
        return "external_harness"
    return "not_required"


def _routing_fields(value: Dict) -> Dict[str, str]:
    return {
        "provider": _normalize_provider(value.get("provider", "")),
        "model": (value.get("model") or "").strip(),
        "backend_mode": (value.get("backend_mode") or "").strip().lower(),
        "auth_mode": (value.get("auth_mode") or "").strip().lower(),
        "output_policy": (
            value.get("output_policy") or "hash_and_summary_only"
        ).strip(),
    }


def _normalize_provider(raw: str) -> str:
    """Normalize a provider name using aliases. Returns canonical name or raw."""
    key = (raw or "").strip().lower()
    return PROVIDER_ALIASES.get(key, key)


def _find_config_file() -> Optional[Path]:
    """Locate pipeline config file in standard locations."""
    from utils import tasks_root
    candidates = [
        tasks_root() / "state" / "pipeline_config.yaml",
        tasks_root() / "state" / "pipeline_config.yml",
        tasks_root() / "state" / "pipeline_config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def load_pipeline_config(path: Optional[str] = None) -> Dict:
    """Load pipeline config from YAML/JSON file.

    Returns dict with structure:
      {"default": {"provider": "...", "model": "..."},
       "roles": {"pm": {"provider": "...", "model": "..."}, ...}}

    Returns empty dict if no config file found.
    """
    if path:
        config_path = Path(path)
    else:
        config_path = _find_config_file()

    if not config_path or not config_path.exists():
        return {}

    suffix = config_path.suffix.lower()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        if suffix in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(raw_text) or {}
        else:
            import json
            data = json.loads(raw_text)
    except Exception as exc:
        logger.error("Failed to load pipeline config file %s: %s", config_path, exc)
        raise ValueError("Pipeline config file load failed ({}): {}".format(config_path, exc))

    # Extract pipeline section (support both top-level and nested)
    pipeline = data.get("pipeline", data)
    if not isinstance(pipeline, dict):
        raise ValueError("Invalid pipeline config format: 'pipeline' must be a dict")

    result = {}
    # Parse default section
    default = pipeline.get("default", {})
    if isinstance(default, dict):
        result["default"] = _routing_fields(default)

    # Parse roles section
    roles_raw = pipeline.get("roles", {})
    if isinstance(roles_raw, dict):
        roles = {}
        for role_name, role_cfg in roles_raw.items():
            if isinstance(role_cfg, dict):
                roles[role_name.lower().strip()] = _routing_fields(role_cfg)
        if roles:
            result["roles"] = roles

    return result


def _apply_env_overrides(config: Dict) -> Dict:
    """Apply environment variable overrides to the config.

    Supported env vars:
      PIPELINE_DEFAULT_PROVIDER  - Override default provider
      PIPELINE_DEFAULT_MODEL     - Override default model
      PIPELINE_ROLE_{ROLE}_PROVIDER - Override provider for specific role (e.g. PIPELINE_ROLE_PM_PROVIDER)
      PIPELINE_ROLE_{ROLE}_MODEL    - Override model for specific role (e.g. PIPELINE_ROLE_PM_MODEL)
    """
    ROLE_PIPELINE_ORDER = ["pm", "dev", "tester", "qa", "coordinator", "gatekeeper", "utility"]

    result = dict(config)

    # Default overrides
    env_default_provider = os.getenv("PIPELINE_DEFAULT_PROVIDER", "").strip()
    env_default_model = os.getenv("PIPELINE_DEFAULT_MODEL", "").strip()
    env_default_backend = os.getenv("PIPELINE_DEFAULT_BACKEND_MODE", "").strip()
    env_default_auth = os.getenv("PIPELINE_DEFAULT_AUTH_MODE", "").strip()
    env_default_output = os.getenv("PIPELINE_DEFAULT_OUTPUT_POLICY", "").strip()
    if any((env_default_provider, env_default_model, env_default_backend, env_default_auth, env_default_output)):
        default = dict(result.get("default", {}))
        if env_default_provider:
            default["provider"] = _normalize_provider(env_default_provider)
        if env_default_model:
            default["model"] = env_default_model
        if env_default_backend:
            default["backend_mode"] = env_default_backend.lower()
        if env_default_auth:
            default["auth_mode"] = env_default_auth.lower()
        if env_default_output:
            default["output_policy"] = env_default_output
        result["default"] = default

    # Per-role overrides
    roles = dict(result.get("roles", {}))
    for role in ROLE_PIPELINE_ORDER:
        env_provider = os.getenv("PIPELINE_ROLE_{}_PROVIDER".format(role.upper()), "").strip()
        env_model = os.getenv("PIPELINE_ROLE_{}_MODEL".format(role.upper()), "").strip()
        env_backend = os.getenv("PIPELINE_ROLE_{}_BACKEND_MODE".format(role.upper()), "").strip()
        env_auth = os.getenv("PIPELINE_ROLE_{}_AUTH_MODE".format(role.upper()), "").strip()
        env_output = os.getenv("PIPELINE_ROLE_{}_OUTPUT_POLICY".format(role.upper()), "").strip()
        if any((env_provider, env_model, env_backend, env_auth, env_output)):
            role_cfg = dict(roles.get(role, {}))
            if env_provider:
                role_cfg["provider"] = _normalize_provider(env_provider)
            if env_model:
                role_cfg["model"] = env_model
            if env_backend:
                role_cfg["backend_mode"] = env_backend.lower()
            if env_auth:
                role_cfg["auth_mode"] = env_auth.lower()
            if env_output:
                role_cfg["output_policy"] = env_output
            roles[role] = role_cfg
    if roles:
        result["roles"] = roles

    return result


def resolve_role_config(role_name: str, config: Dict) -> Dict:
    """Resolve the effective provider/model for a role.

    Applies fallback: role config → default config → empty.

    Returns {"provider": "...", "model": "..."}.
    """
    default = config.get("default", {})
    roles = config.get("roles", {})
    role_cfg = roles.get(role_name.lower(), {})

    provider = role_cfg.get("provider", "") or default.get("provider", "")
    model = role_cfg.get("model", "") or default.get("model", "")
    backend_mode = role_cfg.get("backend_mode", "") or default.get("backend_mode", "")
    auth_mode = role_cfg.get("auth_mode", "") or default.get("auth_mode", "")
    output_policy = (
        role_cfg.get("output_policy", "")
        or default.get("output_policy", "")
        or "hash_and_summary_only"
    )
    if not backend_mode and provider:
        backend_mode = _default_backend_mode(provider)
    if not auth_mode and backend_mode:
        auth_mode = _default_auth_mode(backend_mode)

    return {
        "provider": provider,
        "model": model,
        "backend_mode": backend_mode,
        "auth_mode": auth_mode,
        "output_policy": output_policy,
    }


def validate_pipeline_config(config: Dict) -> List[str]:
    """Validate pipeline config and return a list of error messages.

    Checks:
    1. Provider names are valid (after alias resolution)
    2. Model IDs are non-empty when provider is set
    3. Provider/model combinations are sensible

    Returns empty list if valid.
    """
    errors: List[str] = []

    # Validate default
    default = config.get("default", {})
    if default:
        provider = default.get("provider", "")
        model = default.get("model", "")
        backend_mode = default.get("backend_mode", "")
        auth_mode = default.get("auth_mode", "")
        if provider and provider not in VALID_PROVIDERS:
            errors.append("Default config provider '{}' is invalid, "
                          "valid values: {}".format(provider, ", ".join(sorted(VALID_PROVIDERS))))
        if provider and not model:
            errors.append("Default config specifies provider '{}' but no model".format(provider))
        if model and not provider:
            # Try to infer
            # Infer provider from model name
            inferred = ("anthropic" if "claude" in model.lower()
                        else "openai" if "gpt" in model.lower() else "")
            if not inferred:
                errors.append("Default config model '{}' cannot infer provider, "
                              "please specify provider explicitly".format(model))
        if backend_mode and backend_mode not in VALID_BACKEND_MODES:
            errors.append("Default config backend_mode '{}' is invalid".format(backend_mode))
        if auth_mode and auth_mode not in VALID_AUTH_MODES:
            errors.append("Default config auth_mode '{}' is invalid".format(auth_mode))

    # Validate roles
    ROLE_PIPELINE_ORDER = ["pm", "dev", "tester", "qa", "coordinator", "gatekeeper", "utility"]
    roles = config.get("roles", {})
    for role_name, role_cfg in roles.items():
        if role_name not in ROLE_PIPELINE_ORDER:
            errors.append("Unknown role '{}', valid roles: {}".format(
                role_name, ", ".join(ROLE_PIPELINE_ORDER)))
            continue
        provider = role_cfg.get("provider", "")
        model = role_cfg.get("model", "")
        backend_mode = role_cfg.get("backend_mode", "")
        auth_mode = role_cfg.get("auth_mode", "")
        if provider and provider not in VALID_PROVIDERS:
            errors.append("Role '{}' provider '{}' is invalid, "
                          "valid values: {}".format(role_name, provider,
                                                    ", ".join(sorted(VALID_PROVIDERS))))
        if provider and not model:
            errors.append("Role '{}' specifies provider '{}' but no model".format(
                role_name, provider))
        if model and not provider:
            # Infer provider from model name
            inferred = ("anthropic" if "claude" in model.lower()
                        else "openai" if "gpt" in model.lower() else "")
            if not inferred:
                errors.append("Role '{}' model '{}' cannot infer provider, "
                              "please specify provider explicitly".format(role_name, model))
        if backend_mode and backend_mode not in VALID_BACKEND_MODES:
            errors.append("Role '{}' backend_mode '{}' is invalid".format(role_name, backend_mode))
        if auth_mode and auth_mode not in VALID_AUTH_MODES:
            errors.append("Role '{}' auth_mode '{}' is invalid".format(role_name, auth_mode))

    return errors


def validate_provider_availability(config: Dict) -> List[str]:
    """Check that configured providers have required API keys available.

    Returns list of warning messages (non-fatal).
    """
    warnings: List[str] = []

    all_providers = set()
    entries = [config.get("default", {})]
    entries.extend(config.get("roles", {}).values())
    for entry in entries:
        provider = entry.get("provider")
        backend_mode = entry.get("backend_mode") or _default_backend_mode(provider)
        if provider and backend_mode.endswith("_api"):
            all_providers.add(provider)

    if "anthropic" in all_providers:
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            warnings.append("provider 'anthropic' is configured but ANTHROPIC_API_KEY env var is not set")

    if "openai" in all_providers:
        if not os.getenv("OPENAI_API_KEY", "").strip():
            warnings.append("provider 'openai' is configured but OPENAI_API_KEY env var is not set")

    return warnings


def get_effective_pipeline_config(config_path: Optional[str] = None) -> Dict:
    """Load, merge, and return the effective pipeline config.

    Combines: YAML file → env overrides → resolved config.
    Raises ValueError on critical config errors.
    """
    config = load_pipeline_config(config_path)
    config = _apply_env_overrides(config)

    # Validate structure
    errors = validate_pipeline_config(config)
    if errors:
        msg = "Pipeline config validation failed:\n" + "\n".join("  - " + e for e in errors)
        logger.error(msg)
        raise ValueError(msg)

    # Check provider availability (warn, don't fail)
    warnings = validate_provider_availability(config)
    for w in warnings:
        logger.warning("[PipelineConfig] %s", w)

    return config


def apply_config_to_stages(stages: List[Dict],
                           config: Dict) -> List[Dict]:
    """Apply pipeline config to role pipeline stages.

    For each stage, resolves the effective provider/model from config
    (with fallback to default), and merges into the stage dict.

    Returns the modified stages list.
    """
    if not config:
        return stages

    for stage in stages:
        name = stage.get("name", "")
        resolved = resolve_role_config(name, config)
        if resolved["model"]:
            stage["model"] = resolved["model"]
            stage["provider"] = resolved["provider"]
            stage["backend_mode"] = resolved["backend_mode"]
            stage["auth_mode"] = resolved["auth_mode"]
            stage["output_policy"] = resolved["output_policy"]

    return stages


def log_role_routing(stages: List[Dict], config: Dict) -> List[Dict]:
    """Log and return the routing decisions for each role.

    Returns a list of dicts: [{"role": "pm", "provider": "anthropic",
                                "model": "claude-opus-4-6", "source": "config_file"}]
    """
    ROLE_PIPELINE_ORDER = ["pm", "dev", "tester", "qa"], get_claude_model, get_model_provider
    routing: List[Dict] = []

    for role in ROLE_PIPELINE_ORDER:
        resolved = resolve_role_config(role, config)
        if resolved["model"]:
            source = "config_role" if config.get("roles", {}).get(role, {}).get("model") \
                else "config_default"
            provider = resolved["provider"]
            model = resolved["model"]
        else:
            source = "global"
            model = get_claude_model() or "(default)"
            provider = get_model_provider() or "(default)"

        entry = {
            "role": role,
            "provider": provider,
            "model": model,
            "source": source,
        }
        routing.append(entry)
        logger.info("[PipelineConfig] Role '%s' → provider=%s, model=%s (source: %s)",
                    role, provider, model, source)

    return routing
