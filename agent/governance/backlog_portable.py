"""Portable backlog export/import helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from . import graph_snapshot_store as store


BACKLOG_EXPORT_SCHEMA = "aming-claw.backlog.export"
BACKLOG_EXPORT_SCHEMA_VERSION = 1

BACKLOG_COLUMNS = [
    "bug_id",
    "title",
    "status",
    "priority",
    "target_files",
    "test_files",
    "acceptance_criteria",
    "chain_task_id",
    "commit",
    "discovered_at",
    "fixed_at",
    "details_md",
    "chain_trigger_json",
    "required_docs",
    "provenance_paths",
    "chain_stage",
    "last_failure_reason",
    "stage_updated_at",
    "runtime_state",
    "current_task_id",
    "root_task_id",
    "worktree_path",
    "worktree_branch",
    "bypass_policy_json",
    "mf_type",
    "takeover_json",
    "runtime_updated_at",
    "created_at",
    "updated_at",
]

JSON_LIST_COLUMNS = {
    "target_files",
    "test_files",
    "acceptance_criteria",
    "required_docs",
    "provenance_paths",
}

JSON_OBJECT_COLUMNS = {
    "chain_trigger_json",
    "bypass_policy_json",
    "takeover_json",
}

DEFAULTS = {
    "title": "",
    "status": "OPEN",
    "priority": "P3",
    "target_files": [],
    "test_files": [],
    "acceptance_criteria": [],
    "chain_task_id": "",
    "commit": "",
    "discovered_at": "",
    "fixed_at": "",
    "details_md": "",
    "chain_trigger_json": {},
    "required_docs": [],
    "provenance_paths": [],
    "chain_stage": "",
    "last_failure_reason": "",
    "stage_updated_at": "",
    "runtime_state": "",
    "current_task_id": "",
    "root_task_id": "",
    "worktree_path": "",
    "worktree_branch": "",
    "bypass_policy_json": {},
    "mf_type": "",
    "takeover_json": {},
    "runtime_updated_at": "",
}


def export_backlog_portable(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    status: str = "",
    priority: str = "",
    bug_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Export backlog_bugs rows as a portable JSON payload."""
    columns = _available_backlog_columns(conn)
    selected_bug_ids = [str(item).strip() for item in (bug_ids or []) if str(item).strip()]

    sql = f"SELECT {', '.join(_quote_identifier(col) for col in columns)} FROM backlog_bugs WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(str(status))
    if priority:
        sql += " AND priority = ?"
        params.append(str(priority))
    if selected_bug_ids:
        placeholders = ", ".join("?" for _ in selected_bug_ids)
        sql += f" AND bug_id IN ({placeholders})"
        params.extend(selected_bug_ids)
    if "created_at" in columns:
        sql += " ORDER BY created_at DESC, bug_id ASC"
    else:
        sql += " ORDER BY bug_id ASC"

    rows = conn.execute(sql, params).fetchall()
    portable_rows = [normalize_backlog_row_for_export(row, columns=columns) for row in rows]
    exported_at = store.utc_now()
    return {
        "schema": BACKLOG_EXPORT_SCHEMA,
        "schema_version": BACKLOG_EXPORT_SCHEMA_VERSION,
        "project_id": project_id,
        "exported_at": exported_at,
        "filters": {
            "status": status,
            "priority": priority,
            "bug_ids": selected_bug_ids,
        },
        "row_count": len(portable_rows),
        "rows": portable_rows,
    }


def import_backlog_portable(
    conn: sqlite3.Connection,
    project_id: str,
    payload: dict[str, Any],
    *,
    on_conflict: str = "skip",
    dry_run: bool = False,
    actor: str = "backlog_import",
) -> dict[str, Any]:
    """Import a portable backlog payload into the current project's DB."""
    strategy = str(on_conflict or "skip").strip().lower()
    if strategy not in {"skip", "overwrite", "fail"}:
        raise ValueError("on_conflict must be one of: skip, overwrite, fail")

    rows = _portable_rows(payload)
    columns = _available_backlog_columns(conn)
    now = store.utc_now()

    inserted: list[str] = []
    updated: list[str] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    prepared: list[tuple[str, bool, dict[str, str]]] = []

    for idx, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            errors.append({"index": str(idx), "bug_id": "", "error": "row must be an object"})
            continue
        bug_id = str(raw_row.get("bug_id") or "").strip()
        if not bug_id:
            errors.append({"index": str(idx), "bug_id": "", "error": "bug_id is required"})
            continue
        if bug_id in seen:
            errors.append({"index": str(idx), "bug_id": bug_id, "error": "duplicate bug_id in payload"})
            continue
        seen.add(bug_id)

        exists = _bug_exists(conn, bug_id)
        if exists and strategy == "skip":
            skipped.append({"bug_id": bug_id, "reason": "exists"})
            continue
        if exists and strategy == "fail":
            errors.append({"index": str(idx), "bug_id": bug_id, "error": "bug_id already exists"})
            continue

        db_row = _portable_row_to_db(raw_row, columns=columns, now=now)
        prepared.append((bug_id, exists, db_row))

    if strategy == "fail" and errors:
        return _import_result(
            project_id,
            payload,
            on_conflict=strategy,
            dry_run=dry_run,
            actor=actor,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            errors=errors,
        )

    for bug_id, exists, db_row in prepared:
        if exists:
            if not dry_run:
                _update_backlog_row(conn, bug_id, db_row)
            updated.append(bug_id)
        else:
            if not dry_run:
                _insert_backlog_row(conn, db_row)
            inserted.append(bug_id)

    if not dry_run and (inserted or updated):
        conn.commit()

    return _import_result(
        project_id,
        payload,
        on_conflict=strategy,
        dry_run=dry_run,
        actor=actor,
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        errors=errors,
    )


def normalize_backlog_row_for_export(row: sqlite3.Row | dict[str, Any], *, columns: list[str] | None = None) -> dict[str, Any]:
    """Convert a backlog DB row to structured portable JSON fields."""
    selected_columns = columns or list(BACKLOG_COLUMNS)
    out: dict[str, Any] = {}
    for col in selected_columns:
        value = _row_value(row, col)
        if col in JSON_LIST_COLUMNS:
            out[col] = _json_list(value)
        elif col in JSON_OBJECT_COLUMNS:
            out[col] = _json_object(value)
        else:
            out[col] = "" if value is None else str(value)
    return out


def _available_backlog_columns(conn: sqlite3.Connection) -> list[str]:
    info = conn.execute("PRAGMA table_info(backlog_bugs)").fetchall()
    names = {_row_value(row, "name") or row[1] for row in info}
    columns = [col for col in BACKLOG_COLUMNS if col in names]
    if "bug_id" not in columns:
        raise sqlite3.OperationalError("backlog_bugs table is missing bug_id column")
    return columns


def _portable_rows(payload: dict[str, Any]) -> list[Any]:
    if not isinstance(payload, dict):
        raise ValueError("backlog import payload must be a JSON object")
    schema = str(payload.get("schema") or "")
    if schema and schema != BACKLOG_EXPORT_SCHEMA:
        raise ValueError(f"unsupported backlog export schema: {schema}")
    version = payload.get("schema_version", BACKLOG_EXPORT_SCHEMA_VERSION)
    try:
        version_int = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_version must be an integer") from exc
    if version_int > BACKLOG_EXPORT_SCHEMA_VERSION:
        raise ValueError(f"unsupported backlog export schema_version: {version_int}")
    rows = payload.get("rows")
    if rows is None and isinstance(payload.get("bugs"), list):
        rows = payload.get("bugs")
    if not isinstance(rows, list):
        raise ValueError("backlog import payload must contain rows[]")
    return rows


def _portable_row_to_db(row: dict[str, Any], *, columns: list[str], now: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for col in columns:
        if col == "bug_id":
            value = str(row.get("bug_id") or "").strip()
        else:
            value = row.get(col, DEFAULTS.get(col, ""))
        if col in JSON_LIST_COLUMNS:
            out[col] = json.dumps(_json_list(value), ensure_ascii=False)
        elif col in JSON_OBJECT_COLUMNS:
            out[col] = json.dumps(_json_object(value), ensure_ascii=False, sort_keys=True)
        elif col in {"created_at", "updated_at"}:
            out[col] = str(value or now)
        else:
            out[col] = "" if value is None else str(value)
    return out


def _insert_backlog_row(conn: sqlite3.Connection, row: dict[str, str]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO backlog_bugs ({', '.join(_quote_identifier(col) for col in columns)}) "
        f"VALUES ({placeholders})"
    )
    conn.execute(sql, [row[col] for col in columns])


def _update_backlog_row(conn: sqlite3.Connection, bug_id: str, row: dict[str, str]) -> None:
    columns = [col for col in row if col != "bug_id"]
    assignments = ", ".join(f"{_quote_identifier(col)} = ?" for col in columns)
    sql = f"UPDATE backlog_bugs SET {assignments} WHERE bug_id = ?"
    conn.execute(sql, [row[col] for col in columns] + [bug_id])


def _bug_exists(conn: sqlite3.Connection, bug_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM backlog_bugs WHERE bug_id = ? LIMIT 1", (bug_id,)).fetchone()
    return row is not None


def _import_result(
    project_id: str,
    payload: dict[str, Any],
    *,
    on_conflict: str,
    dry_run: bool,
    actor: str,
    inserted: list[str],
    updated: list[str],
    skipped: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "ok": not errors,
        "project_id": project_id,
        "source_project_id": str(payload.get("project_id") or ""),
        "schema": str(payload.get("schema") or BACKLOG_EXPORT_SCHEMA),
        "schema_version": int(payload.get("schema_version") or BACKLOG_EXPORT_SCHEMA_VERSION),
        "imported_at": store.utc_now(),
        "actor": actor,
        "on_conflict": on_conflict,
        "dry_run": bool(dry_run),
        "input_count": len(payload.get("rows") or payload.get("bugs") or []),
        "inserted_count": len(inserted),
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "inserted_bug_ids": inserted,
        "updated_bug_ids": updated,
        "skipped": skipped,
        "errors": errors,
    }


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_value(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _quote_identifier(name: str) -> str:
    if name not in BACKLOG_COLUMNS:
        raise ValueError(f"unexpected backlog column: {name}")
    return '"' + name.replace('"', '""') + '"'
