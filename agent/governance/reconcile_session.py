"""Reconcile session state machine (CR0a).

Pure module: NO DB writes, NO filesystem I/O, NO network at import time.
State machine: idle -> active -> finalizing -> finalized | finalize_failed | rolled_back.
"""
from __future__ import annotations
import io, json, re, shutil, sqlite3, subprocess, tarfile, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence

_GOVERNANCE_DIR = Path(__file__).resolve().parent
_SNAPSHOT_DIRNAME = "reconcile_snapshots"
_OVERLAY_FILENAME = "graph.rebase.overlay.json"
_GRAPH_FILENAME = "graph.json"


class SessionAlreadyActiveError(Exception):
    """Raised when an active/finalizing session already exists (CR0b -> HTTP 409)."""


class SessionClusterGateError(ValueError):
    """Raised when queued reconcile clusters are not safe to finalize."""

    def __init__(self, message: str, summary: Optional[dict] = None):
        super().__init__(message)
        self.summary = summary or {}


@dataclass
class ReconcileSession:
    project_id: str
    session_id: str
    run_id: Optional[str] = None
    status: str = "active"
    started_at: str = ""
    finalized_at: Optional[str] = None
    cluster_count_total: int = 0
    cluster_count_resolved: int = 0
    cluster_count_failed: int = 0
    bypass_gates: List[str] = field(default_factory=list)
    started_by: Optional[str] = None
    snapshot_path: Optional[str] = None
    snapshot_head_sha: Optional[str] = None
    base_commit_sha: str = ""
    target_branch: str = ""
    target_head_sha: str = ""
    finalize_error: dict = field(default_factory=dict)


@dataclass
class SessionFinalizationResult:
    project_id: str
    session_id: str
    status: str
    finalized_at: str
    overlay_archived_to: Optional[str] = None
    graph_path: Optional[str] = None
    graph_backup_path: Optional[str] = None
    materialized_node_count: int = 0
    materialization_counts: dict = field(default_factory=dict)


@dataclass
class SessionRollbackResult:
    project_id: str
    session_id: str
    status: str
    rolled_back_at: str
    snapshot_path: Optional[str] = None


@dataclass
class RestoreResult:
    project_id: str
    session_id: str
    graph_bytes: int
    node_state_rows: int


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _gov(base: Optional[Path] = None) -> Path:
    return Path(base) if base is not None else _GOVERNANCE_DIR

def _snapshot_dir(base: Optional[Path] = None) -> Path:
    return _gov(base) / _SNAPSHOT_DIRNAME

def _overlay_path(base: Optional[Path] = None) -> Path:
    return _gov(base) / _OVERLAY_FILENAME

def _graph_path(base: Optional[Path] = None) -> Path:
    return _gov(base) / _GRAPH_FILENAME


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _branch_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    slug = re.sub(r"-+", "-", slug).strip("-._")
    return slug or "project"


def default_target_branch(project_id: str, session_id: str) -> str:
    """Return the branch used to accumulate one reconcile session."""
    return f"reconcile/{_branch_slug(project_id)}-{str(session_id or '')[:12]}"


def _load_json_object(path: Path) -> dict:
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load JSON object from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _node_id(node: dict) -> str:
    return str(node.get("id") or node.get("node_id") or "").strip()


def _normalize_rel(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")


def _primary_paths(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    raw = node.get("primary")
    if raw is None:
        raw = node.get("primary_files")
    if isinstance(raw, str):
        return [_normalize_rel(raw)] if raw else []
    if isinstance(raw, (list, tuple)):
        return [_normalize_rel(str(p)) for p in raw if str(p).strip()]
    return []


def _path_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_normalize_rel(value)] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [_normalize_rel(str(p)) for p in value if str(p).strip()]
    return []


def _merge_path_lists(*values: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        for path in _path_list(value):
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _candidate_test_paths(node: dict) -> list[str]:
    paths = _path_list(node.get("test"))
    coverage = node.get("test_coverage")
    if isinstance(coverage, dict):
        paths.extend(_path_list(coverage.get("test_files")))
    return _merge_path_lists(paths)


def _path_exists(path: str, *, workspace_dir: Optional[Path],
                 graph_path: Path) -> bool:
    rel = _normalize_rel(path)
    if not rel:
        return False
    p = Path(rel)
    if p.is_absolute():
        return p.exists()
    roots = [workspace_dir, _repo_root(), graph_path.parent]
    return any((Path(root) / rel).exists() for root in roots if root is not None)


def _graph_nodes_by_id(graph_doc: dict) -> dict[str, dict]:
    deps_graph = graph_doc.get("deps_graph") if isinstance(graph_doc, dict) else {}
    return _node_link_nodes_by_id(deps_graph)


def _node_link_nodes_by_id(deps_graph: dict) -> dict[str, dict]:
    raw_nodes = deps_graph.get("nodes") if isinstance(deps_graph, dict) else []
    out: dict[str, dict] = {}
    if isinstance(raw_nodes, dict):
        iterable = raw_nodes.values()
    elif isinstance(raw_nodes, list):
        iterable = raw_nodes
    else:
        iterable = []
    for node in iterable:
        if not isinstance(node, dict):
            continue
        nid = _node_id(node)
        if nid:
            clean = dict(node)
            clean["id"] = nid
            out[nid] = clean
    return out


def _graph_links(graph_doc: dict) -> list[dict]:
    deps_graph = graph_doc.get("deps_graph") if isinstance(graph_doc, dict) else {}
    return _node_link_links(deps_graph)


def _node_link_links(deps_graph: dict) -> list[dict]:
    if not isinstance(deps_graph, dict):
        return []
    raw = deps_graph.get("edges")
    if raw is None:
        raw = deps_graph.get("links")
    if not isinstance(raw, list):
        return []
    return [dict(e) for e in raw if isinstance(e, dict)]


def _overlay_nodes_by_id(overlay_doc: dict) -> dict[str, dict]:
    raw_nodes = overlay_doc.get("nodes") if isinstance(overlay_doc, dict) else {}
    out: dict[str, dict] = {}
    if isinstance(raw_nodes, dict):
        iterable = raw_nodes.items()
    elif isinstance(raw_nodes, list):
        iterable = ((None, n) for n in raw_nodes)
    else:
        iterable = []
    for key, node in iterable:
        if not isinstance(node, dict):
            continue
        nid = _node_id(node) or str(key or "").strip()
        if not nid:
            raise ValueError("overlay node missing id/node_id")
        clean = dict(node)
        clean["node_id"] = nid
        out[nid] = clean
    return out


def _layer_verify_level(layer: str) -> int:
    if isinstance(layer, str) and layer.startswith("L"):
        try:
            return max(1, int(layer[1:]) + 1)
        except ValueError:
            return 1
    return 1


def _overlay_to_graph_node(node_id: str, overlay_node: dict, *,
                           session_id: str, finalized_at: str) -> dict:
    layer = (
        overlay_node.get("layer")
        or overlay_node.get("parent_layer")
        or (node_id.split(".", 1)[0] if "." in node_id else "L7")
    )
    primary = _primary_paths(overlay_node)
    secondary = _path_list(overlay_node.get("secondary"))
    test = _path_list(overlay_node.get("test"))
    metadata = dict(overlay_node.get("metadata") or {})
    metadata.update({
        "materialized_from_overlay": True,
        "reconcile_session_id": session_id,
        "materialized_at": finalized_at,
    })
    deps = list(overlay_node.get("deps") or [])
    return {
        "id": node_id,
        "title": overlay_node.get("title") or node_id,
        "layer": str(layer),
        "verify_level": overlay_node.get("verify_level") or _layer_verify_level(str(layer)),
        "gate_mode": overlay_node.get("gate_mode") or "auto",
        "test_coverage": overlay_node.get("test_coverage") or "none",
        "primary": primary,
        "secondary": secondary,
        "test": test,
        "artifacts": list(overlay_node.get("artifacts") or []),
        "_deps": deps,
        "deps": deps,
        "metadata": metadata,
        "verify_status": overlay_node.get("verify_status") or "pending",
    }


def _duplicate_primary_report(nodes: dict[str, dict]) -> list[dict]:
    seen_primary: dict[str, str] = {}
    duplicate_primary: list[dict] = []
    for nid, node in nodes.items():
        for primary in _primary_paths(node):
            prior = seen_primary.get(primary)
            if prior and prior != nid:
                duplicate_primary.append({
                    "primary": primary,
                    "first_node_id": prior,
                    "second_node_id": nid,
                })
            else:
                seen_primary[primary] = nid
    return duplicate_primary


def _validate_materialized_graph(graph_doc: dict, *, graph_path: Path,
                                 workspace_dir: Optional[Path]) -> dict:
    nodes = _graph_nodes_by_id(graph_doc)
    if not nodes:
        raise ValueError("materialized graph has no nodes")
    missing: list[dict] = []
    for nid, node in nodes.items():
        for primary in _primary_paths(node):
            if not _path_exists(primary, workspace_dir=workspace_dir,
                                graph_path=graph_path):
                missing.append({"node_id": nid, "primary": primary})
    if missing:
        raise ValueError(f"materialized graph references missing primary paths: {missing[:10]}")
    duplicates = _duplicate_primary_report(nodes)
    return {
        "duplicate_primary_count": len(duplicates),
        "duplicate_primary_examples": duplicates[:10],
    }


def _node_link_section(nodes: dict[str, dict], links: list[dict], *,
                       edge_key: str = "edges") -> dict:
    return {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": list(nodes.values()),
        edge_key: links,
    }


def _node_link_document(nodes: dict[str, dict], links: list[dict], *,
                        extra_graphs: Optional[dict[str, dict]] = None) -> dict:
    data = {
        "version": 1,
        "deps_graph": _node_link_section(nodes, links, edge_key="edges"),
        "gates_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [],
            "edges": [],
        },
    }
    if extra_graphs:
        data.update(extra_graphs)
    return data


def _edge_endpoints(edge: dict) -> tuple[str, str]:
    return (
        str(edge.get("source") or edge.get("from") or "").strip(),
        str(edge.get("target") or edge.get("to") or "").strip(),
    )


def _canonical_edge(src: str, dst: str, edge: Optional[dict] = None) -> dict:
    edge = dict(edge or {})
    edge.pop("from", None)
    edge.pop("to", None)
    edge["source"] = src
    edge["target"] = dst
    return edge


def _remap_candidate_graph_section(section: Any,
        candidate_to_final: dict[str, str],
        final_nodes: dict[str, dict]) -> tuple[dict, int]:
    if not isinstance(section, dict):
        return {}, 0
    links: list[dict] = []
    seen_links: set[tuple[str, str, str]] = set()
    for edge in _node_link_links(section):
        src, dst = _edge_endpoints(edge)
        remapped_src = candidate_to_final.get(src)
        remapped_dst = candidate_to_final.get(dst)
        if not remapped_src or not remapped_dst or remapped_src == remapped_dst:
            continue
        relation = str(edge.get("relation") or edge.get("type") or "")
        key = (remapped_src, remapped_dst, relation)
        if key in seen_links:
            continue
        links.append(_canonical_edge(remapped_src, remapped_dst, edge))
        seen_links.add(key)
    return _node_link_section(final_nodes, links, edge_key="links"), len(links)


def _build_overlay_primary_map(overlay_nodes: dict[str, dict]) -> dict[str, str]:
    primary_to_overlay: dict[str, str] = {}
    duplicates: list[dict] = []
    for overlay_id, node in overlay_nodes.items():
        for primary in _primary_paths(node):
            prior = primary_to_overlay.get(primary)
            if prior and prior != overlay_id:
                duplicates.append({
                    "primary": primary,
                    "first_node_id": prior,
                    "second_node_id": overlay_id,
                })
            primary_to_overlay[primary] = overlay_id
    if duplicates:
        raise ValueError(f"overlay has duplicate primary coverage: {duplicates[:10]}")
    return primary_to_overlay


def _candidate_hierarchy_parent_map(candidate_doc: dict) -> dict[str, str]:
    hierarchy = candidate_doc.get("hierarchy_graph")
    parents: dict[str, str] = {}
    if not isinstance(hierarchy, dict):
        return parents
    for edge in _node_link_links(hierarchy):
        relation = str(edge.get("relation") or edge.get("type") or "")
        if relation and relation != "contains":
            continue
        src, dst = _edge_endpoints(edge)
        if src and dst:
            parents.setdefault(dst, src)
    return parents


def _next_layer_id(layer: str, used_ids: set[str]) -> str:
    prefix = layer if layer.startswith("L") else "L7"
    max_suffix = 0
    for nid in used_ids:
        if not nid.startswith(prefix + "."):
            continue
        try:
            max_suffix = max(max_suffix, int(nid.split(".", 1)[1]))
        except (IndexError, ValueError):
            continue
    while True:
        max_suffix += 1
        candidate = f"{prefix}.{max_suffix}"
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate


def _choose_overlay_final_id(
        overlay_id: str, candidate_ids: list[str],
        candidate_nodes: dict[str, dict], final_nodes: dict[str, dict],
        used_ids: set[str]) -> str:
    if overlay_id not in final_nodes:
        used_ids.add(overlay_id)
        return overlay_id
    existing = final_nodes[overlay_id]
    if _primary_paths(existing):
        raise ValueError(f"overlay node id collides with candidate leaf node: {overlay_id}")
    layers = {
        str(candidate_nodes[cid].get("layer") or "")
        for cid in candidate_ids if cid in candidate_nodes
    }
    non_empty_layers = sorted(layer for layer in layers if layer)
    layer = non_empty_layers[0] if len(non_empty_layers) == 1 else "L7"
    return _next_layer_id(layer, used_ids)


def _compose_candidate_aware_graph(*, candidate_graph_path: Path,
        overlay_nodes: dict[str, dict], session_id: str,
        finalized_at: str) -> tuple[dict[str, dict], list[dict], dict, dict[str, dict]]:
    """Use candidate deps_graph as skeleton and overlay as approved leaves."""
    candidate_doc = _load_json_object(candidate_graph_path)
    candidate_graph = candidate_doc.get("deps_graph")
    if not isinstance(candidate_graph, dict):
        raise ValueError(f"candidate graph missing deps_graph: {candidate_graph_path}")
    candidate_nodes = _node_link_nodes_by_id(candidate_graph)
    candidate_links = _node_link_links(candidate_graph)
    if not candidate_nodes:
        raise ValueError(f"candidate graph has no nodes: {candidate_graph_path}")

    primary_to_overlay = _build_overlay_primary_map(overlay_nodes)
    candidate_by_primary: dict[str, list[str]] = {}
    for candidate_id, candidate_node in candidate_nodes.items():
        for primary in _primary_paths(candidate_node):
            candidate_by_primary.setdefault(primary, []).append(candidate_id)

    missing = [
        primary for primary in sorted(primary_to_overlay)
        if primary not in candidate_by_primary
    ]
    ambiguous = [
        {"primary": primary, "candidate_node_ids": ids}
        for primary, ids in sorted(candidate_by_primary.items())
        if primary in primary_to_overlay and len(ids) > 1
    ]
    if missing:
        raise ValueError(f"overlay primaries missing from candidate graph: {missing[:10]}")
    if ambiguous:
        raise ValueError(f"candidate graph has ambiguous primary coverage: {ambiguous[:10]}")

    candidate_to_final: dict[str, str] = {}
    overlay_to_candidates: dict[str, list[str]] = {}
    final_nodes: dict[str, dict] = {}
    overlay_to_final: dict[str, str] = {}
    hierarchy_parents = _candidate_hierarchy_parent_map(candidate_doc)

    for candidate_id, candidate_node in candidate_nodes.items():
        primaries = _primary_paths(candidate_node)
        if not primaries:
            clean = dict(candidate_node)
            clean["id"] = candidate_id
            metadata = dict(clean.get("metadata") or {})
            metadata["materialized_from_candidate_hierarchy"] = True
            metadata["reconcile_session_id"] = session_id
            metadata["materialized_at"] = finalized_at
            clean["metadata"] = metadata
            final_nodes[candidate_id] = clean
            candidate_to_final[candidate_id] = candidate_id
            continue
        overlay_ids = {
            primary_to_overlay[p] for p in primaries if p in primary_to_overlay
        }
        if not overlay_ids:
            continue
        if len(overlay_ids) > 1:
            raise ValueError(
                f"candidate node {candidate_id} maps to multiple overlay nodes: "
                f"{sorted(overlay_ids)}")
        overlay_id = next(iter(overlay_ids))
        overlay_to_candidates.setdefault(overlay_id, []).append(candidate_id)

    used_ids = set(candidate_nodes) | set(overlay_nodes) | set(final_nodes)
    for overlay_id, candidate_ids in sorted(overlay_to_candidates.items()):
        parent_ids = {
            hierarchy_parents[cid] for cid in candidate_ids
            if cid in hierarchy_parents
        }
        if len(parent_ids) > 1:
            raise ValueError(
                "overlay node aggregates candidate leaves from multiple hierarchy parents: "
                f"{overlay_id} -> {sorted(parent_ids)}")
        final_id = _choose_overlay_final_id(
            overlay_id, candidate_ids, candidate_nodes, final_nodes, used_ids)
        overlay_to_final[overlay_id] = final_id
        for candidate_id in candidate_ids:
            candidate_to_final[candidate_id] = final_id

    for overlay_id, overlay_node in overlay_nodes.items():
        candidate_ids = overlay_to_candidates.get(overlay_id, [])
        if not candidate_ids:
            continue
        candidate_layers = {
            str(candidate_nodes[cid].get("layer") or "") for cid in candidate_ids
            if cid in candidate_nodes
        }
        final_id = overlay_to_final[overlay_id]
        node = _overlay_to_graph_node(
            final_id, overlay_node,
            session_id=session_id, finalized_at=finalized_at,
        )
        candidate_leafs = [candidate_nodes[cid] for cid in candidate_ids if cid in candidate_nodes]
        node["secondary"] = _merge_path_lists(
            node.get("secondary"),
            *[c.get("secondary") for c in candidate_leafs],
            *[c.get("secondary_files") for c in candidate_leafs],
        )
        node["test"] = _merge_path_lists(
            node.get("test"),
            *[_candidate_test_paths(c) for c in candidate_leafs],
        )
        node["artifacts"] = _merge_path_lists(
            node.get("artifacts"),
            *[c.get("artifacts") for c in candidate_leafs],
        )
        if len(candidate_layers) == 1:
            layer = next(iter(candidate_layers))
            if layer:
                node["layer"] = layer
                node["verify_level"] = _layer_verify_level(layer)
        metadata = dict(node.get("metadata") or {})
        metadata["candidate_node_ids"] = sorted(candidate_ids)
        metadata["candidate_graph_path"] = str(candidate_graph_path)
        metadata["materialized_with_candidate_hierarchy"] = True
        if final_id != overlay_id:
            metadata["overlay_node_id"] = overlay_id
            metadata["reallocated_from_colliding_overlay_id"] = True
        node["metadata"] = metadata
        final_nodes[final_id] = node

    missing_overlay_ids = sorted(set(overlay_nodes) - set(overlay_to_final))
    if missing_overlay_ids:
        raise ValueError(
            "overlay nodes were not represented in candidate graph: "
            f"{missing_overlay_ids[:10]}")

    links: list[dict] = []
    seen_links: set[tuple[str, str, str]] = set()
    for edge in candidate_links:
        src, dst = _edge_endpoints(edge)
        remapped_src = candidate_to_final.get(src)
        remapped_dst = candidate_to_final.get(dst)
        if not remapped_src or not remapped_dst or remapped_src == remapped_dst:
            continue
        relation = str(edge.get("relation") or edge.get("type") or "")
        key = (remapped_src, remapped_dst, relation)
        if key in seen_links:
            continue
        links.append(_canonical_edge(remapped_src, remapped_dst, edge))
        seen_links.add(key)

    deps_by_node: dict[str, list[str]] = {nid: [] for nid in final_nodes}
    for edge in links:
        src, dst = _edge_endpoints(edge)
        if dst in deps_by_node and src not in deps_by_node[dst]:
            deps_by_node[dst].append(src)
    for nid, deps in deps_by_node.items():
        final_nodes[nid]["_deps"] = deps
        final_nodes[nid]["deps"] = deps

    extra_graphs: dict[str, dict] = {}
    hierarchy_graph, hierarchy_link_count = _remap_candidate_graph_section(
        candidate_doc.get("hierarchy_graph"), candidate_to_final, final_nodes)
    if hierarchy_graph:
        extra_graphs["hierarchy_graph"] = hierarchy_graph
    evidence_graph, evidence_link_count = _remap_candidate_graph_section(
        candidate_doc.get("evidence_graph"), candidate_to_final, final_nodes)
    if evidence_graph:
        extra_graphs["evidence_graph"] = evidence_graph

    return final_nodes, links, {
        "candidate_graph_path": str(candidate_graph_path),
        "candidate_node_count": len(candidate_nodes),
        "candidate_link_count": len(candidate_links),
        "candidate_hierarchy_nodes": len([
            n for n in final_nodes.values() if not _primary_paths(n)
        ]),
        "candidate_leaf_nodes_remapped": len(overlay_to_candidates),
        "hierarchy_link_count": hierarchy_link_count,
        "evidence_link_count": evidence_link_count,
    }, extra_graphs


def materialize_overlay_to_graph(conn: sqlite3.Connection, project_id: str,
        session_id: str, *, overlay_path: Path, graph_path: Path,
        workspace_dir: Optional[Path] = None, full_rebase: bool = False,
        candidate_graph_path: Optional[Path] = None,
        finalized_at: Optional[str] = None) -> dict:
    """Compose overlay + carry-forward nodes and atomically write graph.json.

    The overlay remains untouched.  Callers archive/delete it only after this
    function succeeds and the session row has been finalized.
    """
    finalized_at = finalized_at or _utcnow_iso()
    overlay_path = Path(overlay_path)
    graph_path = Path(graph_path)
    if not overlay_path.exists():
        raise ValueError(f"reconcile overlay not found: {overlay_path}")
    if not graph_path.exists():
        raise ValueError(f"graph.json not found: {graph_path}")

    old_graph = _load_json_object(graph_path)
    overlay_doc = _load_json_object(overlay_path)
    old_nodes = _graph_nodes_by_id(old_graph)
    overlay_nodes = _overlay_nodes_by_id(overlay_doc)
    existing_state: dict[str, dict] = {}
    try:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT node_id, verify_status, build_status FROM node_state "
            "WHERE project_id=?",
            (project_id,),
        ).fetchall():
            existing_state[str(row["node_id"])] = {
                "verify_status": row["verify_status"],
                "build_status": row["build_status"],
            }
    except sqlite3.Error:
        existing_state = {}

    primary_to_overlay = _build_overlay_primary_map(overlay_nodes)
    overlay_primary = set(primary_to_overlay)
    if not overlay_primary and full_rebase:
        raise ValueError("full_rebase finalize refuses an empty overlay")

    candidate_meta: dict = {}
    extra_graphs: dict[str, dict] = {}
    if full_rebase and candidate_graph_path and Path(candidate_graph_path).exists():
        final_nodes, links, candidate_meta, extra_graphs = _compose_candidate_aware_graph(
            candidate_graph_path=Path(candidate_graph_path),
            overlay_nodes=overlay_nodes,
            session_id=session_id,
            finalized_at=finalized_at,
        )
        replaced_old = [
            old_id for old_id, old_node in old_nodes.items()
            if set(_primary_paths(old_node)) & overlay_primary
        ]
        archived_old = [old_id for old_id in old_nodes if old_id not in replaced_old]
        carried_forward: list[str] = []
    else:
        final_nodes = {}
        replaced_old = []
        archived_old = []
        carried_forward = []

        for old_id, old_node in old_nodes.items():
            primaries = _primary_paths(old_node)
            covered = bool(set(primaries) & overlay_primary)
            exists_on_disk = (
                not primaries
                or all(_path_exists(p, workspace_dir=workspace_dir, graph_path=graph_path)
                       for p in primaries)
            )
            if covered:
                replaced_old.append(old_id)
                continue
            if full_rebase or not exists_on_disk:
                archived_old.append(old_id)
                continue
            clean = dict(old_node)
            metadata = dict(clean.get("metadata") or {})
            metadata["carry_forward_unverified"] = True
            metadata["carry_forward_provenance"] = {
                "session_id": session_id,
                "source_node_id": old_id,
                "original_status_at_carry": existing_state.get(old_id, {}).get("verify_status", ""),
                "carried_at": finalized_at,
            }
            clean["metadata"] = metadata
            final_nodes[old_id] = clean
            carried_forward.append(old_id)

        for overlay_id, overlay_node in overlay_nodes.items():
            if overlay_id in final_nodes:
                raise ValueError(f"overlay node id collides with carried-forward node: {overlay_id}")
            final_nodes[overlay_id] = _overlay_to_graph_node(
                overlay_id, overlay_node,
                session_id=session_id, finalized_at=finalized_at,
            )

        links = []
        final_id_set = set(final_nodes)
        seen_links: set[tuple[str, str, str]] = set()
        for edge in _graph_links(old_graph):
            src, dst = _edge_endpoints(edge)
            if src in final_id_set and dst in final_id_set:
                key = (src, dst, str(edge.get("relation") or edge.get("type") or ""))
                if key not in seen_links:
                    links.append(_canonical_edge(src, dst, edge))
                    seen_links.add(key)
        for overlay_id, overlay_node in overlay_nodes.items():
            for dep in overlay_node.get("deps") or []:
                dep_id = str(dep).strip()
                if dep_id and dep_id in final_id_set and dep_id != overlay_id:
                    key = (dep_id, overlay_id, "depends_on")
                    if key not in seen_links:
                        links.append({"source": dep_id, "target": overlay_id, "relation": "depends_on"})
                        seen_links.add(key)

    final_doc = _node_link_document(final_nodes, links, extra_graphs=extra_graphs)
    validation_summary = _validate_materialized_graph(
        final_doc, graph_path=graph_path, workspace_dir=workspace_dir)

    graph_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = graph_path.with_name(
        f"{graph_path.name}.symbol-archive-{session_id}.bak")
    tmp_path = graph_path.with_name(f"{graph_path.name}.rebase-{session_id}.tmp")
    tmp_path.write_text(
        json.dumps(final_doc, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    shutil.copy2(str(graph_path), str(backup_path))
    tmp_path.replace(graph_path)

    node_state_rows = []
    for nid, node in final_nodes.items():
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        source = "overlay" if metadata.get("materialized_from_overlay") else "carry_forward"
        prior_state = existing_state.get(nid, {})
        verify_status = (
            node.get("verify_status")
            or (prior_state.get("verify_status") if source == "carry_forward" else "")
            or node.get("parsed_verify_status")
            or "pending"
        )
        build_status = (
            prior_state.get("build_status")
            if source == "carry_forward" and prior_state.get("build_status")
            else "impl:done"
        )
        node_state_rows.append(
            (project_id, nid, verify_status, build_status,
             json.dumps({
                 "type": "reconcile_session_materialized",
                 "session_id": session_id,
                 "source": source,
             }, ensure_ascii=False), "reconcile-session-finalize", finalized_at)
        )
    conn.executemany(
        "INSERT OR REPLACE INTO node_state "
        "(project_id, node_id, verify_status, build_status, evidence_json, "
        "updated_by, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        node_state_rows,
    )
    if archived_old:
        conn.executemany(
            "UPDATE node_state SET verify_status='archived_during_rebase', "
            "evidence_json=?, updated_by='reconcile-session-finalize', "
            "updated_at=?, version=version+1 "
            "WHERE project_id=? AND node_id=?",
            [
                (json.dumps({
                    "type": "reconcile_session_archived",
                    "session_id": session_id,
                }, ensure_ascii=False), finalized_at, project_id, nid)
                for nid in archived_old
            ],
        )

    counts = {
        "new_overlay_nodes": len(overlay_nodes),
        "carried_forward_nodes": len(carried_forward),
        "archived_orphan_nodes": len(archived_old),
        "replaced_old_nodes": len(replaced_old),
        "final_node_count": len(final_nodes),
        "final_edge_count": len(links),
        "duplicate_primary_count": validation_summary["duplicate_primary_count"],
    }
    for key in (
        "candidate_node_count",
        "candidate_link_count",
        "candidate_hierarchy_nodes",
        "candidate_leaf_nodes_remapped",
        "hierarchy_link_count",
        "evidence_link_count",
    ):
        if key in candidate_meta:
            counts[key] = candidate_meta[key]
    event_payload = {
        "event": "reconcile.session.materialized",
        "project_id": project_id,
        "session_id": session_id,
        "graph_path": str(graph_path),
        "graph_backup_path": str(backup_path),
        "overlay_path": str(overlay_path),
        "counts": counts,
        "candidate_materialization": candidate_meta,
        "duplicate_primary_examples": validation_summary["duplicate_primary_examples"],
        "full_rebase_flag": bool(full_rebase),
    }
    try:
        conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, session_id, "reconcile.session.materialized",
             json.dumps(event_payload, ensure_ascii=False), finalized_at),
        )
    except sqlite3.Error:
        pass
    return event_payload


def _row_to_session(row: sqlite3.Row) -> ReconcileSession:
    raw = row["bypass_gates_json"] if row["bypass_gates_json"] is not None else "[]"
    try:
        bypass = list(json.loads(raw) or [])
    except (TypeError, ValueError):
        bypass = []
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    finalize_error = {}
    if "finalize_error_json" in keys:
        try:
            finalize_error = dict(json.loads(row["finalize_error_json"] or "{}") or {})
        except (TypeError, ValueError):
            finalize_error = {}
    return ReconcileSession(
        project_id=row["project_id"], session_id=row["session_id"],
        run_id=row["run_id"], status=row["status"],
        started_at=row["started_at"], finalized_at=row["finalized_at"],
        cluster_count_total=int(row["cluster_count_total"] or 0),
        cluster_count_resolved=int(row["cluster_count_resolved"] or 0),
        cluster_count_failed=int(row["cluster_count_failed"] or 0),
        bypass_gates=bypass, started_by=row["started_by"],
        snapshot_path=row["snapshot_path"], snapshot_head_sha=row["snapshot_head_sha"],
        base_commit_sha=row["base_commit_sha"] if "base_commit_sha" in keys else "",
        target_branch=(
            row["target_branch"] if "target_branch" in keys and row["target_branch"]
            else default_target_branch(row["project_id"], row["session_id"])
        ),
        target_head_sha=(
            row["target_head_sha"] if "target_head_sha" in keys and row["target_head_sha"]
            else (row["base_commit_sha"] if "base_commit_sha" in keys else "")
        ),
        finalize_error=finalize_error,
    )


def _git_head_sha(cwd: Optional[Path] = None) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=str(cwd) if cwd else None, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def changed_files_since_base(base_commit_sha: str, *,
                             head: str = "HEAD",
                             cwd: Optional[Path] = None) -> list[str]:
    """Return repo-relative files changed since a reconcile session baseline."""
    base = str(base_commit_sha or "").strip()
    if not base:
        return []
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", f"{base}..{head}"],
            cwd=str(cwd or _repo_root()),
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    return [
        _normalize_rel(line)
        for line in out.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


def changed_files_for_session(session: Optional[ReconcileSession], *,
                              head: str = "HEAD",
                              cwd: Optional[Path] = None) -> list[str]:
    if session is None:
        return []
    return changed_files_since_base(session.base_commit_sha, head=head, cwd=cwd)


def get_active_session(conn: sqlite3.Connection, project_id: str) -> Optional[ReconcileSession]:
    """Return the active/finalizing/finalize_failed session for project_id, else None."""
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM reconcile_sessions WHERE project_id = ? "
        "AND status IN ('active','finalizing','finalize_failed') LIMIT 1", (project_id,)).fetchone()
    return _row_to_session(row) if row else None


def start_session(conn: sqlite3.Connection, project_id: str, *,
        session_id: Optional[str] = None, run_id: Optional[str] = None,
        started_by: Optional[str] = None,
        bypass_gates: Optional[Sequence[str]] = None,
        full_rebase: bool = False,
        dropped_cluster_fingerprints: Optional[Sequence[str]] = None,
        base_commit_sha: Optional[str] = None,
        target_branch: Optional[str] = None,
        governance_dir: Optional[Path] = None) -> ReconcileSession:
    """Insert a new active session; raise SessionAlreadyActiveError on conflict."""
    if full_rebase and not dropped_cluster_fingerprints:
        raise ValueError("full_rebase=True requires explicit dropped_cluster_fingerprints")
    sid = session_id or uuid.uuid4().hex
    now = _utcnow_iso()
    bypass_json = json.dumps(list(bypass_gates or []))
    base_sha = (base_commit_sha or _git_head_sha(_repo_root())).strip()
    branch_name = (target_branch or default_target_branch(project_id, sid)).strip()
    try:
        conn.execute(
            "INSERT INTO reconcile_sessions (project_id, session_id, run_id, status, "
            "started_at, bypass_gates_json, started_by, base_commit_sha, "
            "snapshot_head_sha, target_branch, target_head_sha) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (project_id, sid, run_id, now, bypass_json, started_by, base_sha,
             base_sha, branch_name, base_sha))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        msg = str(exc).lower()
        if "idx_reconcile_sessions_one_active" in msg or "unique" in msg:
            raise SessionAlreadyActiveError(
                f"a reconcile session is already active for project {project_id!r}") from exc
        raise
    overlay = _overlay_path(governance_dir)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(json.dumps({
        "session_id": sid,
        "project_id": project_id,
        "base_commit_sha": base_sha,
        "target_branch": branch_name,
        "target_head_sha": base_sha,
    }))
    return ReconcileSession(project_id=project_id, session_id=sid, run_id=run_id,
        status="active", started_at=now,
        bypass_gates=list(bypass_gates or []), started_by=started_by,
        base_commit_sha=base_sha, snapshot_head_sha=base_sha,
        target_branch=branch_name, target_head_sha=base_sha)


def transition_to_finalizing(conn: sqlite3.Connection, project_id: str,
        session_id: str) -> ReconcileSession:
    cur = conn.execute(
        "UPDATE reconcile_sessions SET status='finalizing' "
        "WHERE project_id=? AND session_id=? AND status='active'",
        (project_id, session_id))
    if cur.rowcount == 0:
        raise ValueError(f"no active session {session_id!r} for {project_id!r}")
    conn.commit()
    sess = get_active_session(conn, project_id)
    if sess is None:
        raise ValueError("session vanished after transition")
    return sess


def _cluster_gate_summary(
        conn: sqlite3.Connection, project_id: str, session_id: str) -> dict:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT run_id FROM reconcile_sessions WHERE project_id=? AND session_id=?",
        (project_id, session_id)).fetchone()
    run_id = row["run_id"] if row is not None else None
    try:
        from . import reconcile_deferred_queue as q

        summary = q.sync_session_counts(
            project_id, run_id=run_id, session_id=session_id, conn=conn)
    except Exception:
        return {"total": 0, "ready_for_finalize": True}
    return summary


def finalize_session(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        governance_dir: Optional[Path] = None,
        graph_path: Optional[Path] = None,
        workspace_dir: Optional[Path] = None,
        candidate_graph_path: Optional[Path] = None,
        full_rebase: bool = False,
        enforce_cluster_completion: bool = True) -> SessionFinalizationResult:
    if enforce_cluster_completion:
        summary = _cluster_gate_summary(conn, project_id, session_id)
        if int(summary.get("total") or 0) > 0 and not summary.get("ready_for_finalize"):
            raise SessionClusterGateError(
                "reconcile clusters are not complete; finish cluster chain pass before finalize",
                summary=summary,
            )
    row = conn.execute(
        "SELECT status FROM reconcile_sessions "
        "WHERE project_id=? AND session_id=? AND status IN ('active','finalizing','finalize_failed')",
        (project_id, session_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"no in-flight session {session_id!r} for {project_id!r}")
    now = _utcnow_iso()
    overlay = _overlay_path(governance_dir)
    target_graph = Path(graph_path) if graph_path is not None else _graph_path(governance_dir)
    conn.execute(
        "UPDATE reconcile_sessions SET status='finalizing', finalize_error_json='{}' "
        "WHERE project_id=? AND session_id=? AND status IN ('active','finalizing','finalize_failed')",
        (project_id, session_id),
    )
    conn.commit()
    try:
        materialized = materialize_overlay_to_graph(
            conn, project_id, session_id,
            overlay_path=overlay,
            graph_path=target_graph,
            workspace_dir=workspace_dir,
            candidate_graph_path=candidate_graph_path,
            full_rebase=full_rebase,
            finalized_at=now,
        )
    except Exception as exc:
        conn.rollback()
        error_payload = {
            "type": type(exc).__name__,
            "message": str(exc),
            "failed_at": _utcnow_iso(),
            "overlay_path": str(overlay),
            "graph_path": str(target_graph),
        }
        conn.execute(
            "UPDATE reconcile_sessions SET status='finalize_failed', "
            "finalize_error_json=? "
            "WHERE project_id=? AND session_id=? AND status='finalizing'",
            (json.dumps(error_payload, ensure_ascii=False), project_id, session_id),
        )
        try:
            conn.execute(
                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, session_id, "reconcile.session.finalize_failed",
                 json.dumps(error_payload, ensure_ascii=False), error_payload["failed_at"]),
            )
        except sqlite3.Error:
            pass
        conn.commit()
        raise
    cur = conn.execute(
        "UPDATE reconcile_sessions SET status='finalized', finalized_at=? "
        "WHERE project_id=? AND session_id=? AND status='finalizing'",
        (now, project_id, session_id))
    if cur.rowcount == 0:
        raise ValueError(f"no in-flight session {session_id!r} for {project_id!r}")
    archived: Optional[str] = None
    if overlay.exists():
        bak = overlay.with_suffix(overlay.suffix + ".bak")
        shutil.copy2(str(overlay), str(bak))
        overlay.unlink()
        archived = str(bak)
    conn.commit()
    counts = materialized.get("counts", {}) if isinstance(materialized, dict) else {}
    return SessionFinalizationResult(project_id=project_id, session_id=session_id,
        status="finalized", finalized_at=now, overlay_archived_to=archived,
        graph_path=str(target_graph),
        graph_backup_path=str(materialized.get("graph_backup_path") or ""),
        materialized_node_count=int(counts.get("final_node_count") or 0),
        materialization_counts=dict(counts))


def rollback_session(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        snapshot_path: Optional[Path] = None,
        governance_dir: Optional[Path] = None,
        restore_graph_snapshot: bool = False) -> SessionRollbackResult:
    now = _utcnow_iso()
    cur = conn.execute(
        "UPDATE reconcile_sessions SET status='rolled_back', finalized_at=? "
        "WHERE project_id=? AND session_id=? AND status IN ('active','finalizing','finalize_failed')",
        (now, project_id, session_id))
    if cur.rowcount == 0:
        raise ValueError(f"no in-flight session {session_id!r} for {project_id!r}")
    conn.commit()
    if snapshot_path is None:
        snapshot_path = _snapshot_dir(governance_dir) / f"{session_id}.tar.gz"
    snapshot_path = Path(snapshot_path)
    if restore_graph_snapshot and snapshot_path.exists():
        restore_snapshot(conn, project_id, session_id,
            snapshot_path=snapshot_path, governance_dir=governance_dir)
    overlay = _overlay_path(governance_dir)
    if overlay.exists():
        overlay.unlink()
    return SessionRollbackResult(project_id=project_id, session_id=session_id,
        status="rolled_back", rolled_back_at=now,
        snapshot_path=str(snapshot_path) if snapshot_path else None)


def is_gate_bypassed(session: Optional[ReconcileSession], gate_name: str) -> bool:
    if session is None or not gate_name:
        return False
    return gate_name in (session.bypass_gates or [])


def _dump_node_state(conn: sqlite3.Connection, project_id: str) -> str:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT project_id, node_id, verify_status, build_status, evidence_json, "
        "updated_by, updated_at, version FROM node_state "
        "WHERE project_id = ? ORDER BY node_id", (project_id,)).fetchall()
    parts = ["DELETE FROM node_state WHERE project_id = '"
             + project_id.replace("'", "''") + "';"]
    for r in rows:
        vals = [r["project_id"], r["node_id"], r["verify_status"], r["build_status"],
                r["evidence_json"], r["updated_by"], r["updated_at"], r["version"]]
        rendered = []
        for v in vals:
            if v is None:
                rendered.append("NULL")
            elif isinstance(v, int):
                rendered.append(str(v))
            else:
                rendered.append("'" + str(v).replace("'", "''") + "'")
        parts.append(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, "
            "evidence_json, updated_by, updated_at, version) VALUES ("
            + ", ".join(rendered) + ");")
    return "\n".join(parts) + "\n"


def _verify_status_distribution(conn: sqlite3.Connection, project_id: str) -> dict:
    rows = conn.execute(
        "SELECT verify_status, COUNT(*) AS n FROM node_state "
        "WHERE project_id = ? GROUP BY verify_status", (project_id,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["verify_status"]] = int(r["n"])
        except Exception:
            out[r[0]] = int(r[1])
    return out


def capture_snapshot(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        governance_dir: Optional[Path] = None) -> Path:
    """Write reconcile_snapshots/{session_id}.tar.gz with graph/node_state/manifest."""
    snap_dir = _snapshot_dir(governance_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    out_path = snap_dir / f"{session_id}.tar.gz"
    graph_p = _graph_path(governance_dir)
    graph_bytes = graph_p.read_bytes() if graph_p.exists() else b"{}"
    sql_dump = _dump_node_state(conn, project_id).encode("utf-8")
    node_count = conn.execute(
        "SELECT COUNT(*) FROM node_state WHERE project_id = ?",
        (project_id,)).fetchone()[0]
    manifest = {
        "project_id": project_id, "session_id": session_id,
        "head_commit_sha": _git_head_sha(_gov(governance_dir)),
        "taken_at": _utcnow_iso(), "node_count": int(node_count or 0),
        "verify_status_distribution": _verify_status_distribution(conn, project_id),
    }
    try:
        row = conn.execute(
            "SELECT base_commit_sha FROM reconcile_sessions "
            "WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
        if row is not None:
            manifest["base_commit_sha"] = row["base_commit_sha"] if isinstance(row, sqlite3.Row) else row[0]
    except sqlite3.Error:
        pass
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")

    def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(str(out_path), mode="w:gz") as tar:
        _add(tar, "graph.json", graph_bytes)
        _add(tar, "node_state.sql", sql_dump)
        _add(tar, "manifest.json", manifest_bytes)
    try:
        conn.execute(
            "UPDATE reconcile_sessions SET snapshot_path=?, snapshot_head_sha=? "
            "WHERE project_id=? AND session_id=?",
            (str(out_path), manifest["head_commit_sha"], project_id, session_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return out_path


def restore_snapshot(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        snapshot_path: Optional[Path] = None,
        governance_dir: Optional[Path] = None) -> RestoreResult:
    if snapshot_path is None:
        snapshot_path = _snapshot_dir(governance_dir) / f"{session_id}.tar.gz"
    snapshot_path = Path(snapshot_path)
    graph_bytes = b""
    sql_text = ""
    with tarfile.open(str(snapshot_path), mode="r:gz") as tar:
        gm = tar.extractfile("graph.json")
        if gm is not None:
            graph_bytes = gm.read()
        sm = tar.extractfile("node_state.sql")
        if sm is not None:
            sql_text = sm.read().decode("utf-8")
    graph_p = _graph_path(governance_dir)
    graph_p.parent.mkdir(parents=True, exist_ok=True)
    graph_p.write_bytes(graph_bytes)
    if sql_text:
        conn.executescript(sql_text)
        conn.commit()
    rows = conn.execute(
        "SELECT COUNT(*) FROM node_state WHERE project_id = ?",
        (project_id,)).fetchone()[0]
    return RestoreResult(project_id=project_id, session_id=session_id,
        graph_bytes=len(graph_bytes), node_state_rows=int(rows or 0))


__all__ = [
    "ReconcileSession", "SessionFinalizationResult", "SessionRollbackResult",
    "RestoreResult", "SessionAlreadyActiveError", "SessionClusterGateError",
    "get_active_session", "start_session", "transition_to_finalizing",
    "finalize_session", "rollback_session", "is_gate_bypassed",
    "capture_snapshot", "restore_snapshot", "changed_files_since_base",
    "changed_files_for_session", "default_target_branch",
]
