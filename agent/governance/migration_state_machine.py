"""Migration state machine for safe graph.v2.json → graph.json swap.

4-condition swap gate, 14-day deadline with max 1-week extension, abort path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIGRATION_DEADLINE_DAYS = 14
MIGRATION_DEADLINE_EXTEND_MAX = 7  # max extension in days


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SwapGateResult:
    """Result of check_swap_gate evaluation."""
    can_swap: bool
    chain_count: int = 0
    signoff_ok: bool = False
    p0_p1_blockers: int = 0
    owner_approved: bool = False
    reason: str = ""


@dataclass
class MigrationWindow:
    """Represents an active migration window."""
    started_at: datetime
    deadline_at: datetime
    owner: str
    state: str = "active"  # active | completed | aborted
    current_extension: int = 0
    abort_reason: str = ""


# ---------------------------------------------------------------------------
# R6: 4-condition swap gate
# ---------------------------------------------------------------------------

def check_swap_gate(
    chain_count: int = 0,
    signoff_ok: bool = False,
    p0_p1_blockers: int = 0,
    owner_approved: bool = False,
) -> SwapGateResult:
    """Check if all 4 conditions for graph swap are met.

    Conditions:
    1. chain_count >= 3 (at least 3 green chains)
    2. signoff_ok = True (signoff ticket succeeded)
    3. p0_p1_blockers == 0 (zero P0/P1 blockers)
    4. owner_approved = True (owner has approved)

    Returns SwapGateResult with can_swap=True only when ALL conditions met.
    """
    reasons = []
    if chain_count < 3:
        reasons.append(f"chain_count={chain_count} < 3")
    if not signoff_ok:
        reasons.append("signoff not ok")
    if p0_p1_blockers != 0:
        reasons.append(f"p0_p1_blockers={p0_p1_blockers} != 0")
    if not owner_approved:
        reasons.append("owner not approved")

    can_swap = len(reasons) == 0

    return SwapGateResult(
        can_swap=can_swap,
        chain_count=chain_count,
        signoff_ok=signoff_ok,
        p0_p1_blockers=p0_p1_blockers,
        owner_approved=owner_approved,
        reason="; ".join(reasons) if reasons else "all conditions met",
    )


# ---------------------------------------------------------------------------
# R6: Migration window (14-day deadline)
# ---------------------------------------------------------------------------

def trigger_migration_window(
    owner: str,
    start: Optional[datetime] = None,
) -> MigrationWindow:
    """Start a migration window with a 14-day deadline.

    Args:
        owner: Owner identifier.
        start: Optional start time (defaults to now UTC).

    Returns:
        MigrationWindow with deadline = start + 14 days.
    """
    if start is None:
        start = datetime.now(timezone.utc)

    deadline = start + timedelta(days=MIGRATION_DEADLINE_DAYS)

    return MigrationWindow(
        started_at=start,
        deadline_at=deadline,
        owner=owner,
        state="active",
    )


def check_deadline_expired(window: MigrationWindow, now: Optional[datetime] = None) -> bool:
    """Check if the migration deadline has expired.

    Returns True if now > deadline_at.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now > window.deadline_at


def extend_deadline(window: MigrationWindow, days: int) -> MigrationWindow:
    """Extend the migration deadline by up to MIGRATION_DEADLINE_EXTEND_MAX days.

    Args:
        window: Active migration window.
        days: Number of days to extend.

    Returns:
        Updated MigrationWindow.

    Raises:
        ValueError: If total extension exceeds MIGRATION_DEADLINE_EXTEND_MAX.
    """
    new_total = window.current_extension + days
    if new_total > MIGRATION_DEADLINE_EXTEND_MAX:
        raise ValueError(
            f"Total extension {new_total} days exceeds max {MIGRATION_DEADLINE_EXTEND_MAX}"
        )

    window.deadline_at += timedelta(days=days)
    window.current_extension = new_total
    return window


def abort_migration(window: MigrationWindow, reason: str) -> MigrationWindow:
    """Abort the migration with a reason.

    Args:
        window: Active migration window.
        reason: Reason for abort.

    Returns:
        Updated MigrationWindow with state='aborted'.
    """
    window.state = "aborted"
    window.abort_reason = reason
    return window
