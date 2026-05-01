"""Symbol disappearance review for the v2 graph swap.

This module implements pre-swap diff/classification of nodes that disappear
between an old graph and a new candidate graph (e.g. graph.json vs
graph.v2.json). It is invoked from the ``review`` subcommand of
``scripts/phase-z-v2.py`` and from the spec §6 reconcile workflow.

The module is deliberately deterministic — no AI / no network. All
classification heuristics derive solely from the JSON contents of the two
graphs and a few simple structural rules.

Public API:

* :data:`REMOVAL_REASONS` — tuple of the 5 canonical removal reasons.
* :data:`OBSERVER_DECISIONS` — tuple of the 4 canonical observer decisions.
* :func:`diff_removed_nodes` — return nodes present in old graph but absent
  in the new candidate graph.
* :func:`classify_removal` — assign a removal reason to a single removed
  node (deterministic).
* :func:`require_observer_decision` — validate that every removed node has
  a recorded observer decision.
* :func:`detect_governance_markers` — flag nodes carrying B36 dangling-L7,
  legacy waiver, or manual carve-out markers.

Spec reference: docs/governance/reconcile-workflow.md §6
(Phase 4 Disappearance Review).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Public constants — order is significant for spec compliance.
# ---------------------------------------------------------------------------

REMOVAL_REASONS: Tuple[str, ...] = (
    "files_relocated",
    "files_deleted",
    "merged_into_other_node",
    "low_confidence_inference",
    "no_matching_call_topology",
)
"""All 5 canonical removal reasons. See spec §6.2."""

OBSERVER_DECISIONS: Tuple[str, ...] = (
    "approve_removal",
    "map_to_new_node",
    "preserve_as_supplement",
    "block_swap",
)
"""All 4 canonical observer decisions. See spec §6.3."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _nodes_of(graph: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Return a list of node dicts from a graph mapping.

    Accepts either ``{"nodes": [...]}`` or ``{"nodes": {id: node, ...}}``.
    """
    if not isinstance(graph, Mapping):
        return []
    raw = graph.get("nodes")
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        return [n for n in raw.values() if isinstance(n, Mapping)]
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, Mapping)]
    return []


def _index_by_id(nodes: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for n in nodes:
        nid = n.get("node_id") or n.get("id")
        if isinstance(nid, str):
            out[nid] = n
    return out


def _primary_paths(node: Mapping[str, Any]) -> List[str]:
    """Extract primary file paths from a node, accepting str or list."""
    primary = node.get("primary")
    if primary is None:
        primary = node.get("primary_files")
    if primary is None:
        return []
    if isinstance(primary, str):
        return [primary]
    if isinstance(primary, (list, tuple)):
        return [str(p) for p in primary]
    return []


def _relocated_set(old_paths: Sequence[str], new_graph_paths: Iterable[str]) -> bool:
    """Return True if all old_paths basenames exist somewhere in new graph."""
    if not old_paths:
        return False
    new_basenames = {p.rsplit("/", 1)[-1] for p in new_graph_paths if p}
    old_basenames = {p.rsplit("/", 1)[-1] for p in old_paths if p}
    if not old_basenames:
        return False
    # all old basenames also appear under a (potentially different) directory
    return old_basenames.issubset(new_basenames)


def _all_new_primary_paths(new_graph: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for n in _nodes_of(new_graph):
        out.extend(_primary_paths(n))
    return out


# ---------------------------------------------------------------------------
# diff_removed_nodes
# ---------------------------------------------------------------------------

def diff_removed_nodes(
    old_graph: Mapping[str, Any],
    new_graph: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Return nodes present in *old_graph* but absent in *new_graph*.

    The returned list contains shallow copies of the old-graph node dicts so
    that callers can attach metadata (decision, classification, …) without
    mutating the original graph.

    Stable ordering: nodes are returned in the order they appear in the old
    graph (insertion order) for deterministic downstream JSON output.
    """
    old_nodes = _nodes_of(old_graph)
    new_index = _index_by_id(_nodes_of(new_graph))
    removed: List[Dict[str, Any]] = []
    for node in old_nodes:
        nid = node.get("node_id") or node.get("id")
        if not isinstance(nid, str):
            continue
        if nid not in new_index:
            removed.append(dict(node))
    return removed


# ---------------------------------------------------------------------------
# classify_removal
# ---------------------------------------------------------------------------

def classify_removal(
    node: Mapping[str, Any],
    old_graph: Mapping[str, Any],
    new_graph: Mapping[str, Any],
) -> str:
    """Classify why *node* disappears between the two graphs.

    Returns one of :data:`REMOVAL_REASONS`. The classification is a
    deterministic decision tree based purely on the JSON contents of the
    graphs and the ``primary`` paths recorded on the node — no filesystem
    access is required.

    Decision order (first match wins):

    1. ``files_relocated`` — every primary path basename is present somewhere
       in the new graph (file moved to a different module).
    2. ``files_deleted`` — node carries an explicit ``deleted: true`` /
       ``status == 'deleted'`` marker, or every primary path appears in
       ``new_graph['removed_files']`` (legacy schema).
    3. ``merged_into_other_node`` — the node's ``merged_into`` field
       references a node id present in the new graph, OR another node in the
       new graph claims this id in ``merged_from``.
    4. ``low_confidence_inference`` — node was originally inferred with
       ``confidence < 0.7`` (or ``inference_confidence < 0.7``).
    5. ``no_matching_call_topology`` — fallback when none of the above
       conditions hold.
    """
    new_primary = _all_new_primary_paths(new_graph)
    node_primary = _primary_paths(node)

    # 1. relocation — basenames preserved
    if node_primary and _relocated_set(node_primary, new_primary):
        return "files_relocated"

    # 2. files deleted — explicit markers
    if node.get("deleted") is True or node.get("status") == "deleted":
        return "files_deleted"
    removed_files = new_graph.get("removed_files") if isinstance(new_graph, Mapping) else None
    if isinstance(removed_files, (list, tuple)) and node_primary:
        removed_set = set(removed_files)
        if all(p in removed_set for p in node_primary):
            return "files_deleted"

    # 3. merged into another node
    merged_into = node.get("merged_into")
    if isinstance(merged_into, str) and merged_into:
        new_index = _index_by_id(_nodes_of(new_graph))
        if merged_into in new_index:
            return "merged_into_other_node"
    nid = node.get("node_id") or node.get("id")
    if isinstance(nid, str):
        for new_node in _nodes_of(new_graph):
            merged_from = new_node.get("merged_from") or []
            if isinstance(merged_from, (list, tuple)) and nid in merged_from:
                return "merged_into_other_node"

    # 4. low confidence inference
    conf = node.get("confidence")
    if conf is None:
        conf = node.get("inference_confidence")
    try:
        if conf is not None and float(conf) < 0.7:
            return "low_confidence_inference"
    except (TypeError, ValueError):
        pass

    # 5. fallback
    return "no_matching_call_topology"


# ---------------------------------------------------------------------------
# require_observer_decision
# ---------------------------------------------------------------------------

def require_observer_decision(
    removed_nodes: Sequence[Mapping[str, Any]],
    decisions: Mapping[str, str],
) -> Dict[str, Any]:
    """Validate that every removed node has a valid observer decision.

    Args:
        removed_nodes: output of :func:`diff_removed_nodes` (or any iterable
            of node-like dicts with a ``node_id`` field).
        decisions: mapping ``{node_id: decision_string}`` where each decision
            must be in :data:`OBSERVER_DECISIONS`.

    Returns a dict with three keys:

    * ``ok`` — ``True`` only when every removed node has a valid decision
      AND no decision is ``"block_swap"``.
    * ``missing`` — list of node_ids that have no decision or an unknown
      decision string.
    * ``blocked`` — list of node_ids whose decision is ``"block_swap"``.
    """
    missing: List[str] = []
    blocked: List[str] = []
    valid_decisions = set(OBSERVER_DECISIONS)
    for node in removed_nodes:
        nid = node.get("node_id") or node.get("id")
        if not isinstance(nid, str):
            continue
        d = decisions.get(nid) if isinstance(decisions, Mapping) else None
        if d not in valid_decisions:
            missing.append(nid)
            continue
        if d == "block_swap":
            blocked.append(nid)
    ok = not missing and not blocked
    return {"ok": ok, "missing": missing, "blocked": blocked}


# ---------------------------------------------------------------------------
# detect_governance_markers
# ---------------------------------------------------------------------------

def detect_governance_markers(node: Mapping[str, Any]) -> Dict[str, bool]:
    """Flag governance-relevant markers on *node*.

    Returns a dict with three boolean flags:

    * ``b36_dangling_l7`` — node carries the B36 dangling-L7 marker (either
      via ``governance_markers`` list or a top-level ``b36_dangling_l7``
      flag) **or** is on layer L7 with no QA pass record.
    * ``legacy_waiver`` — node has a ``waiver`` field or its
      ``governance_markers`` list contains ``"legacy_waiver"``.
    * ``manual_carve_out`` — node has ``manual_carve_out: true`` or its
      ``governance_markers`` list contains ``"manual_carve_out"``.
    """
    markers: Sequence[str] = ()
    raw_markers = node.get("governance_markers")
    if isinstance(raw_markers, (list, tuple)):
        markers = tuple(str(m) for m in raw_markers)
    markers_set = set(markers)

    b36 = bool(node.get("b36_dangling_l7")) or "b36_dangling_l7" in markers_set
    if not b36:
        # heuristic: L7 node with no qa_passed flag
        layer = node.get("layer") or node.get("parent_layer")
        if isinstance(layer, str) and layer.upper().startswith("L7") and not node.get("qa_passed"):
            b36 = True

    legacy = bool(node.get("waiver")) or "legacy_waiver" in markers_set
    manual = bool(node.get("manual_carve_out")) or "manual_carve_out" in markers_set

    return {
        "b36_dangling_l7": b36,
        "legacy_waiver": legacy,
        "manual_carve_out": manual,
    }


# ---------------------------------------------------------------------------
# Convenience: full review report (used by the CLI)
# ---------------------------------------------------------------------------

def review_report(
    old_graph: Mapping[str, Any],
    new_graph: Mapping[str, Any],
    decisions: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Produce a JSON-serialisable disappearance review report.

    The CLI ``review`` subcommand returns this structure. ``decisions`` is
    optional — when omitted, every removed node is reported as missing a
    decision.
    """
    removed = diff_removed_nodes(old_graph, new_graph)
    items: List[Dict[str, Any]] = []
    for node in removed:
        nid = node.get("node_id") or node.get("id")
        items.append(
            {
                "node_id": nid,
                "title": node.get("title"),
                "primary": _primary_paths(node),
                "reason": classify_removal(node, old_graph, new_graph),
                "markers": detect_governance_markers(node),
            }
        )
    decision_status = require_observer_decision(removed, decisions or {})
    return {
        "removed_count": len(items),
        "items": items,
        "decision_status": decision_status,
        "removal_reasons": list(REMOVAL_REASONS),
        "observer_decisions": list(OBSERVER_DECISIONS),
    }


__all__ = [
    "REMOVAL_REASONS",
    "OBSERVER_DECISIONS",
    "diff_removed_nodes",
    "classify_removal",
    "require_observer_decision",
    "detect_governance_markers",
    "review_report",
]
