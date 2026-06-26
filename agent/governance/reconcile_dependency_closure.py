"""Deterministic dependency closure helpers for reconcile workers.

The functions in this module are intentionally pure: inputs are JSON-shaped
Python values and outputs contain only JSON-serializable primitives.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

FAN_IN = "fan_in"
FAN_OUT = "fan_out"
_VALID_DIRECTIONS = {FAN_IN, FAN_OUT}


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _stable_unique(values: Iterable[Any]) -> list[str]:
    return sorted({_clean_id(value) for value in values if _clean_id(value)})


def _edge_type(link: Mapping[str, Any]) -> str:
    return str(link.get("type") or link.get("relation_type") or "depends_on").strip()


def _allowed_edge_types(edge_types: Iterable[Any] | None) -> set[str] | None:
    if edge_types is None:
        return None
    return {_clean_id(edge_type) for edge_type in edge_types if _clean_id(edge_type)}


def _links_from_graph_or_links(graph_or_links: Any) -> Iterable[Any]:
    if isinstance(graph_or_links, Mapping):
        deps_graph = graph_or_links.get("deps_graph")
        if isinstance(deps_graph, Mapping):
            return deps_graph.get("links") or []
        return graph_or_links.get("links") or []
    return graph_or_links or []


def normalize_dependency_links(
    graph_or_links: Any,
    *,
    edge_types: Iterable[Any] | None = None,
) -> list[dict[str, str]]:
    """Return deduplicated dependency links sorted by source, target, and type."""

    allowed = _allowed_edge_types(edge_types)
    seen: set[tuple[str, str, str]] = set()
    normalized: list[dict[str, str]] = []
    for raw_link in _links_from_graph_or_links(graph_or_links):
        if not isinstance(raw_link, Mapping):
            continue
        source = _clean_id(raw_link.get("source"))
        target = _clean_id(raw_link.get("target"))
        relation_type = _edge_type(raw_link)
        if not source or not target or source == target:
            continue
        if allowed is not None and relation_type not in allowed:
            continue
        key = (source, target, relation_type)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"source": source, "target": target, "type": relation_type})
    return sorted(
        normalized,
        key=lambda link: (link["source"], link["target"], link["type"]),
    )


def build_dependency_adjacency(
    graph_or_links: Any,
    *,
    direction: str = FAN_OUT,
    edge_types: Iterable[Any] | None = None,
) -> dict[str, list[str]]:
    """Build a stable adjacency map for fan-out or fan-in traversal."""

    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_VALID_DIRECTIONS)}")
    adjacency: dict[str, set[str]] = {}
    for link in normalize_dependency_links(graph_or_links, edge_types=edge_types):
        source = link["source"]
        target = link["target"]
        if direction == FAN_OUT:
            adjacency.setdefault(source, set()).add(target)
        else:
            adjacency.setdefault(target, set()).add(source)
    return {
        node_id: sorted(children)
        for node_id, children in sorted(adjacency.items())
        if children
    }


def stable_dependency_dfs(
    root: Any,
    adjacency: Mapping[str, Iterable[Any]],
    *,
    include_root: bool = False,
) -> list[str]:
    """Return deterministic depth-first traversal from one root."""

    root_id = _clean_id(root)
    if not root_id:
        return []

    normalized_adjacency = {
        _clean_id(node_id): _stable_unique(children)
        for node_id, children in adjacency.items()
        if _clean_id(node_id)
    }
    seen: set[str] = set()
    ordered: list[str] = []
    if include_root:
        stack = [root_id]
    else:
        seen.add(root_id)
        stack = list(reversed(normalized_adjacency.get(root_id, [])))

    while stack:
        node_id = stack.pop()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        ordered.append(node_id)
        for child_id in reversed(normalized_adjacency.get(node_id, [])):
            if child_id not in seen:
                stack.append(child_id)
    return ordered


def dependency_closure(
    root_nodes: Iterable[Any],
    graph_or_links: Any,
    *,
    direction: str = FAN_OUT,
    edge_types: Iterable[Any] | None = None,
    include_roots: bool = False,
) -> dict[str, Any]:
    """Compute a deterministic dependency closure for root nodes."""

    roots = _stable_unique(root_nodes)
    adjacency = build_dependency_adjacency(
        graph_or_links,
        direction=direction,
        edge_types=edge_types,
    )
    by_root = {
        root_id: stable_dependency_dfs(root_id, adjacency, include_root=include_roots)
        for root_id in roots
    }
    ordered: list[str] = []
    seen: set[str] = set()
    for root_id in roots:
        for node_id in by_root[root_id]:
            if node_id not in seen:
                seen.add(node_id)
                ordered.append(node_id)
    return {
        "schema_version": "dependency_closure.v1",
        "direction": direction,
        "roots": roots,
        "include_roots": bool(include_roots),
        "order": ordered,
        "reachable": ordered,
        "node_count": len(ordered),
        "by_root": by_root,
        "adjacency": adjacency,
    }


def reduce_dependency_closures(closures: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Reduce one or more closure payloads into sorted deterministic unions."""

    directions: set[str] = set()
    roots: set[str] = set()
    reachable: set[str] = set()
    by_direction: dict[str, set[str]] = {}
    by_root: dict[str, set[str]] = {}
    closure_count = 0

    for closure in closures:
        if not isinstance(closure, Mapping):
            continue
        closure_count += 1
        direction = _clean_id(closure.get("direction"))
        if direction:
            directions.add(direction)
        closure_roots = _stable_unique(closure.get("roots") or [])
        roots.update(closure_roots)
        nodes = _stable_unique(closure.get("reachable") or closure.get("order") or [])
        reachable.update(nodes)
        if direction:
            by_direction.setdefault(direction, set()).update(nodes)
        raw_by_root = closure.get("by_root") or {}
        if isinstance(raw_by_root, Mapping):
            for root_id, root_nodes in raw_by_root.items():
                clean_root = _clean_id(root_id)
                if not clean_root:
                    continue
                roots.add(clean_root)
                by_root.setdefault(clean_root, set()).update(_stable_unique(root_nodes or []))

    return {
        "schema_version": "dependency_closure_reduce.v1",
        "closure_count": closure_count,
        "directions": sorted(directions),
        "roots": sorted(roots),
        "reachable": sorted(reachable),
        "node_count": len(reachable),
        "by_direction": {
            direction: sorted(nodes)
            for direction, nodes in sorted(by_direction.items())
        },
        "by_root": {
            root_id: sorted(nodes)
            for root_id, nodes in sorted(by_root.items())
        },
    }


def dependency_graph_has_cycle(
    graph_or_links: Any,
    *,
    edge_types: Iterable[Any] | None = None,
) -> bool:
    """Return True when normalized dependency links contain a directed cycle."""

    adjacency = build_dependency_adjacency(
        graph_or_links,
        direction=FAN_OUT,
        edge_types=edge_types,
    )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for child_id in adjacency.get(node_id, []):
            if visit(child_id):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in sorted(adjacency))


def dependency_impact_closure(
    root_nodes: Iterable[Any],
    graph_or_links: Any,
    *,
    edge_types: Iterable[Any] | None = None,
    include_roots: bool = False,
) -> dict[str, Any]:
    """Compute fan-in, fan-out, and reduced impact closure in one payload."""

    fan_in = dependency_closure(
        root_nodes,
        graph_or_links,
        direction=FAN_IN,
        edge_types=edge_types,
        include_roots=include_roots,
    )
    fan_out = dependency_closure(
        root_nodes,
        graph_or_links,
        direction=FAN_OUT,
        edge_types=edge_types,
        include_roots=include_roots,
    )
    reduced = reduce_dependency_closures([fan_in, fan_out])
    return {
        "schema_version": "dependency_impact_closure.v1",
        "roots": reduced["roots"],
        "fan_in": fan_in,
        "fan_out": fan_out,
        "impact": reduced,
        "impacted_nodes": reduced["reachable"],
        "impacted_node_count": reduced["node_count"],
        "has_cycle": dependency_graph_has_cycle(graph_or_links, edge_types=edge_types),
    }


__all__ = [
    "FAN_IN",
    "FAN_OUT",
    "build_dependency_adjacency",
    "dependency_closure",
    "dependency_graph_has_cycle",
    "dependency_impact_closure",
    "normalize_dependency_links",
    "reduce_dependency_closures",
    "stable_dependency_dfs",
]
