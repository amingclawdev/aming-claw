"""Phase A — adapter that wraps existing reconcile.phase_diff into Discrepancy list."""
from __future__ import annotations

from typing import List, TYPE_CHECKING

from ..reconcile import phase_diff

if TYPE_CHECKING:
    from .context import ReconcileContext

# Lazy import to avoid circular ref at module level
_Discrepancy = None


def _get_discrepancy():
    global _Discrepancy
    if _Discrepancy is None:
        from . import Discrepancy
        _Discrepancy = Discrepancy
    return _Discrepancy


def run(ctx: "ReconcileContext") -> list:
    """Run phase_diff via context and convert DiffReport → list[Discrepancy]."""
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

    return results
