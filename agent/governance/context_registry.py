"""Role-scoped context pack registry.

Context packs are local governance inputs for observer/worker prompt assembly.
They are deliberately separate from source-controlled skills: skills are the
public operating contract, while context packs can be local, private, and
role-scoped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid


VISIBILITY_PUBLIC_SKILL = "public_skill"
VISIBILITY_INTERNAL_PRODUCT = "internal_product"
VISIBILITY_TASK_CONTEXT = "task_context"
VISIBILITY_PRIVATE_FOUNDER = "private_founder"

VALID_VISIBILITIES = {
    VISIBILITY_PUBLIC_SKILL,
    VISIBILITY_INTERNAL_PRODUCT,
    VISIBILITY_TASK_CONTEXT,
    VISIBILITY_PRIVATE_FOUNDER,
}

OBSERVER_ROLES = {"observer"}
FALLBACK_PACK_ID = "observer_safe_expertise_routing.v1"
FALLBACK_DOC_REL_PATH = "skills/aming-claw/references/observer-context-safe.md"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS context_packs (
    pack_id              TEXT NOT NULL,
    project_id           TEXT NOT NULL,
    project_scope        TEXT NOT NULL DEFAULT '',
    title                TEXT NOT NULL DEFAULT '',
    visibility           TEXT NOT NULL DEFAULT 'public_skill',
    allowed_roles_json   TEXT NOT NULL DEFAULT '[]',
    mode_scope_json      TEXT NOT NULL DEFAULT '[]',
    backlog_id           TEXT NOT NULL DEFAULT '',
    source_type          TEXT NOT NULL DEFAULT 'local_db',
    source_path          TEXT NOT NULL DEFAULT '',
    summary              TEXT NOT NULL DEFAULT '',
    body                 TEXT NOT NULL DEFAULT '',
    version              TEXT NOT NULL DEFAULT 'v1',
    content_hash         TEXT NOT NULL DEFAULT '',
    no_export            INTEGER NOT NULL DEFAULT 0,
    enabled              INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    created_by           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, pack_id)
);
CREATE INDEX IF NOT EXISTS idx_context_packs_project_visibility
    ON context_packs(project_id, visibility, enabled);
CREATE INDEX IF NOT EXISTS idx_context_packs_backlog
    ON context_packs(project_id, backlog_id);

CREATE TABLE IF NOT EXISTS context_resolution_events (
    resolution_id        TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT '',
    mode                 TEXT NOT NULL DEFAULT '',
    backlog_id           TEXT NOT NULL DEFAULT '',
    requested_by         TEXT NOT NULL DEFAULT '',
    selected_packs_json  TEXT NOT NULL DEFAULT '[]',
    blocked_packs_json   TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_resolution_project_created
    ON context_resolution_events(project_id, created_at);
"""


class ContextRegistryError(ValueError):
    """Raised for invalid context registry operations."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_list(value: Any, *, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        return [value] if value else list(default or [])
    if not isinstance(value, Iterable) or isinstance(value, (bytes, dict)):
        raise ContextRegistryError("list field must be a string or list of strings")
    result = [str(item).strip() for item in value if str(item).strip()]
    return result or list(default or [])


def _json_loads_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item)]


def _normalize_visibility(value: Any) -> str:
    visibility = str(value or VISIBILITY_PUBLIC_SKILL).strip()
    if visibility not in VALID_VISIBILITIES:
        raise ContextRegistryError(
            "visibility must be one of: " + ", ".join(sorted(VALID_VISIBILITIES))
        )
    return visibility


def _normalize_enabled(value: Any, *, default: bool = True) -> int:
    if value is None:
        return 1 if default else 0
    return 1 if bool(value) else 0


def _validate_role_boundary(*, visibility: str, allowed_roles: list[str], no_export: bool) -> tuple[list[str], bool]:
    if visibility != VISIBILITY_PRIVATE_FOUNDER:
        return allowed_roles or ["*"], bool(no_export)

    roles = allowed_roles or ["observer"]
    if "*" in roles or any(role not in OBSERVER_ROLES for role in roles):
        raise ContextRegistryError("private_founder context packs are observer-only in V1")
    return roles, True


def compute_content_hash(pack: Mapping[str, Any]) -> str:
    payload = {
        "pack_id": str(pack.get("pack_id") or ""),
        "title": str(pack.get("title") or ""),
        "visibility": str(pack.get("visibility") or ""),
        "summary": str(pack.get("summary") or ""),
        "body": str(pack.get("body") or ""),
        "version": str(pack.get("version") or ""),
        "source_type": str(pack.get("source_type") or ""),
    }
    return "sha256:" + sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def upsert_context_pack(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    pack_id: str,
    title: str = "",
    visibility: str = VISIBILITY_PUBLIC_SKILL,
    allowed_roles: list[str] | str | None = None,
    mode_scope: list[str] | str | None = None,
    project_scope: str = "",
    backlog_id: str = "",
    source_type: str = "local_db",
    source_path: str = "",
    summary: str = "",
    body: str = "",
    version: str = "v1",
    no_export: bool | None = None,
    enabled: bool | None = True,
    created_by: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    ident = (pack_id or "").strip()
    if not pid:
        raise ContextRegistryError("project_id is required")
    if not ident:
        raise ContextRegistryError("pack_id is required")
    normalized_visibility = _normalize_visibility(visibility)
    roles = _json_list(allowed_roles, default=["observer"] if normalized_visibility == VISIBILITY_PRIVATE_FOUNDER else ["*"])
    modes = _json_list(mode_scope, default=["*"])
    roles, normalized_no_export = _validate_role_boundary(
        visibility=normalized_visibility,
        allowed_roles=roles,
        no_export=bool(no_export),
    )
    pack = {
        "pack_id": ident,
        "project_id": pid,
        "project_scope": (project_scope or pid).strip(),
        "title": (title or ident).strip(),
        "visibility": normalized_visibility,
        "allowed_roles": roles,
        "mode_scope": modes,
        "backlog_id": (backlog_id or "").strip(),
        "source_type": (source_type or "local_db").strip(),
        "source_path": (source_path or "").strip(),
        "summary": (summary or "").strip(),
        "body": body or "",
        "version": (version or "v1").strip(),
        "no_export": normalized_no_export,
        "enabled": bool(enabled) if enabled is not None else True,
    }
    pack["content_hash"] = compute_content_hash(pack)
    now = _utc_now()

    conn.execute(
        """
        INSERT INTO context_packs (
            pack_id, project_id, project_scope, title, visibility,
            allowed_roles_json, mode_scope_json, backlog_id, source_type,
            source_path, summary, body, version, content_hash, no_export,
            enabled, created_at, updated_at, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, pack_id) DO UPDATE SET
            project_scope=excluded.project_scope,
            title=excluded.title,
            visibility=excluded.visibility,
            allowed_roles_json=excluded.allowed_roles_json,
            mode_scope_json=excluded.mode_scope_json,
            backlog_id=excluded.backlog_id,
            source_type=excluded.source_type,
            source_path=excluded.source_path,
            summary=excluded.summary,
            body=excluded.body,
            version=excluded.version,
            content_hash=excluded.content_hash,
            no_export=excluded.no_export,
            enabled=excluded.enabled,
            updated_at=excluded.updated_at,
            created_by=excluded.created_by
        """,
        (
            ident,
            pid,
            pack["project_scope"],
            pack["title"],
            pack["visibility"],
            _json_dumps(pack["allowed_roles"]),
            _json_dumps(pack["mode_scope"]),
            pack["backlog_id"],
            pack["source_type"],
            pack["source_path"],
            pack["summary"],
            pack["body"],
            pack["version"],
            pack["content_hash"],
            1 if pack["no_export"] else 0,
            1 if pack["enabled"] else 0,
            now,
            now,
            (created_by or "").strip(),
        ),
    )
    conn.commit()
    result = get_context_pack(conn, project_id=pid, pack_id=ident, include_body=True, role="observer")
    assert result is not None
    return result


def seed_private_context_from_file(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_path: str,
    pack_id: str = "private_founder_paradigm.v1",
    title: str = "Private founder judgment context",
    summary: str = "Private observer-only context imported from a local evidence file.",
    created_by: str = "",
) -> dict[str, Any]:
    path = Path(source_path).expanduser()
    if not path.exists() or not path.is_file():
        raise ContextRegistryError(f"source_path does not exist: {source_path}")
    body = path.read_text(encoding="utf-8")
    return upsert_context_pack(
        conn,
        project_id=project_id,
        pack_id=pack_id,
        title=title,
        visibility=VISIBILITY_PRIVATE_FOUNDER,
        allowed_roles=["observer"],
        mode_scope=["*"],
        source_type="private_file",
        source_path=str(path),
        summary=summary,
        body=body,
        version="v1",
        no_export=True,
        enabled=True,
        created_by=created_by,
    )


def _row_to_pack(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else dict(row)
    data["allowed_roles"] = _json_loads_list(data.pop("allowed_roles_json", "[]"))
    data["mode_scope"] = _json_loads_list(data.pop("mode_scope_json", "[]"))
    data["no_export"] = bool(data.get("no_export"))
    data["enabled"] = bool(data.get("enabled"))
    return data


def _can_see_private_body(*, role: str, visibility: str) -> bool:
    return visibility != VISIBILITY_PRIVATE_FOUNDER or role in OBSERVER_ROLES


def _pack_to_dict(pack: Mapping[str, Any], *, include_body: bool, role: str) -> dict[str, Any]:
    visibility = str(pack.get("visibility") or "")
    redacted = not include_body or not _can_see_private_body(role=role, visibility=visibility)
    data = {
        "pack_id": pack.get("pack_id", ""),
        "project_id": pack.get("project_id", ""),
        "project_scope": pack.get("project_scope", ""),
        "title": pack.get("title", ""),
        "visibility": visibility,
        "allowed_roles": list(pack.get("allowed_roles") or []),
        "mode_scope": list(pack.get("mode_scope") or []),
        "backlog_id": pack.get("backlog_id", ""),
        "source_type": pack.get("source_type", ""),
        "source_path": pack.get("source_path", ""),
        "summary": pack.get("summary", ""),
        "version": pack.get("version", ""),
        "content_hash": pack.get("content_hash", ""),
        "no_export": bool(pack.get("no_export")),
        "enabled": bool(pack.get("enabled")),
        "body_redacted": redacted,
    }
    if redacted:
        if visibility == VISIBILITY_PRIVATE_FOUNDER or data["no_export"]:
            data["source_path"] = ""
            data["source_path_redacted"] = True
    else:
        data["body"] = pack.get("body", "")
    return data


def list_context_packs(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    include_body: bool = False,
    role: str = "observer",
    visibility: str | None = None,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    params: list[Any] = [pid, "*"]
    where = ["project_id IN (?, ?)"]
    if visibility:
        where.append("visibility = ?")
        params.append(visibility)
    if enabled_only:
        where.append("enabled = 1")
    rows = conn.execute(
        "SELECT * FROM context_packs WHERE "
        + " AND ".join(where)
        + " ORDER BY updated_at DESC, pack_id ASC",
        params,
    ).fetchall()
    return [
        _pack_to_dict(_row_to_pack(row), include_body=include_body, role=role)
        for row in rows
    ]


def get_context_pack(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    pack_id: str,
    include_body: bool = False,
    role: str = "observer",
) -> dict[str, Any] | None:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    ident = (pack_id or "").strip()
    row = conn.execute(
        """
        SELECT * FROM context_packs
        WHERE pack_id = ? AND project_id IN (?, ?)
        ORDER BY CASE WHEN project_id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (ident, pid, "*", pid),
    ).fetchone()
    if not row:
        return None
    return _pack_to_dict(_row_to_pack(row), include_body=include_body, role=role)


def _role_allowed(pack: Mapping[str, Any], role: str) -> bool:
    roles = set(str(item) for item in pack.get("allowed_roles") or [])
    return "*" in roles or role in roles


def _mode_allowed(pack: Mapping[str, Any], mode: str) -> bool:
    modes = set(str(item) for item in pack.get("mode_scope") or [])
    return not modes or "*" in modes or not mode or mode in modes


def _backlog_allowed(pack: Mapping[str, Any], backlog_id: str) -> bool:
    bound = str(pack.get("backlog_id") or "")
    return not bound or not backlog_id or bound == backlog_id


def _fallback_pack() -> dict[str, Any] | None:
    path = _repo_root() / FALLBACK_DOC_REL_PATH
    if not path.exists():
        return None
    body = path.read_text(encoding="utf-8")
    pack = {
        "pack_id": FALLBACK_PACK_ID,
        "project_id": "*",
        "project_scope": "*",
        "title": "Observer-safe expertise routing",
        "visibility": VISIBILITY_PUBLIC_SKILL,
        "allowed_roles": ["observer"],
        "mode_scope": ["*"],
        "backlog_id": "",
        "source_type": "fallback_doc",
        "source_path": FALLBACK_DOC_REL_PATH,
        "summary": "Observer-safe routing rules for specialized expert review.",
        "body": body,
        "version": "v1",
        "no_export": False,
        "enabled": True,
    }
    pack["content_hash"] = compute_content_hash(pack)
    return pack


def _audit_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pack_id": pack.get("pack_id", ""),
        "version": pack.get("version", ""),
        "visibility": pack.get("visibility", ""),
        "source_type": pack.get("source_type", ""),
        "content_hash": pack.get("content_hash", ""),
        "body_redacted": True,
        "no_export": bool(pack.get("no_export")),
    }


def resolve_context(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    role: str,
    mode: str = "",
    backlog_id: str = "",
    requested_by: str = "",
    include_body: bool = True,
    record_resolution: bool = True,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    role_name = (role or "").strip()
    if not pid:
        raise ContextRegistryError("project_id is required")
    if not role_name:
        raise ContextRegistryError("role is required")

    rows = conn.execute(
        """
        SELECT * FROM context_packs
        WHERE project_id IN (?, ?) AND enabled = 1
        ORDER BY CASE WHEN project_id = ? THEN 0 ELSE 1 END, updated_at DESC, pack_id ASC
        """,
        (pid, "*", pid),
    ).fetchall()
    candidates = [_row_to_pack(row) for row in rows]
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for pack in candidates:
        if pack.get("visibility") == VISIBILITY_PRIVATE_FOUNDER and role_name not in OBSERVER_ROLES:
            blocked.append({**_audit_pack(pack), "reason": "private_founder_observer_only"})
            continue
        if not _role_allowed(pack, role_name):
            continue
        if not _mode_allowed(pack, mode):
            continue
        if not _backlog_allowed(pack, backlog_id):
            continue
        selected.append(pack)

    fallback = _fallback_pack()
    selected_ids = {str(pack.get("pack_id") or "") for pack in selected}
    if (
        fallback is not None
        and fallback["pack_id"] not in selected_ids
        and _role_allowed(fallback, role_name)
        and _mode_allowed(fallback, mode)
    ):
        selected.append(fallback)

    pack_dicts = [
        _pack_to_dict(pack, include_body=include_body, role=role_name)
        for pack in selected
    ]
    resolution_id = "ctxres-" + uuid.uuid4().hex[:12]
    audit_packs = [_audit_pack(pack) for pack in selected]
    if record_resolution:
        conn.execute(
            """
            INSERT INTO context_resolution_events (
                resolution_id, project_id, role, mode, backlog_id,
                requested_by, selected_packs_json, blocked_packs_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolution_id,
                pid,
                role_name,
                mode or "",
                backlog_id or "",
                requested_by or "",
                _json_dumps(audit_packs),
                _json_dumps(blocked),
                _utc_now(),
            ),
        )
        conn.commit()

    context_parts = []
    for pack in pack_dicts:
        text = str(pack.get("body") or pack.get("summary") or "").strip()
        if text:
            context_parts.append(f"## {pack.get('title') or pack.get('pack_id')}\n\n{text}")

    return {
        "ok": True,
        "project_id": pid,
        "role": role_name,
        "mode": mode or "",
        "backlog_id": backlog_id or "",
        "resolution_id": resolution_id,
        "packs": pack_dicts,
        "selected_packs": audit_packs,
        "blocked_packs": blocked,
        "context_text": "\n\n".join(context_parts),
        "count": len(pack_dicts),
    }
