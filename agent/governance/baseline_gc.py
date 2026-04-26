"""Baseline Garbage Collection (§17a.6).

Implements three-rule retention for version_baselines rows and their
companion file directories:

1. Keep the last *N* baselines (ordered by baseline_id DESC).
2. Keep at least one baseline per distinct calendar month.
3. Keep ALL baselines whose ``trigger`` is ``'manual-fix'``.

Historical *reconstructed* baselines receive **no** special protection
(KEEP_RECONSTRUCTED = False).

``dry_run=True`` (the default) previews what would be deleted without
performing any mutations.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEEP_RECONSTRUCTED = False  # R2: no special protection for reconstructed rows


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------

def _baselines_root(project_id: str) -> Path:
    """Mirror of baseline_service._baselines_root (avoid circular dep)."""
    from .db import _governance_root
    return _governance_root() / project_id / "baselines"


def _fetch_all_baselines(conn, project_id: str) -> List[dict]:
    """Return all baselines ordered by baseline_id ASC (oldest first)."""
    cur = conn.execute(
        "SELECT baseline_id, trigger, created_at, reconstructed "
        "FROM version_baselines WHERE project_id = ? "
        "ORDER BY baseline_id ASC",
        (project_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _classify(
    baselines: List[dict],
    keep_last_n: int,
) -> tuple:
    """Return (keep_set, delete_list) applying the three retention rules.

    Each kept baseline is annotated with ``reason_kept``.
    """
    if not baselines:
        return [], []

    total = len(baselines)

    # --- Rule 1: last N by baseline_id (already sorted ASC) ----
    last_n_ids = {b["baseline_id"] for b in baselines[max(0, total - keep_last_n):]}

    # --- Rule 3: manual-fix trigger ----
    manual_fix_ids = {b["baseline_id"] for b in baselines if b["trigger"] == "manual-fix"}

    # --- Rule 2: one per calendar month ----
    month_rep_ids: set = set()
    seen_months: set = set()
    # Walk oldest→newest so the earliest baseline per month is kept.
    for b in baselines:
        ym = b["created_at"][:7]  # "YYYY-MM"
        if ym not in seen_months:
            seen_months.add(ym)
            month_rep_ids.add(b["baseline_id"])

    # --- Rule 4 (R11): slice baselines with unresolved merge_status ----
    # Slice baselines where merge_status IN (NULL, 'unknown', 'conflict')
    # must NEVER be deleted by GC.
    unresolved_slice_ids = set()
    for b in baselines:
        scope_kind = b.get("scope_kind")
        merge_status = b.get("merge_status")
        if scope_kind is not None and merge_status != "merged":
            unresolved_slice_ids.add(b["baseline_id"])

    # Build annotated lists
    keep: List[dict] = []
    delete: List[dict] = []
    for b in baselines:
        bid = b["baseline_id"]
        reasons: List[str] = []
        if bid in last_n_ids:
            reasons.append("last_n")
        if bid in manual_fix_ids:
            reasons.append("manual_fix")
        if bid in month_rep_ids:
            reasons.append("month_representative")
        if bid in unresolved_slice_ids:
            reasons.append("unresolved_slice")
        if reasons:
            b["reason_kept"] = ",".join(reasons)
            keep.append(b)
        else:
            delete.append(b)

    return keep, delete


# ---------------------------------------------------------------------------
# Destructive helpers (only called when dry_run=False)
# ---------------------------------------------------------------------------

def delete_companion_files(project_id: str, baseline_id: int) -> bool:
    """Remove companion file directory tree (R4).

    Returns True if the directory existed and was removed.
    """
    d = _baselines_root(project_id) / str(baseline_id)
    if d.is_dir():
        shutil.rmtree(d)
        return True
    return False


def delete_baseline_row(conn, project_id: str, baseline_id: int) -> None:
    """DELETE a single row from version_baselines (R5)."""
    conn.execute(
        "DELETE FROM version_baselines WHERE project_id = ? AND baseline_id = ?",
        (project_id, baseline_id),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def gc_baselines(
    project_id: str,
    dry_run: bool = True,
    keep_last_n: int = 100,
) -> Dict[str, Any]:
    """Run baseline garbage collection (R1, R3, R6, R7).

    Returns dict:
        kept      – number of baselines retained
        deleted   – number of baselines removed (or would-be in dry_run)
        dry_run   – echo of the flag
        details   – first 5 kept baselines with reason_kept annotation
    """
    from .db import get_connection
    from . import audit_service

    conn = get_connection(project_id)
    try:
        baselines = _fetch_all_baselines(conn, project_id)
        keep, delete = _classify(baselines, keep_last_n)

        if not dry_run:
            for b in delete:
                delete_companion_files(project_id, b["baseline_id"])
                delete_baseline_row(conn, project_id, b["baseline_id"])
            conn.commit()

        result: Dict[str, Any] = {
            "kept": len(keep),
            "deleted": len(delete),
            "dry_run": dry_run,
            "details": keep[:5],
        }

        # R7: audit log
        audit_service.record(
            conn,
            project_id,
            "gc.run",
            actor="baseline-gc",
            kept=result["kept"],
            deleted=result["deleted"],
            dry_run=dry_run,
        )
        conn.commit()

        return result
    finally:
        conn.close()
