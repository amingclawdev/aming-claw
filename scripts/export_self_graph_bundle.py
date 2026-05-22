#!/usr/bin/env python3
"""Export a portable self graph bundle from a materialized graph snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESOURCES_DIR = ROOT / "agent" / "mcp" / "resources"
DEFAULT_BUNDLE_DIR = DEFAULT_RESOURCES_DIR / "self-graph-bundle"
DEFAULT_MANIFEST = DEFAULT_RESOURCES_DIR / "self-graph-bundle-manifest.json"
DEFAULT_SEED = DEFAULT_RESOURCES_DIR / "seed-graph-summary.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the current Aming Claw self graph and accepted semantic projection."
    )
    parser.add_argument(
        "--snapshot-dir",
        required=True,
        help="Materialized graph snapshot directory containing graph.json and semantic-enrichment/.",
    )
    parser.add_argument("--snapshot-id", required=True, help="Active graph snapshot id.")
    parser.add_argument("--projection-id", required=True, help="Active semantic projection id.")
    parser.add_argument("--event-watermark", required=True, type=int, help="Semantic projection event watermark.")
    parser.add_argument("--bundle-version", default="1.1.0", help="Portable bundle version.")
    parser.add_argument("--bundle-major", default=1, type=int, help="Portable bundle major version.")
    parser.add_argument("--project-id", default="aming-claw", help="Project id for the exported bundle.")
    parser.add_argument("--output-dir", default=str(DEFAULT_BUNDLE_DIR), help="Bundle output directory.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Root bundle manifest output path.")
    parser.add_argument("--seed-summary", default=str(DEFAULT_SEED), help="Existing seed graph summary resource.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snapshot_dir = Path(args.snapshot_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    seed_summary = Path(args.seed_summary).expanduser().resolve()

    graph_path = snapshot_dir / "graph.json"
    semantic_state_path = snapshot_dir / "semantic-enrichment" / "semantic-graph-state.json"
    semantic_index_path = snapshot_dir / "semantic-enrichment" / "semantic-index.json"
    graph = _load_json(graph_path)
    semantic_state = _load_json(semantic_state_path)
    semantic_index = _load_optional_json(semantic_index_path)

    source_commit = str(semantic_state.get("commit_sha") or "")
    generated_at = str(semantic_state.get("updated_at") or graph.get("generated_at") or "")
    if not source_commit:
        raise SystemExit("semantic graph state is missing commit_sha")
    if not seed_summary.is_file():
        raise SystemExit(f"seed graph summary not found: {seed_summary}")

    output_dir.mkdir(parents=True, exist_ok=True)
    graph_structure = _build_graph_structure(
        graph=graph,
        semantic_state=semantic_state,
        project_id=args.project_id,
        source_commit=source_commit,
        snapshot_id=args.snapshot_id,
        generated_at=generated_at,
        graph_path=graph_path,
    )
    semantic_projection = _build_semantic_projection(
        semantic_state=semantic_state,
        semantic_index=semantic_index,
        project_id=args.project_id,
        source_commit=source_commit,
        snapshot_id=args.snapshot_id,
        projection_id=args.projection_id,
        event_watermark=args.event_watermark,
        generated_at=generated_at,
        semantic_state_path=semantic_state_path,
        semantic_index_path=semantic_index_path,
    )

    graph_structure_path = output_dir / "graph-structure.json"
    semantic_projection_path = output_dir / "semantic-projection.json"
    _write_json(graph_structure_path, graph_structure, pretty=False)
    _write_json(semantic_projection_path, semantic_projection, pretty=False)

    root_manifest = _build_manifest(
        args=args,
        source_commit=source_commit,
        generated_at=generated_at,
        graph_structure=graph_structure,
        semantic_projection=semantic_projection,
        seed_summary=seed_summary,
        graph_structure_path=graph_structure_path,
        semantic_projection_path=semantic_projection_path,
        manifest_path=manifest_path,
    )
    _write_json(output_dir / "manifest.json", root_manifest)
    _write_json(manifest_path, root_manifest)
    _assert_no_private_paths([manifest_path, output_dir / "manifest.json", graph_structure_path, semantic_projection_path])
    print(json.dumps(_export_report(root_manifest), indent=2, sort_keys=True))
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return _load_json(path)


def _write_json(path: Path, payload: dict[str, Any], *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(payload, indent=2, sort_keys=True)
    else:
        text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _build_graph_structure(
    *,
    graph: dict[str, Any],
    semantic_state: dict[str, Any],
    project_id: str,
    source_commit: str,
    snapshot_id: str,
    generated_at: str,
    graph_path: Path,
) -> dict[str, Any]:
    deps_nodes = graph.get("deps_graph", {}).get("nodes") or []
    node_map: dict[str, dict[str, Any]] = {}
    function_index: list[dict[str, Any]] = []
    file_index: dict[str, dict[str, Any]] = {}

    for raw_node in deps_nodes:
        if not isinstance(raw_node, dict):
            continue
        node = _portable_node(raw_node)
        node_id = node.get("id")
        if not node_id:
            continue
        node_map[str(node_id)] = node
        _append_file_index(file_index, node)
        function_index.extend(_portable_function_index(node))

    graph_edges: list[dict[str, Any]] = []
    edge_counts: dict[str, int] = {}
    for graph_name in ("hierarchy_graph", "deps_graph", "evidence_graph"):
        links = graph.get(graph_name, {}).get("links") or []
        edge_counts[graph_name] = len(links)
        graph_edges.extend(_portable_edge(graph_name, edge) for edge in links if isinstance(edge, dict))

    return _sanitize({
        "schema_version": 1,
        "bundle_kind": "aming_claw_self_graph_structure",
        "project_id": project_id,
        "source_commit": source_commit,
        "snapshot_id": snapshot_id,
        "generated_at": generated_at,
        "source": {
            "graph_artifact": graph_path.name,
            "graph_sha256": _sha256(graph_path),
            "graph_generated_at": graph.get("generated_at", ""),
            "snapshot_kind": semantic_state.get("snapshot_kind", ""),
        },
        "metrics": {
            "node_count": len(node_map),
            "edge_count": len(graph_edges),
            "edge_counts": edge_counts,
            "function_index_count": len(function_index),
            "file_index_count": len(file_index),
        },
        "nodes": [node_map[node_id] for node_id in sorted(node_map, key=_node_sort_key)],
        "edges": sorted(graph_edges, key=lambda item: (
            str(item.get("graph") or ""),
            str(item.get("source") or ""),
            str(item.get("target") or ""),
            str(item.get("type") or ""),
        )),
        "function_index": sorted(function_index, key=lambda item: (
            str(item.get("path") or ""),
            int((item.get("line") or [0])[0] or 0),
            str(item.get("function") or ""),
        )),
        "file_index": [file_index[path] for path in sorted(file_index)],
    })


def _portable_node(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    portable_metadata = {
        "module": metadata.get("module", ""),
        "file_role": metadata.get("file_role", ""),
        "hierarchy_parent": metadata.get("hierarchy_parent", ""),
        "graph_metrics": metadata.get("graph_metrics", {}),
        "quality_flags": metadata.get("quality_flags", []),
        "feature_hash": metadata.get("feature_hash", ""),
        "hash_scheme": metadata.get("hash_scheme", ""),
        "file_hashes": metadata.get("file_hashes", {}),
        "functions": metadata.get("functions", []),
        "function_lines": metadata.get("function_lines", {}),
        "function_hashes": metadata.get("function_hashes", {}),
        "function_count": metadata.get("function_count", 0),
        "typed_relations": metadata.get("typed_relations", []),
    }
    return {
        "id": raw.get("id", ""),
        "layer": raw.get("layer", ""),
        "title": raw.get("title", ""),
        "primary": raw.get("primary", []) or [],
        "secondary": raw.get("secondary", []) or [],
        "test": raw.get("test", []) or [],
        "config": raw.get("config", []) or [],
        "artifacts": raw.get("artifacts", []) or [],
        "test_coverage": raw.get("test_coverage", ""),
        "verify_level": raw.get("verify_level", 0),
        "metadata": portable_metadata,
    }


def _append_file_index(file_index: dict[str, dict[str, Any]], node: dict[str, Any]) -> None:
    node_id = str(node.get("id") or "")
    if not node_id:
        return
    for role in ("primary", "secondary", "test", "config", "artifacts"):
        for path in node.get(role, []) or []:
            if not isinstance(path, str) or not path:
                continue
            entry = file_index.setdefault(path, {"path": path, "bindings": []})
            entry["bindings"].append({"node_id": node_id, "role": role})


def _portable_function_index(node: dict[str, Any]) -> list[dict[str, Any]]:
    node_id = str(node.get("id") or "")
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    module = str(metadata.get("module") or "")
    primary = node.get("primary") or []
    primary_path = primary[0] if primary else ""
    lines = metadata.get("function_lines") if isinstance(metadata.get("function_lines"), dict) else {}
    hashes = metadata.get("function_hashes") if isinstance(metadata.get("function_hashes"), dict) else {}
    functions = metadata.get("functions") if isinstance(metadata.get("functions"), list) else []

    index: list[dict[str, Any]] = []
    for full_name in functions:
        if not isinstance(full_name, str) or not full_name:
            continue
        short_name = full_name.split("::", 1)[-1]
        line = lines.get(short_name) or []
        index.append({
            "node_id": node_id,
            "module": module,
            "function": full_name,
            "short_name": short_name,
            "path": primary_path,
            "line": line,
            "hash": hashes.get(full_name, ""),
        })
    return index


def _portable_edge(graph_name: str, edge: dict[str, Any]) -> dict[str, Any]:
    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    payload = {
        "graph": graph_name,
        "source": edge.get("source", ""),
        "target": edge.get("target", ""),
        "type": edge.get("type", ""),
        "metadata": {
            key: metadata.get(key)
            for key in ("edge_kind", "relation_type", "source_role", "target_role")
            if metadata.get(key) not in (None, "")
        },
    }
    if edge.get("evidence_count") is not None:
        payload["evidence_count"] = edge.get("evidence_count")
    sample = edge.get("evidence_sample")
    if isinstance(sample, list):
        payload["evidence_sample"] = sample[:8]
    elif edge.get("evidence"):
        payload["evidence_sample"] = [edge.get("evidence")]
    return payload


def _build_semantic_projection(
    *,
    semantic_state: dict[str, Any],
    semantic_index: dict[str, Any],
    project_id: str,
    source_commit: str,
    snapshot_id: str,
    projection_id: str,
    event_watermark: int,
    generated_at: str,
    semantic_state_path: Path,
    semantic_index_path: Path,
) -> dict[str, Any]:
    node_semantics = semantic_state.get("node_semantics") if isinstance(semantic_state.get("node_semantics"), dict) else {}
    accepted_features = semantic_state.get("accepted_features") if isinstance(semantic_state.get("accepted_features"), dict) else {}
    semantic_nodes = {
        node_id: _portable_node_semantic(node_id, payload)
        for node_id, payload in node_semantics.items()
        if isinstance(node_id, str) and isinstance(payload, dict)
    }
    return _sanitize({
        "schema_version": 1,
        "bundle_kind": "aming_claw_self_semantic_projection",
        "project_id": project_id,
        "source_commit": source_commit,
        "snapshot_id": snapshot_id,
        "projection_id": projection_id,
        "event_watermark": event_watermark,
        "generated_at": generated_at,
        "source": {
            "semantic_state_artifact": semantic_state_path.name,
            "semantic_state_sha256": _sha256(semantic_state_path),
            "semantic_index_artifact": semantic_index_path.name if semantic_index_path.is_file() else "",
            "semantic_index_sha256": _sha256(semantic_index_path) if semantic_index_path.is_file() else "",
            "state_source": semantic_state.get("source", ""),
            "state_updated_at": semantic_state.get("updated_at", ""),
        },
        "metrics": {
            "accepted_node_semantics": len(semantic_nodes),
            "accepted_feature_count": len(accepted_features),
            "completed_node_count": len(semantic_state.get("completed_node_ids") or []),
            "edge_semantics": len(semantic_state.get("edge_semantics") or {}),
            "semantic_job_counts": semantic_state.get("semantic_job_counts", {}),
            "semantic_index_keys": sorted(semantic_index.keys()) if semantic_index else [],
        },
        "completed_node_ids": sorted(semantic_state.get("completed_node_ids") or [], key=_node_sort_key),
        "node_semantics": {node_id: semantic_nodes[node_id] for node_id in sorted(semantic_nodes, key=_node_sort_key)},
        "accepted_feature_index": _portable_accepted_features(accepted_features),
        "open_issues": semantic_state.get("open_issues", []),
        "health_issues": semantic_state.get("health_issues", []),
        "edge_semantics": semantic_state.get("edge_semantics", {}),
    })


def _portable_node_semantic(node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    audit = payload.get("graph_query_audit") or payload.get("semantic_graph_query_audit") or {}
    return {
        "node_id": node_id,
        "status": payload.get("status", ""),
        "feature_name": payload.get("feature_name", ""),
        "domain_label": payload.get("domain_label", ""),
        "intent": payload.get("intent", ""),
        "semantic_summary": payload.get("semantic_summary", ""),
        "quality_flags": payload.get("quality_flags", []),
        "doc_status": payload.get("doc_status", ""),
        "test_status": payload.get("test_status", ""),
        "config_status": payload.get("config_status", ""),
        "primary": payload.get("primary", []),
        "secondary": payload.get("secondary", []),
        "test": payload.get("test", []),
        "config": payload.get("config", []),
        "feature_hash": payload.get("feature_hash", ""),
        "payload_hash": payload.get("payload_hash", ""),
        "file_hashes": payload.get("file_hashes", {}),
        "function_hashes": payload.get("function_hashes", {}),
        "source_event_id": payload.get("source_event_id", ""),
        "source_snapshot_id": payload.get("source_snapshot_id", ""),
        "updated_at": payload.get("updated_at", ""),
        "self_check": payload.get("self_check", {}),
        "semantic_ai_self_check": payload.get("semantic_ai_self_check", {}),
        "graph_query_audit": _audit_summary(audit),
        "graph_structure_suggestions": payload.get("graph_structure_suggestions", []),
        "graph_structure_ops": payload.get("graph_structure_ops", {}),
        "graph_enrich_config_suggestions": payload.get("graph_enrich_config_suggestions", []),
        "graph_enrich_config_ops": payload.get("graph_enrich_config_ops", {}),
        "open_issues": payload.get("open_issues", []),
        "health_issues": payload.get("health_issues", []),
    }


def _audit_summary(audit: Any) -> dict[str, Any]:
    if not isinstance(audit, dict):
        return {}
    queries = audit.get("queries")
    if not isinstance(queries, list):
        return {
            "ok": audit.get("ok", None),
            "error": audit.get("error", ""),
            "query_count": 0,
            "tools": [],
        }
    tools: list[str] = []
    failed = 0
    hashes: list[dict[str, Any]] = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        tool = str(query.get("tool") or "")
        if tool and tool not in tools:
            tools.append(tool)
        if query.get("ok") is False:
            failed += 1
        hashes.append({
            "seq": query.get("seq"),
            "tool": tool,
            "args_hash": query.get("args_hash", ""),
            "result_hash": query.get("result_hash", ""),
            "result_count": query.get("result_count", 0),
        })
    return {
        "ok": audit.get("ok", failed == 0),
        "error": audit.get("error", ""),
        "query_count": len(hashes),
        "failed_query_count": failed,
        "tools": tools,
        "query_hashes": hashes,
    }


def _portable_accepted_features(accepted_features: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for node_id, payload in accepted_features.items():
        if not isinstance(node_id, str):
            continue
        if not isinstance(payload, dict):
            result[node_id] = payload
            continue
        result[node_id] = {
            "feature_name": payload.get("feature_name", ""),
            "domain_label": payload.get("domain_label", ""),
            "status": payload.get("status", ""),
            "payload_hash": payload.get("payload_hash", ""),
            "source_event_id": payload.get("source_event_id", ""),
            "updated_at": payload.get("updated_at", ""),
        }
    return {key: result[key] for key in sorted(result, key=_node_sort_key)}


def _build_manifest(
    *,
    args: argparse.Namespace,
    source_commit: str,
    generated_at: str,
    graph_structure: dict[str, Any],
    semantic_projection: dict[str, Any],
    seed_summary: Path,
    graph_structure_path: Path,
    semantic_projection_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    resources = [
        _resource_entry("seed_graph_summary", seed_summary, required=True),
        _resource_entry("graph_structure", graph_structure_path, required=True),
        _resource_entry("semantic_projection", semantic_projection_path, required=True),
    ]
    return {
        "schema_version": 1,
        "bundle_kind": "aming_claw_self_graph_semantic_bundle",
        "bundle_major": args.bundle_major,
        "bundle_version": args.bundle_version,
        "source_commit": source_commit,
        "source_commit_short": source_commit[:7],
        "snapshot_id": args.snapshot_id,
        "projection_id": args.projection_id,
        "event_watermark": args.event_watermark,
        "generated_at": generated_at,
        "manifest_resource": _repo_rel(manifest_path),
        "bundle_manifest_copy": "agent/mcp/resources/self-graph-bundle/manifest.json",
        "summary": {
            "node_count": graph_structure["metrics"]["node_count"],
            "edge_count": graph_structure["metrics"]["edge_count"],
            "function_index_count": graph_structure["metrics"]["function_index_count"],
            "accepted_node_semantics": semantic_projection["metrics"]["accepted_node_semantics"],
            "completed_node_count": semantic_projection["metrics"]["completed_node_count"],
            "edge_semantics": semantic_projection["metrics"]["edge_semantics"],
            "node_semantic_scope": "accepted node semantics only; missing nodes are intentionally not synthesized",
            "edge_semantic_scope": "edge semantic enrichment is not packaged in this V1 seal",
        },
        "source_truth_contract": {
            "trusted_sources": [
                "committed code at source_commit",
                "source-controlled hints/config/rules",
                "accepted semantic events through review gates",
                "deterministic reconcile output",
            ],
            "derived_state": [
                "graph structure read model",
                "semantic projection read model",
                "file/function indexes",
            ],
            "not_trusted_state": [
                "raw AI proposals before observer/review acceptance",
                "direct graph database edits",
            ],
        },
        "consumer_contract": {
            "supported_bundle_major": 1,
            "incompatible_major_action": "emit_plugin_update_reminder",
            "import_behavior": "read_only_context; do not import into governance DB automatically",
            "path_contract": "resources are repo-relative and must not contain private machine paths",
        },
        "resource_uris": {
            "manifest": "aming-claw://self-graph-bundle-manifest",
            "bundle_manifest": "aming-claw://self-graph-bundle/manifest",
            "graph_structure": "aming-claw://self-graph-bundle/graph-structure",
            "semantic_projection": "aming-claw://self-graph-bundle/semantic-projection",
            "seed_summary": "aming-claw://seed-graph-summary",
        },
        "resources": resources,
    }


def _resource_entry(role: str, path: Path, *, required: bool) -> dict[str, Any]:
    return {
        "role": role,
        "path": _repo_rel(path),
        "required": required,
        "mime_type": "application/json",
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        text = value.replace(str(ROOT), "").replace(str(ROOT.resolve()), "")
        text = text.replace("\\", "/")
        text = re.sub(r"/Users/[^\\s,'\")]+", "<redacted-local-path>", text)
        return text
    if isinstance(value, dict):
        return {str(_sanitize(key)): _sanitize(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    return value


def _assert_no_private_paths(paths: list[Path]) -> None:
    findings: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if "/Users/" in text or "\\Users\\" in text:
            findings.append(str(path))
    if findings:
        raise SystemExit("exported bundle contains private user paths: " + ", ".join(findings))


def _node_sort_key(value: Any) -> tuple[str, int, str]:
    text = str(value or "")
    match = re.fullmatch(r"([A-Za-z]+)(\d*)\.(\d+)", text)
    if not match:
        return (text, -1, text)
    prefix = f"{match.group(1)}{match.group(2)}"
    return (prefix, int(match.group(3)), text)


def _export_report(manifest: dict[str, Any]) -> dict[str, Any]:
    by_role = {item["role"]: {"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"]} for item in manifest["resources"]}
    return {
        "ok": True,
        "bundle_version": manifest["bundle_version"],
        "source_commit": manifest["source_commit"],
        "snapshot_id": manifest["snapshot_id"],
        "projection_id": manifest["projection_id"],
        "event_watermark": manifest["event_watermark"],
        "summary": manifest["summary"],
        "resources": by_role,
    }


if __name__ == "__main__":
    raise SystemExit(main())
