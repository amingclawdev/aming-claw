"""Commit-bound documentation asset state projection.

Documentation files are source artifacts first.  Weak path matches can propose
a binding, but impact scope should only consume accepted graph bindings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "doc_asset_state.v1"
DOC_FILE_KINDS = {"doc", "index_doc"}
ARCHIVE_STATUSES = {"archive"}
IGNORED_STATUSES = {"ignored"}


def build_doc_asset_state(
    *,
    project_id: str,
    run_id: str,
    commit_sha: str,
    file_inventory: Iterable[Mapping[str, Any]],
    graph_nodes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project doc file binding state from inventory plus graph nodes.

    The projection is replayable: rows come from committed file inventory and
    candidate/active graph node metadata.  AI or scanner proposals remain
    candidates until a source-controlled hint or review-gated flow materializes
    a graph binding.
    """

    nodes = [dict(node) for node in graph_nodes if isinstance(node, Mapping)]
    accepted_by_path = _accepted_doc_bindings(nodes)
    candidates_by_path = _doc_binding_candidates(nodes)
    docs: list[dict[str, Any]] = []

    for raw_row in file_inventory:
        if not isinstance(raw_row, Mapping):
            continue
        row = dict(raw_row)
        doc_kind = str(row.get("file_kind") or "")
        if doc_kind not in DOC_FILE_KINDS:
            continue
        path = _norm_path(row.get("path"))
        if not path:
            continue
        accepted = accepted_by_path.get(path, [])
        candidates = [] if accepted else candidates_by_path.get(path, [])
        status = _binding_status(row, accepted=accepted, candidates=candidates)
        docs.append({
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "run_id": run_id,
            "commit_sha": commit_sha,
            "path": path,
            "doc_kind": doc_kind,
            "sha256": str(row.get("sha256") or ""),
            "file_hash": str(row.get("file_hash") or ""),
            "size_bytes": int(row.get("size_bytes") or 0),
            "scan_status": str(row.get("scan_status") or ""),
            "graph_status": str(row.get("graph_status") or ""),
            "binding_status": status,
            "accepted_bindings": accepted,
            "binding_candidates": candidates,
            "impact_scope_policy": "accepted_bindings_only",
        })

    docs = sorted(docs, key=lambda item: item["path"])
    counts = Counter(str(row.get("binding_status") or "unbound") for row in docs)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "doc_count": len(docs),
        "accepted_count": counts.get("accepted", 0),
        "candidate_count": counts.get("candidate", 0),
        "unbound_count": counts.get("unbound", 0),
        "by_status": dict(sorted(counts.items())),
        "impact_scope_policy": "accepted_bindings_only",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "summary": summary,
        "docs": docs,
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


def _accepted_doc_bindings(nodes: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        node_id = str(node.get("id") or node.get("node_id") or "")
        title = str(node.get("title") or node.get("module") or node_id)
        for path in _path_list(node.get("secondary")):
            out[path].append({
                "node_id": node_id,
                "title": title,
                "role": "doc",
                "source": "graph_node",
            })
    return {path: sorted(values, key=lambda item: item["node_id"]) for path, values in out.items()}


def _doc_binding_candidates(nodes: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        candidates = metadata.get("asset_binding_candidates") if isinstance(metadata, Mapping) else []
        for raw_candidate in candidates or []:
            if not isinstance(raw_candidate, Mapping):
                continue
            if str(raw_candidate.get("asset_kind") or "") != "doc":
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


def _norm_path(raw: Any) -> str:
    return str(raw or "").replace("\\", "/").strip("/")


__all__ = ["SCHEMA_VERSION", "build_doc_asset_state"]
