"""Baseline storage service for Phase I reconciliation.

Provides create_baseline(), list_baselines(), get_baseline(), get_by_commit(),
diff(), backfill_reconstructed(), require_baseline(), and companion-file I/O
with sha256 verification.

Design: append-only (no UPDATE/DELETE). Companion files on shared-volume.
"""

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .errors import BaselineMissingError, BaselineCorruptedError

log = logging.getLogger(__name__)

TRIGGER_ALLOWLIST = frozenset({"auto-chain", "reconcile-task", "manual-fix", "init"})

_COMPANION_DIR_ENV = "SHARED_VOLUME_PATH"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _baselines_root(project_id: str) -> Path:
    """Return companion-file directory for a project's baselines."""
    from .db import _governance_root
    root = _governance_root()
    return root / project_id / "baselines"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Companion files (R6)
# ---------------------------------------------------------------------------

def _write_companion_files(project_id: str, baseline_id: int,
                           graph_json: dict, code_doc_map_json: dict) -> dict:
    """Write graph.json, code_doc_map.json, and manifest.json to disk.

    Returns dict with sha256 hashes.
    """
    base_dir = _baselines_root(project_id) / str(baseline_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    graph_bytes = json.dumps(graph_json, sort_keys=True, ensure_ascii=False).encode("utf-8")
    cdm_bytes = json.dumps(code_doc_map_json, sort_keys=True, ensure_ascii=False).encode("utf-8")

    graph_sha = _sha256(graph_bytes)
    cdm_sha = _sha256(cdm_bytes)

    (base_dir / "graph.json").write_bytes(graph_bytes)
    (base_dir / "code_doc_map.json").write_bytes(cdm_bytes)

    manifest = {
        "baseline_id": baseline_id,
        "project_id": project_id,
        "graph_sha256": graph_sha,
        "code_doc_map_sha256": cdm_sha,
        "created_at": _utc_now(),
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    (base_dir / "manifest.json").write_bytes(manifest_bytes)

    return {"graph_sha": graph_sha, "code_doc_map_sha": cdm_sha}


def read_companion_file(project_id: str, baseline_id: int, filename: str) -> dict:
    """Read a companion file and verify its sha256 against the manifest.

    Raises BaselineCorruptedError on mismatch.
    """
    base_dir = _baselines_root(project_id) / str(baseline_id)
    manifest_path = base_dir / "manifest.json"
    file_path = base_dir / filename

    if not manifest_path.exists():
        raise BaselineMissingError(project_id, baseline_id)
    if not file_path.exists():
        raise BaselineMissingError(project_id, baseline_id)

    manifest = json.loads(manifest_path.read_bytes())
    file_bytes = file_path.read_bytes()
    actual_sha = _sha256(file_bytes)

    # Determine expected sha from manifest
    if filename == "graph.json":
        expected_sha = manifest.get("graph_sha256", "")
    elif filename == "code_doc_map.json":
        expected_sha = manifest.get("code_doc_map_sha256", "")
    else:
        # No verification for unknown files
        return json.loads(file_bytes)

    if actual_sha != expected_sha:
        raise BaselineCorruptedError(
            project_id, baseline_id,
            f"sha256 mismatch for {filename}: expected {expected_sha}, got {actual_sha}"
        )

    return json.loads(file_bytes)


# ---------------------------------------------------------------------------
# Core CRUD (R1)
# ---------------------------------------------------------------------------

def create_baseline(conn: sqlite3.Connection, project_id: str,
                    chain_version: str, trigger: str, triggered_by: str,
                    graph_json: dict = None, code_doc_map_json: dict = None,
                    node_state_snap: str = "", chain_event_max: int = 0,
                    notes: str = "", reconstructed: int = 0,
                    scope_kind: str = None, scope_value: str = None,
                    parent_baseline_id: int = None) -> dict:
    """Create a new baseline row + companion files.

    R7: trigger allowlist enforcement.
    R8: append-only — only INSERT, never UPDATE/DELETE.
    R3: Optional scope_kind, scope_value, parent_baseline_id for slice baselines.
    """
    if triggered_by not in TRIGGER_ALLOWLIST:
        raise ValueError(
            f"triggered_by must be one of {sorted(TRIGGER_ALLOWLIST)}, got {triggered_by!r}"
        )

    now = _utc_now()

    # Compute shas from companion data
    graph_json = graph_json or {}
    code_doc_map_json = code_doc_map_json or {}

    # Determine next baseline_id for this project
    row = conn.execute(
        "SELECT COALESCE(MAX(baseline_id), 0) AS max_id FROM version_baselines WHERE project_id = ?",
        (project_id,)
    ).fetchone()
    next_id = (row["max_id"] if row else 0) + 1

    # Write companion files first
    shas = _write_companion_files(project_id, next_id, graph_json, code_doc_map_json)
    graph_sha = shas["graph_sha"]
    code_doc_map_sha = shas["code_doc_map_sha"]

    # Build scope_id from kind+value if provided
    scope_id = f"{scope_kind}:{scope_value}" if scope_kind and scope_value else None

    conn.execute(
        """INSERT INTO version_baselines
           (project_id, baseline_id, chain_version, graph_sha, code_doc_map_sha,
            node_state_snap, chain_event_max, trigger, triggered_by,
            reconstructed, created_at, notes,
            scope_id, parent_baseline_id, scope_kind, scope_value,
            merged_into, merge_status, merge_evidence_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, next_id, chain_version, graph_sha, code_doc_map_sha,
         node_state_snap, chain_event_max, trigger, triggered_by,
         reconstructed, now, notes,
         scope_id, parent_baseline_id, scope_kind, scope_value,
         None, None, None),
    )
    conn.commit()

    return {
        "baseline_id": next_id,
        "project_id": project_id,
        "chain_version": chain_version,
        "graph_sha": graph_sha,
        "code_doc_map_sha": code_doc_map_sha,
        "trigger": trigger,
        "triggered_by": triggered_by,
        "reconstructed": reconstructed,
        "created_at": now,
        "scope_kind": scope_kind,
        "scope_value": scope_value,
        "parent_baseline_id": parent_baseline_id,
    }


def list_baselines(conn: sqlite3.Connection, project_id: str) -> list:
    """Return all baselines for a project, ordered by baseline_id DESC."""
    rows = conn.execute(
        """SELECT project_id, baseline_id, chain_version, graph_sha, code_doc_map_sha,
                  node_state_snap, chain_event_max, trigger, triggered_by,
                  reconstructed, created_at, notes
           FROM version_baselines WHERE project_id = ?
           ORDER BY baseline_id DESC""",
        (project_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_baseline(conn: sqlite3.Connection, project_id: str, baseline_id: int) -> dict:
    """Get a single baseline by ID. Returns dict or raises BaselineMissingError."""
    row = conn.execute(
        """SELECT project_id, baseline_id, chain_version, graph_sha, code_doc_map_sha,
                  node_state_snap, chain_event_max, trigger, triggered_by,
                  reconstructed, created_at, notes
           FROM version_baselines WHERE project_id = ? AND baseline_id = ?""",
        (project_id, baseline_id)
    ).fetchone()
    if not row:
        raise BaselineMissingError(project_id, baseline_id)
    return dict(row)


def get_by_commit(conn: sqlite3.Connection, project_id: str, chain_version: str) -> dict:
    """Get baseline by chain_version (commit SHA). Uses idx_baselines_chain_version."""
    row = conn.execute(
        """SELECT project_id, baseline_id, chain_version, graph_sha, code_doc_map_sha,
                  node_state_snap, chain_event_max, trigger, triggered_by,
                  reconstructed, created_at, notes
           FROM version_baselines
           WHERE project_id = ? AND chain_version = ?
           ORDER BY baseline_id DESC LIMIT 1""",
        (project_id, chain_version)
    ).fetchone()
    if not row:
        raise BaselineMissingError(project_id, 0)
    return dict(row)


def diff(conn: sqlite3.Connection, project_id: str,
         from_id: int, to_id: int, scope: str = "full") -> dict:
    """Compare two baselines and return structured delta (AC-I5).

    Returns dict with: nodes_added, nodes_removed, node_state_changes, chain_events_count.
    """
    from_bl = get_baseline(conn, project_id, from_id)
    to_bl = get_baseline(conn, project_id, to_id)

    # Read companion graphs for comparison
    try:
        from_graph = read_companion_file(project_id, from_id, "graph.json")
    except (BaselineMissingError, BaselineCorruptedError):
        from_graph = {}
    try:
        to_graph = read_companion_file(project_id, to_id, "graph.json")
    except (BaselineMissingError, BaselineCorruptedError):
        to_graph = {}

    from_nodes = set(from_graph.get("nodes", {}).keys()) if isinstance(from_graph.get("nodes"), dict) else set()
    to_nodes = set(to_graph.get("nodes", {}).keys()) if isinstance(to_graph.get("nodes"), dict) else set()

    nodes_added = sorted(to_nodes - from_nodes)
    nodes_removed = sorted(from_nodes - to_nodes)

    # Node state changes: compare node_state_snap JSON
    from_snap = {}
    to_snap = {}
    try:
        from_snap = json.loads(from_bl.get("node_state_snap") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        to_snap = json.loads(to_bl.get("node_state_snap") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    node_state_changes = []
    all_nodes = set(from_snap.keys()) | set(to_snap.keys())
    for nid in sorted(all_nodes):
        f_state = from_snap.get(nid)
        t_state = to_snap.get(nid)
        if f_state != t_state:
            node_state_changes.append({
                "node_id": nid,
                "from": f_state,
                "to": t_state,
            })

    # Chain events count between the two baselines
    from_max = from_bl.get("chain_event_max", 0) or 0
    to_max = to_bl.get("chain_event_max", 0) or 0
    chain_events_count = max(0, to_max - from_max)

    return {
        "from_baseline": from_id,
        "to_baseline": to_id,
        "scope": scope,
        "nodes_added": nodes_added,
        "nodes_removed": nodes_removed,
        "node_state_changes": node_state_changes,
        "chain_events_count": chain_events_count,
    }


def backfill_reconstructed(conn: sqlite3.Connection, project_id: str) -> list:
    """Backfill baselines from chain history for projects missing baselines (AC-I9).

    Creates rows with reconstructed=1.
    """
    # Check if baselines already exist
    existing = conn.execute(
        "SELECT COUNT(*) AS cnt FROM version_baselines WHERE project_id = ?",
        (project_id,)
    ).fetchone()
    if existing and existing["cnt"] > 0:
        return []

    # Get chain history from project_version
    pv = conn.execute(
        "SELECT chain_version, updated_at FROM project_version WHERE project_id = ?",
        (project_id,)
    ).fetchone()
    if not pv:
        return []

    chain_version = pv["chain_version"]

    # Get chain_event_max
    ce_row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM chain_events"
    ).fetchone()
    chain_event_max = ce_row["max_id"] if ce_row else 0

    # Create reconstructed baseline
    result = create_baseline(
        conn, project_id,
        chain_version=chain_version,
        trigger="init",
        triggered_by="init",
        graph_json={},
        code_doc_map_json={},
        node_state_snap="{}",
        chain_event_max=chain_event_max,
        notes="Backfilled from chain history (reconstructed)",
        reconstructed=1,
    )

    return [result]


# ---------------------------------------------------------------------------
# Phase H/Z guard (R9)
# ---------------------------------------------------------------------------

def require_baseline(conn: sqlite3.Connection, project_id: str,
                     baseline_id: int = None) -> dict:
    """Guard: raise BaselineMissingError if baseline not found.

    Also files OPT-BACKLOG-BASELINE-MISSING alert.
    R9: never silently skip.
    """
    if baseline_id is not None:
        row = conn.execute(
            "SELECT baseline_id FROM version_baselines WHERE project_id = ? AND baseline_id = ?",
            (project_id, baseline_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT baseline_id FROM version_baselines WHERE project_id = ? ORDER BY baseline_id DESC LIMIT 1",
            (project_id,)
        ).fetchone()

    if not row:
        _file_baseline_missing_alert(conn, project_id, baseline_id)
        raise BaselineMissingError(project_id, baseline_id)

    return dict(row)


def _file_baseline_missing_alert(conn: sqlite3.Connection, project_id: str,
                                 baseline_id: int = None):
    """File OPT-BACKLOG-BASELINE-MISSING alert as backlog bug (best-effort)."""
    bid = baseline_id or 0
    bug_id = f"OPT-BACKLOG-BASELINE-MISSING-B{bid}"
    now = _utc_now()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO backlog_bugs
               (bug_id, title, status, priority, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P1', ?, ?)""",
            (bug_id,
             f"Baseline missing: project={project_id} baseline_id={bid}",
             now, now),
        )
        conn.commit()
    except Exception as exc:
        log.warning("baseline_service: failed to file backlog alert %s: %s", bug_id, exc)


# ---------------------------------------------------------------------------
# R4: Batch-insert mutation rows
# ---------------------------------------------------------------------------

def record_baseline_mutations(conn: sqlite3.Connection, project_id: str,
                              baseline_id: int, mutations: list) -> int:
    """Batch-insert mutation rows for a slice baseline.

    Each mutation dict should contain: mutation_id, mutation_type,
    affected_file, affected_node, before_sha256, after_sha256.
    Returns the number of rows inserted.
    """
    inserted = 0
    for m in mutations:
        conn.execute(
            """INSERT INTO baseline_mutations
               (project_id, baseline_id, mutation_id, mutation_type,
                affected_file, affected_node, before_sha256, after_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, baseline_id,
             m["mutation_id"], m.get("mutation_type", ""),
             m.get("affected_file", ""), m.get("affected_node", ""),
             m.get("before_sha256", ""), m.get("after_sha256", "")),
        )
        inserted += 1
    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# R6: Compute post-state for a baseline
# ---------------------------------------------------------------------------

def compute_post_state(conn: sqlite3.Connection, project_id: str,
                       baseline_id: int) -> dict:
    """Return {key -> sha256} from companion files + node_state_snap at baseline.

    Keys are file paths from companion graph nodes and node IDs from state snap.
    """
    post_state = {}

    # Include node_state_snap hashes
    bl = get_baseline(conn, project_id, baseline_id)
    snap_raw = bl.get("node_state_snap", "{}")
    try:
        snap = json.loads(snap_raw) if snap_raw else {}
    except (json.JSONDecodeError, TypeError):
        snap = {}
    for node_id, state_val in snap.items():
        # Hash the state value to get a sha256
        if isinstance(state_val, str):
            post_state[node_id] = _sha256(state_val.encode("utf-8"))
        else:
            post_state[node_id] = _sha256(json.dumps(state_val, sort_keys=True).encode("utf-8"))

    # Include companion file hashes
    post_state["graph.json"] = bl.get("graph_sha", "")
    post_state["code_doc_map.json"] = bl.get("code_doc_map_sha", "")

    # Include mutation after_sha256 values (from baseline_mutations table)
    try:
        rows = conn.execute(
            """SELECT affected_file, after_sha256 FROM baseline_mutations
               WHERE project_id = ? AND baseline_id = ?""",
            (project_id, baseline_id),
        ).fetchall()
        for row in rows:
            key = row["affected_file"] or row[0]
            val = row["after_sha256"] or row[1]
            if key and val:
                post_state[key] = val
    except Exception:
        pass  # baseline_mutations table may not exist yet

    return post_state


# ---------------------------------------------------------------------------
# R5: Merge slice baselines into a full baseline
# ---------------------------------------------------------------------------

def attempt_merge_slice_baselines_into(conn: sqlite3.Connection,
                                       project_id: str,
                                       full_baseline_id: int) -> dict:
    """Merge unmerged slice baselines into a full baseline using content fingerprints.

    For each unmerged slice baseline:
    - If every mutation's after_sha256 matches the full baseline's post_state,
      set merge_status='merged' and merged_into=full_baseline_id.
    - If any mutation diverges, set merge_status='conflict' and file a backlog row.
    - If no mutations exist, set merge_status='unknown'.

    Returns dict with counts: merged, conflict, unknown.
    """
    full_post_state = compute_post_state(conn, project_id, full_baseline_id)

    # Find all unmerged slice baselines (scope_kind IS NOT NULL, merge_status IS NULL)
    rows = conn.execute(
        """SELECT baseline_id, scope_kind, scope_value
           FROM version_baselines
           WHERE project_id = ? AND scope_kind IS NOT NULL
             AND (merge_status IS NULL OR merge_status = '')""",
        (project_id,),
    ).fetchall()

    result = {"merged": 0, "conflict": 0, "unknown": 0}
    now = _utc_now()

    for row in rows:
        slice_bid = row["baseline_id"]

        # Get mutations for this slice baseline
        mutations = conn.execute(
            """SELECT mutation_id, affected_file, after_sha256
               FROM baseline_mutations
               WHERE project_id = ? AND baseline_id = ?""",
            (project_id, slice_bid),
        ).fetchall()

        if not mutations:
            # No mutations → unknown
            conn.execute(
                """UPDATE version_baselines
                   SET merge_status = 'unknown', merged_into = ?, merge_evidence_json = ?
                   WHERE project_id = ? AND baseline_id = ?""",
                (full_baseline_id,
                 json.dumps({"reason": "no_mutations", "checked_at": now}),
                 project_id, slice_bid),
            )
            result["unknown"] += 1
            continue

        # Check each mutation's after_sha256 against full_post_state
        diverged = []
        for mut in mutations:
            key = mut["affected_file"]
            after_sha = mut["after_sha256"]
            full_sha = full_post_state.get(key)
            if after_sha and full_sha and after_sha != full_sha:
                diverged.append({
                    "mutation_id": mut["mutation_id"],
                    "affected_file": key,
                    "slice_sha256": after_sha,
                    "full_sha256": full_sha,
                })

        if not diverged:
            # All mutations match → merged
            conn.execute(
                """UPDATE version_baselines
                   SET merge_status = 'merged', merged_into = ?, merge_evidence_json = ?
                   WHERE project_id = ? AND baseline_id = ?""",
                (full_baseline_id,
                 json.dumps({"merged_at": now, "mutation_count": len(mutations)}),
                 project_id, slice_bid),
            )
            result["merged"] += 1
        else:
            # Divergence → conflict
            evidence = {"diverged_mutations": diverged, "checked_at": now}
            conn.execute(
                """UPDATE version_baselines
                   SET merge_status = 'conflict', merged_into = ?, merge_evidence_json = ?
                   WHERE project_id = ? AND baseline_id = ?""",
                (full_baseline_id,
                 json.dumps(evidence),
                 project_id, slice_bid),
            )
            result["conflict"] += 1

            # File backlog row for conflict
            bug_id = f"OPT-BACKLOG-SLICE-MERGE-CONFLICT-B{slice_bid}"
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO backlog_bugs
                       (bug_id, title, status, priority, created_at, updated_at)
                       VALUES (?, ?, 'OPEN', 'P1', ?, ?)""",
                    (bug_id,
                     f"Slice baseline B{slice_bid} merge conflict with full B{full_baseline_id}",
                     now, now),
                )
            except Exception as exc:
                log.warning("baseline_service: failed to file conflict backlog %s: %s",
                            bug_id, exc)

    conn.commit()
    return result


# ---------------------------------------------------------------------------
# R7: Get last relevant baseline (§8.4)
# ---------------------------------------------------------------------------

def get_last_relevant_baseline(conn: sqlite3.Connection, project_id: str,
                               scope_kind: str = None,
                               scope_value: str = None) -> dict:
    """Return the newest relevant baseline for the given scope.

    Per §8.4: returns the newest of:
    - The last full baseline (scope_kind IS NULL)
    - The last matching unmerged/merged slice baseline (if scope provided)

    If no scope is provided, returns the last full baseline.
    Raises BaselineMissingError if nothing found.
    """
    # Get last full baseline
    full_row = conn.execute(
        """SELECT * FROM version_baselines
           WHERE project_id = ? AND scope_kind IS NULL
           ORDER BY baseline_id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()

    if not scope_kind or not scope_value:
        if not full_row:
            raise BaselineMissingError(project_id, None)
        return dict(full_row)

    # Get last matching slice baseline (unmerged or merged)
    slice_row = conn.execute(
        """SELECT * FROM version_baselines
           WHERE project_id = ? AND scope_kind = ? AND scope_value = ?
             AND (merge_status IS NULL OR merge_status IN ('', 'merged', 'unknown'))
           ORDER BY baseline_id DESC LIMIT 1""",
        (project_id, scope_kind, scope_value),
    ).fetchone()

    if not full_row and not slice_row:
        raise BaselineMissingError(project_id, None)

    if not full_row:
        return dict(slice_row)
    if not slice_row:
        return dict(full_row)

    # Return whichever is newer (higher baseline_id)
    if slice_row["baseline_id"] > full_row["baseline_id"]:
        return dict(slice_row)
    return dict(full_row)


# ---------------------------------------------------------------------------
# R11: GC safety — slice baselines with unresolved status must not be deleted
# ---------------------------------------------------------------------------

def is_slice_baseline_gc_safe(conn: sqlite3.Connection, project_id: str,
                              baseline_id: int) -> bool:
    """Return True if the baseline is safe to GC-delete.

    Slice baselines with merge_status IN (NULL, 'unknown', 'conflict')
    must NEVER be deleted by GC.
    """
    row = conn.execute(
        """SELECT scope_kind, merge_status FROM version_baselines
           WHERE project_id = ? AND baseline_id = ?""",
        (project_id, baseline_id),
    ).fetchone()
    if not row:
        return True  # Non-existent rows are safe to "delete"

    # Full baselines (no scope_kind) follow normal GC rules
    if row["scope_kind"] is None:
        return True

    # Slice baselines: only 'merged' status is safe to GC
    merge_status = row["merge_status"]
    if merge_status == "merged":
        return True

    # NULL, 'unknown', 'conflict' → NOT safe to delete
    return False
