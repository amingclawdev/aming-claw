"""Dashboard-ready review bundle for graph governance snapshots."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import graph_snapshot_store as store
from . import reconcile_feedback


DASHBOARD_REVIEW_DIR = "dashboard-review"
DASHBOARD_REVIEW_BUNDLE_NAME = "bundle.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _decode_notes(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _layer_rank(layer: Any) -> int:
    text = str(layer or "").upper().strip()
    if text.startswith("L"):
        try:
            return int(text[1:].split(".", 1)[0])
        except ValueError:
            return 99
    return 99


def _node_brief(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node.get("node_id") or node.get("id") or "",
        "layer": node.get("layer") or "",
        "title": node.get("title") or "",
        "kind": node.get("kind") or "",
        "primary_files": node.get("primary_files") or node.get("primary") or [],
        "doc_files": node.get("secondary_files") or node.get("secondary") or [],
        "test_files": node.get("test_files") or node.get("test") or [],
        "subsystem": (node.get("metadata") or {}).get("subsystem")
        or (node.get("metadata") or {}).get("subsystem_key")
        or "",
    }


def _edge_brief(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "src": edge.get("src") or edge.get("source") or edge.get("from") or "",
        "dst": edge.get("dst") or edge.get("target") or edge.get("to") or "",
        "edge_type": edge.get("edge_type") or edge.get("type") or "",
        "direction": edge.get("direction") or "",
        "evidence": edge.get("evidence") or {},
    }


def _limited_graph(
    *,
    name: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    node_ids: set[str],
    edge_filter,
    node_limit: int,
    edge_limit: int,
) -> dict[str, Any]:
    selected_nodes = [_node_brief(node) for node in nodes if str(node.get("node_id") or "") in node_ids]
    selected_edges = [_edge_brief(edge) for edge in edges if edge_filter(_edge_brief(edge))]
    selected_edges = [
        edge for edge in selected_edges
        if edge["src"] in node_ids or edge["dst"] in node_ids
    ]
    visible_nodes = selected_nodes[:node_limit]
    visible_edges = selected_edges[:edge_limit]
    visible_node_ids = {str(node.get("node_id") or "") for node in visible_nodes}
    return {
        "name": name,
        "node_count": len(selected_nodes),
        "edge_count": len(selected_edges),
        "omitted_node_count": max(0, len(selected_nodes) - len(visible_nodes)),
        "omitted_edge_count": max(0, len(selected_edges) - len(visible_edges)),
        "nodes": visible_nodes,
        "edges": [
            edge for edge in visible_edges
            if edge["src"] in visible_node_ids or edge["dst"] in visible_node_ids
        ],
    }


def _mermaid_id(raw: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(raw or ""))
    return safe or "node"


def _mermaid_label(node: dict[str, Any]) -> str:
    title = str(node.get("title") or node.get("node_id") or "").replace('"', "'")
    return f'{node.get("node_id", "")}\\n{title[:60]}'


def _to_mermaid(graph: dict[str, Any], *, max_edges: int = 80) -> str:
    lines = ["graph TD"]
    for node in graph.get("nodes") or []:
        node_id = str(node.get("node_id") or "")
        lines.append(f'  {_mermaid_id(node_id)}["{_mermaid_label(node)}"]')
    known = {str(node.get("node_id") or "") for node in graph.get("nodes") or []}
    for edge in (graph.get("edges") or [])[:max_edges]:
        src = str(edge.get("src") or "")
        dst = str(edge.get("dst") or "")
        if src not in known or dst not in known:
            continue
        label = str(edge.get("edge_type") or "")
        lines.append(f"  {_mermaid_id(src)} -- {label} --> {_mermaid_id(dst)}")
    return "\n".join(lines)


def _semantic_summary(project_id: str, snapshot_id: str) -> dict[str, Any]:
    base = store.snapshot_companion_dir(project_id, snapshot_id) / "semantic-enrichment"
    semantic_index = _read_json(base / "semantic-index.json", {})
    review_report = _read_json(base / "semantic-review-report.json", {})
    graph_state = _read_json(base / "semantic-graph-state.json", {})
    features = semantic_index.get("features") if isinstance(semantic_index, dict) else []
    features = [feature for feature in features or [] if isinstance(feature, dict)]
    status_counts = Counter(str(feature.get("enrichment_status") or "") for feature in features)
    samples = [
        {
            "node_id": feature.get("node_id", ""),
            "feature_name": feature.get("feature_name", ""),
            "enrichment_status": feature.get("enrichment_status", ""),
            "domain_label": feature.get("domain_label", ""),
            "intent": feature.get("intent", ""),
            "doc_coverage_review": feature.get("doc_coverage_review", ""),
            "test_coverage_review": feature.get("test_coverage_review", ""),
            "config_coverage_review": feature.get("config_coverage_review", ""),
        }
        for feature in features[:25]
    ]
    return {
        "artifact_dir": str(base),
        "semantic_index_path": str(base / "semantic-index.json"),
        "semantic_review_report_path": str(base / "semantic-review-report.json"),
        "semantic_graph_state_path": str(base / "semantic-graph-state.json"),
        "feature_count": int(semantic_index.get("feature_count") or len(features) or 0)
        if isinstance(semantic_index, dict) else 0,
        "enrichment_status_counts": dict(sorted(status_counts.items())),
        "accepted_feature_count": len(graph_state.get("accepted_features") or {})
        if isinstance(graph_state, dict) else 0,
        "completed_node_count": len(graph_state.get("completed_node_ids") or [])
        if isinstance(graph_state, dict) else 0,
        "open_issue_count": len(graph_state.get("open_issues") or [])
        if isinstance(graph_state, dict) else 0,
        "ai_review_report": {
            key: review_report.get(key)
            for key in [
                "ai_input_mode",
                "ai_selected_count",
                "ai_complete_count",
                "ai_error_count",
                "ai_unavailable_count",
                "ai_batch_count",
                "ai_batch_complete_count",
                "dynamic_semantic_graph_state",
            ]
            if isinstance(review_report, dict) and key in review_report
        },
        "sample_features": samples,
    }


def build_dashboard_review_bundle(
    conn,
    project_id: str,
    snapshot_id: str,
    *,
    node_limit: int = 120,
    edge_limit: int = 240,
    queue_group_limit: int = 20,
    persist: bool = True,
) -> dict[str, Any]:
    """Build the graph/status/semantic/review surface a dashboard needs first."""
    store.ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    nodes = store.list_graph_snapshot_nodes(conn, project_id, snapshot_id, limit=1000)
    edges = store.list_graph_snapshot_edges(conn, project_id, snapshot_id, limit=2000)
    notes = _decode_notes(snapshot.get("notes"))
    files = store.list_graph_snapshot_files(conn, project_id, snapshot_id, limit=1000)

    by_layer = Counter(str(node.get("layer") or "") for node in nodes)
    by_kind = Counter(str(node.get("kind") or "") for node in nodes)
    by_edge_type = Counter(str(edge.get("edge_type") or "") for edge in edges)

    architecture_ids = {
        str(node.get("node_id") or "")
        for node in nodes
        if _layer_rank(node.get("layer")) <= 4
    }
    feature_ids = {
        str(node.get("node_id") or "")
        for node in nodes
        if _layer_rank(node.get("layer")) >= 5
    }
    hierarchy_graph = _limited_graph(
        name="architecture_hierarchy",
        nodes=nodes,
        edges=edges,
        node_ids=architecture_ids,
        edge_filter=lambda edge: edge.get("edge_type") == "contains"
        or edge.get("direction") == "hierarchy",
        node_limit=node_limit,
        edge_limit=edge_limit,
    )
    feature_dependency_graph = _limited_graph(
        name="feature_dependency",
        nodes=nodes,
        edges=edges,
        node_ids=feature_ids,
        edge_filter=lambda edge: edge.get("edge_type") != "contains"
        and edge.get("direction") != "hierarchy",
        node_limit=node_limit,
        edge_limit=edge_limit,
    )
    hierarchy_graph["mermaid"] = _to_mermaid(hierarchy_graph)
    feature_dependency_graph["mermaid"] = _to_mermaid(feature_dependency_graph)

    queue = reconcile_feedback.build_feedback_review_queue(
        project_id,
        snapshot_id,
        group_by="feature",
        limit=queue_group_limit,
    )
    feedback_summary = reconcile_feedback.feedback_summary(project_id, snapshot_id)

    bundle = {
        "ok": True,
        "project_id": project_id,
        "snapshot": {
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha", ""),
            "snapshot_kind": snapshot.get("snapshot_kind", ""),
            "status": snapshot.get("status", ""),
            "created_at": snapshot.get("created_at", ""),
            "graph_sha256": snapshot.get("graph_sha256", ""),
            "inventory_sha256": snapshot.get("inventory_sha256", ""),
        },
        "status": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes_by_layer": dict(sorted(by_layer.items())),
            "nodes_by_kind": dict(sorted(by_kind.items())),
            "edges_by_type": dict(sorted(by_edge_type.items())),
            "file_inventory": files.get("summary", {}),
            "pending_scope_reconcile": notes.get("pending_scope_reconcile", {}),
            "governance_index": {
                "feature_count": (notes.get("governance_index") or {}).get("feature_count", 0),
                "symbol_count": (notes.get("governance_index") or {}).get("symbol_count", 0),
                "doc_heading_count": (notes.get("governance_index") or {}).get("doc_heading_count", 0),
                "artifacts": (notes.get("governance_index") or {}).get("artifacts", {}),
            },
        },
        "graphs": {
            "architecture_hierarchy": hierarchy_graph,
            "feature_dependency": feature_dependency_graph,
        },
        "semantic": _semantic_summary(project_id, snapshot_id),
        "ai_review": {
            "feedback_summary": feedback_summary,
            "queue_summary": queue.get("summary", {}),
            "queue_groups": queue.get("groups", []),
        },
    }
    if persist:
        out = (
            store.snapshot_companion_dir(project_id, snapshot_id)
            / DASHBOARD_REVIEW_DIR
            / DASHBOARD_REVIEW_BUNDLE_NAME
        )
        bundle["artifact_path"] = str(out)
        _write_json(out, bundle)
    return bundle


__all__ = [
    "DASHBOARD_REVIEW_BUNDLE_NAME",
    "DASHBOARD_REVIEW_DIR",
    "build_dashboard_review_bundle",
]
