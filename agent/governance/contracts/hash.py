"""Deterministic hashing helpers for governance contract definitions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


DERIVED_HASH_FIELDS = frozenset(
    {
        "_source_path",
        "definition_hash",
        "execution_state_hash",
        "instruction_bundle_hash",
        "runtime_guide_hash",
        "source",
        "source_path",
    }
)

LIFECYCLE_HASH_FIELDS = frozenset(
    {
        "deprecated_at",
        "deprecated_by",
        "deprecation_reason",
        "status",
    }
)


def canonical_json(value: Any) -> str:
    """Return compact, sorted JSON for stable hashing and source writes."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def strip_derived_hash_fields(value: Any) -> Any:
    """Remove load-time/hash/lifecycle fields that must not affect replay."""

    if isinstance(value, Mapping):
        return {
            str(key): strip_derived_hash_fields(child)
            for key, child in value.items()
            if str(key) not in DERIVED_HASH_FIELDS
            and str(key) not in LIFECYCLE_HASH_FIELDS
        }
    if isinstance(value, list):
        return [strip_derived_hash_fields(child) for child in value]
    return value


def stable_sha256(value: Any) -> str:
    """Hash a JSON-serializable value with a stable ``sha256:`` prefix."""

    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def definition_hash(definition: Mapping[str, Any]) -> str:
    """Hash the semantic contract definition, excluding lifecycle state."""

    return stable_sha256(strip_derived_hash_fields(definition))


def file_sha256(path: str | Path) -> str:
    """Return the sha256 digest for a file as ``sha256:<hex>``."""

    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return f"sha256:{digest}"
