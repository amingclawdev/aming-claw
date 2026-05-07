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
    get_active_graph_snapshot,
    graph_payload_stats,
    index_graph_snapshot,
    list_pending_scope_reconcile,
    snapshot_graph_path,
    snapshot_id_for,
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


def _deps_graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    nodes = deps.get("nodes") if isinstance(deps, dict) else []
    return [node for node in nodes or [] if isinstance(node, dict)]


def _deps_graph_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    edges = []
    if isinstance(deps, dict):
        edges = deps.get("edges") if "edges" in deps else deps.get("links")
    return [edge for edge in edges or [] if isinstance(edge, dict)]


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

    phase_result = build_graph_v2_from_symbols(
        str(root),
        dry_run=True,
        scratch_dir=str(scratch_dir),
        run_id=rid,
    )
    if phase_result.get("status") != "ok":
        return {
            "ok": False,
            "project_id": project_id,
            "run_id": rid,
            "commit_sha": commit,
            "status": phase_result.get("status", "unknown"),
            "abort_reason": phase_result.get("abort_reason", ""),
            "phase_result": phase_result,
        }

    candidate_graph = build_rebase_candidate_graph(
        str(root),
        phase_result,
        session_id=rid,
        run_id=rid,
    )
    file_inventory = _normalize_inventory_commit(
        [
            row for row in (phase_result.get("file_inventory") or [])
            if isinstance(row, dict)
        ],
        commit_sha=commit,
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
    activation = None
    if activate:
        activation = activate_graph_snapshot(
            conn,
            project_id,
            sid,
            expected_old_snapshot_id=expected_old_snapshot_id,
        )
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
    updated = _update_pending_scope_candidate(
        conn,
        project_id,
        covered_commit_shas=covered,
        snapshot_id=sid,
        target_commit_sha=target,
        run_id=rid,
    )
    return {
        **result,
        "pending_count": len(pending),
        "covered_commit_shas": covered,
        "covered_pending_count": len(covered),
        "pending_rows_bound": updated,
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
    }


__all__ = [
    "run_pending_scope_reconcile_candidate",
    "run_state_only_full_reconcile",
]
