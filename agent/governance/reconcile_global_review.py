"""Incremental semantic/global review for scope-reconcile snapshots.

This module is state-only.  It catches a scope snapshot's semantic state up to
the changed files, compares it with the prior global picture, and records an
auditable graph query trace for the incremental review pass.
"""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import graph_query_trace
from . import graph_snapshot_store as store
from . import reconcile_feedback
from . import reconcile_semantic_enrichment as semantic


GlobalReviewAiCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def _decode(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
    return default


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(payload), encoding="utf-8")
    return str(path)


def _path_component(value: str, *, max_len: int = 32) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    if not text:
        text = "run"
    if len(text) <= max_len:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    prefix = text[: max_len - len(digest) - 1].rstrip("-._")
    return f"{prefix}-{digest}"


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    out: list[str] = []
    for item in values:
        text = str(item or "").strip().replace("\\", "/")
        if text and text not in out:
            out.append(text)
    return out


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def _status_penalty(kind: str, status: str) -> tuple[int, str]:
    text = str(status or "").strip().lower().replace("-", "_")
    if not text or text in {"n/a", "na", "not_applicable", "not_applicable.", "present", "covered", "good", "adequate"}:
        return 0, ""
    if kind == "doc":
        if _contains_any(text, ("missing", "insufficient", "under_documented")):
            return 8, "doc_status_poor"
        if _contains_any(text, ("weak", "thin")):
            return 5, "doc_status_weak"
        if _contains_any(text, ("partial", "no_anchor", "no_dedicated_anchor")):
            return 3, "doc_status_partial"
    if kind == "test":
        if "missing" in text:
            return 10, "test_status_missing"
        if _contains_any(text, ("weak", "thin", "minimal", "gaps")):
            return 6, "test_status_weak"
        if _contains_any(text, ("over_broad", "over-broad", "broad_but", "indirect_only", "too_broad")):
            return 5, "test_status_over_broad"
        if "partial" in text:
            return 4, "test_status_partial"
    if kind == "config":
        if _contains_any(text, ("missing", "insufficient")):
            return 3, "config_status_weak"
        if "minimal" in text:
            return 1, "config_status_minimal"
    return 0, ""


def _quality_flag_penalty(flag: str) -> tuple[int, str, str]:
    text = str(flag or "").strip().lower()
    if not text:
        return 0, "", ""
    if text == "semantic_hash_mismatch":
        return 0, "semantic_hash_mismatch", "governance_observability"
    if text == "semantic_ai_error":
        return 0, "semantic_ai_error", "governance_observability"
    if text in {"needs_human_signoff", "review_required"}:
        return 4, text, "semantic_review"
    if text == "missing_test_binding":
        return 4, text, "artifact_binding"
    if text == "missing_doc_binding":
        return 3, text, "artifact_binding"
    if text.startswith("missing_"):
        return 2, text, "artifact_binding"
    return 1, text, "semantic_review"


def _function_count_penalty(function_count: int) -> tuple[int, str]:
    if function_count >= 70:
        return 10, "very_large_feature_surface"
    if function_count >= 50:
        return 8, "large_feature_surface"
    if function_count >= 30:
        return 5, "high_function_count"
    if function_count >= 20:
        return 3, "moderate_function_count"
    return 0, ""


def _open_issue_penalties(open_issues: Any) -> tuple[dict[str, int], dict[str, int]]:
    if not isinstance(open_issues, list):
        return {}, {}
    raw_counts: dict[str, int] = {}
    for issue in open_issues:
        if not isinstance(issue, dict):
            raw_counts["open_issue"] = raw_counts.get("open_issue", 0) + 1
            continue
        issue_type = str(issue.get("type") or issue.get("kind") or "").lower()
        reason = str(issue.get("reason") or "").lower()
        summary = str(issue.get("summary") or issue.get("message") or "").lower()
        text = " ".join([issue_type, reason, summary])
        if "merge" in reason or "duplicate" in text or "overlap" in text:
            key = "duplicate_or_overlap_issue"
        elif "split" in reason or "split" in issue_type:
            key = "broad_responsibility_issue"
        elif "test" in text:
            key = "test_gap_issue"
        elif "doc" in text:
            key = "doc_gap_issue"
        elif "relation" in text or "dependency" in text or "edge" in text:
            key = "dependency_gap_issue"
        elif "code_review" in text:
            key = "code_review_issue"
        elif "observer" in text or "signoff" in text:
            key = "observer_decision_issue"
        else:
            key = "open_issue"
        raw_counts[key] = raw_counts.get(key, 0) + 1
    weights = {
        "duplicate_or_overlap_issue": 6,
        "broad_responsibility_issue": 4,
        "test_gap_issue": 4,
        "doc_gap_issue": 3,
        "dependency_gap_issue": 2,
        "code_review_issue": 3,
        "observer_decision_issue": 5,
        "open_issue": 1,
    }
    caps = {
        "duplicate_or_overlap_issue": 12,
        "broad_responsibility_issue": 10,
        "test_gap_issue": 12,
        "doc_gap_issue": 9,
        "dependency_gap_issue": 8,
        "code_review_issue": 6,
        "observer_decision_issue": 10,
        "open_issue": 5,
    }
    penalties = {
        key: min(raw_counts[key] * weights.get(key, 1), caps.get(key, 5))
        for key in raw_counts
    }
    return penalties, raw_counts


def _inventory_run_id_from_notes(notes: dict[str, Any]) -> str:
    for container_key in ("governance_index", "pending_scope_reconcile"):
        container = notes.get(container_key)
        if isinstance(container, dict):
            value = str(container.get("run_id") or "").strip()
            if value:
                return value
    return str(notes.get("run_id") or "").strip()


def _file_kind_weight(kind: str, *, pending: bool = False) -> float:
    normalized = str(kind or "").strip().lower()
    if pending:
        return {
            "source": 2.0,
            "test": 1.0,
            "doc": 0.5,
            "index_doc": 0.3,
            "config": 0.5,
            "script": 0.35,
            "unknown": 0.5,
            "generated": 0.05,
        }.get(normalized, 0.5)
    return {
        "source": 4.0,
        "test": 2.0,
        "doc": 1.0,
        "index_doc": 0.5,
        "config": 1.0,
        "script": 0.75,
        "unknown": 1.0,
        "generated": 0.05,
    }.get(normalized, 1.0)


def _file_hygiene_picture(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
    notes = _snapshot_notes(snapshot)
    run_id = _inventory_run_id_from_notes(notes)
    if not run_id or not _table_exists(conn, "reconcile_file_inventory"):
        return {
            "available": False,
            "run_id": run_id,
            "file_hygiene_score": 100.0,
            "project_health_penalty": 0.0,
            "reason": "missing_inventory_run" if not run_id else "missing_inventory_table",
        }

    rows = conn.execute(
        """
        SELECT path, file_kind, language, size_bytes, scan_status, graph_status,
               decision, reason, mapped_node_ids, attached_node_ids, attachment_role
        FROM reconcile_file_inventory
        WHERE project_id = ? AND run_id = ?
        """,
        (project_id, run_id),
    ).fetchall()
    if not rows:
        return {
            "available": False,
            "run_id": run_id,
            "file_hygiene_score": 100.0,
            "project_health_penalty": 0.0,
            "reason": "empty_inventory",
        }

    by_kind: dict[str, int] = {}
    by_scan_status: dict[str, int] = {}
    by_graph_status: dict[str, int] = {}
    orphan_by_kind: dict[str, int] = {}
    pending_by_kind: dict[str, int] = {}
    cleanup_by_kind: dict[str, int] = {}
    review_required: list[dict[str, Any]] = []
    cleanup_candidates: list[dict[str, Any]] = []
    review_count = 0
    orphan_count = 0
    pending_count = 0
    error_count = 0
    cleanup_count = 0
    cleanup_bytes = 0
    hygiene_penalty = 0.0
    project_penalty = 0.0

    for raw_row in rows:
        row = dict(raw_row)
        path = str(row.get("path") or "")
        kind = str(row.get("file_kind") or "unknown")
        scan = str(row.get("scan_status") or "")
        graph = str(row.get("graph_status") or "")
        decision = str(row.get("decision") or "")
        size = int(row.get("size_bytes") or 0)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_scan_status[scan] = by_scan_status.get(scan, 0) + 1
        by_graph_status[graph] = by_graph_status.get(graph, 0) + 1

        is_orphan = scan == "orphan" or graph == "unmapped"
        is_pending = scan == "pending_decision" or graph == "pending_decision"
        is_error = scan == "error" or graph == "error"
        is_cleanup = kind == "generated" or (scan == "ignored" and graph == "ignored" and decision == "ignore")

        if is_orphan:
            orphan_count += 1
            orphan_by_kind[kind] = orphan_by_kind.get(kind, 0) + 1
            weight = _file_kind_weight(kind)
            hygiene_penalty += weight * 1.4
            project_penalty += weight * 0.35
        if is_pending:
            pending_count += 1
            pending_by_kind[kind] = pending_by_kind.get(kind, 0) + 1
            weight = _file_kind_weight(kind, pending=True)
            hygiene_penalty += weight
            project_penalty += weight * 0.25
        if is_error:
            error_count += 1
            hygiene_penalty += 6
            project_penalty += 2
        if is_orphan or is_pending or is_error:
            review_count += 1
            if len(review_required) < 50:
                review_required.append({
                    "path": path,
                    "file_kind": kind,
                    "scan_status": scan,
                    "graph_status": graph,
                    "decision": decision,
                    "size_bytes": size,
                    "suggested_dashboard_actions": ["attach_to_node", "create_node", "delete_candidate", "waive"],
                })
        if is_cleanup:
            cleanup_count += 1
            cleanup_by_kind[kind] = cleanup_by_kind.get(kind, 0) + 1
            cleanup_bytes += size
            if len(cleanup_candidates) < 5000:
                cleanup_candidates.append({
                    "path": path,
                    "file_kind": kind,
                    "scan_status": scan,
                    "graph_status": graph,
                    "decision": decision,
                    "size_bytes": size,
                    "suggested_dashboard_actions": ["delete_candidate", "waive"],
                })

    cleanup_mb = cleanup_bytes / (1024 * 1024)
    cleanup_hygiene_penalty = min(25.0, cleanup_count * 0.03 + cleanup_mb * 0.04)
    cleanup_project_penalty = min(4.0, cleanup_count * 0.01 + cleanup_mb * 0.005)
    hygiene_penalty += cleanup_hygiene_penalty
    project_penalty += cleanup_project_penalty
    hygiene_penalty = min(70.0, hygiene_penalty)
    project_penalty = min(12.0, project_penalty)
    return {
        "available": True,
        "run_id": run_id,
        "total_files": len(rows),
        "file_hygiene_score": round(max(0.0, 100.0 - hygiene_penalty), 2),
        "project_health_penalty": round(project_penalty, 2),
        "review_required_count": review_count,
        "orphan_count": orphan_count,
        "pending_decision_count": pending_count,
        "error_count": error_count,
        "cleanup_candidate_count": cleanup_count,
        "cleanup_candidate_bytes": cleanup_bytes,
        "cleanup_candidate_mb": round(cleanup_mb, 2),
        "by_kind": dict(sorted(by_kind.items())),
        "by_scan_status": dict(sorted(by_scan_status.items())),
        "by_graph_status": dict(sorted(by_graph_status.items())),
        "orphan_by_kind": dict(sorted(orphan_by_kind.items())),
        "pending_decision_by_kind": dict(sorted(pending_by_kind.items())),
        "cleanup_candidate_by_kind": dict(sorted(cleanup_by_kind.items())),
        "review_required_sample": review_required,
        "cleanup_candidate_sample": sorted(
            cleanup_candidates,
            key=lambda item: int(item.get("size_bytes") or 0),
            reverse=True,
        )[:50],
    }


def _snapshot_notes(snapshot: dict[str, Any]) -> dict[str, Any]:
    notes = _decode(snapshot.get("notes"), {})
    return notes if isinstance(notes, dict) else {}


def _scope_delta(notes: dict[str, Any]) -> dict[str, Any]:
    pending = notes.get("pending_scope_reconcile") if isinstance(notes.get("pending_scope_reconcile"), dict) else {}
    delta = pending.get("scope_file_delta") if isinstance(pending.get("scope_file_delta"), dict) else {}
    return delta if isinstance(delta, dict) else {}


def _infer_base_snapshot_id(snapshot: dict[str, Any], explicit: str = "") -> str:
    if explicit:
        return explicit
    notes = _snapshot_notes(snapshot)
    pending = notes.get("pending_scope_reconcile") if isinstance(notes.get("pending_scope_reconcile"), dict) else {}
    for key in ("active_snapshot_id", "base_snapshot_id", "previous_snapshot_id"):
        value = str(pending.get(key) or "").strip()
        if value:
            return value
    return str(snapshot.get("parent_snapshot_id") or "")


def _infer_changed_paths(snapshot: dict[str, Any], explicit: Any = None) -> list[str]:
    explicit_paths = _string_list(explicit)
    if explicit_paths:
        return explicit_paths
    delta = _scope_delta(_snapshot_notes(snapshot))
    for key in ("impacted_files", "changed_files", "added_files", "hash_changed_files"):
        paths = _string_list(delta.get(key))
        if paths:
            return paths
    return []


def _node_paths(node: dict[str, Any]) -> set[str]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    paths = []
    for key in ("primary_files", "secondary_files", "test_files"):
        paths.extend(_string_list(node.get(key)))
    paths.extend(_string_list(metadata.get("config_files")))
    return set(paths)


def _changed_node_ids(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    changed_paths: list[str],
    explicit_node_ids: Any = None,
) -> list[str]:
    requested = set(_string_list(explicit_node_ids))
    changed = set(changed_paths)
    if not changed and requested:
        return sorted(requested)
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        layer="L7",
        limit=1000,
    )
    out: set[str] = set(requested)
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        paths = _node_paths(node)
        if changed.intersection(paths):
            out.add(node_id)
    return sorted(out)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def _load_semantic_nodes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, dict[str, Any]]:
    if not _table_exists(conn, "graph_semantic_nodes"):
        return {}
    rows = conn.execute(
        """
        SELECT node_id, status, feature_hash, file_hashes_json, semantic_json,
               feedback_round, batch_index, updated_at
        FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (project_id, snapshot_id),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _decode(row["semantic_json"], {})
        if not isinstance(payload, dict):
            payload = {}
        out[str(row["node_id"])] = {
            **payload,
            "node_id": str(row["node_id"]),
            "status": str(row["status"] or payload.get("status") or ""),
            "feature_hash": str(row["feature_hash"] or payload.get("feature_hash") or ""),
            "file_hashes": _decode(row["file_hashes_json"], {}),
            "feedback_round": row["feedback_round"],
            "batch_index": row["batch_index"],
            "updated_at": row["updated_at"],
        }
    return out


def _load_semantic_jobs(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, dict[str, Any]]:
    if not _table_exists(conn, "graph_semantic_jobs"):
        return {}
    rows = conn.execute(
        """
        SELECT node_id, status, feature_hash, feedback_round, batch_index,
               attempt_count, last_error, updated_at
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {str(row["node_id"]): dict(row) for row in rows}


def _semantic_picture(semantic_nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    features: list[dict[str, Any]] = []
    for node_id, entry in sorted(semantic_nodes.items()):
        status = str(entry.get("status") or "")
        domain = str(entry.get("domain_label") or "")
        by_status[status] = by_status.get(status, 0) + 1
        if domain:
            by_domain[domain] = by_domain.get(domain, 0) + 1
        features.append({
            "node_id": node_id,
            "feature_name": entry.get("feature_name") or entry.get("source_title") or node_id,
            "domain_label": domain,
            "status": status,
            "feature_hash": entry.get("feature_hash") or "",
        })
    return {
        "semantic_node_count": len(semantic_nodes),
        "by_status": dict(sorted(by_status.items())),
        "by_domain": dict(sorted(by_domain.items())),
        "features": features[:200],
    }


def _semantic_coverage_picture(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    semantic_nodes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        layer="L7",
        limit=2000,
    )
    total = len(nodes)
    semantic_complete = 0
    doc_bound = 0
    test_bound = 0
    config_bound = 0
    health_scores: list[int] = []
    artifact_scores: list[int] = []
    status_counts: dict[str, int] = {}
    quality_flag_counts: dict[str, int] = {}
    health_issue_counts: dict[str, int] = {}
    artifact_issue_counts: dict[str, int] = {}
    penalty_by_category: dict[str, int] = {}
    low_health: list[dict[str, Any]] = []
    semantic_pending: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        architecture = metadata.get("architecture_signals") if isinstance(metadata.get("architecture_signals"), dict) else {}
        semantic_entry = semantic_nodes.get(node_id, {})
        semantic_status = str(semantic_entry.get("status") or "")
        status_counts[semantic_status or "missing"] = status_counts.get(semantic_status or "missing", 0) + 1
        score = 100
        artifact_score = 100
        issues: list[str] = []
        artifact_issues: list[str] = []

        def add_penalty(issue: str, amount: int, category: str, *, artifact: bool = False) -> None:
            nonlocal score, artifact_score
            if amount <= 0 or not issue:
                return
            if artifact:
                artifact_score -= amount
                artifact_issues.append(issue)
                artifact_issue_counts[issue] = artifact_issue_counts.get(issue, 0) + 1
            score -= amount
            issues.append(issue)
            health_issue_counts[issue] = health_issue_counts.get(issue, 0) + 1
            penalty_by_category[category] = penalty_by_category.get(category, 0) + amount

        if semantic_status == "ai_complete":
            semantic_complete += 1
        else:
            semantic_pending.append({
                "node_id": node_id,
                "title": node.get("title") or node_id,
                "semantic_status": semantic_status or "missing",
                "primary_files": _string_list(node.get("primary_files"))[:10],
            })
        if _string_list(node.get("secondary_files")):
            doc_bound += 1
        else:
            add_penalty("missing_doc_binding", 6, "artifact_binding", artifact=True)
        if _string_list(node.get("test_files")):
            test_bound += 1
        else:
            add_penalty("missing_test_binding", 8, "artifact_binding", artifact=True)
        if _string_list(metadata.get("config_files")):
            config_bound += 1
        for kind, status in (
            ("doc", str(semantic_entry.get("doc_status") or "")),
            ("test", str(semantic_entry.get("test_status") or "")),
            ("config", str(semantic_entry.get("config_status") or "")),
        ):
            penalty, issue = _status_penalty(kind, status)
            add_penalty(issue, penalty, "artifact_status", artifact=(kind in {"doc", "test", "config"}))

        function_count = int(metadata.get("function_count") or 0)
        penalty, issue = _function_count_penalty(function_count)
        add_penalty(issue, penalty, "complexity")
        test_binding_count = len(_string_list(node.get("test_files")))
        doc_binding_count = len(_string_list(node.get("secondary_files")))
        if test_binding_count >= 30:
            add_penalty("very_broad_test_binding", 4, "artifact_binding", artifact=True)
        elif test_binding_count >= 15:
            add_penalty("broad_test_binding", 2, "artifact_binding", artifact=True)
        if doc_binding_count >= 10:
            add_penalty("broad_doc_binding", 2, "artifact_binding", artifact=True)
        roles = _string_list(architecture.get("roles"))
        typed_relations = metadata.get("typed_relations") if isinstance(metadata.get("typed_relations"), list) else []
        if (
            not typed_relations
            and any(role in {"orchestration", "state", "domain_contract", "gateway_entry"} for role in roles)
        ):
            add_penalty("important_node_without_typed_relations", 4, "dependency_model")
        for flag in _string_list(semantic_entry.get("quality_flags")):
            quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
            penalty, issue, category = _quality_flag_penalty(flag)
            add_penalty(issue, penalty, category, artifact=category == "artifact_binding")
        open_issue_penalties, open_issue_counts = _open_issue_penalties(semantic_entry.get("open_issues"))
        for issue, count in open_issue_counts.items():
            health_issue_counts[f"{issue}_reported"] = health_issue_counts.get(f"{issue}_reported", 0) + count
        for issue, penalty in open_issue_penalties.items():
            add_penalty(issue, penalty, "semantic_open_issues")
        score = max(0, score)
        artifact_score = max(0, artifact_score)
        health_scores.append(score)
        artifact_scores.append(artifact_score)
        if score < 90 or issues:
            low_health.append({
                "node_id": node_id,
                "title": node.get("title") or node_id,
                "score": score,
                "artifact_score": artifact_score,
                "semantic_status": semantic_status or "missing",
                "issues": sorted(set(issues)),
                "artifact_issues": sorted(set(artifact_issues)),
                "primary_files": _string_list(node.get("primary_files"))[:10],
                "doc_files": _string_list(node.get("secondary_files"))[:10],
                "test_files": _string_list(node.get("test_files"))[:10],
            })
    low_health.sort(key=lambda item: (int(item.get("score") or 0), str(item.get("node_id") or "")))
    avg_score = round(sum(health_scores) / len(health_scores), 2) if health_scores else 0.0
    artifact_avg_score = round(sum(artifact_scores) / len(artifact_scores), 2) if artifact_scores else 0.0
    def ratio(count: int) -> float:
        return round(count / total, 4) if total else 0.0
    semantic_coverage_ratio = ratio(semantic_complete)
    file_hygiene = _file_hygiene_picture(conn, project_id, snapshot_id)
    file_hygiene_penalty = float(file_hygiene.get("project_health_penalty") or 0.0)
    project_health_score = round(max(0.0, avg_score - file_hygiene_penalty), 2)
    return {
        "score_version": "project_health_v4_existing_data_plus_file_hygiene",
        "feature_count": total,
        "semantic_complete_count": semantic_complete,
        "semantic_pending_count": len(semantic_pending),
        "semantic_pending_nodes": semantic_pending[:100],
        "semantic_coverage_ratio": semantic_coverage_ratio,
        "governance_observability_score": round(semantic_coverage_ratio * 100, 2),
        "doc_bound_count": doc_bound,
        "doc_coverage_ratio": ratio(doc_bound),
        "test_bound_count": test_bound,
        "test_coverage_ratio": ratio(test_bound),
        "config_bound_count": config_bound,
        "artifact_binding_score": artifact_avg_score,
        "file_hygiene_score": file_hygiene.get("file_hygiene_score", 100.0),
        "file_hygiene": file_hygiene,
        "raw_project_health_score": avg_score,
        "project_health_score": project_health_score,
        "average_health_score": project_health_score,
        "semantic_status_counts": dict(sorted(status_counts.items())),
        "quality_flag_counts": dict(sorted(quality_flag_counts.items())),
        "artifact_issue_counts": dict(sorted(artifact_issue_counts.items())),
        "project_health_issue_counts": dict(sorted(health_issue_counts.items())),
        "project_health_penalty_by_category": dict(sorted(penalty_by_category.items())),
        "project_health_global_penalties": {
            "file_hygiene": file_hygiene_penalty,
        },
        "low_health_count": len(low_health),
        "low_health_nodes": low_health[:100],
    }


def _call_ai(ai_call: GlobalReviewAiCall | None, payload: dict[str, Any]) -> dict[str, Any]:
    if ai_call is None:
        return {}
    try:
        response = ai_call("reconcile_global_semantic_review", payload)
    except Exception as exc:  # noqa: BLE001 - review remains advisory
        return {"_ai_error": str(exc)}
    return response if isinstance(response, dict) else {}


def _global_review_dir(project_id: str, snapshot_id: str) -> Path:
    return store.snapshot_companion_dir(project_id, snapshot_id) / "semantic-enrichment" / "global-review"


def _trace_full_context(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str,
    run_id: str,
    project_root: str | Path | None,
    budget: dict[str, int] | None,
) -> dict[str, Any]:
    trace = graph_query_trace.start_trace(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
        query_source="ai_global_review",
        query_purpose="global_architecture_review",
        run_id=run_id,
        budget=budget,
    )["trace"]
    trace_id = trace["trace_id"]
    queries: list[dict[str, Any]] = []
    budget_exhausted = False

    def query(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal budget_exhausted
        if budget_exhausted:
            queries.append({
                "tool": tool,
                "args": args,
                "skipped": True,
                "skip_reason": "query_budget_exhausted",
            })
            return {}
        result = graph_query_trace.traced_query(
            conn,
            project_id,
            snapshot_id,
            trace_id=trace_id,
            tool=tool,
            args=args,
            project_root=project_root,
        )
        queries.append({
            "tool": tool,
            "args": args,
            "result_count": result.get("result_count", 0),
            "budget_exceeded": result.get("budget_exceeded", False),
        })
        if result.get("budget_exceeded") or result.get("error") == "query_budget_exceeded":
            budget_exhausted = True
        return result.get("result") if isinstance(result.get("result"), dict) else {}

    subsystems = query("list_subsystems", {"limit": 200})
    low_health = query("list_low_health_nodes", {"limit": 200})
    inspected: list[dict[str, Any]] = []
    for item in (low_health.get("nodes") or [])[:20]:
        node = item.get("node") if isinstance(item, dict) else {}
        node_id = str((node or {}).get("node_id") or "")
        if not node_id:
            continue
        inspected_node = query("get_node", {"node_id": node_id, "include_semantic": True})
        inspected.append({
            "node_id": node_id,
            "health": {
                "approx_health_score": item.get("approx_health_score"),
                "issues": item.get("issues") or [],
            },
            "node": inspected_node.get("node") or {},
            "semantic": inspected_node.get("semantic") or {},
        })
    finished = graph_query_trace.finish_trace(
        conn,
        project_id,
        trace_id,
        status="budget_exceeded" if budget_exhausted else "complete",
        reason=(
            "full global semantic review context budget exceeded"
            if budget_exhausted
            else "full global semantic review context captured"
        ),
    )["trace"]
    return {
        "trace_id": trace_id,
        "trace": {
            "status": finished.get("status"),
            "usage": finished.get("usage") or {},
            "event_count": finished.get("event_count", 0),
            "artifact_path": finished.get("artifact_path", ""),
        },
        "queries": queries,
        "subsystems": subsystems,
        "low_health": low_health,
        "inspected_nodes": inspected,
    }


def _update_snapshot_global_review_notes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    patch: dict[str, Any],
) -> None:
    row = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not row:
        return
    notes = _snapshot_notes(row)
    global_notes = notes.setdefault("global_semantic_review", {})
    if not isinstance(global_notes, dict):
        global_notes = {}
        notes["global_semantic_review"] = global_notes
    global_notes.update(patch)
    conn.execute(
        "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
        (_json(notes), project_id, snapshot_id),
    )


def run_full_global_review(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    project_root: str | Path | None = None,
    *,
    global_review_use_ai: bool = False,
    global_review_ai_call: GlobalReviewAiCall | None = None,
    actor: str = "observer",
    run_id: str = "",
    query_budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a full semantic health picture for one graph snapshot.

    This intentionally does not run feature-level semantic enrichment. It only
    reviews already stored structure/semantic state so callers can establish an
    old global picture before scope-level incremental review.
    """
    store.ensure_schema(conn)
    graph_query_trace.ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    rid = run_id or f"full-global-review-{snapshot_id}"
    rid_path = _path_component(rid)
    semantic_nodes = _load_semantic_nodes(conn, project_id, snapshot_id)
    semantic_picture = _semantic_picture(semantic_nodes)
    health_picture = _semantic_coverage_picture(
        conn,
        project_id,
        snapshot_id,
        semantic_nodes,
    )
    context = _trace_full_context(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
        run_id=rid,
        project_root=project_root,
        budget=query_budget,
    )
    ai_response: dict[str, Any] = {}
    ai_issues: list[dict[str, Any]] = []
    if global_review_use_ai:
        payload = {
            "schema_version": 1,
            "mode": "full_global_semantic_review",
            "permissions": {
                "can": ["query_graph", "read_provided_context", "return_review_suggestions"],
                "cannot": ["modify_code", "modify_docs", "modify_tests", "mutate_graph_topology", "file_backlog"],
            },
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha") or "",
            "semantic_picture": semantic_picture,
            "health_picture": health_picture,
            "graph_query_trace": context,
            "instructions": [
                "Review the entire semantic graph picture, not individual feature implementation.",
                "Identify global architecture risks such as duplicate capabilities, unclear ownership, missing semantic coverage, or unhealthy doc/test/config bindings.",
                "Return open_issues only for graph corrections or project improvements supported by evidence.",
                "Represent coverage/doc/test drift as status observations unless the user explicitly requests backlog filing.",
            ],
        }
        ai_response = _call_ai(global_review_ai_call, payload)
        raw_issues = ai_response.get("open_issues") if isinstance(ai_response, dict) else []
        ai_issues = [item for item in (raw_issues or []) if isinstance(item, dict)]

    generated_at = utc_now()
    report = {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot.get("commit_sha") or "",
        "run_id": rid,
        "generated_at": generated_at,
        "status": "reviewed",
        "semantic_picture": semantic_picture,
        "health_picture": health_picture,
        "graph_query_trace": context,
        "global_ai_review": {
            "requested": bool(global_review_use_ai),
            "response_present": bool(ai_response and not ai_response.get("_ai_error")),
            "error": ai_response.get("_ai_error", "") if isinstance(ai_response, dict) else "",
            "open_issue_count": len(ai_issues),
            "response": ai_response if global_review_use_ai else {},
        },
    }
    out_dir = _global_review_dir(project_id, snapshot_id)
    report_path = str(out_dir / f"{rid_path}.json")
    latest_path = str(out_dir / "latest-full-review.json")
    report["report_path"] = report_path
    report["latest_report_path"] = latest_path
    _write_json(Path(report_path), report)
    _write_json(Path(latest_path), report)
    _update_snapshot_global_review_notes(
        conn,
        project_id,
        snapshot_id,
        {
            "latest_full_review_path": latest_path,
            "latest_full_run_id": rid,
            "latest_full_status": report["status"],
            "latest_full_semantic_coverage_ratio": health_picture.get("semantic_coverage_ratio", 0),
            "latest_full_average_health_score": health_picture.get("average_health_score", 0),
            "updated_at": generated_at,
        },
    )
    return {"ok": True, **report}


def _trace_incremental_context(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str,
    run_id: str,
    changed_node_ids: list[str],
    project_root: str | Path | None,
    budget: dict[str, int] | None,
) -> dict[str, Any]:
    trace = graph_query_trace.start_trace(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
        query_source="ai_global_review",
        query_purpose="global_architecture_review",
        run_id=run_id,
        budget=budget,
    )["trace"]
    trace_id = trace["trace_id"]
    queries: list[dict[str, Any]] = []
    budget_exhausted = False

    def query(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal budget_exhausted
        if budget_exhausted:
            queries.append({
                "tool": tool,
                "args": args,
                "skipped": True,
                "skip_reason": "query_budget_exhausted",
            })
            return {}
        result = graph_query_trace.traced_query(
            conn,
            project_id,
            snapshot_id,
            trace_id=trace_id,
            tool=tool,
            args=args,
            project_root=project_root,
        )
        queries.append({
            "tool": tool,
            "args": args,
            "result_count": result.get("result_count", 0),
            "budget_exceeded": result.get("budget_exceeded", False),
        })
        if result.get("budget_exceeded") or result.get("error") == "query_budget_exceeded":
            budget_exhausted = True
        return result.get("result") if isinstance(result.get("result"), dict) else {}

    query("list_subsystems", {"limit": 50})
    low_health = query("list_low_health_nodes", {"limit": 50})
    inspected: list[dict[str, Any]] = []
    for node_id in changed_node_ids[:20]:
        node = query("get_node", {"node_id": node_id, "include_semantic": True})
        neighbors = query("get_neighbors", {"node_id": node_id, "limit": 40})
        inspected.append({
            "node_id": node_id,
            "node": node.get("node") or {},
            "semantic": node.get("semantic") or {},
            "neighbor_count": neighbors.get("count", 0),
        })
    finished = graph_query_trace.finish_trace(
        conn,
        project_id,
        trace_id,
        status="budget_exceeded" if budget_exhausted else "complete",
        reason=(
            "incremental global semantic review context budget exceeded"
            if budget_exhausted
            else "incremental global semantic review context captured"
        ),
    )["trace"]
    return {
        "trace_id": trace_id,
        "trace": {
            "status": finished.get("status"),
            "usage": finished.get("usage") or {},
            "event_count": finished.get("event_count", 0),
            "artifact_path": finished.get("artifact_path", ""),
        },
        "queries": queries,
        "low_health": low_health,
        "inspected_nodes": inspected,
    }


def run_incremental_global_review(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    project_root: str | Path | None = None,
    *,
    base_snapshot_id: str = "",
    changed_paths: Any = None,
    changed_node_ids: Any = None,
    run_semantic: bool = True,
    semantic_use_ai: bool | None = None,
    semantic_ai_call: GlobalReviewAiCall | None = None,
    semantic_ai_feature_limit: int | None = None,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_ai_input_mode: str | None = None,
    semantic_config_path: str | Path | None = None,
    classify_feedback: bool = True,
    global_review_use_ai: bool = False,
    global_review_ai_call: GlobalReviewAiCall | None = None,
    actor: str = "observer",
    run_id: str = "",
    query_budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Run the post-scope incremental semantic review closure."""
    store.ensure_schema(conn)
    graph_query_trace.ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    base_snapshot = _infer_base_snapshot_id(snapshot, base_snapshot_id)
    paths = _infer_changed_paths(snapshot, changed_paths)
    node_ids = _changed_node_ids(
        conn,
        project_id,
        snapshot_id,
        paths,
        explicit_node_ids=changed_node_ids,
    )
    rid = run_id or f"incremental-global-review-{snapshot_id}"
    rid_path = _path_component(rid)
    semantic_result: dict[str, Any] = {}
    if run_semantic:
        semantic_result = semantic.run_semantic_enrichment(
            conn,
            project_id,
            snapshot_id,
            project_root,
            use_ai=semantic_use_ai,
            ai_call=semantic_ai_call,
            created_by=actor,
            ai_feature_limit=semantic_ai_feature_limit,
            semantic_ai_batch_size=semantic_ai_batch_size,
            semantic_ai_batch_by=semantic_ai_batch_by,
            semantic_ai_input_mode=semantic_ai_input_mode,
            semantic_ai_scope="changed" if paths else "selected",
            semantic_changed_paths=paths,
            semantic_node_ids=node_ids,
            semantic_selector_match="any",
            semantic_graph_state=True,
            semantic_skip_completed=True,
            semantic_base_snapshot_id=base_snapshot,
            semantic_config_path=semantic_config_path,
            trace_dir=(
                store.snapshot_companion_dir(project_id, snapshot_id)
                / "state-review-trace"
                / rid_path
                / "semantic-enrichment"
            ),
        )

    current_semantics = _load_semantic_nodes(conn, project_id, snapshot_id)
    base_semantics = _load_semantic_nodes(conn, project_id, base_snapshot) if base_snapshot else {}
    semantic_jobs = _load_semantic_jobs(conn, project_id, snapshot_id)
    changed_semantics = {
        node_id: current_semantics.get(node_id, {})
        for node_id in node_ids
    }
    complete_node_ids = sorted(
        node_id
        for node_id, entry in changed_semantics.items()
        if str(entry.get("status") or "") == "ai_complete"
    )
    pending_node_ids = sorted(
        node_id
        for node_id in node_ids
        if node_id not in set(complete_node_ids)
    )
    context = _trace_incremental_context(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
        run_id=rid,
        changed_node_ids=node_ids,
        project_root=project_root,
        budget=query_budget,
    )

    semantic_summary_gate = semantic.feedback_review_gate(
        (semantic_result.get("summary") or {}) if semantic_result else {
            "ai_complete_count": len(complete_node_ids),
            "semantic_graph_state": {"hit_count": 0},
            "semantic_run_status": "ai_complete" if complete_node_ids else "ai_pending",
            "ai_selected_count": len(node_ids),
        }
    )
    if node_ids and pending_node_ids:
        review_gate = {
            "allowed": False,
            "reason": "changed_semantic_pending",
            "changed_node_count": len(node_ids),
            "changed_complete_count": len(complete_node_ids),
            "pending_node_count": len(pending_node_ids),
            "semantic_summary_gate": semantic_summary_gate,
        }
    elif node_ids:
        review_gate = {
            "allowed": True,
            "reason": "changed_semantic_complete",
            "changed_node_count": len(node_ids),
            "changed_complete_count": len(complete_node_ids),
            "pending_node_count": 0,
            "semantic_summary_gate": semantic_summary_gate,
        }
    else:
        review_gate = semantic_summary_gate
    blocked = bool(node_ids and pending_node_ids)
    ai_response: dict[str, Any] = {}
    ai_issues: list[dict[str, Any]] = []
    if global_review_use_ai and not blocked:
        payload = {
            "schema_version": 1,
            "mode": "incremental_global_semantic_review",
            "permissions": {
                "can": ["query_graph", "read_provided_context", "return_review_suggestions"],
                "cannot": ["modify_code", "modify_docs", "modify_tests", "mutate_graph_topology", "file_backlog"],
            },
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "base_snapshot_id": base_snapshot,
            "changed_paths": paths,
            "changed_node_ids": node_ids,
            "base_global_picture": _semantic_picture(base_semantics),
            "current_global_picture": _semantic_picture(current_semantics),
            "changed_semantics": changed_semantics,
            "graph_query_trace": context,
            "instructions": [
                "Review only the incremental change against the prior global picture.",
                "Return open_issues for graph corrections or project improvements only when evidence supports action.",
                "Represent coverage/doc/test drift as status observations unless the user explicitly requests backlog filing.",
            ],
        }
        ai_response = _call_ai(global_review_ai_call, payload)
        raw_issues = ai_response.get("open_issues") if isinstance(ai_response, dict) else []
        ai_issues = [item for item in (raw_issues or []) if isinstance(item, dict)]

    classification: dict[str, Any] = {}
    if classify_feedback and not blocked:
        if ai_issues:
            classification = reconcile_feedback.classify_semantic_open_issues(
                project_id,
                snapshot_id,
                source_round=f"global-incremental:{rid}",
                created_by=actor,
                issues=ai_issues,
                base_snapshot_id=base_snapshot,
            )
        else:
            classification = reconcile_feedback.classify_semantic_open_issues(
                project_id,
                snapshot_id,
                source_round=(semantic_result.get("feedback_round", "") if semantic_result else ""),
                created_by=actor,
                node_ids=node_ids,
                base_snapshot_id=base_snapshot,
            )

    generated_at = utc_now()
    report = {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "base_snapshot_id": base_snapshot,
        "run_id": rid,
        "generated_at": generated_at,
        "changed_paths": paths,
        "changed_node_ids": node_ids,
        "complete_node_ids": complete_node_ids,
        "pending_node_ids": pending_node_ids,
        "semantic_job_statuses": {
            node_id: (semantic_jobs.get(node_id) or {}).get("status", "")
            for node_id in node_ids
        },
        "status": "blocked_semantic_pending" if blocked else "reviewed",
        "blocked": blocked,
        "review_gate": review_gate,
        "semantic_enrichment": {
            "ok": semantic_result.get("ok", False) if semantic_result else False,
            "feedback_round": semantic_result.get("feedback_round", ""),
            "summary": semantic_result.get("summary", {}) if semantic_result else {},
            "semantic_index_path": semantic_result.get("semantic_index_path", ""),
            "review_report_path": semantic_result.get("review_report_path", ""),
        },
        "base_global_picture": _semantic_picture(base_semantics),
        "current_global_picture": _semantic_picture(current_semantics),
        "graph_query_trace": context,
        "global_ai_review": {
            "requested": bool(global_review_use_ai),
            "response_present": bool(ai_response and not ai_response.get("_ai_error")),
            "error": ai_response.get("_ai_error", "") if isinstance(ai_response, dict) else "",
            "open_issue_count": len(ai_issues),
        },
        "feedback_classification": classification,
    }
    out_dir = _global_review_dir(project_id, snapshot_id)
    report_path = str(out_dir / f"{rid_path}.json")
    latest_path = str(out_dir / "latest-incremental-review.json")
    report["report_path"] = report_path
    report["latest_report_path"] = latest_path
    _write_json(Path(report_path), report)
    _write_json(Path(latest_path), report)
    _update_snapshot_global_review_notes(
        conn,
        project_id,
        snapshot_id,
        {
            "latest_incremental_review_path": latest_path,
            "latest_incremental_run_id": rid,
            "latest_incremental_status": report["status"],
            "base_snapshot_id": base_snapshot,
            "changed_node_count": len(node_ids),
            "pending_node_count": len(pending_node_ids),
            "updated_at": generated_at,
        },
    )
    return {"ok": True, **report}


__all__ = ["run_full_global_review", "run_incremental_global_review"]
