"""Task Registry — SQLite-backed task lifecycle management (v5).

Dual-field state model:
  execution_status: queued → claimed → running → succeeded/failed/timed_out/cancelled
  notification_status: none → pending → sent → read

Supports: retry, priority, assignment, fencing token, progress heartbeat.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid

log = logging.getLogger(__name__)

EXECUTION_STATUSES = {
    "queued", "claimed", "running", "waiting_human", "blocked",
    "succeeded", "failed", "cancelled", "timed_out", "enqueue_failed",
    "design_mismatch", "observer_hold",
}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out", "design_mismatch"}

NOTIFICATION_STATUSES = {"none", "pending", "sent", "read"}

# Backward compat
VALID_STATUSES = EXECUTION_STATUSES


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_task_id() -> str:
    return f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _utc_iso_after(seconds: int) -> str:
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_observer_mode(conn: sqlite3.Connection, project_id: str) -> bool:
    """Return True if observer_mode is enabled for this project."""
    try:
        row = conn.execute(
            "SELECT observer_mode FROM project_version WHERE project_id = ?", (project_id,)
        ).fetchone()
        return bool(row and row["observer_mode"])
    except Exception:
        return False


def create_task(
    conn: sqlite3.Connection,
    project_id: str,
    prompt: str,
    task_type: str = "task",
    related_nodes: list[str] = None,
    created_by: str = "",
    priority: int = 0,
    max_attempts: int = 3,
    metadata: dict = None,
    parent_task_id: str = None,
    retry_round: int = 0,
) -> dict:
    """Create a new task. If observer_mode is on, task starts as observer_hold."""
    task_id = _new_task_id()
    now = _utc_iso()

    # Auto-store original prompt for retry context recovery
    metadata = metadata or {}
    if "_original_prompt" not in metadata:
        metadata["_original_prompt"] = prompt

    # Observer mode: new tasks start held, not queued
    initial_status = "observer_hold" if _is_observer_mode(conn, project_id) else "queued"

    notify = "pending" if metadata.get("chat_id") else "none"
    conn.execute(
        """INSERT INTO tasks
           (task_id, project_id, status, execution_status, notification_status,
            type, prompt, related_nodes,
            created_by, created_at, updated_at, priority, max_attempts, metadata_json,
            parent_task_id, retry_round)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id, project_id, initial_status, initial_status, notify,
            task_type, prompt,
            json.dumps(related_nodes or []),
            created_by, now, now, priority, max_attempts,
            json.dumps(metadata or {}),
            parent_task_id, retry_round,
        ),
    )

    log.info("Task created: %s (project: %s, type: %s, status: %s, retry_round: %d)",
             task_id, project_id, task_type, initial_status, retry_round)
    return {
        "task_id": task_id,
        "project_id": project_id,
        "status": initial_status,
        "type": task_type,
        "created_at": now,
        "observer_hold": initial_status == "observer_hold",
    }


def claim_task(
    conn: sqlite3.Connection,
    project_id: str,
    assigned_to: str,
    worker_id: str = "",
) -> tuple[dict, str] | tuple[None, str]:
    """Claim the next available task with fencing token.

    Returns (task_dict, fence_token) or (None, "") if no tasks.
    """
    now = _utc_iso()
    row = conn.execute(
        """SELECT task_id, type, prompt, related_nodes, priority, attempt_count, max_attempts, metadata_json
           FROM tasks
           WHERE project_id = ? AND execution_status IN ('queued')
           ORDER BY priority DESC, created_at ASC
           LIMIT 1""",
        (project_id,),
    ).fetchone()

    if not row:
        return None, ""

    task_id = row["task_id"]
    attempt_num = row["attempt_count"] + 1
    fence_token = f"fence-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    lease_expires = _utc_iso_after(300)  # 5 min lease

    # CAS update: only queued → claimed
    result = conn.execute(
        """UPDATE tasks SET status = 'claimed', execution_status = 'claimed',
           assigned_to = ?, started_at = ?, updated_at = ?, attempt_count = ?,
           metadata_json = json_set(COALESCE(metadata_json, '{}'),
             '$.fence_token', ?,
             '$.lease_owner', ?,
             '$.lease_expires_at', ?,
             '$.worker_pid', ?
           )
           WHERE task_id = ? AND execution_status IN ('queued')""",
        (assigned_to, now, now, attempt_num,
         fence_token, worker_id or assigned_to, lease_expires, str(os.getpid()),
         task_id),
    )
    if result.rowcount == 0:
        return None, ""  # Already claimed by another worker

    conn.execute(
        """INSERT INTO task_attempts (task_id, attempt_num, status, started_at)
           VALUES (?, ?, 'running', ?)""",
        (task_id, attempt_num, now),
    )

    log.info("task.claimed: %s type=%s by=%s attempt=%d fence=%s",
             task_id, row["type"], worker_id or assigned_to, attempt_num, fence_token)
    return {
        "task_id": task_id,
        "type": row["type"],
        "prompt": row["prompt"],
        "related_nodes": json.loads(row["related_nodes"] or "[]"),
        "priority": row["priority"],
        "attempt_num": attempt_num,
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }, fence_token


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    status: str = "succeeded",
    result: dict = None,
    error_message: str = "",
    fence_token: str = "",
    project_id: str = "",
    completed_by: str = "",
    override_reason: str = "",
) -> dict:
    """Mark a task as completed (succeeded/failed). Dual-field update."""
    if status not in ("succeeded", "failed", "timed_out"):
        from .errors import ValidationError
        raise ValidationError(f"Invalid completion status: {status}")

    now = _utc_iso()
    row = conn.execute(
        "SELECT attempt_count, max_attempts, notification_status, metadata_json, assigned_to FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()

    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)

    # M1: Ownership check — only assignee or observer can complete
    assigned_to = row["assigned_to"] or ""
    if completed_by and assigned_to and completed_by != assigned_to:
        is_observer = completed_by.startswith("observer")
        if not is_observer:
            from .errors import GovernanceError
            raise GovernanceError(
                f"Ownership violation: task assigned to {assigned_to}, "
                f"completed_by {completed_by}", 403)
        # M2: Observer override — allow but audit + warn
        log.warning("task_registry: observer override: %s completing task %s "
                     "assigned to %s (reason: %s)",
                     completed_by, task_id, assigned_to,
                     override_reason or "not provided")
        try:
            from . import event_bus, audit_service
            event_bus.publish("task.observer_override", {
                "project_id": project_id,
                "task_id": task_id,
                "assigned_to": assigned_to,
                "override_by": completed_by,
                "override_reason": override_reason,
            })
            audit_service.record(
                conn, project_id, "task.observer_override",
                actor=completed_by,
                details={
                    "task_id": task_id,
                    "assigned_to": assigned_to,
                    "override_reason": override_reason,
                },
            )
        except Exception:
            pass  # audit failure should not block completion

    # Fence token check (if provided)
    if fence_token:
        stored_fence = json.loads(row["metadata_json"] or "{}").get("fence_token", "")
        if stored_fence and stored_fence != fence_token:
            from .errors import GovernanceError
            raise GovernanceError("Fence token mismatch: task reclaimed by another worker", 409)

    # Determine execution status
    exec_status = status
    if status == "failed" and row["attempt_count"] < row["max_attempts"]:
        if _is_observer_mode(conn, project_id):
            exec_status = "observer_hold"  # Auto-retry but hold for observer
        else:
            exec_status = "queued"  # Auto-retry

    # Determine notification status
    notify_status = row["notification_status"]
    if exec_status in TERMINAL_STATUSES and notify_status == "none":
        # Has chat_id → needs notification
        meta = json.loads(row["metadata_json"] or "{}")
        if meta.get("chat_id"):
            # If executor already sent the reply directly (coordinator flow),
            # mark as "sent" to prevent gateway from sending a duplicate.
            if (result or {}).get("_reply_sent"):
                notify_status = "sent"
            else:
                notify_status = "pending"

    conn.execute(
        """UPDATE tasks SET status = ?, execution_status = ?,
           notification_status = ?,
           completed_at = ?, updated_at = ?,
           result_json = ?, error_message = ?
           WHERE task_id = ?""",
        (exec_status, exec_status, notify_status,
         now, now,
         json.dumps(result or {}, ensure_ascii=False), error_message,
         task_id),
    )

    # Update attempt
    conn.execute(
        """UPDATE task_attempts SET status = ?, completed_at = ?,
           result_json = ?, error_message = ?
           WHERE task_id = ? AND status = 'running'""",
        (status, now, json.dumps(result or {}, ensure_ascii=False),
         error_message, task_id),
    )

    result_summary = str(result)[:200] if result else "{}"
    log.info("task.complete: %s status=%s exec_status=%s by=%s result=%s",
             task_id, status, exec_status, completed_by or assigned_to, result_summary)

    response = {
        "task_id": task_id,
        "status": exec_status,
        "retrying": exec_status == "queued",
        "completed_at": now,
    }

    # Auto-chain: dispatch next stage asynchronously on success/failure.
    # Non-chain types (task, coordinator) are ignored by auto_chain.CHAIN
    # so they pass through without spawning a thread.
    # Cancelled: terminal, no auto-chain, no retry.
    if exec_status == "cancelled":
        pass  # skip auto-chain
    elif status == "failed" and project_id:
        meta = json.loads(row["metadata_json"] or "{}")
        type_row = conn.execute(
            "SELECT type FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        task_type = type_row["type"] if type_row else "task"
        conn.commit()
        response["auto_chain"] = {"dispatched": True}
        _dispatch_auto_chain_failed(
            project_id, task_id, task_type,
            result or {}, meta,
            error_message or (result or {}).get("error", ""),
        )
    elif exec_status == "succeeded" and project_id:
        meta = json.loads(row["metadata_json"] or "{}")
        type_row = conn.execute(
            "SELECT type FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        task_type = type_row["type"] if type_row else "task"
        # Commit before auto_chain opens its independent connection.
        # Without this, the caller's open transaction holds a write lock and
        # auto_chain's separate conn fails with "database is locked".
        conn.commit()
        response["auto_chain"] = {"dispatched": True}
        _dispatch_auto_chain_success(
            project_id, task_id, task_type,
            exec_status, result or {}, meta,
        )

    return response


# ---------------------------------------------------------------------------
# Background auto-chain dispatch helpers
# ---------------------------------------------------------------------------

def _dispatch_auto_chain_success(
    project_id: str,
    task_id: str,
    task_type: str,
    exec_status: str,
    result: dict,
    metadata: dict,
) -> None:
    """Fire auto_chain.on_task_completed in a background thread.

    Errors are logged (not swallowed) so they appear in service logs.
    """
    def _run():
        try:
            from . import auto_chain
            from .db import get_connection
            conn = get_connection(project_id)
            try:
                auto_chain.on_task_completed(
                    conn, project_id, task_id,
                    task_type=task_type,
                    status=exec_status,
                    result=result,
                    metadata=metadata,
                )
            finally:
                conn.close()
        except Exception:
            log.error(
                "auto_chain.on_task_completed failed for task %s (project=%s, type=%s)",
                task_id, project_id, task_type,
                exc_info=True,
            )

    t = threading.Thread(target=_run, name=f"auto-chain-{task_id}", daemon=True)
    t.start()


def _dispatch_auto_chain_failed(
    project_id: str,
    task_id: str,
    task_type: str,
    result: dict,
    metadata: dict,
    reason: str,
) -> None:
    """Fire auto_chain.on_task_failed in a background thread.

    Errors are logged (not swallowed) so they appear in service logs.
    """
    def _run():
        try:
            from . import auto_chain
            from .db import get_connection
            conn = get_connection(project_id)
            try:
                auto_chain.on_task_failed(
                    conn, project_id, task_id,
                    task_type=task_type,
                    result=result,
                    metadata=metadata,
                    reason=reason,
                )
            finally:
                conn.close()
        except Exception:
            log.error(
                "auto_chain.on_task_failed failed for task %s (project=%s, type=%s)",
                task_id, project_id, task_type,
                exc_info=True,
            )

    t = threading.Thread(target=_run, name=f"auto-chain-fail-{task_id}", daemon=True)
    t.start()


def hold_task(conn: sqlite3.Connection, task_id: str) -> dict:
    """Put a queued task into observer_hold — pauses auto-chain and executor pickup."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET status = 'observer_hold', execution_status = 'observer_hold',
           updated_at = ? WHERE task_id = ? AND execution_status = 'queued'""",
        (now, task_id),
    )
    if conn.total_changes == 0:
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        current = row["execution_status"] if row else "not found"
        raise ValueError(f"Task {task_id} cannot be held (current status: {current})")
    log.info("Task held by observer: %s", task_id)
    return {"task_id": task_id, "status": "observer_hold"}


def release_task(conn: sqlite3.Connection, task_id: str) -> dict:
    """Release an observer_hold task back to queued — resumes auto-chain and executor."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET status = 'queued', execution_status = 'queued',
           updated_at = ? WHERE task_id = ? AND execution_status = 'observer_hold'""",
        (now, task_id),
    )
    if conn.total_changes == 0:
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        current = row["execution_status"] if row else "not found"
        raise ValueError(f"Task {task_id} cannot be released (current status: {current})")
    log.info("Task released by observer: %s", task_id)
    return {"task_id": task_id, "status": "queued"}


def set_observer_mode(conn: sqlite3.Connection, project_id: str, enabled: bool) -> dict:
    """Enable or disable observer_mode for a project."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """UPDATE project_version SET observer_mode = ?, updated_at = ?
           WHERE project_id = ?""",
        (1 if enabled else 0, now, project_id),
    )
    log.info("Observer mode %s for project %s", "enabled" if enabled else "disabled", project_id)
    return {"project_id": project_id, "observer_mode": enabled}


def get_observer_mode(conn: sqlite3.Connection, project_id: str) -> bool:
    """Return current observer_mode flag for a project."""
    return _is_observer_mode(conn, project_id)


def cancel_task(conn: sqlite3.Connection, task_id: str, reason: str = "") -> dict:
    """Cancel a task. Terminal state — no auto-chain, no retry."""
    now = _utc_iso()
    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)
    conn.execute(
        """UPDATE tasks SET status = 'cancelled', execution_status = 'cancelled',
           completed_at = ?, updated_at = ?, error_message = ?
           WHERE task_id = ?""",
        (now, now, reason or "Cancelled by observer", task_id),
    )
    log.info("task.cancelled: %s reason=%s", task_id, reason or "observer")
    return {"task_id": task_id, "status": "cancelled"}


def mark_notified(conn: sqlite3.Connection, task_id: str) -> dict:
    """Mark a task's notification as sent."""
    now = _utc_iso()
    conn.execute(
        "UPDATE tasks SET notification_status = 'sent', notified_at = ? WHERE task_id = ?",
        (now, task_id),
    )
    return {"task_id": task_id, "notification_status": "sent"}


def list_pending_notifications(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """List tasks that need notification (execution done but user not notified)."""
    rows = conn.execute(
        """SELECT task_id, execution_status, result_json, error_message,
                  completed_at, metadata_json
           FROM tasks
           WHERE project_id = ? AND notification_status = 'pending'
             AND execution_status IN ('succeeded', 'failed', 'timed_out', 'cancelled')
           ORDER BY completed_at ASC""",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_progress(conn: sqlite3.Connection, task_id: str,
                    phase: str, percent: int, message: str) -> dict:
    """Update task progress heartbeat."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET
           execution_status = 'running',
           updated_at = ?,
           metadata_json = json_set(COALESCE(metadata_json, '{}'),
             '$.progress_phase', ?,
             '$.progress_percent', ?,
             '$.progress_message', ?,
             '$.progress_at', ?,
             '$.lease_expires_at', ?
           )
           WHERE task_id = ? AND execution_status IN ('claimed', 'running')""",
        (now, phase, percent, message, now, _utc_iso_after(300), task_id),
    )
    return {"task_id": task_id, "phase": phase, "percent": percent}


def recover_stale_tasks(conn: sqlite3.Connection, project_id: str) -> dict:
    """Recover tasks with expired leases — re-queue them."""
    now = _utc_iso()
    rows = conn.execute(
        """SELECT task_id FROM tasks
           WHERE project_id = ? AND execution_status IN ('claimed', 'running')
             AND json_extract(metadata_json, '$.lease_expires_at') < ?""",
        (project_id, now),
    ).fetchall()

    recovered = 0
    for row in rows:
        conn.execute(
            "UPDATE tasks SET execution_status = 'queued', status = 'queued' WHERE task_id = ?",
            (row["task_id"],),
        )
        recovered += 1
        log.info("Recovered stale task: %s", row["task_id"])

    return {"recovered": recovered}


def list_tasks(
    conn: sqlite3.Connection,
    project_id: str,
    status: str = None,
    limit: int = 50,
) -> list[dict]:
    """List tasks for a project."""
    cols = """task_id, status, type, prompt, assigned_to, created_by,
                      created_at, updated_at, attempt_count, priority,
                      result_json, metadata_json"""
    if status:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM tasks WHERE project_id = ? AND status = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM tasks WHERE project_id = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields for API consumers
        for field in ("result_json", "metadata_json"):
            raw = d.get(field)
            if raw and isinstance(raw, str):
                try:
                    d[field.replace("_json", "")] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d[field.replace("_json", "")] = raw
            else:
                d[field.replace("_json", "")] = raw
        results.append(d)
    return results


def get_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """Get a single task with attempts."""
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return None

    task = dict(row)
    attempts = conn.execute(
        "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_num",
        (task_id,),
    ).fetchall()
    task["attempts"] = [dict(a) for a in attempts]
    return task


def escalate_task(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Escalate a task via QA→Dev retry loop (max 3 rounds).

    - retry_round < 3: increment retry_round, create a child task with parent linkage,
      return new task_id.
    - retry_round >= 3: mark task as design_mismatch, log user notification, return None.
    """
    row = conn.execute(
        """SELECT project_id, type, prompt, related_nodes, created_by, priority,
                  max_attempts, metadata_json, retry_round
           FROM tasks WHERE task_id = ?""",
        (task_id,),
    ).fetchone()

    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)

    retry_round = row["retry_round"] or 0

    if retry_round < 3:
        new_round = retry_round + 1
        result = create_task(
            conn,
            project_id=row["project_id"],
            prompt=row["prompt"],
            task_type=row["type"],
            related_nodes=json.loads(row["related_nodes"] or "[]"),
            created_by=row["created_by"] or "",
            priority=row["priority"],
            max_attempts=row["max_attempts"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            parent_task_id=task_id,
            retry_round=new_round,
        )
        log.info(
            "Escalated task %s → %s (retry_round=%d)",
            task_id, result["task_id"], new_round,
        )
        return result["task_id"]
    else:
        now = _utc_iso()
        conn.execute(
            """UPDATE tasks SET status = 'design_mismatch', execution_status = 'design_mismatch',
               updated_at = ? WHERE task_id = ?""",
            (now, task_id),
        )
        log.warning(
            "Task %s reached max escalation (retry_round=%d) — marked design_mismatch. "
            "Manual intervention required.",
            task_id, retry_round,
        )
        return None
