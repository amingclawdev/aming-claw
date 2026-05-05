"""Graph context selection for chain gates and prompts.

Normal chain tasks read the active governance graph.  Reconcile-cluster tasks
must read the session-local candidate graph plus approved overlay instead, so
the same graph/doc/test checks can run without coupling to stale graph.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


RECONCILE_CLUSTER_OPERATION = "reconcile-cluster"
GRAPH_OVERLAY_FILENAME = "graph.rebase.overlay.json"


@dataclass(frozen=True)
class GraphContext:
    mode: str
    candidate_graph_path: str = ""
    overlay_path: str = ""


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _state_root(project_id: str) -> Path:
    shared = os.environ.get("SHARED_VOLUME_PATH")
    if shared:
        root = Path(shared)
    else:
        root = _workspace_root() / "shared-volume"
    return root / "codex-tasks" / "state" / "governance" / project_id


def _governance_dir() -> Path:
    return Path(__file__).resolve().parent


def _normalize_path(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\\", "/").strip()
    if text.lower() in {"none", "null", "n/a", "na", "-"}:
        return ""
    while text.startswith("./"):
        text = text[2:]
    return text


def _path_list(*values: Any) -> list[str]:
    paths: list[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, str):
            items: Iterable[Any] = [value]
        elif isinstance(value, dict):
            items = []
        else:
            try:
                items = list(value)
            except TypeError:
                items = [value]
        for item in items:
            path = _normalize_path(item)
            if path and path not in paths:
                paths.append(path)
    return paths


def _node_id(node: dict[str, Any]) -> str:
    for key in ("id", "node_id", "candidate_node_id"):
        value = _normalize_path(node.get(key))
        if value:
            return value
    return ""


def _primary_paths(node: dict[str, Any]) -> list[str]:
    return _path_list(node.get("primary"), node.get("primary_files"))


def _doc_paths(node: dict[str, Any]) -> list[str]:
    return [
        path for path in _path_list(node.get("secondary"), node.get("secondary_files"))
        if path.endswith(".md")
    ]


def _test_paths(node: dict[str, Any]) -> list[str]:
    coverage = node.get("test_coverage")
    coverage_files = coverage.get("test_files") if isinstance(coverage, dict) else []
    return _path_list(node.get("test"), node.get("tests"), node.get("test_files"), coverage_files)


def _load_json(path: str | Path) -> dict[str, Any]:
    try:
        p = Path(path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _section_nodes(section: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(section, dict):
        return {}
    raw_nodes = section.get("nodes")
    if raw_nodes is None:
        raw_nodes = section.get("node_link", {}).get("nodes") if isinstance(section.get("node_link"), dict) else None
    nodes: dict[str, dict[str, Any]] = {}
    if isinstance(raw_nodes, dict):
        iterable = raw_nodes.items()
        for key, node in iterable:
            if isinstance(node, dict):
                clean = dict(node)
                clean.setdefault("id", key)
                nid = _node_id(clean)
                if nid:
                    nodes[nid] = clean
    elif isinstance(raw_nodes, list):
        for node in raw_nodes:
            if isinstance(node, dict):
                nid = _node_id(node)
                if nid:
                    nodes[nid] = dict(node)
    return nodes


def _graph_nodes(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    for key in ("deps_graph", "hierarchy_graph", "evidence_graph"):
        nodes = _section_nodes(doc.get(key))
        if nodes:
            return nodes
    nodes = _section_nodes(doc)
    if nodes:
        return nodes
    raw = doc.get("nodes")
    if isinstance(raw, dict):
        return {
            str(key): {"id": str(key), **value}
            for key, value in raw.items()
            if isinstance(value, dict)
        }
    return {}


def _section_links(section: Any) -> list[dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    raw = section.get("links")
    if raw is None:
        raw = section.get("edges")
    if raw is None and isinstance(section.get("node_link"), dict):
        raw = section["node_link"].get("links") or section["node_link"].get("edges")
    return [dict(edge) for edge in raw or [] if isinstance(edge, dict)]


def _graph_links(doc: dict[str, Any]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in ("deps_graph", "hierarchy_graph", "evidence_graph"):
        for edge in _section_links(doc.get(key)):
            src = _normalize_path(edge.get("source") or edge.get("from") or edge.get("from_node"))
            dst = _normalize_path(edge.get("target") or edge.get("to") or edge.get("to_node"))
            relation = _normalize_path(edge.get("relation") or edge.get("type"))
            if not src or not dst:
                continue
            item = (src, dst, relation)
            if item not in seen:
                links.append({"source": src, "target": dst, "relation": relation})
                seen.add(item)
    return links


def _candidate_graph_path(metadata: dict[str, Any]) -> str:
    for container in (
        metadata,
        metadata.get("cluster_payload") if isinstance(metadata.get("cluster_payload"), dict) else {},
        metadata.get("cluster_report") if isinstance(metadata.get("cluster_report"), dict) else {},
    ):
        value = container.get("candidate_graph_path") if isinstance(container, dict) else ""
        if value:
            return str(value)
    return ""


def _overlay_path(metadata: dict[str, Any]) -> str:
    for key in ("overlay_path", "reconcile_overlay_path"):
        value = metadata.get(key)
        if value:
            return str(value)
    return str(_governance_dir() / GRAPH_OVERLAY_FILENAME)


def _has_explicit_overlay_path(metadata: dict[str, Any]) -> bool:
    return any(metadata.get(key) for key in ("overlay_path", "reconcile_overlay_path"))


def _payload_candidate_nodes(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payload = metadata.get("cluster_payload") if isinstance(metadata.get("cluster_payload"), dict) else {}
    raw_nodes = payload.get("candidate_nodes") or payload.get("proposed_nodes") or []
    nodes: dict[str, dict[str, Any]] = {}
    for node in raw_nodes:
        if isinstance(node, dict):
            nid = _node_id(node)
            if nid:
                nodes[nid] = dict(node)
    return nodes


def is_reconcile_graph_context(metadata: dict[str, Any] | None) -> bool:
    return isinstance(metadata, dict) and metadata.get("operation_type") == RECONCILE_CLUSTER_OPERATION


def resolve_context(project_id: str, metadata: dict[str, Any] | None = None) -> GraphContext:
    metadata = metadata if isinstance(metadata, dict) else {}
    if is_reconcile_graph_context(metadata):
        return GraphContext(
            mode="reconcile_session",
            candidate_graph_path=_candidate_graph_path(metadata),
            overlay_path=_overlay_path(metadata),
        )
    return GraphContext(mode="active", candidate_graph_path=str(_state_root(project_id) / "graph.json"))


def load_reconcile_context_nodes(
    project_id: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], GraphContext]:
    """Return candidate+overlay nodes for a reconcile-cluster graph context."""
    metadata = metadata if isinstance(metadata, dict) else {}
    context = resolve_context(project_id, metadata)
    candidate_doc = _load_json(context.candidate_graph_path) if context.candidate_graph_path else {}
    candidate_nodes = _graph_nodes(candidate_doc) or _payload_candidate_nodes(metadata)
    should_load_overlay = bool(context.candidate_graph_path or _has_explicit_overlay_path(metadata))
    overlay_doc = _load_json(context.overlay_path) if context.overlay_path and should_load_overlay else {}
    overlay_nodes = _graph_nodes(overlay_doc)

    merged = dict(candidate_nodes)
    for overlay_id, overlay_node in overlay_nodes.items():
        primary = set(_primary_paths(overlay_node))
        matched_candidate = [
            cid for cid, candidate in candidate_nodes.items()
            if primary and primary.intersection(_primary_paths(candidate))
        ]
        if len(matched_candidate) == 1:
            base = dict(candidate_nodes[matched_candidate[0]])
            base.update(overlay_node)
            base.setdefault("id", overlay_id)
            base["primary"] = _path_list(overlay_node.get("primary"), candidate_nodes[matched_candidate[0]].get("primary"))
            base["secondary"] = _path_list(
                overlay_node.get("secondary"),
                overlay_node.get("secondary_files"),
                candidate_nodes[matched_candidate[0]].get("secondary"),
                candidate_nodes[matched_candidate[0]].get("secondary_files"),
            )
            base["test"] = _path_list(
                overlay_node.get("test"),
                overlay_node.get("tests"),
                overlay_node.get("test_files"),
                _test_paths(candidate_nodes[matched_candidate[0]]),
            )
            merged[matched_candidate[0]] = base
        else:
            merged[overlay_id] = overlay_node
    return merged, candidate_doc, context


def _path_exists(path: str, workspace_root: str | Path | None = None) -> bool:
    p = Path(path)
    if p.is_absolute():
        return p.exists()
    roots = []
    if workspace_root:
        roots.append(Path(workspace_root))
    roots.append(_workspace_root())
    roots.append(Path.cwd())
    return any((root / path).exists() for root in roots)


def get_graph_doc_associations(
    project_id: str,
    target_files: list[str],
    *,
    metadata: dict[str, Any] | None = None,
    workspace_root: str | Path | None = None,
) -> list[str]:
    """Return graph-linked docs for target files in the active graph context."""
    if not is_reconcile_graph_context(metadata):
        return []
    nodes, _candidate_doc, _context = load_reconcile_context_nodes(project_id, metadata)
    target_set = {_normalize_path(path) for path in target_files or []}
    docs: set[str] = set()
    for node in nodes.values():
        primary = set(_primary_paths(node))
        secondary_docs = set(_doc_paths(node))
        if primary.intersection(target_set):
            docs.update(secondary_docs)
        if secondary_docs.intersection(target_set):
            docs.update(path for path in primary if path.endswith(".md"))
    return sorted(path for path in docs if _path_exists(path, workspace_root=workspace_root))


def get_related_nodes(
    project_id: str,
    target_files: list[str],
    *,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Return node ids whose primary files match the target files."""
    if not is_reconcile_graph_context(metadata):
        return []
    nodes, _candidate_doc, _context = load_reconcile_context_nodes(project_id, metadata)
    target_set = {_normalize_path(path) for path in target_files or []}
    related = [
        nid for nid, node in nodes.items()
        if set(_primary_paths(node)).intersection(target_set)
    ]
    return sorted(related)


def _proposed_primary(proposed_nodes: list[dict[str, Any]] | None) -> set[str]:
    primary: set[str] = set()
    for node in proposed_nodes or []:
        if isinstance(node, dict):
            primary.update(_primary_paths(node))
    return primary


def build_reconcile_graph_preflight(
    project_id: str,
    metadata: dict[str, Any] | None = None,
    *,
    proposed_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize session-local graph impact for PM/Dev/QA/Gatekeeper prompts."""
    metadata = metadata if isinstance(metadata, dict) else {}
    nodes, candidate_doc, context = load_reconcile_context_nodes(project_id, metadata)
    target_files = _path_list(metadata.get("target_files"))
    target_set = set(target_files) | _proposed_primary(proposed_nodes)
    target_node_ids = [
        nid for nid, node in nodes.items()
        if set(_primary_paths(node)).intersection(target_set)
    ]
    target_id_set = set(target_node_ids)

    impacted_ids: set[str] = set(target_node_ids)
    for edge in _graph_links(candidate_doc):
        src = edge["source"]
        dst = edge["target"]
        if src in target_id_set:
            impacted_ids.add(dst)
        if dst in target_id_set:
            impacted_ids.add(src)

    related_docs: set[str] = set()
    related_tests: set[str] = set()
    coverage: list[dict[str, Any]] = []
    for nid in sorted(impacted_ids):
        node = nodes.get(nid)
        if not node:
            continue
        docs = _doc_paths(node)
        tests = _test_paths(node)
        related_docs.update(docs)
        related_tests.update(tests)
        if nid in target_id_set:
            coverage.append({
                "node_id": nid,
                "primary": _primary_paths(node),
                "doc_status": "covered" if docs else "missing",
                "test_status": "covered" if tests else "missing",
                "docs": docs,
                "tests": tests,
            })

    candidate_leaf_primary = {
        path for node in _graph_nodes(candidate_doc).values()
        for path in _primary_paths(node)
    }
    should_load_overlay = bool(context.candidate_graph_path or _has_explicit_overlay_path(metadata))
    overlay_doc = _load_json(context.overlay_path) if context.overlay_path and should_load_overlay else {}
    overlay_primary = {
        path for node in _graph_nodes(overlay_doc).values()
        for path in _primary_paths(node)
    }
    proposed_primary = _proposed_primary(proposed_nodes)
    remaining = sorted(candidate_leaf_primary - overlay_primary - proposed_primary)

    return {
        "mode": context.mode,
        "candidate_graph_path": context.candidate_graph_path,
        "overlay_path": context.overlay_path,
        "target_files": sorted(target_set),
        "target_node_ids": sorted(target_node_ids),
        "impacted_node_ids": sorted(impacted_ids),
        "related_docs": sorted(related_docs),
        "related_tests": sorted(related_tests),
        "coverage": coverage,
        "remaining_candidate_leaf_count": len(remaining),
        "remaining_candidate_leaf_sample": remaining[:20],
    }
