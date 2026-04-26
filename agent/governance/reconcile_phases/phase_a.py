"""Phase A — adapter that wraps existing reconcile.phase_diff into Discrepancy list.

Also detects:
- orphan_in_db: node_state records with no corresponding graph node
- stuck_testing: nodes in 'testing' status (likely stalled verification)
"""
from __future__ import annotations

import logging
from typing import List, TYPE_CHECKING

from ..reconcile import phase_diff

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)

# Lazy import to avoid circular ref at module level
_Discrepancy = None


def _get_discrepancy():
    global _Discrepancy
    if _Discrepancy is None:
        from . import Discrepancy
        _Discrepancy = Discrepancy
    return _Discrepancy


def run(ctx: "ReconcileContext", *, scope=None) -> list:
    """Run phase_diff via context and convert DiffReport → list[Discrepancy].

    Additionally checks DB-vs-graph consistency:
    - orphan_in_db: nodes in node_state table but not in graph definition
    - stuck_testing: nodes stuck in 'testing' verify_status

    When scope is provided (ResolvedScope), filters results to nodes whose
    files intersect scope.files() or whose node_id is in scope.node_set.
    """
    Discrepancy = _get_discrepancy()
    graph = ctx.graph
    if graph is None:
        return []

    diff = phase_diff(graph, ctx.file_set, ctx.file_metadata)
    results: list = []

    for ref in diff.stale_refs:
        results.append(Discrepancy(
            type="stale_ref",
            node_id=ref.node_id,
            field=ref.field,
            detail=f"{ref.old_path} -> {ref.suggestion or '(no match)'}",
            confidence=ref.confidence,
        ))

    for nid in diff.orphan_nodes:
        results.append(Discrepancy(
            type="orphan_node",
            node_id=nid,
            field=None,
            detail="all primary files missing",
            confidence="high",
        ))

    for path in diff.unmapped_files:
        results.append(Discrepancy(
            type="unmapped_file",
            node_id=None,
            field=None,
            detail=path,
            confidence="low",
        ))

    for ref in diff.stale_doc_refs:
        results.append(Discrepancy(
            type="stale_doc_ref",
            node_id=ref.node_id,
            field=ref.field,
            detail=f"{ref.old_path} -> {ref.suggestion or '(no match)'}",
            confidence=ref.confidence,
        ))

    for path in diff.unmapped_docs:
        results.append(Discrepancy(
            type="unmapped_doc",
            node_id=None,
            field=None,
            detail=path,
            confidence="low",
        ))

    # --- DB-vs-graph consistency checks ---
    _check_orphan_db_records(ctx, graph, results, Discrepancy)
    _check_stuck_testing_nodes(ctx, graph, results, Discrepancy)

    # --- scope filtering ---
    if scope is not None:
        results = _filter_by_scope(results, scope, graph)

    return results


def _filter_by_scope(results, scope, graph):
    """Keep only discrepancies whose node/file intersects scope."""
    scope_files = scope.files()
    scope_nodes = scope.node_set
    filtered = []
    for d in results:
        # File-level discrepancies (unmapped_file, unmapped_doc, stale_doc_ref)
        if d.node_id is None:
            # detail holds the file path for unmapped_file/unmapped_doc
            if d.detail in scope_files:
                filtered.append(d)
            continue
        # Node-level: check node_id in scope or node files intersect scope
        if d.node_id in scope_nodes:
            filtered.append(d)
            continue
        if graph is not None:
            try:
                node = graph.get_node(d.node_id)
                if node is not None:
                    from .scope import _node_files
                    nf = set(_node_files(node))
                    if nf & scope_files:
                        filtered.append(d)
                        continue
            except Exception:
                pass
    return filtered


def _check_orphan_db_records(ctx, graph, results, Discrepancy):
    """Detect node_state records that have no corresponding graph node."""
    try:
        all_db = ctx.all_db_node_state
        if not all_db:
            return
        graph_nodes = set(graph.list_nodes())
        orphan_db_ids = sorted(set(all_db.keys()) - graph_nodes)
        for nid in orphan_db_ids:
            row = all_db[nid]
            status = row.get("verify_status", "unknown")
            updated_by = row.get("updated_by", "unknown")
            results.append(Discrepancy(
                type="orphan_in_db",
                node_id=nid,
                field=None,
                detail=f"node_state exists (status={status}, by={updated_by}) "
                       f"but node not in graph definition",
                confidence="medium",
            ))
        if orphan_db_ids:
            log.info("Phase A: found %d orphan DB records not in graph", len(orphan_db_ids))
    except Exception as exc:
        log.warning("Phase A: orphan DB check failed (non-blocking): %s", exc)


def _check_stuck_testing_nodes(ctx, graph, results, Discrepancy):
    """Detect nodes stuck in 'testing' verify_status."""
    try:
        node_state = ctx.node_state
        if not node_state:
            return
        for nid, state in node_state.items():
            if state.get("verify_status") == "testing":
                results.append(Discrepancy(
                    type="stuck_testing",
                    node_id=nid,
                    field=None,
                    detail=f"node in 'testing' status (updated_by={state.get('updated_by', '?')})",
                    confidence="medium",
                ))
    except Exception as exc:
        log.warning("Phase A: stuck testing check failed (non-blocking): %s", exc)
