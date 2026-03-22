"""Project service — project registration, isolation, and routing.

Each project gets its own directory with graph.json, governance.db, and audit logs.
"""

import json
import os
import sys
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
from .errors import ValidationError


def _projects_file() -> Path:
    p = _governance_root() / "projects.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def register_project(project_id: str, name: str = "", workspace_path: str = "") -> dict:
    """Register a new project."""
    if not project_id or not project_id.replace("-", "").replace("_", "").isalnum():
        raise ValidationError(f"Invalid project_id: {project_id!r}")

    projects = _load_projects()
    if project_id in projects["projects"]:
        return projects["projects"][project_id]

    project_dir = _governance_root() / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "project_id": project_id,
        "name": name or project_id,
        "workspace_path": workspace_path,
        "created_at": _utc_iso(),
        "status": "initialized",
        "node_count": 0,
    }
    projects["projects"][project_id] = entry
    _save_projects(projects)
    return entry


def get_project(project_id: str) -> dict | None:
    projects = _load_projects()
    return projects["projects"].get(project_id)


def list_projects() -> list[dict]:
    projects = _load_projects()
    return list(projects["projects"].values())


def project_exists(project_id: str) -> bool:
    return get_project(project_id) is not None


def import_graph(project_id: str, md_path: str) -> dict:
    """Import acceptance graph from markdown for a project."""
    if not project_exists(project_id):
        raise ValidationError(f"Project {project_id!r} not registered")

    graph = AcceptanceGraph()
    result = graph.import_from_markdown(md_path)

    # Finalize edges from deps
    _finalize_graph_edges(graph)

    # Save graph
    graph_path = _governance_root() / project_id / "graph.json"
    graph.save(graph_path)

    # Initialize node states in SQLite
    conn = get_connection(project_id)
    try:
        count = state_service.init_node_states(conn, project_id, graph)
        conn.commit()
    finally:
        conn.close()

    # Update project info
    projects = _load_projects()
    if project_id in projects["projects"]:
        projects["projects"][project_id]["node_count"] = graph.node_count()
        projects["projects"][project_id]["status"] = "active"
        _save_projects(projects)

    result["node_states_initialized"] = count
    return result


def _finalize_graph_edges(graph: AcceptanceGraph):
    """After markdown import, parse deps from node text and add edges."""
    # The markdown parser stores raw deps text; we need to wire edges
    # This is handled during import_from_markdown via _parse_node_block
    # For now, the edges should already be in the graph from the deps field
    pass


def load_project_graph(project_id: str) -> AcceptanceGraph:
    """Load the saved graph for a project."""
    graph_path = _governance_root() / project_id / "graph.json"
    if not graph_path.exists():
        raise ValidationError(f"No graph found for project {project_id!r}. Run import-graph first.")
    graph = AcceptanceGraph()
    graph.load(graph_path)
    return graph


def bootstrap(
    project_id: str,
    project_name: str = "",
    workspace_path: str = "",
    graph_source: str = None,
    coordinator_principal: str = "",
    admin_secret: str = "",
) -> dict:
    """One-shot bootstrap: register project + import graph + register coordinator."""
    # 1. Register project
    project = register_project(project_id, project_name, workspace_path)

    # 2. Import graph if source provided
    graph_result = None
    if graph_source and Path(graph_source).exists():
        graph_result = import_graph(project_id, graph_source)

    # 3. Register coordinator
    coord_result = None
    if coordinator_principal:
        conn = get_connection(project_id)
        try:
            coord_result = role_service.register(
                conn, coordinator_principal, project_id,
                "coordinator", admin_secret=admin_secret,
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "project": project,
        "graph": graph_result,
        "coordinator": {
            "session_id": coord_result["session_id"],
            "token": coord_result["token"],
        } if coord_result else None,
    }
