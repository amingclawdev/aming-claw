"""Project service — project initialization, isolation, and routing.

Trust chain:
  1. Human calls POST /api/init {project, password} → gets coordinator token (one-time)
  2. Same project re-init → 403 (unless password provided for token reset)
  3. Human gives coordinator token to Coordinator agent
  4. Coordinator uses its token to assign roles to other agents via /api/role/assign
"""

from __future__ import annotations

import json
import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime, timezone

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from utils import tasks_root
from .db import get_connection, _governance_root
from .graph import AcceptanceGraph
from . import state_service
from . import role_service
from . import audit_service
from .errors import ValidationError, AuthError, PermissionDeniedError


def _projects_file() -> Path:
    p = _governance_root() / "projects.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _load_projects() -> dict:
    path = _projects_file()
    if path.exists():
        with open(str(path), "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "projects": {}}


def _save_projects(data: dict):
    path = _projects_file()
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# Project ID normalization
# ============================================================

def _normalize_project_id(raw: str) -> str:
    """Normalize project ID to lowercase kebab-case.
    Delegates to shared utility in utils.py.
    """
    # Import from shared utils to avoid duplication.
    # Uses try/except for Docker context where utils may not be on path.
    try:
        from utils import normalize_project_id
        return normalize_project_id(raw)
    except ImportError:
        pass
    # Fallback: inline logic (same as utils.normalize_project_id)
    import re
    s = raw.strip()
    if not s:
        return ""
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.lower().strip('-')


def _check_id_conflict(normalized: str, projects: dict) -> str | None:
    """Check if a normalized ID conflicts with existing projects.
    Returns the conflicting project_id or None.
    """
    for existing_id in projects.get("projects", {}):
        if _normalize_project_id(existing_id) == normalized and existing_id != normalized:
            return existing_id
    return None


# ============================================================
# /api/init — one-time project initialization
# ============================================================

def init_project(project_id: str, password: str = "", project_name: str = "", workspace_path: str = "") -> dict:
    """Initialize a project. No password or token required.

    Rules:
      - project_id is normalized to lowercase kebab-case
      - First call: creates project → returns project info
      - Repeat call: returns existing project info (idempotent)

    Returns: {project: {project_id, name, status, created_at}}
    """
    if not project_id:
        raise ValidationError("project_id is required")

    # Normalize ID
    original_id = project_id
    project_id = _normalize_project_id(project_id)

    if not project_id or not project_id.replace("-", "").isalnum():
        raise ValidationError(f"Invalid project_id: {original_id!r} (normalized: {project_id!r})")

    # Check for conflicting IDs
    projects = _load_projects()
    conflict = _check_id_conflict(project_id, projects)
    if conflict:
        raise ValidationError(
            f"Project ID conflict: {original_id!r} normalizes to {project_id!r} "
            f"which conflicts with existing project {conflict!r}"
        )

    existing = projects["projects"].get(project_id)

    if existing and existing.get("initialized"):
        # Already exists — return existing project (idempotent)
        return {
            "project": {
                "project_id": project_id,
                "name": existing.get("name", project_id),
                "status": existing.get("status", "active"),
                "created_at": existing.get("created_at", ""),
            },
            "message": "Project already initialized",
        }

    # First-time initialization
    project_dir = _governance_root() / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "project_id": project_id,
        "name": project_name or project_id,
        "workspace_path": workspace_path,
        "created_at": _utc_iso(),
        "initialized": True,
        "status": "active",
        "node_count": 0,
    }
    projects["projects"][project_id] = entry
    _save_projects(projects)

    # Ensure DB exists
    conn = get_connection(project_id)
    conn.close()

    result = {
        "project": {
            "project_id": project_id,
            "name": entry["name"],
            "status": "active",
            "created_at": entry["created_at"],
        },
        "message": "Project initialized. Submit tasks via API or Telegram.",
    }
    if original_id != project_id:
        result["normalized_from"] = original_id
        result["message"] += f" Note: project_id normalized from '{original_id}' to '{project_id}'."
    return result


def _reset_coordinator_token(project_id: str, projects: dict, entry: dict) -> dict:
    """Reset coordinator token for an existing project."""
    conn = get_connection(project_id)
    try:
        # Re-register coordinator (will refresh existing session)
        coord_result = role_service.register(
            conn, "coordinator", project_id, "coordinator",
        )
        conn.commit()

        audit_service.record(
            conn, project_id, "coordinator_token_reset",
            actor="human",
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "project": {
            "project_id": project_id,
            "name": entry.get("name", project_id),
            "status": entry.get("status", "active"),
        },
        "coordinator": {
            "session_id": coord_result["session_id"],
            "token": coord_result["token"],
        },
        "message": "Coordinator token has been reset.",
    }


# ============================================================
# Role assignment (coordinator only)
# ============================================================

def assign_role(
    conn,
    project_id: str,
    coordinator_session: dict,
    principal_id: str,
    role: str,
    scope: list = None,
) -> dict:
    """Coordinator assigns a role to another agent.

    Only coordinators can call this. Returns the new agent's token.
    """
    if coordinator_session.get("role") != "coordinator":
        raise PermissionDeniedError(
            coordinator_session.get("role", "unknown"),
            "assign_role",
            {"detail": "Only coordinator can assign roles"},
        )
    if role == "coordinator":
        raise PermissionDeniedError(
            "coordinator", "assign_role",
            {"detail": "Cannot assign coordinator role. Use /api/init to get coordinator token."},
        )

    result = role_service.register(
        conn, principal_id, project_id, role, scope=scope,
    )

    audit_service.record(
        conn, project_id, "role_assigned",
        actor=coordinator_session.get("principal_id", ""),
        assigned_principal=principal_id,
        assigned_role=role,
        session_id=coordinator_session.get("session_id", ""),
    )

    return {
        "principal_id": principal_id,
        "role": role,
        "session_id": result["session_id"],
        "token": result["token"],
        "scope": scope or [],
        "expires_at": result.get("expires_at", ""),
        "message": f"Give this token to {principal_id}. It grants {role} access to {project_id}.",
    }


def revoke_role(
    conn,
    project_id: str,
    coordinator_session: dict,
    session_id: str,
) -> dict:
    """Coordinator revokes an agent's session."""
    if coordinator_session.get("role") != "coordinator":
        raise PermissionDeniedError(
            coordinator_session.get("role", "unknown"),
            "revoke_role",
        )

    result = role_service.deregister(conn, session_id)

    audit_service.record(
        conn, project_id, "role_revoked",
        actor=coordinator_session.get("principal_id", ""),
        revoked_session=session_id,
    )

    return result


# ============================================================
# Project query helpers
# ============================================================

def get_project(project_id: str) -> dict | None:
    projects = _load_projects()
    return projects["projects"].get(project_id)


def list_projects() -> list[dict]:
    projects = _load_projects()
    result = []
    for p in projects["projects"].values():
        # Never expose password_hash
        safe = {k: v for k, v in p.items() if k != "password_hash"}
        result.append(safe)
    return result


def project_exists(project_id: str) -> bool:
    return get_project(project_id) is not None


# ============================================================
# Graph import
# ============================================================

def import_graph(project_id: str, md_path: str) -> dict:
    """Import acceptance graph from markdown for a project."""
    if not project_exists(project_id):
        raise ValidationError(f"Project {project_id!r} not registered")

    graph = AcceptanceGraph()
    result = graph.import_from_markdown(md_path)

    graph_path = _governance_root() / project_id / "graph.json"
    graph.save(graph_path)

    conn = get_connection(project_id)
    try:
        count = state_service.init_node_states(conn, project_id, graph)
        conn.commit()
    finally:
        conn.close()

    projects = _load_projects()
    if project_id in projects["projects"]:
        projects["projects"][project_id]["node_count"] = graph.node_count()
        _save_projects(projects)

    result["node_states_initialized"] = count
    return result


def sync_node_state_from_graph(project_id: str) -> dict:
    """Rebuild or sync runtime node_state rows from the persisted graph definition.

    This is intended for governance recovery paths. It never infers new business
    acceptance; it only re-materializes node_state rows from graph.json and
    import-declared statuses already encoded in the graph.
    """
    if not project_exists(project_id):
        raise ValidationError(f"Project {project_id!r} not registered")

    graph = load_project_graph(project_id)

    conn = get_connection(project_id)
    try:
        initialized = state_service.init_node_states(conn, project_id, graph)
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM node_state WHERE project_id = ?",
            (project_id,),
        ).fetchone()["cnt"]
        conn.commit()
    finally:
        conn.close()

    return {
        "project_id": project_id,
        "graph_nodes": graph.node_count(),
        "node_states_initialized": initialized,
        "node_state_total": total,
        "repair_mode": "sync_from_graph",
    }


def bootstrap_project(
    workspace_path: str,
    project_name: str = "",
    config_override: dict = None,
    scan_depth: int = 3,
    exclude_patterns: list = None,
) -> dict:
    """Bootstrap a project from workspace — atomic orchestrator (R4).

    Steps: config discovery -> init_project -> scan_codebase -> generate_graph
           -> node_state init -> version seed -> preflight check.

    Rollback on failure: removes project entry if it was freshly created.

    Returns: {project_id, graph_stats, config, preflight, warning?}
    """
    import sys as _sys
    _agent_root = str(Path(__file__).resolve().parents[1])
    if _agent_root not in _sys.path:
        _sys.path.insert(0, _agent_root)

    from project_config import (
        effective_graph_exclude_roots,
        generate_default_config,
        load_project_config,
    )

    ws = Path(workspace_path).resolve()
    if not ws.is_dir():
        raise ValidationError(f"workspace_path does not exist or is not a directory: {workspace_path}")

    # Step 1: Config discovery
    try:
        config = load_project_config(ws)
    except (FileNotFoundError, ValueError):
        config = generate_default_config(str(ws), project_name)

    if config_override:
        # Apply overrides
        if "project_id" in config_override:
            config.project_id = config_override["project_id"]
        if "language" in config_override:
            config.language = config_override["language"]
        if "testing" in config_override and "unit_command" in config_override["testing"]:
            config.testing.unit_command = config_override["testing"]["unit_command"]
        if "graph" in config_override and isinstance(config_override["graph"], dict):
            graph_override = config_override["graph"]
            if "exclude_paths" in graph_override:
                config.graph.exclude_paths = [
                    str(value).replace("\\", "/").strip().strip("/")
                    for value in graph_override.get("exclude_paths") or []
                    if str(value or "").strip()
                ]
        if "ai" in config_override and isinstance(config_override["ai"], dict):
            ai_override = config_override["ai"]
            if isinstance(ai_override.get("routing"), dict):
                for role, route in ai_override["routing"].items():
                    if isinstance(route, dict):
                        config.ai.routing[str(role).lower()] = {
                            "provider": str(route.get("provider", "") or "").strip(),
                            "model": str(route.get("model", "") or "").strip(),
                        }

    pid = config.project_id or project_name or ws.name.lower().replace("_", "-")
    pid = _normalize_project_id(pid)

    # Step 2: init_project (idempotent — AC6)
    is_new = not project_exists(pid)
    try:
        init_result = init_project(
            project_id=pid,
            project_name=project_name or pid,
            workspace_path=str(ws),
        )
    except Exception as e:
        raise ValidationError(f"Project initialization failed: {e}")

    try:
        # Step 3: snapshot-native full reconcile + activation. The old
        # generate_graph path wrote a legacy graph.json only; dashboard and
        # graph-governance now consume active graph snapshots.
        configured_excludes = effective_graph_exclude_roots(config)
        effective_excludes = sorted({
            str(value).replace("\\", "/").strip().strip("/")
            for value in ((exclude_patterns or []) + configured_excludes)
            if str(value or "").strip()
        })
        conn = get_connection(pid)
        try:
            # Step 4: version seed remains for legacy gates and health checks.
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT OR REPLACE INTO project_version "
                "(project_id, chain_version, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?)",
                (pid, "bootstrap", now, "bootstrap"),
            )
            conn.commit()

            from .state_reconcile import run_state_only_full_reconcile

            reconcile_result = run_state_only_full_reconcile(
                conn,
                pid,
                ws,
                run_id=f"bootstrap-full-{pid}",
                snapshot_kind="full",
                created_by="bootstrap",
                activate=True,
                notes_extra={
                    "source": "bootstrap_project_v2",
                    "effective_exclude_roots": effective_excludes,
                    "scan_depth": scan_depth,
                },
                semantic_enrich=True,
                semantic_use_ai=False,
                semantic_enqueue_stale=False,
            )
            conn.commit()

            graph_stats = reconcile_result.get("graph_stats") or {}
            index_counts = reconcile_result.get("index_counts") or {}
            node_count = int(graph_stats.get("node_count") or index_counts.get("nodes") or 0)
            edge_count = int(graph_stats.get("edge_count") or index_counts.get("edges") or 0)
            preflight_result = {
                "status": "pass" if reconcile_result.get("ok") else "fail",
                "details": {
                    "bootstrap_mode": "snapshot_full_reconcile",
                    "snapshot_id": reconcile_result.get("snapshot_id", ""),
                    "activation": reconcile_result.get("activation") or {},
                    "projection_status": (
                        reconcile_result.get("activation") or {}
                    ).get("projection_status", ""),
                    "node_count": node_count,
                    "edge_count": edge_count,
                },
            }

            # Update project metadata
            projects = _load_projects()
            if pid in projects["projects"]:
                projects["projects"][pid]["node_count"] = node_count
                projects["projects"][pid]["active_snapshot_id"] = reconcile_result.get("snapshot_id", "")
                _save_projects(projects)

            # Step 5: Backfill chain history for this project at bootstrap.
            try:
                from .chain_trailer import backfill_legacy_chain_history
                backfill_legacy_chain_history(project_id=pid, incremental=False)
            except Exception:
                pass  # Non-fatal — git may not be available in all contexts
        finally:
            conn.close()

    except Exception as e:
        # Rollback: remove project if newly created
        if is_new:
            projects = _load_projects()
            projects["projects"].pop(pid, None)
            _save_projects(projects)
        raise ValidationError(f"Bootstrap failed: {e}")

    # Build response
    config_dict = {
        "project_id": config.project_id or pid,
        "language": config.language,
        "testing": {"unit_command": config.testing.unit_command},
        "deploy": {"strategy": config.deploy.strategy},
        "graph": {
            "exclude_paths": list(getattr(config.graph, "exclude_paths", []) or []),
            "ignore_globs": list(getattr(config.graph, "ignore_globs", []) or []),
            "nested_projects": {
                "mode": getattr(config.graph.nested_projects, "mode", "exclude"),
                "roots": list(getattr(config.graph.nested_projects, "roots", []) or []),
            },
            "effective_exclude_roots": effective_graph_exclude_roots(config),
        },
        "ai": {"routing": dict(getattr(config.ai, "routing", {}) or {})},
    }

    result = {
        "project_id": pid,
        "graph_stats": {
            "node_count": node_count,
            "edge_count": edge_count,
            "layers": (graph_stats or {}).get("layers") or {},
        },
        "config": config_dict,
        "preflight": preflight_result,
        "snapshot_id": reconcile_result.get("snapshot_id", ""),
        "activation": reconcile_result.get("activation") or {},
        "bootstrap_mode": "snapshot_full_reconcile",
    }

    return result


def load_project_graph(project_id: str) -> AcceptanceGraph:
    from .db import _resolve_project_dir
    project_dir = _resolve_project_dir(project_id)
    graph_path = project_dir / "graph.json"
    if not graph_path.exists():
        raise ValidationError(f"No graph found for project {project_id!r}. Run import-graph first.")
    graph = AcceptanceGraph()
    graph.load(graph_path)
    return graph
