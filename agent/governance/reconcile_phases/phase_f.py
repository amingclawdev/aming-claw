"""Phase F — verify_status freshness checker.

Iterates all nodes with verify_status='qa_pass', fetches the last git
commit timestamp per primary file, and emits Discrepancy(type='verify_status_stale')
when file_mtime > node_updated_at + GRACE_PERIOD.

**Invariants**
- NEVER imports sqlite3 or opens governance.db directly.
- apply_phase_f_mutations uses POST /api/wf/{pid}/verify-update only.
- Nodes whose primary files are ALL in Phase A unmapped_files are skipped.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

import urllib.request
import urllib.error
import json

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)

# --- constants ---------------------------------------------------------------
DEFAULT_GRACE_PERIOD_DAYS = 1


# --- helpers -----------------------------------------------------------------

def _extract_unmapped_files_from_phase_a(
    phase_a_discrepancies: Optional[List[Any]],
) -> Set[str]:
    """Extract the set of unmapped file paths from Phase A discrepancies."""
    if not phase_a_discrepancies:
        return set()
    unmapped: Set[str] = set()
    for d in phase_a_discrepancies:
        if getattr(d, "type", None) == "unmapped_file":
            unmapped.add(getattr(d, "detail", ""))
    return unmapped


def _get_node_primary_files(graph: Any, node_id: str) -> List[str]:
    """Get primary files for a node from the graph."""
    try:
        node_data = graph.get_node(node_id)
        primary = node_data.get("primary", [])
        if isinstance(primary, str):
            return [primary] if primary else []
        return list(primary) if primary else []
    except Exception:
        return []


# --- core algorithm ----------------------------------------------------------

def run(
    ctx: "ReconcileContext",
    *,
    phase_a_discrepancies: Optional[List[Any]] = None,
    scope=None,
) -> list:
    """Run Phase F verify_status freshness check.

    Args:
        ctx: ReconcileContext with graph, node_state, workspace_path.
        phase_a_discrepancies: output of Phase A, used to skip orphaned nodes.

    Returns:
        List of Discrepancy objects for stale qa_pass nodes.
    """
    from . import Discrepancy

    graph = ctx.graph
    if graph is None:
        return []

    node_state = ctx.node_state
    if not node_state:
        return []

    grace_days = ctx.options.get("grace_period_days", DEFAULT_GRACE_PERIOD_DAYS)
    grace_period = timedelta(days=grace_days)

    unmapped_files = _extract_unmapped_files_from_phase_a(phase_a_discrepancies)

    results: list = []

    for node_id, state_row in node_state.items():
        verify_status = state_row.get("verify_status", "")
        if verify_status != "qa_pass":
            continue

        primary_files = _get_node_primary_files(graph, node_id)
        if not primary_files:
            continue

        # R6: Skip nodes whose primary files are ALL in Phase A unmapped_files
        if unmapped_files and all(f in unmapped_files for f in primary_files):
            continue

        # Get node updated_at
        updated_at_str = state_row.get("updated_at", "")
        if not updated_at_str:
            continue

        try:
            node_updated_at = _parse_datetime(updated_at_str)
        except (ValueError, TypeError):
            log.warning("Cannot parse updated_at for node %s: %s", node_id, updated_at_str)
            continue

        # Check each primary file for staleness
        max_days_stale = 0.0
        stale_file = None

        for fpath in primary_files:
            if unmapped_files and fpath in unmapped_files:
                continue  # Skip unmapped files individually

            file_mtime = ctx.git_log_per_file_last_commit_date(fpath)
            if file_mtime is None:
                continue

            threshold = node_updated_at + grace_period
            if file_mtime > threshold:
                days = (file_mtime - node_updated_at).total_seconds() / 86400
                if days > max_days_stale:
                    max_days_stale = days
                    stale_file = fpath

        if stale_file is not None and max_days_stale > 0:
            # R2: confidence and suggested_action based on days_stale
            if max_days_stale > 7:
                confidence = "high"
                suggested_action = "revert_to_pending"
            else:
                confidence = "medium"
                suggested_action = "flag_for_review"

            results.append(Discrepancy(
                type="verify_status_stale",
                node_id=node_id,
                field="primary",
                detail=(
                    f"file={stale_file} days_stale={max_days_stale:.1f} "
                    f"suggested_action={suggested_action}"
                ),
                confidence=confidence,
            ))

    # --- scope filtering: keep only nodes in scope.node_set ---
    if scope is not None:
        scope_nodes = scope.node_set
        if scope_nodes:
            results = [d for d in results if d.node_id in scope_nodes]

    return results


# --- mutation ----------------------------------------------------------------

def apply_phase_f_mutations(
    ctx: "ReconcileContext",
    discrepancies: list,
    *,
    threshold: str = "high",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Apply mutations for Phase F discrepancies via HTTP API.

    Uses POST /api/wf/{pid}/verify-update with status='pending'.
    NEVER directly accesses the database.

    Args:
        ctx: ReconcileContext.
        discrepancies: list of Discrepancy objects from run().
        threshold: minimum confidence level to act on ('high' or 'medium').
        dry_run: if True, no HTTP calls are made.

    Returns:
        Dict with 'applied' count and 'skipped' count.
    """
    applied = 0
    skipped = 0
    errors = 0

    threshold_levels = {"high"}
    if threshold == "medium":
        threshold_levels = {"high", "medium"}

    for d in discrepancies:
        if getattr(d, "type", "") != "verify_status_stale":
            continue

        if getattr(d, "confidence", "") not in threshold_levels:
            skipped += 1
            continue

        node_id = getattr(d, "node_id", None)
        if not node_id:
            skipped += 1
            continue

        if dry_run:
            skipped += 1
            continue

        # POST /api/wf/{pid}/verify-update
        try:
            url = f"http://localhost:40000/api/wf/{ctx.project_id}/verify-update"
            payload = json.dumps({
                "nodes": [node_id],
                "status": "pending",
                "evidence": {
                    "reason": "phase_f_staleness_revert",
                    "detail": getattr(d, "detail", ""),
                },
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            applied += 1
        except (urllib.error.URLError, OSError) as exc:
            log.error("Failed to update node %s: %s", node_id, exc)
            errors += 1

    return {"applied": applied, "skipped": skipped, "errors": errors}


# --- helpers (private) -------------------------------------------------------

def _parse_datetime(s: str) -> datetime:
    """Parse ISO datetime string to timezone-aware datetime."""
    # Handle various ISO formats
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Fallback: try without timezone
        dt = datetime.fromisoformat(s.replace("+00:00", "").replace("Z", ""))
        return dt.replace(tzinfo=timezone.utc)
