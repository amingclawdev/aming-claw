"""Configuration loader for state-only reconcile semantic enrichment."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "reconcile" / "semantic_enrichment.yaml"
PROJECT_OVERRIDE_PATH = Path(".aming-claw") / "reconcile" / "semantic_enrichment.yaml"

_REQUIRED_FIELDS = {"version", "analyzer", "prompt_template"}
_FORBIDDEN_ALLOWED = {
    "modify_code",
    "modify_docs",
    "modify_tests",
    "mutate_graph_topology",
    "run_command",
    "execute_script",
    "create_chain_task",
    "finalize_snapshot",
}


class SemanticConfigError(Exception):
    """Base exception for semantic analyzer config failures."""


class SemanticConfigValidationError(SemanticConfigError):
    """Raised when semantic analyzer config is invalid."""


@dataclass
class SemanticInputPolicy:
    include_source_excerpt: bool = True
    max_excerpt_chars: int = 12000
    include_symbol_refs: bool = True
    include_doc_refs: bool = True
    include_config_refs: bool = True
    include_review_feedback: bool = True
    include_file_hashes: bool = True


@dataclass
class SemanticAnalyzerConfig:
    version: str
    analyzer: str
    provider: str = "injected"
    model: str = ""
    use_ai_default: bool = False
    temperature: float = 0.0
    max_tokens: int = 4000
    permissions_can: list[str] = field(default_factory=list)
    permissions_cannot: list[str] = field(default_factory=list)
    input_policy: SemanticInputPolicy = field(default_factory=SemanticInputPolicy)
    output_schema: dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""
    source_path: str = ""
    override_path: str = ""

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        source_path: str = "",
        override_path: str = "",
    ) -> "SemanticAnalyzerConfig":
        missing = _REQUIRED_FIELDS - set(data)
        if missing:
            raise SemanticConfigValidationError(
                f"Missing required semantic config fields: {sorted(missing)}"
            )
        analyzer = str(data.get("analyzer") or "").strip()
        if not analyzer:
            raise SemanticConfigValidationError("'analyzer' cannot be empty")
        prompt_template = str(data.get("prompt_template") or "").strip()
        if not prompt_template:
            raise SemanticConfigValidationError("'prompt_template' cannot be empty")
        permissions = data.get("permissions") or {}
        if not isinstance(permissions, dict):
            raise SemanticConfigValidationError("'permissions' must be a mapping")
        can = [str(item) for item in (permissions.get("can") or []) if str(item)]
        cannot = [str(item) for item in (permissions.get("cannot") or []) if str(item)]
        forbidden = sorted(set(can) & _FORBIDDEN_ALLOWED)
        if forbidden:
            raise SemanticConfigValidationError(
                "semantic analyzer cannot allow mutation permissions: "
                + ", ".join(forbidden)
            )
        input_policy_raw = data.get("input_policy") or {}
        if not isinstance(input_policy_raw, dict):
            raise SemanticConfigValidationError("'input_policy' must be a mapping")
        try:
            max_excerpt = int(input_policy_raw.get("max_excerpt_chars", 12000))
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("input_policy.max_excerpt_chars must be an integer") from exc
        if max_excerpt < 0:
            raise SemanticConfigValidationError("input_policy.max_excerpt_chars must be >= 0")
        input_policy = SemanticInputPolicy(
            include_source_excerpt=bool(input_policy_raw.get("include_source_excerpt", True)),
            max_excerpt_chars=max_excerpt,
            include_symbol_refs=bool(input_policy_raw.get("include_symbol_refs", True)),
            include_doc_refs=bool(input_policy_raw.get("include_doc_refs", True)),
            include_config_refs=bool(input_policy_raw.get("include_config_refs", True)),
            include_review_feedback=bool(input_policy_raw.get("include_review_feedback", True)),
            include_file_hashes=bool(input_policy_raw.get("include_file_hashes", True)),
        )
        try:
            max_tokens = int(data.get("max_tokens", 4000))
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("'max_tokens' must be an integer") from exc
        try:
            temperature = float(data.get("temperature", 0.0))
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("'temperature' must be numeric") from exc
        return cls(
            version=str(data.get("version") or ""),
            analyzer=analyzer,
            provider=str(data.get("provider") or "injected"),
            model=str(data.get("model") or ""),
            use_ai_default=bool(data.get("use_ai_default", False)),
            temperature=temperature,
            max_tokens=max_tokens,
            permissions_can=can,
            permissions_cannot=cannot,
            input_policy=input_policy,
            output_schema=data.get("output_schema") if isinstance(data.get("output_schema"), dict) else {},
            prompt_template=prompt_template,
            source_path=source_path,
            override_path=override_path,
        )

    def to_instruction_payload(self) -> dict[str, Any]:
        return {
            "mode": "state_only_semantic_enrichment",
            "analyzer": self.analyzer,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "mutate_project_files": False,
            "mutate_graph_topology": False,
            "return_semantic_fields_and_suggestions_only": True,
            "permissions": {
                "can": sorted(set(self.permissions_can)),
                "cannot": sorted(set(self.permissions_cannot)),
            },
            "input_policy": asdict(self.input_policy),
            "output_schema": self.output_schema,
            "prompt_template": self.prompt_template,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "analyzer": self.analyzer,
            "provider": self.provider,
            "model": self.model,
            "use_ai_default": self.use_ai_default,
            "source_path": self.source_path,
            "override_path": self.override_path,
            "input_policy": asdict(self.input_policy),
        }


def _default_config_dict() -> dict[str, Any]:
    return {
        "version": "1.0",
        "analyzer": "reconcile_semantic",
        "provider": "injected",
        "model": "",
        "use_ai_default": False,
        "temperature": 0,
        "max_tokens": 4000,
        "permissions": {
            "can": [
                "read_graph_snapshot",
                "read_governance_index",
                "read_feature_context",
                "read_review_feedback",
                "emit_semantic_index",
                "emit_review_suggestions",
            ],
            "cannot": sorted(_FORBIDDEN_ALLOWED),
        },
        "input_policy": asdict(SemanticInputPolicy()),
        "output_schema": {
            "required": [
                "feature_name",
                "semantic_summary",
                "intent",
                "domain_label",
                "doc_coverage_review",
                "test_coverage_review",
                "config_coverage_review",
                "dependency_patch_suggestions",
                "applied_feedback_ids",
            ]
        },
        "prompt_template": "You are the reconcile semantic analyzer. Return structured JSON only.",
    }


def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SemanticConfigValidationError(f"Invalid YAML in {path}: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise SemanticConfigValidationError(f"YAML file {path} must contain a mapping")
    return payload


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_semantic_enrichment_config(
    *,
    project_root: str | Path | None = None,
    config_path: str | Path | None = None,
) -> SemanticAnalyzerConfig:
    """Load default semantic analyzer config with optional project override."""
    env_path = os.getenv("RECONCILE_SEMANTIC_CONFIG", "").strip()
    base_path = Path(config_path or env_path or DEFAULT_CONFIG_PATH)
    source_payload = _read_yaml(base_path)
    source_path = str(base_path) if source_payload is not None else ""
    payload = source_payload if source_payload is not None else _default_config_dict()

    override_path = ""
    if project_root:
        candidate = Path(project_root).resolve() / PROJECT_OVERRIDE_PATH
        override_payload = _read_yaml(candidate)
        if override_payload is not None:
            payload = _deep_merge(payload, override_payload)
            override_path = str(candidate)
    return SemanticAnalyzerConfig.from_dict(
        payload,
        source_path=source_path,
        override_path=override_path,
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "PROJECT_OVERRIDE_PATH",
    "SemanticAnalyzerConfig",
    "SemanticConfigError",
    "SemanticConfigValidationError",
    "SemanticInputPolicy",
    "load_semantic_enrichment_config",
]
