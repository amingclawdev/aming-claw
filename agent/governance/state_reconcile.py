"""State-only reconcile runners.

These helpers materialize reconcile outputs as governance state. They are not
chain stages and they must not edit project source, documentation, or tests.
Observer signoff or a later merge/finalize path decides when a candidate graph
snapshot becomes active.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from agent.governance import graph_events
from agent.governance.graph_snapshot_store import (
    PENDING_STATUS_FAILED,
    PENDING_STATUS_MATERIALIZED,
    PENDING_STATUS_QUEUED,
    PENDING_STATUS_RUNNING,
    SNAPSHOT_STATUS_CANDIDATE,
    activate_graph_snapshot,
    create_graph_snapshot,
    ensure_schema as ensure_graph_snapshot_schema,
    finalize_graph_snapshot,
    get_active_graph_snapshot,
    get_graph_snapshot,
    graph_payload_edges,
    graph_payload_stats,
    index_graph_snapshot,
    list_graph_snapshot_files,
    list_pending_scope_reconcile,
    snapshot_companion_dir,
    snapshot_graph_path,
    snapshot_id_for,
    waive_pending_scope_reconcile,
    write_companion_files,
)
from agent.governance.graph_correction_patches import (
    annotate_graph_node_roles,
    annotate_graph_relationship_metrics,
    apply_correction_patches,
    ensure_schema as ensure_graph_correction_schema,
    list_replayable_patches,
    persist_node_migrations,
    record_patch_apply_report,
)
from agent.governance.db import sqlite_write_lock
from agent.governance.dirty_worktree import filter_dirty_files, parse_git_porcelain_paths
from agent.governance.governance_index import (
    build_governance_index,
    merge_feature_hashes_into_graph_nodes,
    persist_governance_index,
)
from agent.governance.checkout_provenance import describe_checkout
from agent.governance.reconcile_semantic_enrichment import run_semantic_enrichment
from agent.governance.reconcile_trace import ReconcileTrace, artifact_ref
from agent.governance.reconcile_file_inventory import (
    filter_governed_inventory_rows,
    filter_governed_paths,
    git_tracked_paths,
)
from agent.governance.reconcile_phases.phase_z_v2 import (
    build_test_consumer_fanin_index,
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
    extract_typed_relations,
    parse_production_modules,
    parse_production_module_file,
)


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


def _short_commit(commit_sha: str) -> str:
    text = str(commit_sha or "").strip()
    return text[:7] if text else "unknown"


def _governance_state_dir(project_id: str, run_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "state-reconcile" / run_id


def _git_changed_files(project_root: str | Path, base_ref: str, target_ref: str) -> list[str]:
    base = str(base_ref or "").strip()
    target = str(target_ref or "").strip()
    if not base or not target or base == target:
        return []
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", f"{base}..{target}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    return sorted({
        line.replace("\\", "/").strip("/")
        for line in (result.stdout or "").splitlines()
        if line.strip()
    })


def _git_dirty_files(project_root: str | Path) -> list[str]:
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    return filter_dirty_files(parse_git_porcelain_paths(result.stdout or ""))


def _deps_graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    nodes = deps.get("nodes") if isinstance(deps, dict) else []
    return [node for node in nodes or [] if isinstance(node, dict)]


def _deps_graph_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    return graph_payload_edges(graph_json)


def _normalize_inventory_commit(
    rows: list[dict[str, Any]],
    *,
    commit_sha: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if commit_sha and not item.get("last_scanned_commit"):
            item["last_scanned_commit"] = commit_sha
        if item.get("sha256") and not item.get("file_hash"):
            item["file_hash"] = f"sha256:{item['sha256']}"
        out.append(item)
    return out


def _rows_by_path(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row.get("path") or "").replace("\\", "/").strip("/")
        if path:
            out[path] = row
    return out


def _snapshot_inventory_rows(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> list[dict[str, Any]]:
    if not snapshot_id:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while True:
        try:
            payload = list_graph_snapshot_files(
                conn,
                project_id,
                snapshot_id,
                limit=page_size,
                offset=offset,
            )
        except Exception:
            return rows
        page = [dict(row) for row in payload.get("files") or [] if isinstance(row, dict)]
        rows.extend(page)
        filtered_count = int(payload.get("filtered_count") or payload.get("total_count") or 0)
        offset += len(page)
        if not page or offset >= filtered_count:
            return rows


def _row_file_hash(row: dict[str, Any]) -> str:
    value = str(row.get("file_hash") or "").strip()
    if value:
        return value
    sha = str(row.get("sha256") or "").strip()
    return f"sha256:{sha}" if sha else ""


def _build_scope_file_delta(
    *,
    project_root: str | Path | None = None,
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    changed_files: list[str],
) -> dict[str, Any]:
    if project_root is not None:
        old_rows = filter_governed_inventory_rows(project_root, old_rows)
        new_rows = filter_governed_inventory_rows(project_root, new_rows)
        changed_files = filter_governed_paths(project_root, changed_files)
        tracked_paths = git_tracked_paths(project_root)
        if tracked_paths is not None:
            def row_path(row: dict[str, Any]) -> str:
                return str(row.get("path") or "").replace("\\", "/").strip("/")

            changed_path_set = {
                str(path or "").replace("\\", "/").strip("/")
                for path in changed_files
                if str(path or "").strip()
            }
            old_rows = [
                row for row in old_rows
                if row_path(row) in tracked_paths
                or row_path(row) in changed_path_set
            ]
            new_rows = [
                row for row in new_rows
                if row_path(row) in tracked_paths
            ]
    old_by_path = _rows_by_path(old_rows)
    new_by_path = _rows_by_path(new_rows)
    old_paths = set(old_by_path)
    new_paths = set(new_by_path)
    added = sorted(new_paths - old_paths)
    removed = sorted(old_paths - new_paths)
    changed = sorted({path.replace("\\", "/").strip("/") for path in changed_files if path})
    hash_changed = sorted(
        path for path in (old_paths & new_paths)
        if _row_file_hash(old_by_path[path]) != _row_file_hash(new_by_path[path])
    )
    status_candidate_paths = set(changed) | set(hash_changed)
    status_changed = sorted(
        path for path in (old_paths & new_paths)
        if path in status_candidate_paths
        and (
            str(old_by_path[path].get("graph_status") or "")
            != str(new_by_path[path].get("graph_status") or "")
            or str(old_by_path[path].get("scan_status") or "")
            != str(new_by_path[path].get("scan_status") or "")
        )
    )
    ignored_status_changed = sorted(
        path for path in (old_paths & new_paths)
        if path not in status_candidate_paths
        and (
            str(old_by_path[path].get("graph_status") or "")
            != str(new_by_path[path].get("graph_status") or "")
            or str(old_by_path[path].get("scan_status") or "")
            != str(new_by_path[path].get("scan_status") or "")
        )
    )
    impacted = sorted(set(changed) | set(added) | set(removed) | set(hash_changed) | set(status_changed))
    return {
        "strategy": "full_scan_with_incremental_file_delta",
        "changed_files": changed,
        "added_files": added,
        "removed_files": removed,
        "hash_changed_files": hash_changed,
        "status_changed_files": status_changed,
        "ignored_status_changed_files": ignored_status_changed,
        "impacted_files": impacted,
        "changed_file_count": len(changed),
        "impacted_file_count": len(impacted),
    }


def _read_snapshot_graph(project_id: str, snapshot_id: str) -> dict[str, Any]:
    if not snapshot_id:
        return {}
    try:
        payload = json.loads(snapshot_graph_path(project_id, snapshot_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_snapshot_companion(project_id: str, snapshot_id: str, filename: str, default: Any) -> Any:
    try:
        payload = json.loads((snapshot_companion_dir(project_id, snapshot_id) / filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload


_RECONCILE_COMPARISON_VOLATILE_KEYS = {
    "artifact_path",
    "attached_to",
    "attachment_source",
    "candidate_node_id",
    "cluster_id",
    "created_at",
    "drift_sha256",
    "generated_at",
    "graph_sha256",
    "inventory_sha256",
    "phase_report_path",
    "project_root",
    "reason",
    "review_report_path",
    "run_id",
    "scratch_dir",
    "semantic_index_path",
    "snapshot_artifact",
    "snapshot_id",
    "snapshot_path",
    "state_dir",
    "summary_path",
    "trace",
    "trace_dir",
    "updated_at",
    "version",
}


def _stable_json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_for_reconcile_comparison(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_for_reconcile_comparison(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _RECONCILE_COMPARISON_VOLATILE_KEYS
        }
    if isinstance(value, list):
        normalized = [_normalize_for_reconcile_comparison(item) for item in value]
        return sorted(normalized, key=_stable_json_key)
    return value


def normalize_reconcile_snapshot_for_comparison(
    graph_json: dict[str, Any],
    *,
    file_inventory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a stable structural view for full-vs-scope reconcile comparison.

    The helper deliberately strips run-specific metadata, artifact paths,
    cluster provenance, timestamps, and snapshot ids while preserving node ids,
    graph structure, file bindings, file hashes, function metadata, and
    inventory state. It is intended for regression tests that compare full
    rebuild output with scope reconcile output for the same final project
    state.
    """
    nodes = _deps_graph_nodes(graph_json)
    edges = _deps_graph_edges(graph_json)
    inventory = file_inventory or []
    return {
        "nodes": _normalize_for_reconcile_comparison(nodes),
        "edges": _normalize_for_reconcile_comparison(edges),
        "file_inventory": _normalize_for_reconcile_comparison(inventory),
    }


def repair_snapshot_feature_hash_metadata(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    snapshot_id: str = "",
    actor: str = "observer",
) -> dict[str, Any]:
    """Backfill indexed feature/file hashes into an existing snapshot and node index."""
    ensure_graph_snapshot_schema(conn)
    snapshot = (
        get_graph_snapshot(conn, project_id, snapshot_id)
        if snapshot_id
        else get_active_graph_snapshot(conn, project_id)
    )
    if not snapshot:
        raise KeyError(f"graph snapshot not found for project {project_id}: {snapshot_id or 'active'}")
    sid = str(snapshot.get("snapshot_id") or "")
    graph_json = _read_snapshot_graph(project_id, sid)
    if not graph_json:
        raise ValueError(f"snapshot graph companion is empty or unreadable: {project_id}/{sid}")
    file_inventory = _read_snapshot_companion(project_id, sid, "file_inventory.json", [])
    if not isinstance(file_inventory, list):
        file_inventory = []
    drift_ledger = _read_snapshot_companion(project_id, sid, "drift_ledger.json", [])
    if not isinstance(drift_ledger, list):
        drift_ledger = []
    governance_index = build_governance_index(
        conn,
        project_id,
        project_root,
        run_id=f"hash-repair-{_short_commit(str(snapshot.get('commit_sha') or ''))}",
        commit_sha=str(snapshot.get("commit_sha") or ""),
        candidate_graph=graph_json,
        snapshot_id=sid,
        snapshot_kind=str(snapshot.get("snapshot_kind") or ""),
        file_inventory=file_inventory,
    )
    merge_summary = merge_feature_hashes_into_graph_nodes(graph_json, governance_index)
    artifacts = write_companion_files(
        project_id,
        sid,
        graph_json=graph_json,
        file_inventory=file_inventory,
        drift_ledger=drift_ledger,
    )
    index_counts = index_graph_snapshot(
        conn,
        project_id,
        sid,
        nodes=_deps_graph_nodes(graph_json),
        edges=_deps_graph_edges(graph_json),
    )
    try:
        notes = json.loads(str(snapshot.get("notes") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        notes = {}
    if not isinstance(notes, dict):
        notes = {}
    notes["feature_hash_metadata_repair"] = {
        "actor": actor,
        "merge_summary": merge_summary,
        "index_counts": index_counts,
        "artifacts": {
            "graph_sha256": artifacts.get("graph_sha256", ""),
            "inventory_sha256": artifacts.get("inventory_sha256", ""),
            "drift_sha256": artifacts.get("drift_sha256", ""),
        },
    }
    conn.execute(
        """
        UPDATE graph_snapshots
        SET graph_sha256 = ?, inventory_sha256 = ?, drift_sha256 = ?, notes = ?
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (
            artifacts.get("graph_sha256", ""),
            artifacts.get("inventory_sha256", ""),
            artifacts.get("drift_sha256", ""),
            json.dumps(notes, ensure_ascii=False, sort_keys=True),
            project_id,
            sid,
        ),
    )
    return {
        "snapshot_id": sid,
        "commit_sha": snapshot.get("commit_sha", ""),
        "merge_summary": merge_summary,
        "index_counts": index_counts,
        "artifacts": artifacts,
    }


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        return [node for node in deps.get("nodes", []) if isinstance(node, dict)]
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else []
    if isinstance(nodes, list):
        return [node for node in nodes if isinstance(node, dict)]
    if isinstance(nodes, dict):
        out: list[dict[str, Any]] = []
        for node_id, node in nodes.items():
            item = dict(node) if isinstance(node, dict) else {}
            item.setdefault("id", node_id)
            out.append(item)
        return out
    return []


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "")


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _node_parent(node: dict[str, Any]) -> str:
    metadata = _node_metadata(node)
    return str(metadata.get("hierarchy_parent") or node.get("parent") or node.get("parent_id") or "")


def _path_values(node: dict[str, Any], key: str) -> list[str]:
    aliases = {
        "primary": ("primary", "primary_files"),
        "secondary": ("secondary", "secondary_files"),
        "test": ("test", "test_files"),
        "config": ("config", "config_files"),
    }.get(key, (key,))
    out: list[str] = []
    seen: set[str] = set()
    metadata = _node_metadata(node)
    for alias in aliases:
        raw = node.get(alias)
        if raw is None and alias.endswith("_files"):
            raw = metadata.get(alias)
        values = raw if isinstance(raw, list) else [raw] if raw else []
        for value in values:
            path = str(value or "").replace("\\", "/").strip("/")
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _node_file_hashes(node: dict[str, Any]) -> dict[str, str]:
    metadata = _node_metadata(node)
    raw = metadata.get("file_hashes")
    if not isinstance(raw, dict):
        return {}
    return {
        str(path or "").replace("\\", "/").strip("/"): str(value or "")
        for path, value in raw.items()
        if str(path or "").strip()
    }


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    return (
        str(edge.get("source") or edge.get("from") or ""),
        str(edge.get("target") or edge.get("to") or ""),
        str(edge.get("type") or edge.get("relation") or edge.get("relation_type") or ""),
        str(metadata.get("edge_kind") or edge.get("kind") or ""),
    )


_INCREMENTAL_METADATA_FILE_KINDS = {"config", "doc", "index_doc"}


def _norm_repo_path(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _node_for_primary_path(graph_json: dict[str, Any], path: str) -> dict[str, Any] | None:
    norm = str(path or "").replace("\\", "/").strip("/")
    if not norm:
        return None
    matches = [
        node for node in _graph_nodes(graph_json)
        if norm in set(_path_values(node, "primary"))
    ]
    return matches[0] if len(matches) == 1 else None


def _module_function_signature(module: Any) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for func in getattr(module, "functions", []) or []:
        name = str(getattr(func, "name", "") or "")
        qualified_name = str(getattr(func, "qualified_name", "") or "")
        if not name or not qualified_name:
            continue
        signature.append({
            "name": name,
            "qualified_name": qualified_name,
            "lineno": int(getattr(func, "lineno", 0) or 0),
            "end_lineno": int(getattr(func, "end_lineno", 0) or 0),
        })
    return sorted(signature, key=lambda item: item["qualified_name"])


def _node_function_signature(node: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _node_metadata(node)
    raw_functions = metadata.get("functions") if isinstance(metadata.get("functions"), list) else []
    raw_lines = metadata.get("function_lines") if isinstance(metadata.get("function_lines"), dict) else {}
    signature: list[dict[str, Any]] = []
    for raw in raw_functions:
        qualified_name = str(raw or "")
        if not qualified_name:
            continue
        name = qualified_name.rsplit("::", 1)[-1]
        lines = raw_lines.get(name) if isinstance(raw_lines.get(name), list) else []
        signature.append({
            "name": name,
            "qualified_name": qualified_name,
            "lineno": int(lines[0]) if len(lines) >= 1 else 0,
            "end_lineno": int(lines[1]) if len(lines) >= 2 else int(lines[0]) if lines else 0,
        })
    return sorted(signature, key=lambda item: item["qualified_name"])


def _source_path_incremental_eligibility(
    project_root: str | Path,
    active_graph_json: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    node = _node_for_primary_path(active_graph_json, path)
    if not node:
        return {"supported": False, "reason": "source_primary_node_not_unique", "path": path}
    module = parse_production_module_file(str(Path(project_root).resolve()), path)
    if module is None:
        return {"supported": False, "reason": "source_adapter_parse_failed", "path": path}
    metadata = _node_metadata(node)
    expected_module = str(metadata.get("module") or "")
    parsed_module = str(getattr(module, "module_name", "") or "")
    if expected_module and parsed_module != expected_module:
        return {
            "supported": False,
            "reason": "source_module_identity_changed",
            "path": path,
            "expected_module": expected_module,
            "parsed_module": parsed_module,
        }
    if getattr(module, "import_map", None):
        return {"supported": False, "reason": "source_imports_require_full_rebuild", "path": path}
    if getattr(module, "adapter_imports", None):
        return {"supported": False, "reason": "source_adapter_imports_require_full_rebuild", "path": path}
    if getattr(module, "adapter_relations", None):
        return {"supported": False, "reason": "source_adapter_relations_require_full_rebuild", "path": path}
    for func in getattr(module, "functions", []) or []:
        if getattr(func, "decorators", None):
            return {"supported": False, "reason": "source_decorators_require_full_rebuild", "path": path}
        if getattr(func, "calls", None):
            return {"supported": False, "reason": "source_function_calls_require_full_rebuild", "path": path}
    try:
        typed_relations = extract_typed_relations(
            str(Path(project_root).resolve()),
            {parsed_module: module},
        )
    except Exception:
        typed_relations = [{"error": "typed_relation_scan_failed"}]
    if typed_relations:
        return {
            "supported": False,
            "reason": "source_typed_relations_require_full_rebuild",
            "path": path,
            "typed_relation_count": len(typed_relations),
        }
    parsed_signature = _module_function_signature(module)
    active_signature = _node_function_signature(node)
    if parsed_signature != active_signature:
        return {
            "supported": False,
            "reason": "source_function_signature_changed",
            "path": path,
            "parsed_signature": parsed_signature,
            "active_signature": active_signature,
        }
    return {
        "supported": True,
        "reason": "source_hash_only_structure_stable",
        "path": path,
        "node_id": _node_id(node),
        "module": parsed_module,
    }


def _node_ids_for_paths(graph_json: dict[str, Any], paths: set[str]) -> list[str]:
    if not paths:
        return []
    node_ids: set[str] = set()
    for node in _graph_nodes(graph_json):
        node_id = _node_id(node)
        if not node_id:
            continue
        for role in ("primary", "secondary", "test", "config"):
            if paths.intersection(_path_values(node, role)):
                node_ids.add(node_id)
                break
    return sorted(node_ids)


def _edge_delta_payload(key: tuple[str, str, str, str]) -> dict[str, str]:
    source, target, relation_type, edge_kind = key
    return {
        "source": source,
        "target": target,
        "relation_type": relation_type,
        "edge_kind": edge_kind,
    }


def _mark_scope_file_delta_strategy(
    scope_file_delta: dict[str, Any],
    *,
    strategy: str,
    graph_delta_mode: str = "",
    fallback_reason: str = "",
) -> dict[str, Any]:
    out = dict(scope_file_delta)
    out["strategy"] = strategy
    if graph_delta_mode:
        out["graph_delta_mode"] = graph_delta_mode
    if fallback_reason:
        out["fallback_reason"] = fallback_reason
    return out


def _incremental_metadata_scope_eligibility(
    scope_file_delta: dict[str, Any],
    *,
    project_root: str | Path,
    active_graph_json: dict[str, Any],
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if scope_file_delta.get("added_files"):
        return {"supported": False, "reason": "added_files_require_full_rebuild"}
    if scope_file_delta.get("removed_files"):
        return {"supported": False, "reason": "removed_files_require_full_rebuild"}
    if scope_file_delta.get("status_changed_files"):
        return {"supported": False, "reason": "inventory_status_change_requires_full_rebuild"}

    hash_changed = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("hash_changed_files", [])
        if str(path or "").strip()
    }
    impacted = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("impacted_files", [])
        if str(path or "").strip()
    }
    if not impacted:
        return {"supported": False, "reason": "no_impacted_files"}
    if impacted - hash_changed:
        return {
            "supported": False,
            "reason": "non_hash_impacted_files_require_full_rebuild",
            "paths": sorted(impacted - hash_changed),
        }

    old_by_path = _rows_by_path(old_rows)
    new_by_path = _rows_by_path(new_rows)
    unsupported: list[dict[str, str]] = []
    source_checks: list[dict[str, Any]] = []
    source_paths: list[str] = []
    metadata_paths: list[str] = []
    test_paths: list[str] = []
    for path in sorted(impacted):
        old_row = old_by_path.get(path) or {}
        new_row = new_by_path.get(path) or {}
        kind = str(new_row.get("file_kind") or old_row.get("file_kind") or "")
        role = str(new_row.get("attachment_role") or old_row.get("attachment_role") or "")
        if kind == "test" and role != "primary":
            test_paths.append(path)
            continue
        if kind in _INCREMENTAL_METADATA_FILE_KINDS and role != "primary":
            metadata_paths.append(path)
            continue
        if kind == "source" and role == "primary":
            check = _source_path_incremental_eligibility(project_root, active_graph_json, path)
            source_checks.append(check)
            if not check.get("supported"):
                unsupported.append({
                    "path": path,
                    "file_kind": kind,
                    "attachment_role": role,
                    "reason": str(check.get("reason") or ""),
                })
            else:
                source_paths.append(path)
            continue
        if kind not in _INCREMENTAL_METADATA_FILE_KINDS or role == "primary":
            unsupported.append({"path": path, "file_kind": kind, "attachment_role": role})
    if unsupported:
        detailed_reasons = sorted({
            str(item.get("reason") or "")
            for item in unsupported
            if str(item.get("reason") or "")
        })
        return {
            "supported": False,
            "reason": detailed_reasons[0] if len(detailed_reasons) == 1 else "structural_or_unknown_file_requires_full_rebuild",
            "unsupported": unsupported,
            "source_checks": source_checks,
        }
    if test_paths and (source_paths or metadata_paths):
        return {
            "supported": False,
            "reason": "test_fanin_mixed_changes_require_full_rebuild",
            "test_paths": test_paths,
            "source_paths": source_paths,
            "metadata_paths": metadata_paths,
            "source_checks": source_checks,
        }
    mode = "metadata_only"
    if test_paths:
        mode = "test_fanin_hash_only"
    elif source_paths and metadata_paths:
        mode = "mixed_hash_only"
    elif source_paths:
        mode = "source_hash_only"
    return {
        "supported": True,
        "reason": "hash_only_structure_stable",
        "mode": mode,
        "source_paths": source_paths,
        "metadata_paths": metadata_paths,
        "test_paths": test_paths,
        "source_checks": source_checks,
    }


def _fanin_entry_path(entry: dict[str, Any]) -> str:
    return _norm_repo_path(entry.get("rel_path") or entry.get("path"))


def _graph_fanin_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": _fanin_entry_path(entry),
        "evidence": str(entry.get("evidence") or "test_import_fanin"),
        "imports": sorted({
            str(token or "").strip()
            for token in entry.get("imports", []) or []
            if str(token or "").strip()
        }),
    }


def _apply_incremental_test_fanin_bindings(
    project_root: str | Path,
    candidate_graph: dict[str, Any],
    *,
    changed_test_paths: list[str],
) -> dict[str, Any]:
    changed = {
        _norm_repo_path(path)
        for path in changed_test_paths
        if _norm_repo_path(path)
    }
    if not changed:
        return {"ok": True, "changed_test_paths": [], "updated_node_ids": []}

    from agent.governance.project_profile import discover_project_profile

    root = Path(project_root).resolve()
    profile = discover_project_profile(str(root))
    modules = parse_production_modules(str(root), profile=profile)
    fanin_index = build_test_consumer_fanin_index(str(root), modules, profile=profile)
    new_fanin_by_module: dict[str, list[dict[str, Any]]] = {}
    for module_name, entries in fanin_index.items():
        kept = [
            _graph_fanin_entry(entry)
            for entry in entries or []
            if _fanin_entry_path(entry) in changed
        ]
        if kept:
            new_fanin_by_module[str(module_name)] = kept

    updated_node_ids: list[str] = []
    updated_modules: list[str] = []
    for node in _deps_graph_nodes(candidate_graph):
        node_id = _node_id(node)
        metadata = _node_metadata(node)
        module_name = str(metadata.get("module") or node.get("module") or "")
        if not module_name:
            continue
        old_fanin = [
            dict(entry)
            for entry in metadata.get("test_consumer_fanin", []) or []
            if isinstance(entry, dict)
        ]
        old_changed_paths = {
            _norm_repo_path(entry.get("path"))
            for entry in old_fanin
            if _norm_repo_path(entry.get("path")) in changed
        }
        new_changed_fanin = new_fanin_by_module.get(module_name, [])
        if not old_changed_paths and not new_changed_fanin:
            continue

        kept_fanin = [
            _graph_fanin_entry(entry)
            for entry in old_fanin
            if _norm_repo_path(entry.get("path")) not in changed
        ]
        merged_fanin = sorted(
            kept_fanin + new_changed_fanin,
            key=lambda item: (str(item.get("path") or ""), ",".join(item.get("imports") or [])),
        )
        old_fanin_paths = {
            _norm_repo_path(entry.get("path"))
            for entry in old_fanin
            if _norm_repo_path(entry.get("path"))
        }
        direct_tests = {
            path for path in _path_values(node, "test")
            if path not in old_fanin_paths
        }
        fanin_paths = {
            _norm_repo_path(entry.get("path"))
            for entry in merged_fanin
            if _norm_repo_path(entry.get("path"))
        }
        new_tests = sorted(direct_tests | fanin_paths)
        old_tests = sorted(_path_values(node, "test"))
        old_normalized_fanin = sorted(
            [_graph_fanin_entry(entry) for entry in old_fanin],
            key=lambda item: (str(item.get("path") or ""), ",".join(item.get("imports") or [])),
        )
        if new_tests == old_tests and merged_fanin == old_normalized_fanin:
            continue
        node["test"] = new_tests
        node["test_coverage"] = "direct" if new_tests else "none"
        metadata["test_consumer_fanin"] = merged_fanin
        node["metadata"] = metadata
        if node_id:
            updated_node_ids.append(node_id)
        updated_modules.append(module_name)

    return {
        "ok": True,
        "changed_test_paths": sorted(changed),
        "updated_node_ids": sorted(set(updated_node_ids)),
        "updated_modules": sorted(set(updated_modules)),
    }


def _build_scope_graph_delta(
    *,
    old_graph_json: dict[str, Any],
    new_graph_json: dict[str, Any],
    scope_file_delta: dict[str, Any],
    strategy: str,
    mode: str,
    fallback_reason: str = "",
) -> dict[str, Any]:
    old_nodes = {_node_id(node): node for node in _graph_nodes(old_graph_json) if _node_id(node)}
    new_nodes = {_node_id(node): node for node in _graph_nodes(new_graph_json) if _node_id(node)}
    old_edges = {
        _edge_key(edge): edge
        for edge in graph_payload_edges(old_graph_json)
        if _edge_key(edge)[:3] != ("", "", "")
    }
    new_edges = {
        _edge_key(edge): edge
        for edge in graph_payload_edges(new_graph_json)
        if _edge_key(edge)[:3] != ("", "", "")
    }
    changed_paths = {
        str(path or "").replace("\\", "/").strip("/")
        for path in (
            list(scope_file_delta.get("hash_changed_files") or [])
            + list(scope_file_delta.get("status_changed_files") or [])
            + list(scope_file_delta.get("added_files") or [])
            + list(scope_file_delta.get("removed_files") or [])
        )
        if str(path or "").strip()
    }
    changed_node_ids = sorted(
        set(_node_ids_for_paths(old_graph_json, changed_paths))
        | set(_node_ids_for_paths(new_graph_json, changed_paths))
    )
    return {
        "strategy": strategy,
        "mode": mode,
        "fallback_reason": fallback_reason,
        "added_nodes": sorted(set(new_nodes) - set(old_nodes)),
        "updated_nodes": changed_node_ids,
        "removed_nodes": sorted(set(old_nodes) - set(new_nodes)),
        "added_edges": [
            _edge_delta_payload(key)
            for key in sorted(set(new_edges) - set(old_edges))
        ],
        "removed_edges": [
            _edge_delta_payload(key)
            for key in sorted(set(old_edges) - set(new_edges))
        ],
        "file_inventory_delta": {
            "added_files": list(scope_file_delta.get("added_files") or []),
            "removed_files": list(scope_file_delta.get("removed_files") or []),
            "hash_changed_files": list(scope_file_delta.get("hash_changed_files") or []),
            "status_changed_files": list(scope_file_delta.get("status_changed_files") or []),
            "impacted_files": list(scope_file_delta.get("impacted_files") or []),
            "changed_file_count": int(scope_file_delta.get("changed_file_count") or 0),
            "impacted_file_count": int(scope_file_delta.get("impacted_file_count") or 0),
        },
        "semantic_stale_node_ids": changed_node_ids,
    }


def _scope_event_id(event_type: str, target_type: str, target_id: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "event_type": event_type,
            "target_type": target_type,
            "target_id": target_id,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"scope-{event_type}-{digest}"


def _emit_scope_graph_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    old_snapshot_id: str,
    new_snapshot_id: str,
    old_graph_json: dict[str, Any],
    new_graph_json: dict[str, Any],
    scope_file_delta: dict[str, Any],
    baseline_commit: str,
    target_commit: str,
    created_by: str,
) -> dict[str, Any]:
    graph_events.ensure_schema(conn)
    old_nodes = {_node_id(node): node for node in _graph_nodes(old_graph_json) if _node_id(node)}
    new_nodes = {_node_id(node): node for node in _graph_nodes(new_graph_json) if _node_id(node)}
    changed_files = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("hash_changed_files", [])
        if str(path or "").strip()
    }
    emitted: list[dict[str, Any]] = []

    def emit(
        event_type: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any],
        *,
        stable_node_key: str = "",
        feature_hash: str = "",
        file_hashes: dict[str, Any] | None = None,
    ) -> None:
        event = graph_events.create_event(
            conn,
            project_id,
            new_snapshot_id,
            event_type=event_type,
            event_kind="scope_reconcile",
            target_type=target_type,
            target_id=target_id,
            status=graph_events.EVENT_STATUS_OBSERVED,
            confidence=1.0,
            baseline_commit=baseline_commit,
            target_commit=target_commit,
            stable_node_key=stable_node_key,
            feature_hash=feature_hash,
            file_hashes=file_hashes or {},
            payload=payload,
            evidence={
                "source": "scope_reconcile",
                "old_snapshot_id": old_snapshot_id,
                "new_snapshot_id": new_snapshot_id,
            },
            created_by=created_by,
            event_id=_scope_event_id(event_type, target_type, target_id, payload),
        )
        emitted.append(event)

    old_ids = set(old_nodes)
    new_ids = set(new_nodes)
    for node_id in sorted(new_ids - old_ids):
        node = new_nodes[node_id]
        metadata = _node_metadata(node)
        emit(
            "node_added",
            "node",
            node_id,
            {
                "node_id": node_id,
                "title": node.get("title") or "",
                "layer": node.get("layer") or "",
                "primary": _path_values(node, "primary"),
                "hierarchy_parent": _node_parent(node),
            },
            stable_node_key=str(metadata.get("stable_node_key") or ""),
            feature_hash=str(metadata.get("feature_hash") or ""),
        )
    for node_id in sorted(old_ids - new_ids):
        node = old_nodes[node_id]
        emit(
            "node_removed",
            "node",
            node_id,
            {
                "node_id": node_id,
                "title": node.get("title") or "",
                "layer": node.get("layer") or "",
                "primary": _path_values(node, "primary"),
                "hierarchy_parent": _node_parent(node),
            },
        )

    binding_events = {
        "secondary": ("doc_binding_added", "doc_binding_removed"),
        "test": ("test_binding_added", "test_binding_removed"),
        "config": ("config_binding_added", "config_binding_removed"),
    }
    for node_id in sorted(old_ids & new_ids):
        old_node = old_nodes[node_id]
        new_node = new_nodes[node_id]
        old_parent = _node_parent(old_node)
        new_parent = _node_parent(new_node)
        if old_parent != new_parent:
            emit(
                "node_reparented",
                "node",
                node_id,
                {"node_id": node_id, "old_parent": old_parent, "new_parent": new_parent},
            )
        metadata = _node_metadata(new_node)
        current_file_hashes = _node_file_hashes(new_node)
        for file_role in ("primary", "secondary", "test", "config"):
            role_changed = sorted(set(_path_values(new_node, file_role)).intersection(changed_files))
            if not role_changed:
                continue
            event_file_hashes = {
                path: current_file_hashes[path]
                for path in role_changed
                if path in current_file_hashes
            }
            emit(
                "file_hash_changed",
                "node",
                node_id,
                {"node_id": node_id, "files": role_changed, "file_role": file_role},
                stable_node_key=str(metadata.get("stable_node_key") or ""),
                feature_hash=str(metadata.get("feature_hash") or ""),
                file_hashes=event_file_hashes,
            )
        old_meta = _node_metadata(old_node)
        new_meta = metadata
        if new_meta.get("exclude_as_feature") is True and old_meta.get("exclude_as_feature") is not True:
            emit(
                "package_marker_excluded",
                "node",
                node_id,
                {
                    "node_id": node_id,
                    "primary": _path_values(new_node, "primary"),
                    "file_role": new_meta.get("file_role") or "",
                },
            )
        for key, (added_type, removed_type) in binding_events.items():
            old_paths = set(_path_values(old_node, key))
            new_paths = set(_path_values(new_node, key))
            for path in sorted(new_paths - old_paths):
                emit(added_type, "node", node_id, {"node_id": node_id, "path": path, "binding": key})
            for path in sorted(old_paths - new_paths):
                emit(removed_type, "node", node_id, {"node_id": node_id, "path": path, "binding": key})

    old_edges = {_edge_key(edge): edge for edge in graph_payload_edges(old_graph_json) if _edge_key(edge)[:3] != ("", "", "")}
    new_edges = {_edge_key(edge): edge for edge in graph_payload_edges(new_graph_json) if _edge_key(edge)[:3] != ("", "", "")}
    for key in sorted(set(new_edges) - set(old_edges)):
        source, target, relation, edge_kind = key
        emit(
            "edge_added",
            "edge",
            f"{source}->{target}:{relation}",
            {
                "source": source,
                "target": target,
                "relation_type": relation,
                "edge_kind": edge_kind,
                "edge": new_edges[key],
            },
        )
    for key in sorted(set(old_edges) - set(new_edges)):
        source, target, relation, edge_kind = key
        emit(
            "edge_removed",
            "edge",
            f"{source}->{target}:{relation}",
            {
                "source": source,
                "target": target,
                "relation_type": relation,
                "edge_kind": edge_kind,
                "edge": old_edges[key],
            },
        )

    by_type: dict[str, int] = {}
    for event in emitted:
        event_type = str(event.get("event_type") or "")
        by_type[event_type] = by_type.get(event_type, 0) + 1
    return {
        "enabled": True,
        "event_count": len(emitted),
        "by_type": dict(sorted(by_type.items())),
        "snapshot_id": new_snapshot_id,
        "old_snapshot_id": old_snapshot_id,
        "target_commit": target_commit,
    }


def _semantic_enrichment_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    summary = dict(result.get("summary") or {})
    return {
        "ok": bool(result.get("ok")),
        "feedback_round": result.get("feedback_round", 0),
        "semantic_index_path": result.get("semantic_index_path", ""),
        "review_report_path": result.get("review_report_path", ""),
        "round_semantic_index_path": result.get("round_semantic_index_path", ""),
        "round_review_report_path": result.get("round_review_report_path", ""),
        "feature_count": summary.get("feature_count", 0),
        "semantic_run_status": summary.get("semantic_run_status", ""),
        "ai_complete_count": summary.get("ai_complete_count", 0),
        "ai_unavailable_count": summary.get("ai_unavailable_count", 0),
        "ai_error_count": summary.get("ai_error_count", 0),
        "ai_skipped_count": summary.get("ai_skipped_count", 0),
        "feedback_count": summary.get("feedback_count", 0),
        "unresolved_feedback_count": summary.get("unresolved_feedback_count", 0),
        "quality_flag_counts": summary.get("quality_flag_counts") or {},
        "feature_payload_input_count": summary.get("feature_payload_input_count", 0),
        "feature_payload_output_count": summary.get("feature_payload_output_count", 0),
        "feature_payload_input_dir": summary.get("feature_payload_input_dir", ""),
        "feature_payload_output_dir": summary.get("feature_payload_output_dir", ""),
        "batch_payload_input_dir": summary.get("batch_payload_input_dir", ""),
        "batch_payload_output_dir": summary.get("batch_payload_output_dir", ""),
        "ai_selected_count": summary.get("ai_selected_count", 0),
        "ai_attempted_count": summary.get("ai_attempted_count", 0),
        "ai_skipped_selector_count": summary.get("ai_skipped_selector_count", 0),
        "semantic_hash_mismatch_count": summary.get("semantic_hash_mismatch_count", 0),
        "ai_input_mode": summary.get("ai_input_mode", ""),
        "dynamic_semantic_graph_state": summary.get("dynamic_semantic_graph_state", False),
        "requested_ai_batch_size": summary.get("requested_ai_batch_size"),
        "ai_batch_size": summary.get("ai_batch_size", 1),
        "ai_batch_by": summary.get("ai_batch_by", ""),
        "ai_batch_count": summary.get("ai_batch_count", 0),
        "ai_batch_complete_count": summary.get("ai_batch_complete_count", 0),
        "ai_batch_error_count": summary.get("ai_batch_error_count", 0),
        "semantic_graph_state": summary.get("semantic_graph_state") or {},
        "semantic_batch_memory": summary.get("semantic_batch_memory") or {},
        "semantic_selector": summary.get("semantic_selector") or {},
    }


def run_state_only_full_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    run_id: str = "",
    commit_sha: str = "",
    snapshot_id: str | None = None,
    snapshot_kind: str = "full",
    created_by: str = "observer",
    activate: bool = False,
    expected_old_snapshot_id: str | None = None,
    notes_extra: dict[str, Any] | None = None,
    semantic_enrich: bool = True,
    semantic_use_ai: bool | None = None,
    semantic_feedback_items: list[dict[str, Any]] | dict[str, Any] | None = None,
    semantic_feedback_round: int | None = None,
    semantic_max_excerpt_chars: int | None = None,
    semantic_ai_call: Any = None,
    semantic_ai_feature_limit: int | None = None,
    semantic_ai_provider: str | None = None,
    semantic_ai_model: str | None = None,
    semantic_ai_role: str | None = None,
    semantic_ai_chain_role: str | None = None,
    semantic_analyzer_role: str | None = None,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_ai_input_mode: str | None = None,
    semantic_dynamic_graph_state: bool | None = None,
    semantic_graph_state: bool = True,
    semantic_skip_completed: bool = True,
    semantic_classify_feedback: bool = True,
    semantic_batch_memory: bool | None = False,
    semantic_batch_memory_id: str | None = None,
    semantic_base_snapshot_id: str | None = None,
    semantic_config_path: str | Path | None = None,
    semantic_enqueue_stale: bool = True,
) -> dict[str, Any]:
    """Create a candidate full-reconcile graph snapshot from current files.

    The function writes only governance artifacts under shared governance state.
    It leaves repository files untouched and keeps activation optional.
    """
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    commit = commit_sha or _git_commit(root) or "unknown"
    sid = snapshot_id or snapshot_id_for(snapshot_kind, commit)
    rid = run_id or sid
    checkout_provenance = describe_checkout(
        root,
        project_id=project_id,
        commit_sha=commit,
    )
    state_dir = _governance_state_dir(project_id, rid)
    scratch_dir = state_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    trace = ReconcileTrace(
        project_id=project_id,
        run_id=rid,
        snapshot_id=sid,
        trace_dir=state_dir / "trace",
    )
    trace.step(
        "run-input",
        input_payload={
            "project_id": project_id,
            "project_root": str(root),
            "project_root_role": "execution_root",
            "checkout_provenance": checkout_provenance,
            "run_id": rid,
            "snapshot_id": sid,
            "snapshot_kind": snapshot_kind,
            "commit_sha": commit,
            "created_by": created_by,
        },
        output_payload={
            "state_dir": str(state_dir),
            "scratch_dir": str(scratch_dir),
            "semantic_enrich": semantic_enrich,
            "semantic_use_ai": semantic_use_ai,
            "semantic_ai_feature_limit": semantic_ai_feature_limit,
            "semantic_ai_batch_size": semantic_ai_batch_size,
            "semantic_ai_batch_by": semantic_ai_batch_by,
            "semantic_ai_input_mode": semantic_ai_input_mode,
            "semantic_dynamic_graph_state": semantic_dynamic_graph_state,
            "semantic_graph_state": semantic_graph_state,
            "semantic_skip_completed": semantic_skip_completed,
            "semantic_batch_memory": semantic_batch_memory,
            "semantic_base_snapshot_id": semantic_base_snapshot_id,
            "semantic_ai_provider": semantic_ai_provider,
            "semantic_ai_model": semantic_ai_model,
            "semantic_ai_role": semantic_ai_role,
            "semantic_ai_chain_role": semantic_ai_chain_role,
            "semantic_analyzer_role": semantic_analyzer_role,
            "semantic_ai_scope": semantic_ai_scope,
            "semantic_node_ids": semantic_node_ids,
            "semantic_layers": semantic_layers,
            "semantic_quality_flags": semantic_quality_flags,
            "semantic_missing": semantic_missing,
        },
    )

    phase_result = build_graph_v2_from_symbols(
        str(root),
        dry_run=True,
        scratch_dir=str(scratch_dir),
        run_id=rid,
    )
    trace.step(
        "build-graph-v2",
        input_payload={
            "project_root": str(root),
            "dry_run": True,
            "scratch_dir": str(scratch_dir),
            "run_id": rid,
        },
        output_payload={
            "status": phase_result.get("status", ""),
            "report_path": phase_result.get("report_path") or "",
            "report": artifact_ref(phase_result.get("report_path") or ""),
            "node_count": phase_result.get("node_count", 0),
            "feature_cluster_count": len(phase_result.get("feature_clusters") or []),
            "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
            "typed_relation_count": len(phase_result.get("typed_relations") or []),
        },
        status="ok" if phase_result.get("status") == "ok" else "failed",
    )
    if phase_result.get("status") != "ok":
        trace_summary = trace.finalize(status="failed", extra={"abort_reason": phase_result.get("abort_reason", "")})
        return {
            "ok": False,
            "project_id": project_id,
            "run_id": rid,
            "commit_sha": commit,
            "status": phase_result.get("status", "unknown"),
            "abort_reason": phase_result.get("abort_reason", ""),
            "phase_result": phase_result,
            "trace": trace_summary,
        }

    candidate_graph = build_rebase_candidate_graph(
        str(root),
        phase_result,
        session_id=rid,
        run_id=rid,
    )
    trace.step(
        "build-candidate-graph",
        input_payload={
            "phase_report_path": phase_result.get("report_path") or "",
            "session_id": rid,
        },
        output_payload={
            "graph_stats": graph_payload_stats(candidate_graph),
        },
    )
    active_snapshot_for_corrections = get_active_graph_snapshot(conn, project_id) or {}
    role_annotation = annotate_graph_node_roles(candidate_graph)
    candidate_graph = role_annotation["graph"]
    relationship_metrics = annotate_graph_relationship_metrics(candidate_graph)
    candidate_graph = relationship_metrics["graph"]
    ensure_graph_correction_schema(conn)
    replayable_patches = list_replayable_patches(conn, project_id)
    patch_application = apply_correction_patches(
        candidate_graph,
        replayable_patches,
        from_snapshot_id=str(active_snapshot_for_corrections.get("snapshot_id") or ""),
        to_snapshot_id=sid,
    )
    candidate_graph = patch_application["graph"]
    trace.step(
        "graph-correction-patches",
        input_payload={
            "active_snapshot_id": active_snapshot_for_corrections.get("snapshot_id", ""),
            "patch_count": len(replayable_patches),
        },
        output_payload={
            "file_role_annotation": role_annotation["report"],
            "relationship_metrics": relationship_metrics["report"],
            "patch_report": patch_application["report"],
            "graph_stats": graph_payload_stats(candidate_graph),
        },
    )
    file_inventory = _normalize_inventory_commit(
        [
            row for row in (phase_result.get("file_inventory") or [])
            if isinstance(row, dict)
        ],
        commit_sha=commit,
    )
    trace.step(
        "normalize-file-inventory",
        input_payload={
            "raw_file_inventory_count": len(phase_result.get("file_inventory") or []),
            "commit_sha": commit,
        },
        output_payload={
            "file_inventory_count": len(file_inventory),
            "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
        },
    )
    nodes = _deps_graph_nodes(candidate_graph)
    edges = _deps_graph_edges(candidate_graph)
    notes = {
        "state_only": True,
        "run_id": rid,
        "snapshot_kind": snapshot_kind,
        "phase_report_path": phase_result.get("report_path") or "",
        "phase_node_count": phase_result.get("node_count", 0),
        "feature_cluster_count": len(phase_result.get("feature_clusters") or []),
        "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
        "file_role_annotation": role_annotation["report"],
        "relationship_metrics": relationship_metrics["report"],
        "graph_correction_patch_report": patch_application["report"],
        "checkout_provenance": checkout_provenance,
        **(notes_extra or {}),
    }
    notes["trace"] = {
        "trace_dir": str(trace.trace_dir),
        "summary_path": str(trace.trace_dir / "summary.json"),
    }
    governance_index = build_governance_index(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=commit,
        candidate_graph=candidate_graph,
        snapshot_id=sid,
        snapshot_kind=snapshot_kind,
        file_inventory=file_inventory,
    )
    hash_metadata_merge = merge_feature_hashes_into_graph_nodes(candidate_graph, governance_index)
    enriched_inventory = governance_index.get("file_inventory")
    if isinstance(enriched_inventory, list):
        file_inventory = _normalize_inventory_commit(
            [row for row in enriched_inventory if isinstance(row, dict)],
            commit_sha=commit,
        )
        notes["file_inventory_summary"] = governance_index.get("file_inventory_summary") or {}
    notes["governance_hint_bindings"] = governance_index.get("governance_hint_bindings") or {}
    notes["governance_index_hash_metadata"] = hash_metadata_merge
    nodes = _deps_graph_nodes(candidate_graph)
    edges = _deps_graph_edges(candidate_graph)
    with sqlite_write_lock():
        snapshot = create_graph_snapshot(
            conn,
            project_id,
            snapshot_id=sid,
            commit_sha=commit,
            snapshot_kind=snapshot_kind,
            graph_json=candidate_graph,
            file_inventory=file_inventory,
            drift_ledger=[],
            status=SNAPSHOT_STATUS_CANDIDATE,
            created_by=created_by,
            notes=json.dumps(notes, ensure_ascii=False, sort_keys=True),
        )
        index_counts = index_graph_snapshot(
            conn,
            project_id,
            sid,
            nodes=nodes,
            edges=edges,
        )
        governance_index_summary = persist_governance_index(
            conn,
            project_id,
            governance_index,
            persist_inventory=True,
        )
        migration_count = persist_node_migrations(
            conn,
            project_id,
            from_snapshot_id=str(active_snapshot_for_corrections.get("snapshot_id") or ""),
            to_snapshot_id=sid,
            migrations=patch_application["report"].get("migrations") or [],
        )
        patch_apply_counts = record_patch_apply_report(
            conn,
            project_id,
            snapshot_id=sid,
            report=patch_application["report"],
        )
        notes["graph_correction_patch_report"]["migration_count"] = migration_count
        notes["graph_correction_patch_report"]["patch_apply_counts"] = patch_apply_counts
        notes["governance_index"] = governance_index_summary
        conn.execute(
            "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
            (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
        )
        conn.commit()
    governance_index = {**governance_index, "persist_summary": governance_index_summary}
    trace.step(
        "create-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "snapshot_kind": snapshot_kind,
            "commit_sha": commit,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "file_inventory_count": len(file_inventory),
        },
        output_payload={
            "snapshot": snapshot,
            "snapshot_path": str(snapshot_graph_path(project_id, sid)),
            "snapshot_artifact": artifact_ref(snapshot_graph_path(project_id, sid)),
        },
    )
    trace.step(
        "index-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        output_payload={"index_counts": index_counts},
    )
    trace.step(
        "build-governance-index",
        input_payload={
            "snapshot_id": sid,
            "run_id": rid,
            "commit_sha": commit,
        },
        output_payload={
            "summary": governance_index_summary,
            "artifacts": governance_index_summary.get("artifacts") or {},
            "hash_metadata_merge": hash_metadata_merge,
        },
    )
    semantic_enrichment: dict[str, Any] = {}
    if semantic_enrich:
        semantic_result = run_semantic_enrichment(
            conn,
            project_id,
            sid,
            root,
            feedback_items=semantic_feedback_items,
            feedback_round=semantic_feedback_round,
            use_ai=semantic_use_ai,
            ai_call=semantic_ai_call,
            created_by=created_by,
            max_excerpt_chars=semantic_max_excerpt_chars,
            semantic_ai_provider=semantic_ai_provider,
            semantic_ai_model=semantic_ai_model,
            semantic_ai_role=semantic_ai_role,
            semantic_ai_chain_role=semantic_ai_chain_role,
            semantic_analyzer_role=semantic_analyzer_role,
            ai_feature_limit=semantic_ai_feature_limit,
            semantic_ai_batch_size=semantic_ai_batch_size,
            semantic_ai_batch_by=semantic_ai_batch_by,
            semantic_ai_input_mode=semantic_ai_input_mode,
            semantic_dynamic_graph_state=semantic_dynamic_graph_state,
            semantic_graph_state=semantic_graph_state,
            semantic_skip_completed=semantic_skip_completed,
            semantic_batch_memory=semantic_batch_memory,
            semantic_batch_memory_id=semantic_batch_memory_id,
            semantic_base_snapshot_id=semantic_base_snapshot_id,
            semantic_ai_scope=semantic_ai_scope,
            semantic_node_ids=semantic_node_ids,
            semantic_layers=semantic_layers,
            semantic_quality_flags=semantic_quality_flags,
            semantic_missing=semantic_missing,
            semantic_changed_paths=semantic_changed_paths,
            semantic_path_prefixes=semantic_path_prefixes,
            semantic_selector_match=semantic_selector_match,
            semantic_include_structural=semantic_include_structural,
            semantic_config_path=semantic_config_path,
            trace_dir=trace.trace_dir / "semantic-enrichment",
            enqueue_stale=semantic_enqueue_stale,
        )
        semantic_enrichment = _semantic_enrichment_summary(semantic_result)
        if semantic_classify_feedback:
            from agent.governance import reconcile_feedback
            from agent.governance import reconcile_semantic_enrichment

            review_gate = reconcile_semantic_enrichment.feedback_review_gate(
                semantic_result.get("summary") or {},
            )
            if review_gate.get("allowed"):
                semantic_enrichment["feedback_queue"] = reconcile_feedback.classify_semantic_state_rounds(
                    project_id,
                    sid,
                    created_by=created_by,
                    base_snapshot_id=semantic_base_snapshot_id or "",
                )
            else:
                semantic_enrichment["feedback_queue"] = {
                    "blocked": True,
                    "gate": review_gate,
                }
    trace.step(
        "semantic-enrichment",
        input_payload={
            "enabled": semantic_enrich,
            "snapshot_id": sid,
            "semantic_use_ai": semantic_use_ai,
            "semantic_ai_feature_limit": semantic_ai_feature_limit,
            "semantic_ai_batch_size": semantic_ai_batch_size,
            "semantic_ai_batch_by": semantic_ai_batch_by,
            "semantic_ai_input_mode": semantic_ai_input_mode,
            "semantic_dynamic_graph_state": semantic_dynamic_graph_state,
            "semantic_graph_state": semantic_graph_state,
            "semantic_skip_completed": semantic_skip_completed,
            "semantic_classify_feedback": semantic_classify_feedback,
            "semantic_batch_memory": semantic_batch_memory,
            "semantic_base_snapshot_id": semantic_base_snapshot_id,
            "semantic_ai_provider": semantic_ai_provider,
            "semantic_ai_model": semantic_ai_model,
            "semantic_ai_role": semantic_ai_role,
            "semantic_ai_chain_role": semantic_ai_chain_role,
            "semantic_analyzer_role": semantic_analyzer_role,
            "semantic_ai_scope": semantic_ai_scope,
            "semantic_node_ids": semantic_node_ids,
            "semantic_layers": semantic_layers,
            "semantic_quality_flags": semantic_quality_flags,
            "semantic_missing": semantic_missing,
            "semantic_changed_paths": semantic_changed_paths,
            "semantic_path_prefixes": semantic_path_prefixes,
            "semantic_selector_match": semantic_selector_match,
            "semantic_include_structural": semantic_include_structural,
            "semantic_config_path": str(semantic_config_path or ""),
        },
        output_payload=semantic_enrichment,
    )
    activation = None
    if activate:
        with sqlite_write_lock():
            activation = activate_graph_snapshot(
                conn,
                project_id,
                sid,
                expected_old_snapshot_id=expected_old_snapshot_id,
            )
            conn.commit()
        trace.step(
            "activate-snapshot",
            input_payload={
                "snapshot_id": sid,
                "expected_old_snapshot_id": expected_old_snapshot_id,
            },
            output_payload={"activation": activation},
        )
    else:
        trace.step(
            "activate-snapshot",
            input_payload={"snapshot_id": sid, "activate": False},
            output_payload={"activation": None, "status": "skipped"},
            status="skipped",
        )
    trace_summary = trace.finalize(status="ok")
    return {
        "ok": True,
        "project_id": project_id,
        "run_id": rid,
        "commit_sha": commit,
        "snapshot_id": sid,
        "snapshot_status": "active" if activation else SNAPSHOT_STATUS_CANDIDATE,
        "snapshot_path": str(snapshot_graph_path(project_id, sid)),
        "phase_report_path": phase_result.get("report_path") or "",
        "graph_stats": graph_payload_stats(candidate_graph),
        "index_counts": index_counts,
        "governance_index": governance_index_summary,
        "semantic_enrichment": semantic_enrichment,
        "trace": trace_summary,
        "file_inventory_count": len(file_inventory),
        "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
        "feature_cluster_count": len(phase_result.get("feature_clusters") or []),
        "snapshot": snapshot,
        "activation": activation,
    }


def _pending_commits_through_target(
    pending: list[dict[str, Any]],
    target_commit_sha: str,
) -> list[str]:
    commits = [
        str(row.get("commit_sha") or "").strip()
        for row in pending
        if str(row.get("commit_sha") or "").strip()
    ]
    if target_commit_sha in commits:
        return commits[: commits.index(target_commit_sha) + 1]
    return commits


def _update_pending_scope_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    covered_commit_shas: list[str],
    snapshot_id: str,
    target_commit_sha: str,
    run_id: str,
    activated: bool = False,
) -> int:
    """Mark pending_scope_reconcile rows bound to the just-built candidate.

    OPT-BACKLOG-PENDING-SCOPE-TRANSITION-MISSING: when `activated` is True the
    candidate snapshot has already replaced the active snapshot, so the
    pending row is fully materialized and shouldn't stay in `running`. When
    False the candidate is parked awaiting an explicit activate call, so
    `running` is the correct interim state.
    """
    commits = [c for c in covered_commit_shas if c]
    if not commits:
        return 0
    placeholders = ",".join("?" for _ in commits)
    final_status = PENDING_STATUS_MATERIALIZED if activated else PENDING_STATUS_RUNNING
    evidence = {
        "source": "pending_scope_materializer",
        "snapshot_id": snapshot_id,
        "target_commit_sha": target_commit_sha,
        "run_id": run_id,
        "covered_commit_shas": commits,
        "activated": bool(activated),
        "final_status": final_status,
    }
    cur = conn.execute(
        f"""
        UPDATE pending_scope_reconcile
        SET status = ?,
            snapshot_id = ?,
            evidence_json = ?
        WHERE project_id = ?
          AND commit_sha IN ({placeholders})
          AND status IN (?, ?, ?)
        """,
        (
            final_status,
            snapshot_id,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            project_id,
            *commits,
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ),
    )
    return int(cur.rowcount or 0)


def _has_semantic_selector_override(*values: Any) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, set, dict)) and not value:
            continue
        return True
    return False


def _run_scope_semantic_enrichment(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    root: Path,
    *,
    created_by: str,
    semantic_options: dict[str, Any],
    trace: ReconcileTrace,
) -> dict[str, Any]:
    semantic_enrich = bool(semantic_options.get("semantic_enrich", True))
    semantic_enrichment: dict[str, Any] = {}
    if semantic_enrich:
        semantic_result = run_semantic_enrichment(
            conn,
            project_id,
            snapshot_id,
            root,
            feedback_items=semantic_options.get("semantic_feedback_items"),
            feedback_round=semantic_options.get("semantic_feedback_round"),
            use_ai=semantic_options.get("semantic_use_ai"),
            ai_call=semantic_options.get("semantic_ai_call"),
            created_by=created_by,
            max_excerpt_chars=semantic_options.get("semantic_max_excerpt_chars"),
            semantic_ai_provider=semantic_options.get("semantic_ai_provider"),
            semantic_ai_model=semantic_options.get("semantic_ai_model"),
            semantic_ai_role=semantic_options.get("semantic_ai_role"),
            semantic_ai_chain_role=semantic_options.get("semantic_ai_chain_role"),
            semantic_analyzer_role=semantic_options.get("semantic_analyzer_role"),
            ai_feature_limit=semantic_options.get("semantic_ai_feature_limit"),
            semantic_ai_batch_size=semantic_options.get("semantic_ai_batch_size"),
            semantic_ai_batch_by=semantic_options.get("semantic_ai_batch_by", "subsystem"),
            semantic_ai_input_mode=semantic_options.get("semantic_ai_input_mode"),
            semantic_dynamic_graph_state=semantic_options.get("semantic_dynamic_graph_state"),
            semantic_graph_state=bool(semantic_options.get("semantic_graph_state", True)),
            semantic_skip_completed=bool(semantic_options.get("semantic_skip_completed", True)),
            semantic_batch_memory=semantic_options.get("semantic_batch_memory", False),
            semantic_batch_memory_id=semantic_options.get("semantic_batch_memory_id"),
            semantic_base_snapshot_id=semantic_options.get("semantic_base_snapshot_id") or "",
            semantic_ai_scope=semantic_options.get("semantic_ai_scope"),
            semantic_node_ids=semantic_options.get("semantic_node_ids"),
            semantic_layers=semantic_options.get("semantic_layers"),
            semantic_quality_flags=semantic_options.get("semantic_quality_flags"),
            semantic_missing=semantic_options.get("semantic_missing"),
            semantic_changed_paths=semantic_options.get("semantic_changed_paths"),
            semantic_path_prefixes=semantic_options.get("semantic_path_prefixes"),
            semantic_selector_match=semantic_options.get("semantic_selector_match"),
            semantic_include_structural=bool(semantic_options.get("semantic_include_structural", False)),
            semantic_config_path=semantic_options.get("semantic_config_path"),
            trace_dir=trace.trace_dir / "semantic-enrichment",
            enqueue_stale=bool(semantic_options.get("semantic_enqueue_stale", True)),
        )
        semantic_enrichment = _semantic_enrichment_summary(semantic_result)
        if bool(semantic_options.get("semantic_classify_feedback", True)):
            from agent.governance import reconcile_feedback
            from agent.governance import reconcile_semantic_enrichment

            review_gate = reconcile_semantic_enrichment.feedback_review_gate(
                semantic_result.get("summary") or {},
            )
            if review_gate.get("allowed"):
                semantic_enrichment["feedback_queue"] = reconcile_feedback.classify_semantic_state_rounds(
                    project_id,
                    snapshot_id,
                    created_by=created_by,
                    base_snapshot_id=semantic_options.get("semantic_base_snapshot_id") or "",
                )
            else:
                semantic_enrichment["feedback_queue"] = {
                    "blocked": True,
                    "gate": review_gate,
                }
    trace.step(
        "semantic-enrichment",
        input_payload={
            "enabled": semantic_enrich,
            "snapshot_id": snapshot_id,
            "semantic_use_ai": semantic_options.get("semantic_use_ai"),
            "semantic_ai_feature_limit": semantic_options.get("semantic_ai_feature_limit"),
            "semantic_ai_batch_size": semantic_options.get("semantic_ai_batch_size"),
            "semantic_ai_batch_by": semantic_options.get("semantic_ai_batch_by", "subsystem"),
            "semantic_ai_input_mode": semantic_options.get("semantic_ai_input_mode"),
            "semantic_dynamic_graph_state": semantic_options.get("semantic_dynamic_graph_state"),
            "semantic_graph_state": semantic_options.get("semantic_graph_state", True),
            "semantic_skip_completed": semantic_options.get("semantic_skip_completed", True),
            "semantic_classify_feedback": semantic_options.get("semantic_classify_feedback", True),
            "semantic_batch_memory": semantic_options.get("semantic_batch_memory", False),
            "semantic_base_snapshot_id": semantic_options.get("semantic_base_snapshot_id") or "",
            "semantic_ai_provider": semantic_options.get("semantic_ai_provider"),
            "semantic_ai_model": semantic_options.get("semantic_ai_model"),
            "semantic_ai_role": semantic_options.get("semantic_ai_role"),
            "semantic_ai_chain_role": semantic_options.get("semantic_ai_chain_role"),
            "semantic_analyzer_role": semantic_options.get("semantic_analyzer_role"),
            "semantic_ai_scope": semantic_options.get("semantic_ai_scope"),
            "semantic_node_ids": semantic_options.get("semantic_node_ids"),
            "semantic_layers": semantic_options.get("semantic_layers"),
            "semantic_quality_flags": semantic_options.get("semantic_quality_flags"),
            "semantic_missing": semantic_options.get("semantic_missing"),
            "semantic_changed_paths": semantic_options.get("semantic_changed_paths"),
            "semantic_path_prefixes": semantic_options.get("semantic_path_prefixes"),
            "semantic_selector_match": semantic_options.get("semantic_selector_match"),
            "semantic_include_structural": semantic_options.get("semantic_include_structural", False),
            "semantic_config_path": str(semantic_options.get("semantic_config_path") or ""),
        },
        output_payload=semantic_enrichment,
    )
    return semantic_enrichment


def _run_incremental_metadata_scope_reconcile_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    root: Path,
    *,
    target: str,
    rid: str,
    sid: str,
    active: dict[str, Any],
    active_inventory: list[dict[str, Any]],
    changed_files: list[str],
    checkout_provenance: dict[str, Any],
    created_by: str,
    activate: bool,
    semantic_options: dict[str, Any],
) -> dict[str, Any]:
    active_snapshot_id = str(active.get("snapshot_id") or "")
    active_commit = str(active.get("commit_sha") or "")
    active_graph_json = _read_snapshot_graph(project_id, active_snapshot_id)
    if not active_snapshot_id or not _deps_graph_nodes(active_graph_json):
        return {"ok": False, "fallback_reason": "no_active_graph_payload"}

    candidate_graph = _json_clone(active_graph_json)
    if isinstance(candidate_graph, dict):
        candidate_graph["run_id"] = rid
        candidate_graph["session_id"] = rid
        metadata = dict(candidate_graph.get("metadata") or {})
        metadata["incremental_scope_reconcile"] = {
            "strategy": "incremental_graph_delta",
            "mode": "metadata_only",
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "target_commit": target,
        }
        candidate_graph["metadata"] = metadata

    governance_index = build_governance_index(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=target,
        candidate_graph=candidate_graph,
        snapshot_id=sid,
        snapshot_kind="scope",
    )
    file_inventory = _normalize_inventory_commit(
        [
            row for row in (governance_index.get("file_inventory") or [])
            if isinstance(row, dict)
        ],
        commit_sha=target,
    )
    scope_file_delta = _build_scope_file_delta(
        project_root=root,
        old_rows=active_inventory,
        new_rows=file_inventory,
        changed_files=changed_files,
    )
    eligibility = _incremental_metadata_scope_eligibility(
        scope_file_delta,
        project_root=root,
        active_graph_json=active_graph_json,
        old_rows=active_inventory,
        new_rows=file_inventory,
    )
    if not eligibility.get("supported"):
        return {
            "ok": False,
            "fallback_reason": str(eligibility.get("reason") or "incremental_metadata_unsupported"),
            "incremental_eligibility": eligibility,
        }
    graph_delta_mode = str(eligibility.get("mode") or "metadata_only")
    if isinstance(candidate_graph, dict):
        metadata = dict(candidate_graph.get("metadata") or {})
        incremental_metadata = dict(metadata.get("incremental_scope_reconcile") or {})
        incremental_metadata["mode"] = graph_delta_mode
        metadata["incremental_scope_reconcile"] = incremental_metadata
        candidate_graph["metadata"] = metadata
    test_fanin_update: dict[str, Any] = {}
    if graph_delta_mode == "test_fanin_hash_only":
        test_fanin_update = _apply_incremental_test_fanin_bindings(
            root,
            candidate_graph,
            changed_test_paths=[
                str(path)
                for path in eligibility.get("test_paths", []) or []
                if str(path).strip()
            ],
        )
        governance_index = build_governance_index(
            conn,
            project_id,
            root,
            run_id=rid,
            commit_sha=target,
            candidate_graph=candidate_graph,
            snapshot_id=sid,
            snapshot_kind="scope",
        )
        file_inventory = _normalize_inventory_commit(
            [
                row for row in (governance_index.get("file_inventory") or [])
                if isinstance(row, dict)
            ],
            commit_sha=target,
        )
        scope_file_delta = _build_scope_file_delta(
            project_root=root,
            old_rows=active_inventory,
            new_rows=file_inventory,
            changed_files=changed_files,
        )
        final_eligibility = _incremental_metadata_scope_eligibility(
            scope_file_delta,
            project_root=root,
            active_graph_json=active_graph_json,
            old_rows=active_inventory,
            new_rows=file_inventory,
        )
        if not final_eligibility.get("supported"):
            return {
                "ok": False,
                "fallback_reason": str(final_eligibility.get("reason") or "incremental_test_fanin_unsupported"),
                "incremental_eligibility": final_eligibility,
                "test_fanin_update": test_fanin_update,
            }
        graph_delta_mode = str(final_eligibility.get("mode") or graph_delta_mode)
        eligibility = {
            **final_eligibility,
            "test_fanin_update": test_fanin_update,
        }

    state_dir = _governance_state_dir(project_id, rid)
    scratch_dir = state_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    trace = ReconcileTrace(
        project_id=project_id,
        run_id=rid,
        snapshot_id=sid,
        trace_dir=state_dir / "trace",
    )
    trace.step(
        "run-input",
        input_payload={
            "project_id": project_id,
            "project_root": str(root),
            "project_root_role": "execution_root",
            "checkout_provenance": checkout_provenance,
            "run_id": rid,
            "snapshot_id": sid,
            "snapshot_kind": "scope",
            "commit_sha": target,
            "created_by": created_by,
            "scope_reconcile_strategy": "incremental_graph_delta",
            "scope_graph_delta_mode": graph_delta_mode,
        },
        output_payload={
            "state_dir": str(state_dir),
            "scratch_dir": str(scratch_dir),
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "changed_files": changed_files,
            "semantic_enrich": semantic_options.get("semantic_enrich", True),
        },
    )
    trace.step(
        "reuse-active-graph",
        input_payload={
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "target_commit": target,
        },
        output_payload={
            "graph_stats": graph_payload_stats(candidate_graph),
            "eligibility": eligibility,
            "test_fanin_update": test_fanin_update,
        },
    )
    hash_metadata_merge = merge_feature_hashes_into_graph_nodes(candidate_graph, governance_index)
    file_inventory = _normalize_inventory_commit(
        [
            row for row in (governance_index.get("file_inventory") or [])
            if isinstance(row, dict)
        ],
        commit_sha=target,
    )
    enriched_inventory = governance_index.get("file_inventory")
    if isinstance(enriched_inventory, list):
        file_inventory = _normalize_inventory_commit(
            [row for row in enriched_inventory if isinstance(row, dict)],
            commit_sha=target,
        )
    nodes = _deps_graph_nodes(candidate_graph)
    edges = _deps_graph_edges(candidate_graph)
    scope_file_delta = _mark_scope_file_delta_strategy(
        scope_file_delta,
        strategy="incremental_graph_delta",
        graph_delta_mode=graph_delta_mode,
    )
    scope_graph_delta = _build_scope_graph_delta(
        old_graph_json=active_graph_json,
        new_graph_json=candidate_graph,
        scope_file_delta=scope_file_delta,
        strategy="incremental_graph_delta",
        mode=graph_delta_mode,
    )
    notes = {
        "state_only": True,
        "run_id": rid,
        "snapshot_kind": "scope",
        "scope_reconcile_strategy": "incremental_graph_delta",
        "scope_graph_delta_mode": graph_delta_mode,
        "incremental_scope_reconcile": {
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "target_commit": target,
            "eligibility": eligibility,
        },
        "scope_file_delta": scope_file_delta,
        "scope_graph_delta": scope_graph_delta,
        "file_inventory_summary": governance_index.get("file_inventory_summary") or {},
        "governance_hint_bindings": governance_index.get("governance_hint_bindings") or {},
        "governance_index_hash_metadata": hash_metadata_merge,
        "checkout_provenance": checkout_provenance,
    }
    notes["trace"] = {
        "trace_dir": str(trace.trace_dir),
        "summary_path": str(trace.trace_dir / "summary.json"),
    }
    with sqlite_write_lock():
        snapshot = create_graph_snapshot(
            conn,
            project_id,
            snapshot_id=sid,
            commit_sha=target,
            parent_snapshot_id=active_snapshot_id,
            snapshot_kind="scope",
            graph_json=candidate_graph,
            file_inventory=file_inventory,
            drift_ledger=[],
            status=SNAPSHOT_STATUS_CANDIDATE,
            created_by=created_by,
            notes=json.dumps(notes, ensure_ascii=False, sort_keys=True),
        )
        index_counts = index_graph_snapshot(
            conn,
            project_id,
            sid,
            nodes=nodes,
            edges=edges,
        )
        governance_index_summary = persist_governance_index(
            conn,
            project_id,
            governance_index,
            persist_inventory=True,
        )
        notes["governance_index"] = governance_index_summary
        conn.execute(
            "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
            (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
        )
        conn.commit()
    trace.step(
        "create-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "snapshot_kind": "scope",
            "commit_sha": target,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "file_inventory_count": len(file_inventory),
        },
        output_payload={
            "snapshot": snapshot,
            "snapshot_path": str(snapshot_graph_path(project_id, sid)),
            "snapshot_artifact": artifact_ref(snapshot_graph_path(project_id, sid)),
        },
    )
    trace.step(
        "index-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        output_payload={"index_counts": index_counts},
    )
    trace.step(
        "build-governance-index",
        input_payload={
            "snapshot_id": sid,
            "run_id": rid,
            "commit_sha": target,
            "index_scope": "candidate_snapshot",
        },
        output_payload={
            "summary": governance_index_summary,
            "artifacts": governance_index_summary.get("artifacts") or {},
            "hash_metadata_merge": hash_metadata_merge,
        },
    )
    semantic_enrichment = _run_scope_semantic_enrichment(
        conn,
        project_id,
        sid,
        root,
        created_by=created_by,
        semantic_options=semantic_options,
        trace=trace,
    )
    notes["semantic_enrichment"] = semantic_enrichment
    activation = None
    if activate:
        with sqlite_write_lock():
            activation = activate_graph_snapshot(
                conn,
                project_id,
                sid,
                expected_old_snapshot_id=active_snapshot_id,
            )
            conn.commit()
        trace.step(
            "activate-snapshot",
            input_payload={
                "snapshot_id": sid,
                "expected_old_snapshot_id": active_snapshot_id,
            },
            output_payload={"activation": activation},
        )
    else:
        trace.step(
            "activate-snapshot",
            input_payload={"snapshot_id": sid, "activate": False},
            output_payload={"activation": None, "status": "skipped"},
            status="skipped",
        )
    with sqlite_write_lock():
        conn.execute(
            "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
            (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
        )
        conn.commit()
    trace_summary = trace.finalize(status="ok")
    return {
        "ok": True,
        "project_id": project_id,
        "run_id": rid,
        "commit_sha": target,
        "snapshot_id": sid,
        "snapshot_status": "active" if activation else SNAPSHOT_STATUS_CANDIDATE,
        "snapshot_path": str(snapshot_graph_path(project_id, sid)),
        "phase_report_path": "",
        "graph_stats": graph_payload_stats(candidate_graph),
        "index_counts": index_counts,
        "governance_index": governance_index_summary,
        "semantic_enrichment": semantic_enrichment,
        "trace": trace_summary,
        "file_inventory_count": len(file_inventory),
        "file_inventory_summary": governance_index.get("file_inventory_summary") or {},
        "feature_cluster_count": governance_index_summary.get("feature_count", 0),
        "snapshot": snapshot,
        "activation": activation,
        "incremental_eligibility": eligibility,
    }


def _finalize_scope_reconcile_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    result: dict[str, Any],
    root: Path,
    active: dict[str, Any],
    active_inventory: list[dict[str, Any]],
    changed_files: list[str],
    pending_count: int,
    covered_commit_shas: list[str],
    target: str,
    rid: str,
    sid: str,
    created_by: str,
    semantic_enqueue_stale: bool,
    strategy: str,
    graph_delta_mode: str,
    fallback_reason: str = "",
) -> dict[str, Any]:
    scope_file_delta = _build_scope_file_delta(
        project_root=root,
        old_rows=active_inventory,
        new_rows=_snapshot_inventory_rows(conn, project_id, sid),
        changed_files=changed_files,
    )
    scope_file_delta = _mark_scope_file_delta_strategy(
        scope_file_delta,
        strategy=strategy,
        graph_delta_mode=graph_delta_mode,
        fallback_reason=fallback_reason,
    )
    old_graph_json = _read_snapshot_graph(project_id, str(active.get("snapshot_id") or ""))
    new_graph_json = _read_snapshot_graph(project_id, sid)
    scope_graph_delta = _build_scope_graph_delta(
        old_graph_json=old_graph_json,
        new_graph_json=new_graph_json,
        scope_file_delta=scope_file_delta,
        strategy=strategy,
        mode=graph_delta_mode,
        fallback_reason=fallback_reason,
    )
    scope_event_summary: dict[str, Any] = {}
    pending_notes = {
        "covered_commit_shas": covered_commit_shas,
        "covered_commit_count": len(covered_commit_shas),
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
        "scope_file_delta": scope_file_delta,
        "scope_graph_delta": scope_graph_delta,
        "semantic_enqueue_stale": bool(semantic_enqueue_stale),
    }
    row = conn.execute(
        "SELECT notes FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, sid),
    ).fetchone()
    with sqlite_write_lock():
        scope_event_summary = _emit_scope_graph_events(
            conn,
            project_id,
            old_snapshot_id=str(active.get("snapshot_id") or ""),
            new_snapshot_id=sid,
            old_graph_json=old_graph_json,
            new_graph_json=new_graph_json,
            scope_file_delta=scope_file_delta,
            baseline_commit=str(active.get("commit_sha") or ""),
            target_commit=target,
            created_by=created_by,
        )
        pending_notes["scope_graph_events"] = scope_event_summary
        activation_succeeded = bool(result.get("activation"))
        updated = _update_pending_scope_candidate(
            conn,
            project_id,
            covered_commit_shas=covered_commit_shas,
            snapshot_id=sid,
            target_commit_sha=target,
            run_id=rid,
            activated=activation_succeeded,
        )
        if row:
            try:
                notes = json.loads(row["notes"] if hasattr(row, "keys") else row[0])
            except Exception:
                notes = {}
            notes["scope_file_delta"] = scope_file_delta
            notes["scope_graph_delta"] = scope_graph_delta
            notes["pending_scope_reconcile"] = pending_notes
            conn.execute(
                "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
                (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
            )
        conn.commit()
    return {
        **result,
        "pending_count": pending_count,
        "covered_commit_shas": covered_commit_shas,
        "covered_pending_count": len(covered_commit_shas),
        "pending_rows_bound": updated,
        "scope_file_delta": scope_file_delta,
        "scope_graph_delta": scope_graph_delta,
        "scope_graph_events": scope_event_summary,
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
    }


def run_pending_scope_reconcile_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    target_commit_sha: str = "",
    run_id: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
    activate: bool = False,
    semantic_enrich: bool = True,
    semantic_use_ai: bool | None = None,
    semantic_feedback_items: list[dict[str, Any]] | dict[str, Any] | None = None,
    semantic_feedback_round: int | None = None,
    semantic_max_excerpt_chars: int | None = None,
    semantic_ai_call: Any = None,
    semantic_ai_feature_limit: int | None = None,
    semantic_ai_provider: str | None = None,
    semantic_ai_model: str | None = None,
    semantic_ai_role: str | None = None,
    semantic_ai_chain_role: str | None = None,
    semantic_analyzer_role: str | None = None,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_ai_input_mode: str | None = None,
    semantic_dynamic_graph_state: bool | None = None,
    semantic_graph_state: bool = True,
    semantic_skip_completed: bool = True,
    semantic_classify_feedback: bool = True,
    semantic_batch_memory: bool | None = False,
    semantic_batch_memory_id: str | None = None,
    semantic_base_snapshot_id: str | None = None,
    semantic_config_path: str | Path | None = None,
    semantic_enqueue_stale: bool = True,
) -> dict[str, Any]:
    """Materialize pending scope rows as a reviewable candidate snapshot.

    The current MVP rebuilds a state-only candidate graph from the current
    worktree, then binds pending commits up to the target commit to that
    candidate. It intentionally does not activate the snapshot.

    OPT-BACKLOG-MATERIALIZE-NO-WORKER-NOTIFY: when called from the dashboard
    flow, the caller can set `semantic_enqueue_stale=False` so the materialize
    does not silently fill graph_semantic_jobs with ai_pending rows the
    in-process worker won't auto-drain. Operators then explicitly enqueue
    enrichment via POST /semantic/jobs (which publishes
    semantic_job.enqueued).
    """
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    head = _git_commit(root) or "unknown"
    target = target_commit_sha or head
    if head != "unknown" and target != head:
        raise ValueError(
            "pending scope materializer scans the current worktree; "
            f"target_commit_sha must equal HEAD ({head}), got {target}"
        )
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED],
    )
    if not pending:
        return {
            "ok": False,
            "project_id": project_id,
            "reason": "no_pending_scope_reconcile",
            "target_commit_sha": target,
            "pending_count": 0,
        }
    covered = _pending_commits_through_target(pending, target)
    if not covered:
        return {
            "ok": False,
            "project_id": project_id,
            "reason": "no_pending_commits_selected",
            "target_commit_sha": target,
            "pending_count": len(pending),
        }
    dirty_files = _git_dirty_files(root)
    if dirty_files:
        preview = ", ".join(dirty_files[:8])
        suffix = f", ... +{len(dirty_files) - 8} more" if len(dirty_files) > 8 else ""
        raise ValueError(
            "pending scope materializer requires a clean git worktree; "
            f"uncommitted files: {preview}{suffix}"
        )

    active = get_active_graph_snapshot(conn, project_id) or {}
    active_inventory = _snapshot_inventory_rows(conn, project_id, active.get("snapshot_id", ""))
    changed_files = _git_changed_files(
        root,
        str(active.get("commit_sha") or ""),
        target,
    )
    has_semantic_selector_override = _has_semantic_selector_override(
        semantic_ai_scope,
        semantic_node_ids,
        semantic_layers,
        semantic_quality_flags,
        semantic_missing,
        semantic_changed_paths,
        semantic_path_prefixes,
        semantic_selector_match,
    )
    effective_semantic_ai_scope = semantic_ai_scope
    effective_semantic_changed_paths = semantic_changed_paths
    effective_semantic_selector_match = semantic_selector_match
    if changed_files and not has_semantic_selector_override:
        effective_semantic_ai_scope = "changed"
        effective_semantic_changed_paths = changed_files
        effective_semantic_selector_match = "primary"
    rid = run_id or f"scope-reconcile-{_short_commit(target)}-pending"
    sid = snapshot_id or snapshot_id_for("scope", target)
    checkout_provenance = describe_checkout(
        root,
        project_id=project_id,
        commit_sha=target,
    )
    semantic_options = {
        "semantic_enrich": semantic_enrich,
        "semantic_use_ai": semantic_use_ai,
        "semantic_feedback_items": semantic_feedback_items,
        "semantic_feedback_round": semantic_feedback_round,
        "semantic_max_excerpt_chars": semantic_max_excerpt_chars,
        "semantic_ai_call": semantic_ai_call,
        "semantic_ai_feature_limit": semantic_ai_feature_limit,
        "semantic_ai_provider": semantic_ai_provider,
        "semantic_ai_model": semantic_ai_model,
        "semantic_ai_role": semantic_ai_role,
        "semantic_ai_chain_role": semantic_ai_chain_role,
        "semantic_analyzer_role": semantic_analyzer_role,
        "semantic_ai_scope": effective_semantic_ai_scope,
        "semantic_node_ids": semantic_node_ids,
        "semantic_layers": semantic_layers,
        "semantic_quality_flags": semantic_quality_flags,
        "semantic_missing": semantic_missing,
        "semantic_changed_paths": effective_semantic_changed_paths,
        "semantic_path_prefixes": semantic_path_prefixes,
        "semantic_selector_match": effective_semantic_selector_match,
        "semantic_include_structural": semantic_include_structural,
        "semantic_ai_batch_size": semantic_ai_batch_size,
        "semantic_ai_batch_by": semantic_ai_batch_by,
        "semantic_ai_input_mode": semantic_ai_input_mode,
        "semantic_dynamic_graph_state": semantic_dynamic_graph_state,
        "semantic_graph_state": semantic_graph_state,
        "semantic_skip_completed": semantic_skip_completed,
        "semantic_classify_feedback": semantic_classify_feedback,
        "semantic_batch_memory": semantic_batch_memory,
        "semantic_batch_memory_id": semantic_batch_memory_id,
        "semantic_base_snapshot_id": semantic_base_snapshot_id or active.get("snapshot_id", ""),
        "semantic_config_path": semantic_config_path,
        "semantic_enqueue_stale": semantic_enqueue_stale,
    }
    incremental_result = _run_incremental_metadata_scope_reconcile_candidate(
        conn,
        project_id,
        root,
        target=target,
        rid=rid,
        sid=sid,
        active=active,
        active_inventory=active_inventory,
        changed_files=changed_files,
        checkout_provenance=checkout_provenance,
        created_by=created_by,
        activate=activate,
        semantic_options=semantic_options,
    )
    if incremental_result.get("ok"):
        incremental_mode = str(
            (incremental_result.get("incremental_eligibility") or {}).get("mode")
            or "metadata_only"
        )
        return _finalize_scope_reconcile_candidate(
            conn,
            project_id,
            result=incremental_result,
            root=root,
            active=active,
            active_inventory=active_inventory,
            changed_files=changed_files,
            pending_count=len(pending),
            covered_commit_shas=covered,
            target=target,
            rid=rid,
            sid=sid,
            created_by=created_by,
            semantic_enqueue_stale=semantic_enqueue_stale,
            strategy="incremental_graph_delta",
            graph_delta_mode=incremental_mode,
        )
    fallback_reason = str(
        incremental_result.get("fallback_reason")
        or "incremental_scope_unsupported"
    )
    result = run_state_only_full_reconcile(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=target,
        snapshot_id=sid,
        snapshot_kind="scope",
        created_by=created_by,
        # MF-2026-05-10-014: pass through caller's activate intent so the
        # dashboard "Queue scope reconcile" path can incrementally catch up
        # the active snapshot in one HTTP round-trip. MF-012's hook then
        # auto-rebuilds the projection on activation.
        activate=activate,
        semantic_enrich=semantic_enrich,
        semantic_use_ai=semantic_use_ai,
        semantic_feedback_items=semantic_feedback_items,
        semantic_feedback_round=semantic_feedback_round,
        semantic_max_excerpt_chars=semantic_max_excerpt_chars,
        semantic_ai_call=semantic_ai_call,
        semantic_ai_feature_limit=semantic_ai_feature_limit,
        semantic_ai_batch_size=semantic_ai_batch_size,
        semantic_ai_batch_by=semantic_ai_batch_by,
        semantic_ai_input_mode=semantic_ai_input_mode,
        semantic_dynamic_graph_state=semantic_dynamic_graph_state,
        semantic_graph_state=semantic_graph_state,
        semantic_skip_completed=semantic_skip_completed,
        semantic_classify_feedback=semantic_classify_feedback,
        semantic_batch_memory=semantic_batch_memory,
        semantic_batch_memory_id=semantic_batch_memory_id,
        semantic_base_snapshot_id=semantic_base_snapshot_id or active.get("snapshot_id", ""),
        semantic_ai_provider=semantic_ai_provider,
        semantic_ai_model=semantic_ai_model,
        semantic_ai_role=semantic_ai_role,
        semantic_ai_chain_role=semantic_ai_chain_role,
        semantic_analyzer_role=semantic_analyzer_role,
        semantic_ai_scope=effective_semantic_ai_scope,
        semantic_node_ids=semantic_node_ids,
        semantic_layers=semantic_layers,
        semantic_quality_flags=semantic_quality_flags,
        semantic_missing=semantic_missing,
        semantic_changed_paths=effective_semantic_changed_paths,
        semantic_path_prefixes=semantic_path_prefixes,
        semantic_selector_match=effective_semantic_selector_match,
        semantic_include_structural=semantic_include_structural,
        semantic_config_path=semantic_config_path,
        semantic_enqueue_stale=semantic_enqueue_stale,
        notes_extra={
            "pending_scope_reconcile": {
                "covered_commit_shas": covered,
                "covered_commit_count": len(covered),
                "active_snapshot_id": active.get("snapshot_id", ""),
                "active_graph_commit": active.get("commit_sha", ""),
                "semantic_selector_defaulted_to_changed_files": bool(
                    changed_files and not has_semantic_selector_override
                ),
                "semantic_enqueue_stale": bool(semantic_enqueue_stale),
            }
        },
    )
    if not result.get("ok"):
        return {
            **result,
            "pending_count": len(pending),
            "covered_commit_shas": covered,
        }
    return _finalize_scope_reconcile_candidate(
        conn,
        project_id,
        result=result,
        root=root,
        active=active,
        active_inventory=active_inventory,
        changed_files=changed_files,
        pending_count=len(pending),
        covered_commit_shas=covered,
        target=target,
        rid=rid,
        sid=sid,
        created_by=created_by,
        semantic_enqueue_stale=semantic_enqueue_stale,
        strategy="full_rebuild_fallback",
        graph_delta_mode="full_rebuild",
        fallback_reason=fallback_reason,
    )


def run_backfill_escape_hatch(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    target_commit_sha: str = "",
    run_id: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
    reason: str = "",
    expected_old_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Activate a HEAD full snapshot and waive stuck pending scope rows.

    This is the explicit observer escape hatch for early scope-reconcile bugs:
    it rebuilds graph state from the current commit, activates that state with
    normal snapshot CAS semantics, and preserves queued/running/failed pending
    rows as waived audit records instead of deleting them.
    """
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    head = _git_commit(root) or "unknown"
    target = target_commit_sha or head
    if head != "unknown" and target != head:
        raise ValueError(
            "backfill escape hatch scans the current worktree; "
            f"target_commit_sha must equal HEAD ({head}), got {target}"
        )
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED],
    )
    pending_commits = [
        str(row.get("commit_sha") or "").strip()
        for row in pending
        if str(row.get("commit_sha") or "").strip()
    ]
    active = get_active_graph_snapshot(conn, project_id) or {}
    rid = run_id or f"backfill-escape-{_short_commit(target)}"
    sid = snapshot_id or snapshot_id_for("full", target)
    result = run_state_only_full_reconcile(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=target,
        snapshot_id=sid,
        snapshot_kind="full",
        created_by=created_by,
        activate=False,
        notes_extra={
            "backfill_escape_hatch": {
                "reason": reason,
                "pending_scope_commits": pending_commits,
                "pending_scope_count": len(pending_commits),
                "active_snapshot_id": active.get("snapshot_id", ""),
                "active_graph_commit": active.get("commit_sha", ""),
            }
        },
    )
    if not result.get("ok"):
        return {
            **result,
            "pending_scope_commits": pending_commits,
            "pending_scope_count": len(pending_commits),
        }
    with sqlite_write_lock():
        finalize = finalize_graph_snapshot(
            conn,
            project_id,
            result["snapshot_id"],
            target_commit_sha=target,
            expected_old_snapshot_id=expected_old_snapshot_id,
            actor=created_by,
            materialize_pending=False,
            evidence={"source": "backfill_escape_hatch", "reason": reason},
        )
        waiver = waive_pending_scope_reconcile(
            conn,
            project_id,
            commit_shas=pending_commits,
            snapshot_id=result["snapshot_id"],
            actor=created_by,
            reason=reason,
            evidence={"source": "backfill_escape_hatch"},
        )
        conn.commit()
    return {
        **result,
        "snapshot_status": "active",
        "activation": finalize,
        "pending_scope_commits": pending_commits,
        "pending_scope_count": len(pending_commits),
        "pending_scope_waiver": waiver,
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
    }


__all__ = [
    "normalize_reconcile_snapshot_for_comparison",
    "repair_snapshot_feature_hash_metadata",
    "run_backfill_escape_hatch",
    "run_pending_scope_reconcile_candidate",
    "run_state_only_full_reconcile",
]
