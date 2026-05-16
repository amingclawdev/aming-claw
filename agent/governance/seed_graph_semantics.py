"""Import packaged seed graph context into semantic state.

The seed graph resource is intentionally lightweight packaged context for fresh
plugin sessions. This module turns its core surface summaries into trusted local
semantic state for nodes that already exist in an active graph snapshot.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import graph_events
from . import graph_snapshot_store as store
from . import reconcile_semantic_enrichment as semantic


DEFAULT_SEED_GRAPH_SUMMARY_PATH = (
    Path(__file__).resolve().parents[1] / "mcp" / "resources" / "seed-graph-summary.json"
)


def import_seed_graph_semantics(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    seed_path: str | Path | None = None,
    actor: str = "seed_graph_import",
    projection_id: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Map packaged seed graph surfaces to snapshot nodes and import semantics."""
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    seed = _load_seed_summary(seed_path)
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        include_semantic=False,
        limit=1000,
    )
    path_index = _path_index(nodes)
    surfaces = seed.get("core_surfaces") if isinstance(seed.get("core_surfaces"), list) else []

    reports: list[dict[str, Any]] = []
    node_matches: dict[str, dict[str, Any]] = {}
    unmapped_paths: set[str] = set()
    for raw_surface in surfaces:
        if not isinstance(raw_surface, dict):
            continue
        surface = _normalize_surface(raw_surface)
        matched_node_ids: set[str] = set()
        surface_unmapped: list[str] = []
        for raw_path in surface["paths"]:
            norm = _normalize_path(raw_path)
            matches = path_index.get(norm, [])
            if not matches:
                unmapped_paths.add(raw_path)
                surface_unmapped.append(raw_path)
                continue
            for node in matches:
                node_id = str(node.get("node_id") or "")
                if not node_id:
                    continue
                matched_node_ids.add(node_id)
                entry = node_matches.setdefault(
                    node_id,
                    {"node": node, "surfaces": [], "matched_paths": set()},
                )
                entry["matched_paths"].add(raw_path)
        for node_id in sorted(matched_node_ids):
            node_matches[node_id]["surfaces"].append(surface)
        reports.append({
            "name": surface["name"],
            "path_count": len(surface["paths"]),
            "matched_node_ids": sorted(matched_node_ids),
            "unmapped_paths": surface_unmapped,
        })

    semantic_state = semantic._empty_semantic_graph_state(project_id, snapshot_id, snapshot)
    now = store.utc_now()
    for node_id, match in sorted(node_matches.items()):
        node = match["node"]
        node_surfaces = match["surfaces"]
        matched_paths = sorted(match["matched_paths"])
        semantic_state["node_semantics"][node_id] = _semantic_entry_for_node(
            node,
            node_surfaces,
            matched_paths,
            seed=seed,
            updated_at=now,
        )
    semantic._rebuild_semantic_graph_state_indexes(semantic_state)

    result: dict[str, Any] = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "seed_path": str(seed_path or DEFAULT_SEED_GRAPH_SUMMARY_PATH),
        "seed_schema_version": seed.get("schema_version"),
        "seed_last_curated_commit": seed.get("last_curated_commit", ""),
        "surface_count": len([surface for surface in surfaces if isinstance(surface, dict)]),
        "matched_surface_count": sum(1 for report in reports if report["matched_node_ids"]),
        "unmapped_surface_count": sum(1 for report in reports if not report["matched_node_ids"]),
        "imported_node_count": len(semantic_state["node_semantics"]),
        "unmapped_paths": sorted(unmapped_paths),
        "surface_reports": reports,
        "dry_run": bool(dry_run),
    }

    if dry_run:
        return result

    semantic._persist_semantic_state_to_db(conn, project_id, snapshot_id, semantic_state)
    backfill = graph_events.backfill_existing_semantic_events(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
    )
    projection = graph_events.build_semantic_projection(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
        projection_id=projection_id or _default_projection_id(snapshot_id),
        backfill_existing=False,
    )
    health = projection.get("health") if isinstance(projection.get("health"), dict) else {}
    result.update({
        "backfill": backfill,
        "projection_id": projection.get("projection_id", ""),
        "projection_status": projection.get("status", ""),
        "semantic_current_count": health.get("semantic_current_count", 0),
        "semantic_missing_count": health.get("semantic_missing_count", 0),
        "semantic_status_counts": health.get("semantic_status_counts", {}),
    })
    return result


def _load_seed_summary(seed_path: str | Path | None) -> dict[str, Any]:
    path = Path(seed_path) if seed_path else DEFAULT_SEED_GRAPH_SUMMARY_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"seed graph summary must be a JSON object: {path}")
    return payload


def _normalize_surface(surface: dict[str, Any]) -> dict[str, Any]:
    name = str(surface.get("name") or "").strip() or "unnamed"
    paths = [
        str(path).replace("\\", "/").strip()
        for path in (surface.get("paths") or [])
        if str(path or "").strip()
    ]
    return {
        "name": name,
        "paths": paths,
        "notes": str(surface.get("notes") or "").strip(),
    }


def _path_index(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        for path in _node_paths(node):
            norm = _normalize_path(path)
            if norm:
                index.setdefault(norm, []).append(node)
    return index


def _node_paths(node: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("primary_files", "secondary_files", "test_files", "config_files"):
        raw = node.get(key)
        if isinstance(raw, list):
            paths.extend(str(path) for path in raw if str(path or "").strip())
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    raw_config = metadata.get("config_files")
    if isinstance(raw_config, list):
        paths.extend(str(path) for path in raw_config if str(path or "").strip())
    return sorted(set(paths))


def _normalize_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _semantic_entry_for_node(
    node: dict[str, Any],
    surfaces: list[dict[str, Any]],
    matched_paths: list[str],
    *,
    seed: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    names = [surface["name"] for surface in surfaces if surface.get("name")]
    notes = [surface["notes"] for surface in surfaces if surface.get("notes")]
    feature_name = "Seed Graph: " + ", ".join(names) if names else str(node.get("title") or node.get("node_id") or "")
    summary = " ".join(notes).strip()
    if not summary:
        summary = f"Packaged seed graph context for {feature_name}."
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    file_hashes = metadata.get("file_hashes") if isinstance(metadata.get("file_hashes"), dict) else {}
    return {
        "status": "semantic_graph_state",
        "feature_name": feature_name,
        "domain_label": _domain_label(names),
        "intent": summary,
        "semantic_summary": summary,
        "quality_flags": ["seed_graph_import"],
        "primary": node.get("primary_files") or [],
        "secondary": node.get("secondary_files") or [],
        "test": node.get("test_files") or [],
        "config": node.get("config_files") or metadata.get("config_files") or [],
        "feature_hash": graph_events.feature_hash_for_node(node),
        "file_hashes": file_hashes,
        "seed_source": "seed-graph-summary",
        "seed_schema_version": seed.get("schema_version"),
        "seed_last_curated_commit": seed.get("last_curated_commit", ""),
        "seed_surface_names": names,
        "seed_paths": matched_paths,
        "operation_type": "seed_graph_import",
        "updated_at": updated_at,
    }


def _domain_label(names: list[str]) -> str:
    if not names:
        return "seed_graph"
    if len(names) == 1:
        return "seed_graph." + re.sub(r"[^a-z0-9]+", "_", names[0].lower()).strip("_")
    return "seed_graph.multi_surface"


def _default_projection_id(snapshot_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", snapshot_id).strip("-")
    return f"semproj-seed-{safe or 'snapshot'}"
