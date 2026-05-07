"""State-only governance index scanner.

The governance index is the substrate that full/scope reconcile can consume
before any code-writing chain stage runs. It scans project files, hashes, symbol
locations, documentation headings, and the active graph snapshot mapping, then
optionally persists those artifacts as governance state.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from agent.governance.external_project_governance import (
    build_coverage_state,
    build_doc_index,
    build_symbol_index,
)
from agent.governance.graph_snapshot_store import (
    ensure_schema as ensure_graph_snapshot_schema,
    get_active_graph_snapshot,
)
from agent.governance.project_profile import ProjectProfile, discover_project_profile
from agent.governance.reconcile_file_inventory import (
    build_file_inventory,
    summarize_file_inventory,
    upsert_file_inventory,
)


GOVERNANCE_INDEX_SCHEMA_VERSION = 1


def _utc_now() -> str:
    from agent.governance.graph_snapshot_store import utc_now

    return utc_now()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )


def _decode_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            values = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            values = [raw]
    else:
        values = []
    out: list[str] = []
    for item in values:
        text = str(item or "").replace("\\", "/").strip("/")
        if text:
            out.append(text)
    return sorted(set(out))


def _decode_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _git_commit(project_root: str | Path, ref: str = "HEAD") -> str:
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    return (result.stdout or "").strip()


def _make_run_id(commit_sha: str) -> str:
    short = (commit_sha or "unknown")[:7] or "unknown"
    return f"governance-index-{short}-{uuid.uuid4().hex[:8]}"


def load_snapshot_nodes_for_inventory(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> list[dict[str, Any]]:
    """Load active graph index rows in the node shape expected by inventory."""
    if not snapshot_id:
        return []
    ensure_graph_snapshot_schema(conn)
    rows = conn.execute(
        """
        SELECT node_id, layer, title, kind, primary_files_json,
               secondary_files_json, test_files_json, metadata_json
        FROM graph_nodes_index
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (project_id, snapshot_id),
    ).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        get = row.__getitem__ if hasattr(row, "keys") else lambda key: row[key]
        metadata = _decode_json_object(get("metadata_json"))
        kind = str(get("kind") or metadata.get("kind") or "")
        nodes.append({
            "id": str(get("node_id") or ""),
            "node_id": str(get("node_id") or ""),
            "layer": str(get("layer") or ""),
            "title": str(get("title") or ""),
            "kind": kind,
            "primary": _decode_json_list(get("primary_files_json")),
            "secondary": _decode_json_list(get("secondary_files_json")),
            "test": _decode_json_list(get("test_files_json")),
            "metadata": metadata,
        })
    return [node for node in nodes if node.get("id")]


def load_active_snapshot_nodes(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return the active graph snapshot row and decoded node index."""
    active = get_active_graph_snapshot(conn, project_id)
    if not active:
        return None, []
    return active, load_snapshot_nodes_for_inventory(conn, project_id, active["snapshot_id"])


def _candidate_graph_from_nodes(nodes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"deps_graph": {"nodes": list(nodes), "edges": []}}


def build_governance_index(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    run_id: str = "",
    commit_sha: str = "",
    profile: ProjectProfile | None = None,
    include_active_graph: bool = True,
) -> dict[str, Any]:
    """Build a governance index without mutating project source files."""
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    commit = commit_sha or _git_commit(root) or "unknown"
    rid = run_id or _make_run_id(commit)
    profile = profile or discover_project_profile(str(root))

    active_snapshot: dict[str, Any] | None = None
    active_nodes: list[dict[str, Any]] = []
    if include_active_graph:
        active_snapshot, active_nodes = load_active_snapshot_nodes(conn, project_id)

    file_inventory = build_file_inventory(
        project_root=str(root),
        run_id=rid,
        nodes=active_nodes,
        profile=profile,
        last_scanned_commit=commit,
    )
    symbol_index = build_symbol_index(
        project_root=root,
        file_inventory=file_inventory,
        profile=profile,
    )
    doc_index = build_doc_index(project_root=root, file_inventory=file_inventory)
    coverage_state = build_coverage_state(
        candidate_graph=_candidate_graph_from_nodes(active_nodes),
        file_inventory=file_inventory,
    )
    coverage_state.update({
        "active_snapshot_id": active_snapshot.get("snapshot_id") if active_snapshot else "",
        "active_graph_commit": active_snapshot.get("commit_sha") if active_snapshot else "",
        "commit_sha": commit,
        "run_id": rid,
        "schema_version": GOVERNANCE_INDEX_SCHEMA_VERSION,
        "symbol_count": symbol_index.get("symbol_count", 0),
        "doc_heading_count": doc_index.get("heading_count", 0),
    })

    return {
        "schema_version": GOVERNANCE_INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": rid,
        "commit_sha": commit,
        "generated_at": _utc_now(),
        "project_root": str(root),
        "active_snapshot": dict(active_snapshot) if active_snapshot else {},
        "active_node_count": len(active_nodes),
        "profile": asdict(profile),
        "file_inventory": file_inventory,
        "file_inventory_summary": summarize_file_inventory(file_inventory),
        "symbol_index": symbol_index,
        "doc_index": doc_index,
        "coverage_state": coverage_state,
    }


def governance_index_artifact_dir(
    project_id: str,
    run_id: str,
    *,
    artifact_root: str | Path | None = None,
) -> Path:
    if artifact_root is not None:
        return Path(artifact_root).resolve() / run_id
    from .db import _governance_root

    return _governance_root() / project_id / "governance-index" / run_id


def persist_governance_index(
    conn: sqlite3.Connection,
    project_id: str,
    index: dict[str, Any],
    *,
    artifact_root: str | Path | None = None,
    persist_inventory: bool = True,
) -> dict[str, Any]:
    """Write governance index artifacts and optionally persist inventory rows."""
    run_id = str(index.get("run_id") or "")
    if not run_id:
        raise ValueError("governance index is missing run_id")
    base = governance_index_artifact_dir(project_id, run_id, artifact_root=artifact_root)
    artifacts = {
        "profile_path": base / "project-profile.json",
        "file_inventory_path": base / "file-inventory.json",
        "symbol_index_path": base / "symbol-index.json",
        "doc_index_path": base / "doc-index.json",
        "coverage_state_path": base / "coverage-state.json",
        "summary_path": base / "summary.json",
    }
    _write_json(artifacts["profile_path"], index.get("profile") or {})
    _write_json(artifacts["file_inventory_path"], index.get("file_inventory") or [])
    _write_json(artifacts["symbol_index_path"], index.get("symbol_index") or {})
    _write_json(artifacts["doc_index_path"], index.get("doc_index") or {})
    _write_json(artifacts["coverage_state_path"], index.get("coverage_state") or {})

    inventory_count = 0
    if persist_inventory:
        inventory_count = upsert_file_inventory(
            conn,
            project_id,
            index.get("file_inventory") or [],
            replace_run=True,
        )

    summary = {
        "schema_version": GOVERNANCE_INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "commit_sha": index.get("commit_sha") or "",
        "active_snapshot_id": (index.get("active_snapshot") or {}).get("snapshot_id", ""),
        "active_graph_commit": (index.get("active_snapshot") or {}).get("commit_sha", ""),
        "active_node_count": index.get("active_node_count", 0),
        "file_inventory_summary": index.get("file_inventory_summary") or {},
        "symbol_count": (index.get("symbol_index") or {}).get("symbol_count", 0),
        "doc_heading_count": (index.get("doc_index") or {}).get("heading_count", 0),
        "inventory_rows_persisted": inventory_count,
        "artifacts": {name: str(path) for name, path in artifacts.items()},
        "generated_at": _utc_now(),
    }
    _write_json(artifacts["summary_path"], summary)
    return summary


def build_and_persist_governance_index(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build and persist a governance index in one explicit state operation."""
    persist_keys = {"artifact_root", "persist_inventory"}
    build_kwargs = {k: v for k, v in kwargs.items() if k not in persist_keys}
    index = build_governance_index(conn, project_id, project_root, **build_kwargs)
    summary = persist_governance_index(
        conn,
        project_id,
        index,
        artifact_root=kwargs.get("artifact_root"),
        persist_inventory=bool(kwargs.get("persist_inventory", True)),
    )
    return {**index, "persist_summary": summary}


__all__ = [
    "GOVERNANCE_INDEX_SCHEMA_VERSION",
    "build_and_persist_governance_index",
    "build_governance_index",
    "governance_index_artifact_dir",
    "load_active_snapshot_nodes",
    "load_snapshot_nodes_for_inventory",
    "persist_governance_index",
]
