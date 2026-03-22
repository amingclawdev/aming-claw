"""State service — runtime state management (Layer 2).

Manages node verify_status transitions with permission/evidence/gate checks.
All mutations go through SQLite transactions with audit logging.
"""

import json
import sqlite3
from datetime import datetime, timezone

from .enums import VerifyStatus, Role
from .models import Evidence
from .errors import (
    NodeNotFoundError, ConflictError, ValidationError,
    ReleaseBlockedError,
)
from .permissions import check_transition, check_nodes_scope
from .evidence import validate_evidence
from .gate_policy import check_gates_or_raise
from . import audit_service
from . import event_bus


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_node_states(conn: sqlite3.Connection, project_id: str, graph) -> int:
    """Initialize node_state rows from graph. Returns count of nodes initialized."""
    count = 0
    now = _utc_iso()
    for node_id in graph.list_nodes():
        existing = conn.execute(
            "SELECT 1 FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()
        if not existing:
            node_data = graph.get_node(node_id)
            conn.execute(
                """INSERT INTO node_state
                   (project_id, node_id, verify_status, build_status, updated_at, version)
                   VALUES (?, ?, 'pending', ?, ?, 1)""",
                (project_id, node_id, node_data.get("build_status", "impl:missing"), now),
            )
            count += 1
    return count


def get_node_status(conn: sqlite3.Connection, project_id: str, node_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, node_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result.get("evidence_json"):
        try:
            result["evidence"] = json.loads(result["evidence_json"])
        except (json.JSONDecodeError, TypeError):
            result["evidence"] = None
    return result


def _get_status_fn(conn: sqlite3.Connection, project_id: str):
    """Return a function that gets VerifyStatus for a node_id."""
    def _fn(node_id: str) -> VerifyStatus:
        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()
        if row is None:
            return VerifyStatus.PENDING
        return VerifyStatus.from_str(row["verify_status"])
    return _fn


def verify_update(
    conn: sqlite3.Connection,
    project_id: str,
    graph,
    node_ids: list[str],
    target_status: str,
    session: dict,
    evidence_dict: dict = None,
) -> dict:
    """Core verify-update operation.

    Flow:
    1. Permission check (role from session, not body)
    2. Evidence validation
    3. Gate check
    4. State mutation + history + audit
    5. Event publish

    Returns dict with updated_nodes and affected_downstream.
    """
    role = Role.from_str(session["role"])
    scope = session.get("scope", [])
    target = VerifyStatus.from_str(target_status)
    evidence = Evidence.from_dict(evidence_dict or {})
    evidence.producer = session.get("session_id", "")

    # Scope check
    if scope:
        check_nodes_scope(node_ids, scope)

    updated = []
    now = _utc_iso()

    for node_id in node_ids:
        # Verify node exists in graph
        if not graph.has_node(node_id):
            raise NodeNotFoundError(node_id)

        # Get current state
        current = get_node_status(conn, project_id, node_id)
        if current is None:
            raise NodeNotFoundError(node_id)

        from_status = VerifyStatus.from_str(current["verify_status"])
        if from_status == target:
            continue  # No-op, already at target

        # 1. Permission check
        check_transition(from_status, target, role)

        # 2. Evidence validation
        validate_evidence(from_status, target, evidence)

        # 3. Gate check (only for forward transitions)
        gates = graph.get_gates(node_id)
        if gates and target in (VerifyStatus.T2_PASS, VerifyStatus.QA_PASS):
            check_gates_or_raise(node_id, gates, _get_status_fn(conn, project_id))

        # 4. Mutate state
        new_version = current["version"] + 1
        conn.execute(
            """UPDATE node_state
               SET verify_status = ?, evidence_json = ?, updated_by = ?,
                   updated_at = ?, version = ?
               WHERE project_id = ? AND node_id = ? AND version = ?""",
            (
                target.value, evidence.to_json(), session.get("session_id", ""),
                now, new_version,
                project_id, node_id, current["version"],
            ),
        )

        # Check optimistic lock
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            raise ConflictError(details={
                "node_id": node_id,
                "expected_version": current["version"],
            })

        # 5. Write history
        conn.execute(
            """INSERT INTO node_history
               (project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, node_id, from_status.value, target.value,
                role.value, evidence.to_json(), session.get("session_id", ""),
                now, new_version,
            ),
        )

        # 6. Audit
        audit_service.record(
            conn, project_id, "verify_update",
            actor=session.get("principal_id", ""),
            node_ids=[node_id],
            from_status=from_status.value,
            to_status=target.value,
            session_id=session.get("session_id", ""),
        )

        updated.append(node_id)

        # 7. Event
        event_bus.publish("node.status_changed", {
            "project_id": project_id,
            "node_id": node_id,
            "from": from_status.value,
            "to": target.value,
            "role": role.value,
        })

    # Compute downstream impact
    downstream = set()
    for nid in updated:
        downstream |= graph.descendants(nid)

    return {
        "updated_nodes": updated,
        "affected_downstream": sorted(downstream - set(updated)),
        "version": new_version if updated else None,
    }


def release_gate(
    conn: sqlite3.Connection,
    project_id: str,
    graph,
    scope: list[str] = None,
    profile: str = None,
) -> dict:
    """Release gate check. Returns 200 if all green, raises ReleaseBlockedError otherwise."""
    import fnmatch

    all_nodes = graph.list_nodes()
    check_nodes = all_nodes

    # Filter by scope if provided
    if scope:
        check_nodes = [n for n in all_nodes if any(fnmatch.fnmatch(n, p) for p in scope)]

    blockers = []
    summary = {"qa_pass": 0, "t2_pass": 0, "pending": 0, "failed": 0, "waived": 0, "other": 0}

    for node_id in check_nodes:
        state = get_node_status(conn, project_id, node_id)
        status = state["verify_status"] if state else "pending"

        if status in summary:
            summary[status] += 1
        else:
            summary["other"] += 1

        if status not in ("qa_pass", "waived"):
            blockers.append({
                "node_id": node_id,
                "status": status,
                "reason": f"Node is {status}, not qa_pass",
            })

    result = {
        "release": len(blockers) == 0,
        "profile": profile,
        "checked_nodes": len(check_nodes),
        "total_nodes": len(all_nodes),
        "summary": summary,
    }

    if blockers:
        raise ReleaseBlockedError(blockers, summary)

    return result


def get_summary(conn: sqlite3.Connection, project_id: str) -> dict:
    """Get summary statistics."""
    rows = conn.execute(
        "SELECT verify_status, COUNT(*) as cnt FROM node_state WHERE project_id = ? GROUP BY verify_status",
        (project_id,),
    ).fetchall()

    by_status = {row["verify_status"]: row["cnt"] for row in rows}
    total = sum(by_status.values())

    return {
        "project_id": project_id,
        "total_nodes": total,
        "by_status": by_status,
    }


def create_snapshot(conn: sqlite3.Connection, project_id: str, created_by: str = "") -> int:
    """Create a snapshot of current node_state. Returns version number."""
    rows = conn.execute(
        "SELECT * FROM node_state WHERE project_id = ?", (project_id,),
    ).fetchall()

    snapshot = [dict(r) for r in rows]

    # Get next version
    row = conn.execute(
        "SELECT MAX(version) as max_v FROM snapshots WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    next_version = (row["max_v"] or 0) + 1

    conn.execute(
        "INSERT INTO snapshots (project_id, version, snapshot_json, created_at, created_by) VALUES (?, ?, ?, ?, ?)",
        (project_id, next_version, json.dumps(snapshot, ensure_ascii=False), _utc_iso(), created_by),
    )

    return next_version


def rollback(conn: sqlite3.Connection, project_id: str, target_version: int, session: dict) -> dict:
    """Rollback to a specific snapshot version."""
    row = conn.execute(
        "SELECT snapshot_json FROM snapshots WHERE project_id = ? AND version = ?",
        (project_id, target_version),
    ).fetchone()

    if row is None:
        raise ValidationError(f"Snapshot version {target_version} not found")

    snapshot = json.loads(row["snapshot_json"])

    # Get current version for audit
    current_row = conn.execute(
        "SELECT MAX(version) as max_v FROM snapshots WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    current_version = current_row["max_v"] or 0

    # Restore state
    changes = []
    for node_state in snapshot:
        old = get_node_status(conn, project_id, node_state["node_id"])
        if old and old["verify_status"] != node_state["verify_status"]:
            changes.append({
                "node_id": node_state["node_id"],
                "from": old["verify_status"],
                "to": node_state["verify_status"],
            })

        conn.execute(
            """INSERT OR REPLACE INTO node_state
               (project_id, node_id, verify_status, build_status, evidence_json,
                updated_by, updated_at, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, node_state["node_id"],
                node_state["verify_status"], node_state.get("build_status", "impl:missing"),
                node_state.get("evidence_json"), session.get("session_id", ""),
                _utc_iso(), node_state.get("version", 1),
            ),
        )

    audit_service.record(
        conn, project_id, "rollback",
        actor=session.get("principal_id", ""),
        from_version=current_version, to_version=target_version,
        nodes_affected=len(changes),
    )

    event_bus.publish("rollback.executed", {
        "project_id": project_id,
        "from_version": current_version,
        "to_version": target_version,
    })

    return {
        "rolled_back_from": current_version,
        "rolled_back_to": target_version,
        "nodes_affected": len(changes),
        "changes": changes,
    }
