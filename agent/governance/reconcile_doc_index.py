"""Final reconcile doc index and coverage report.

This module generates the operator-facing signoff artifact for a graph rebase
session.  It reads candidate graph + approved overlay + file inventory and
never mutates graph.json.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable


DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
FEATURE_INDEX_MARKER_START = "<!-- RECONCILE-FEATURE-INDEX:START -->"
FEATURE_INDEX_MARKER_END = "<!-- RECONCILE-FEATURE-INDEX:END -->"


def _normalize(path: Any) -> str:
    text = str(path or "").replace("\\", "/").strip()
    if text.lower() in {"none", "null", "n/a", "na", "-"}:
        return ""
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _path_list(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, dict):
            items = []
        else:
            try:
                items = list(value)
            except TypeError:
                items = [value]
        for item in items:
            path = _normalize(item)
            if path and path not in out:
                out.append(path)
    return out


def _load_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"JSON input not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {p}")
    return data


def _write_text_lf(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or node.get("candidate_node_id") or "").strip()


def _section_nodes(section: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(section, dict):
        return {}
    raw = section.get("nodes")
    nodes: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                clean = dict(value)
                clean.setdefault("id", str(key))
                nid = _node_id(clean)
                if nid:
                    nodes[nid] = clean
    elif isinstance(raw, list):
        for node in raw:
            if isinstance(node, dict):
                nid = _node_id(node)
                if nid:
                    nodes[nid] = dict(node)
    return nodes


def _candidate_nodes(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    for key in ("deps_graph", "hierarchy_graph", "evidence_graph"):
        nodes = _section_nodes(doc.get(key))
        if nodes:
            return nodes
    return _section_nodes(doc)


def _overlay_nodes(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _section_nodes(doc)


def _primary(node: dict[str, Any]) -> list[str]:
    return _path_list(node.get("primary"), node.get("primary_files"))


def _docs(node: dict[str, Any]) -> list[str]:
    return [
        p for p in _path_list(node.get("secondary"), node.get("secondary_files"))
        if _is_doc_path(p)
    ]


def _tests(node: dict[str, Any]) -> list[str]:
    coverage = node.get("test_coverage")
    coverage_files = coverage.get("test_files") if isinstance(coverage, dict) else []
    return _path_list(node.get("test"), node.get("tests"), node.get("test_files"), coverage_files)


def _merge_unique(*values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for seq in values:
        for item in seq:
            path = _normalize(item)
            if path and path not in out:
                out.append(path)
    return out


def _node_sort_key(node_id: str) -> tuple[int, int, str]:
    match = re.match(r"^L(\d+)\.(\d+)$", str(node_id or ""))
    if match:
        return (int(match.group(1)), int(match.group(2)), "")
    return (999, 999, str(node_id or ""))


def _markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "").replace("\n", " ").strip()
    return text.replace("|", "\\|")


def _format_paths(paths: Iterable[str], *, max_items: int = 3) -> str:
    clean = [_normalize(path) for path in paths or []]
    clean = [path for path in clean if path]
    if not clean:
        return "missing"
    shown = clean[:max_items]
    rendered = "<br>".join(f"`{_markdown_cell(path)}`" for path in shown)
    if len(clean) > max_items:
        rendered += f"<br>+{len(clean) - max_items} more"
    return rendered


def _feature_node_id(feature: dict[str, Any]) -> str:
    return str(feature.get("overlay_node_id") or feature.get("candidate_node_id") or "")


def _is_doc_path(path: str) -> bool:
    p = _normalize(path)
    return Path(p).suffix.lower() in DOC_SUFFIXES or p.startswith("docs/")


def _is_index_doc(path: str) -> bool:
    p = _normalize(path)
    name = Path(p).name.lower()
    return p in {"README.md", "WORKFLOW.md"} or name in {"readme.md", "index.md"}


def _inventory_summary(rows: list[dict[str, Any]], referenced_files: set[str]) -> dict[str, Any]:
    unresolved: list[dict[str, str]] = []
    resolved_referenced: list[dict[str, str]] = []
    nonblocking_unreferenced: list[dict[str, str]] = []
    index_docs: list[dict[str, Any]] = []
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        path = _normalize(row.get("path"))
        kind = str(row.get("file_kind") or "")
        status = str(row.get("scan_status") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        if _is_index_doc(path):
            index_docs.append({
                "path": path,
                "file_kind": kind,
                "scan_status": status,
                "graph_referenced": path in referenced_files,
                "index_asset": True,
            })
            continue
        if status in {"orphan", "pending_decision", "error"} and kind in {"source", "test", "doc"}:
            if path in referenced_files:
                resolved_referenced.append({
                    "path": path,
                    "file_kind": kind,
                    "scan_status": status,
                    "reason": str(row.get("reason") or ""),
                    "resolution": "graph_referenced",
                })
                continue
            unresolved.append({
                "path": path,
                "file_kind": kind,
                "scan_status": status,
                "reason": str(row.get("reason") or ""),
            })
        elif status in {"archive", "support", "ignored"} and kind in {"source", "test", "doc"}:
            if path not in referenced_files:
                nonblocking_unreferenced.append({
                    "path": path,
                    "file_kind": kind,
                    "scan_status": status,
                    "reason": str(row.get("reason") or ""),
                })
    return {
        "by_kind": dict(sorted(by_kind.items())),
        "by_status": dict(sorted(by_status.items())),
        "index_docs": sorted(index_docs, key=lambda x: x["path"]),
        "resolved_referenced_files": sorted(
            resolved_referenced, key=lambda x: (x["file_kind"], x["path"])
        ),
        "nonblocking_unreferenced_files": sorted(
            nonblocking_unreferenced, key=lambda x: (x["file_kind"], x["path"])
        ),
        "unresolved_files": sorted(unresolved, key=lambda x: (x["file_kind"], x["path"])),
    }


def build_final_doc_index(
    *,
    project_id: str,
    session_id: str,
    candidate_graph_path: str | Path,
    overlay_path: str | Path,
    file_inventory_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a signoff report for the final reconcile graph candidate."""
    candidate_doc = _load_json(candidate_graph_path)
    overlay_doc = _load_json(overlay_path)
    candidate_nodes = _candidate_nodes(candidate_doc)
    overlay_nodes = _overlay_nodes(overlay_doc)

    overlay_by_primary: dict[str, str] = {}
    for overlay_id, node in overlay_nodes.items():
        for path in _primary(node):
            overlay_by_primary[path] = overlay_id

    features: list[dict[str, Any]] = []
    referenced_files: set[str] = set()
    missing_source_leafs: list[dict[str, Any]] = []
    missing_doc_nodes: list[dict[str, Any]] = []
    missing_test_nodes: list[dict[str, Any]] = []

    for node_id, candidate in sorted(candidate_nodes.items()):
        primaries = _primary(candidate)
        if not primaries:
            continue
        overlay_ids = sorted({overlay_by_primary[p] for p in primaries if p in overlay_by_primary})
        overlay = overlay_nodes.get(overlay_ids[0], {}) if len(overlay_ids) == 1 else {}
        docs = _merge_unique(_docs(overlay), _docs(candidate))
        tests = _merge_unique(_tests(overlay), _tests(candidate))
        primary = _merge_unique(_primary(overlay), primaries)
        referenced_files.update(primary)
        referenced_files.update(docs)
        referenced_files.update(tests)
        approved = len(overlay_ids) == 1
        feature = {
            "candidate_node_id": node_id,
            "overlay_node_id": overlay_ids[0] if approved else "",
            "title": overlay.get("title") or candidate.get("title") or node_id,
            "primary": primary,
            "docs": docs,
            "tests": tests,
            "approved": approved,
            "doc_status": "covered" if docs else "missing",
            "test_status": "covered" if tests else "missing",
        }
        features.append(feature)
        if not approved:
            missing_source_leafs.append(feature)
        if approved and not docs:
            missing_doc_nodes.append(feature)
        if approved and not tests:
            missing_test_nodes.append(feature)

    inventory = _inventory_summary(list(file_inventory_rows or []), referenced_files)
    blocking_issues = []
    if missing_source_leafs:
        blocking_issues.append("candidate_source_leaf_missing_from_overlay")
    if missing_doc_nodes:
        blocking_issues.append("approved_feature_missing_doc")
    if missing_test_nodes:
        blocking_issues.append("approved_feature_missing_test")
    if inventory["unresolved_files"]:
        blocking_issues.append("file_inventory_unresolved")

    return {
        "project_id": project_id,
        "session_id": session_id,
        "candidate_graph_path": str(candidate_graph_path),
        "overlay_path": str(overlay_path),
        "summary": {
            "candidate_leaf_count": len(features),
            "approved_leaf_count": len([f for f in features if f["approved"]]),
            "missing_source_leaf_count": len(missing_source_leafs),
            "missing_doc_count": len(missing_doc_nodes),
            "missing_test_count": len(missing_test_nodes),
            "unresolved_file_count": len(inventory["unresolved_files"]),
            "index_doc_count": len(inventory["index_docs"]),
            "ready_for_signoff": not blocking_issues,
            "blocking_issues": blocking_issues,
        },
        "features": features,
        "missing_source_leafs": missing_source_leafs,
        "missing_doc_nodes": missing_doc_nodes,
        "missing_test_nodes": missing_test_nodes,
        "inventory": inventory,
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        f"# Reconcile Final Doc Index: {report.get('project_id', '')}",
        "",
        f"- session_id: `{report.get('session_id', '')}`",
        f"- ready_for_signoff: `{summary.get('ready_for_signoff')}`",
        f"- approved_leaf_count: `{summary.get('approved_leaf_count')}` / `{summary.get('candidate_leaf_count')}`",
        f"- missing_doc_count: `{summary.get('missing_doc_count')}`",
        f"- missing_test_count: `{summary.get('missing_test_count')}`",
        f"- unresolved_file_count: `{summary.get('unresolved_file_count')}`",
        "",
        "## Blocking Issues",
    ]
    issues = summary.get("blocking_issues") or []
    if issues:
        lines.extend(f"- `{issue}`" for issue in issues)
    else:
        lines.append("- none")
    lines.extend(["", "## Features"])
    for feature in report.get("features", []):
        lines.append(f"### {feature.get('overlay_node_id') or feature.get('candidate_node_id')}: {feature.get('title', '')}")
        lines.append(f"- candidate_node_id: `{feature.get('candidate_node_id', '')}`")
        lines.append(f"- approved: `{feature.get('approved')}`")
        lines.append(f"- code: {', '.join(f'`{p}`' for p in feature.get('primary', [])) or 'none'}")
        lines.append(f"- docs: {', '.join(f'`{p}`' for p in feature.get('docs', [])) or 'missing'}")
        lines.append(f"- tests: {', '.join(f'`{p}`' for p in feature.get('tests', [])) or 'missing'}")
        lines.append("")
    lines.append("## Index Docs")
    for item in report.get("inventory", {}).get("index_docs", []):
        lines.append(
            f"- `{item.get('path')}` status=`{item.get('scan_status')}` "
            f"graph_referenced=`{item.get('graph_referenced')}`"
        )
    if not report.get("inventory", {}).get("index_docs"):
        lines.append("- none")
    lines.extend(["", "## Inventory Rows Resolved By Graph References"])
    for item in report.get("inventory", {}).get("resolved_referenced_files", []):
        lines.append(
            f"- `{item.get('path')}` kind=`{item.get('file_kind')}` "
            f"prior_status=`{item.get('scan_status')}` resolution=`{item.get('resolution')}`"
        )
    if not report.get("inventory", {}).get("resolved_referenced_files"):
        lines.append("- none")
    lines.extend(["", "## Nonblocking Unreferenced Files"])
    for item in report.get("inventory", {}).get("nonblocking_unreferenced_files", []):
        lines.append(
            f"- `{item.get('path')}` kind=`{item.get('file_kind')}` "
            f"status=`{item.get('scan_status')}` reason={item.get('reason', '')}"
        )
    if not report.get("inventory", {}).get("nonblocking_unreferenced_files"):
        lines.append("- none")
    lines.extend(["", "## Unresolved Files"])
    for item in report.get("inventory", {}).get("unresolved_files", []):
        lines.append(
            f"- `{item.get('path')}` kind=`{item.get('file_kind')}` "
            f"status=`{item.get('scan_status')}` reason={item.get('reason', '')}"
        )
    if not report.get("inventory", {}).get("unresolved_files"):
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def render_repo_feature_index(report: dict[str, Any], *, source_path: str | Path | None = None) -> str:
    """Render the compact repository-facing feature index.

    Unlike the review artifact, this file is intended to be checked into docs.
    It keeps coverage debt visible without requiring the user to inspect
    shared-volume artifacts directly.
    """
    summary = report.get("summary", {})
    features = sorted(
        [f for f in report.get("features", []) if isinstance(f, dict)],
        key=lambda f: _node_sort_key(_feature_node_id(f)),
    )
    inventory = report.get("inventory", {}) if isinstance(report.get("inventory"), dict) else {}
    source_label = _normalize(source_path) if source_path else _normalize(report.get("candidate_graph_path"))
    lines = [
        "# Governance Feature Index",
        "",
        "This index is generated from the latest reconcile doc-index review. It is",
        "the repo-level entry point for feature nodes, owned code, linked docs,",
        "linked tests, and remaining doc/test debt.",
        "",
        "## Source",
        "",
        f"- project_id: `{report.get('project_id', '')}`",
        f"- session_id: `{report.get('session_id', '')}`",
        f"- source_review: `{source_label}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Candidate feature leaves | `{summary.get('candidate_leaf_count', 0)}` |",
        f"| Approved feature leaves | `{summary.get('approved_leaf_count', 0)}` |",
        f"| Missing source leaves | `{summary.get('missing_source_leaf_count', 0)}` |",
        f"| Missing docs | `{summary.get('missing_doc_count', 0)}` |",
        f"| Missing tests | `{summary.get('missing_test_count', 0)}` |",
        f"| Unresolved files | `{summary.get('unresolved_file_count', 0)}` |",
        f"| Index docs tracked | `{summary.get('index_doc_count', 0)}` |",
        "",
        "## Blocking Issues",
        "",
    ]
    issues = summary.get("blocking_issues") or []
    if issues:
        lines.extend(f"- `{issue}`" for issue in issues)
    else:
        lines.append("- none")

    lines.extend([
        "",
        "## Feature Nodes",
        "",
        "| Node | Feature | Code | Docs | Tests | Debt |",
        "|---|---|---|---|---|---|",
    ])
    for feature in features:
        debt = []
        if not feature.get("approved"):
            debt.append("unapproved")
        if feature.get("doc_status") != "covered":
            debt.append("doc")
        if feature.get("test_status") != "covered":
            debt.append("test")
        lines.append(
            "| {node} | {title} | {code} | {docs} | {tests} | {debt} |".format(
                node=f"`{_markdown_cell(_feature_node_id(feature))}`",
                title=_markdown_cell(feature.get("title", "")),
                code=_format_paths(feature.get("primary", [])),
                docs=_format_paths(feature.get("docs", [])),
                tests=_format_paths(feature.get("tests", [])),
                debt=", ".join(debt) if debt else "none",
            )
        )

    lines.extend([
        "",
        "## Unresolved Files",
        "",
    ])
    unresolved = inventory.get("unresolved_files") or []
    if unresolved:
        lines.extend([
            "| Path | Kind | Status | Reason |",
            "|---|---|---|---|",
        ])
        for item in unresolved:
            lines.append(
                "| `{path}` | `{kind}` | `{status}` | {reason} |".format(
                    path=_markdown_cell(item.get("path", "")),
                    kind=_markdown_cell(item.get("file_kind", "")),
                    status=_markdown_cell(item.get("scan_status", "")),
                    reason=_markdown_cell(item.get("reason", "")),
                )
            )
    else:
        lines.append("- none")

    lines.extend([
        "",
        "## Index Docs",
        "",
    ])
    index_docs = inventory.get("index_docs") or []
    if index_docs:
        lines.extend([
            "| Path | Status | Graph Referenced |",
            "|---|---|---:|",
        ])
        for item in index_docs:
            lines.append(
                "| `{path}` | `{status}` | `{referenced}` |".format(
                    path=_markdown_cell(item.get("path", "")),
                    status=_markdown_cell(item.get("scan_status", "")),
                    referenced=_markdown_cell(item.get("graph_referenced", "")),
                )
            )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def render_governance_readme_feature_index_block(
    *,
    feature_index_path: str = "feature-index.md",
    summary: dict[str, Any] | None = None,
) -> str:
    summary = summary or {}
    return "\n".join([
        FEATURE_INDEX_MARKER_START,
        "## Generated Indices",
        "",
        "| File | Description |",
        "|------|-------------|",
        (
            f"| [{feature_index_path}]({feature_index_path}) | Reconcile feature index: "
            f"{summary.get('approved_leaf_count', 0)}/{summary.get('candidate_leaf_count', 0)} "
            f"approved feature nodes, {summary.get('missing_doc_count', 0)} doc gaps, "
            f"{summary.get('missing_test_count', 0)} test gaps |"
        ),
        FEATURE_INDEX_MARKER_END,
        "",
        "",
    ])


def upsert_governance_readme_feature_index(
    readme_text: str,
    *,
    feature_index_path: str = "feature-index.md",
    summary: dict[str, Any] | None = None,
) -> str:
    block = render_governance_readme_feature_index_block(
        feature_index_path=feature_index_path,
        summary=summary,
    )
    pattern = re.compile(
        re.escape(FEATURE_INDEX_MARKER_START)
        + r".*?"
        + re.escape(FEATURE_INDEX_MARKER_END)
        + r"(?:\r?\n)*",
        re.DOTALL,
    )
    if pattern.search(readme_text):
        return pattern.sub(block, readme_text)
    anchor = "\n## Specifications\n"
    if anchor in readme_text:
        return readme_text.replace(anchor, "\n" + block + anchor.lstrip("\n"), 1)
    return readme_text.rstrip() + "\n\n" + block


def materialize_repo_feature_index(
    *,
    review_json_path: str | Path,
    feature_index_path: str | Path,
    governance_readme_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write repo-facing feature index and optionally update governance README."""
    report = _load_json(review_json_path)
    feature_path = Path(feature_index_path)
    _write_text_lf(
        feature_path,
        render_repo_feature_index(report, source_path=review_json_path),
    )

    readme_updated = False
    if governance_readme_path is not None:
        readme_path = Path(governance_readme_path)
        existing = readme_path.read_text(encoding="utf-8") if readme_path.exists() else "# Governance Documentation\n"
        rel = os.path.relpath(feature_path, start=readme_path.parent).replace("\\", "/")
        updated = upsert_governance_readme_feature_index(
            existing,
            feature_index_path=rel,
            summary=report.get("summary", {}),
        )
        _write_text_lf(readme_path, updated)
        readme_updated = updated != existing

    return {
        "feature_index_path": str(feature_path),
        "governance_readme_path": str(governance_readme_path or ""),
        "readme_updated": readme_updated,
        "summary": report.get("summary", {}),
    }


def write_final_doc_index(
    *,
    project_id: str,
    session_id: str,
    candidate_graph_path: str | Path,
    overlay_path: str | Path,
    output_dir: str | Path,
    file_inventory_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write JSON and Markdown review artifacts and return the report payload."""
    report = build_final_doc_index(
        project_id=project_id,
        session_id=session_id,
        candidate_graph_path=candidate_graph_path,
        overlay_path=overlay_path,
        file_inventory_rows=file_inventory_rows,
    )
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "graph.rebase.doc-index.review.json"
    md_path = out / "graph.rebase.doc-index.review.md"
    _write_text_lf(json_path, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    _write_text_lf(md_path, render_markdown(report))
    report["artifact_paths"] = {"json": str(json_path), "markdown": str(md_path)}
    return report
