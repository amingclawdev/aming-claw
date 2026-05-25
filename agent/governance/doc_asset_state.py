"""Commit-bound documentation asset state projection.

Documentation files are source artifacts first.  Weak path matches can propose
a binding, but impact scope should only consume accepted graph bindings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "doc_asset_state.v1"
DOC_FILE_KINDS = {"doc", "index_doc"}
ASSET_FILE_KINDS = {"doc", "index_doc", "test", "config"}
ARCHIVE_STATUSES = {"archive"}
IGNORED_STATUSES = {"ignored"}
ROLE_TO_ASSET_KIND = {
    "secondary": "doc",
    "secondary_files": "doc",
    "doc": "doc",
    "docs": "doc",
    "test": "test",
    "tests": "test",
    "test_files": "test",
    "config": "config",
    "config_files": "config",
}


def build_doc_asset_state(
    *,
    project_id: str,
    run_id: str,
    commit_sha: str,
    file_inventory: Iterable[Mapping[str, Any]],
    graph_nodes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project doc/test/config file binding state from inventory plus graph nodes.

    The projection is replayable: rows come from committed file inventory and
    candidate/active graph node metadata.  AI or scanner proposals remain
    candidates until a source-controlled hint or review-gated flow materializes
    a graph binding.
    """

    nodes = [dict(node) for node in graph_nodes if isinstance(node, Mapping)]
    accepted_by_kind_path = _accepted_asset_bindings(nodes)
    candidates_by_kind_path = _asset_binding_candidates(nodes)
    assets: list[dict[str, Any]] = []

    for raw_row in file_inventory:
        if not isinstance(raw_row, Mapping):
            continue
        row = dict(raw_row)
        file_kind = str(row.get("file_kind") or "")
        if file_kind not in ASSET_FILE_KINDS:
            continue
        asset_kind = _asset_kind_for_file_kind(file_kind)
        path = _norm_path(row.get("path"))
        if not path:
            continue
        accepted = accepted_by_kind_path.get(asset_kind, {}).get(path, [])
        candidates = [] if accepted else candidates_by_kind_path.get(path, [])
        status = _binding_status(row, accepted=accepted, candidates=candidates)
        asset = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "run_id": run_id,
            "commit_sha": commit_sha,
            "asset_kind": asset_kind,
            "path": path,
            "doc_kind": file_kind if asset_kind == "doc" else "",
            "file_kind": file_kind,
            "sha256": str(row.get("sha256") or ""),
            "file_hash": str(row.get("file_hash") or ""),
            "size_bytes": int(row.get("size_bytes") or 0),
            "scan_status": str(row.get("scan_status") or ""),
            "graph_status": str(row.get("graph_status") or ""),
            "binding_status": status,
            "accepted_bindings": accepted,
            "binding_candidates": candidates,
            "impact_scope_policy": "accepted_bindings_only",
        }
        assets.append(asset)

    assets = sorted(assets, key=lambda item: (item["asset_kind"], item["path"]))
    docs = [item for item in assets if item["asset_kind"] == "doc"]
    tests = [item for item in assets if item["asset_kind"] == "test"]
    configs = [item for item in assets if item["asset_kind"] == "config"]
    counts = Counter(str(row.get("binding_status") or "unbound") for row in docs)
    all_counts = Counter(str(row.get("binding_status") or "unbound") for row in assets)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "doc_count": len(docs),
        "test_count": len(tests),
        "config_count": len(configs),
        "asset_count": len(assets),
        "accepted_count": counts.get("accepted", 0),
        "candidate_count": counts.get("candidate", 0),
        "unbound_count": counts.get("unbound", 0),
        "by_status": dict(sorted(counts.items())),
        "asset_by_status": dict(sorted(all_counts.items())),
        "impact_scope_policy": "accepted_bindings_only",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "summary": summary,
        "assets": assets,
        "docs": docs,
        "tests": tests,
        "configs": configs,
    }


def _binding_status(
    row: Mapping[str, Any],
    *,
    accepted: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    scan_status = str(row.get("scan_status") or "")
    if accepted:
        return "accepted"
    if scan_status in IGNORED_STATUSES:
        return "ignored"
    if scan_status in ARCHIVE_STATUSES:
        return "archive"
    if candidates:
        return "candidate"
    return "unbound"


def _accepted_asset_bindings(
    nodes: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    out: dict[str, dict[str, list[dict[str, Any]]]] = {
        "doc": defaultdict(list),
        "test": defaultdict(list),
        "config": defaultdict(list),
    }
    for node in nodes:
        node_id = str(node.get("id") or node.get("node_id") or "")
        title = str(node.get("title") or node.get("module") or node_id)
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        raw_paths_by_kind = {
            "doc": _path_list(node.get("secondary")) + _path_list(node.get("secondary_files")),
            "test": _path_list(node.get("test")) + _path_list(node.get("tests")) + _path_list(node.get("test_files")),
            "config": (
                _path_list(node.get("config"))
                + _path_list(node.get("config_files"))
                + _path_list(metadata.get("config_files") if isinstance(metadata, Mapping) else [])
            ),
        }
        for asset_kind, paths in raw_paths_by_kind.items():
            for path in sorted(set(paths)):
                out[asset_kind][path].append({
                    "node_id": node_id,
                    "title": title,
                    "role": asset_kind,
                    "source": "graph_node",
                })
    return {
        kind: {path: sorted(values, key=lambda item: item["node_id"]) for path, values in paths.items()}
        for kind, paths in out.items()
    }


def _asset_binding_candidates(nodes: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        candidates = metadata.get("asset_binding_candidates") if isinstance(metadata, Mapping) else []
        for raw_candidate in candidates or []:
            if not isinstance(raw_candidate, Mapping):
                continue
            if str(raw_candidate.get("asset_kind") or "") not in {"doc", "test", "config"}:
                continue
            path = _norm_path(raw_candidate.get("asset_path"))
            if not path:
                continue
            out[path].append(_compact_candidate(raw_candidate, node))
    return {path: sorted(values, key=lambda item: item["proposal_hash"]) for path, values in out.items()}


def _compact_candidate(candidate: Mapping[str, Any], node: Mapping[str, Any]) -> dict[str, Any]:
    precheck = candidate.get("self_precheck") if isinstance(candidate.get("self_precheck"), Mapping) else {}
    proposal_hash = str(candidate.get("proposal_hash") or precheck.get("proposal_hash") or "")
    return {
        "schema_version": str(candidate.get("schema_version") or ""),
        "operation": str(candidate.get("operation") or ""),
        "asset_kind": str(candidate.get("asset_kind") or ""),
        "asset_path": _norm_path(candidate.get("asset_path")),
        "target_node_id": str(candidate.get("target_node_id") or node.get("id") or node.get("node_id") or ""),
        "target_module": str(candidate.get("target_module") or ""),
        "target_title": str(candidate.get("target_title") or node.get("title") or ""),
        "evidence_kind": str(candidate.get("evidence_kind") or ""),
        "source": str(candidate.get("source") or ""),
        "proposal_hash": proposal_hash,
        "precheck": dict(precheck),
    }


def _path_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
        values = list(raw)
    else:
        values = []
    return sorted({path for item in values if (path := _norm_path(item))})


def _asset_kind_for_file_kind(file_kind: str) -> str:
    if file_kind in {"doc", "index_doc"}:
        return "doc"
    if file_kind == "test":
        return "test"
    if file_kind == "config":
        return "config"
    return file_kind


def _norm_path(raw: Any) -> str:
    return str(raw or "").replace("\\", "/").strip("/")


__all__ = ["SCHEMA_VERSION", "build_doc_asset_state"]
