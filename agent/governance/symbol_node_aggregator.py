"""Phase Z v2 PR2 — File-to-node aggregator.

Groups scored functions into governance-graph nodes.  Default: one node
per file.  Split when a file has >= SPLIT_MIN_FUNCTIONS functions AND
>= SPLIT_MIN_LAYER_TIERS distinct layers.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.governance.reconcile_phases.phase_z_v2 import (
    CallGraph,
    ModuleInfo,
)

# ---------------------------------------------------------------------------
# R8: Split thresholds
# ---------------------------------------------------------------------------
SPLIT_MIN_FUNCTIONS: int = 50
SPLIT_MIN_LAYER_TIERS: int = 3


# ---------------------------------------------------------------------------
# R13: Dominant layer (mode) and outliers
# ---------------------------------------------------------------------------
def determine_dominant_layer(
    functions: List[str],
    function_layers: Dict[str, Dict[str, Any]],
) -> Tuple[str, List[str]]:
    """Return (mode_layer, outlier_qnames)."""
    if not functions:
        return ("L3", [])
    layer_counts: Counter[str] = Counter()
    for qname in functions:
        info = function_layers.get(qname, {})
        layer_counts[info.get("layer", "L3")] += 1
    mode_layer = layer_counts.most_common(1)[0][0]
    outliers = [
        qname for qname in functions
        if function_layers.get(qname, {}).get("layer", "L3") != mode_layer
    ]
    return (mode_layer, outliers)


# ---------------------------------------------------------------------------
# R10: Region hint parser
# ---------------------------------------------------------------------------
_REGION_START = re.compile(r"^\s*#\s*region:\s*(.+?)\s*$", re.IGNORECASE)
_REGION_END = re.compile(r"^\s*#\s*endregion\b", re.IGNORECASE)


def parse_region_hints(file_path: str) -> Dict[str, List[str]]:
    """Parse ``# region: NAME`` / ``# endregion`` markers.

    Returns {region_name: [list of function qnames]} — but since we
    don't have AST context here, we return region_name → [] and let
    the caller populate function lists by line range.

    Actually returns {region_name: [line_start, line_end]} as strings
    for the caller to map functions into.
    """
    regions: Dict[str, List[str]] = {}
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except (OSError, IOError):
        return regions

    stack: List[Tuple[str, int]] = []
    for i, line in enumerate(lines, 1):
        m = _REGION_START.match(line)
        if m:
            stack.append((m.group(1).strip(), i))
            continue
        if _REGION_END.match(line) and stack:
            name, start = stack.pop()
            regions[name] = [str(start), str(i)]
    return regions


def _functions_in_region(
    region_lines: List[str],
    all_functions: List[str],
    func_line_map: Dict[str, int],
) -> List[str]:
    """Return functions whose start line falls within region bounds."""
    start = int(region_lines[0])
    end = int(region_lines[1])
    return [
        f for f in all_functions
        if start <= func_line_map.get(f, 0) <= end
    ]


# ---------------------------------------------------------------------------
# R12: Connected-component splitting within a single file
# ---------------------------------------------------------------------------
def split_by_connected_components(
    functions: List[str],
    call_graph: CallGraph,
) -> List[List[str]]:
    """Partition *functions* by call-graph connectivity (within file)."""
    func_set = set(functions)
    adj: Dict[str, Set[str]] = defaultdict(set)
    for fn in functions:
        for target in call_graph.edges.get(fn, []):
            if target in func_set:
                adj[fn].add(target)
                adj[target].add(fn)

    visited: Set[str] = set()
    components: List[List[str]] = []
    for fn in functions:
        if fn in visited:
            continue
        comp: List[str] = []
        stack = [fn]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            comp.append(node)
            for nb in adj.get(node, set()):
                if nb not in visited:
                    stack.append(nb)
        components.append(sorted(comp))
    return components


# ---------------------------------------------------------------------------
# R9, R11, R14: Main aggregation
# ---------------------------------------------------------------------------
def _make_node_id(file_path: str, suffix: str = "") -> str:
    """Generate a deterministic proposed node ID."""
    base = file_path.replace("\\", "/").replace("/", ".").replace(".py", "")
    if suffix:
        base = f"{base}.{suffix}"
    short = hashlib.sha1(base.encode()).hexdigest()[:8]
    return f"sym-{short}"


def aggregate_functions_into_nodes(
    modules: List[ModuleInfo],
    function_layers: Dict[str, Dict[str, Any]],
    call_graph: CallGraph,
) -> List[Dict[str, Any]]:
    """Aggregate scored functions into governance node dicts.

    Default: 1 node per file.  Split when the file has
    >= SPLIT_MIN_FUNCTIONS AND >= SPLIT_MIN_LAYER_TIERS distinct layers.
    """
    nodes: List[Dict[str, Any]] = []

    for mod in modules:
        file_path = mod.path
        func_qnames = [f.qualified_name for f in mod.functions]
        if not func_qnames:
            continue

        # Count distinct layer tiers
        layers_seen: Set[str] = set()
        for qn in func_qnames:
            layers_seen.add(
                function_layers.get(qn, {}).get("layer", "L3")
            )

        should_split = (
            len(func_qnames) >= SPLIT_MIN_FUNCTIONS
            and len(layers_seen) >= SPLIT_MIN_LAYER_TIERS
        )

        if not should_split:
            dominant, outliers = determine_dominant_layer(
                func_qnames, function_layers
            )
            nodes.append({
                "node_id_proposed": _make_node_id(file_path),
                "title": mod.module_name,
                "primary": [file_path],
                "dominant_layer": dominant,
                "outlier_functions": outliers,
                "function_count": len(func_qnames),
                "split_reason": None,
            })
            continue

        # Try region hints first (R11)
        regions = parse_region_hints(file_path)
        func_line_map = {
            f.qualified_name: f.lineno for f in mod.functions
        }

        if regions:
            # Region hints override CC splitting
            grouped: Dict[str, List[str]] = {}
            assigned: Set[str] = set()
            for rname, rlines in regions.items():
                members = _functions_in_region(
                    rlines, func_qnames, func_line_map
                )
                if members:
                    grouped[rname] = members
                    assigned.update(members)
            # Leftover functions not in any region
            leftover = [f for f in func_qnames if f not in assigned]
            if leftover:
                grouped["_unregioned"] = leftover

            for rname, members in grouped.items():
                dominant, outliers = determine_dominant_layer(
                    members, function_layers
                )
                nodes.append({
                    "node_id_proposed": _make_node_id(file_path, rname),
                    "title": f"{mod.module_name}:{rname}",
                    "primary": [file_path],
                    "dominant_layer": dominant,
                    "outlier_functions": outliers,
                    "function_count": len(members),
                    "split_reason": "region_hint",
                })
        else:
            # R12: Connected-component splitting
            components = split_by_connected_components(
                func_qnames, call_graph
            )
            for idx, comp in enumerate(components):
                dominant, outliers = determine_dominant_layer(
                    comp, function_layers
                )
                nodes.append({
                    "node_id_proposed": _make_node_id(
                        file_path, f"cc{idx}"
                    ),
                    "title": f"{mod.module_name}:cc{idx}",
                    "primary": [file_path],
                    "dominant_layer": dominant,
                    "outlier_functions": outliers,
                    "function_count": len(comp),
                    "split_reason": "connected_component",
                })

    return nodes
