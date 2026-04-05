"""AmingConfig — centralised configuration with env > yaml > defaults priority."""

import os
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ENV_PREFIX = "AMING_"

_YAML_FILENAME = ".aming-claw.yaml"


def _load_yaml_file(path: Optional[str] = None) -> dict:
    """Load YAML config file, returning empty dict on any failure."""
    target = Path(path) if path else Path.cwd() / _YAML_FILENAME
    if not target.exists():
        return {}
    try:
        import yaml  # PyYAML is a hard dependency
        with open(target, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Failed to load %s: %s", target, exc)
        return {}


@dataclass
class AmingConfig:
    """Project configuration.

    Load priority: environment variables > .aming-claw.yaml > defaults.
    Env vars use ``AMING_`` prefix (e.g. ``AMING_PROJECT_ID``).
    """

    project_id: str = ""
    workspace_path: str = ""
    governance_port: int = 40000
    notification_backend: str = "telegram"
    redis_url: str = "redis://localhost:6379/0"
    max_workers: int = 4
    db_path: str = ""

    @classmethod
    def load(cls, yaml_path: Optional[str] = None) -> "AmingConfig":
        """Create config with env > yaml > defaults priority."""
        yaml_data = _load_yaml_file(yaml_path)
        kwargs = {}
        for f in fields(cls):
            # 1. Try environment variable (AMING_PROJECT_ID, etc.)
            env_key = _ENV_PREFIX + f.name.upper()
            env_val = os.environ.get(env_key)
            if env_val is not None:
                kwargs[f.name] = _coerce(env_val, f.type)
                continue
            # 2. Try YAML
            if f.name in yaml_data:
                kwargs[f.name] = _coerce(yaml_data[f.name], f.type)
                continue
            # 3. Default (handled by dataclass)
        return cls(**kwargs)


def _coerce(value, type_hint):
    """Coerce a raw value to the field's type."""
    if type_hint is int or type_hint == "int":
        return int(value)
    return str(value) if value is not None else ""
