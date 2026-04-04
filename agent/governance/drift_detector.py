"""Contract-drift detection for governance constants (D10).

Snapshots underscore-prefixed constants from auto_chain, executor_worker, and
ai_lifecycle at baseline time, then detects unauthorized mutations at gate time.
Warn-only: findings are attached as metadata but never block the gate.
"""

import copy
import importlib
import json
import logging
import types
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger(__name__)

# Modules to scan for underscore-prefixed constants
_TARGET_MODULES = {
    "auto_chain": "agent.governance.auto_chain",
    "executor_worker": "agent.executor_worker",
    "ai_lifecycle": "agent.ai_lifecycle",
}


def _serialize(value: Any) -> Any:
    """Serialize a value for stable comparison.

    - sets → sorted lists
    - dicts → recursively serialized
    - other primitives pass through
    """
    if isinstance(value, set):
        return sorted(str(v) for v in value)
    if isinstance(value, frozenset):
        return sorted(str(v) for v in value)
    if isinstance(value, dict):
        return {str(k): _serialize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, (int, float, bool, str, type(None))):
        return value
    # For callable/function references, store their qualified name
    if callable(value):
        return f"<callable:{getattr(value, '__qualname__', str(value))}>"
    return str(value)


def _is_constant(name: str, value: Any) -> bool:
    """Return True if *name* looks like a module-level constant to track."""
    if name.startswith("__"):
        return False
    # Must start with underscore OR be an ALL_CAPS name
    if not (name.startswith("_") or name.isupper() or (name[0].isupper() and "_" in name)):
        return False
    # Skip modules, classes, functions imported at top level
    if isinstance(value, (types.ModuleType, type)):
        return False
    # Skip plain functions (but keep dicts/lists that may contain function refs)
    if isinstance(value, types.FunctionType) and not isinstance(value, dict):
        return False
    return True


def capture_baseline() -> Dict[str, Any]:
    """Snapshot all trackable constants from target modules.

    Returns a dict keyed by 'module_short::CONSTANT_NAME' with serialized values.
    """
    baseline: Dict[str, Any] = {}
    for short_name, module_path in _TARGET_MODULES.items():
        try:
            mod = importlib.import_module(module_path)
        except ImportError:
            log.debug("drift_detector: could not import %s, skipping", module_path)
            continue
        for attr_name in dir(mod):
            value = getattr(mod, attr_name)
            if _is_constant(attr_name, value):
                key = f"{short_name}::{attr_name}"
                baseline[key] = _serialize(copy.deepcopy(value))
    return baseline


@dataclass
class DriftFinding:
    """A single detected constant mutation."""
    changed_key: str
    old_value: Any
    new_value: Any
    authorized: bool


def detect_drift(
    baseline: Dict[str, Any],
    authorized_keys: Optional[Set[str]] = None,
) -> List[DriftFinding]:
    """Re-import modules, compare current values to *baseline*, return findings.

    *authorized_keys*: set of 'module::CONSTANT' keys the PM explicitly
    authorized for change. All other changed keys are flagged unauthorized.
    """
    if authorized_keys is None:
        authorized_keys = set()

    current = capture_baseline()
    findings: List[DriftFinding] = []

    all_keys = set(baseline.keys()) | set(current.keys())
    for key in sorted(all_keys):
        old = baseline.get(key)
        new = current.get(key)
        if old != new:
            findings.append(DriftFinding(
                changed_key=key,
                old_value=old,
                new_value=new,
                authorized=key in authorized_keys,
            ))

    return findings


def findings_to_json(findings: List[DriftFinding]) -> str:
    """Serialize findings list to JSON string for metadata storage."""
    return json.dumps([asdict(f) for f in findings], default=str)
