"""Phase Z v2 PR2 — 5-signal composite layer scorer.

Assigns each function a layer (L0..L6) based on composite scoring of
in-degree, fan-out, import depth, path hint, and entry-point signal.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, List, Optional

from agent.governance.reconcile_phases.phase_z_v2 import (
    CallGraph,
    FunctionMeta,
    ModuleInfo,
)

# ---------------------------------------------------------------------------
# R1: Configurable weights for the 5-signal composite scorer
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS: Dict[str, float] = {
    "in_degree": 0.40,
    "fan_out": 0.20,
    "import_depth": 0.15,
    "path_hint": 0.15,
    "entry_signal": 0.10,
}

# R2: Foundation candidate thresholds
FOUNDATION_TOP_PCT: float = 0.05
FOUNDATION_MIN_IN_DEG: int = 10


# ---------------------------------------------------------------------------
# R3: Entry-point signal detection
# ---------------------------------------------------------------------------
_ENTRY_DECORATORS = {"route", "cli", "get", "post", "put", "delete", "patch"}
_MCP_PATTERNS = {"mcp_tool", "mcp_resource", "mcp_prompt", "server.tool",
                 "server.resource", "server.prompt"}


def is_entrypoint(qname: str, info: FunctionMeta) -> bool:
    """Return True if *info* looks like an entry point."""
    # Decorator check
    for dec in info.decorators:
        dec_lower = dec.lower()
        for pat in _ENTRY_DECORATORS:
            if pat in dec_lower:
                return True
        for pat in _MCP_PATTERNS:
            if pat in dec_lower:
                return True
    # __main__ guard
    if "__main__" in qname or "__main__" in info.name:
        return True
    return False


# ---------------------------------------------------------------------------
# R4: Path-based layer hint
# ---------------------------------------------------------------------------
def path_to_layer_hint(file_path: str) -> float:
    """Return a bias score based on where the file lives."""
    normalized = file_path.replace("\\", "/")
    if normalized.startswith("scripts/") or "/scripts/" in normalized:
        return 0.7
    if (normalized.startswith("agent/governance/")
            or "/agent/governance/" in normalized):
        return 0.5
    if normalized.startswith("agent/") or "/agent/" in normalized:
        return 0.4
    return 0.3  # default for unknown paths


# ---------------------------------------------------------------------------
# R5: Calibration from call graph + module info
# ---------------------------------------------------------------------------
def compute_calibration(
    call_graph: CallGraph,
    modules: List[ModuleInfo],
) -> Dict[str, Any]:
    """Compute normalization thresholds from the call graph."""
    # In-degree counts
    in_deg: Counter[str] = Counter()
    max_fan_out = 0
    for caller, targets in call_graph.edges.items():
        fan_out = len(targets)
        if fan_out > max_fan_out:
            max_fan_out = fan_out
        for t in targets:
            in_deg[t] += 1

    all_in_degs = sorted(in_deg.values(), reverse=True) if in_deg else [0]
    idx = max(0, int(math.ceil(len(all_in_degs) * FOUNDATION_TOP_PCT)) - 1)
    top_5pct_in_deg = all_in_degs[idx] if all_in_degs else 0

    max_in_deg = all_in_degs[0] if all_in_degs else 0

    # Max import depth (approximate via dotted module name depth)
    max_import_depth = 0
    for mod in modules:
        depth = mod.module_name.count(".") + 1
        if depth > max_import_depth:
            max_import_depth = depth

    return {
        "top_5pct_in_deg": top_5pct_in_deg,
        "max_in_deg": max_in_deg,
        "max_fan_out": max_fan_out,
        "max_import_depth": max_import_depth,
        "in_degree_counts": dict(in_deg),
    }


# ---------------------------------------------------------------------------
# R6: Normalization utility
# ---------------------------------------------------------------------------
def normalize(value: float, max_value: float, inverse: bool = False) -> float:
    """Normalize *value* to [0, 1]. If *inverse*, high value → low score."""
    if max_value <= 0:
        return 1.0 if inverse else 0.0
    clamped = min(value, max_value)
    ratio = clamped / max_value
    return 1.0 - ratio if inverse else ratio


# ---------------------------------------------------------------------------
# R7: Score a single function and assign its layer
# ---------------------------------------------------------------------------
def score_function_layer(
    qname: str,
    info: FunctionMeta,
    calibration: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Score *qname* and return layer assignment dict."""
    w = weights or DEFAULT_WEIGHTS

    in_deg_counts: Dict[str, int] = calibration.get("in_degree_counts", {})
    in_deg = in_deg_counts.get(qname, 0)
    fan_out = len(info.calls)

    # Import depth from module name
    mod_name = info.module
    import_depth = mod_name.count(".") + 1 if mod_name else 1

    # Signals
    s_in_degree = normalize(in_deg, calibration.get("max_in_deg", 1))
    s_fan_out = normalize(fan_out, calibration.get("max_fan_out", 1))
    s_import_depth = normalize(
        import_depth, calibration.get("max_import_depth", 1), inverse=True
    )
    # Determine file path from module name
    file_path = mod_name.replace(".", "/") + ".py" if mod_name else ""
    s_path_hint = path_to_layer_hint(file_path)
    s_entry = 1.0 if is_entrypoint(qname, info) else 0.0

    signals = {
        "in_degree": s_in_degree,
        "fan_out": s_fan_out,
        "import_depth": s_import_depth,
        "path_hint": s_path_hint,
        "entry_signal": s_entry,
    }

    composite = sum(w[k] * signals[k] for k in w)

    # Foundation candidate: top 5% in-degree AND >= min threshold
    is_foundation = (
        in_deg >= calibration.get("top_5pct_in_deg", 0)
        and in_deg >= FOUNDATION_MIN_IN_DEG
    )

    # Layer assignment
    if is_foundation and fan_out <= 3:
        layer = "L0"
        candidate = "foundation"
    elif s_entry >= 1.0 and fan_out >= 5:
        layer = "L6"
        candidate = "entry_point"
    else:
        # Composite bucket: map [0, 1] → L1..L5
        bucket = min(4, int(composite * 5))
        layer = f"L{bucket + 1}"
        candidate = "composite"

    return {
        "layer": layer,
        "score": round(composite, 4),
        "candidate": candidate,
        "signals": signals,
    }
