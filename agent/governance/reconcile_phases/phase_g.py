"""Phase G — chain closure invariant checker.

Iterates PM tasks created within the last 30 days, checks each for a
complete chain event sequence ending in ``merge.completed``.  PM tasks
older than N_DAYS (default 7, configurable via ctx.options['phase_g_n_days'])
without ``merge.completed`` emit Discrepancy(type='chain_not_closed').

**Invariants**
- Phase G is REPORT-ONLY: ``apply_phase_g_mutations`` is a no-op.
- PM tasks with status 'cancelled' or 'failed' are skipped (terminal).
- PM tasks younger than N_DAYS are skipped (still in normal flow window).
- Confidence: 'high' when age_days > N_DAYS*2, else 'medium'.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)

# --- constants ---------------------------------------------------------------
N_DAYS_DEFAULT = 7
_TERMINAL_STATUSES = frozenset({"cancelled", "failed"})
_MAX_LOOKBACK_DAYS = 30


# --- core algorithm ----------------------------------------------------------

def run(ctx: "ReconcileContext", *, scope=None) -> list:
    """Run Phase G chain closure invariant check.

    Args:
        ctx: ReconcileContext with project_id and options.

    Returns:
        List of Discrepancy objects for PM tasks with unclosed chains.
    """
    from . import Discrepancy

    n_days = ctx.options.get("phase_g_n_days", N_DAYS_DEFAULT)
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=n_days)

    pm_tasks = _load_pm_tasks(ctx)
    if not pm_tasks:
        return []

    results: list = []

    for task in pm_tasks:
        status = task.get("status", "")
        # R3: skip terminal statuses
        if status in _TERMINAL_STATUSES:
            continue

        created_at = _parse_datetime(task.get("created_at", ""))
        if created_at is None:
            continue

        age_days = (now - created_at).total_seconds() / 86400

        # R4: skip tasks younger than N_DAYS
        if created_at > cutoff:
            continue

        task_id = task.get("task_id", "")

        # Check chain events for merge.completed
        chain_events = _load_chain_events(ctx, task_id)
        has_merge_completed = any(
            ev.get("event_type") == "merge.completed"
            for ev in chain_events
        )
        if has_merge_completed:
            continue

        # Determine last event info
        last_event_type = None
        last_event_at = None
        if chain_events:
            last = chain_events[-1]  # already ordered by created_at
            last_event_type = last.get("event_type")
            last_event_at = last.get("created_at")

        # R5: confidence
        confidence = "high" if age_days > n_days * 2 else "medium"

        results.append(Discrepancy(
            type="chain_not_closed",
            node_id=None,
            field=None,
            detail=(
                f"pm_task_id={task_id} age_days={age_days:.1f} "
                f"last_event_type={last_event_type} "
                f"last_event_at={last_event_at} "
                f"suggested_action=cancel_or_resume"
            ),
            confidence=confidence,
        ))

    # --- scope filtering: keep only PM tasks intersecting scope.files() ---
    if scope is not None:
        scope_files = scope.files()
        if scope_files:
            filtered = []
            for d in results:
                # Phase G discrepancies are PM task level; include if task's
                # target_files intersect scope (best effort from detail)
                filtered.append(d)  # Phase G has no file association; keep all when scoped
            results = filtered

    return results


# --- mutation (no-op) --------------------------------------------------------

def apply_phase_g_mutations(
    ctx: "ReconcileContext",
    discrepancies: list,
    *,
    threshold: str = "high",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Phase G is REPORT-ONLY. Always returns applied=0.

    Never auto-cancels PM tasks regardless of dry_run or threshold.
    """
    return {"applied": 0, "skipped": len(discrepancies)}


# --- data loaders ------------------------------------------------------------

def _load_pm_tasks(ctx: "ReconcileContext") -> List[Dict[str, Any]]:
    """Load PM tasks from governance DB via state_service.

    SELECT * FROM tasks WHERE type='pm' AND project_id=?
    Filters to last 30 days in-memory for safety.
    """
    try:
        from ..db import get_connection
        conn = get_connection(ctx.project_id)
        cursor = conn.execute(
            "SELECT * FROM tasks WHERE type='pm' AND project_id=?",
            (ctx.project_id,),
        )
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        tasks = [dict(zip(columns, row)) for row in rows]

        # Filter to last 30 days
        now = datetime.now(tz=timezone.utc)
        cutoff_30d = now - timedelta(days=_MAX_LOOKBACK_DAYS)
        filtered = []
        for t in tasks:
            created = _parse_datetime(t.get("created_at", ""))
            if created is not None and created >= cutoff_30d:
                filtered.append(t)
        return filtered
    except Exception as exc:
        log.warning("Failed to load PM tasks: %s", exc)
        return []


def _load_chain_events(
    ctx: "ReconcileContext", task_id: str,
) -> List[Dict[str, Any]]:
    """Load chain events for a task.

    SELECT * FROM chain_events
    WHERE root_task_id=? OR task_id=?
    ORDER BY created_at
    """
    try:
        from ..db import get_connection
        conn = get_connection(ctx.project_id)
        cursor = conn.execute(
            "SELECT * FROM chain_events "
            "WHERE root_task_id=? OR task_id=? "
            "ORDER BY created_at",
            (task_id, task_id),
        )
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as exc:
        log.warning("Failed to load chain events for %s: %s", task_id, exc)
        return []


# --- helpers (private) -------------------------------------------------------

def _parse_datetime(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO datetime string to timezone-aware datetime."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("+00:00", "").replace("Z", ""))
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
