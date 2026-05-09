"""State-only reconcile runners.

These helpers materialize reconcile outputs as governance state. They are not
chain stages and they must not edit project source, documentation, or tests.
Observer signoff or a later merge/finalize path decides when a candidate graph
snapshot becomes active.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from agent.governance.graph_snapshot_store import (
    PENDING_STATUS_FAILED,
    PENDING_STATUS_QUEUED,
    PENDING_STATUS_RUNNING,
    SNAPSHOT_STATUS_CANDIDATE,
    activate_graph_snapshot,
    create_graph_snapshot,
    ensure_schema as ensure_graph_snapshot_schema,
    finalize_graph_snapshot,
    get_active_graph_snapshot,
    graph_payload_edges,
    graph_payload_stats,
    index_graph_snapshot,
    list_graph_snapshot_files,
    list_pending_scope_reconcile,
    snapshot_graph_path,
    snapshot_id_for,
    waive_pending_scope_reconcile,
)
from agent.governance.db import sqlite_write_lock
from agent.governance.governance_index import build_governance_index, persist_governance_index
from agent.governance.reconcile_semantic_enrichment import run_semantic_enrichment
from agent.governance.reconcile_trace import ReconcileTrace, artifact_ref
from agent.governance.reconcile_file_inventory import (
    filter_governed_inventory_rows,
    filter_governed_paths,
)
from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
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
    old_by_path = _rows_by_path(old_rows)
    new_by_path = _rows_by_path(new_rows)
    old_paths = set(old_by_path)
    new_paths = set(new_by_path)
    added = sorted(new_paths - old_paths)
    removed = sorted(old_paths - new_paths)
    hash_changed = sorted(
        path for path in (old_paths & new_paths)
        if _row_file_hash(old_by_path[path]) != _row_file_hash(new_by_path[path])
    )
    status_changed = sorted(
        path for path in (old_paths & new_paths)
        if str(old_by_path[path].get("graph_status") or "")
        != str(new_by_path[path].get("graph_status") or "")
        or str(old_by_path[path].get("scan_status") or "")
        != str(new_by_path[path].get("scan_status") or "")
    )
    changed = sorted({path.replace("\\", "/").strip("/") for path in changed_files if path})
    impacted = sorted(set(changed) | set(added) | set(removed) | set(hash_changed) | set(status_changed))
    return {
        "strategy": "full_scan_with_incremental_file_delta",
        "changed_files": changed,
        "added_files": added,
        "removed_files": removed,
        "hash_changed_files": hash_changed,
        "status_changed_files": status_changed,
        "impacted_files": impacted,
        "changed_file_count": len(changed),
        "impacted_file_count": len(impacted),
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
) -> int:
    commits = [c for c in covered_commit_shas if c]
    if not commits:
        return 0
    placeholders = ",".join("?" for _ in commits)
    evidence = {
        "source": "pending_scope_materializer",
        "snapshot_id": snapshot_id,
        "target_commit_sha": target_commit_sha,
        "run_id": run_id,
        "covered_commit_shas": commits,
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
            PENDING_STATUS_RUNNING,
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


def run_pending_scope_reconcile_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    target_commit_sha: str = "",
    run_id: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
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
) -> dict[str, Any]:
    """Materialize pending scope rows as a reviewable candidate snapshot.

    The current MVP rebuilds a state-only candidate graph from the current
    worktree, then binds pending commits up to the target commit to that
    candidate. It intentionally does not activate the snapshot.
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

    active = get_active_graph_snapshot(conn, project_id) or {}
    active_inventory = _snapshot_inventory_rows(conn, project_id, active.get("snapshot_id", ""))
    changed_files = _git_changed_files(
        root,
        str(active.get("commit_sha") or ""),
        target,
    )
    rid = run_id or f"scope-reconcile-{_short_commit(target)}-pending"
    sid = snapshot_id or snapshot_id_for("scope", target)
    result = run_state_only_full_reconcile(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=target,
        snapshot_id=sid,
        snapshot_kind="scope",
        created_by=created_by,
        activate=False,
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
        notes_extra={
            "pending_scope_reconcile": {
                "covered_commit_shas": covered,
                "covered_commit_count": len(covered),
                "active_snapshot_id": active.get("snapshot_id", ""),
                "active_graph_commit": active.get("commit_sha", ""),
            }
        },
    )
    if not result.get("ok"):
        return {
            **result,
            "pending_count": len(pending),
            "covered_commit_shas": covered,
        }
    scope_file_delta = _build_scope_file_delta(
        project_root=root,
        old_rows=active_inventory,
        new_rows=_snapshot_inventory_rows(conn, project_id, sid),
        changed_files=changed_files,
    )
    pending_notes = {
        "covered_commit_shas": covered,
        "covered_commit_count": len(covered),
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
        "scope_file_delta": scope_file_delta,
    }
    row = conn.execute(
        "SELECT notes FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, sid),
    ).fetchone()
    with sqlite_write_lock():
        updated = _update_pending_scope_candidate(
            conn,
            project_id,
            covered_commit_shas=covered,
            snapshot_id=sid,
            target_commit_sha=target,
            run_id=rid,
        )
        if row:
            try:
                notes = json.loads(row["notes"] if hasattr(row, "keys") else row[0])
            except Exception:
                notes = {}
            notes["pending_scope_reconcile"] = pending_notes
            conn.execute(
                "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
                (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
            )
        conn.commit()
    return {
        **result,
        "pending_count": len(pending),
        "covered_commit_shas": covered,
        "covered_pending_count": len(covered),
        "pending_rows_bound": updated,
        "scope_file_delta": scope_file_delta,
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
    }


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
    "run_backfill_escape_hatch",
    "run_pending_scope_reconcile_candidate",
    "run_state_only_full_reconcile",
]
