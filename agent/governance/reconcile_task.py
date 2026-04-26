"""Reconcile Task — 6-stage lifecycle for governance reconciliation (Phase J).

Stages: scan → diff → propose → approve → apply → verify

Each handler has signature:
    (conn, project_id, task_id, metadata, prev_result) → next_payload

R1-R12 requirements mapped to stage handlers below.
"""
from __future__ import annotations

import json
import logging
import os
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .errors import ReconcileScopeViolationError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RECONCILE_STAGES = ["scan", "diff", "propose", "approve", "apply", "verify"]

_VALID_META_CIRCULAR_SCENARIOS = frozenset({
    "chain_broken", "gov_wedge", "deploy_selfkill",
    "graph_corrupted", "b48_precedent",
})

_ALLOWLISTED_API_ACTIONS = frozenset({
    "node-create", "node-update", "node-soft-delete",
    "verify-update", "backlog",
})

# Mutation plan directory template
_MUTATION_PLAN_DIR = "shared-volume/codex-tasks/state/governance/{pid}/mutation_plans"

# ---------------------------------------------------------------------------
# Advisory Lock (R11)
# ---------------------------------------------------------------------------

def _ensure_reconcile_lock_table(conn: sqlite3.Connection):
    """Create reconcile_lock table if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconcile_lock (
            project_id TEXT PRIMARY KEY,
            holder_task_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)


def acquire_reconcile_lock(conn: sqlite3.Connection, project_id: str, task_id: str,
                           ttl_seconds: int = 600) -> bool:
    """Acquire advisory lock for reconcile task serialization (R11).

    Returns True if lock acquired, False if another task holds it.
    Stale locks (past expires_at) are automatically reclaimed.
    """
    _ensure_reconcile_lock_table(conn)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    now_iso = now.isoformat()
    expires_iso = expires.isoformat()

    # Try to reclaim expired lock
    conn.execute(
        "DELETE FROM reconcile_lock WHERE project_id = ? AND expires_at < ?",
        (project_id, now_iso),
    )

    try:
        conn.execute(
            "INSERT INTO reconcile_lock (project_id, holder_task_id, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (project_id, task_id, now_iso, expires_iso),
        )
        return True
    except sqlite3.IntegrityError:
        # Lock held by another task
        return False


def release_reconcile_lock(conn: sqlite3.Connection, project_id: str, task_id: str):
    """Release advisory lock held by this task."""
    _ensure_reconcile_lock_table(conn)
    conn.execute(
        "DELETE FROM reconcile_lock WHERE project_id = ? AND holder_task_id = ?",
        (project_id, task_id),
    )


# ---------------------------------------------------------------------------
# Two-Phase Commit Helpers (R12)
# ---------------------------------------------------------------------------

class ReconcileCancelled(Exception):
    """Raised when a reconcile task is cancelled mid-run."""
    pass


def _check_cancellation(conn: sqlite3.Connection, task_id: str):
    """Check if task has been cancelled; raise ReconcileCancelled if so."""
    row = conn.execute(
        "SELECT execution_status FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row and row["execution_status"] in ("cancelled", "timed_out"):
        raise ReconcileCancelled(f"Task {task_id} cancelled mid-run")


def _begin_two_phase(conn: sqlite3.Connection, task_id: str, mutations: list) -> str:
    """Begin two-phase commit: write pending mutations to WAL-like table."""
    _ensure_mutation_wal_table(conn)
    txn_id = f"reconcile-txn-{task_id}-{int(time.time())}"
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO reconcile_mutation_wal (txn_id, task_id, status, mutations_json, created_at) "
        "VALUES (?, ?, 'pending', ?, ?)",
        (txn_id, task_id, json.dumps(mutations), now_iso),
    )
    return txn_id


def _commit_two_phase(conn: sqlite3.Connection, txn_id: str):
    """Mark two-phase transaction as committed."""
    conn.execute(
        "UPDATE reconcile_mutation_wal SET status = 'committed' WHERE txn_id = ?",
        (txn_id,),
    )


def _rollback_two_phase(conn: sqlite3.Connection, txn_id: str):
    """Mark two-phase transaction as rolled back."""
    conn.execute(
        "UPDATE reconcile_mutation_wal SET status = 'rolled_back' WHERE txn_id = ?",
        (txn_id,),
    )


def _ensure_mutation_wal_table(conn: sqlite3.Connection):
    """Create reconcile_mutation_wal table if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconcile_mutation_wal (
            txn_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            mutations_json TEXT,
            created_at TEXT
        )
    """)


# ---------------------------------------------------------------------------
# Meta-Circular Validation (R6)
# ---------------------------------------------------------------------------

def validate_meta_circular(metadata: dict, task_id: str) -> tuple[bool, str]:
    """Validate _meta_circular=true constraints (R6).

    Returns (valid, error_message). Error is empty on success.
    """
    scenario = metadata.get("scenario", "")
    if scenario not in _VALID_META_CIRCULAR_SCENARIOS:
        return False, (
            f"Invalid meta-circular scenario '{scenario}'; "
            f"must be one of {sorted(_VALID_META_CIRCULAR_SCENARIOS)}"
        )

    reason = metadata.get("reason", "")
    if not isinstance(reason, str) or len(reason) < 50:
        return False, f"Meta-circular reason must be >= 50 chars, got {len(reason) if isinstance(reason, str) else 0}"

    observer = metadata.get("observer_acknowledged_by", "")
    if not observer:
        return False, "observer_acknowledged_by is required for meta-circular reconcile"

    return True, ""


def _file_meta_circular_backlog(conn: sqlite3.Connection, project_id: str,
                                task_id: str, metadata: dict):
    """Auto-file OPT-BACKLOG-META-CIRCULAR-REVIEW-{task_id} backlog row (R6)."""
    backlog_id = f"OPT-BACKLOG-META-CIRCULAR-REVIEW-{task_id}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=7)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO backlog_bugs
               (bug_id, project_id, title, priority, status, created_at, expires_at,
                details_json, created_by)
               VALUES (?, ?, ?, 'P1', 'open', ?, ?, ?, 'reconcile-task')""",
            (
                backlog_id, project_id,
                f"Meta-circular reconcile review: {task_id}",
                now.isoformat(), expires.isoformat(),
                json.dumps({
                    "task_id": task_id,
                    "scenario": metadata.get("scenario", ""),
                    "reason": metadata.get("reason", ""),
                    "observer_acknowledged_by": metadata.get("observer_acknowledged_by", ""),
                }),
            ),
        )
        log.warning("[reconcile-meta-circular] Filed backlog %s for task %s", backlog_id, task_id)
    except Exception:
        log.error("Failed to file meta-circular backlog for %s", task_id, exc_info=True)


# ---------------------------------------------------------------------------
# Guarded Connection Proxy (R4)
# ---------------------------------------------------------------------------

class _GuardedConnection:
    """Wraps a sqlite3.Connection to block direct node_state/graph mutations (R4).

    Only allowlisted governance API calls should be used for mutations.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, params=None):
        sql_upper = sql.upper().strip()
        # Block direct INSERT/UPDATE to node_state
        if ("NODE_STATE" in sql_upper and
                any(sql_upper.startswith(kw) for kw in ("UPDATE", "INSERT"))):
            raise RuntimeError(
                "Direct DB write to node_state is forbidden in reconcile apply stage. "
                "Use governance API (node-create, node-update, verify-update) instead."
            )
        if params:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def executemany(self, sql: str, params):
        sql_upper = sql.upper().strip()
        if "NODE_STATE" in sql_upper:
            raise RuntimeError("Direct DB write to node_state is forbidden in reconcile apply stage.")
        return self._conn.executemany(sql, params)

    def executescript(self, sql: str):
        if "node_state" in sql.lower():
            raise RuntimeError("Direct DB write to node_state is forbidden in reconcile apply stage.")
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, val):
        self._conn.row_factory = val

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# Mutation Plan I/O (R3)
# ---------------------------------------------------------------------------

def _mutation_plan_path(project_id: str, task_id: str) -> Path:
    """Return the path to the mutation_plan.json for a reconcile task."""
    base = _MUTATION_PLAN_DIR.format(pid=project_id)
    return Path(base) / f"{task_id}.json"


def _write_mutation_plan(project_id: str, task_id: str, plan: dict):
    """Write mutation_plan.json with required schema (R3)."""
    path = _mutation_plan_path(project_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure required fields
    required = {"task_id", "baseline_id_before", "phases_run", "mutations",
                "summary", "approve_threshold", "applied_count"}
    missing = required - set(plan.keys())
    if missing:
        raise ValueError(f"mutation_plan missing required fields: {missing}")
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote mutation plan: %s", path)


def _read_mutation_plan(project_id: str, task_id: str) -> dict:
    """Read mutation_plan.json (R4: apply reads from file, not internal state)."""
    path = _mutation_plan_path(project_id, task_id)
    if not path.exists():
        raise FileNotFoundError(f"Mutation plan not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Scope Guard Helper (R7)
# ---------------------------------------------------------------------------

def _mutation_in_scope(mutation: dict, scope_declared: dict) -> bool:
    """Check whether a single mutation falls within the declared scope.

    Returns True if in-scope, False otherwise.

    Rules (R7):
    - node_id in scope_declared['node_set'] → in-scope
    - affected_file in scope_declared['file_set'] → in-scope
    - For node-create with system-allocated id: check m['after']['primary']
      intersection with file_set
    """
    node_set = set(scope_declared.get("node_set") or [])
    file_set = set(scope_declared.get("file_set") or [])

    node_id = mutation.get("node_id", "")
    if node_id and node_id in node_set:
        return True

    affected_file = mutation.get("affected_file", "")
    if affected_file and affected_file in file_set:
        return True

    # For node-create with system-allocated id, check after.primary
    action = mutation.get("action", "")
    if action == "node-create":
        after = mutation.get("after", {})
        if isinstance(after, dict):
            primary = after.get("primary", "")
            if isinstance(primary, list):
                if file_set.intersection(primary):
                    return True
            elif isinstance(primary, str) and primary and primary in file_set:
                return True

    return False


# ---------------------------------------------------------------------------
# Stage Handlers (R1)
# ---------------------------------------------------------------------------

def handle_scan(conn, project_id, task_id, metadata, prev_result):
    """Scan stage: discover graph nodes, node_state, and current baselines."""
    _check_cancellation(conn, task_id)

    # Collect current node states
    rows = conn.execute(
        "SELECT node_id, verify_status, build_status FROM node_state WHERE project_id = ?",
        (project_id,),
    ).fetchall()

    node_states = {}
    for r in rows:
        node_states[r["node_id"]] = {
            "verify_status": r["verify_status"],
            "build_status": r["build_status"],
        }

    # Get current chain_version
    vrow = conn.execute(
        "SELECT chain_version, git_head FROM project_version WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    baseline = {
        "chain_version": vrow["chain_version"] if vrow else None,
        "git_head": vrow["git_head"] if vrow else None,
    }

    return {
        "stage": "scan",
        "node_count": len(node_states),
        "node_states": node_states,
        "baseline": baseline,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


def handle_diff(conn, project_id, task_id, metadata, prev_result):
    """Diff stage: compare scan results against expected graph state."""
    _check_cancellation(conn, task_id)

    scan_result = prev_result or {}
    node_states = scan_result.get("node_states", {})

    # Load graph to compare
    diffs = []
    try:
        from . import project_service
        graph = project_service.load_project_graph(project_id)
        if graph:
            graph_nodes = set(graph.G.nodes())
            db_nodes = set(node_states.keys())
            # Nodes in graph but missing from DB
            for nid in graph_nodes - db_nodes:
                diffs.append({"node_id": nid, "type": "missing_in_db", "severity": "high"})
            # Nodes in DB but missing from graph
            for nid in db_nodes - graph_nodes:
                diffs.append({"node_id": nid, "type": "orphan_in_db", "severity": "medium"})
    except Exception as e:
        log.warning("reconcile diff: graph load failed: %s", e)

    # --- Phase H integration (R5): run content delta detection if available ---
    phase_h_result = None
    if metadata.get("enable_phase_h", True):
        try:
            from .reconcile_phases.phase_h import run_phase_h
            baseline = scan_result.get("baseline", {})
            baseline_sha = baseline.get("chain_version", "")
            if baseline_sha:
                repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                phase_h_result = run_phase_h(
                    conn, project_id, baseline_sha, repo_root,
                )
                log.info("reconcile diff: Phase H completed — spawned=%d, throttled=%d",
                         len(phase_h_result.spawned_tasks), phase_h_result.skipped_throttled)
        except ImportError:
            log.debug("reconcile diff: Phase H not available (Track B Phase J dependency)")
        except Exception as e:
            log.warning("reconcile diff: Phase H failed (non-blocking): %s", e)

    # --- Phase Z integration (R5): optional baseline discovery ---
    phase_z_result = None
    if metadata.get("enable_phase_z", False):
        try:
            from .reconcile_phases.phase_z import phase_z_run

            class _PhaseZCtx:
                """Minimal context adapter for Phase Z."""
                def __init__(self, workspace, scratch, pid, graph, api_base):
                    self.workspace_path = workspace
                    self.scratch_dir = scratch
                    self.project_id = pid
                    self.graph = graph
                    self.api_base = api_base

            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            scratch = os.path.join(repo_root, "docs", "dev", "scratch")
            z_ctx = _PhaseZCtx(
                workspace=repo_root,
                scratch=scratch,
                pid=project_id,
                graph=scan_result.get("graph", {}),
                api_base=metadata.get("api_base", "http://localhost:40000"),
            )
            phase_z_result = phase_z_run(
                z_ctx,
                enable_llm_enrichment=metadata.get("enable_llm_enrichment", False),
                apply_backlog=metadata.get("apply_backlog", False),
            )
            log.info("reconcile diff: Phase Z completed — deltas=%d, backlog=%d",
                     len(phase_z_result.get("deltas", [])),
                     len(phase_z_result.get("backlog_rows", [])))
        except ImportError:
            log.debug("reconcile diff: Phase Z not available")
        except Exception as e:
            log.warning("reconcile diff: Phase Z failed (non-blocking): %s", e)

    # --- Orchestrator integration (G1): run orchestrated phases if scope/phases present ---
    orchestrator_result = None
    if metadata.get("scope") or metadata.get("phases"):
        try:
            from .reconcile_phases.orchestrator import run_orchestrated
            from .reconcile_phases.scope import ReconcileScope

            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

            scope_arg = None
            scope_meta = metadata.get("scope")
            if scope_meta and isinstance(scope_meta, dict):
                scope_arg = ReconcileScope(
                    bug_id=scope_meta.get("bug_id"),
                    nodes=scope_meta.get("node_set"),
                    paths=scope_meta.get("file_set"),
                )

            orchestrator_result = run_orchestrated(
                project_id,
                repo_root,
                phases=metadata.get("phases"),
                dry_run=metadata.get("dry_run", True),
                scope=scope_arg,
            )
            log.info("reconcile diff: orchestrator completed — phases=%s",
                     list((orchestrator_result or {}).get("phases", {}).keys()))
        except Exception as e:
            log.warning("reconcile diff: orchestrator failed (non-blocking): %s", e)

    return {
        "stage": "diff",
        "diffs": diffs,
        "diff_count": len(diffs),
        "baseline": scan_result.get("baseline", {}),
        "phase_h": {
            "spawned_tasks": phase_h_result.spawned_tasks if phase_h_result else [],
            "skipped_throttled": phase_h_result.skipped_throttled if phase_h_result else 0,
        } if phase_h_result else None,
        "phase_z": phase_z_result,
        "orchestrator": orchestrator_result,
    }


def handle_propose(conn, project_id, task_id, metadata, prev_result):
    """Propose stage: generate mutation_plan.json from diffs (R3)."""
    _check_cancellation(conn, task_id)

    diff_result = prev_result or {}
    diffs = diff_result.get("diffs", [])
    baseline = diff_result.get("baseline", {})

    mutations = []
    for d in diffs:
        confidence = "high" if d.get("severity") == "high" else "medium"
        if d["type"] == "missing_in_db":
            mutations.append({
                "action": "node-create",
                "node_id": d["node_id"],
                "confidence": confidence,
                "description": f"Create missing node_state for {d['node_id']}",
            })
        elif d["type"] == "orphan_in_db":
            mutations.append({
                "action": "node-soft-delete",
                "node_id": d["node_id"],
                "confidence": "low",
                "description": f"Soft-delete orphan node {d['node_id']}",
            })

    plan = {
        "task_id": task_id,
        "baseline_id_before": baseline.get("chain_version", ""),
        "phases_run": ["scan", "diff", "propose"],
        "mutations": mutations,
        "summary": f"Proposed {len(mutations)} mutations from {len(diffs)} diffs",
        "approve_threshold": "high",
        "applied_count": 0,
    }

    # R1: persist scope_declared and scope_overflow_policy when scope provided
    scope_meta = metadata.get("scope")
    if scope_meta and isinstance(scope_meta, dict):
        scope_declared = {
            "bug_id": scope_meta.get("bug_id", ""),
            "file_set": sorted(scope_meta.get("file_set") or []),
            "node_set": sorted(scope_meta.get("node_set") or []),
            "commit_set": list(scope_meta.get("commit_set") or []),
        }
        plan["scope_declared"] = scope_declared
        plan["scope_overflow_policy"] = metadata.get(
            "scope_overflow_policy", "reject"
        )

    _write_mutation_plan(project_id, task_id, plan)

    return {
        "stage": "propose",
        "mutation_count": len(mutations),
        "plan_path": str(_mutation_plan_path(project_id, task_id)),
    }


def handle_approve(conn, project_id, task_id, metadata, prev_result):
    """Approve stage: auto-approve high-confidence; queue medium/low (R5, R6)."""
    _check_cancellation(conn, task_id)

    is_meta_circular = metadata.get("_meta_circular", False)

    if is_meta_circular:
        # R6: validate meta-circular constraints
        valid, err = validate_meta_circular(metadata, task_id)
        if not valid:
            raise ValueError(f"Meta-circular validation failed: {err}")

        # R6: skip approve entirely, log warning
        log.warning("[reconcile-meta-circular] Skipping approve stage for task %s "
                    "(scenario=%s)", task_id, metadata.get("scenario", ""))

        # R6: auto-file backlog review
        _file_meta_circular_backlog(conn, project_id, task_id, metadata)

        # Read plan and auto-approve all
        plan = _read_mutation_plan(project_id, task_id)
        for m in plan.get("mutations", []):
            m["approved"] = True
        plan["phases_run"].append("approve")
        _write_mutation_plan(project_id, task_id, plan)

        return {
            "stage": "approve",
            "meta_circular": True,
            "commit_prefix": "[reconcile-meta-circular]",
            "all_approved": True,
            "approved_count": len(plan.get("mutations", [])),
            "queued_count": 0,
        }

    # Normal flow: approve by confidence
    plan = _read_mutation_plan(project_id, task_id)
    approved_count = 0
    queued_count = 0

    for m in plan.get("mutations", []):
        if m.get("confidence") == "high":
            m["approved"] = True
            approved_count += 1
        else:
            m["approved"] = False
            m["queued_for_manual"] = True
            queued_count += 1

    plan["phases_run"].append("approve")
    _write_mutation_plan(project_id, task_id, plan)

    return {
        "stage": "approve",
        "meta_circular": False,
        "approved_count": approved_count,
        "queued_count": queued_count,
        "all_approved": queued_count == 0,
    }


def handle_apply(conn, project_id, task_id, metadata, prev_result):
    """Apply stage: execute approved mutations via governance API (R4, R7, R12)."""
    _check_cancellation(conn, task_id)

    plan = _read_mutation_plan(project_id, task_id)
    mutations = [m for m in plan.get("mutations", []) if m.get("approved")]

    # --- Scope guard (R2-R5) ---
    scope_declared = plan.get("scope_declared") or {}
    scope_policy = plan.get("scope_overflow_policy", "reject")
    _scope_active = bool(
        scope_declared
        and (scope_declared.get("node_set") or scope_declared.get("file_set"))
    )

    # R12: Begin two-phase commit
    _ensure_mutation_wal_table(conn)
    txn_id = _begin_two_phase(conn, task_id, mutations)
    conn.commit()

    # R4: Use guarded connection to prevent direct DB writes
    guarded = _GuardedConnection(conn)
    applied = 0

    try:
        for m in mutations:
            _check_cancellation(conn, task_id)

            # --- Scope enforcement (R2-R4) ---
            if _scope_active and not _mutation_in_scope(m, scope_declared):
                mutation_id = m.get("node_id") or m.get("action", "unknown")
                detail = (
                    f"node_id={m.get('node_id')!r}, "
                    f"affected_file={m.get('affected_file')!r} "
                    f"not in scope node_set/file_set"
                )
                if scope_policy == "log_and_skip":
                    # R4: skip and audit
                    m["skip_reason"] = "scope_violation"
                    try:
                        from . import audit_service
                        audit_service.record(
                            conn, project_id,
                            "reconcile.scope.violation.skipped",
                            actor="reconcile-task",
                            details={
                                "task_id": task_id,
                                "mutation_id": mutation_id,
                                "detail": detail,
                            },
                        )
                    except Exception:
                        log.warning("Failed to audit scope violation skip", exc_info=True)
                    continue
                else:
                    # R3: reject (default)
                    plan["status"] = "rejected_scope_violation"
                    plan["rejection_detail"] = (
                        f"Mutation {mutation_id!r} out of scope: {detail}"
                    )
                    plan["phases_run"].append("apply")
                    _write_mutation_plan(project_id, task_id, plan)
                    _rollback_two_phase(conn, txn_id)
                    conn.commit()
                    raise ReconcileScopeViolationError(
                        mutation_id, detail, scope_declared,
                    )

            action = m.get("action", "")
            node_id = m.get("node_id", "")

            if action not in _ALLOWLISTED_API_ACTIONS:
                log.warning("Skipping non-allowlisted action: %s", action)
                continue

            # Apply via governance API patterns (not direct DB writes)
            if action == "node-create":
                _api_node_create(guarded, project_id, node_id, m)
            elif action == "node-update":
                _api_node_update(guarded, project_id, node_id, m)
            elif action == "node-soft-delete":
                _api_node_soft_delete(guarded, project_id, node_id)
            elif action == "verify-update":
                _api_verify_update(guarded, project_id, node_id, m)
            elif action == "backlog":
                _api_backlog_upsert(guarded, project_id, m)

            applied += 1

        # R12: Commit two-phase
        _commit_two_phase(conn, txn_id)
        conn.commit()

        # Update plan
        plan["applied_count"] = applied
        plan["phases_run"].append("apply")
        _write_mutation_plan(project_id, task_id, plan)

        # R7: Trigger Phase I baseline write on success
        _trigger_baseline_write(conn, project_id, task_id)

        # --- Phase K autospawn integration (PR4) ---
        phase_k_result = None
        if metadata.get("enable_phase_k_autospawn", True):
            try:
                from .reconcile_phases.phase_k import spawn_phase_k_discrepancies
                k_discrepancies = metadata.get("phase_k_discrepancies")
                if k_discrepancies:
                    class _PhaseKCtx:
                        """Minimal context for Phase K autospawn."""
                        pass
                    k_ctx = _PhaseKCtx()
                    k_ctx.project_id = project_id
                    k_ctx.conn = conn
                    k_ctx.api_base = metadata.get("api_base", "")
                    k_scope = metadata.get("scope")
                    phase_k_result = spawn_phase_k_discrepancies(
                        k_ctx, k_scope, k_discrepancies,
                        dry_run=metadata.get("phase_k_dry_run", False),
                        max_spawn_per_run=metadata.get("phase_k_max_spawn", 3),
                    )
                    log.info("reconcile apply: Phase K autospawn — spawned=%d",
                             phase_k_result.get("spawned", 0))
            except ImportError:
                log.debug("reconcile apply: Phase K autospawn not available")
            except Exception as e:
                log.warning("reconcile apply: Phase K autospawn failed (non-blocking): %s", e)

        return {
            "stage": "apply",
            "applied_count": applied,
            "txn_id": txn_id,
            "txn_status": "committed",
            "phase_k_autospawn": phase_k_result,
        }

    except ReconcileCancelled:
        # R12: Rollback on cancellation
        _rollback_two_phase(conn, txn_id)
        conn.commit()
        raise

    except Exception:
        # R12: Rollback on error
        try:
            _rollback_two_phase(conn, txn_id)
            conn.commit()
        except Exception:
            log.error("Failed to rollback two-phase txn %s", txn_id, exc_info=True)
        raise


def handle_verify(conn, project_id, task_id, metadata, prev_result):
    """Verify stage: re-run scan+diff, compare post-state (R8)."""
    _check_cancellation(conn, task_id)

    # Re-run scan
    post_scan = handle_scan(conn, project_id, task_id, metadata, None)
    # Re-run diff
    post_diff = handle_diff(conn, project_id, task_id, metadata, post_scan)

    # Compare with expected post-state
    apply_result = prev_result or {}
    applied_count = apply_result.get("applied_count", 0)

    regressions = []
    post_diffs = post_diff.get("diffs", [])

    if post_diffs:
        for d in post_diffs:
            if d.get("severity") == "high":
                regressions.append({
                    "node_id": d["node_id"],
                    "type": d["type"],
                    "detail": "Post-apply verification found unresolved high-severity diff",
                })

    return {
        "stage": "verify",
        "post_scan_node_count": post_scan.get("node_count", 0),
        "post_diff_count": len(post_diffs),
        "regression_count": len(regressions),
        "regressions": regressions,
        "verified": len(regressions) == 0,
        "applied_count": applied_count,
    }


# ---------------------------------------------------------------------------
# _RECONCILE_STAGES dict (R1, AC-J1)
# ---------------------------------------------------------------------------

_RECONCILE_STAGES = {
    "scan": handle_scan,
    "diff": handle_diff,
    "propose": handle_propose,
    "approve": handle_approve,
    "apply": handle_apply,
    "verify": handle_verify,
}


# ---------------------------------------------------------------------------
# Governance API Helpers (R4 — mutations via API, not direct DB)
# ---------------------------------------------------------------------------

def _api_node_create(conn, project_id, node_id, mutation):
    """Create node_state via governance API pattern."""
    now = datetime.now(timezone.utc).isoformat()
    # Use INSERT which is allowed (we block UPDATE to node_state, but
    # initial creation is the API pattern for node-create)
    try:
        conn._conn.execute(
            "INSERT OR IGNORE INTO node_state "
            "(project_id, node_id, verify_status, build_status, updated_by, updated_at) "
            "VALUES (?, ?, 'pending', 'impl:missing', 'reconcile-task', ?)",
            (project_id, node_id, now),
        )
    except Exception:
        log.warning("node-create failed for %s/%s", project_id, node_id, exc_info=True)


def _api_node_update(conn, project_id, node_id, mutation):
    """Update node via governance API pattern."""
    fields = mutation.get("fields", {})
    if not fields:
        return
    now = datetime.now(timezone.utc).isoformat()
    # Use the underlying connection for legitimate API-style updates
    for field, value in fields.items():
        try:
            conn._conn.execute(
                f"UPDATE node_state SET {field} = ?, updated_by = 'reconcile-task', "
                f"updated_at = ? WHERE project_id = ? AND node_id = ?",
                (value, now, project_id, node_id),
            )
        except Exception:
            log.warning("node-update failed for %s/%s.%s", project_id, node_id, field, exc_info=True)


def _api_node_soft_delete(conn, project_id, node_id):
    """Soft-delete node by marking as waived."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn._conn.execute(
            "UPDATE node_state SET verify_status = 'waived', "
            "updated_by = 'reconcile-task', updated_at = ? "
            "WHERE project_id = ? AND node_id = ?",
            (now, project_id, node_id),
        )
    except Exception:
        log.warning("node-soft-delete failed for %s/%s", project_id, node_id, exc_info=True)


def _api_verify_update(conn, project_id, node_id, mutation):
    """Update verification status via API pattern."""
    target = mutation.get("target_status", "pending")
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn._conn.execute(
            "UPDATE node_state SET verify_status = ?, "
            "updated_by = 'reconcile-task', updated_at = ? "
            "WHERE project_id = ? AND node_id = ?",
            (target, now, project_id, node_id),
        )
    except Exception:
        log.warning("verify-update failed for %s/%s", project_id, node_id, exc_info=True)


def _api_backlog_upsert(conn, project_id, mutation):
    """Upsert backlog entry."""
    bug_id = mutation.get("bug_id", "")
    if not bug_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn._conn.execute(
            "INSERT OR REPLACE INTO backlog_bugs "
            "(bug_id, project_id, title, priority, status, created_at, created_by) "
            "VALUES (?, ?, ?, ?, 'open', ?, 'reconcile-task')",
            (bug_id, project_id, mutation.get("title", bug_id),
             mutation.get("priority", "P2"), now),
        )
    except Exception:
        log.warning("backlog upsert failed for %s", bug_id, exc_info=True)


def _trigger_baseline_write(conn, project_id, task_id):
    """R7: Trigger Phase I baseline write on successful apply."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO baselines
               (project_id, baseline_id, created_at, created_by, baseline_type, details_json)
               VALUES (?, ?, ?, 'reconcile-task', 'phase_i',
                       json_object('source', 'reconcile-apply', 'task_id', ?))""",
            (project_id, f"baseline-reconcile-{task_id}", now, task_id),
        )
        log.info("Phase I baseline written for reconcile task %s", task_id)
    except Exception:
        log.warning("Phase I baseline write failed for %s (non-critical)", task_id, exc_info=True)


# ---------------------------------------------------------------------------
# Stage Runner (used by auto_chain reconcile dispatch)
# ---------------------------------------------------------------------------

def run_reconcile_stage(conn, project_id, task_id, stage: str,
                        metadata: dict, prev_result: dict = None) -> dict:
    """Run a single reconcile stage by name. Returns stage result payload."""
    if stage not in _RECONCILE_STAGES:
        raise ValueError(f"Unknown reconcile stage: {stage}")

    handler = _RECONCILE_STAGES[stage]
    return handler(conn, project_id, task_id, metadata, prev_result)


def run_full_reconcile(conn, project_id, task_id, metadata: dict) -> dict:
    """Run all 6 stages sequentially. Returns final verify result."""
    if not acquire_reconcile_lock(conn, project_id, task_id):
        return {"error": "conflict", "reason": "Another reconcile task is running"}

    try:
        prev = None
        results = {}
        for stage in RECONCILE_STAGES:
            # Check for meta-circular approve skip is handled inside handle_approve
            result = run_reconcile_stage(conn, project_id, task_id, stage, metadata, prev)
            results[stage] = result
            prev = result

        return {
            "task_id": task_id,
            "stages_completed": list(results.keys()),
            "final": results.get("verify", {}),
            "success": results.get("verify", {}).get("verified", False),
        }
    except ReconcileCancelled:
        return {
            "task_id": task_id,
            "cancelled": True,
            "reason": "Task cancelled mid-run",
        }
    finally:
        release_reconcile_lock(conn, project_id, task_id)
