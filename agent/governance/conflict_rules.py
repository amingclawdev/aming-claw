"""Conflict rule engine — zero-token pre-AI decision layer.

Checks queued/claimed tasks and memory for conflicts before creating new tasks.
Returns one of: "new", "duplicate", "conflict", "queue", "block", "retry".

Rules (checked in order):
  1. Same intent hash within 1h → "duplicate"
  2. Same file + opposite operation (add vs delete) → "conflict"
  3. Same module + concurrent refactor → "conflict"
  4. Upstream depends_on task not succeeded → "queue"
  5. Past failure_pattern with followup_needed + same module → "retry"
  6. Otherwise → "new"
"""

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# Operation type keyword extraction (English + Chinese)
OP_KEYWORDS: dict[str, list[str]] = {
    "add": ["添加", "新增", "创建", "实现", "add", "create", "implement", "new"],
    "modify": ["修改", "更新", "优化", "改", "update", "modify", "optimize", "improve"],
    "delete": ["删除", "移除", "去掉", "delete", "remove", "drop"],
    "refactor": ["重构", "重写", "迁移", "refactor", "rewrite", "migrate"],
    "test": ["测试", "验证", "检查", "test", "verify", "check"],
}

# Opposite operations that conflict
OPPOSITE_OPS = {
    ("add", "delete"), ("delete", "add"),
    ("add", "refactor"), ("refactor", "add"),
}


def extract_operation_type(prompt: str) -> str:
    """Extract operation type from prompt text using keyword matching."""
    prompt_lower = prompt.lower()
    for op_type, keywords in OP_KEYWORDS.items():
        for kw in keywords:
            if kw in prompt_lower:
                return op_type
    return "modify"  # Default


def compute_intent_hash(prompt: str) -> str:
    """SHA-256 hash of normalized prompt for duplicate detection."""
    normalized = " ".join(prompt.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def check_conflicts(
    conn: sqlite3.Connection,
    project_id: str,
    target_files: list[str],
    operation_type: str,
    intent_hash: str,
    prompt: str = "",
    depends_on: list[str] = None,
) -> dict:
    """Run conflict rules against active queue and memory.

    Returns:
        {"decision": str, "reason": str, "details": dict}
        decision is one of: "new", "duplicate", "conflict", "queue", "block", "retry"
    """

    # Rule 1: Duplicate detection — same intent hash within 1 hour
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = conn.execute("""
            SELECT task_id, prompt, created_at FROM tasks
            WHERE project_id = ? AND created_at > ?
              AND status IN ('queued', 'claimed', 'succeeded')
        """, (project_id, one_hour_ago)).fetchall()

        for row in rows:
            existing_hash = compute_intent_hash(row["prompt"] or "")
            if existing_hash == intent_hash:
                return {
                    "decision": "duplicate",
                    "reason": f"Similar task created within 1h: {row['task_id']}",
                    "details": {"existing_task_id": row["task_id"], "created_at": row["created_at"]},
                }
    except Exception as e:
        log.warning("Rule 1 (duplicate) check failed: %s", e)

    # Rule 2: Same file + opposite operation
    try:
        active_rows = conn.execute("""
            SELECT task_id, metadata_json, type FROM tasks
            WHERE project_id = ? AND status IN ('queued', 'claimed')
        """, (project_id,)).fetchall()

        import json
        for row in active_rows:
            meta = {}
            if row["metadata_json"]:
                try:
                    meta = json.loads(row["metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            existing_files = set(meta.get("target_files", []))
            existing_op = meta.get("operation_type", "")
            overlapping = set(target_files) & existing_files
            if overlapping and existing_op and (existing_op, operation_type) in OPPOSITE_OPS:
                return {
                    "decision": "conflict",
                    "reason": f"Opposite operations on same files: {overlapping}",
                    "details": {
                        "conflicting_task_id": row["task_id"],
                        "existing_op": existing_op,
                        "new_op": operation_type,
                        "overlapping_files": list(overlapping),
                    },
                }
    except Exception as e:
        log.warning("Rule 2 (opposite op) check failed: %s", e)

    # Rule 3: Same module + concurrent refactor
    try:
        if operation_type == "refactor" and target_files:
            # Extract module from first target file
            module = target_files[0].split("/")[0] if "/" in target_files[0] else ""
            for row in active_rows:
                meta = {}
                if row["metadata_json"]:
                    try:
                        meta = json.loads(row["metadata_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                existing_op = meta.get("operation_type", "")
                existing_files = meta.get("target_files", [])
                if existing_op == "refactor":
                    existing_module = existing_files[0].split("/")[0] if existing_files and "/" in existing_files[0] else ""
                    if module and module == existing_module:
                        return {
                            "decision": "conflict",
                            "reason": f"Concurrent refactor on module '{module}'",
                            "details": {"conflicting_task_id": row["task_id"], "module": module},
                        }
    except Exception as e:
        log.warning("Rule 3 (concurrent refactor) check failed: %s", e)

    # Rule 4: Upstream dependency not succeeded
    if depends_on:
        try:
            for dep_id in depends_on:
                dep_row = conn.execute(
                    "SELECT status FROM tasks WHERE task_id = ?", (dep_id,)
                ).fetchone()
                if dep_row and dep_row["status"] != "succeeded":
                    return {
                        "decision": "queue",
                        "reason": f"Upstream task {dep_id} not yet succeeded (status: {dep_row['status']})",
                        "details": {"blocked_by": dep_id, "dep_status": dep_row["status"]},
                    }
        except Exception as e:
            log.warning("Rule 4 (dependency) check failed: %s", e)

    # Rule 5: Past failure_pattern with same module → suggest retry with context
    try:
        if target_files:
            module = target_files[0].split("/")[0] if "/" in target_files[0] else target_files[0]
            failure_rows = conn.execute("""
                SELECT content, metadata_json FROM memories
                WHERE project_id = ? AND kind = 'failure_pattern' AND status = 'active'
                  AND module_id = ?
                ORDER BY created_at DESC LIMIT 1
            """, (project_id, module)).fetchall()
            if failure_rows:
                return {
                    "decision": "retry",
                    "reason": f"Past failure pattern found for module '{module}'",
                    "details": {"failure_content": failure_rows[0]["content"][:200]},
                }
    except Exception as e:
        log.warning("Rule 5 (failure pattern) check failed: %s", e)

    # Rule 6: No conflicts → new task
    return {
        "decision": "new",
        "reason": "No conflicts detected",
        "details": {},
    }
