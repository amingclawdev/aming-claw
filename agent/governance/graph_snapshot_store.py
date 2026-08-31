"""Commit-indexed graph snapshot state store.

This module is intentionally state-only: it stores graph snapshots, indexes,
drift rows, and pending scope-reconcile rows. It does not modify source,
documentation, or test files.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


QA_GRAPH_BASIS_EXACT_CANDIDATE = "exact_candidate_snapshot"
QA_GRAPH_BASIS_CANONICAL_BASE_DIFF = "canonical_base_plus_candidate_diff"
QA_GRAPH_BASES = frozenset(
    {
        QA_GRAPH_BASIS_EXACT_CANDIDATE,
        QA_GRAPH_BASIS_CANONICAL_BASE_DIFF,
    }
)


GRAPH_SNAPSHOT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_snapshots (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  parent_snapshot_id TEXT NOT NULL DEFAULT '',
  snapshot_kind TEXT NOT NULL,
  ref_name TEXT NOT NULL DEFAULT '',
  branch_ref TEXT NOT NULL DEFAULT '',
  graph_sha256 TEXT NOT NULL DEFAULT '',
  inventory_sha256 TEXT NOT NULL DEFAULT '',
  drift_sha256 TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_snapshots_commit
  ON graph_snapshots(project_id, commit_sha);

CREATE INDEX IF NOT EXISTS idx_graph_snapshots_status
  ON graph_snapshots(project_id, status, commit_sha);

CREATE TABLE IF NOT EXISTS graph_current_full_reconcile_provenance (
  provenance_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  target_commit_sha TEXT NOT NULL,
  protected_action TEXT NOT NULL,
  protected_entrypoint TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_started_at TEXT NOT NULL,
  marker_created_at TEXT NOT NULL,
  reconcile_event_id INTEGER NOT NULL,
  reconcile_event_created_at TEXT NOT NULL,
  route_evidence_json TEXT NOT NULL DEFAULT '{}',
  marker_json TEXT NOT NULL DEFAULT '{}',
  provenance_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_current_full_reconcile_provenance_target
  ON graph_current_full_reconcile_provenance(
    project_id, target_commit_sha, snapshot_id, reconcile_event_id
  );

CREATE TABLE IF NOT EXISTS graph_snapshot_refs (
  project_id TEXT NOT NULL,
  ref_name TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, ref_name)
);

CREATE TABLE IF NOT EXISTS graph_ref_events (
  project_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  ref_name TEXT NOT NULL,
  branch_ref TEXT NOT NULL DEFAULT '',
  batch_id TEXT NOT NULL DEFAULT '',
  merge_queue_id TEXT NOT NULL DEFAULT '',
  operation_type TEXT NOT NULL,
  old_snapshot_id TEXT NOT NULL DEFAULT '',
  new_snapshot_id TEXT NOT NULL DEFAULT '',
  old_commit TEXT NOT NULL DEFAULT '',
  new_commit TEXT NOT NULL DEFAULT '',
  old_projection_id TEXT NOT NULL DEFAULT '',
  new_projection_id TEXT NOT NULL DEFAULT '',
  merge_epoch TEXT NOT NULL DEFAULT '',
  rollback_epoch TEXT NOT NULL DEFAULT '',
  replay_epoch TEXT NOT NULL DEFAULT '',
  source_event_id TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY(project_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_ref_events_ref
  ON graph_ref_events(project_id, ref_name, created_at);

CREATE INDEX IF NOT EXISTS idx_graph_ref_events_operation
  ON graph_ref_events(project_id, operation_type, created_at);

CREATE TABLE IF NOT EXISTS graph_nodes_index (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  primary_files_json TEXT NOT NULL DEFAULT '[]',
  secondary_files_json TEXT NOT NULL DEFAULT '[]',
  test_files_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, snapshot_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_primary
  ON graph_nodes_index(project_id, snapshot_id, node_id);

CREATE TABLE IF NOT EXISTS graph_edges_index (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, snapshot_id, src, dst, edge_type, direction)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_dst
  ON graph_edges_index(project_id, snapshot_id, dst);

CREATE TABLE IF NOT EXISTS graph_drift_ledger (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  path TEXT NOT NULL,
  node_id TEXT NOT NULL DEFAULT '',
  target_symbol TEXT NOT NULL DEFAULT '',
  drift_type TEXT NOT NULL,
  status TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, snapshot_id, path, drift_type, target_symbol)
);

CREATE INDEX IF NOT EXISTS idx_graph_drift_status
  ON graph_drift_ledger(project_id, status, drift_type);

CREATE TABLE IF NOT EXISTS pending_scope_reconcile (
  project_id TEXT NOT NULL,
  ref_name TEXT NOT NULL DEFAULT 'active',
  branch_ref TEXT NOT NULL DEFAULT '',
  worktree_id TEXT NOT NULL DEFAULT '',
  worktree_path TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL,
  parent_commit_sha TEXT NOT NULL DEFAULT '',
  queued_at TEXT NOT NULL,
  status TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  snapshot_id TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, ref_name, worktree_id, commit_sha)
);

CREATE TABLE IF NOT EXISTS reconcile_run_metrics (
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL DEFAULT '',
  parent_commit_sha TEXT NOT NULL DEFAULT '',
  snapshot_kind TEXT NOT NULL DEFAULT '',
  strategy TEXT NOT NULL DEFAULT '',
  graph_delta_mode TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  changed_file_count INTEGER NOT NULL DEFAULT 0,
  impacted_file_count INTEGER NOT NULL DEFAULT 0,
  event_count INTEGER NOT NULL DEFAULT 0,
  node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  elapsed_ms INTEGER NOT NULL DEFAULT 0,
  trace_summary_path TEXT NOT NULL DEFAULT '',
  fallback_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, run_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_project_created
  ON reconcile_run_metrics(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_strategy
  ON reconcile_run_metrics(project_id, strategy, graph_delta_mode);

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_queue_recent
  ON reconcile_run_metrics(
    project_id, created_at DESC, run_id DESC, snapshot_id DESC
  );

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_queue_strategy_recent
  ON reconcile_run_metrics(
    project_id, strategy, created_at DESC, run_id DESC, snapshot_id DESC
  );

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_queue_nonterminal
  ON reconcile_run_metrics(
    project_id, created_at DESC, run_id DESC, snapshot_id DESC
  )
  WHERE LOWER(TRIM(status)) NOT IN (
    'candidate_ready', 'complete', 'failed', 'terminalized_stale'
  );

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_queue_strategy_nonterminal
  ON reconcile_run_metrics(
    project_id, strategy, created_at DESC, run_id DESC, snapshot_id DESC
  )
  WHERE LOWER(TRIM(status)) NOT IN (
    'candidate_ready', 'complete', 'failed', 'terminalized_stale'
  );

CREATE TABLE IF NOT EXISTS graph_current_full_build_claim_history (
  claim_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  status TEXT NOT NULL,
  manager_epoch TEXT NOT NULL,
  manager_pid INTEGER NOT NULL,
  manager_started_at TEXT NOT NULL,
  manager_start_identity TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  released_at TEXT NOT NULL DEFAULT '',
  terminal_status TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_current_full_build_claim_active_snapshot
  ON graph_current_full_build_claim_history(project_id, snapshot_id)
  WHERE status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS idx_current_full_build_claim_identity
  ON graph_current_full_build_claim_history(project_id, run_id, snapshot_id);

CREATE TABLE IF NOT EXISTS graph_reconcile_manager_generations (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  certificate_id TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  manager_pid INTEGER NOT NULL,
  manager_started_at TEXT NOT NULL,
  process_start_identity TEXT NOT NULL,
  manager_start_identity TEXT NOT NULL,
  lock_identity TEXT NOT NULL,
  predecessor_certificate_id TEXT NOT NULL DEFAULT '',
  predecessor_generation_id TEXT NOT NULL DEFAULT '',
  predecessor_sequence INTEGER NOT NULL DEFAULT 0,
  predecessor_certificate_hash TEXT NOT NULL DEFAULT '',
  prior_manager_pid INTEGER NOT NULL DEFAULT 0,
  observed_prior_generation_id TEXT NOT NULL DEFAULT '',
  prior_process_start_identity TEXT NOT NULL DEFAULT '',
  prior_pid_death_method TEXT NOT NULL DEFAULT '',
  prior_pid_death_verified_at TEXT NOT NULL DEFAULT '',
  certified_at TEXT NOT NULL,
  certificate_hash TEXT NOT NULL,
  UNIQUE(project_id, generation_id),
  UNIQUE(project_id, manager_start_identity),
  UNIQUE(project_id, lock_identity),
  CHECK(manager_pid > 0),
  CHECK(prior_manager_pid >= 0),
  CHECK(prior_pid_death_method IN ('', 'esrch')),
  CHECK(
    (prior_manager_pid = 0
      AND prior_process_start_identity = ''
      AND observed_prior_generation_id = ''
      AND prior_pid_death_method = ''
      AND prior_pid_death_verified_at = '')
    OR
    (prior_manager_pid > 0
      AND prior_pid_death_method = 'esrch'
      AND prior_pid_death_verified_at <> '')
  ),
  CHECK(
    (predecessor_certificate_id = ''
      AND predecessor_generation_id = ''
      AND predecessor_sequence = 0
      AND predecessor_certificate_hash = '')
    OR
    (predecessor_certificate_id <> ''
      AND predecessor_generation_id <> ''
      AND predecessor_sequence > 0
      AND predecessor_certificate_hash <> '')
  )
);

CREATE INDEX IF NOT EXISTS idx_reconcile_manager_generation_latest
  ON graph_reconcile_manager_generations(project_id, sequence DESC);

CREATE TRIGGER IF NOT EXISTS trg_reconcile_manager_generation_insert_identity
BEFORE INSERT ON graph_reconcile_manager_generations
WHEN EXISTS (
  SELECT 1 FROM graph_reconcile_manager_generations
  WHERE project_id = NEW.project_id
    AND (
      certificate_id = NEW.certificate_id
      OR generation_id = NEW.generation_id
      OR manager_start_identity = NEW.manager_start_identity
      OR lock_identity = NEW.lock_identity
    )
)
BEGIN
  SELECT RAISE(ABORT, 'manager_generation_identity_conflict');
END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_manager_generation_no_update
BEFORE UPDATE ON graph_reconcile_manager_generations
BEGIN
  SELECT RAISE(ABORT, 'manager_generation_append_only');
END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_manager_generation_no_delete
BEFORE DELETE ON graph_reconcile_manager_generations
BEGIN
  SELECT RAISE(ABORT, 'manager_generation_append_only');
END;

CREATE TABLE IF NOT EXISTS graph_reconcile_metric_physical_identities (
  identity_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  metric_rowid INTEGER NOT NULL CHECK(metric_rowid > 0)
);

CREATE INDEX IF NOT EXISTS idx_reconcile_metric_physical_identity_latest
  ON graph_reconcile_metric_physical_identities(
    project_id, run_id, snapshot_id, identity_sequence DESC
  );

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_after_insert
AFTER INSERT ON reconcile_run_metrics
BEGIN
  INSERT INTO graph_reconcile_metric_physical_identities
    (project_id, run_id, snapshot_id, metric_rowid)
  VALUES (NEW.project_id, NEW.run_id, NEW.snapshot_id, NEW.rowid);
END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_insert_conflict
BEFORE INSERT ON graph_reconcile_metric_physical_identities
WHEN EXISTS (
  SELECT 1 FROM graph_reconcile_metric_physical_identities
  WHERE identity_sequence = NEW.identity_sequence
)
BEGIN SELECT RAISE(ABORT, 'reconcile_metric_identity_conflict'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_no_update
BEFORE UPDATE ON graph_reconcile_metric_physical_identities
BEGIN SELECT RAISE(ABORT, 'reconcile_metric_identity_append_only'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_no_delete
BEFORE DELETE ON graph_reconcile_metric_physical_identities
BEGIN SELECT RAISE(ABORT, 'reconcile_metric_identity_append_only'); END;

CREATE TABLE IF NOT EXISTS graph_reconcile_metric_identity_schema_state (
  marker TEXT PRIMARY KEY
    CHECK(marker = 'physical_identity_backfill_v1')
);

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_marker_complete
BEFORE INSERT ON graph_reconcile_metric_identity_schema_state
WHEN EXISTS (
  SELECT 1 FROM reconcile_run_metrics AS metric WHERE NOT EXISTS (
    SELECT 1 FROM graph_reconcile_metric_physical_identities AS identity
    WHERE identity.project_id=metric.project_id AND identity.run_id=metric.run_id
      AND identity.snapshot_id=metric.snapshot_id AND identity.metric_rowid=metric.rowid
  )
)
BEGIN SELECT RAISE(ABORT, 'reconcile_metric_identity_marker_incomplete'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_marker_insert_conflict
BEFORE INSERT ON graph_reconcile_metric_identity_schema_state
WHEN EXISTS (SELECT 1 FROM graph_reconcile_metric_identity_schema_state)
BEGIN SELECT RAISE(ABORT, 'reconcile_metric_identity_marker_conflict'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_marker_no_update
BEFORE UPDATE ON graph_reconcile_metric_identity_schema_state
BEGIN SELECT RAISE(ABORT, 'reconcile_metric_identity_marker_append_only'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_metric_identity_marker_no_delete
BEFORE DELETE ON graph_reconcile_metric_identity_schema_state
BEGIN SELECT RAISE(ABORT, 'reconcile_metric_identity_marker_append_only'); END;

CREATE TABLE IF NOT EXISTS graph_reconcile_run_terminalizations (
  terminalization_id TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  source_run_id TEXT NOT NULL,
  source_snapshot_id TEXT NOT NULL,
  source_metric_identity_sequence INTEGER NOT NULL CHECK(source_metric_identity_sequence > 0),
  source_raw_status TEXT NOT NULL CHECK(source_raw_status IN ('running','finalizing')),
  source_fingerprint TEXT NOT NULL,
  replacement_proof_kind TEXT NOT NULL DEFAULT 'materialized'
    CHECK(replacement_proof_kind IN ('materialized','manager_generation')),
  replacement_run_id TEXT NOT NULL,
  replacement_snapshot_id TEXT NOT NULL,
  replacement_metric_identity_sequence INTEGER NOT NULL CHECK(replacement_metric_identity_sequence > 0),
  replacement_raw_status TEXT NOT NULL CHECK(replacement_raw_status IN ('candidate_ready','complete')),
  replacement_fingerprint TEXT NOT NULL,
  manager_certificate_id TEXT NOT NULL,
  manager_certificate_hash TEXT NOT NULL,
  timeline_event_id INTEGER NOT NULL CHECK(timeline_event_id > 0),
  timeline_event_hash TEXT NOT NULL,
  terminal_status TEXT NOT NULL CHECK(terminal_status = 'terminalized_stale'),
  created_at TEXT NOT NULL,
  ledger_hash TEXT NOT NULL,
  PRIMARY KEY(project_id, source_run_id, source_snapshot_id),
  CHECK(length(source_fingerprint)=71 AND substr(source_fingerprint,1,7)='sha256:' AND substr(source_fingerprint,8) NOT GLOB '*[^0-9a-f]*'),
  CHECK(length(replacement_fingerprint)=71 AND substr(replacement_fingerprint,1,7)='sha256:' AND substr(replacement_fingerprint,8) NOT GLOB '*[^0-9a-f]*'),
  CHECK(length(manager_certificate_hash)=71 AND substr(manager_certificate_hash,1,7)='sha256:' AND substr(manager_certificate_hash,8) NOT GLOB '*[^0-9a-f]*'),
  CHECK(length(timeline_event_hash)=71 AND substr(timeline_event_hash,1,7)='sha256:' AND substr(timeline_event_hash,8) NOT GLOB '*[^0-9a-f]*'),
  CHECK(length(ledger_hash)=71 AND substr(ledger_hash,1,7)='sha256:' AND substr(ledger_hash,8) NOT GLOB '*[^0-9a-f]*')
);

CREATE TRIGGER IF NOT EXISTS trg_reconcile_run_terminalization_insert_conflict
BEFORE INSERT ON graph_reconcile_run_terminalizations
WHEN EXISTS (
  SELECT 1 FROM graph_reconcile_run_terminalizations
  WHERE terminalization_id=NEW.terminalization_id OR (
    project_id=NEW.project_id AND source_run_id=NEW.source_run_id
    AND source_snapshot_id=NEW.source_snapshot_id
  )
)
BEGIN SELECT RAISE(ABORT, 'reconcile_run_terminalization_identity_conflict'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_run_terminalization_no_update
BEFORE UPDATE ON graph_reconcile_run_terminalizations
BEGIN SELECT RAISE(ABORT, 'reconcile_run_terminalization_append_only'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconcile_run_terminalization_no_delete
BEFORE DELETE ON graph_reconcile_run_terminalizations
BEGIN SELECT RAISE(ABORT, 'reconcile_run_terminalization_append_only'); END;
"""

SNAPSHOT_STATUS_CANDIDATE = "candidate"
SNAPSHOT_STATUS_FINALIZING = "finalizing"
SNAPSHOT_STATUS_ACTIVE = "active"
SNAPSHOT_STATUS_SUPERSEDED = "superseded"
SNAPSHOT_STATUS_ABANDONED = "abandoned"

ALLOWED_SNAPSHOT_STATUSES = {
    SNAPSHOT_STATUS_CANDIDATE,
    SNAPSHOT_STATUS_FINALIZING,
    SNAPSHOT_STATUS_ACTIVE,
    SNAPSHOT_STATUS_SUPERSEDED,
    SNAPSHOT_STATUS_ABANDONED,
}

PENDING_STATUS_QUEUED = "queued"
PENDING_STATUS_RUNNING = "running"
PENDING_STATUS_MATERIALIZED = "materialized"
PENDING_STATUS_FAILED = "failed"
PENDING_STATUS_WAIVED = "waived"
PENDING_STATUS_SUPERSEDED = "superseded"

ALLOWED_PENDING_STATUSES = {
    PENDING_STATUS_QUEUED,
    PENDING_STATUS_RUNNING,
    PENDING_STATUS_MATERIALIZED,
    PENDING_STATUS_FAILED,
    PENDING_STATUS_WAIVED,
    PENDING_STATUS_SUPERSEDED,
}
GRAPH_REF_OPERATION_TYPES = {
    "activate",
    "merge",
    "rollback",
    "revert",
    "replay",
    "backfill_escape",
}


class GraphSnapshotConflictError(RuntimeError):
    """Raised when snapshot activation loses its compare-and-swap race."""


class GraphSnapshotBuildClaimConflictError(RuntimeError):
    """Raised when a durable current-full build claim cannot be acquired."""

    def __init__(self, reason: str, claim: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.reason = str(reason or "current_full_build_claim_conflict")
        self.claim = dict(claim or {})


class ManagerGenerationCertificateConflictError(RuntimeError):
    """Raised when immutable manager-generation authority cannot be appended."""

    def __init__(self, reason: str, certificate: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.reason = str(reason or "manager_generation_certificate_conflict")
        self.certificate = dict(certificate or {})


class ReconcileRunTerminalizationProofError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code or "terminalization_proof_invalid")
        super().__init__(self.reason_code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(GRAPH_SNAPSHOT_SCHEMA_SQL)
    _ensure_graph_snapshot_ref_columns(conn)
    _ensure_reconcile_terminalization_ledger_columns(conn)
    _migrate_pending_scope_reconcile_branch_identity(conn)


def _graph_activation_policy_for_connection(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Bind active-graph effects to the opened DB's physical world identity.

    No caller-supplied plane, outer HTTP guard, or ambient environment can
    promote this connection.  This is the final pre-effect fence for graph
    refs, semantic projection rebuilds, and graph-ref events.
    """
    from .db import classify_graph_activation_connection

    return classify_graph_activation_connection(conn)


def _require_active_graph_activation_for_connection(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    policy = _graph_activation_policy_for_connection(conn)
    if policy.get("active_graph_activation_allowed") is not True:
        raise ValueError(
            "active graph activation is forbidden for this database runtime plane"
        )
    return policy


RECONCILE_METRIC_PHYSICAL_IDENTITY_SCHEMA = (
    "graph_reconcile_metric_physical_identity.v1"
)
_RECONCILE_METRIC_PHYSICAL_IDENTITY_MARKER = "physical_identity_backfill_v1"


def _reconcile_metric_identity_before_marker_hook(
    _conn: sqlite3.Connection,
) -> None:
    """Internal fault-injection seam after backfill and before its marker."""


def _reconcile_metric_identity_migration_receipt(
    *, backfilled: int, writes_performed: bool
) -> dict[str, Any]:
    return {
        "schema_version": RECONCILE_METRIC_PHYSICAL_IDENTITY_SCHEMA,
        "migration_complete": True,
        "backfilled": int(backfilled),
        "writes_performed": bool(writes_performed),
    }


def ensure_reconcile_metric_physical_identity_migration(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Recoverably backfill immutable metric identities after schema prewarm."""

    marker = conn.execute(
        "SELECT 1 FROM graph_reconcile_metric_identity_schema_state "
        "WHERE marker = ?",
        (_RECONCILE_METRIC_PHYSICAL_IDENTITY_MARKER,),
    ).fetchone()
    if marker:
        return _reconcile_metric_identity_migration_receipt(
            backfilled=0, writes_performed=False
        )
    if conn.in_transaction:
        raise RuntimeError("metric identity migration requires a clean transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        marker = conn.execute(
            "SELECT 1 FROM graph_reconcile_metric_identity_schema_state "
            "WHERE marker = ?",
            (_RECONCILE_METRIC_PHYSICAL_IDENTITY_MARKER,),
        ).fetchone()
        if marker:
            conn.rollback()
            return _reconcile_metric_identity_migration_receipt(
                backfilled=0, writes_performed=False
            )
        before_changes = conn.total_changes
        conn.execute(
            "INSERT INTO graph_reconcile_metric_physical_identities "
            "(project_id,run_id,snapshot_id,metric_rowid) "
            "SELECT metric.project_id,metric.run_id,metric.snapshot_id,metric.rowid "
            "FROM reconcile_run_metrics AS metric WHERE NOT EXISTS ("
            "SELECT 1 FROM graph_reconcile_metric_physical_identities AS identity "
            "WHERE identity.project_id=metric.project_id "
            "AND identity.run_id=metric.run_id "
            "AND identity.snapshot_id=metric.snapshot_id "
            "AND identity.metric_rowid=metric.rowid) ORDER BY metric.rowid"
        )
        backfilled = conn.total_changes - before_changes
        _reconcile_metric_identity_before_marker_hook(conn)
        conn.execute(
            "INSERT INTO graph_reconcile_metric_identity_schema_state(marker) "
            "VALUES (?)",
            (_RECONCILE_METRIC_PHYSICAL_IDENTITY_MARKER,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _reconcile_metric_identity_migration_receipt(
        backfilled=backfilled, writes_performed=True
    )


MANAGER_GENERATION_CERTIFICATE_SCHEMA = (
    "graph_reconcile_manager_generation_certificate.v1"
)
MANAGER_GENERATION_MAX_CHAIN_DEPTH = 10_000
_MANAGER_GENERATION_PUBLIC_FIELDS = (
    "certificate_id",
    "sequence",
    "project_id",
    "generation_id",
    "manager_pid",
    "manager_started_at",
    "process_start_identity",
    "manager_start_identity",
    "lock_identity",
    "predecessor_certificate_id",
    "predecessor_generation_id",
    "predecessor_sequence",
    "predecessor_certificate_hash",
    "prior_manager_pid",
    "observed_prior_generation_id",
    "prior_process_start_identity",
    "prior_pid_death_method",
    "prior_pid_death_verified_at",
    "certified_at",
    "certificate_hash",
)


def _manager_generation_certificate_id(project_id: str, generation_id: str) -> str:
    digest = hashlib.sha256(
        (str(project_id) + "\0" + str(generation_id)).encode("utf-8")
    ).hexdigest()
    return f"gmcert-{digest[:24]}"


def _manager_generation_certificate_hash(values: Mapping[str, Any]) -> str:
    exact = {
        key: values[key]
        for key in _MANAGER_GENERATION_PUBLIC_FIELDS
        if key != "certificate_hash"
    }
    encoded = json.dumps(
        exact,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def manager_generation_certificate_public_receipt(
    certificate: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one fixed copy-safe generation receipt without private paths."""

    result = {
        "schema_version": MANAGER_GENERATION_CERTIFICATE_SCHEMA,
        **{
            key: certificate.get(
                key,
                0
                if key in {
                    "sequence",
                    "manager_pid",
                    "prior_manager_pid",
                    "predecessor_sequence",
                }
                else "",
            )
            for key in _MANAGER_GENERATION_PUBLIC_FIELDS
        },
        "server_derived": True,
    }
    result["sequence"] = int(result["sequence"] or 0)
    result["manager_pid"] = int(result["manager_pid"] or 0)
    result["prior_manager_pid"] = int(result["prior_manager_pid"] or 0)
    result["predecessor_sequence"] = int(result["predecessor_sequence"] or 0)
    return result


def _manager_generation_exact_replay(
    existing: Mapping[str, Any],
    supplied: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {
        key: supplied[key]
        for key in supplied
        if key != "certificate_hash"
    }
    expected["predecessor_certificate_id"] = str(
        existing.get("predecessor_certificate_id") or ""
    )
    expected["predecessor_generation_id"] = str(
        existing.get("predecessor_generation_id") or ""
    )
    expected["predecessor_sequence"] = int(
        existing.get("predecessor_sequence") or 0
    )
    expected["predecessor_certificate_hash"] = str(
        existing.get("predecessor_certificate_hash") or ""
    )
    expected["sequence"] = int(existing.get("sequence") or 0)
    expected["certificate_hash"] = _manager_generation_certificate_hash(expected)
    comparable = {
        key: existing.get(key)
        for key in expected
    }
    if comparable != expected:
        raise ManagerGenerationCertificateConflictError(
            "manager_generation_replay_conflict", existing
        )
    receipt = manager_generation_certificate_public_receipt(existing)
    receipt.update({"replayed": True, "writes_performed": False})
    return receipt


def _manager_generation_after_insert_hook() -> None:
    """Internal fault-injection seam; production behavior is intentionally empty."""


def _validate_manager_generation_certificate_row(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
) -> int:
    current = dict(row)
    seen: set[str] = set()
    for _depth in range(MANAGER_GENERATION_MAX_CHAIN_DEPTH):
        certificate_id = str(current.get("certificate_id") or "")
        if not certificate_id or certificate_id in seen:
            raise ManagerGenerationCertificateConflictError(
                "manager_generation_predecessor_cycle", current
            )
        seen.add(certificate_id)
        expected_hash = _manager_generation_certificate_hash(current)
        if not hmac.compare_digest(
            str(current.get("certificate_hash") or ""), expected_hash
        ):
            raise ManagerGenerationCertificateConflictError(
                "manager_generation_certificate_hash_invalid", current
            )
        predecessor_id = str(current.get("predecessor_certificate_id") or "")
        if not predecessor_id:
            return len(seen)
        predecessor_row = conn.execute(
            "SELECT * FROM graph_reconcile_manager_generations "
            "WHERE certificate_id = ? AND project_id = ?",
            (predecessor_id, str(current.get("project_id") or "")),
        ).fetchone()
        predecessor = dict(predecessor_row) if predecessor_row else {}
        if not predecessor:
            raise ManagerGenerationCertificateConflictError(
                "manager_generation_predecessor_missing", current
            )
        expected = {
            "predecessor_generation_id": str(
                predecessor.get("generation_id") or ""
            ),
            "predecessor_sequence": int(predecessor.get("sequence") or 0),
            "predecessor_certificate_hash": str(
                predecessor.get("certificate_hash") or ""
            ),
        }
        if (
            int(predecessor.get("sequence") or 0)
            >= int(current.get("sequence") or 0)
            or any(current.get(key) != value for key, value in expected.items())
        ):
            raise ManagerGenerationCertificateConflictError(
                "manager_generation_predecessor_binding_invalid", current
            )
        current = predecessor
    raise ManagerGenerationCertificateConflictError(
        "manager_generation_history_too_deep", current
    )


def record_manager_generation_certificate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    generation_id: str,
    manager_pid: int,
    manager_started_at: str,
    process_start_identity: str,
    manager_start_identity: str,
    lock_identity: str,
    prior_manager_pid: int = 0,
    observed_prior_generation_id: str = "",
    prior_process_start_identity: str = "",
    prior_pid_death_method: str = "",
    prior_pid_death_verified_at: str = "",
    certified_at: str,
) -> dict[str, Any]:
    """Append one exact, immutable, linearly chained manager certificate."""

    ensure_schema(conn)
    conn.commit()
    project = str(project_id or "")
    prior_pid = int(prior_manager_pid or 0)
    values: dict[str, Any] = {
        "certificate_id": _manager_generation_certificate_id(
            project, str(generation_id or "")
        ),
        "project_id": project,
        "generation_id": str(generation_id or ""),
        "manager_pid": int(manager_pid or 0),
        "manager_started_at": str(manager_started_at or ""),
        "process_start_identity": str(process_start_identity or ""),
        "manager_start_identity": str(manager_start_identity or ""),
        "lock_identity": str(lock_identity or ""),
        "prior_manager_pid": prior_pid,
        "observed_prior_generation_id": str(observed_prior_generation_id or ""),
        "prior_process_start_identity": str(prior_process_start_identity or ""),
        "prior_pid_death_method": str(prior_pid_death_method or ""),
        "prior_pid_death_verified_at": str(prior_pid_death_verified_at or ""),
        "certified_at": str(certified_at or ""),
    }
    required = (
        "project_id",
        "generation_id",
        "manager_started_at",
        "process_start_identity",
        "manager_start_identity",
        "lock_identity",
        "certified_at",
    )
    if values["manager_pid"] <= 0 or any(not str(values[key]).strip() for key in required):
        raise ValueError("manager generation certificate requires complete exact identity")
    if prior_pid:
        if (
            values["prior_pid_death_method"] != "esrch"
            or not values["prior_pid_death_verified_at"].strip()
        ):
            raise ValueError("prior manager PID requires exact ESRCH death proof")
    elif any(
        (
            values["prior_process_start_identity"],
            values["observed_prior_generation_id"],
            values["prior_pid_death_method"],
            values["prior_pid_death_verified_at"],
        )
    ):
        raise ValueError("prior death fields require a prior manager PID")

    existing_row = conn.execute(
        "SELECT * FROM graph_reconcile_manager_generations "
        "WHERE project_id = ? AND generation_id = ?",
        (project, values["generation_id"]),
    ).fetchone()
    if existing_row:
        existing = dict(existing_row)
        _validate_manager_generation_certificate_row(conn, existing)
        return _manager_generation_exact_replay(existing, values)

    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute(
            "SELECT * FROM graph_reconcile_manager_generations "
            "WHERE project_id = ? AND generation_id = ?",
            (project, values["generation_id"]),
        ).fetchone()
        if existing_row:
            conn.rollback()
            existing = dict(existing_row)
            _validate_manager_generation_certificate_row(conn, existing)
            return _manager_generation_exact_replay(existing, values)

        predecessor_row = conn.execute(
            "SELECT * FROM graph_reconcile_manager_generations "
            "WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
            (project,),
        ).fetchone()
        predecessor = dict(predecessor_row) if predecessor_row else {}
        predecessor_depth = 0
        if predecessor:
            predecessor_depth = _validate_manager_generation_certificate_row(
                conn, predecessor
            )
        candidate_depth = predecessor_depth + 1
        if candidate_depth > max(0, int(MANAGER_GENERATION_MAX_CHAIN_DEPTH)):
            raise ManagerGenerationCertificateConflictError(
                "manager_generation_history_too_deep", predecessor
            )
        if predecessor:
            observed_prior_generation = str(
                values["observed_prior_generation_id"] or ""
            )
            if observed_prior_generation:
                observed_prior_certificate = conn.execute(
                    "SELECT certificate_id FROM graph_reconcile_manager_generations "
                    "WHERE project_id = ? AND generation_id = ?",
                    (project, observed_prior_generation),
                ).fetchone()
                if (
                    observed_prior_certificate
                    and observed_prior_generation
                    != str(predecessor.get("generation_id") or "")
                ):
                    raise ManagerGenerationCertificateConflictError(
                        "manager_generation_predecessor_mismatch", predecessor
                    )
            elif prior_pid != int(predecessor.get("manager_pid") or 0):
                raise ManagerGenerationCertificateConflictError(
                    "manager_generation_predecessor_mismatch", predecessor
                )
        values["predecessor_certificate_id"] = str(
            predecessor.get("certificate_id") or ""
        )
        values["predecessor_generation_id"] = str(
            predecessor.get("generation_id") or ""
        )
        values["predecessor_sequence"] = int(predecessor.get("sequence") or 0)
        values["predecessor_certificate_hash"] = str(
            predecessor.get("certificate_hash") or ""
        )
        values["sequence"] = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 "
                "FROM graph_reconcile_manager_generations"
            ).fetchone()[0]
        )
        values["certificate_hash"] = _manager_generation_certificate_hash(values)
        columns = list(values)
        conn.execute(
            "INSERT INTO graph_reconcile_manager_generations "
            f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
        _manager_generation_after_insert_hook()
        row = conn.execute(
            "SELECT * FROM graph_reconcile_manager_generations "
            "WHERE certificate_id = ?",
            (values["certificate_id"],),
        ).fetchone()
        if not row:
            raise RuntimeError("manager generation certificate insert was not durable")
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if "manager_generation_identity_conflict" in str(exc):
            raise ManagerGenerationCertificateConflictError(
                "manager_generation_identity_conflict"
            ) from exc
        raise
    except Exception:
        conn.rollback()
        raise
    receipt = manager_generation_certificate_public_receipt(dict(row))
    receipt.update({"replayed": False, "writes_performed": True})
    return receipt


def current_manager_generation_certificate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    manager_start_identity: str = "",
) -> dict[str, Any]:
    """Read the latest immutable manager certificate for one project."""

    ensure_schema(conn)
    params: list[Any] = [str(project_id or "")]
    identity_filter = ""
    if manager_start_identity:
        identity_filter = " AND manager_start_identity = ?"
        params.append(str(manager_start_identity))
    row = conn.execute(
        "SELECT * FROM graph_reconcile_manager_generations "
        f"WHERE project_id = ?{identity_filter} ORDER BY sequence DESC LIMIT 1",
        params,
    ).fetchone()
    if not row:
        return {}
    certificate = dict(row)
    _validate_manager_generation_certificate_row(conn, certificate)
    return manager_generation_certificate_public_receipt(certificate)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def _table_pk_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.OperationalError:
        return []
    items: list[tuple[int, str]] = []
    for row in rows:
        pk = int(row["pk"] if hasattr(row, "keys") else row[5])
        if pk:
            items.append((pk, str(row["name"] if hasattr(row, "keys") else row[1])))
    return [name for _pk, name in sorted(items)]


def _ensure_graph_snapshot_ref_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "graph_snapshots"):
        return
    columns = _table_columns(conn, "graph_snapshots")
    if "ref_name" not in columns:
        conn.execute("ALTER TABLE graph_snapshots ADD COLUMN ref_name TEXT NOT NULL DEFAULT ''")
    if "branch_ref" not in columns:
        conn.execute("ALTER TABLE graph_snapshots ADD COLUMN branch_ref TEXT NOT NULL DEFAULT ''")


def _ensure_reconcile_terminalization_ledger_columns(
    conn: sqlite3.Connection,
) -> None:
    if not _table_exists(conn, "graph_reconcile_run_terminalizations"):
        return
    columns = _table_columns(conn, "graph_reconcile_run_terminalizations")
    if "replacement_proof_kind" not in columns:
        conn.execute(
            "ALTER TABLE graph_reconcile_run_terminalizations ADD COLUMN "
            "replacement_proof_kind TEXT NOT NULL DEFAULT 'materialized' "
            "CHECK(replacement_proof_kind IN ('materialized','manager_generation'))"
        )


def _migrate_pending_scope_reconcile_branch_identity(conn: sqlite3.Connection) -> None:
    """Upgrade pending scope rows from commit-only identity to ref/worktree identity."""
    if not _table_exists(conn, "pending_scope_reconcile"):
        return
    columns = _table_columns(conn, "pending_scope_reconcile")
    expected_columns = {"ref_name", "branch_ref", "worktree_id", "worktree_path"}
    expected_pk = ["project_id", "ref_name", "worktree_id", "commit_sha"]
    if expected_columns.issubset(columns) and _table_pk_columns(conn, "pending_scope_reconcile") == expected_pk:
        _ensure_pending_scope_reconcile_indexes(conn)
        return

    legacy_name = "pending_scope_reconcile_legacy_branch_identity"
    conn.execute("DROP TABLE IF EXISTS pending_scope_reconcile_migrated")
    conn.execute(f"DROP TABLE IF EXISTS {legacy_name}")
    conn.execute("DROP INDEX IF EXISTS idx_pending_scope_status")
    conn.execute("DROP INDEX IF EXISTS idx_pending_scope_branch")
    conn.execute(f"ALTER TABLE pending_scope_reconcile RENAME TO {legacy_name}")
    conn.execute(
        """
        CREATE TABLE pending_scope_reconcile (
          project_id TEXT NOT NULL,
          ref_name TEXT NOT NULL DEFAULT 'active',
          branch_ref TEXT NOT NULL DEFAULT '',
          worktree_id TEXT NOT NULL DEFAULT '',
          worktree_path TEXT NOT NULL DEFAULT '',
          commit_sha TEXT NOT NULL,
          parent_commit_sha TEXT NOT NULL DEFAULT '',
          queued_at TEXT NOT NULL,
          status TEXT NOT NULL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          snapshot_id TEXT NOT NULL DEFAULT '',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(project_id, ref_name, worktree_id, commit_sha)
        )
        """
    )
    legacy_columns = _table_columns(conn, legacy_name)

    def expr(column: str, default: str) -> str:
        if column in legacy_columns:
            return f"COALESCE({column}, {default})"
        return default

    conn.execute(
        f"""
        INSERT OR REPLACE INTO pending_scope_reconcile
          (project_id, ref_name, branch_ref, worktree_id, worktree_path,
           commit_sha, parent_commit_sha, queued_at, status, retry_count,
           snapshot_id, evidence_json)
        SELECT
          project_id,
          {expr('ref_name', "'active'")},
          {expr('branch_ref', "''")},
          {expr('worktree_id', "''")},
          {expr('worktree_path', "''")},
          commit_sha,
          parent_commit_sha,
          queued_at,
          status,
          retry_count,
          snapshot_id,
          evidence_json
        FROM {legacy_name}
        """
    )
    conn.execute(f"DROP TABLE {legacy_name}")
    _ensure_pending_scope_reconcile_indexes(conn)


def _ensure_pending_scope_reconcile_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_scope_status "
        "ON pending_scope_reconcile(project_id, status, ref_name, queued_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_scope_branch "
        "ON pending_scope_reconcile(project_id, branch_ref, worktree_id, commit_sha)"
    )


def normalize_pending_scope_identity(
    *,
    ref_name: str = "",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
) -> dict[str, str]:
    branch = str(branch_ref or "").strip()
    raw_path = str(worktree_path or "").strip()
    normalized_path = ""
    if raw_path:
        try:
            normalized_path = str(Path(raw_path).expanduser().resolve()).replace("\\", "/")
        except Exception:
            normalized_path = str(Path(raw_path).expanduser()).replace("\\", "/")
    wid = str(worktree_id or "").strip()
    if not wid and normalized_path:
        digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:12]
        wid = f"worktree:{digest}"
    ref = str(ref_name or "").strip()
    if not ref:
        ref = branch or wid or "active"
    return {
        "ref_name": ref,
        "branch_ref": branch,
        "worktree_id": wid,
        "worktree_path": normalized_path,
    }


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, sort_keys=True, ensure_ascii=False)


def _latest_projection_id(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    ref_name: str = "",
    branch_ref: str = "",
    schema_ready: bool = False,
) -> str:
    if not snapshot_id:
        return ""
    try:
        if schema_ready:
            filters: list[str] = []
            params: list[Any] = [project_id, snapshot_id]
            if ref_name:
                if str(ref_name or "") == "active":
                    filters.append("(ref_name = ? OR ref_name = '')")
                    params.append("active")
                else:
                    filters.append("ref_name = ?")
                    params.append(str(ref_name or ""))
            filters.append("branch_ref = ?")
            params.append(str(branch_ref or ""))
            where_extra = " AND " + " AND ".join(filters) if filters else ""
            row = conn.execute(
                f"""
                SELECT projection_id FROM graph_semantic_projections
                WHERE project_id = ? AND snapshot_id = ?{where_extra}
                ORDER BY event_watermark DESC, created_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
            return str(row["projection_id"] if row else "")
        from . import graph_events

        projection = graph_events.get_semantic_projection(
            conn,
            project_id,
            snapshot_id,
            ref_name=ref_name,
            branch_ref=branch_ref,
        )
    except Exception:
        return ""
    return str((projection or {}).get("projection_id") or "")


def record_graph_ref_event(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str,
    operation_type: str,
    old_snapshot_id: str = "",
    new_snapshot_id: str = "",
    old_commit: str = "",
    new_commit: str = "",
    old_projection_id: str = "",
    new_projection_id: str = "",
    branch_ref: str = "",
    batch_id: str = "",
    merge_queue_id: str = "",
    merge_epoch: str = "",
    rollback_epoch: str = "",
    replay_epoch: str = "",
    source_event_id: str = "",
    actor: str = "",
    evidence: dict[str, Any] | None = None,
    event_id: str = "",
    created_at: str = "",
    schema_ready: bool = False,
) -> dict[str, Any]:
    if not schema_ready:
        ensure_schema(conn)
    op = str(operation_type or "").strip()
    if op not in GRAPH_REF_OPERATION_TYPES:
        raise ValueError(f"invalid graph ref operation_type: {operation_type}")
    event = event_id or f"gref-{uuid.uuid4().hex[:16]}"
    now = created_at or utc_now()
    row = {
        "project_id": project_id,
        "event_id": event,
        "ref_name": str(ref_name or "active"),
        "branch_ref": str(branch_ref or ""),
        "batch_id": str(batch_id or ""),
        "merge_queue_id": str(merge_queue_id or ""),
        "operation_type": op,
        "old_snapshot_id": str(old_snapshot_id or ""),
        "new_snapshot_id": str(new_snapshot_id or ""),
        "old_commit": str(old_commit or ""),
        "new_commit": str(new_commit or ""),
        "old_projection_id": str(old_projection_id or ""),
        "new_projection_id": str(new_projection_id or ""),
        "merge_epoch": str(merge_epoch or ""),
        "rollback_epoch": str(rollback_epoch or ""),
        "replay_epoch": str(replay_epoch or ""),
        "source_event_id": str(source_event_id or ""),
        "actor": str(actor or ""),
        "evidence_json": _json(evidence or {}),
        "created_at": now,
    }
    conn.execute(
        """
        INSERT INTO graph_ref_events (
          project_id, event_id, ref_name, branch_ref, batch_id, merge_queue_id,
          operation_type, old_snapshot_id, new_snapshot_id, old_commit, new_commit,
          old_projection_id, new_projection_id, merge_epoch, rollback_epoch,
          replay_epoch, source_event_id, actor, evidence_json, created_at
        )
        VALUES (
          :project_id, :event_id, :ref_name, :branch_ref, :batch_id, :merge_queue_id,
          :operation_type, :old_snapshot_id, :new_snapshot_id, :old_commit, :new_commit,
          :old_projection_id, :new_projection_id, :merge_epoch, :rollback_epoch,
          :replay_epoch, :source_event_id, :actor, :evidence_json, :created_at
        )
        """,
        row,
    )
    out = dict(row)
    out["evidence"] = evidence or {}
    return out


def list_graph_ref_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "",
    operation_type: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM graph_ref_events WHERE project_id = ?"
    if ref_name:
        sql += " AND ref_name = ?"
        params.append(ref_name)
    if operation_type:
        sql += " AND operation_type = ?"
        params.append(operation_type)
    sql += " ORDER BY created_at DESC, event_id DESC LIMIT ?"
    params.append(max(1, min(500, int(limit or 50))))
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["evidence"] = _decode_json(item.get("evidence_json"), {})
        out.append(item)
    return out


def _json_list(data: Any) -> str:
    if data is None:
        return "[]"
    if isinstance(data, list):
        return json.dumps(data, sort_keys=True, ensure_ascii=False)
    return json.dumps([data], sort_keys=True, ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_id_for(snapshot_kind: str, commit_sha: str, suffix: str | None = None) -> str:
    clean_kind = (snapshot_kind or "snapshot").strip().replace("_", "-").lower()
    short = (commit_sha or "unknown").strip()[:7] or "unknown"
    tail = suffix or uuid.uuid4().hex[:4]
    return f"{clean_kind}-{short}-{tail}"


def _snapshot_root(project_id: str, snapshot_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "graph-snapshots" / snapshot_id


def snapshot_companion_dir(project_id: str, snapshot_id: str) -> Path:
    return _snapshot_root(project_id, snapshot_id)


def snapshot_graph_path(project_id: str, snapshot_id: str) -> Path:
    return snapshot_companion_dir(project_id, snapshot_id) / "graph.json"


def write_companion_files(
    project_id: str,
    snapshot_id: str,
    *,
    graph_json: dict[str, Any] | None = None,
    file_inventory: list[dict[str, Any]] | None = None,
    drift_ledger: list[dict[str, Any]] | None = None,
    created_at: str = "",
) -> dict[str, str]:
    base_dir = _snapshot_root(project_id, snapshot_id)
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        import errno as _errno
        if exc.errno == _errno.ENOSPC:
            from .stale_artifact_cleanup import cleanup_recommendation as _cr
            rec = _cr(project_id)
            raise OSError(
                exc.errno,
                (
                    f"No space left on device while creating snapshot directory for "
                    f"{project_id}/{snapshot_id}. "
                    f"Run the graph-snapshots GC to reclaim space: "
                    f"dry-run via {rec['api']['dry_run']} or MCP tool "
                    f"'{rec['mcp']['dry_run_tool']}' with dimension='graph_snapshots'."
                ),
                str(base_dir),
            ) from exc
        raise

    graph_bytes = _json(graph_json or {}).encode("utf-8")
    inventory_bytes = _json(file_inventory or []).encode("utf-8")
    drift_bytes = _json(drift_ledger or []).encode("utf-8")

    graph_sha = _sha256_bytes(graph_bytes)
    inventory_sha = _sha256_bytes(inventory_bytes)
    drift_sha = _sha256_bytes(drift_bytes)

    try:
        (base_dir / "graph.json").write_bytes(graph_bytes)
        (base_dir / "file_inventory.json").write_bytes(inventory_bytes)
        (base_dir / "drift_ledger.json").write_bytes(drift_bytes)
    except OSError as exc:
        import errno as _errno
        if exc.errno == _errno.ENOSPC:
            from .stale_artifact_cleanup import cleanup_recommendation as _cr
            rec = _cr(project_id)
            raise OSError(
                exc.errno,
                (
                    f"No space left on device while writing snapshot files for "
                    f"{project_id}/{snapshot_id}. "
                    f"Run the graph-snapshots GC to reclaim space: "
                    f"dry-run via {rec['api']['dry_run']} or MCP tool "
                    f"'{rec['mcp']['dry_run_tool']}' with dimension='graph_snapshots'."
                ),
                str(base_dir),
            ) from exc
        raise

    manifest_created_at = str(created_at or "").strip()
    manifest_path = base_dir / "manifest.json"
    if not manifest_created_at:
        try:
            existing_manifest = json.loads(manifest_path.read_bytes())
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing_manifest = {}
        if isinstance(existing_manifest, Mapping):
            manifest_created_at = str(
                existing_manifest.get("created_at") or ""
            ).strip()
    if not manifest_created_at:
        manifest_created_at = utc_now()
    manifest = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "graph_sha256": graph_sha,
        "inventory_sha256": inventory_sha,
        "drift_sha256": drift_sha,
        "created_at": manifest_created_at,
    }
    manifest_path.write_text(_json(manifest), encoding="utf-8")
    return {
        "graph_sha256": graph_sha,
        "inventory_sha256": inventory_sha,
        "drift_sha256": drift_sha,
        "path": str(base_dir),
    }


def validate_snapshot_companion_integrity(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the durable snapshot row against its exact companion bytes."""

    project_id = str(snapshot.get("project_id") or "").strip()
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    base_dir = snapshot_companion_dir(project_id, snapshot_id)
    files: dict[str, dict[str, Any]] = {}
    required = (
        ("graph", "graph.json", "graph_sha256"),
        ("inventory", "file_inventory.json", "inventory_sha256"),
        ("drift", "drift_ledger.json", "drift_sha256"),
    )
    for label, filename, hash_field in required:
        path = base_dir / filename
        expected_hash = str(snapshot.get(hash_field) or "").strip()
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return {
                "valid": False,
                "error": f"current_full_candidate_{label}_companion_missing",
                "files": files,
            }
        except OSError as exc:
            return {
                "valid": False,
                "error": f"current_full_candidate_{label}_companion_unreadable",
                "files": files,
                "os_error": type(exc).__name__,
            }
        actual_hash = _sha256_bytes(payload)
        files[label] = {
            "artifact": filename,
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "matches": actual_hash == expected_hash,
        }
        if actual_hash != expected_hash:
            return {
                "valid": False,
                "error": f"current_full_candidate_{label}_companion_hash_mismatch",
                "files": files,
            }

    manifest_path = base_dir / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except FileNotFoundError:
        return {
            "valid": False,
            "error": "current_full_candidate_manifest_missing",
            "files": files,
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "error": "current_full_candidate_manifest_invalid",
            "files": files,
            "manifest_error": type(exc).__name__,
        }
    expected_manifest = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "graph_sha256": str(snapshot.get("graph_sha256") or "").strip(),
        "inventory_sha256": str(snapshot.get("inventory_sha256") or "").strip(),
        "drift_sha256": str(snapshot.get("drift_sha256") or "").strip(),
        "created_at": str(snapshot.get("created_at") or "").strip(),
    }
    if not isinstance(manifest, Mapping) or set(manifest) != set(expected_manifest):
        actual_keys = (
            sorted(str(key) for key in manifest)
            if isinstance(manifest, Mapping)
            else []
        )
        return {
            "valid": False,
            "error": "current_full_candidate_manifest_schema_mismatch",
            "files": files,
            "manifest_missing_fields": sorted(
                set(expected_manifest) - set(actual_keys)
            ),
            "manifest_extra_fields": sorted(
                set(actual_keys) - set(expected_manifest)
            ),
        }
    mismatches = sorted(
        key
        for key, value in expected_manifest.items()
        if str(manifest.get(key) or "") != value
    )
    if mismatches:
        return {
            "valid": False,
            "error": "current_full_candidate_manifest_binding_mismatch",
            "files": files,
            "manifest_mismatch_fields": mismatches,
        }
    if manifest_bytes != _json(dict(manifest)).encode("utf-8"):
        return {
            "valid": False,
            "error": "current_full_candidate_manifest_not_canonical",
            "files": files,
        }
    return {
        "valid": True,
        "error": "",
        "files": files,
        "manifest": {"canonical": True, "bound_created_at": True},
    }


# ---------------------------------------------------------------------------
# Snapshot retention policy
# ---------------------------------------------------------------------------

DEFAULT_SNAPSHOT_KEEP_LAST_N = 10


def get_snapshot_retention_config(
    project_id: str,
    *,
    keep_last_n: int | None = None,
) -> dict[str, Any]:
    """Return effective retention config, with project-config override support.

    Follows the existing pattern of ``project_service.get_project_config_metadata``.
    The config path is ``governance.snapshot_retention.keep_last_n``.
    Falls back to ``DEFAULT_SNAPSHOT_KEEP_LAST_N`` when not configured.
    """
    effective_n = int(keep_last_n) if keep_last_n is not None else DEFAULT_SNAPSHOT_KEEP_LAST_N
    try:
        from . import project_service
        metadata = project_service.get_project_config_metadata(project_id) or {}
        governance = metadata.get("governance") if isinstance(metadata.get("governance"), dict) else {}
        retention = governance.get("snapshot_retention") if isinstance(governance.get("snapshot_retention"), dict) else {}
        configured_n = retention.get("keep_last_n")
        if configured_n is not None:
            try:
                effective_n = max(1, int(configured_n))
            except (TypeError, ValueError):
                pass
    except Exception:  # noqa: BLE001 — advisory; default is always safe
        pass
    return {
        "project_id": project_id,
        "keep_last_n": effective_n,
        "source": "project_config" if keep_last_n is None else "explicit",
    }


def _bundle_referenced_snapshot_ids() -> set[str]:
    """Return snapshot_ids that are referenced by plugin bundle manifests.

    These are treated as sealed full baselines and must never be deleted.
    """
    from .self_graph_bundle_check import SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH
    import sys as _sys
    referenced: set[str] = set()
    # Walk the installed package tree to locate bundle manifests
    pkg_root = Path(__file__).resolve().parents[2]
    candidates: list[Path] = [pkg_root / SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH]
    # Also search shared-volume for other project bundle manifests if accessible
    try:
        from .db import _governance_root
        groot = _governance_root()
        if groot.exists():
            for manifest_path in groot.rglob("self-graph-bundle-manifest.json"):
                candidates.append(manifest_path)
    except Exception:  # noqa: BLE001
        pass
    for manifest_path in candidates:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            sid = str(manifest.get("snapshot_id") or "").strip()
            if sid:
                referenced.add(sid)
        except Exception:  # noqa: BLE001
            pass
    return referenced


def select_snapshot_retention_candidates(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    keep_last_n: int | None = None,
    extra_bundle_snapshot_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Compute retention selection without performing any deletion.

    Returns:
        {
            "protected": [{"snapshot_id": ..., "reason": ..., ...}],
            "candidates": [{"snapshot_id": ..., "age_days": ..., "size_bytes": ..., ...}],
            "config": {...},
        }

    Protection rules (NEVER delete):
    1. The currently active snapshot (status=active or referenced by graph_snapshot_refs).
    2. Any snapshot whose pending_scope_reconcile row is in RUNNING or QUEUED status
       (reconcile in progress).
    3. The last N scope/full snapshots by created_at (default N=10).
    4. All snapshots referenced by plugin bundle manifests (sealed full baselines).
    5. Any snapshot with snapshot_kind='full' that is the most recent full baseline.
    """
    ensure_schema(conn)
    config = get_snapshot_retention_config(project_id, keep_last_n=keep_last_n)
    effective_n = config["keep_last_n"]

    # Collect protected snapshot ids and reasons
    protected_ids: dict[str, list[str]] = {}

    def _protect(sid: str, reason: str) -> None:
        protected_ids.setdefault(sid, []).append(reason)

    # Rule 1: active snapshot
    active_row = conn.execute(
        "SELECT snapshot_id FROM graph_snapshot_refs WHERE project_id=? AND ref_name='active'",
        (project_id,),
    ).fetchone()
    active_snapshot_id = str(active_row["snapshot_id"] if active_row else "")
    if active_snapshot_id:
        _protect(active_snapshot_id, "active_snapshot")

    # Also protect all snapshots pointed to by any ref
    ref_rows = conn.execute(
        "SELECT snapshot_id, ref_name FROM graph_snapshot_refs WHERE project_id=?",
        (project_id,),
    ).fetchall()
    for ref_row in ref_rows:
        _protect(str(ref_row["snapshot_id"]), f"ref_pointer:{ref_row['ref_name']}")

    # Rule 2: reconcile-in-progress (running/queued pending scope rows)
    in_progress_rows = conn.execute(
        """
        SELECT DISTINCT snapshot_id FROM pending_scope_reconcile
        WHERE project_id=? AND status IN (?, ?) AND snapshot_id != ''
        """,
        (project_id, PENDING_STATUS_RUNNING, PENDING_STATUS_QUEUED),
    ).fetchall()
    for row in in_progress_rows:
        sid = str(row["snapshot_id"] or "").strip()
        if sid:
            _protect(sid, "reconcile_in_progress")

    # Rule 3: most recent N scope/full snapshots (by created_at DESC)
    all_scope_full = conn.execute(
        """
        SELECT snapshot_id, snapshot_kind, created_at
        FROM graph_snapshots
        WHERE project_id=? AND snapshot_kind IN ('scope', 'full')
        ORDER BY created_at DESC, snapshot_id DESC
        """,
        (project_id,),
    ).fetchall()
    for idx, row in enumerate(all_scope_full):
        sid = str(row["snapshot_id"])
        if idx < effective_n:
            _protect(sid, f"keep_last_n:{effective_n}")

    # Rule 5: most recent full baseline is always protected (regardless of N)
    most_recent_full = conn.execute(
        """
        SELECT snapshot_id
        FROM graph_snapshots
        WHERE project_id=? AND snapshot_kind='full'
        ORDER BY created_at DESC, snapshot_id DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if most_recent_full:
        _protect(str(most_recent_full["snapshot_id"]), "most_recent_full_baseline")

    # Rule 4: bundle-referenced snapshot ids
    bundle_refs = _bundle_referenced_snapshot_ids()
    if extra_bundle_snapshot_ids:
        bundle_refs = bundle_refs | set(extra_bundle_snapshot_ids)
    for sid in bundle_refs:
        _protect(sid, "bundle_manifest_reference")

    # Gather all snapshot dirs on disk
    # Use a sentinel snapshot_id to get the parent reliably:
    # _snapshot_root(project_id, "_sentinel").parent == .../project_id/graph-snapshots
    snap_root = _snapshot_root(project_id, "_sentinel").parent
    disk_snapshot_ids: set[str] = set()
    try:
        if snap_root.exists():
            disk_snapshot_ids = {d.name for d in snap_root.iterdir() if d.is_dir()}
    except OSError:
        pass

    # Gather all snapshot ids in DB
    db_rows = conn.execute(
        "SELECT snapshot_id, snapshot_kind, status, created_at FROM graph_snapshots WHERE project_id=?",
        (project_id,),
    ).fetchall()
    db_by_id: dict[str, dict[str, Any]] = {str(row["snapshot_id"]): dict(row) for row in db_rows}

    all_ids = (disk_snapshot_ids | set(db_by_id.keys())) - set(protected_ids.keys())

    protected_list: list[dict[str, Any]] = []
    for sid, reasons in protected_ids.items():
        row = db_by_id.get(sid, {})
        dir_path = snap_root / sid
        protected_list.append({
            "snapshot_id": sid,
            "reasons": sorted(set(reasons)),
            "snapshot_kind": str(row.get("snapshot_kind") or "unknown"),
            "status": str(row.get("status") or "unknown"),
            "created_at": str(row.get("created_at") or ""),
            "dir_exists": dir_path.exists(),
        })

    candidates_list: list[dict[str, Any]] = []
    from datetime import timezone as _tz
    now_ts = datetime.now(_tz.utc)
    for sid in sorted(all_ids):
        row = db_by_id.get(sid, {})
        dir_path = snap_root / sid
        # Compute age
        age_days: float | None = None
        created_at_str = str(row.get("created_at") or "")
        if created_at_str:
            try:
                dt = datetime.strptime(created_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
                age_days = (now_ts - dt).total_seconds() / 86400
            except (ValueError, OSError):
                pass
        # Compute size
        size_bytes = 0
        if dir_path.exists():
            try:
                size_bytes = sum(f.stat().st_size for f in dir_path.rglob("*") if f.is_file())
            except OSError:
                size_bytes = 0
        candidates_list.append({
            "snapshot_id": sid,
            "snapshot_kind": str(row.get("snapshot_kind") or "unknown"),
            "status": str(row.get("status") or "disk_only"),
            "created_at": created_at_str,
            "age_days": round(age_days, 2) if age_days is not None else None,
            "size_bytes": size_bytes,
            "dir_exists": dir_path.exists(),
            "in_db": sid in db_by_id,
        })

    return {
        "project_id": project_id,
        "config": config,
        "protected_count": len(protected_list),
        "candidate_count": len(candidates_list),
        "protected": protected_list,
        "candidates": candidates_list,
    }


def run_snapshot_retention_gc(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    keep_last_n: int | None = None,
    dry_run: bool = True,
    actor: str = "retention_gc",
    extra_bundle_snapshot_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Run retention GC on graph-snapshot companion dirs.

    Always honours protection rules; never deletes active/in-progress/sealed-baseline.
    Pass dry_run=True (the default) to compute candidates without deleting.
    Returns a dict with deleted_dirs, freed_bytes, candidates, and errors.
    """
    import shutil as _shutil
    selection = select_snapshot_retention_candidates(
        conn,
        project_id,
        keep_last_n=keep_last_n,
        extra_bundle_snapshot_ids=extra_bundle_snapshot_ids,
    )
    snap_root = _snapshot_root(project_id, "_sentinel").parent

    deleted_dirs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    freed_bytes = 0

    for candidate in selection["candidates"]:
        sid = str(candidate["snapshot_id"])
        dir_path = snap_root / sid
        size_bytes = int(candidate.get("size_bytes") or 0)
        if dry_run:
            deleted_dirs.append({
                "snapshot_id": sid,
                "path": str(dir_path),
                "size_bytes": size_bytes,
                "dry_run": True,
            })
            freed_bytes += size_bytes
            continue
        if not dir_path.exists():
            continue
        try:
            _shutil.rmtree(dir_path)
            deleted_dirs.append({
                "snapshot_id": sid,
                "path": str(dir_path),
                "size_bytes": size_bytes,
                "dry_run": False,
            })
            freed_bytes += size_bytes
        except OSError as exc:
            errors.append({
                "snapshot_id": sid,
                "path": str(dir_path),
                "error": str(exc),
            })

    return {
        "ok": len(errors) == 0,
        "project_id": project_id,
        "dry_run": dry_run,
        "actor": actor,
        "config": selection["config"],
        "protected_count": selection["protected_count"],
        "candidate_count": selection["candidate_count"],
        "deleted_count": len(deleted_dirs),
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "deleted_dirs": deleted_dirs,
        "errors": errors,
        "protected": selection["protected"],
        "candidates": selection["candidates"],
    }


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        return [n for n in deps.get("nodes", []) if isinstance(n, dict)]
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else []
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    if isinstance(nodes, dict):
        result = []
        for node_id, node in nodes.items():
            item = dict(node) if isinstance(node, dict) else {}
            item.setdefault("id", str(node_id))
            result.append(item)
        return result
    return []


def graph_payload_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized hierarchy/evidence/dependency edges from a graph payload."""
    if not isinstance(graph_json, dict):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    sections = [
        ("hierarchy_graph", "hierarchy"),
        ("evidence_graph", "evidence"),
        ("deps_graph", "dependency"),
        ("gates_graph", "gate"),
    ]
    for section_name, default_direction in sections:
        section = graph_json.get(section_name)
        if not isinstance(section, dict):
            continue
        raw_edges = section.get("edges") if "edges" in section else section.get("links")
        for edge in raw_edges or []:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("src") or edge.get("source") or "")
            dst = str(edge.get("dst") or edge.get("target") or "")
            edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on")
            direction = str(edge.get("direction") or default_direction)
            if not src or not dst:
                continue
            key = (src, dst, edge_type, direction)
            if key in seen:
                continue
            item = dict(edge)
            item["src"] = src
            item["dst"] = dst
            item["edge_type"] = edge_type
            item["direction"] = direction
            evidence = item.get("evidence")
            metadata = item.get("metadata")
            if metadata and "evidence" not in item:
                item["evidence"] = metadata
            elif evidence and "metadata" not in item:
                item.setdefault("metadata", {"evidence": evidence})
            item.setdefault("section", section_name)
            result.append(item)
            seen.add(key)
    if result:
        return result
    edges = graph_json.get("edges") if isinstance(graph_json, dict) else []
    if isinstance(edges, list):
        return [e for e in edges if isinstance(e, dict)]
    return []


def _graph_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    return graph_payload_edges(graph_json)


def graph_payload_stats(graph_json: dict[str, Any]) -> dict[str, int]:
    return {"nodes": len(_graph_nodes(graph_json)), "edges": len(_graph_edges(graph_json))}


def _decode_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return default
    return default


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    if key not in row.keys():
        return default
    return row[key]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _compact_pending_scope_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence = _decode_json(row.get("evidence_json"), {})
    return {
        "ref_name": str(row.get("ref_name") or ""),
        "branch_ref": str(row.get("branch_ref") or ""),
        "worktree_id": str(row.get("worktree_id") or ""),
        "commit_sha": str(row.get("commit_sha") or ""),
        "status": str(row.get("status") or ""),
        "snapshot_id": str(row.get("snapshot_id") or ""),
        "evidence": evidence,
    }


def _source_matches_ref_event(source: str, event: dict[str, Any]) -> bool:
    source_value = str(source or "").strip()
    if not source_value:
        return False
    return source_value in {
        str(event.get("event_id") or ""),
        str(event.get("merge_epoch") or ""),
        str(event.get("new_snapshot_id") or ""),
        str(event.get("new_commit") or ""),
    }


def _rollback_source_values(event: dict[str, Any]) -> set[str]:
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    sources = {str(event.get("source_event_id") or "").strip()}
    for key in ("abandoned_event_ids", "abandoned_merge_epochs", "abandoned_snapshot_ids"):
        values = evidence.get(key)
        if isinstance(values, list):
            sources.update(str(value or "").strip() for value in values)
        elif values:
            sources.add(str(values).strip())
    return {source for source in sources if source}


def _projection_rows_by_id(
    conn: sqlite3.Connection,
    project_id: str,
    projection_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    ids = sorted({str(pid or "").strip() for pid in projection_ids if str(pid or "").strip()})
    if not ids or not _table_exists(conn, "graph_semantic_projections"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM graph_semantic_projections
        WHERE project_id = ? AND projection_id IN ({placeholders})
        """,
        (project_id, *ids),
    ).fetchall()
    return {str(row["projection_id"]): dict(row) for row in rows}


def _semantic_job_rows_for_snapshots(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_ids: set[str],
) -> list[dict[str, Any]]:
    if not snapshot_ids or not _table_exists(conn, "graph_semantic_jobs"):
        return []
    ids = sorted(snapshot_ids)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id IN ({placeholders})
        ORDER BY snapshot_id, node_id
        """,
        (project_id, *ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _semantic_job_rollback_disposition(
    row: dict[str, Any],
    *,
    active_snapshot_id: str,
    abandoned_snapshot_ids: set[str],
    branch_candidate_snapshot_ids: set[str],
) -> str:
    snapshot_id = str(row.get("snapshot_id") or "")
    status = str(row.get("status") or "").strip().lower()
    if snapshot_id in abandoned_snapshot_ids:
        return "cancelled_by_rollback" if status in {"cancelled", "canceled"} else "abandoned"
    if snapshot_id == active_snapshot_id:
        return "current"
    if snapshot_id in branch_candidate_snapshot_ids or row.get("branch_ref"):
        return "candidate"
    return "historical"


def build_graph_rollback_epoch_state(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "active",
    rollback_epoch: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Return compact PB-005/PB-011 graph rollback state for operators/tests."""
    ensure_schema(conn)
    bounded_limit = max(1, min(500, int(limit or 50)))
    ref = normalize_pending_scope_identity(ref_name=ref_name)["ref_name"]
    ref_row = conn.execute(
        """
        SELECT snapshot_id, commit_sha
        FROM graph_snapshot_refs
        WHERE project_id = ? AND ref_name = ?
        """,
        (project_id, ref),
    ).fetchone()
    all_events = list_graph_ref_events(conn, project_id, limit=bounded_limit)
    active_snapshot_id = str(ref_row["snapshot_id"] if ref_row else "")
    target_events = [event for event in all_events if str(event.get("ref_name") or "") == ref]
    rollback_events = [
        event for event in target_events
        if event.get("operation_type") == "rollback"
        and (not rollback_epoch or event.get("rollback_epoch") == rollback_epoch)
    ]
    active_event = next(
        (
            event for event in target_events
            if str(event.get("new_snapshot_id") or "") == active_snapshot_id
        ),
        target_events[0] if target_events else {},
    )
    rollback_event = rollback_events[0] if rollback_events else {}
    abandoned_sources = _rollback_source_values(rollback_event)
    abandoned_merge_events: list[dict[str, Any]] = []
    for event in target_events:
        if event.get("operation_type") != "merge":
            continue
        explicit_match = any(_source_matches_ref_event(source, event) for source in abandoned_sources)
        same_batch = (
            rollback_event
            and event.get("batch_id")
            and event.get("batch_id") == rollback_event.get("batch_id")
        )
        if explicit_match or (not abandoned_sources and same_batch):
            abandoned_merge_events.append(event)

    abandoned_event_ids = {str(event.get("event_id") or "") for event in abandoned_merge_events}
    abandoned_merge_epochs = {
        str(event.get("merge_epoch") or "")
        for event in abandoned_merge_events
        if event.get("merge_epoch")
    }
    branch_candidate_events = [
        event for event in all_events
        if str(event.get("ref_name") or "") != ref
    ]
    abandoned_snapshot_ids = {
        str(event.get("new_snapshot_id") or "")
        for event in abandoned_merge_events
        if event.get("new_snapshot_id")
    }
    branch_candidate_snapshot_ids = {
        str(event.get("new_snapshot_id") or "")
        for event in branch_candidate_events
        if event.get("new_snapshot_id")
    }
    projection_ids: set[str] = set()
    event_snapshot_ids: set[str] = {active_snapshot_id} if active_snapshot_id else set()
    for event in all_events:
        snapshot_id = str(event.get("new_snapshot_id") or "").strip()
        if snapshot_id:
            event_snapshot_ids.add(snapshot_id)
        for key in ("old_projection_id", "new_projection_id"):
            value = str(event.get(key) or "").strip()
            if value:
                projection_ids.add(value)
    projection_rows = _projection_rows_by_id(conn, project_id, projection_ids)
    projection_states: list[dict[str, Any]] = []
    seen_projection_ids: set[str] = set()
    for event in all_events:
        projection_id = str(event.get("new_projection_id") or "").strip()
        if not projection_id or projection_id in seen_projection_ids:
            continue
        seen_projection_ids.add(projection_id)
        projection_row = projection_rows.get(projection_id, {})
        event_id = str(event.get("event_id") or "")
        event_ref = str(event.get("ref_name") or "")
        event_branch = str(event.get("branch_ref") or "")
        if event_id in abandoned_event_ids or str(event.get("merge_epoch") or "") in abandoned_merge_epochs:
            status = "abandoned"
        elif event_id == str(active_event.get("event_id") or "") and event_ref == ref:
            status = "current"
        elif event_ref != ref or event_branch:
            status = "candidate"
        else:
            status = "historical"
        projection_states.append({
            "projection_id": projection_id,
            "snapshot_id": str(event.get("new_snapshot_id") or ""),
            "ref_name": event_ref,
            "branch_ref": event_branch,
            "operation_type": str(event.get("operation_type") or ""),
            "event_id": event_id,
            "merge_epoch": str(event.get("merge_epoch") or ""),
            "rollback_epoch": str(event.get("rollback_epoch") or ""),
            "status": status,
            "base_commit": str(projection_row.get("base_commit") or event.get("new_commit") or ""),
        })

    all_pending_scope_rows = list_pending_scope_reconcile(conn, project_id)
    pending_rows = [
        _compact_pending_scope_row(row)
        for row in all_pending_scope_rows
    ][:bounded_limit]
    semantic_jobs = []
    for row in _semantic_job_rows_for_snapshots(conn, project_id, event_snapshot_ids):
        semantic_jobs.append({
            "snapshot_id": str(row.get("snapshot_id") or ""),
            "node_id": str(row.get("node_id") or ""),
            "status": str(row.get("status") or ""),
            "branch_ref": str(row.get("branch_ref") or ""),
            "operation_type": str(row.get("operation_type") or ""),
            "feature_hash": str(row.get("feature_hash") or ""),
            "attempt_count": int(row.get("attempt_count") or 0),
            "worker_id": str(row.get("worker_id") or ""),
            "claim_id": str(row.get("claim_id") or ""),
            "lease_expires_at": str(row.get("lease_expires_at") or ""),
            "last_error": str(row.get("last_error") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "rollback_disposition": _semantic_job_rollback_disposition(
                row,
                active_snapshot_id=active_snapshot_id,
                abandoned_snapshot_ids=abandoned_snapshot_ids,
                branch_candidate_snapshot_ids=branch_candidate_snapshot_ids,
            ),
        })
    return {
        "ok": True,
        "project_id": project_id,
        "ref_name": ref,
        "rollback_epoch": rollback_epoch or str(rollback_event.get("rollback_epoch") or ""),
        "active": {
            "snapshot_id": active_snapshot_id,
            "commit_sha": str(ref_row["commit_sha"] if ref_row else ""),
            "event_id": str(active_event.get("event_id") or ""),
            "operation_type": str(active_event.get("operation_type") or ""),
            "projection_id": str(active_event.get("new_projection_id") or ""),
        },
        "rollback_event": rollback_event,
        "abandoned_merge_epochs": sorted(abandoned_merge_epochs),
        "abandoned_merge_events": abandoned_merge_events[:bounded_limit],
        "branch_candidates": branch_candidate_events[:bounded_limit],
        "projection_states": projection_states[:bounded_limit],
        "pending_scope": pending_rows,
        "semantic_jobs": semantic_jobs[:bounded_limit],
        "total_counts": {
            "ref_events": len(all_events),
            "abandoned_merge_events": len(abandoned_merge_events),
            "branch_candidates": len(branch_candidate_events),
            "projection_states": len(projection_states),
            "pending_scope": len(all_pending_scope_rows),
            "semantic_jobs": len(semantic_jobs),
        },
        "truncated": {
            "ref_events": len(all_events) >= bounded_limit,
            "abandoned_merge_events": len(abandoned_merge_events) > bounded_limit,
            "branch_candidates": len(branch_candidate_events) > bounded_limit,
            "projection_states": len(projection_states) > bounded_limit,
            "pending_scope": len(all_pending_scope_rows) > bounded_limit,
            "semantic_jobs": len(semantic_jobs) > bounded_limit,
        },
    }


def invalidate_semantic_jobs_for_rollback_epoch(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "active",
    rollback_epoch: str,
    actor: str = "observer",
    now: str = "",
) -> dict[str, Any]:
    """Cancel open semantic jobs tied to merge snapshots abandoned by rollback."""
    ensure_schema(conn)
    if not _table_exists(conn, "graph_semantic_jobs"):
        return {
            "ok": True,
            "project_id": project_id,
            "rollback_epoch": rollback_epoch,
            "matched_count": 0,
            "invalidated_count": 0,
            "semantic_jobs": [],
        }
    state = build_graph_rollback_epoch_state(
        conn,
        project_id,
        ref_name=ref_name,
        rollback_epoch=rollback_epoch,
        limit=500,
    )
    abandoned_snapshot_ids = {
        str(event.get("new_snapshot_id") or "")
        for event in state.get("abandoned_merge_events", [])
        if event.get("new_snapshot_id")
    }
    if not abandoned_snapshot_ids:
        return {
            "ok": True,
            "project_id": project_id,
            "rollback_epoch": rollback_epoch,
            "matched_count": 0,
            "invalidated_count": 0,
            "semantic_jobs": [],
        }
    rows = _semantic_job_rows_for_snapshots(conn, project_id, abandoned_snapshot_ids)
    terminal = {
        "cancelled",
        "canceled",
        "failed",
        "ai_complete",
        "complete",
        "rule_complete",
    }
    open_rows = [
        row for row in rows
        if str(row.get("status") or "").strip().lower() not in terminal
    ]
    stamp = now or utc_now()
    if open_rows:
        placeholders = ",".join("(?, ?, ?)" for _ in open_rows)
        params: list[Any] = []
        for row in open_rows:
            params.extend([project_id, row["snapshot_id"], row["node_id"]])
        reason = f"invalidated by rollback_epoch {rollback_epoch} ({actor})"
        conn.execute(
            f"""
            UPDATE graph_semantic_jobs
            SET status = 'cancelled',
                operation_type = 'rollback_invalidated',
                worker_id = '',
                claim_id = '',
                claimed_at = '',
                lease_expires_at = '',
                claimed_by = '',
                last_error = ?,
                updated_at = ?
            WHERE (project_id, snapshot_id, node_id) IN ({placeholders})
            """,
            (reason, stamp, *params),
        )
    return {
        "ok": True,
        "project_id": project_id,
        "rollback_epoch": rollback_epoch,
        "matched_count": len(rows),
        "invalidated_count": len(open_rows),
        "semantic_jobs": _semantic_job_rows_for_snapshots(conn, project_id, abandoned_snapshot_ids),
    }


def _semantic_hash_state(status: str, feature_hash: str, payload: dict[str, Any]) -> str:
    status_norm = str(status or "").strip().lower()
    validation = payload.get("semantic_state_validation")
    if isinstance(validation, dict):
        validation_status = str(validation.get("status") or "").lower()
        if validation_status in {"stale_hash_mismatch", "hash_mismatch", "stale"}:
            return "stale"
        if validation.get("valid") is True:
            return "current"
        if validation.get("valid") is False:
            return "stale"

    flags = payload.get("quality_flags")
    if isinstance(flags, list):
        flag_set = {str(flag or "").strip().lower() for flag in flags}
        if flag_set.intersection({"semantic_hash_mismatch", "source_hash_changed", "semantic_stale"}):
            return "stale"

    if status_norm in {"pending_review", "review_pending"}:
        return "pending"
    if status_norm in {"ai_complete", "semantic_graph_state", "reviewed"} and feature_hash:
        return "current"
    if status_norm in {"pending_ai", "ai_pending", "running", "ai_running"}:
        return "pending"
    if status_norm in {"ai_failed", "failed"}:
        return "failed"
    return "unknown"


def _semantic_overlay_from_node_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _decode_json(_row_value(row, "semantic_json", ""), {})
    if not isinstance(payload, dict):
        payload = {}
    file_hashes = _decode_json(_row_value(row, "semantic_file_hashes_json", ""), {})
    if not isinstance(file_hashes, dict):
        file_hashes = {}

    node_status = str(_row_value(row, "semantic_status", "") or "")
    job_status = str(_row_value(row, "semantic_job_status", "") or "")
    job_status_norm = job_status.lower()
    payload_status = str(payload.get("status") or "")
    status = node_status or payload_status or "structure_only"
    if not node_status and not payload_status and job_status_norm in {
        "pending_ai",
        "ai_pending",
        "running",
        "ai_running",
        "ai_failed",
        "failed",
        "cancelled",
        "canceled",
        "rejected",
    }:
        status = job_status
    api_status = "review_pending" if status == "pending_review" else status
    feature_hash = str(
        _row_value(row, "semantic_feature_hash", "")
        or payload.get("feature_hash")
        or ""
    )
    updated_at = str(
        _row_value(row, "semantic_updated_at", "")
        or _row_value(row, "semantic_job_updated_at", "")
        or payload.get("updated_at")
        or ""
    )

    overlay = dict(payload)
    overlay.update({
        "status": api_status,
        "node_status": node_status,
        "job_status": job_status,
        "feature_hash": feature_hash,
        "file_hashes": file_hashes,
        "feedback_round": _row_value(row, "semantic_feedback_round", payload.get("feedback_round", 0)) or 0,
        "batch_index": _row_value(row, "semantic_batch_index", payload.get("batch_index")),
        "updated_at": updated_at,
        "hash_state": _semantic_hash_state(status, feature_hash, payload),
        "has_semantic_payload": bool(node_status and payload),
    })

    if job_status:
        overlay["job"] = {
            "status": job_status,
            "feature_hash": str(_row_value(row, "semantic_job_feature_hash", "") or ""),
            "attempt_count": int(_row_value(row, "semantic_job_attempt_count", 0) or 0),
            "last_error": str(_row_value(row, "semantic_job_last_error", "") or ""),
            "worker_id": str(_row_value(row, "semantic_job_worker_id", "") or ""),
            "claim_id": str(_row_value(row, "semantic_job_claim_id", "") or ""),
            "claimed_at": str(_row_value(row, "semantic_job_claimed_at", "") or ""),
            "lease_expires_at": str(_row_value(row, "semantic_job_lease_expires_at", "") or ""),
            "claimed_by": str(_row_value(row, "semantic_job_claimed_by", "") or ""),
            "updated_at": str(_row_value(row, "semantic_job_updated_at", "") or ""),
        }
    return overlay


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_artifact(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload if payload is not None else default


def _current_graph_path(project_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "graph.json"


def _baseline_graph_path(project_id: str, baseline_id: int) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "baselines" / str(baseline_id) / "graph.json"


def _resolve_import_commit(conn: sqlite3.Connection, project_id: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    try:
        row = conn.execute(
            "SELECT chain_version, git_head FROM project_version WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        # chain_version is the last governed/service version; git_head may include
        # advisory MF commits that the graph has not materialized yet.
        if hasattr(row, "keys"):
            chain_version = row["chain_version"] if "chain_version" in row.keys() else ""
            git_head = row["git_head"] if "git_head" in row.keys() else ""
        else:
            chain_version = row[0] if len(row) > 0 else ""
            git_head = row[1] if len(row) > 1 else ""
        return chain_version or git_head or "unknown"
    return "unknown"


def select_existing_graph_source(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    extra_graph_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any] | None:
    """Select the best existing graph payload to import.

    Empty baseline companion graphs are skipped because older scan-only
    baselines often wrote `{}` while the active graph still lived at the
    shared-volume graph path.
    """
    ensure_schema(conn)
    try:
        rows = conn.execute(
            """
            SELECT baseline_id FROM version_baselines
            WHERE project_id = ?
            ORDER BY baseline_id DESC
            """,
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if rows:
        from .baseline_service import read_companion_file

        for row in rows:
            baseline_id = row["baseline_id"] if hasattr(row, "keys") else row[0]
            try:
                graph_json = read_companion_file(project_id, int(baseline_id), "graph.json")
            except Exception:
                continue
            stats = graph_payload_stats(graph_json)
            if stats["nodes"] > 0:
                path = _baseline_graph_path(project_id, int(baseline_id))
                return {
                    "source_kind": "baseline_companion",
                    "source_path": str(path),
                    "source_ref": str(baseline_id),
                    "graph_json": graph_json,
                    "stats": stats,
                }

    candidates: list[tuple[str, Path]] = [("shared_volume_current", _current_graph_path(project_id))]
    for path in extra_graph_paths or []:
        candidates.append(("explicit_path", Path(path)))

    for source_kind, path in candidates:
        if not path.exists():
            continue
        graph_json = _read_json_file(path)
        stats = graph_payload_stats(graph_json)
        if stats["nodes"] > 0:
            return {
                "source_kind": source_kind,
                "source_path": str(path),
                "source_ref": "",
                "graph_json": graph_json,
                "stats": stats,
            }
    return None


def create_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    snapshot_kind: str,
    snapshot_id: str | None = None,
    parent_snapshot_id: str = "",
    ref_name: str = "",
    branch_ref: str = "",
    graph_json: dict[str, Any] | None = None,
    file_inventory: list[dict[str, Any]] | None = None,
    drift_ledger: list[dict[str, Any]] | None = None,
    status: str = SNAPSHOT_STATUS_CANDIDATE,
    created_by: str = "",
    notes: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    if status not in ALLOWED_SNAPSHOT_STATUSES:
        raise ValueError(f"invalid graph snapshot status: {status}")
    sid = snapshot_id or snapshot_id_for(snapshot_kind, commit_sha)
    ref_value = str(ref_name or "").strip()
    branch_value = str(branch_ref or "").strip()
    if not ref_value and branch_value:
        ref_value = branch_value
    if ref_value == "active" and not branch_value:
        ref_value = ""
    now = utc_now()
    shas = write_companion_files(
        project_id,
        sid,
        graph_json=graph_json,
        file_inventory=file_inventory,
        drift_ledger=drift_ledger,
        created_at=now,
    )
    conn.execute(
        """
        INSERT INTO graph_snapshots
          (project_id, snapshot_id, commit_sha, parent_snapshot_id, snapshot_kind,
           ref_name, branch_ref, graph_sha256, inventory_sha256, drift_sha256, status, created_at,
           created_by, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            sid,
            commit_sha,
            parent_snapshot_id,
            snapshot_kind,
            ref_value,
            branch_value,
            shas["graph_sha256"],
            shas["inventory_sha256"],
            shas["drift_sha256"],
            status,
            now,
            created_by,
            notes,
        ),
    )
    return {
        "project_id": project_id,
        "snapshot_id": sid,
        "commit_sha": commit_sha,
        "snapshot_kind": snapshot_kind,
        "ref_name": ref_value,
        "branch_ref": branch_value,
        "status": status,
        "created_at": now,
        "path": shas["path"],
        "graph_sha256": shas["graph_sha256"],
        "inventory_sha256": shas["inventory_sha256"],
        "drift_sha256": shas["drift_sha256"],
    }


def index_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    nodes: Iterable[dict[str, Any]] | None = None,
    edges: Iterable[dict[str, Any]] | None = None,
) -> dict[str, int]:
    ensure_schema(conn)
    node_count = 0
    for node in nodes or []:
        node_id = str(node.get("id") or node.get("node_id") or "")
        if not node_id:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO graph_nodes_index
              (project_id, snapshot_id, node_id, layer, title, kind,
               primary_files_json, secondary_files_json, test_files_json, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                snapshot_id,
                node_id,
                str(node.get("layer") or ""),
                str(node.get("title") or ""),
                str(node.get("kind") or node.get("metadata", {}).get("kind") or ""),
                _json_list(node.get("primary") or node.get("primary_files")),
                _json_list(node.get("secondary") or node.get("secondary_files")),
                _json_list(node.get("test") or node.get("test_files")),
                _json(node.get("metadata") or {}),
            ),
        )
        node_count += 1

    edge_count = 0
    for edge in edges or []:
        src = str(edge.get("src") or edge.get("source") or "")
        dst = str(edge.get("dst") or edge.get("target") or "")
        edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on")
        direction = str(edge.get("direction") or "dependency")
        if not src or not dst:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO graph_edges_index
              (project_id, snapshot_id, src, dst, edge_type, direction, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                snapshot_id,
                src,
                dst,
                edge_type,
                direction,
                _json(edge.get("evidence") or edge.get("evidence_json") or {}),
            ),
        )
        edge_count += 1
    return {"nodes": node_count, "edges": edge_count}


def activate_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    expected_old_snapshot_id: str | None = None,
    ref_name: str = "active",
    operation_type: str = "activate",
    branch_ref: str = "",
    batch_id: str = "",
    merge_queue_id: str = "",
    merge_epoch: str = "",
    rollback_epoch: str = "",
    replay_epoch: str = "",
    source_event_id: str = "",
    evidence: dict[str, Any] | None = None,
    actor: str = "activate_hook",
    auto_rebuild_projection: bool = True,
    schema_ready: bool = False,
    post_commit_hooks: bool = True,
) -> dict[str, Any]:
    if not schema_ready:
        ensure_schema(conn)
    # This must precede every ref/projection/event write below.  In particular,
    # direct in-process callers cannot bypass the HTTP dev-plane guard by
    # supplying a stable-looking argument or changing an environment value.
    _require_active_graph_activation_for_connection(conn)
    if schema_ready and auto_rebuild_projection:
        raise ValueError(
            "transaction-safe graph activation requires auto_rebuild_projection=false"
        )
    ref_name = normalize_pending_scope_identity(ref_name=ref_name)["ref_name"]
    op = str(operation_type or "").strip()
    if op not in GRAPH_REF_OPERATION_TYPES:
        raise ValueError(f"invalid graph ref operation_type: {operation_type}")
    activation_branch_ref = str(branch_ref or "").strip()
    if not activation_branch_ref and ref_name != "active":
        activation_branch_ref = ref_name
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    snapshot = dict(row)
    target_ref_activation = ref_name == "active"
    snapshot_ref_identity = str(snapshot.get("ref_name") or "").strip()
    snapshot_branch_identity = str(snapshot.get("branch_ref") or "").strip()
    snapshot_is_branch_candidate = bool(
        snapshot_branch_identity or (snapshot_ref_identity and snapshot_ref_identity != "active")
    )
    if target_ref_activation and snapshot_is_branch_candidate:
        raise ValueError(
            "branch graph candidate cannot be activated as active target graph truth; "
            "merge to the target ref and run target-ref scope reconcile first"
        )
    old = conn.execute(
        "SELECT snapshot_id, commit_sha FROM graph_snapshot_refs WHERE project_id = ? AND ref_name = ?",
        (project_id, ref_name),
    ).fetchone()
    old_id = old["snapshot_id"] if old else ""
    old_commit = old["commit_sha"] if old else ""
    if expected_old_snapshot_id is not None and old_id != expected_old_snapshot_id:
        raise GraphSnapshotConflictError(
            f"active snapshot changed: expected {expected_old_snapshot_id!r}, got {old_id!r}"
        )
    old_projection_id = _latest_projection_id(
        conn,
        project_id,
        old_id,
        ref_name=ref_name,
        branch_ref=activation_branch_ref,
        schema_ready=schema_ready,
    )

    now = utc_now()
    conn.execute(
        """
        INSERT INTO graph_snapshot_refs(project_id, ref_name, snapshot_id, commit_sha, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_id, ref_name) DO UPDATE SET
          snapshot_id = excluded.snapshot_id,
          commit_sha = excluded.commit_sha,
          updated_at = excluded.updated_at
        """,
        (project_id, ref_name, snapshot_id, snapshot["commit_sha"], now),
    )
    if ref_name != "active" or activation_branch_ref:
        conn.execute(
            """
            UPDATE graph_snapshots
            SET ref_name = CASE WHEN ref_name = '' THEN ? ELSE ref_name END,
                branch_ref = CASE WHEN branch_ref = '' THEN ? ELSE branch_ref END
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (
                "" if ref_name == "active" and not activation_branch_ref else ref_name,
                activation_branch_ref,
                project_id,
                snapshot_id,
            ),
        )
    if target_ref_activation:
        conn.execute(
            "UPDATE graph_snapshots SET status = ? WHERE project_id = ? AND snapshot_id = ?",
            (SNAPSHOT_STATUS_ACTIVE, project_id, snapshot_id),
        )
    if target_ref_activation and old_id and old_id != snapshot_id:
        conn.execute(
            "UPDATE graph_snapshots SET status = ? WHERE project_id = ? AND snapshot_id = ?",
            (SNAPSHOT_STATUS_SUPERSEDED, project_id, old_id),
        )
    result: dict[str, Any] = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "previous_snapshot_id": old_id,
        "ref_name": ref_name,
        "candidate_ref_update": not target_ref_activation,
    }

    # MF-2026-05-10-012: dashboard derives feature counters from
    # graph_semantic_projections (per-snapshot cache), not from raw events.
    # Reconcile and admin recovery can both leave a freshly created snapshot
    # without a projection, which manifests as "Node semantic 0/0" the moment
    # it becomes active. Auto-rebuild on activate is idempotent — if the
    # target snapshot already has a projection, skip. If projection rebuild
    # fails (advisory only), we still report the activation as successful.
    projection_status = "skipped"
    if auto_rebuild_projection and ref_name == "active":
        try:
            from . import graph_events  # local import to avoid module cycle

            existing = graph_events.get_semantic_projection(
                conn,
                project_id,
                snapshot_id,
                ref_name=ref_name,
                branch_ref=activation_branch_ref,
            )
            if not existing or existing.get("status") in (None, "", "missing"):
                graph_events.materialize_events(conn, project_id, snapshot_id, actor=actor)
                graph_events.build_semantic_projection(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=actor,
                    ref_name=ref_name,
                    branch_ref=activation_branch_ref,
                )
                projection_status = "rebuilt"
            else:
                projection_status = "already_present"
        except Exception as exc:  # noqa: BLE001 - advisory; activation already committed
            projection_status = f"rebuild_failed: {exc}"
    result["projection_status"] = projection_status
    new_projection_id = _latest_projection_id(
        conn,
        project_id,
        snapshot_id,
        ref_name=ref_name,
        branch_ref=activation_branch_ref,
        schema_ready=schema_ready,
    )
    try:
        ref_event = record_graph_ref_event(
            conn,
            project_id,
            ref_name=ref_name,
            operation_type=op,
            old_snapshot_id=old_id,
            new_snapshot_id=snapshot_id,
            old_commit=old_commit,
            new_commit=str(snapshot["commit_sha"] or ""),
            old_projection_id=old_projection_id,
            new_projection_id=new_projection_id,
            branch_ref=activation_branch_ref,
            batch_id=batch_id,
            merge_queue_id=merge_queue_id,
            merge_epoch=merge_epoch,
            rollback_epoch=rollback_epoch,
            replay_epoch=replay_epoch,
            source_event_id=source_event_id,
            actor=actor,
            evidence={
                "source": "activate_graph_snapshot",
                "projection_status": projection_status,
                **(evidence or {}),
            },
            schema_ready=schema_ready,
        )
        result["graph_ref_event_id"] = ref_event["event_id"]
        result["old_projection_id"] = old_projection_id
        result["new_projection_id"] = new_projection_id
    except ValueError:
        raise
    # MF 2026-05-11: snapshot activation is an in-process hook (no HTTP),
    # so _emit_dashboard_changed never fires for it. Publish here so the
    # dashboard's SSE subscribers refetch when a new snapshot becomes
    # active (reconcile / pending-scope materialize, etc.).
    if target_ref_activation and post_commit_hooks:
        try:
            from . import event_bus
            event_bus.publish("snapshot.activated", {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "previous_snapshot_id": old_id,
                "commit_sha": snapshot["commit_sha"],
                "ref_name": ref_name,
                "projection_status": projection_status,
                "graph_ref_event_id": result.get("graph_ref_event_id", ""),
                "source": "activate_graph_snapshot",
            })
            event_bus.publish("dashboard.changed", {
                "project_id": project_id,
                "path": "/internal/snapshot/activate",
                "method": "WORKER",
                "source": "activate_graph_snapshot",
            })
        except Exception:  # noqa: BLE001 - advisory
            pass
    # Post-activation GC: run retention GC automatically after a successful
    # target-ref activation. This is advisory-only; never blocks activation.
    gc_result: dict[str, Any] | None = None
    if target_ref_activation and post_commit_hooks:
        try:
            gc_result = run_snapshot_retention_gc(
                conn,
                project_id,
                dry_run=False,
                actor=f"post_activate:{actor}",
            )
        except Exception as exc:  # noqa: BLE001 - advisory; activation already done
            gc_result = {"ok": False, "error": str(exc), "dry_run": False}
    result["retention_gc"] = gc_result
    return result


def finalize_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    target_commit_sha: str = "",
    expected_old_snapshot_id: str | None = None,
    ref_name: str = "active",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
    actor: str = "observer",
    materialize_pending: bool = True,
    covered_commit_shas: Iterable[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Activate a candidate graph snapshot and settle matching pending scope rows.

    This is the explicit signoff bridge from a state-only reconcile candidate to
    the active graph ref. It performs the same compare-and-swap check as
    ``activate_graph_snapshot`` and only marks pending rows for the exact
    snapshot commit as materialized.
    """
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    snapshot = dict(row)
    status = str(snapshot.get("status") or "")
    if status not in {
        SNAPSHOT_STATUS_CANDIDATE,
        SNAPSHOT_STATUS_FINALIZING,
        SNAPSHOT_STATUS_ACTIVE,
    }:
        raise ValueError(f"cannot finalize graph snapshot in status {status!r}")
    commit_sha = str(snapshot.get("commit_sha") or "")
    if target_commit_sha and commit_sha != target_commit_sha:
        raise ValueError(
            f"snapshot commit mismatch: expected {target_commit_sha}, got {commit_sha}"
        )

    activation = activate_graph_snapshot(
        conn,
        project_id,
        snapshot_id,
        expected_old_snapshot_id=expected_old_snapshot_id,
        ref_name=ref_name,
    )
    materialized_count = 0
    if materialize_pending:
        identity = normalize_pending_scope_identity(
            ref_name=ref_name,
            branch_ref=branch_ref,
            worktree_id=worktree_id,
            worktree_path=worktree_path,
        )
        commit_targets = sorted({
            str(item or "").strip()
            for item in (covered_commit_shas or [commit_sha])
            if str(item or "").strip()
        })
        if not commit_targets:
            commit_targets = [commit_sha]
        pending_evidence = {
            "source": "graph_snapshot_finalizer",
            "actor": actor,
            "snapshot_id": snapshot_id,
            "ref_name": identity["ref_name"],
            "branch_ref": identity["branch_ref"],
            "worktree_id": identity["worktree_id"],
            "worktree_path": identity["worktree_path"],
            "covered_commit_shas": commit_targets,
            **(evidence or {}),
        }
        placeholders = ",".join("?" for _ in commit_targets)
        cur = conn.execute(
            f"""
            UPDATE pending_scope_reconcile
            SET status = ?,
                snapshot_id = ?,
                evidence_json = ?
            WHERE project_id = ?
              AND ref_name = ?
              AND worktree_id = ?
              AND commit_sha IN ({placeholders})
              AND status IN (?, ?, ?)
            """,
            (
                PENDING_STATUS_MATERIALIZED,
                snapshot_id,
                _json(pending_evidence),
                project_id,
                identity["ref_name"],
                identity["worktree_id"],
                *commit_targets,
                PENDING_STATUS_QUEUED,
                PENDING_STATUS_RUNNING,
                PENDING_STATUS_FAILED,
            ),
        )
        materialized_count = int(cur.rowcount or 0)
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": commit_sha,
        "activation": activation,
        "pending_materialized_count": materialized_count,
        "ref_name": ref_name,
    }


def get_active_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "active",
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT s.*
        FROM graph_snapshot_refs r
        JOIN graph_snapshots s
          ON s.project_id = r.project_id AND s.snapshot_id = r.snapshot_id
        WHERE r.project_id = ? AND r.ref_name = ?
        """,
        (project_id, ref_name),
    ).fetchone()
    return dict(row) if row else None


def _stable_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _timestamp_value(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def record_current_full_reconcile_provenance(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    snapshot_id: str,
    target_commit_sha: str,
    request_id: str,
    request_started_at: str,
    route_evidence: Mapping[str, Any],
    reconcile_event_id: int,
    reconcile_event_created_at: str,
    runtime_context_scope: Mapping[str, Any] | None = None,
    marker_created_at: str | None = None,
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Seal one protected current-full completion to its durable reconcile event."""

    if not schema_ready:
        ensure_schema(conn)
    project_id = str(project_id or "").strip()
    snapshot_id = str(snapshot_id or "").strip()
    target_commit_sha = str(target_commit_sha or "").strip().lower()
    request_id = str(request_id or "").strip()
    request_started_at = str(request_started_at or "").strip()
    reconcile_event_created_at = str(reconcile_event_created_at or "").strip()
    try:
        reconcile_event_id = int(reconcile_event_id)
    except (TypeError, ValueError):
        reconcile_event_id = 0
    marker_created_at = str(marker_created_at or utc_now()).strip()
    required = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "target_commit_sha": target_commit_sha,
        "request_id": request_id,
        "request_started_at": request_started_at,
        "marker_created_at": marker_created_at,
        "reconcile_event_created_at": reconcile_event_created_at,
    }
    missing = [key for key, value in required.items() if not value]
    if missing or reconcile_event_id <= 0:
        missing_fields = [*missing]
        if reconcile_event_id <= 0:
            missing_fields.append("reconcile_event_id")
        raise ValueError(
            "current-full reconcile provenance requires: "
            + ", ".join(missing_fields)
        )
    if schema_ready:
        snapshot_row = conn.execute(
            "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
            (project_id, snapshot_id),
        ).fetchone()
        snapshot = dict(snapshot_row) if snapshot_row else {}
    else:
        snapshot = get_graph_snapshot(conn, project_id, snapshot_id) or {}
    if (
        str(snapshot.get("status") or "").strip() != SNAPSHOT_STATUS_ACTIVE
        or str(snapshot.get("commit_sha") or "").strip().lower()
        != target_commit_sha
    ):
        raise ValueError(
            "current-full reconcile provenance requires the active target snapshot"
        )

    protected_action = "graph_current_full_reconcile"
    protected_entrypoint = (
        "POST /api/graph-governance/{project_id}/reconcile/current-full"
    )
    safe_route_evidence = dict(route_evidence or {})
    safe_runtime_context_scope = {
        key: str((runtime_context_scope or {}).get(key) or "").strip()
        for key in (
            "project_id",
            "backlog_id",
            "task_id",
            "parent_task_id",
            "runtime_context_id",
            "merge_queue_id",
            "contract_execution_id",
        )
        if str((runtime_context_scope or {}).get(key) or "").strip()
    }
    route_runtime_context_scope = (
        safe_route_evidence.get("runtime_context_scope")
        if isinstance(
            safe_route_evidence.get("runtime_context_scope"),
            Mapping,
        )
        else {}
    )
    if safe_runtime_context_scope:
        if (
            str(route_runtime_context_scope.get("source") or "").strip()
            != "parallel_branch_runtime_context"
            or route_runtime_context_scope.get("server_derived") is not True
        ):
            raise ValueError(
                "current-full reconcile runtime context scope requires server-derived route evidence"
            )
        comparable_route_scope = {
            key: str(route_runtime_context_scope.get(key) or "").strip()
            for key in safe_runtime_context_scope
        }
        if comparable_route_scope != safe_runtime_context_scope:
            raise ValueError(
                "current-full reconcile runtime context scope must match route evidence"
            )
        for field in (
            "backlog_id",
            "task_id",
            "parent_task_id",
            "runtime_context_id",
            "merge_queue_id",
            "contract_execution_id",
        ):
            route_value = str(safe_route_evidence.get(field) or "").strip()
            scope_value = safe_runtime_context_scope.get(field, "")
            if route_value and route_value != scope_value:
                raise ValueError(
                    "current-full reconcile route evidence runtime scope mismatch: "
                    + field
                )
    provenance_id = f"cfrp-{uuid.uuid4().hex[:24]}"
    marker_core = {
        "schema_version": "current_full_reconcile.provenance.v2",
        "source": "graph_governance_api",
        "protected_action": protected_action,
        "protected_entrypoint": protected_entrypoint,
        "provenance_id": provenance_id,
        "request_id": request_id,
        "request_started_at": request_started_at,
        "marker_created_at": marker_created_at,
        "target_commit_sha": target_commit_sha,
        "snapshot_id": snapshot_id,
        "activate": True,
        "normal_update_path": True,
        "reconcile_event_id": reconcile_event_id,
        "reconcile_event_created_at": reconcile_event_created_at,
        "route_evidence": safe_route_evidence,
    }
    if safe_runtime_context_scope:
        marker_core["runtime_context_scope"] = {
            **safe_runtime_context_scope,
            "source": "parallel_branch_runtime_context",
            "server_derived": True,
        }
    provenance_hash = _stable_sha256(marker_core)
    marker = {**marker_core, "provenance_hash": provenance_hash}
    notes = _snapshot_notes(snapshot)
    notes["current_full_reconcile"] = marker
    conn.execute(
        """
        INSERT INTO graph_current_full_reconcile_provenance (
          provenance_id, project_id, snapshot_id, target_commit_sha,
          protected_action, protected_entrypoint, request_id,
          request_started_at, marker_created_at, reconcile_event_id,
          reconcile_event_created_at, route_evidence_json, marker_json,
          provenance_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provenance_id,
            project_id,
            snapshot_id,
            target_commit_sha,
            protected_action,
            protected_entrypoint,
            request_id,
            request_started_at,
            marker_created_at,
            reconcile_event_id,
            reconcile_event_created_at,
            json.dumps(safe_route_evidence, sort_keys=True),
            json.dumps(marker, sort_keys=True),
            provenance_hash,
            marker_created_at,
        ),
    )
    conn.execute(
        "UPDATE graph_snapshots SET notes = ? "
        "WHERE project_id = ? AND snapshot_id = ?",
        (json.dumps(notes, sort_keys=True), project_id, snapshot_id),
    )
    return {
        "schema_version": "graph_current_full_reconcile_provenance.v1",
        "provenance_id": provenance_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "target_commit_sha": target_commit_sha,
        "protected_action": protected_action,
        "protected_entrypoint": protected_entrypoint,
        "request_id": request_id,
        "request_started_at": request_started_at,
        "marker_created_at": marker_created_at,
        "reconcile_event_id": reconcile_event_id,
        "reconcile_event_created_at": reconcile_event_created_at,
        "route_evidence": safe_route_evidence,
        "runtime_context_scope": safe_runtime_context_scope,
        "provenance_hash": provenance_hash,
        "marker": marker,
    }


def _current_full_snapshot_provenance_binding(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one snapshot's own durable current-full provenance.

    This deliberately does not compare task/runtime scope with another
    ContractRuntime.  A later active current-full snapshot may belong to a
    different bounded task; its job here is only to prove that the live
    canonical snapshot was produced and activated through the protected
    current-full path.
    """

    snapshot = dict(snapshot or {})
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    snapshot_commit = str(snapshot.get("commit_sha") or "").strip().lower()
    snapshot_status = str(snapshot.get("status") or "").strip()
    snapshot_kind = str(snapshot.get("snapshot_kind") or "").strip()
    marker = _snapshot_notes(snapshot).get("current_full_reconcile")
    marker = dict(marker) if isinstance(marker, Mapping) else {}
    provenance_id = str(marker.get("provenance_id") or "").strip()
    provenance_row = None
    if provenance_id and snapshot_id and snapshot_commit:
        provenance_rows = conn.execute(
            """
            SELECT *
            FROM graph_current_full_reconcile_provenance
            WHERE provenance_id = ? AND project_id = ?
              AND snapshot_id = ? AND target_commit_sha = ?
            LIMIT 2
            """,
            (
                provenance_id,
                project_id,
                snapshot_id,
                snapshot_commit,
            ),
        ).fetchall()
        if len(provenance_rows) == 1:
            provenance_row = provenance_rows[0]
    provenance = dict(provenance_row) if provenance_row else {}
    try:
        stored_marker = json.loads(
            str(provenance.get("marker_json") or "{}")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_marker = {}
    try:
        route_evidence = json.loads(
            str(provenance.get("route_evidence_json") or "{}")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        route_evidence = {}
    marker_core = dict(marker)
    marker_hash = str(marker_core.pop("provenance_hash", "") or "").strip()
    provenance_hash = str(
        provenance.get("provenance_hash") or ""
    ).strip()
    marker_runtime_scope = (
        marker.get("runtime_context_scope")
        if isinstance(marker.get("runtime_context_scope"), Mapping)
        else {}
    )
    route_runtime_scope = (
        route_evidence.get("runtime_context_scope")
        if isinstance(route_evidence.get("runtime_context_scope"), Mapping)
        else {}
    )
    scope_fields = (
        "project_id",
        "backlog_id",
        "task_id",
        "parent_task_id",
        "runtime_context_id",
        "merge_queue_id",
        "contract_execution_id",
    )

    def comparable_scope(scope: Mapping[str, Any]) -> dict[str, str]:
        return {
            key: str(scope.get(key) or "").strip()
            for key in scope_fields
            if str(scope.get(key) or "").strip()
        }

    runtime_context_scope_link_verified = bool(
        (
            not marker_runtime_scope
            and not route_runtime_scope
        )
        or (
            marker_runtime_scope
            and route_runtime_scope
            and comparable_scope(marker_runtime_scope)
            == comparable_scope(route_runtime_scope)
            and marker_runtime_scope.get("server_derived") is True
            and route_runtime_scope.get("server_derived") is True
            and marker_runtime_scope.get("source")
            == "parallel_branch_runtime_context"
            and route_runtime_scope.get("source")
            == "parallel_branch_runtime_context"
        )
    )
    try:
        marker_reconcile_event_id = int(
            marker.get("reconcile_event_id") or 0
        )
        provenance_reconcile_event_id = int(
            provenance.get("reconcile_event_id") or 0
        )
    except (TypeError, ValueError):
        marker_reconcile_event_id = 0
        provenance_reconcile_event_id = 0
    verified = bool(
        project_id
        and snapshot_id
        and snapshot_commit
        and snapshot_status == SNAPSHOT_STATUS_ACTIVE
        and snapshot_kind == "full"
        and marker
        and provenance
        and marker == stored_marker
        and marker_hash
        and marker_hash == provenance_hash
        and _stable_sha256(marker_core) == marker_hash
        and str(marker.get("schema_version") or "").strip()
        == "current_full_reconcile.provenance.v2"
        and marker.get("normal_update_path") is True
        and marker.get("activate") is True
        and str(marker.get("target_commit_sha") or "").strip().lower()
        == snapshot_commit
        and str(marker.get("snapshot_id") or "").strip() == snapshot_id
        and str(marker.get("provenance_id") or "").strip()
        == str(provenance.get("provenance_id") or "").strip()
        and str(marker.get("protected_action") or "").strip()
        == "graph_current_full_reconcile"
        and str(marker.get("protected_entrypoint") or "").strip()
        == "POST /api/graph-governance/{project_id}/reconcile/current-full"
        and str(provenance.get("protected_action") or "").strip()
        == str(marker.get("protected_action") or "").strip()
        and str(provenance.get("protected_entrypoint") or "").strip()
        == str(marker.get("protected_entrypoint") or "").strip()
        and str(provenance.get("request_id") or "").strip()
        == str(marker.get("request_id") or "").strip()
        and bool(str(marker.get("request_id") or "").strip())
        and str(provenance.get("request_started_at") or "").strip()
        == str(marker.get("request_started_at") or "").strip()
        and _timestamp_value(marker.get("request_started_at")) is not None
        and str(provenance.get("marker_created_at") or "").strip()
        == str(marker.get("marker_created_at") or "").strip()
        and _timestamp_value(marker.get("marker_created_at")) is not None
        and provenance_reconcile_event_id > 0
        and provenance_reconcile_event_id == marker_reconcile_event_id
        and str(
            provenance.get("reconcile_event_created_at") or ""
        ).strip()
        == str(marker.get("reconcile_event_created_at") or "").strip()
        and _timestamp_value(
            marker.get("reconcile_event_created_at")
        )
        is not None
        and isinstance(route_evidence, Mapping)
        and route_evidence == marker.get("route_evidence")
        and route_evidence.get("schema_version")
        == "graph_current_full_reconcile.route_evidence.v1"
        and bool(
            str(route_evidence.get("authenticated_role") or "").strip()
        )
        and bool(
            str(route_evidence.get("authentication_source") or "").strip()
        )
        and route_evidence.get("raw_route_token_persisted") is False
        and route_evidence.get("protected_action")
        == "graph_current_full_reconcile"
        and runtime_context_scope_link_verified
    )
    return {
        "verified": verified,
        "snapshot_id": snapshot_id,
        "snapshot_commit": snapshot_commit,
        "snapshot_status": snapshot_status,
        "snapshot_kind": snapshot_kind,
        "provenance_id": str(
            provenance.get("provenance_id") or ""
        ).strip(),
        "provenance_target_commit": str(
            provenance.get("target_commit_sha") or ""
        ).strip().lower(),
        "provenance_hash": provenance_hash,
        "reconcile_event_id": provenance_reconcile_event_id,
        "reconcile_event_created_at": str(
            provenance.get("reconcile_event_created_at") or ""
        ).strip(),
        "task_id": str(route_evidence.get("task_id") or "").strip(),
        "runtime_context_id": str(
            route_evidence.get("runtime_context_id") or ""
        ).strip(),
        "contract_execution_id": str(
            route_evidence.get("contract_execution_id") or ""
        ).strip(),
        "runtime_context_scope_link_verified": (
            runtime_context_scope_link_verified
        ),
        "marker": marker,
    }


def current_full_candidate_resume_tuple(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str,
    target_commit_sha: str,
    snapshot: Mapping[str, Any],
    request_metric: Mapping[str, Any],
    expected_snapshot_status: str = "candidate",
) -> dict[str, Any]:
    """Pure-read proof of one exact durable candidate-ready tuple."""

    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    notes = (
        snapshot.get("notes_payload")
        if isinstance(snapshot.get("notes_payload"), Mapping)
        else {}
    )
    origin_run_id = str(notes.get("run_id") or "").strip()
    origin_claim_row = conn.execute(
        """
        SELECT * FROM graph_current_full_build_claim_history
        WHERE project_id = ? AND snapshot_id = ? AND run_id = ?
        """,
        (project_id, snapshot_id, origin_run_id),
    ).fetchone()
    origin_claim = dict(origin_claim_row) if origin_claim_row else {}
    origin_metric_row = conn.execute(
        """
        SELECT * FROM reconcile_run_metrics
        WHERE project_id = ? AND run_id = ? AND snapshot_id = ?
        """,
        (project_id, origin_run_id, snapshot_id),
    ).fetchone()
    origin_metric = dict(origin_metric_row) if origin_metric_row else {}
    origin_metric_evidence_raw = origin_metric.get("evidence_json")
    origin_metric_evidence_value = _decode_json(origin_metric_evidence_raw, None)
    origin_metric_evidence = (
        dict(origin_metric_evidence_value)
        if isinstance(origin_metric_evidence_value, Mapping)
        else {}
    )
    origin_metric_evidence_canonical = bool(
        isinstance(origin_metric_evidence_raw, str)
        and isinstance(origin_metric_evidence_value, Mapping)
        and origin_metric_evidence_raw == _json(dict(origin_metric_evidence_value))
    )
    active_claim_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM graph_current_full_build_claim_history
            WHERE project_id = ? AND snapshot_id = ? AND status = 'active'
            """,
            (project_id, snapshot_id),
        ).fetchone()[0]
    )
    try:
        companion_integrity = validate_snapshot_companion_integrity(snapshot)
    except Exception as exc:
        companion_integrity = {
            "valid": False,
            "error": "current_full_candidate_companion_integrity_unreadable",
            "error_type": type(exc).__name__,
        }
    errors: list[str] = []
    if str(snapshot.get("status") or "") != expected_snapshot_status:
        errors.append(f"snapshot_not_{expected_snapshot_status}")
    if str(snapshot.get("commit_sha") or "") != target_commit_sha:
        errors.append("snapshot_commit_mismatch")
    if str(snapshot.get("snapshot_kind") or "") != "full":
        errors.append("snapshot_kind_mismatch")
    if not all(
        str(snapshot.get(field) or "").strip()
        for field in ("graph_sha256", "inventory_sha256", "drift_sha256")
    ):
        errors.append("snapshot_materialization_incomplete")
    if not companion_integrity.get("valid"):
        errors.append(
            str(
                companion_integrity.get("error")
                or "current_full_candidate_companion_integrity_invalid"
            )
        )
    if not origin_run_id:
        errors.append("candidate_origin_run_missing")
    if active_claim_count:
        errors.append("active_build_claim_present")
    if not origin_claim:
        errors.append("candidate_build_claim_missing")
    else:
        if str(origin_claim.get("commit_sha") or "") != target_commit_sha:
            errors.append("candidate_build_claim_commit_mismatch")
        if str(origin_claim.get("status") or "") != "released":
            errors.append("candidate_build_claim_unreleased")
        if str(origin_claim.get("terminal_status") or "") != "candidate_ready":
            errors.append("candidate_build_claim_not_ready")
    if not origin_metric:
        errors.append("candidate_metric_missing")
    else:
        if str(origin_metric.get("commit_sha") or "") != target_commit_sha:
            errors.append("candidate_metric_commit_mismatch")
        if str(origin_metric.get("status") or "") != "candidate_ready":
            errors.append("candidate_metric_not_ready")
        if str(origin_metric.get("snapshot_kind") or "") != "full":
            errors.append("candidate_metric_kind_mismatch")
        if str(origin_metric.get("strategy") or "") != "current_full_reconcile":
            errors.append("candidate_metric_strategy_mismatch")
        if str(origin_metric.get("graph_delta_mode") or "") != "full_rebuild":
            errors.append("candidate_metric_delta_mode_mismatch")
        if not isinstance(origin_metric_evidence_value, Mapping):
            errors.append("candidate_metric_evidence_malformed")
        elif not origin_metric_evidence:
            errors.append("candidate_metric_evidence_missing")
        else:
            if not origin_metric_evidence_canonical:
                errors.append("candidate_metric_evidence_not_canonical")
            if str(origin_metric_evidence.get("phase") or "") != "candidate_ready":
                errors.append("candidate_metric_phase_mismatch")
            if str(origin_metric_evidence.get("claim_id") or "") != str(
                origin_claim.get("claim_id") or ""
            ):
                errors.append("candidate_metric_claim_mismatch")
    request_metric_status = str(request_metric.get("status") or "").strip()
    if (
        request_metric
        and run_id != origin_run_id
        and request_metric_status != "candidate_ready"
    ):
        errors.append("request_run_metric_not_ready")
    return {
        "valid": not errors,
        "errors": errors,
        "origin_run_id": origin_run_id,
        "snapshot_id": snapshot_id,
        "active_claim_count": active_claim_count,
        "claim_id": str(origin_claim.get("claim_id") or ""),
        "claim_status": str(origin_claim.get("status") or ""),
        "claim_terminal_status": str(origin_claim.get("terminal_status") or ""),
        "origin_metric_status": str(origin_metric.get("status") or ""),
        "origin_metric_graph_delta_mode": str(
            origin_metric.get("graph_delta_mode") or ""
        ),
        "origin_metric_evidence_valid": bool(
            isinstance(origin_metric_evidence_value, Mapping)
            and origin_metric_evidence
            and origin_metric_evidence_canonical
            and str(origin_metric_evidence.get("phase") or "")
            == "candidate_ready"
            and str(origin_metric_evidence.get("claim_id") or "")
            == str(origin_claim.get("claim_id") or "")
        ),
        "origin_metric_phase": str(origin_metric_evidence.get("phase") or ""),
        "origin_metric_claim_id": str(
            origin_metric_evidence.get("claim_id") or ""
        ),
        "origin_metric_evidence_canonical": origin_metric_evidence_canonical,
        "request_metric_status": request_metric_status,
        "companion_integrity": companion_integrity,
    }


def current_full_candidate_tuple_from_db(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str,
    target_commit_sha: str,
    snapshot_id: str,
    expected_snapshot_status: str = "candidate",
) -> dict[str, Any]:
    """Lock-safe exact candidate tuple read with no schema or other writes."""

    snapshot_row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    snapshot = dict(snapshot_row) if snapshot_row else {}
    notes = _decode_json(snapshot.get("notes"), {})
    snapshot["notes_payload"] = (
        dict(notes) if isinstance(notes, Mapping) else {}
    )
    request_metric_row = conn.execute(
        "SELECT * FROM reconcile_run_metrics WHERE project_id = ? "
        "AND run_id = ? AND snapshot_id = ?",
        (project_id, run_id, snapshot_id),
    ).fetchone()
    return current_full_candidate_resume_tuple(
        conn,
        project_id=project_id,
        run_id=run_id,
        target_commit_sha=target_commit_sha,
        snapshot=snapshot,
        request_metric=(dict(request_metric_row) if request_metric_row else {}),
        expected_snapshot_status=expected_snapshot_status,
    )


_CURRENT_FULL_COMPLETE_EVIDENCE_KEYS = {
    "phase",
    "activate_requested",
    "idempotency_scope",
    "request_id",
    "reconcile_event_id",
    "provenance_id",
}


def _current_full_idempotency_scope(
    route_evidence: Mapping[str, Any],
) -> dict[str, str]:
    route_scope = (
        route_evidence.get("route_token_scope")
        if isinstance(route_evidence.get("route_token_scope"), Mapping)
        else {}
    )
    runtime_scope = (
        route_evidence.get("runtime_context_scope")
        if isinstance(route_evidence.get("runtime_context_scope"), Mapping)
        else {}
    )
    return {
        key: str(runtime_scope.get(key) or route_scope.get(key) or "").strip()
        for key in (
            "project_id",
            "backlog_id",
            "task_id",
            "parent_task_id",
            "runtime_context_id",
            "merge_queue_id",
            "contract_execution_id",
        )
        if str(runtime_scope.get(key) or route_scope.get(key) or "").strip()
    }


def current_full_active_terminal_tuple(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str,
    target_commit_sha: str,
    expected_scope: Mapping[str, Any],
    snapshot_id: str,
) -> dict[str, Any]:
    """Pure-read proof of one exact server-authored active completion."""

    expected_scope = dict(expected_scope)
    route_bound = bool(
        expected_scope.get("backlog_id") and expected_scope.get("task_id")
    )
    snapshot_row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    snapshot = dict(snapshot_row) if snapshot_row else {}
    ref_row = conn.execute(
        "SELECT snapshot_id, commit_sha FROM graph_snapshot_refs "
        "WHERE project_id = ? AND ref_name = 'active'",
        (project_id,),
    ).fetchone()
    active_ref = dict(ref_row) if ref_row else {}
    metric_row = conn.execute(
        "SELECT * FROM reconcile_run_metrics WHERE project_id = ? "
        "AND run_id = ? AND snapshot_id = ?",
        (project_id, run_id, snapshot_id),
    ).fetchone()
    metric = dict(metric_row) if metric_row else {}
    evidence_raw = metric.get("evidence_json")
    evidence_value = _decode_json(evidence_raw, None)
    evidence = dict(evidence_value) if isinstance(evidence_value, Mapping) else {}
    evidence_canonical = bool(
        isinstance(evidence_raw, str)
        and isinstance(evidence_value, Mapping)
        and evidence_raw == _json(dict(evidence_value))
    )
    try:
        reconcile_event_id = int(evidence.get("reconcile_event_id") or 0)
    except (TypeError, ValueError):
        reconcile_event_id = 0
    provenance_id = str(evidence.get("provenance_id") or "").strip()
    request_id = str(evidence.get("request_id") or "").strip()
    stored_scope = (
        dict(evidence.get("idempotency_scope"))
        if isinstance(evidence.get("idempotency_scope"), Mapping)
        else None
    )
    try:
        companion_integrity = validate_snapshot_companion_integrity(snapshot)
    except Exception as exc:
        companion_integrity = {
            "valid": False,
            "error": "current_full_terminal_companion_integrity_unreadable",
            "error_type": type(exc).__name__,
        }

    provenance = {}
    provenance_route_evidence: Mapping[str, Any] = {}
    provenance_binding: Mapping[str, Any] = {}
    if provenance_id:
        row = conn.execute(
            """
            SELECT * FROM graph_current_full_reconcile_provenance
            WHERE provenance_id = ? AND project_id = ?
              AND snapshot_id = ? AND target_commit_sha = ?
            """,
            (provenance_id, project_id, snapshot_id, target_commit_sha),
        ).fetchone()
        provenance = dict(row) if row else {}
        decoded_route = _decode_json(provenance.get("route_evidence_json"), {})
        provenance_route_evidence = (
            dict(decoded_route) if isinstance(decoded_route, Mapping) else {}
        )
        provenance_binding = _current_full_snapshot_provenance_binding(
            conn, project_id, snapshot
        )

    timeline_event = {}
    timeline_payload: Mapping[str, Any] = {}
    if reconcile_event_id > 0:
        row = conn.execute(
            "SELECT * FROM task_timeline_events WHERE project_id = ? AND id = ?",
            (project_id, reconcile_event_id),
        ).fetchone()
        timeline_event = dict(row) if row else {}
        decoded_payload = _decode_json(timeline_event.get("payload_json"), {})
        timeline_payload = (
            dict(decoded_payload) if isinstance(decoded_payload, Mapping) else {}
        )
    event_result = (
        timeline_payload.get("graph_reconcile_result")
        if isinstance(timeline_payload.get("graph_reconcile_result"), Mapping)
        else {}
    )
    event_trace = (
        event_result.get("operation_trace")
        if isinstance(event_result.get("operation_trace"), Mapping)
        else {}
    )
    event_runtime_scope = (
        timeline_payload.get("runtime_context_scope")
        if isinstance(timeline_payload.get("runtime_context_scope"), Mapping)
        else {}
    )
    timeline_scope_mismatch_fields: list[str] = []

    errors: list[str] = []
    if not snapshot:
        errors.append("terminal_snapshot_missing")
    else:
        if str(snapshot.get("status") or "") != "active":
            errors.append("terminal_snapshot_not_active")
        if str(snapshot.get("commit_sha") or "") != target_commit_sha:
            errors.append("terminal_snapshot_commit_mismatch")
        if str(snapshot.get("snapshot_kind") or "") != "full":
            errors.append("terminal_snapshot_kind_mismatch")
    if (
        str(active_ref.get("snapshot_id") or "") != snapshot_id
        or str(active_ref.get("commit_sha") or "") != target_commit_sha
    ):
        errors.append("terminal_active_ref_mismatch")
    if not companion_integrity.get("valid"):
        errors.append(
            str(
                companion_integrity.get("error")
                or "current_full_terminal_companion_integrity_invalid"
            )
        )
    if not metric:
        errors.append("terminal_metric_missing")
    else:
        for field, expected in (
            ("status", "complete"),
            ("commit_sha", target_commit_sha),
            ("snapshot_kind", "full"),
            ("strategy", "current_full_reconcile"),
            ("graph_delta_mode", "full_rebuild"),
        ):
            if str(metric.get(field) or "") != expected:
                errors.append(f"terminal_metric_{field}_mismatch")
    if not isinstance(evidence_value, Mapping):
        errors.append("terminal_metric_evidence_malformed")
    else:
        if set(evidence) != _CURRENT_FULL_COMPLETE_EVIDENCE_KEYS:
            errors.append("terminal_metric_evidence_schema_mismatch")
        if not evidence_canonical:
            errors.append("terminal_metric_evidence_not_canonical")
        if str(evidence.get("phase") or "") != "atomic_finalize_complete":
            errors.append("terminal_metric_phase_mismatch")
        if evidence.get("activate_requested") is not True:
            errors.append("terminal_metric_activate_mismatch")
        if stored_scope != expected_scope:
            errors.append("terminal_metric_scope_mismatch")
        if not request_id:
            errors.append("terminal_metric_request_id_missing")
        if route_bound != (reconcile_event_id > 0):
            errors.append("terminal_metric_event_route_mode_mismatch")
        if route_bound != bool(provenance_id):
            errors.append("terminal_metric_provenance_route_mode_mismatch")

    if route_bound:
        if not provenance:
            errors.append("terminal_provenance_missing")
        else:
            if provenance_binding.get("verified") is not True:
                errors.append("terminal_provenance_binding_invalid")
            if str(provenance_binding.get("provenance_id") or "") != provenance_id:
                errors.append("terminal_provenance_id_mismatch")
            if str(provenance.get("request_id") or "") != request_id:
                errors.append("terminal_provenance_request_mismatch")
            if int(provenance.get("reconcile_event_id") or 0) != reconcile_event_id:
                errors.append("terminal_provenance_event_mismatch")
            if str(provenance_route_evidence.get("reconcile_run_id") or "") != run_id:
                errors.append("terminal_provenance_run_mismatch")
            if _current_full_idempotency_scope(provenance_route_evidence) != expected_scope:
                errors.append("terminal_provenance_scope_mismatch")
        if not timeline_event:
            errors.append("terminal_timeline_missing")
        else:
            if (
                str(timeline_event.get("event_type") or "") != "graph.reconcile"
                or str(timeline_event.get("event_kind") or "") != "reconcile"
                or str(timeline_event.get("phase") or "") != "reconcile"
                or str(timeline_event.get("status") or "") != "passed"
                or str(timeline_event.get("backlog_id") or "")
                != expected_scope.get("backlog_id")
                or str(timeline_event.get("task_id") or "")
                != expected_scope.get("task_id")
                or str(timeline_event.get("commit_sha") or "")
                != target_commit_sha
            ):
                errors.append("terminal_timeline_identity_mismatch")
            if (
                str(timeline_payload.get("target_commit_sha") or "")
                != target_commit_sha
                or str(timeline_payload.get("snapshot_id") or "") != snapshot_id
                or str(timeline_payload.get("active_snapshot_id") or "")
                != snapshot_id
                or timeline_payload.get("current_full_reconcile") is not True
                or timeline_payload.get("graph_reconciled") is not True
                or str(event_trace.get("run_id") or "") != run_id
            ):
                errors.append("terminal_timeline_payload_mismatch")
            for key, value in expected_scope.items():
                if key in {"project_id", "backlog_id", "task_id"}:
                    continue
                if str(timeline_payload.get(key) or "") != value:
                    timeline_scope_mismatch_fields.append(key)
                    errors.append("terminal_timeline_scope_mismatch")
                    break
                if event_runtime_scope and str(
                    event_runtime_scope.get(key) or ""
                ) != value:
                    timeline_scope_mismatch_fields.append(
                        f"runtime_context_scope.{key}"
                    )
                    errors.append("terminal_timeline_runtime_scope_mismatch")
                    break

    return {
        "valid": not errors,
        "errors": errors,
        "route_bound": route_bound,
        # Keep the exact server-authored terminal scope available to the
        # current-full facade.  A fresh child route may legitimately encounter
        # a snapshot finalized by its canonical batch parent; the facade still
        # has to prove that parent/child relationship before it can reuse this
        # scope.  Returning the decoded value here avoids reconstructing it
        # from caller claims and never rewrites historical evidence.
        "stored_scope": dict(stored_scope or {}),
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "metric_evidence_canonical": evidence_canonical,
        "metric_evidence_keys": sorted(evidence),
        "timeline_scope_mismatch_fields": timeline_scope_mismatch_fields,
        "request_id": request_id,
        "reconcile_event_id": reconcile_event_id,
        "provenance_id": provenance_id,
        "companion_integrity": companion_integrity,
        "snapshot": snapshot,
        "metric": metric,
        "metric_evidence": evidence,
        "provenance": provenance,
        "timeline_event": timeline_event,
    }


_RECONCILE_TERMINALIZATION_METRIC_FIELDS = (
    "project_id", "run_id", "snapshot_id", "commit_sha",
    "parent_commit_sha", "snapshot_kind", "strategy", "graph_delta_mode",
    "status", "changed_file_count", "impacted_file_count", "event_count",
    "node_count", "edge_count", "elapsed_ms", "trace_summary_path",
    "fallback_reason", "created_at", "evidence_json",
)
_RECONCILE_TERMINALIZATION_UTC_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z"
)


class _ReconcileRunTerminalizationProof:
    __slots__ = ("_canonical", "_seal")

    def __init__(self, payload: Mapping[str, Any]):
        self._canonical = _json(dict(payload))
        self._seal = "sha256:" + hashlib.sha256(
            self._canonical.encode("utf-8")
        ).hexdigest()


def _terminalization_require(condition: Any, reason: str) -> None:
    if not condition:
        raise ReconcileRunTerminalizationProofError(reason)


def _terminalization_typed_value(value: Any) -> Any:
    scalar_types = {type(None): "null", bool: "boolean", int: "integer", float: "number"}
    if type(value) in scalar_types:
        return [scalar_types[type(value)], value]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["array", [_terminalization_typed_value(item) for item in value]]
    if isinstance(value, Mapping):
        _terminalization_require(
            all(isinstance(key, str) for key in value),
            "terminalization_json_type_invalid",
        )
        return [
            "object",
            [[key, _terminalization_typed_value(value[key])] for key in sorted(value)],
        ]
    _terminalization_require(False, "terminalization_json_type_invalid")


def _terminalization_exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate_json_key")
    return result


def _terminalization_reject_constant(_value: str) -> None:
    raise ValueError("non_json_number")


def _terminalization_strict_json_object(raw: Any) -> dict[str, Any]:
    _terminalization_require(
        isinstance(raw, str), "terminalization_metric_evidence_not_text"
    )
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_terminalization_exact_object,
            parse_constant=_terminalization_reject_constant,
        )
    except (TypeError, ValueError) as exc:
        raise ReconcileRunTerminalizationProofError(
            "terminalization_metric_evidence_invalid"
        ) from exc
    _terminalization_require(
        isinstance(decoded, dict), "terminalization_metric_evidence_not_object"
    )
    return decoded


def _terminalization_utc(value: Any) -> datetime:
    raw = str(value or "")
    _terminalization_require(
        _RECONCILE_TERMINALIZATION_UTC_RE.fullmatch(raw) is not None,
        "terminalization_metric_created_at_invalid",
    )
    try:
        return datetime.strptime(
            raw,
            "%Y-%m-%dT%H:%M:%S.%fZ" if "." in raw else "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ReconcileRunTerminalizationProofError(
            "terminalization_metric_created_at_invalid"
        ) from exc


def _terminalization_metric_proof(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    snapshot_id: str,
    *,
    allowed_statuses: frozenset[str],
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT rowid AS _metric_rowid, * FROM reconcile_run_metrics "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        (project_id, run_id, snapshot_id),
    ).fetchone()
    _terminalization_require(row, "terminalization_metric_missing")
    metric = dict(row)
    identity_row = conn.execute(
        "SELECT * FROM graph_reconcile_metric_physical_identities "
        "WHERE project_id=? AND run_id=? AND snapshot_id=? "
        "ORDER BY identity_sequence DESC LIMIT 1",
        (project_id, run_id, snapshot_id),
    ).fetchone()
    identity = dict(identity_row) if identity_row else {}
    _terminalization_require(identity, "terminalization_metric_identity_missing")
    _terminalization_require(
        int(identity.get("metric_rowid") or 0) == int(metric["_metric_rowid"]),
        "terminalization_metric_identity_stale",
    )
    _terminalization_require(
        metric.get("status") in allowed_statuses,
        "terminalization_metric_status_invalid",
    )
    _terminalization_require(not (
        metric.get("snapshot_kind") != "full"
        or metric.get("strategy") != "current_full_reconcile"
        or metric.get("graph_delta_mode") != "full_rebuild"
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(metric.get("commit_sha") or "")) is None
    ), "terminalization_metric_identity_invalid")
    created_at_dt = _terminalization_utc(metric.get("created_at"))
    evidence = _terminalization_strict_json_object(metric.get("evidence_json"))
    scope = evidence.get("idempotency_scope")
    _terminalization_require(
        isinstance(scope, Mapping), "terminalization_metric_scope_invalid"
    )
    exact_metric = {
        field: metric.get(field) for field in _RECONCILE_TERMINALIZATION_METRIC_FIELDS
    }
    exact_metric.update(
        {
            "metric_rowid": int(metric["_metric_rowid"]),
            "metric_identity_sequence": int(identity["identity_sequence"]),
        }
    )
    scope_sha256 = _stable_sha256(_terminalization_typed_value(dict(scope)))
    fingerprint = _stable_sha256(_terminalization_typed_value(exact_metric))
    return {
        "row": metric,
        "scope": dict(scope),
        "scope_sha256": scope_sha256,
        "fingerprint": fingerprint,
        "created_at_dt": created_at_dt,
        "sealed": {
            "run_id": metric["run_id"], "snapshot_id": metric["snapshot_id"],
            "raw_status": metric["status"], "created_at": metric["created_at"],
            "metric_rowid": int(metric["_metric_rowid"]),
            "metric_identity_sequence": int(identity["identity_sequence"]),
            "scope_sha256": scope_sha256, "fingerprint": fingerprint,
        },
    }


def _terminalization_manager_certificate(
    conn: sqlite3.Connection,
    project_id: str,
    supplied: Mapping[str, Any],
) -> dict[str, Any]:
    _terminalization_require(
        isinstance(supplied, Mapping),
        "terminalization_manager_certificate_invalid",
    )
    row = conn.execute(
        "SELECT * FROM graph_reconcile_manager_generations "
        "WHERE project_id=? ORDER BY sequence DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    _terminalization_require(row, "terminalization_manager_certificate_missing")
    certificate = dict(row)
    try:
        _validate_manager_generation_certificate_row(conn, certificate)
    except ManagerGenerationCertificateConflictError as exc:
        raise ReconcileRunTerminalizationProofError(
            "terminalization_manager_certificate_history_invalid"
        ) from exc
    expected = manager_generation_certificate_public_receipt(certificate)
    _terminalization_require(
        set(supplied) == set(expected)
        and _terminalization_typed_value(dict(supplied))
        == _terminalization_typed_value(expected),
        "terminalization_manager_certificate_mismatch",
    )
    return expected


def _terminalization_replacements(
    conn: sqlite3.Connection,
    project_id: str,
    source: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    source_row = source["row"]
    rows = conn.execute(
        "SELECT run_id,snapshot_id FROM reconcile_run_metrics "
        "WHERE project_id=? AND commit_sha=? "
        "AND strategy='current_full_reconcile' "
        "AND status IN ('candidate_ready','complete') "
        "ORDER BY created_at,run_id,snapshot_id",
        (project_id, source_row["commit_sha"]),
    ).fetchall()
    replacements: list[dict[str, Any]] = []
    invalid_seen = False
    for replacement_key in rows:
        try:
            replacement = _terminalization_metric_proof(
                conn,
                project_id,
                str(replacement_key["run_id"]),
                str(replacement_key["snapshot_id"]),
                allowed_statuses=frozenset({"candidate_ready", "complete"}),
            )
        except ReconcileRunTerminalizationProofError:
            invalid_seen = True
            continue
        replacement_row = replacement["row"]
        if replacement["created_at_dt"] <= source["created_at_dt"]:
            continue
        if _terminalization_typed_value(replacement["scope"]) != (
            _terminalization_typed_value(source["scope"])
        ):
            continue
        if conn.execute(
            "SELECT 1 FROM graph_current_full_build_claim_history "
            "WHERE project_id=? AND snapshot_id=? AND status='active' LIMIT 1",
            (project_id, replacement_row["snapshot_id"]),
        ).fetchone():
            invalid_seen = True
            continue
        if replacement_row["status"] == "candidate_ready":
            c1_proof = current_full_candidate_tuple_from_db(
                conn,
                project_id=project_id,
                run_id=replacement_row["run_id"],
                target_commit_sha=source_row["commit_sha"],
                snapshot_id=replacement_row["snapshot_id"],
            )
        else:
            c1_proof = current_full_active_terminal_tuple(
                conn,
                project_id=project_id,
                run_id=replacement_row["run_id"],
                target_commit_sha=source_row["commit_sha"],
                expected_scope=source["scope"],
                snapshot_id=replacement_row["snapshot_id"],
            )
        if not c1_proof.get("valid"):
            if conn.execute(
                "SELECT 1 FROM graph_current_full_build_claim_history "
                "WHERE project_id=? AND snapshot_id=? LIMIT 1",
                (project_id, replacement_row["snapshot_id"]),
            ).fetchone():
                invalid_seen = True
            continue
        replacements.append(replacement)
    return replacements, invalid_seen


def _terminalization_generation_replacement(
    source: Mapping[str, Any], certificate: Mapping[str, Any]
) -> dict[str, Any]:
    _terminalization_require(
        source["created_at_dt"] < _terminalization_utc(
            certificate.get("manager_started_at")
        ),
        "terminalization_source_not_generation_quiesced",
    )
    return {
        "proof_kind": "manager_generation",
        "run_id": "",
        "snapshot_id": "",
        "metric_identity_sequence": int(certificate["sequence"]),
        "raw_status": "complete",
        "fingerprint": str(certificate["certificate_hash"]),
    }


def _terminalization_source_is_stale(
    conn: sqlite3.Connection, project_id: str, snapshot_id: str
) -> bool:
    return not conn.execute(
        "SELECT 1 FROM graph_snapshots WHERE project_id=? AND snapshot_id=? "
        "UNION ALL SELECT 1 FROM graph_current_full_build_claim_history "
        "WHERE project_id=? AND snapshot_id=? AND status='active' LIMIT 1",
        (project_id, snapshot_id, project_id, snapshot_id),
    ).fetchone() and not snapshot_companion_dir(project_id, snapshot_id).exists()


def reconcile_run_terminalization_proof(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str,
    snapshot_id: str,
    manager_certificate: Mapping[str, Any],
) -> _ReconcileRunTerminalizationProof:
    project = str(project_id or "")
    _terminalization_require(
        conn.execute("SELECT 1 FROM graph_reconcile_metric_identity_schema_state").fetchone(),
        "terminalization_metric_identity_migration_incomplete",
    )
    source = _terminalization_metric_proof(
        conn,
        project,
        str(run_id or ""),
        str(snapshot_id or ""),
        allowed_statuses=frozenset({"running", "finalizing"}),
    )
    source_row = source["row"]
    _terminalization_require(
        _terminalization_source_is_stale(
            conn, project, source_row["snapshot_id"]
        ),
        "terminalization_source_not_stale",
    )
    certificate = _terminalization_manager_certificate(
        conn, project, manager_certificate
    )
    replacements, invalid_replacement_seen = _terminalization_replacements(
        conn, project, source
    )
    _terminalization_require(
        not invalid_replacement_seen, "terminalization_replacement_invalid"
    )
    _terminalization_require(
        len(replacements) <= 1, "terminalization_replacement_ambiguous"
    )
    replacement = (
        {**replacements[0]["sealed"], "proof_kind": "materialized"}
        if replacements
        else _terminalization_generation_replacement(source, certificate)
    )
    payload = {
        "schema_version": "reconcile_run_terminalization.proof.v1",
        "project_id": project,
        "source": source["sealed"],
        "replacement": replacement,
        "manager_certificate": certificate,
    }
    return _ReconcileRunTerminalizationProof(payload)


def reconcile_run_terminalization_safe_receipt(
    proof: _ReconcileRunTerminalizationProof,
) -> dict[str, Any]:
    if not isinstance(proof, _ReconcileRunTerminalizationProof):
        raise TypeError("sealed terminalization proof required")
    expected_seal = "sha256:" + hashlib.sha256(
        proof._canonical.encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(proof._seal, expected_seal):
        raise TypeError("sealed terminalization proof invalid")
    payload = json.loads(proof._canonical)
    source = payload["source"]
    replacement = payload["replacement"]
    certificate = payload["manager_certificate"]
    replacement_count = 1 if replacement["proof_kind"] == "materialized" else 0
    return {
        "schema_version": "reconcile_run_terminalization.safe_receipt.v1",
        "source_identity_sha256": _stable_sha256(
            ["terminalization", payload["project_id"], source["run_id"], source["snapshot_id"]]
        ),
        "source_fingerprint": source["fingerprint"],
        "replacement_identity_sha256": _stable_sha256(
            ["terminalization", payload["project_id"], replacement["run_id"], replacement["snapshot_id"]]
            if replacement_count == 1
            else [
                "terminalization",
                "manager_generation",
                payload["project_id"],
                certificate["certificate_id"],
            ]
        ),
        "replacement_fingerprint": replacement["fingerprint"],
        "manager_certificate_hash": certificate["certificate_hash"],
        "proof_sha256": proof._seal,
        "replacement_count": replacement_count,
        "server_derived": True,
    }


def _terminalization_proof_snapshot_ids(
    proof: _ReconcileRunTerminalizationProof,
) -> tuple[str, ...]:
    """Return every process-fence snapshot identity from one sealed proof."""

    reconcile_run_terminalization_safe_receipt(proof)
    payload = json.loads(proof._canonical)
    snapshot_ids = [str(payload["source"]["snapshot_id"])]
    if payload["replacement"]["proof_kind"] == "materialized":
        snapshot_ids.append(str(payload["replacement"]["snapshot_id"]))
    return tuple(snapshot_ids)


_RECONCILE_TERMINALIZATION_LEDGER_FIELDS = (
    "terminalization_id", "project_id", "source_run_id", "source_snapshot_id",
    "source_metric_identity_sequence", "source_raw_status", "source_fingerprint",
    "replacement_proof_kind", "replacement_run_id", "replacement_snapshot_id",
    "replacement_metric_identity_sequence", "replacement_raw_status",
    "replacement_fingerprint", "manager_certificate_id",
    "manager_certificate_hash", "timeline_event_id", "timeline_event_hash",
    "terminal_status", "created_at", "ledger_hash",
)


def _terminalization_ledger_hash(row: Mapping[str, Any]) -> str:
    payload = {
        field: row.get(field)
        for field in _RECONCILE_TERMINALIZATION_LEDGER_FIELDS
        if field != "ledger_hash"
    }
    return _stable_sha256(_terminalization_typed_value(payload))


def _terminalization_timeline_hash(row: Mapping[str, Any]) -> str:
    return _stable_sha256(_terminalization_typed_value(dict(row)))


def _terminalization_historical_certificate(
    conn: sqlite3.Connection,
    project_id: str,
    certificate_id: str,
    certificate_hash: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM graph_reconcile_manager_generations "
        "WHERE project_id=? AND certificate_id=?",
        (project_id, certificate_id),
    ).fetchone()
    _terminalization_require(row, "terminalization_manager_certificate_missing")
    certificate = dict(row)
    try:
        _validate_manager_generation_certificate_row(conn, certificate)
    except ManagerGenerationCertificateConflictError as exc:
        raise ReconcileRunTerminalizationProofError(
            "terminalization_manager_certificate_history_invalid"
        ) from exc
    receipt = manager_generation_certificate_public_receipt(certificate)
    _terminalization_require(
        hmac.compare_digest(receipt["certificate_hash"], certificate_hash),
        "terminalization_manager_certificate_mismatch",
    )
    return receipt


def _terminalization_validate_timeline(
    row: Mapping[str, Any],
    ledger: Mapping[str, Any],
    source: Mapping[str, Any],
    safe_receipt: Mapping[str, Any],
) -> None:
    _terminalization_utc(row.get("created_at"))
    _terminalization_utc(ledger.get("created_at"))
    expected_payload = {
        "schema_version": "graph.reconcile_run_terminalized.timeline.v1",
        "terminalization_id_sha256": _stable_sha256(
            ["terminalization_id", ledger["terminalization_id"]]
        ),
        "source_identity_sha256": safe_receipt["source_identity_sha256"],
        "source_fingerprint": safe_receipt["source_fingerprint"],
        "replacement_identity_sha256": safe_receipt["replacement_identity_sha256"],
        "replacement_fingerprint": safe_receipt["replacement_fingerprint"],
        "manager_certificate_hash": safe_receipt["manager_certificate_hash"],
        "proof_sha256": safe_receipt["proof_sha256"],
        "close_satisfying": False,
        "synthesizes_pass": False,
        "authoritative_pass_synthesized": False,
        "graph_reconciled": False,
        "server_derived": True,
    }
    expected = {
        "project_id": ledger["project_id"], "mf_id": "", "attempt_num": 0,
        "event_type": "graph.reconcile_run_terminalized",
        "phase": "reconcile_terminalization",
        "event_kind": "reconcile_terminalization", "scenario_id": "",
        "parent_event_id": 0,
        "correlation_id": expected_payload["terminalization_id_sha256"],
        "severity": "", "decision": "", "schema_version": 2,
        "actor": "governance_store", "status": "recorded",
        "payload_json": _json(expected_payload),
        "verification_json": _json(
            {"audit_only": True, "protected_close_evidence": False}
        ),
        "artifact_refs_json": "{}", "trace_id": "",
        "commit_sha": source["row"]["commit_sha"],
    }
    _terminalization_require(
        all(row.get(field) == value for field, value in expected.items())
        and isinstance(row.get("backlog_id"), str) and bool(row["backlog_id"])
        and isinstance(row.get("task_id"), str) and bool(row["task_id"])
        and row.get("created_at") == ledger["created_at"],
        "terminalization_timeline_invalid",
    )


def _terminalization_overlay_projection(raw_status: Any, reason: str) -> dict[str, Any]:
    status = str(raw_status or "").strip().lower()
    effective = status if status in {
        "running", "finalizing", "candidate_ready", "complete", "failed"
    } else "unknown"
    return {
        "schema_version": "reconcile_run_terminalization.overlay.v1",
        "valid": False,
        "effective_status": effective,
        "is_terminal": effective in {"candidate_ready", "complete", "failed"},
        "status_reason_code": reason,
    }


def reconcile_run_terminalization_overlay(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Validate one historical terminalization overlay without writing state."""

    try:
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        metric = conn.execute(
            "SELECT status FROM reconcile_run_metrics "
            "WHERE project_id=? AND run_id=? AND snapshot_id=?",
            (project_id, run_id, snapshot_id),
        ).fetchone()
        raw_status = metric["status"] if metric else ""
        rows = conn.execute(
            "SELECT * FROM graph_reconcile_run_terminalizations "
            "WHERE project_id=? AND source_run_id=? AND source_snapshot_id=?",
            (project_id, run_id, snapshot_id),
        ).fetchall()
    except sqlite3.DatabaseError:
        return _terminalization_overlay_projection(
            "", "terminalization_overlay_invalid"
        )
    if not rows:
        return _terminalization_overlay_projection(
            raw_status, "terminalization_overlay_missing"
        )
    if len(rows) != 1:
        return _terminalization_overlay_projection(
            raw_status, "terminalization_overlay_invalid"
        )
    try:
        ledger = dict(rows[0])
        _terminalization_require(
            set(ledger) == set(_RECONCILE_TERMINALIZATION_LEDGER_FIELDS)
            and ledger["terminal_status"] == "terminalized_stale"
            and hmac.compare_digest(
                str(ledger["ledger_hash"]), _terminalization_ledger_hash(ledger)
            ),
            "terminalization_ledger_invalid",
        )
        source = _terminalization_metric_proof(
            conn, project_id, run_id, snapshot_id,
            allowed_statuses=frozenset({"running", "finalizing"}),
        )
        _terminalization_require(
            ledger["source_metric_identity_sequence"]
            == source["sealed"]["metric_identity_sequence"]
            and ledger["source_raw_status"] == source["sealed"]["raw_status"]
            and hmac.compare_digest(
                ledger["source_fingerprint"], source["sealed"]["fingerprint"]
            ),
            "terminalization_source_drift",
        )
        _terminalization_require(
            _terminalization_source_is_stale(conn, project_id, snapshot_id),
            "terminalization_source_not_stale",
        )
        certificate = _terminalization_historical_certificate(
            conn, project_id, ledger["manager_certificate_id"],
            ledger["manager_certificate_hash"],
        )
        replacement_kind = ledger["replacement_proof_kind"]
        if replacement_kind == "materialized":
            replacements, invalid_seen = _terminalization_replacements(
                conn, project_id, source
            )
            _terminalization_require(
                not invalid_seen and len(replacements) == 1,
                "terminalization_replacement_invalid",
            )
            replacement = {
                **replacements[0]["sealed"],
                "proof_kind": "materialized",
            }
        else:
            replacement = _terminalization_generation_replacement(
                source, certificate
            )
        _terminalization_require(
            ledger["replacement_run_id"] == replacement["run_id"]
            and ledger["replacement_snapshot_id"] == replacement["snapshot_id"]
            and ledger["replacement_metric_identity_sequence"]
            == replacement["metric_identity_sequence"]
            and ledger["replacement_raw_status"] == replacement["raw_status"]
            and hmac.compare_digest(
                ledger["replacement_fingerprint"],
                replacement["fingerprint"],
            ),
            "terminalization_replacement_drift",
        )
        proof = _ReconcileRunTerminalizationProof({
            "schema_version": "reconcile_run_terminalization.proof.v1",
            "project_id": project_id,
            "source": source["sealed"],
            "replacement": replacement,
            "manager_certificate": certificate,
        })
        safe_receipt = reconcile_run_terminalization_safe_receipt(proof)
        timeline_row = conn.execute(
            "SELECT * FROM task_timeline_events WHERE id=?",
            (ledger["timeline_event_id"],),
        ).fetchone()
        _terminalization_require(
            timeline_row, "terminalization_timeline_missing"
        )
        timeline = dict(timeline_row)
        _terminalization_require(
            hmac.compare_digest(
                ledger["timeline_event_hash"],
                _terminalization_timeline_hash(timeline),
            ),
            "terminalization_timeline_hash_invalid",
        )
        _terminalization_validate_timeline(
            timeline, ledger, source, safe_receipt
        )
        source_after = _terminalization_metric_proof(
            conn, project_id, run_id, snapshot_id,
            allowed_statuses=frozenset({"running", "finalizing"}),
        )
        if replacement_kind == "materialized":
            replacements_after, invalid_after = _terminalization_replacements(
                conn, project_id, source_after
            )
            replacement_after = (
                {
                    **replacements_after[0]["sealed"],
                    "proof_kind": "materialized",
                }
                if len(replacements_after) == 1
                else {}
            )
        else:
            invalid_after = False
            replacement_after = _terminalization_generation_replacement(
                source_after, certificate
            )
        _terminalization_require(
            source_after["sealed"] == source["sealed"]
            and _terminalization_source_is_stale(conn, project_id, snapshot_id)
            and not invalid_after
            and replacement_after == replacement
            and int(conn.execute("PRAGMA data_version").fetchone()[0])
            == data_version,
            "terminalization_overlay_state_changed",
        )
    except (
        ReconcileRunTerminalizationProofError,
        ManagerGenerationCertificateConflictError,
        sqlite3.DatabaseError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return _terminalization_overlay_projection(
            raw_status, "terminalization_overlay_invalid"
        )
    return {
        "schema_version": "reconcile_run_terminalization.overlay.v1",
        "valid": True,
        "effective_status": "terminalized_stale",
        "is_terminal": True,
        "status_reason_code": "terminalization_overlay_valid",
        "terminalization_id_sha256": _stable_sha256(
            ["terminalization_id", ledger["terminalization_id"]]
        ),
        "source_identity_sha256": safe_receipt["source_identity_sha256"],
        "replacement_identity_sha256": safe_receipt["replacement_identity_sha256"],
        "replacement_count": safe_receipt["replacement_count"],
        "ledger_hash": ledger["ledger_hash"],
        "timeline_event_hash": ledger["timeline_event_hash"],
        "manager_certificate_hash": ledger["manager_certificate_hash"],
    }


def _reconcile_run_terminalization_append_fault(
    _stage: str, _conn: sqlite3.Connection
) -> None:
    """Internal fault-injection seam for the two-row atomic append."""


def _terminalization_append_receipt(
    overlay: Mapping[str, Any], *, writes_performed: bool, replayed: bool
) -> dict[str, Any]:
    return {
        "schema_version": "reconcile_run_terminalization.append.v1",
        "terminalization_id_sha256": overlay["terminalization_id_sha256"],
        "source_identity_sha256": overlay["source_identity_sha256"],
        "replacement_identity_sha256": overlay["replacement_identity_sha256"],
        "replacement_count": int(overlay["replacement_count"]),
        "manager_certificate_hash": overlay["manager_certificate_hash"],
        "ledger_hash": overlay["ledger_hash"],
        "timeline_event_hash": overlay["timeline_event_hash"],
        "writes_performed": bool(writes_performed),
        "replayed": bool(replayed),
        "server_derived": True,
    }


def _terminalization_require_replay_scope(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    snapshot_id: str,
    backlog_id: str,
    task_id: str,
) -> None:
    row = conn.execute(
        "SELECT event.backlog_id,event.task_id "
        "FROM graph_reconcile_run_terminalizations AS ledger "
        "JOIN task_timeline_events AS event ON event.id=ledger.timeline_event_id "
        "WHERE ledger.project_id=? AND ledger.source_run_id=? "
        "AND ledger.source_snapshot_id=?",
        (project_id, run_id, snapshot_id),
    ).fetchone()
    _terminalization_require(
        row is not None
        and row["backlog_id"] == backlog_id
        and row["task_id"] == task_id,
        "terminalization_replay_scope_mismatch",
    )


def record_reconcile_run_terminalization(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str,
    snapshot_id: str,
    backlog_id: str,
    task_id: str,
    manager_certificate: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one neutral timeline fact and overlay after caller prewarm."""

    from . import task_timeline
    from .db import sqlite_write_lock

    if conn.in_transaction:
        raise RuntimeError("terminalization append requires a clean transaction")
    project = str(project_id or "")
    source_run = str(run_id or "")
    source_snapshot = str(snapshot_id or "")
    _terminalization_manager_certificate(conn, project, manager_certificate)
    existing = reconcile_run_terminalization_overlay(
        conn, project, run_id=source_run, snapshot_id=source_snapshot
    )
    if existing.get("valid") is True:
        _terminalization_require_replay_scope(
            conn,
            project,
            source_run,
            source_snapshot,
            str(backlog_id or ""),
            str(task_id or ""),
        )
        return _terminalization_append_receipt(
            existing, writes_performed=False, replayed=True
        )
    if conn.execute(
        "SELECT 1 FROM graph_reconcile_run_terminalizations "
        "WHERE project_id=? AND source_run_id=? AND source_snapshot_id=?",
        (project, source_run, source_snapshot),
    ).fetchone():
        raise ReconcileRunTerminalizationProofError(
            "terminalization_existing_overlay_invalid"
        )

    inserted_event: dict[str, Any] | None = None
    with sqlite_write_lock():
        try:
            conn.execute("BEGIN IMMEDIATE")
            _terminalization_manager_certificate(
                conn, project, manager_certificate
            )
            existing = reconcile_run_terminalization_overlay(
                conn, project, run_id=source_run, snapshot_id=source_snapshot
            )
            if existing.get("valid") is True:
                _terminalization_require_replay_scope(
                    conn,
                    project,
                    source_run,
                    source_snapshot,
                    str(backlog_id or ""),
                    str(task_id or ""),
                )
                conn.rollback()
                return _terminalization_append_receipt(
                    existing, writes_performed=False, replayed=True
                )
            if conn.execute(
                "SELECT 1 FROM graph_reconcile_run_terminalizations "
                "WHERE project_id=? AND source_run_id=? AND source_snapshot_id=?",
                (project, source_run, source_snapshot),
            ).fetchone():
                raise ReconcileRunTerminalizationProofError(
                    "terminalization_existing_overlay_invalid"
                )
            proof = reconcile_run_terminalization_proof(
                conn,
                project,
                run_id=source_run,
                snapshot_id=source_snapshot,
                manager_certificate=manager_certificate,
            )
            safe = reconcile_run_terminalization_safe_receipt(proof)
            payload = json.loads(proof._canonical)
            source = payload["source"]
            replacement = payload["replacement"]
            certificate = payload["manager_certificate"]
            source_metric = conn.execute(
                "SELECT commit_sha FROM reconcile_run_metrics "
                "WHERE project_id=? AND run_id=? AND snapshot_id=?",
                (project, source_run, source_snapshot),
            ).fetchone()
            _terminalization_require(
                source_metric is not None, "terminalization_metric_missing"
            )
            terminalization_id = "terminalization-" + hashlib.sha256(
                f"{project}\0{source_run}\0{source_snapshot}".encode("utf-8")
            ).hexdigest()
            terminalization_id_sha256 = _stable_sha256(
                ["terminalization_id", terminalization_id]
            )
            inserted_event = task_timeline.record_reconcile_run_terminalization_event(
                conn,
                project_id=project,
                backlog_id=str(backlog_id or ""),
                task_id=str(task_id or ""),
                commit_sha=str(source_metric["commit_sha"]),
                terminalization_id_sha256=terminalization_id_sha256,
                source_identity_sha256=safe["source_identity_sha256"],
                source_fingerprint=safe["source_fingerprint"],
                replacement_identity_sha256=safe["replacement_identity_sha256"],
                replacement_fingerprint=safe["replacement_fingerprint"],
                manager_certificate_hash=safe["manager_certificate_hash"],
                proof_sha256=safe["proof_sha256"],
            )
            _reconcile_run_terminalization_append_fault("after_timeline", conn)
            timeline = dict(conn.execute(
                "SELECT * FROM task_timeline_events WHERE id=?",
                (inserted_event["id"],),
            ).fetchone())
            ledger = {
                "terminalization_id": terminalization_id,
                "project_id": project,
                "source_run_id": source["run_id"],
                "source_snapshot_id": source["snapshot_id"],
                "source_metric_identity_sequence": source["metric_identity_sequence"],
                "source_raw_status": source["raw_status"],
                "source_fingerprint": source["fingerprint"],
                "replacement_proof_kind": replacement["proof_kind"],
                "replacement_run_id": replacement["run_id"],
                "replacement_snapshot_id": replacement["snapshot_id"],
                "replacement_metric_identity_sequence": replacement[
                    "metric_identity_sequence"
                ],
                "replacement_raw_status": replacement["raw_status"],
                "replacement_fingerprint": replacement["fingerprint"],
                "manager_certificate_id": certificate["certificate_id"],
                "manager_certificate_hash": certificate["certificate_hash"],
                "timeline_event_id": inserted_event["id"],
                "timeline_event_hash": _terminalization_timeline_hash(timeline),
                "terminal_status": "terminalized_stale",
                "created_at": timeline["created_at"],
                "ledger_hash": "",
            }
            ledger["ledger_hash"] = _terminalization_ledger_hash(ledger)
            columns = list(ledger)
            conn.execute(
                "INSERT INTO graph_reconcile_run_terminalizations "
                f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [ledger[column] for column in columns],
            )
            _reconcile_run_terminalization_append_fault("after_ledger", conn)
            proof_after = reconcile_run_terminalization_proof(
                conn,
                project,
                run_id=source_run,
                snapshot_id=source_snapshot,
                manager_certificate=manager_certificate,
            )
            _terminalization_require(
                proof_after._canonical == proof._canonical
                and hmac.compare_digest(proof_after._seal, proof._seal),
                "terminalization_state_changed",
            )
            overlay = reconcile_run_terminalization_overlay(
                conn, project, run_id=source_run, snapshot_id=source_snapshot
            )
            _terminalization_require(
                overlay.get("valid") is True,
                "terminalization_precommit_overlay_invalid",
            )
            _reconcile_run_terminalization_append_fault("before_commit", conn)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    if inserted_event is not None:
        task_timeline.run_post_commit_hooks(conn, inserted_event)
    return _terminalization_append_receipt(
        overlay, writes_performed=True, replayed=False
    )


def current_full_reconcile_state(
    conn: sqlite3.Connection,
    project_id: str,
    merged_commit_sha: str,
    *,
    current_canonical_commit_sha: str = "",
    reconcile_target_commit_sha: str = "",
    qa_event_id: int = 0,
    qa_event_created_at: str = "",
    qa_source_ref: str = "",
    qa_acceptance_created_at: str = "",
    qa_acceptance_revision: int = 0,
    qa_contract_runtime_verified: bool = False,
    merge_event_id: int = 0,
    merge_event_created_at: str = "",
    reconcile_event_id: int = 0,
    reconcile_event_created_at: str = "",
    expected_contract_execution_id: str = "",
    expected_task_id: str = "",
    expected_runtime_context_id: str = "",
    expected_parent_task_id: str = "",
    expected_merge_queue_id: str = "",
    trusted_contract_execution_lineage_verified: bool = False,
    reconcile_task_id: str = "",
    reconcile_runtime_context_id: str = "",
    allow_taskless: bool = False,
) -> dict[str, Any]:
    """Return DB-backed current-full state after one historical merge.

    A current-full reconcile always materializes canonical code at the time it
    runs.  That reconciled commit may be a descendant of
    ``merged_commit_sha`` when later ordered commits landed before
    reconciliation, and it may itself be an ancestor of the current canonical
    commit after later graph activations.  Durable provenance binds the actual
    reconcile target while active-snapshot checks bind the current canonical
    target; merge ordering and scope bind the historical merge separately.
    """

    ensure_schema(conn)
    project_id = str(project_id or "").strip()
    merged_commit_sha = str(merged_commit_sha or "").strip().lower()
    current_canonical_commit_sha = str(
        current_canonical_commit_sha or merged_commit_sha
    ).strip().lower()
    reconcile_target_commit_sha = str(
        reconcile_target_commit_sha or ""
    ).strip().lower()
    active = get_active_graph_snapshot(conn, project_id) or {}
    active_snapshot_id = str(active.get("snapshot_id") or "").strip()
    active_snapshot_commit = str(active.get("commit_sha") or "").strip().lower()
    active_snapshot_matches_current_canonical = bool(
        active_snapshot_commit
        and active_snapshot_commit == current_canonical_commit_sha
        and str(active.get("status") or "").strip() == SNAPSHOT_STATUS_ACTIVE
    )
    active_marker = _snapshot_notes(active).get("current_full_reconcile")
    active_marker = (
        dict(active_marker) if isinstance(active_marker, Mapping) else {}
    )
    active_current_full = _current_full_snapshot_provenance_binding(
        conn,
        project_id,
        active,
    )
    pending_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM pending_scope_reconcile
            WHERE project_id = ? AND status IN (?, ?, ?)
            """,
            (
                project_id,
                PENDING_STATUS_QUEUED,
                PENDING_STATUS_RUNNING,
                PENDING_STATUS_FAILED,
            ),
        ).fetchone()[0]
    )
    try:
        requested_reconcile_event_id = int(reconcile_event_id or 0)
    except (TypeError, ValueError):
        requested_reconcile_event_id = 0
    active_marker_target = str(
        active_marker.get("target_commit_sha") or ""
    ).strip().lower()
    provenance_id = (
        str(active_marker.get("provenance_id") or "").strip()
        if active_marker_target == current_canonical_commit_sha
        else ""
    )
    provenance_row = None
    if provenance_id and not reconcile_target_commit_sha:
        provenance_row = conn.execute(
            """
            SELECT *
            FROM graph_current_full_reconcile_provenance
            WHERE provenance_id = ? AND project_id = ?
              AND snapshot_id = ? AND target_commit_sha = ?
            """,
            (
                provenance_id,
                project_id,
                active_snapshot_id,
                current_canonical_commit_sha,
            ),
        ).fetchone()
    if provenance_row is None and requested_reconcile_event_id > 0:
        target_commit = (
            reconcile_target_commit_sha or current_canonical_commit_sha
        )
        provenance_rows = conn.execute(
            """
            SELECT *
            FROM graph_current_full_reconcile_provenance
            WHERE project_id = ? AND target_commit_sha = ?
              AND reconcile_event_id = ?
            ORDER BY created_at DESC, provenance_id DESC
            LIMIT 2
            """,
            (
                project_id,
                target_commit,
                requested_reconcile_event_id,
            ),
        ).fetchall()
        if len(provenance_rows) == 1:
            provenance_row = provenance_rows[0]
    if (
        provenance_row is None
        and requested_reconcile_event_id > 0
        and not reconcile_target_commit_sha
        and active_snapshot_matches_current_canonical
    ):
        # A canonically completed reconcile remains durable after a later
        # current-full snapshot becomes active.  Resolve the unique provenance
        # row by its immutable reconcile event, while retaining the independent
        # current-HEAD active-snapshot check above.  Requiring the requested
        # canonical commit to be the active commit prevents a forged/stale
        # target from widening this historical lookup.
        provenance_rows = conn.execute(
            """
            SELECT *
            FROM graph_current_full_reconcile_provenance
            WHERE project_id = ? AND reconcile_event_id = ?
            ORDER BY created_at DESC, provenance_id DESC
            LIMIT 2
            """,
            (project_id, requested_reconcile_event_id),
        ).fetchall()
        if len(provenance_rows) == 1:
            provenance_row = provenance_rows[0]
    if provenance_row is None and requested_reconcile_event_id <= 0:
        target_commit = (
            reconcile_target_commit_sha or current_canonical_commit_sha
        )
        provenance_rows = conn.execute(
            """
            SELECT *
            FROM graph_current_full_reconcile_provenance
            WHERE project_id = ? AND target_commit_sha = ?
            ORDER BY created_at DESC, provenance_id DESC
            LIMIT 2
            """,
            (project_id, target_commit),
        ).fetchall()
        if len(provenance_rows) == 1:
            provenance_row = provenance_rows[0]
    provenance = dict(provenance_row) if provenance_row else {}
    reconciled_commit_sha = str(
        provenance.get("target_commit_sha") or current_canonical_commit_sha
    ).strip().lower()
    reconcile_snapshot_id = str(provenance.get("snapshot_id") or "").strip()
    reconcile_snapshot_row = None
    if reconcile_snapshot_id:
        reconcile_snapshot_row = conn.execute(
            """
            SELECT *
            FROM graph_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (project_id, reconcile_snapshot_id),
        ).fetchone()
    reconcile_snapshot = (
        dict(reconcile_snapshot_row) if reconcile_snapshot_row else {}
    )
    if (
        not reconcile_snapshot
        and active_snapshot_commit == reconciled_commit_sha
    ):
        reconcile_snapshot = dict(active)
        reconcile_snapshot_id = active_snapshot_id
    marker = _snapshot_notes(reconcile_snapshot).get("current_full_reconcile")
    marker = dict(marker) if isinstance(marker, Mapping) else {}
    marker_target_commit = str(marker.get("target_commit_sha") or "").strip().lower()
    reconcile_snapshot_commit = str(
        reconcile_snapshot.get("commit_sha") or ""
    ).strip().lower()
    reconcile_snapshot_status = str(
        reconcile_snapshot.get("status") or ""
    ).strip()
    try:
        stored_marker = json.loads(str(provenance.get("marker_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_marker = {}
    try:
        stored_route_evidence = json.loads(
            str(provenance.get("route_evidence_json") or "{}")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_route_evidence = {}
    marker_core = dict(marker)
    marker_hash = str(marker_core.pop("provenance_hash", "") or "").strip()
    provenance_hash = str(provenance.get("provenance_hash") or "").strip()
    marker_reconcile_event_id = 0
    provenance_reconcile_event_id = 0
    marker_runtime_context_scope = (
        marker.get("runtime_context_scope")
        if isinstance(marker.get("runtime_context_scope"), Mapping)
        else {}
    )
    route_runtime_context_scope = (
        stored_route_evidence.get("runtime_context_scope")
        if isinstance(
            stored_route_evidence.get("runtime_context_scope"),
            Mapping,
        )
        else {}
    )
    runtime_context_scope_link_verified = bool(
        (
            not marker_runtime_context_scope
            and not route_runtime_context_scope
        )
        or (
            marker_runtime_context_scope
            and route_runtime_context_scope
            and {
                key: str(marker_runtime_context_scope.get(key) or "").strip()
                for key in (
                    "project_id",
                    "backlog_id",
                    "task_id",
                    "parent_task_id",
                    "runtime_context_id",
                    "merge_queue_id",
                    "contract_execution_id",
                )
                if str(marker_runtime_context_scope.get(key) or "").strip()
            }
            == {
                key: str(route_runtime_context_scope.get(key) or "").strip()
                for key in (
                    "project_id",
                    "backlog_id",
                    "task_id",
                    "parent_task_id",
                    "runtime_context_id",
                    "merge_queue_id",
                    "contract_execution_id",
                )
                if str(route_runtime_context_scope.get(key) or "").strip()
            }
            and marker_runtime_context_scope.get("server_derived") is True
            and route_runtime_context_scope.get("server_derived") is True
            and marker_runtime_context_scope.get("source")
            == "parallel_branch_runtime_context"
            and route_runtime_context_scope.get("source")
            == "parallel_branch_runtime_context"
        )
    )
    try:
        marker_reconcile_event_id = int(marker.get("reconcile_event_id") or 0)
        provenance_reconcile_event_id = int(
            provenance.get("reconcile_event_id") or 0
        )
    except (TypeError, ValueError):
        pass
    marker_verified = bool(
        marker
        and provenance
        and marker == stored_marker
        and marker_hash
        and marker_hash == provenance_hash
        and _stable_sha256(marker_core) == marker_hash
        and str(marker.get("schema_version") or "").strip()
        == "current_full_reconcile.provenance.v2"
        and marker.get("normal_update_path") is True
        and marker.get("activate") is True
        and marker_target_commit == reconciled_commit_sha
        and str(marker.get("snapshot_id") or "").strip()
        == reconcile_snapshot_id
        and str(marker.get("protected_action") or "").strip()
        == "graph_current_full_reconcile"
        and str(marker.get("protected_entrypoint") or "").strip()
        == "POST /api/graph-governance/{project_id}/reconcile/current-full"
        and str(provenance.get("protected_action") or "").strip()
        == str(marker.get("protected_action") or "").strip()
        and str(provenance.get("protected_entrypoint") or "").strip()
        == str(marker.get("protected_entrypoint") or "").strip()
        and str(provenance.get("request_id") or "").strip()
        == str(marker.get("request_id") or "").strip()
        and bool(str(marker.get("request_id") or "").strip())
        and str(provenance.get("request_started_at") or "").strip()
        == str(marker.get("request_started_at") or "").strip()
        and _timestamp_value(marker.get("request_started_at")) is not None
        and str(provenance.get("marker_created_at") or "").strip()
        == str(marker.get("marker_created_at") or "").strip()
        and provenance_reconcile_event_id > 0
        and provenance_reconcile_event_id == marker_reconcile_event_id
        and str(provenance.get("reconcile_event_created_at") or "").strip()
        == str(marker.get("reconcile_event_created_at") or "").strip()
        and isinstance(stored_route_evidence, Mapping)
        and stored_route_evidence == marker.get("route_evidence")
        and stored_route_evidence.get("schema_version")
        == "graph_current_full_reconcile.route_evidence.v1"
        and bool(
            str(stored_route_evidence.get("authenticated_role") or "").strip()
        )
        and bool(
            str(stored_route_evidence.get("authentication_source") or "").strip()
        )
        and stored_route_evidence.get("raw_route_token_persisted") is False
        and stored_route_evidence.get("protected_action")
        == "graph_current_full_reconcile"
        and runtime_context_scope_link_verified
    )
    try:
        qa_event_id = int(qa_event_id)
        merge_event_id = int(merge_event_id)
        reconcile_event_id = int(reconcile_event_id)
    except (TypeError, ValueError):
        qa_event_id = 0
        merge_event_id = 0
        reconcile_event_id = 0
    qa_time = _timestamp_value(qa_event_created_at)
    qa_acceptance_time = _timestamp_value(qa_acceptance_created_at)
    merge_time = _timestamp_value(merge_event_created_at)
    reconcile_time = _timestamp_value(reconcile_event_created_at)
    marker_time = _timestamp_value(marker.get("marker_created_at"))
    qa_completed_line_ref = bool(
        re.fullmatch(
            r"contract_runtime:[^:]+:completed_lines:\d+",
            str(qa_source_ref or "").strip(),
        )
    )
    qa_source_ref = str(qa_source_ref or "").strip()
    canonical_qa_acceptance_claimed = bool(
        qa_source_ref.startswith("contract_runtime:")
        or str(qa_acceptance_created_at or "").strip()
        or int(qa_acceptance_revision or 0)
        or qa_contract_runtime_verified
    )
    timeline_qa_acceptance_claimed = bool(
        qa_source_ref.startswith("timeline:")
        or qa_event_id
        or str(qa_event_created_at or "").strip()
    )
    qa_acceptance_claimed = bool(
        canonical_qa_acceptance_claimed or timeline_qa_acceptance_claimed
    )
    canonical_qa_acceptance = bool(
        qa_contract_runtime_verified
        and qa_completed_line_ref
        and int(qa_acceptance_revision or 0) > 0
        and qa_acceptance_time is not None
        and qa_event_id == 0
        and not str(qa_event_created_at or "").strip()
    )
    timeline_qa_acceptance = bool(
        not canonical_qa_acceptance_claimed
        and qa_event_id > 0
        and qa_time is not None
    )
    qa_authority_mode = (
        "canonical_contract_runtime_acceptance"
        if canonical_qa_acceptance
        else "timeline_event"
        if timeline_qa_acceptance
        else ""
    )
    qa_order_required = qa_acceptance_claimed
    durable_order_verified = bool(
        merge_event_id > 0
        and reconcile_event_id > merge_event_id
        and provenance_reconcile_event_id == reconcile_event_id
        and str(provenance.get("reconcile_event_created_at") or "").strip()
        == str(reconcile_event_created_at or "").strip()
        and merge_time is not None
        and reconcile_time is not None
        and marker_time is not None
        # Timeline ids/revisions carry strict position. Timestamps are emitted
        # at one-second resolution, so they may be equal without being out of
        # order; only a backwards timestamp is invalid.
        and reconcile_time >= merge_time
        and marker_time >= reconcile_time
        and (
            not qa_order_required
            or (
                canonical_qa_acceptance
                and qa_acceptance_time <= merge_time
            )
            or (
                timeline_qa_acceptance
                and merge_event_id > qa_event_id
                and qa_time <= merge_time
            )
        )
    )
    route_scope = (
        stored_route_evidence.get("route_token_scope")
        if isinstance(stored_route_evidence.get("route_token_scope"), Mapping)
        else {}
    )
    expected_contract_execution_id = str(
        expected_contract_execution_id or ""
    ).strip()
    expected_task_id = str(expected_task_id or "").strip()
    expected_runtime_context_id = str(expected_runtime_context_id or "").strip()
    expected_parent_task_id = str(expected_parent_task_id or "").strip()
    expected_merge_queue_id = str(expected_merge_queue_id or "").strip()
    route_task_id = str(route_scope.get("task_id") or "").strip()
    task_claims = {
        str(value or "").strip()
        for value in (
            reconcile_task_id,
            stored_route_evidence.get("task_id"),
            marker_runtime_context_scope.get("task_id"),
            route_runtime_context_scope.get("task_id"),
        )
        if str(value or "").strip()
    }
    contract_execution_claims = {
        str(value or "").strip()
        for value in (
            stored_route_evidence.get("contract_execution_id"),
            marker_runtime_context_scope.get("contract_execution_id"),
            route_runtime_context_scope.get("contract_execution_id"),
        )
        if str(value or "").strip()
    }
    canonical_runtime_parent_claims = {
        str(value or "").strip()
        for value in (
            marker_runtime_context_scope.get("parent_task_id"),
            route_runtime_context_scope.get("parent_task_id"),
        )
        if str(value or "").strip()
    }
    trusted_historical_dispatch = bool(
        trusted_contract_execution_lineage_verified
        and expected_parent_task_id
        and expected_parent_task_id != expected_contract_execution_id
    )
    if expected_contract_execution_id:
        if route_task_id:
            if route_task_id == expected_task_id:
                # Current mf_parallel reconcile routes are worker-task scoped.
                # That route proves the task dimension only. The corresponding
                # contract is derived exclusively from the sealed, duplicated
                # server-owned runtime-context parent scope; caller-supplied
                # reconcile arguments cannot manufacture this relationship.
                task_claims.add(route_task_id)
                if (
                    runtime_context_scope_link_verified
                    and marker_runtime_context_scope
                    and route_runtime_context_scope
                    and canonical_runtime_parent_claims
                    == {expected_contract_execution_id}
                ):
                    contract_execution_claims.add(
                        expected_contract_execution_id
                    )
                elif (
                    trusted_historical_dispatch
                    and runtime_context_scope_link_verified
                    and canonical_runtime_parent_claims
                    in (set(), {expected_parent_task_id})
                ):
                    # Historical mf_parallel runtime contexts used backlog_id
                    # as parent_task_id.  The caller cannot opt into this
                    # bridge: the server must first prove the exact sealed
                    # ContractRuntime dispatch lineage for the runtime/task.
                    contract_execution_claims.add(
                        expected_contract_execution_id
                    )
            else:
                # Preserve contract-scoped routes and make an unrelated route
                # task a conflicting contract claim so the check fails closed.
                contract_execution_claims.add(route_task_id)
    elif route_task_id:
        # Legacy/non-contract callers use route task scope as the worker task.
        task_claims.add(route_task_id)
    if (
        expected_contract_execution_id
        and expected_contract_execution_id == expected_task_id
        and not contract_execution_claims
        and task_claims == {expected_task_id}
    ):
        # Older single-lane provenance used one shared task identity for both
        # the contract execution and its worker. Preserve that exact legacy
        # shape without weakening separated CEX/worker scope verification.
        contract_execution_claims.add(expected_contract_execution_id)
    runtime_context_claims = {
        str(value or "").strip()
        for value in (
            reconcile_runtime_context_id,
            stored_route_evidence.get("runtime_context_id"),
            route_scope.get("runtime_context_id"),
            marker_runtime_context_scope.get("runtime_context_id"),
            route_runtime_context_scope.get("runtime_context_id"),
        )
        if str(value or "").strip()
    }
    merge_queue_claims = {
        str(value or "").strip()
        for value in (
            marker_runtime_context_scope.get("merge_queue_id"),
            route_runtime_context_scope.get("merge_queue_id"),
        )
        if str(value or "").strip()
    }

    def scope_dimension_verified(
        expected: str,
        claims: set[str],
        *,
        allow_missing: bool,
    ) -> bool:
        if not expected:
            return not claims or len(claims) == 1
        if claims:
            return claims == {expected}
        return bool(allow_missing)

    contract_execution_scope_verified = scope_dimension_verified(
        expected_contract_execution_id,
        contract_execution_claims,
        allow_missing=False,
    )
    task_scope_verified = scope_dimension_verified(
        expected_task_id,
        task_claims,
        allow_missing=allow_taskless,
    )
    runtime_context_scope_verified = scope_dimension_verified(
        expected_runtime_context_id,
        runtime_context_claims,
        allow_missing=allow_taskless,
    )
    parent_task_scope_verified = scope_dimension_verified(
        expected_parent_task_id,
        canonical_runtime_parent_claims,
        allow_missing=trusted_historical_dispatch,
    )
    merge_queue_scope_verified = scope_dimension_verified(
        expected_merge_queue_id,
        merge_queue_claims,
        allow_missing=trusted_historical_dispatch,
    )
    provenance_scope_verified = bool(
        contract_execution_scope_verified
        and task_scope_verified
        and runtime_context_scope_verified
        and parent_task_scope_verified
        and merge_queue_scope_verified
    )
    reconcile_snapshot_verified = bool(
        reconcile_snapshot_id
        and reconcile_snapshot_commit == reconciled_commit_sha
        and reconcile_snapshot_status
        in {SNAPSHOT_STATUS_ACTIVE, SNAPSHOT_STATUS_SUPERSEDED}
    )
    active_snapshot_verified = bool(
        active_snapshot_id
        and str(active.get("status") or "").strip() == SNAPSHOT_STATUS_ACTIVE
        and active_snapshot_commit == current_canonical_commit_sha
        and active_current_full.get("verified") is True
    )
    db_verified = bool(
        project_id
        and merged_commit_sha
        and current_canonical_commit_sha
        and reconciled_commit_sha
        and marker_verified
        and durable_order_verified
        and provenance_scope_verified
        and reconcile_snapshot_verified
        and pending_count == 0
    )
    return {
        "schema_version": "graph_snapshot_store.current_full_reconcile_state.v1",
        "source": "graph_snapshot_store.current_full_reconcile_state",
        "db_verified": db_verified,
        "project_id": project_id,
        "merged_commit_sha": merged_commit_sha,
        "current_canonical_commit_sha": current_canonical_commit_sha,
        "reconciled_commit_sha": reconciled_commit_sha,
        "active_snapshot_id": active_snapshot_id,
        "active_snapshot_commit": active_snapshot_commit,
        "active_snapshot_status": str(active.get("status") or "").strip(),
        "active_snapshot_verified": active_snapshot_verified,
        "active_snapshot_current_full_reconcile_verified": bool(
            active_current_full.get("verified")
        ),
        "active_snapshot_current_full_provenance_id": str(
            active_current_full.get("provenance_id") or ""
        ),
        "active_snapshot_current_full_provenance_target_commit": str(
            active_current_full.get("provenance_target_commit") or ""
        ),
        "active_snapshot_current_full_provenance_hash": str(
            active_current_full.get("provenance_hash") or ""
        ),
        "active_snapshot_current_full_reconcile_event_id": int(
            active_current_full.get("reconcile_event_id") or 0
        ),
        "active_snapshot_current_full_reconcile_event_created_at": str(
            active_current_full.get("reconcile_event_created_at") or ""
        ),
        "active_snapshot_current_full_task_id": str(
            active_current_full.get("task_id") or ""
        ),
        "active_snapshot_current_full_runtime_context_id": str(
            active_current_full.get("runtime_context_id") or ""
        ),
        "active_snapshot_current_full_contract_execution_id": str(
            active_current_full.get("contract_execution_id") or ""
        ),
        "active_snapshot_current_full_runtime_scope_verified": bool(
            active_current_full.get(
                "runtime_context_scope_link_verified"
            )
        ),
        "reconcile_snapshot_id": reconcile_snapshot_id,
        "reconcile_snapshot_commit": reconcile_snapshot_commit,
        "reconcile_snapshot_status": reconcile_snapshot_status,
        "reconcile_snapshot_verified": reconcile_snapshot_verified,
        "current_full_reconcile": bool(marker),
        "current_full_reconcile_marker": marker,
        "current_full_reconcile_marker_verified": marker_verified,
        "current_full_reconcile_provenance": provenance,
        "provenance_verified": marker_verified,
        "durable_order_verified": durable_order_verified,
        "qa_event_id": qa_event_id,
        "qa_event_created_at": str(qa_event_created_at or "").strip(),
        "qa_source_ref": qa_source_ref,
        "qa_acceptance_created_at": str(
            qa_acceptance_created_at or ""
        ).strip(),
        "qa_acceptance_revision": int(qa_acceptance_revision or 0),
        "qa_contract_runtime_verified": bool(qa_contract_runtime_verified),
        "qa_acceptance_claimed": qa_acceptance_claimed,
        "canonical_qa_acceptance_claimed": canonical_qa_acceptance_claimed,
        "timeline_qa_acceptance_claimed": timeline_qa_acceptance_claimed,
        "qa_completed_line_ref_verified": qa_completed_line_ref,
        "canonical_qa_acceptance_verified": canonical_qa_acceptance,
        "timeline_qa_acceptance_verified": timeline_qa_acceptance,
        "qa_authority_mode": qa_authority_mode,
        "merge_event_id": merge_event_id,
        "merge_event_created_at": str(merge_event_created_at or "").strip(),
        "reconcile_event_id": reconcile_event_id,
        "reconcile_event_created_at": str(
            reconcile_event_created_at or ""
        ).strip(),
        "provenance_scope_verified": provenance_scope_verified,
        "contract_execution_scope_verified": (
            contract_execution_scope_verified
        ),
        "task_scope_verified": task_scope_verified,
        "runtime_context_scope_verified": runtime_context_scope_verified,
        "parent_task_scope_verified": parent_task_scope_verified,
        "merge_queue_scope_verified": merge_queue_scope_verified,
        "runtime_context_scope_link_verified": (
            runtime_context_scope_link_verified
        ),
        "expected_contract_execution_id": expected_contract_execution_id,
        "expected_task_id": expected_task_id,
        "expected_runtime_context_id": expected_runtime_context_id,
        "expected_parent_task_id": expected_parent_task_id,
        "expected_merge_queue_id": expected_merge_queue_id,
        "trusted_contract_execution_lineage_verified": bool(
            trusted_contract_execution_lineage_verified
        ),
        "reconcile_task_id": str(reconcile_task_id or "").strip(),
        "reconcile_runtime_context_id": str(
            reconcile_runtime_context_id or ""
        ).strip(),
        "allow_taskless": bool(allow_taskless),
        "strategy": "current_full_reconcile" if marker else "",
        "pending_scope_reconcile_count": pending_count,
        "pending_scope_reconcile_zero": pending_count == 0,
        "source_ref": (
            f"graph_snapshot:{reconcile_snapshot_id}"
            if reconcile_snapshot_id
            else ""
        ),
        "active_source_ref": (
            f"graph_snapshot:{active_snapshot_id}" if active_snapshot_id else ""
        ),
    }


def get_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    return dict(row) if row else None


def list_graph_snapshots(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: Iterable[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM graph_snapshots WHERE project_id = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    sql += " ORDER BY created_at DESC, snapshot_id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 50), 500)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def list_graph_snapshots_for_commit(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
    *,
    statuses: Iterable[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, commit_sha]
    sql = "SELECT * FROM graph_snapshots WHERE project_id = ? AND commit_sha = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    sql += """
        ORDER BY
          CASE status
            WHEN 'active' THEN 0
            WHEN 'superseded' THEN 1
            WHEN 'candidate' THEN 2
            WHEN 'finalizing' THEN 3
            ELSE 4
          END,
          created_at DESC,
          snapshot_id DESC
        LIMIT ?
    """
    params.append(max(1, min(int(limit or 20), 100)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_graph_snapshot_for_commit(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
) -> dict[str, Any] | None:
    rows = list_graph_snapshots_for_commit(
        conn,
        project_id,
        commit_sha,
        statuses=[
            SNAPSHOT_STATUS_ACTIVE,
            SNAPSHOT_STATUS_SUPERSEDED,
            SNAPSHOT_STATUS_CANDIDATE,
            SNAPSHOT_STATUS_FINALIZING,
        ],
        limit=1,
    )
    return rows[0] if rows else None


def list_graph_snapshot_nodes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
    layer: str = "",
    kind: str = "",
    include_semantic: bool = True,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    semantic_join = include_semantic and _table_exists(conn, "graph_semantic_nodes")
    semantic_job_join = include_semantic and _table_exists(conn, "graph_semantic_jobs")
    select_columns = """
        n.node_id, n.layer, n.title, n.kind, n.primary_files_json,
        n.secondary_files_json, n.test_files_json, n.metadata_json
    """
    joins = ""
    if semantic_join:
        select_columns += """,
        s.status AS semantic_status,
        s.feature_hash AS semantic_feature_hash,
        s.file_hashes_json AS semantic_file_hashes_json,
        s.semantic_json AS semantic_json,
        s.feedback_round AS semantic_feedback_round,
        s.batch_index AS semantic_batch_index,
        s.updated_at AS semantic_updated_at
        """
        joins += """
        LEFT JOIN graph_semantic_nodes s
          ON s.project_id = n.project_id
         AND s.snapshot_id = n.snapshot_id
         AND s.node_id = n.node_id
        """
    if semantic_job_join:
        select_columns += """,
        j.status AS semantic_job_status,
        j.feature_hash AS semantic_job_feature_hash,
        j.attempt_count AS semantic_job_attempt_count,
        j.worker_id AS semantic_job_worker_id,
        j.claim_id AS semantic_job_claim_id,
        j.claimed_at AS semantic_job_claimed_at,
        j.lease_expires_at AS semantic_job_lease_expires_at,
        j.claimed_by AS semantic_job_claimed_by,
        j.last_error AS semantic_job_last_error,
        j.updated_at AS semantic_job_updated_at
        """
        joins += """
        LEFT JOIN graph_semantic_jobs j
          ON j.project_id = n.project_id
         AND j.snapshot_id = n.snapshot_id
         AND j.node_id = n.node_id
        """
    sql = f"""
        SELECT {select_columns}
        FROM graph_nodes_index n
        {joins}
        WHERE n.project_id = ? AND n.snapshot_id = ?
    """
    if layer:
        sql += " AND n.layer = ?"
        params.append(layer)
    if kind:
        sql += " AND n.kind = ?"
        params.append(kind)
    sql += " ORDER BY n.node_id LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        node = {
            "node_id": row["node_id"],
            "layer": row["layer"],
            "title": row["title"],
            "kind": row["kind"],
            "primary_files": _decode_json(row["primary_files_json"], []),
            "secondary_files": _decode_json(row["secondary_files_json"], []),
            "test_files": _decode_json(row["test_files_json"], []),
            "metadata": _decode_json(row["metadata_json"], {}),
        }
        if include_semantic:
            node["semantic"] = _semantic_overlay_from_node_row(row)
        nodes.append(node)
    return nodes


def list_graph_snapshot_edges(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 500,
    offset: int = 0,
    edge_type: str = "",
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    sql = """
        SELECT src, dst, edge_type, direction, evidence_json
        FROM graph_edges_index
        WHERE project_id = ? AND snapshot_id = ?
    """
    if edge_type:
        sql += " AND edge_type = ?"
        params.append(edge_type)
    sql += " ORDER BY src, dst, edge_type LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 500), 2000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "src": row["src"],
            "dst": row["dst"],
            "edge_type": row["edge_type"],
            "direction": row["direction"],
            "evidence": _decode_json(row["evidence_json"], {}),
        }
        for row in rows
    ]


def summarize_file_inventory_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compact dashboard summary for snapshot file inventory rows."""
    row_list = list(rows)
    by_kind: dict[str, int] = {}
    by_scan_status: dict[str, int] = {}
    by_graph_status: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    pending: list[str] = []
    for row in row_list:
        kind = str(row.get("file_kind") or "")
        scan = str(row.get("scan_status") or "")
        graph = str(row.get("graph_status") or "")
        decision = str(row.get("decision") or "")
        if kind:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        if scan:
            by_scan_status[scan] = by_scan_status.get(scan, 0) + 1
        if graph:
            by_graph_status[graph] = by_graph_status.get(graph, 0) + 1
        if decision:
            by_decision[decision] = by_decision.get(decision, 0) + 1
        if scan in {"orphan", "pending_decision", "error"} or graph in {"unmapped", "error"}:
            path = str(row.get("path") or "")
            if path:
                pending.append(path)
    return {
        "total": len(row_list),
        "by_kind": dict(sorted(by_kind.items())),
        "by_scan_status": dict(sorted(by_scan_status.items())),
        "by_graph_status": dict(sorted(by_graph_status.items())),
        "by_decision": dict(sorted(by_decision.items())),
        "pending_count": len(pending),
        "pending_sample": pending[:25],
    }


def list_graph_snapshot_files(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
    file_kind: str = "",
    scan_status: str = "",
    graph_status: str = "",
    decision: str = "",
    path_contains: str = "",
    sort: str = "",
) -> dict[str, Any]:
    """List file inventory rows stored with a snapshot companion artifact."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    raw = _read_json_artifact(snapshot_companion_dir(project_id, snapshot_id) / "file_inventory.json", [])
    rows = [dict(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []

    def _matches(row: dict[str, Any]) -> bool:
        if file_kind and str(row.get("file_kind") or "") != file_kind:
            return False
        if scan_status:
            row_scan_status = str(row.get("scan_status") or "")
            if row_scan_status != scan_status:
                return False
            if scan_status == "orphan" and (row.get("attached_node_ids") or row.get("attached_to")):
                return False
        if graph_status and str(row.get("graph_status") or "") != graph_status:
            return False
        if decision and str(row.get("decision") or "") != decision:
            return False
        if path_contains and path_contains not in str(row.get("path") or ""):
            return False
        return True

    filtered = [row for row in rows if _matches(row)]
    normalized_sort = str(sort or "").strip().lower().replace("-", "_")
    if normalized_sort:
        if normalized_sort in {"path", "path_asc"}:
            filtered = sorted(filtered, key=lambda row: str(row.get("path") or ""))
        elif normalized_sort == "size_desc":
            filtered = sorted(
                filtered,
                key=lambda row: (-int(row.get("size_bytes") or 0), str(row.get("path") or "")),
            )
        elif normalized_sort == "size_asc":
            filtered = sorted(
                filtered,
                key=lambda row: (int(row.get("size_bytes") or 0), str(row.get("path") or "")),
            )
        else:
            raise ValueError(f"unsupported file inventory sort: {sort}")
    start = max(0, int(offset or 0))
    end = start + max(1, min(int(limit or 200), 1000))
    return {
        "snapshot": snapshot,
        "summary": summarize_file_inventory_rows(rows),
        "total_count": len(rows),
        "filtered_count": len(filtered),
        "sort": normalized_sort,
        "files": filtered[start:end],
    }


def _count_rows(
    conn: sqlite3.Connection,
    table: str,
    project_id: str,
    snapshot_id: str,
) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    return int(row["count"] if row else 0)


def _group_counts(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    project_id: str,
    snapshot_id: str,
) -> dict[str, int]:
    if not _table_exists(conn, table):
        return {}
    rows = conn.execute(
        f"""
        SELECT {column} AS key, COUNT(*) AS count
        FROM {table}
        WHERE project_id = ? AND snapshot_id = ?
        GROUP BY {column}
        ORDER BY {column}
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {str(row["key"] or ""): int(row["count"]) for row in rows if str(row["key"] or "")}


def _snapshot_notes(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    notes = _decode_json(snapshot.get("notes"), {})
    return notes if isinstance(notes, dict) else {}


def snapshot_materialization_provenance(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Return checkout provenance and warnings recorded for a snapshot."""
    notes = _snapshot_notes(snapshot)
    provenance = notes.get("checkout_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    raw_warnings = provenance.get("warnings") if isinstance(provenance, dict) else []
    warnings = [dict(item) for item in raw_warnings or [] if isinstance(item, dict)]
    return {
        "execution_root": provenance.get("execution_root", ""),
        "execution_root_role": provenance.get("execution_root_role", ""),
        "execution_root_is_ephemeral": bool(provenance.get("execution_root_is_ephemeral")),
        "canonical_project_identity": provenance.get("canonical_project_identity") or {},
        "git": provenance.get("git") or {},
        "warnings": warnings,
        "warning_count": len(warnings),
    }


def _latest_global_review_from_notes(notes: dict[str, Any]) -> dict[str, Any]:
    review_meta = notes.get("global_semantic_review")
    if not isinstance(review_meta, dict):
        return {}
    path = str(review_meta.get("latest_full_review_path") or "").strip()
    if not path:
        return {}
    payload = _read_json_artifact(Path(path), {})
    return payload if isinstance(payload, dict) else {}


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
        values = list(raw)
    else:
        values = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip().replace("\\", "/")
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _is_governed_feature_node(node: dict[str, Any]) -> bool:
    if str(node.get("layer") or "").upper() != "L7":
        return False
    metadata = _node_metadata(node)
    if metadata.get("exclude_as_feature") is True:
        return False
    file_role = str(metadata.get("file_role") or node.get("kind") or "").strip().lower()
    if file_role in {"package_marker", "type_contract", "entrypoint_support"}:
        return False
    return True


def _feature_coverage_picture(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    raw_features = [
        node for node in nodes
        if str(node.get("layer") or "").upper() == "L7"
    ]
    governed_features = [node for node in raw_features if _is_governed_feature_node(node)]
    doc_bound = sum(1 for node in governed_features if _string_list(node.get("secondary_files")))
    test_bound = sum(1 for node in governed_features if _string_list(node.get("test_files")))
    config_bound = sum(
        1 for node in governed_features
        if _string_list(_node_metadata(node).get("config_files"))
    )
    return {
        "raw_feature_count": len(raw_features),
        "governed_feature_count": len(governed_features),
        "excluded_feature_count": max(0, len(raw_features) - len(governed_features)),
        "doc_bound_count": doc_bound,
        "doc_coverage_ratio": _ratio(doc_bound, len(governed_features)),
        "test_bound_count": test_bound,
        "test_coverage_ratio": _ratio(test_bound, len(governed_features)),
        "config_bound_count": config_bound,
        "config_coverage_ratio": _ratio(config_bound, len(governed_features)),
    }


def _l4_asset_picture(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    l4_nodes = [
        node for node in nodes
        if str(node.get("layer") or "").upper() == "L4"
    ]
    by_kind: dict[str, int] = {}
    by_file_role: dict[str, int] = {}
    aggregate_count = 0
    no_primary_count = 0
    for node in l4_nodes:
        metadata = _node_metadata(node)
        kind = str(node.get("kind") or metadata.get("kind") or "asset").strip() or "asset"
        role = str(metadata.get("file_role") or "asset").strip() or "asset"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_file_role[role] = by_file_role.get(role, 0) + 1
        if metadata.get("aggregate_asset") is True:
            aggregate_count += 1
        if not _string_list(node.get("primary_files")):
            no_primary_count += 1
    return {
        "score_version": "l4_asset_contract_v1_role_aware",
        "score": 100.0,
        "asset_count": len(l4_nodes),
        "aggregate_asset_count": aggregate_count,
        "no_primary_asset_count": no_primary_count,
        "by_kind": dict(sorted(by_kind.items())),
        "by_file_role": dict(sorted(by_file_role.items())),
        "policy": "L4 nodes are state/contract/asset nodes; direct files may be intentionally empty and are not scored as L7 feature coverage gaps.",
    }


def _structure_health_picture(
    *,
    nodes: list[dict[str, Any]],
    counts: dict[str, Any],
    file_summary: dict[str, Any],
    graph_corrections: dict[str, Any],
) -> dict[str, Any]:
    coverage = _feature_coverage_picture(nodes)
    feature_count = int(coverage["governed_feature_count"])
    missing_docs = max(0, feature_count - int(coverage["doc_bound_count"]))
    missing_tests = max(0, feature_count - int(coverage["test_bound_count"]))
    file_total = max(1, int(counts.get("files") or 0))
    orphan_files = int(counts.get("orphan_files") or 0)
    pending_files = int(counts.get("pending_decision_files") or 0)
    cleanup_candidates = int(counts.get("cleanup_candidates") or 0)
    proposed_patches = int(graph_corrections.get("proposed_count") or 0)
    high_risk_patches = int(graph_corrections.get("high_risk_proposed_count") or 0)

    coverage_penalty = 0.0
    if feature_count:
        coverage_penalty = ((missing_docs * 6.0) + (missing_tests * 8.0)) / feature_count
    file_penalty = min(
        12.0,
        ((orphan_files * 4.0) + (pending_files * 0.5) + (cleanup_candidates * 0.5)) / file_total * 100.0,
    )
    correction_penalty = min(8.0, proposed_patches * 1.5 + high_risk_patches * 3.0)
    score = round(max(0.0, 100.0 - coverage_penalty - file_penalty - correction_penalty), 2)
    return {
        "score_version": "structure_health_v1_algorithmic_coverage_inventory",
        "score": score,
        "status": "current",
        **coverage,
        "file_hygiene": {
            "total_files": counts.get("files", 0),
            "orphan_files": orphan_files,
            "pending_decision_files": pending_files,
            "cleanup_candidates": cleanup_candidates,
            "summary": file_summary,
        },
        "graph_correction_patches": {
            "proposed_count": proposed_patches,
            "high_risk_proposed_count": high_risk_patches,
        },
        "penalties": {
            "coverage": round(coverage_penalty, 2),
            "file_hygiene": round(file_penalty, 2),
            "graph_corrections": round(correction_penalty, 2),
        },
        "l4_asset_health": _l4_asset_picture(nodes),
    }


def _latest_projection_health(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "graph_semantic_projections"):
        return {}
    try:
        row = conn.execute(
            """
            SELECT projection_id, health_json, created_at
            FROM graph_semantic_projections
            WHERE project_id = ? AND snapshot_id = ?
            ORDER BY event_watermark DESC, created_at DESC
            LIMIT 1
            """,
            (project_id, snapshot_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    health = _decode_json(row["health_json"], {})
    if not isinstance(health, dict):
        health = {}
    return {
        **health,
        "projection_id": row["projection_id"],
        "projection_created_at": row["created_at"],
    }


def _semantic_health_picture(
    *,
    projection_health: dict[str, Any],
    legacy_health: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    if projection_health:
        score = _as_float(projection_health.get("project_health_score"), None)
        return {
            "score_version": projection_health.get("score_version") or "semantic_projection",
            "score": score,
            "status": "current",
            "source": "semantic_projection",
            "projection_id": projection_health.get("projection_id", ""),
            "feature_count": projection_health.get("feature_count"),
            "semantic_current_count": projection_health.get("semantic_current_count"),
            "semantic_missing_count": projection_health.get("semantic_missing_count"),
            "semantic_stale_count": projection_health.get("semantic_stale_count"),
            "semantic_unverified_hash_count": projection_health.get("semantic_unverified_hash_count"),
            "semantic_current_ratio": projection_health.get("semantic_current_ratio"),
            "semantic_trusted_count": projection_health.get("semantic_trusted_count"),
            "semantic_trusted_ratio": projection_health.get("semantic_trusted_ratio"),
            "semantic_review_debt_count": projection_health.get("semantic_review_debt_count"),
            "semantic_review_debt_ratio": projection_health.get("semantic_review_debt_ratio"),
            "doc_coverage_ratio": projection_health.get("doc_coverage_ratio"),
            "test_coverage_ratio": projection_health.get("test_coverage_ratio"),
            "semantic_debt_penalty": projection_health.get("semantic_debt_penalty"),
            "binding_context_penalty": projection_health.get("binding_context_penalty"),
            "open_issue_penalty": projection_health.get("open_issue_penalty"),
            "semantic_open_issue_count": projection_health.get("semantic_open_issue_count"),
            "low_health_count": projection_health.get("low_health_count"),
            "edge_semantic_eligible_count": projection_health.get("edge_semantic_eligible_count"),
            "edge_semantic_requested_count": projection_health.get("edge_semantic_requested_count"),
            "edge_semantic_current_count": projection_health.get("edge_semantic_current_count"),
            "edge_semantic_rule_count": projection_health.get("edge_semantic_rule_count"),
            "edge_semantic_missing_count": projection_health.get("edge_semantic_missing_count"),
            "edge_semantic_unqueued_count": projection_health.get("edge_semantic_unqueued_count"),
            "edge_semantic_needs_ai_count": projection_health.get("edge_semantic_needs_ai_count"),
            "edge_semantic_payload_current_count": projection_health.get("edge_semantic_payload_current_count"),
            "edge_semantic_coverage_ratio": projection_health.get("edge_semantic_coverage_ratio"),
            "edge_semantic_payload_coverage_ratio": projection_health.get("edge_semantic_payload_coverage_ratio"),
        }
    coverage = legacy_health.get("semantic_coverage_ratio")
    if coverage is None:
        coverage = review_meta.get("latest_full_semantic_coverage_ratio")
    if coverage is not None:
        return {
            "score_version": "semantic_metadata_fallback_v1",
            "score": _as_float(legacy_health.get("governance_observability_score"), None),
            "status": "metadata_only",
            "source": "snapshot_notes",
            "semantic_current_ratio": coverage,
            "semantic_coverage_ratio": coverage,
        }
    return {
        "score_version": "semantic_health_v1",
        "score": None,
        "status": "pending",
        "source": "none",
    }


def _project_insight_health_picture(
    *,
    latest_review: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    health = latest_review.get("health_picture") if isinstance(latest_review, dict) else {}
    if isinstance(health, dict) and health:
        file_hygiene = health.get("file_hygiene") if isinstance(health.get("file_hygiene"), dict) else {}
        return {
            "score_version": "project_insight_health_v1_global_review",
            "score": _as_float(health.get("project_health_score"), None),
            "status": "reviewed",
            "source": "global_semantic_review",
            "latest_run_id": review_meta.get("latest_full_run_id", ""),
            "latest_status": review_meta.get("latest_full_status", ""),
            "low_health_count": health.get("low_health_count"),
            "issue_counts": health.get("project_health_issue_counts", {}),
            "file_hygiene_score": health.get("file_hygiene_score"),
            "file_hygiene": {
                "available": bool(file_hygiene.get("available")),
                "run_id": file_hygiene.get("run_id", ""),
                "total_files": file_hygiene.get("total_files"),
                "review_required_count": file_hygiene.get("review_required_count"),
                "orphan_count": file_hygiene.get("orphan_count"),
                "pending_decision_count": file_hygiene.get("pending_decision_count"),
                "error_count": file_hygiene.get("error_count"),
                "cleanup_candidate_count": file_hygiene.get("cleanup_candidate_count"),
                "cleanup_candidate_bytes": file_hygiene.get("cleanup_candidate_bytes"),
                "cleanup_candidate_mb": file_hygiene.get("cleanup_candidate_mb"),
                "by_kind": file_hygiene.get("by_kind", {}),
                "by_scan_status": file_hygiene.get("by_scan_status", {}),
                "by_graph_status": file_hygiene.get("by_graph_status", {}),
                "review_required_sample": file_hygiene.get("review_required_sample", []),
                "cleanup_candidate_sample": file_hygiene.get("cleanup_candidate_sample", []),
            },
        }
    if review_meta:
        return {
            "score_version": "project_insight_health_v1_global_review",
            "score": None,
            "status": "metadata_only",
            "source": "snapshot_notes",
            "latest_run_id": review_meta.get("latest_full_run_id", ""),
            "latest_status": review_meta.get("latest_full_status", ""),
        }
    return {
        "score_version": "project_insight_health_v1_global_review",
        "score": None,
        "status": "pending",
        "source": "none",
    }


def _legacy_health_from_review(
    latest_review: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    health = latest_review.get("health_picture") if isinstance(latest_review, dict) else {}
    if not isinstance(health, dict):
        health = {}
    return {
        "project_health_score": health.get("project_health_score"),
        "raw_project_health_score": health.get("raw_project_health_score"),
        "file_hygiene_score": health.get("file_hygiene_score"),
        "artifact_binding_score": health.get("artifact_binding_score"),
        "governance_observability_score": health.get("governance_observability_score"),
        "doc_coverage_ratio": health.get("doc_coverage_ratio"),
        "test_coverage_ratio": health.get("test_coverage_ratio"),
        "semantic_coverage_ratio": (
            health.get("semantic_coverage_ratio")
            if health.get("semantic_coverage_ratio") is not None
            else review_meta.get("latest_full_semantic_coverage_ratio")
        ),
    }


def _health_from_snapshot_notes(notes: dict[str, Any]) -> dict[str, Any]:
    latest_review = _latest_global_review_from_notes(notes)
    review_meta = notes.get("global_semantic_review") if isinstance(notes.get("global_semantic_review"), dict) else {}
    return _legacy_health_from_review(latest_review, review_meta)


def _dashboard_health(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    nodes: list[dict[str, Any]],
    counts: dict[str, Any],
    file_summary: dict[str, Any],
    graph_corrections: dict[str, Any],
    notes: dict[str, Any],
) -> dict[str, Any]:
    latest_review = _latest_global_review_from_notes(notes)
    review_meta = notes.get("global_semantic_review") if isinstance(notes.get("global_semantic_review"), dict) else {}
    legacy = _legacy_health_from_review(latest_review, review_meta)
    structure = _structure_health_picture(
        nodes=nodes,
        counts=counts,
        file_summary=file_summary,
        graph_corrections=graph_corrections,
    )
    projection = _latest_projection_health(conn, project_id, snapshot_id)
    semantic = _semantic_health_picture(
        projection_health=projection,
        legacy_health=legacy,
        review_meta=review_meta,
    )
    insight = _project_insight_health_picture(
        latest_review=latest_review,
        review_meta=review_meta,
    )
    project_score = (
        legacy.get("project_health_score")
        if legacy.get("project_health_score") is not None
        else structure.get("score")
        if structure.get("score") is not None
        else semantic.get("score")
    )
    return {
        **legacy,
        "project_health_score": project_score,
        "structure_health_score": structure.get("score"),
        "semantic_health_score": semantic.get("score"),
        "project_insight_health_score": insight.get("score"),
        "structure_health": structure,
        "semantic_health": semantic,
        "project_insight_health": insight,
    }


def _semantic_counts(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> dict[str, Any]:
    return {
        "nodes_by_status": _group_counts(conn, "graph_semantic_nodes", "status", project_id, snapshot_id),
        "jobs_by_status": _group_counts(conn, "graph_semantic_jobs", "status", project_id, snapshot_id),
        "semantic_node_count": _count_rows(conn, "graph_semantic_nodes", project_id, snapshot_id),
        "semantic_job_count": _count_rows(conn, "graph_semantic_jobs", project_id, snapshot_id),
    }


def summarize_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Return a compact dashboard-safe summary for one graph snapshot."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    nodes_by_layer = _group_counts(conn, "graph_nodes_index", "layer", project_id, snapshot_id)
    edges_by_type = _group_counts(conn, "graph_edges_index", "edge_type", project_id, snapshot_id)
    semantic = _semantic_counts(conn, project_id, snapshot_id)
    try:
        from .graph_correction_patches import correction_patch_summary

        graph_corrections = correction_patch_summary(conn, project_id)
    except Exception:
        graph_corrections = {
            "total": 0,
            "by_status": {},
            "by_type": {},
            "by_risk": {},
            "last_apply_status": {},
            "proposed_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "stale_count": 0,
            "replayable_count": 0,
            "high_risk_proposed_count": 0,
        }
    try:
        files = list_graph_snapshot_files(conn, project_id, snapshot_id, limit=1)
        file_summary = files["summary"]
        file_total = int(files["total_count"])
    except Exception:
        file_summary = {}
        file_total = 0
    try:
        summary_nodes = list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            limit=100000,
            include_semantic=False,
        )
    except Exception:
        summary_nodes = []

    notes = _snapshot_notes(snapshot)
    semantic_state = {}
    semantic_enrichment = notes.get("semantic_enrichment")
    if isinstance(semantic_enrichment, dict):
        semantic_state = semantic_enrichment.get("semantic_graph_state") or {}
        if not isinstance(semantic_state, dict):
            semantic_state = {}

    by_scan = file_summary.get("by_scan_status", {}) if isinstance(file_summary, dict) else {}
    by_kind = file_summary.get("by_kind", {}) if isinstance(file_summary, dict) else {}
    counts = {
        "nodes": _count_rows(conn, "graph_nodes_index", project_id, snapshot_id),
        "nodes_by_layer": nodes_by_layer,
        "edges": _count_rows(conn, "graph_edges_index", project_id, snapshot_id),
        "edges_by_type": edges_by_type,
        "features": int(nodes_by_layer.get("L7", 0)),
        "files": file_total,
        "orphan_files": int(by_scan.get("orphan", 0)),
        "pending_decision_files": int(by_scan.get("pending_decision", 0)),
        "cleanup_candidates": int(by_kind.get("generated", 0)),
        "ai_review_feedback": int(semantic_state.get("open_issue_count") or 0),
    }
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "snapshot_kind": snapshot["snapshot_kind"],
        "snapshot_status": snapshot["status"],
        "created_at": snapshot["created_at"],
        "created_by": snapshot.get("created_by", ""),
        "graph_sha256": snapshot.get("graph_sha256", ""),
        "inventory_sha256": snapshot.get("inventory_sha256", ""),
        "drift_sha256": snapshot.get("drift_sha256", ""),
        "counts": counts,
        "health": _dashboard_health(
            conn,
            project_id,
            snapshot_id,
            nodes=summary_nodes,
            counts=counts,
            file_summary=file_summary,
            graph_corrections=graph_corrections,
            notes=notes,
        ),
        "semantic": semantic,
        "graph_correction_patches": graph_corrections,
        "file_inventory_summary": file_summary,
    }


def _backlog_counts_for_commits(
    conn: sqlite3.Connection,
    commits: Iterable[str],
) -> dict[str, dict[str, int]]:
    selected = [str(commit or "").strip() for commit in commits if str(commit or "").strip()]
    if not selected or not _table_exists(conn, "backlog_bugs"):
        return {}
    placeholders = ",".join("?" for _ in selected)
    try:
        rows = conn.execute(
            f"""
            SELECT "commit" AS commit_sha, status, mf_type, COUNT(*) AS count
            FROM backlog_bugs
            WHERE "commit" IN ({placeholders})
            GROUP BY "commit", status, mf_type
            """,
            selected,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, int]] = {
        commit: {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0}
        for commit in selected
    }
    for row in rows:
        commit = str(row["commit_sha"] or "")
        status = str(row["status"] or "").lower()
        mf_type = str(row["mf_type"] or "")
        count = int(row["count"] or 0)
        bucket = out.setdefault(commit, {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0})
        bucket["total"] += count
        if status == "open":
            bucket["open"] += count
        if status == "fixed":
            bucket["fixed"] += count
        if mf_type:
            bucket["manual_fix"] += count
        else:
            bucket["chain"] += count
    return out


def _pending_by_commit(conn: sqlite3.Connection, project_id: str) -> dict[str, dict[str, Any]]:
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED],
        ref_name="active",
    )
    return {str(row.get("commit_sha") or ""): row for row in pending}


def list_commit_timeline(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    include_backlog: bool = True,
) -> list[dict[str, Any]]:
    """Return latest snapshot-backed commits for commit-anchored dashboard navigation."""
    snapshots = list_graph_snapshots(conn, project_id, limit=limit)
    active = get_active_graph_snapshot(conn, project_id)
    active_snapshot_id = str(active.get("snapshot_id") or "") if active else ""
    pending = _pending_by_commit(conn, project_id)
    by_commit: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        commit = str(snapshot.get("commit_sha") or "")
        if not commit:
            continue
        existing = by_commit.get(commit)
        if existing and existing.get("snapshot_status") == SNAPSHOT_STATUS_ACTIVE:
            existing["snapshot_count"] += 1
            continue
        if existing and snapshot.get("status") != SNAPSHOT_STATUS_ACTIVE:
            existing["snapshot_count"] += 1
            continue
        summary = summarize_graph_snapshot(conn, project_id, snapshot["snapshot_id"])
        by_commit[commit] = {
            "commit_sha": commit,
            "short_sha": commit[:7],
            "subject": "",
            "created_at": snapshot.get("created_at", ""),
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_kind": snapshot["snapshot_kind"],
            "snapshot_status": snapshot["status"],
            "snapshot_count": int((existing or {}).get("snapshot_count") or 0) + 1,
            "graph_resolution": "exact",
            "is_active": snapshot["snapshot_id"] == active_snapshot_id,
            "pending_scope_reconcile": commit in pending,
            "pending_scope_status": pending.get(commit, {}).get("status", ""),
            "counts": summary["counts"],
            "health": summary["health"],
        }
    if include_backlog:
        backlog = _backlog_counts_for_commits(conn, by_commit.keys())
        for commit, row in by_commit.items():
            row["backlog"] = backlog.get(commit, {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0})
    return list(by_commit.values())[: max(1, min(int(limit or 50), 500))]


def resolve_commit_graph_state(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
) -> dict[str, Any]:
    """Resolve a commit to the graph snapshot dashboard should display."""
    ensure_schema(conn)
    commit_sha = str(commit_sha or "").strip()
    if not commit_sha:
        raise ValueError("commit_sha is required")

    active = get_active_graph_snapshot(conn, project_id)
    active_snapshot_id = str(active.get("snapshot_id") or "") if active else ""
    exact = get_graph_snapshot_for_commit(conn, project_id, commit_sha)
    pending_rows = list_pending_scope_reconcile(conn, project_id, commit_shas=[commit_sha], ref_name="active")
    pending_active = [
        row for row in pending_rows
        if row.get("status") in {PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED}
    ]
    if exact:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": exact["snapshot_id"],
            "resolution": "exact",
            "snapshot_status": exact["status"],
            "snapshot_kind": exact["snapshot_kind"],
            "has_graph": True,
            "has_semantic_review": bool(_snapshot_notes(exact).get("global_semantic_review")),
            "pending_scope_reconcile": bool(pending_active),
            "pending_scope_status": pending_active[0]["status"] if pending_active else "",
            "is_active": exact["snapshot_id"] == active_snapshot_id,
            "warnings": [],
        }
    if pending_active:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": "",
            "resolution": "pending",
            "snapshot_status": "",
            "snapshot_kind": "",
            "has_graph": False,
            "has_semantic_review": False,
            "pending_scope_reconcile": True,
            "pending_scope_status": pending_active[0]["status"],
            "is_active": False,
            "warnings": ["scope reconcile is pending for this commit"],
        }
    if active:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": active["snapshot_id"],
            "resolution": "advisory_latest",
            "snapshot_status": active["status"],
            "snapshot_kind": active["snapshot_kind"],
            "has_graph": True,
            "has_semantic_review": bool(_snapshot_notes(active).get("global_semantic_review")),
            "pending_scope_reconcile": False,
            "pending_scope_status": "",
            "is_active": True,
            "warnings": ["no exact graph snapshot for commit; showing latest active graph as advisory context"],
        }
    return {
        "project_id": project_id,
        "commit_sha": commit_sha,
        "resolved_snapshot_id": "",
        "resolution": "missing",
        "snapshot_status": "",
        "snapshot_kind": "",
        "has_graph": False,
        "has_semantic_review": False,
        "pending_scope_reconcile": False,
        "pending_scope_status": "",
        "is_active": False,
        "warnings": ["no graph snapshot is available"],
    }


def resolve_bounded_qa_graph_basis(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    candidate_commit_sha: str,
) -> dict[str, Any]:
    """Resolve the snapshot side of a bounded QA candidate review.

    Exact candidate snapshots retain their existing behavior. When the
    candidate has no materialized snapshot, only the active canonical snapshot
    may serve as the base for a server-derived candidate diff.
    """

    ensure_schema(conn)
    project_id = str(project_id or "").strip()
    snapshot_id = str(snapshot_id or "").strip()
    candidate_commit_sha = str(candidate_commit_sha or "").strip().lower()
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    snapshot_commit_sha = str(snapshot.get("commit_sha") or "").strip().lower()
    if snapshot_commit_sha == candidate_commit_sha:
        return {
            "schema_version": "bounded_qa.graph_basis.v1",
            "graph_basis": QA_GRAPH_BASIS_EXACT_CANDIDATE,
            "snapshot_id": snapshot_id,
            "canonical_base_snapshot_id": snapshot_id,
            "base_commit_sha": snapshot_commit_sha,
            "candidate_commit_sha": candidate_commit_sha,
            "requires_candidate_diff": False,
        }

    active = get_active_graph_snapshot(conn, project_id)
    active_snapshot_id = str(active.get("snapshot_id") or "") if active else ""
    if not active or snapshot_id != active_snapshot_id:
        raise ValueError(
            "bounded QA base-diff review requires the active canonical graph snapshot"
        )
    active_commit_sha = str(active.get("commit_sha") or "").strip().lower()
    if not active_commit_sha:
        raise ValueError("active canonical graph snapshot has no base commit")

    return {
        "schema_version": "bounded_qa.graph_basis.v1",
        "graph_basis": QA_GRAPH_BASIS_CANONICAL_BASE_DIFF,
        "snapshot_id": snapshot_id,
        "canonical_base_snapshot_id": active_snapshot_id,
        "base_commit_sha": active_commit_sha,
        "candidate_commit_sha": candidate_commit_sha,
        "requires_candidate_diff": True,
    }


def export_graph_snapshot_cache(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    project_root: str | Path,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Export a non-authoritative graph cache into a project's .aming-claw/cache."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    graph_path = snapshot_graph_path(project_id, snapshot_id)
    graph_json = _read_json_artifact(graph_path, {})
    if not isinstance(graph_json, dict) or not graph_json:
        raise ValueError(f"snapshot graph companion is empty or unreadable: {graph_path}")

    root = Path(project_root).resolve()
    base = Path(cache_dir).resolve() if cache_dir else root / ".aming-claw" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    out_graph = base / "graph.current.json"
    out_manifest = base / "graph.current.manifest.json"
    graph_bytes = (
        json.dumps(graph_json, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n"
    ).encode("utf-8")
    graph_sha = _sha256_bytes(graph_bytes)
    out_graph.write_bytes(graph_bytes)
    manifest = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "snapshot_kind": snapshot["snapshot_kind"],
        "exported_at": utc_now(),
        "non_authoritative": True,
        "source_graph_sha256": snapshot["graph_sha256"],
        "export_graph_sha256": graph_sha,
        "graph_path": str(out_graph),
    }
    out_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "cache_dir": str(base),
        "graph_path": str(out_graph),
        "manifest_path": str(out_manifest),
        "manifest": manifest,
    }


def abandon_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str = "observer",
    reason: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    row = get_graph_snapshot(conn, project_id, snapshot_id)
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    if row["status"] == SNAPSHOT_STATUS_ACTIVE:
        raise ValueError("active graph snapshot cannot be abandoned")
    if row["status"] == SNAPSHOT_STATUS_SUPERSEDED:
        raise ValueError("superseded graph snapshot cannot be abandoned")
    notes = _decode_json(row.get("notes"), {})
    if not isinstance(notes, dict):
        notes = {"previous_notes": row.get("notes") or ""}
    notes["abandoned"] = {
        "actor": actor,
        "reason": reason,
        "ts": utc_now(),
    }
    conn.execute(
        "UPDATE graph_snapshots SET status = ?, notes = ? WHERE project_id = ? AND snapshot_id = ?",
        (SNAPSHOT_STATUS_ABANDONED, _json(notes), project_id, snapshot_id),
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "previous_status": row["status"],
        "status": SNAPSHOT_STATUS_ABANDONED,
    }


def get_latest_scan_baseline(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            """
            SELECT baseline_id, chain_version, scope_value, created_at
            FROM version_baselines
            WHERE project_id = ? AND scope_kind = 'commit_sweep'
            ORDER BY baseline_id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def list_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: Iterable[str] | None = None,
    commit_shas: Iterable[str] | None = None,
    ref_name: str | None = None,
    branch_ref: str | None = None,
    worktree_id: str | None = None,
    worktree_path: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM pending_scope_reconcile WHERE project_id = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    commit_values = [str(s) for s in commit_shas or [] if s]
    if commit_values:
        placeholders = ",".join("?" for _ in commit_values)
        sql += f" AND commit_sha IN ({placeholders})"
        params.extend(commit_values)
    if any(value is not None for value in (ref_name, branch_ref, worktree_id, worktree_path)):
        identity = normalize_pending_scope_identity(
            ref_name=str(ref_name or ""),
            branch_ref=str(branch_ref or ""),
            worktree_id=str(worktree_id or ""),
            worktree_path=str(worktree_path or ""),
        )
        sql += " AND ref_name = ? AND worktree_id = ?"
        params.extend([identity["ref_name"], identity["worktree_id"]])
        if branch_ref is not None:
            sql += " AND branch_ref = ?"
            params.append(identity["branch_ref"])
    sql += " ORDER BY queued_at, ref_name, worktree_id, commit_sha"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _pending_scope_hidden_from_normal_paths(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    if status == PENDING_STATUS_SUPERSEDED:
        return True
    evidence = _decode_json(row.get("evidence_json"), {})
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("hidden_from_normal_operator_paths"):
        return True
    if evidence.get("normal_operator_hidden"):
        return True
    if evidence.get("superseded_by_current_full_reconcile"):
        return True
    if evidence.get("superseded_by"):
        return True
    return False


def graph_governance_status(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    from .graph_rule_fingerprint import compact_rule_fingerprint, snapshot_rule_fingerprint

    active = get_active_graph_snapshot(conn, project_id)
    materialization = snapshot_materialization_provenance(active)
    raw_rule_fingerprint = snapshot_rule_fingerprint(active)
    rule_fingerprint = compact_rule_fingerprint(raw_rule_fingerprint) if raw_rule_fingerprint else {}
    scan = get_latest_scan_baseline(conn, project_id)
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ],
    )
    pending = [
        row for row in pending
        if not _pending_scope_hidden_from_normal_paths(row)
    ]
    return {
        "project_id": project_id,
        "active_snapshot_id": active.get("snapshot_id") if active else "",
        "graph_snapshot_commit": active.get("commit_sha") if active else "",
        "materialized_graph_baseline_commit": active.get("commit_sha") if active else "",
        "active_snapshot_materialization": materialization,
        "active_snapshot_warnings": materialization.get("warnings") or [],
        "active_snapshot_rule_fingerprint": rule_fingerprint,
        "active_snapshot_rule_fingerprint_id": rule_fingerprint.get("fingerprint", ""),
        "scan_baseline_commit": scan.get("chain_version") if scan else "",
        "scan_baseline_id": scan.get("baseline_id") if scan else None,
        "pending_scope_reconcile_count": len(pending),
        "pending_scope_reconcile": pending,
    }


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def current_full_run_snapshot_identity_check(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str,
    snapshot_id: str = "",
    commit_sha: str = "",
    idempotency_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove that one current-full run names one durable snapshot identity."""

    expected_snapshot_id = str(snapshot_id or "").strip()
    expected_commit_sha = str(commit_sha or "").strip()
    expected_scope = (
        dict(idempotency_scope)
        if isinstance(idempotency_scope, Mapping)
        else None
    )
    observed: list[dict[str, Any]] = []
    for source, rows in (
        (
            "metric",
            conn.execute(
                """
                SELECT snapshot_id, commit_sha, evidence_json
                FROM reconcile_run_metrics
                WHERE project_id = ? AND run_id = ?
                """,
                (project_id, run_id),
            ).fetchall(),
        ),
        (
            "claim",
            conn.execute(
                """
                SELECT snapshot_id, commit_sha, '' AS evidence_json
                FROM graph_current_full_build_claim_history
                WHERE project_id = ? AND run_id = ?
                """,
                (project_id, run_id),
            ).fetchall(),
        ),
    ):
        for row in rows:
            item = dict(row)
            scope = None
            if source == "metric":
                evidence = _decode_json(item.get("evidence_json"), {})
                stored_scope = (
                    evidence.get("idempotency_scope")
                    if isinstance(evidence, Mapping)
                    else None
                )
                scope = (
                    dict(stored_scope)
                    if isinstance(stored_scope, Mapping)
                    else {}
                )
            observed.append(
                {
                    "source": source,
                    "snapshot_id": str(item.get("snapshot_id") or "").strip(),
                    "commit_sha": str(item.get("commit_sha") or "").strip(),
                    "idempotency_scope": scope,
                }
            )
    if not expected_snapshot_id and observed:
        expected_snapshot_id = observed[0]["snapshot_id"]
    conflict_fields: set[str] = set()
    for item in observed:
        if item["snapshot_id"] != expected_snapshot_id:
            conflict_fields.add("snapshot_id")
        if expected_commit_sha and item["commit_sha"] != expected_commit_sha:
            conflict_fields.add("commit_sha")
        if (
            item["source"] == "metric"
            and expected_scope is not None
            and item["idempotency_scope"] != expected_scope
        ):
            conflict_fields.add("idempotency_scope")
    return {
        "conflict": bool(conflict_fields),
        "reason": (
            "current_full_run_snapshot_identity_conflict"
            if conflict_fields
            else ""
        ),
        "run_id": str(run_id or "").strip(),
        "expected_snapshot_id": expected_snapshot_id,
        "expected_commit_sha": expected_commit_sha,
        "expected_idempotency_scope": expected_scope,
        "conflict_fields": sorted(conflict_fields),
        "observed_identities": observed,
    }


def acquire_current_full_build_claim(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str,
    snapshot_id: str,
    commit_sha: str,
    manager_epoch: str,
    manager_pid: int,
    manager_started_at: str,
    manager_start_identity: str,
    metric_evidence: dict[str, Any] | None = None,
    created_at: str = "",
) -> dict[str, Any]:
    """Durably fence one current-full materialization before it touches state."""

    if conn.in_transaction:
        raise RuntimeError("current-full build claim requires a clean transaction boundary")
    ensure_schema(conn)
    conn.commit()
    values = {
        "project_id": str(project_id or "").strip(),
        "run_id": str(run_id or "").strip(),
        "snapshot_id": str(snapshot_id or "").strip(),
        "commit_sha": str(commit_sha or "").strip(),
        "manager_epoch": str(manager_epoch or "").strip(),
        "manager_pid": int(manager_pid or 0),
        "manager_started_at": str(manager_started_at or "").strip(),
        "manager_start_identity": str(manager_start_identity or "").strip(),
    }
    if not all(values.values()):
        raise ValueError("current-full build claim requires complete server owner identity")
    now = created_at or utc_now()
    claim_id = f"gcfclaim-{uuid.uuid4().hex[:20]}"
    try:
        conn.execute("BEGIN IMMEDIATE")
        run_identity = current_full_run_snapshot_identity_check(
            conn,
            values["project_id"],
            run_id=values["run_id"],
            snapshot_id=values["snapshot_id"],
            commit_sha=values["commit_sha"],
            idempotency_scope=(metric_evidence or {}).get("idempotency_scope")
            if isinstance((metric_evidence or {}).get("idempotency_scope"), Mapping)
            else {},
        )
        if run_identity["conflict"]:
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_run_snapshot_identity_conflict",
                run_identity,
            )
        prior_identity = conn.execute(
            """
            SELECT * FROM graph_current_full_build_claim_history
            WHERE project_id = ? AND run_id = ? AND snapshot_id = ?
            """,
            (values["project_id"], values["run_id"], values["snapshot_id"]),
        ).fetchone()
        if prior_identity:
            prior = dict(prior_identity)
            reason = (
                "current_full_build_identity_terminalized"
                if str(prior.get("status") or "") != "active"
                else "current_full_build_identity_already_claimed"
            )
            raise GraphSnapshotBuildClaimConflictError(reason, prior)
        prior_metric = conn.execute(
            """
            SELECT * FROM reconcile_run_metrics
            WHERE project_id = ? AND run_id = ? AND snapshot_id = ?
            """,
            (values["project_id"], values["run_id"], values["snapshot_id"]),
        ).fetchone()
        if prior_metric:
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_build_metric_identity_exists", dict(prior_metric)
            )
        existing_snapshot = conn.execute(
            """
            SELECT * FROM graph_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (values["project_id"], values["snapshot_id"]),
        ).fetchone()
        if existing_snapshot:
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_snapshot_identity_exists",
                dict(existing_snapshot),
            )
        active = conn.execute(
            """
            SELECT * FROM graph_current_full_build_claim_history
            WHERE project_id = ? AND snapshot_id = ? AND status = 'active'
            """,
            (values["project_id"], values["snapshot_id"]),
        ).fetchone()
        if active:
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_snapshot_build_claimed", dict(active)
            )
        conn.execute(
            """
            INSERT INTO graph_current_full_build_claim_history (
              claim_id, project_id, snapshot_id, run_id, commit_sha, status,
              manager_epoch, manager_pid, manager_started_at,
              manager_start_identity, acquired_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                values["project_id"],
                values["snapshot_id"],
                values["run_id"],
                values["commit_sha"],
                values["manager_epoch"],
                values["manager_pid"],
                values["manager_started_at"],
                values["manager_start_identity"],
                now,
            ),
        )
        record_reconcile_run_metric(
            conn,
            values["project_id"],
            run_id=values["run_id"],
            snapshot_id=values["snapshot_id"],
            commit_sha=values["commit_sha"],
            snapshot_kind="full",
            strategy="current_full_reconcile",
            graph_delta_mode="full_rebuild",
            status="running",
            evidence=metric_evidence or {},
            created_at=now,
            schema_ready=True,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM graph_current_full_build_claim_history WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    return dict(row)


def terminalize_current_full_build_claim(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    claim_id: str,
    run_id: str,
    snapshot_id: str,
    commit_sha: str,
    terminal_status: str,
    manager_start_identity: str,
    elapsed_ms: int = 0,
    trace_summary_path: str = "",
    metric_evidence: dict[str, Any] | None = None,
    created_at: str = "",
) -> dict[str, Any]:
    """Atomically persist the build outcome and release its durable fence."""

    status = str(terminal_status or "").strip()
    if status not in {"candidate_ready", "failed"}:
        raise ValueError("build claim terminal_status must be candidate_ready or failed")
    if conn.in_transaction:
        raise RuntimeError("current-full build terminalization requires a clean transaction boundary")
    now = utc_now()
    terminalization_error = ""
    persisted_snapshot: dict[str, Any] = {}
    companion_integrity: dict[str, Any] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        claim_row = conn.execute(
            """
            SELECT * FROM graph_current_full_build_claim_history
            WHERE claim_id = ? AND project_id = ? AND run_id = ?
              AND snapshot_id = ? AND status = 'active'
            """,
            (claim_id, project_id, run_id, snapshot_id),
        ).fetchone()
        if not claim_row:
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_build_claim_not_active"
            )
        claim = dict(claim_row)
        if str(claim.get("manager_start_identity") or "") != str(
            manager_start_identity or ""
        ):
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_build_claim_foreign_owner", claim
            )
        if str(claim.get("commit_sha") or "") != str(commit_sha or ""):
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_build_claim_commit_mismatch", claim
            )
        if status == "candidate_ready":
            snapshot_row = conn.execute(
                """
                SELECT * FROM graph_snapshots
                WHERE project_id = ? AND snapshot_id = ?
                """,
                (project_id, snapshot_id),
            ).fetchone()
            persisted_snapshot = dict(snapshot_row) if snapshot_row else {}
            snapshot_notes = _decode_json(persisted_snapshot.get("notes"), {})
            snapshot_run_id = str(
                snapshot_notes.get("run_id")
                if isinstance(snapshot_notes, Mapping)
                else ""
            ).strip()
            expected_hashes_present = all(
                str(persisted_snapshot.get(field) or "").strip()
                for field in (
                    "graph_sha256",
                    "inventory_sha256",
                    "drift_sha256",
                )
            )
            if not persisted_snapshot:
                terminalization_error = "current_full_candidate_snapshot_missing"
            elif str(persisted_snapshot.get("commit_sha") or "") != str(
                commit_sha or ""
            ):
                terminalization_error = "current_full_candidate_snapshot_commit_mismatch"
            elif str(persisted_snapshot.get("snapshot_kind") or "") != "full":
                terminalization_error = "current_full_candidate_snapshot_kind_mismatch"
            elif str(persisted_snapshot.get("status") or "") != SNAPSHOT_STATUS_CANDIDATE:
                terminalization_error = "current_full_candidate_snapshot_status_mismatch"
            elif snapshot_run_id != str(run_id or ""):
                terminalization_error = "current_full_candidate_snapshot_run_mismatch"
            elif not expected_hashes_present:
                terminalization_error = "current_full_candidate_materialization_incomplete"
            else:
                companion_integrity = validate_snapshot_companion_integrity(
                    persisted_snapshot
                )
                if not companion_integrity["valid"]:
                    terminalization_error = str(
                        companion_integrity.get("error")
                        or "current_full_candidate_companion_integrity_invalid"
                    )
        effective_status = "failed" if terminalization_error else status
        effective_evidence = dict(metric_evidence or {})
        if terminalization_error:
            effective_evidence.update(
                {
                    "phase": "candidate_persistence_validation_failed",
                    "error": terminalization_error,
                    "requested_terminal_status": status,
                    "candidate_released_as_ready": False,
                    "companion_integrity": companion_integrity,
                }
            )
        record_reconcile_run_metric(
            conn,
            project_id,
            run_id=run_id,
            snapshot_id=snapshot_id,
            commit_sha=commit_sha,
            snapshot_kind="full",
            strategy="current_full_reconcile",
            graph_delta_mode="full_rebuild",
            status=effective_status,
            elapsed_ms=elapsed_ms,
            trace_summary_path=trace_summary_path,
            evidence=effective_evidence,
            created_at=created_at,
            schema_ready=True,
        )
        updated = conn.execute(
            """
            UPDATE graph_current_full_build_claim_history
            SET status = 'released', released_at = ?, terminal_status = ?
            WHERE claim_id = ? AND status = 'active'
            """,
            (now, effective_status, claim_id),
        )
        if updated.rowcount != 1:
            raise GraphSnapshotBuildClaimConflictError(
                "current_full_build_claim_release_race", claim
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if terminalization_error:
        raise GraphSnapshotBuildClaimConflictError(
            terminalization_error,
            {
                **claim,
                "persisted_snapshot": persisted_snapshot,
                "companion_integrity": companion_integrity,
            },
        )
    row = conn.execute(
        "SELECT * FROM graph_current_full_build_claim_history WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    return dict(row)


def record_reconcile_run_metric(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str,
    snapshot_id: str,
    commit_sha: str = "",
    parent_commit_sha: str = "",
    snapshot_kind: str = "",
    strategy: str = "",
    graph_delta_mode: str = "",
    status: str = "",
    changed_file_count: int = 0,
    impacted_file_count: int = 0,
    event_count: int = 0,
    node_count: int = 0,
    edge_count: int = 0,
    elapsed_ms: int = 0,
    trace_summary_path: str = "",
    fallback_reason: str = "",
    evidence: dict[str, Any] | None = None,
    created_at: str = "",
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Persist one reconcile timing row.

    The primary key is run_id + snapshot_id so fallback metadata can be
    upserted after graph events are emitted.
    """
    if not schema_ready:
        ensure_schema(conn)
    rid = str(run_id or snapshot_id or commit_sha or "").strip()
    sid = str(snapshot_id or "").strip()
    if not rid or not sid:
        raise ValueError("reconcile metric requires run_id and snapshot_id")
    now = created_at or utc_now()
    payload = _json(evidence or {})
    conn.execute(
        """
        INSERT INTO reconcile_run_metrics
          (project_id, run_id, snapshot_id, commit_sha, parent_commit_sha,
           snapshot_kind, strategy, graph_delta_mode, status,
           changed_file_count, impacted_file_count, event_count,
           node_count, edge_count, elapsed_ms, trace_summary_path,
           fallback_reason, created_at, evidence_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, run_id, snapshot_id) DO UPDATE SET
          commit_sha = excluded.commit_sha,
          parent_commit_sha = excluded.parent_commit_sha,
          snapshot_kind = excluded.snapshot_kind,
          strategy = excluded.strategy,
          graph_delta_mode = excluded.graph_delta_mode,
          status = excluded.status,
          changed_file_count = excluded.changed_file_count,
          impacted_file_count = excluded.impacted_file_count,
          event_count = excluded.event_count,
          node_count = excluded.node_count,
          edge_count = excluded.edge_count,
          elapsed_ms = excluded.elapsed_ms,
          trace_summary_path = excluded.trace_summary_path,
          fallback_reason = excluded.fallback_reason,
          evidence_json = excluded.evidence_json
        """,
        (
            project_id,
            rid,
            sid,
            str(commit_sha or ""),
            str(parent_commit_sha or ""),
            str(snapshot_kind or ""),
            str(strategy or ""),
            str(graph_delta_mode or ""),
            str(status or ""),
            int(changed_file_count or 0),
            int(impacted_file_count or 0),
            int(event_count or 0),
            int(node_count or 0),
            int(edge_count or 0),
            int(elapsed_ms or 0),
            str(trace_summary_path or ""),
            str(fallback_reason or ""),
            now,
            payload,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM reconcile_run_metrics
        WHERE project_id=? AND run_id=? AND snapshot_id=?
        """,
        (project_id, rid, sid),
    ).fetchone()
    return dict(row)


def _metric_from_snapshot_row(row: sqlite3.Row) -> dict[str, Any] | None:
    snapshot = dict(row)
    notes = _decode_json(snapshot.get("notes"), {})
    if not isinstance(notes, dict):
        return None
    scope_delta = notes.get("scope_file_delta")
    if not isinstance(scope_delta, dict):
        pending = notes.get("pending_scope_reconcile")
        scope_delta = pending.get("scope_file_delta") if isinstance(pending, dict) else {}
    if not isinstance(scope_delta, dict):
        scope_delta = {}
    pending_notes = notes.get("pending_scope_reconcile") if isinstance(notes.get("pending_scope_reconcile"), dict) else {}
    event_summary = pending_notes.get("scope_graph_events") if isinstance(pending_notes, dict) else {}
    if not isinstance(event_summary, dict):
        event_summary = {}
    graph_stats: dict[str, Any] = {}
    graph_path = snapshot_graph_path(str(snapshot.get("project_id") or ""), str(snapshot.get("snapshot_id") or ""))
    if graph_path.exists():
        try:
            graph_stats = graph_payload_stats(json.loads(graph_path.read_text(encoding="utf-8")))
        except Exception:
            graph_stats = {}
    trace_ref = notes.get("trace") if isinstance(notes.get("trace"), dict) else {}
    trace_summary_path = str(trace_ref.get("summary_path") or "")
    trace_summary: dict[str, Any] = {}
    if trace_summary_path:
        try:
            trace_summary = json.loads(Path(trace_summary_path).read_text(encoding="utf-8"))
        except Exception:
            trace_summary = {}
    strategy = str(
        notes.get("scope_reconcile_strategy")
        or scope_delta.get("strategy")
        or ("legacy_full_like" if snapshot.get("snapshot_kind") == "scope" else snapshot.get("snapshot_kind") or "")
    )
    mode = str(
        notes.get("scope_graph_delta_mode")
        or scope_delta.get("graph_delta_mode")
        or ("full_rebuild" if strategy == "legacy_full_like" else "")
    )
    fallback_reason = str(scope_delta.get("fallback_reason") or "")
    return {
        "run_id": str(notes.get("run_id") or snapshot.get("snapshot_id") or ""),
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "commit_sha": str(snapshot.get("commit_sha") or ""),
        "parent_commit_sha": str(pending_notes.get("active_graph_commit") or ""),
        "snapshot_kind": str(snapshot.get("snapshot_kind") or ""),
        "strategy": strategy,
        "graph_delta_mode": mode,
        "status": str(trace_summary.get("status") or snapshot.get("status") or ""),
        "changed_file_count": _int_value(scope_delta.get("changed_file_count")),
        "impacted_file_count": _int_value(scope_delta.get("impacted_file_count")),
        "event_count": _int_value(event_summary.get("event_count")),
        "node_count": _int_value(graph_stats.get("nodes")),
        "edge_count": _int_value(graph_stats.get("edges")),
        "elapsed_ms": _int_value(trace_summary.get("elapsed_ms")),
        "trace_summary_path": trace_summary_path,
        "fallback_reason": fallback_reason,
        "created_at": str(snapshot.get("created_at") or ""),
        "evidence": {
            "source": "graph_snapshot_notes_backfill",
            "snapshot_status": snapshot.get("status") or "",
        },
    }


def backfill_reconcile_run_metrics_from_snapshots(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Best-effort import of historical trace timings into metrics table."""
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM graph_snapshots
        WHERE project_id=? AND snapshot_kind IN ('scope', 'full')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (project_id, int(limit or 100)),
    ).fetchall()
    imported = 0
    for row in rows:
        metric = _metric_from_snapshot_row(row)
        if not metric:
            continue
        try:
            record_reconcile_run_metric(
                conn,
                project_id,
                run_id=metric["run_id"],
                snapshot_id=metric["snapshot_id"],
                commit_sha=metric["commit_sha"],
                parent_commit_sha=metric["parent_commit_sha"],
                snapshot_kind=metric["snapshot_kind"],
                strategy=metric["strategy"],
                graph_delta_mode=metric["graph_delta_mode"],
                status=metric["status"],
                changed_file_count=metric["changed_file_count"],
                impacted_file_count=metric["impacted_file_count"],
                event_count=metric["event_count"],
                node_count=metric["node_count"],
                edge_count=metric["edge_count"],
                elapsed_ms=metric["elapsed_ms"],
                trace_summary_path=metric["trace_summary_path"],
                fallback_reason=metric["fallback_reason"],
                evidence=metric["evidence"],
                created_at=metric["created_at"],
            )
            imported += 1
        except Exception:
            continue
    return {"project_id": project_id, "scanned": len(rows), "imported": imported}


_RECONCILE_METRIC_TERMINAL_STATUSES = frozenset(
    {"candidate_ready", "complete", "failed", "terminalized_stale"}
)
_RECONCILE_METRIC_NONTERMINAL_STATUSES = frozenset({"running", "finalizing"})
_RECONCILE_METRIC_WINDOW_MAX = 1000
_RECONCILE_METRIC_OVERLAY_SCAN_MIN = 64
_RECONCILE_METRIC_OVERLAY_SCAN_MAX = 2000
_RECONCILE_METRIC_CURSOR_RE = re.compile(
    r"rrm1\.([1-9][0-9]{0,18})\.([0-9a-f]{64})\.([0-9a-f]{64})"
)


class InvalidReconcileMetricCursor(ValueError):
    """A continuation cursor cannot prove its exact query position."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code or "invalid_cursor")
        super().__init__(self.reason_code)


class ReconcileMetricWindowOverflow(RuntimeError):
    """The compatibility list API cannot truthfully return a partial window."""

    def __init__(self, window: Mapping[str, Any]):
        self.window = {
            key: value for key, value in window.items() if key != "rows"
        }
        super().__init__("reconcile metric list exceeds the bounded window")


def _reconcile_metric_cursor_scope_digest(
    project_id: str,
    strategy: str,
) -> str:
    canonical = json.dumps(
        {
            "project_id": str(project_id or ""),
            "strategy": str(strategy or ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reconcile_metric_cursor_tuple_digest(
    *,
    rowid: int,
    scope_digest: str,
    created_at: Any,
    run_id: Any,
    snapshot_id: Any,
) -> str:
    canonical = json.dumps(
        [
            str(scope_digest or ""),
            int(rowid),
            str(created_at or ""),
            str(run_id or ""),
            str(snapshot_id or ""),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reconcile_metric_next_cursor(
    row: Mapping[str, Any],
    *,
    project_id: str,
    strategy: str,
) -> str:
    rowid = int(row.get("_reconcile_metric_rowid") or 0)
    if rowid < 1:
        raise InvalidReconcileMetricCursor("cursor_rowid_missing")
    scope_digest = _reconcile_metric_cursor_scope_digest(project_id, strategy)
    tuple_digest = _reconcile_metric_cursor_tuple_digest(
        rowid=rowid,
        scope_digest=scope_digest,
        created_at=row.get("created_at"),
        run_id=row.get("run_id"),
        snapshot_id=row.get("snapshot_id"),
    )
    return f"rrm1.{rowid}.{scope_digest}.{tuple_digest}"


def _reconcile_metric_cursor_position(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    strategy: str,
    cursor: str,
) -> tuple[str, str, str] | None:
    raw_cursor = str(cursor or "").strip()
    if not raw_cursor:
        return None
    match = _RECONCILE_METRIC_CURSOR_RE.fullmatch(raw_cursor)
    if match is None:
        raise InvalidReconcileMetricCursor("cursor_malformed")
    rowid = int(match.group(1))
    if rowid > 9_223_372_036_854_775_807:
        raise InvalidReconcileMetricCursor("cursor_rowid_out_of_range")
    submitted_scope_digest = match.group(2)
    submitted_tuple_digest = match.group(3)
    expected_scope_digest = _reconcile_metric_cursor_scope_digest(
        project_id,
        strategy,
    )
    if not hmac.compare_digest(submitted_scope_digest, expected_scope_digest):
        raise InvalidReconcileMetricCursor("cursor_scope_mismatch")

    params: list[Any] = [rowid, project_id]
    strategy_sql = ""
    if strategy:
        strategy_sql = " AND strategy=?"
        params.append(strategy)
    row = conn.execute(
        f"""
        SELECT rowid AS _reconcile_metric_rowid, created_at, run_id, snapshot_id
        FROM reconcile_run_metrics
        WHERE rowid=? AND project_id=?{strategy_sql}
          AND LOWER(TRIM(status)) NOT IN (
            'candidate_ready', 'complete', 'failed', 'terminalized_stale'
          )
        """,
        params,
    ).fetchone()
    if row is None:
        raise InvalidReconcileMetricCursor("cursor_position_missing")
    row = dict(row)
    actual_tuple_digest = _reconcile_metric_cursor_tuple_digest(
        rowid=rowid,
        scope_digest=expected_scope_digest,
        created_at=row.get("created_at"),
        run_id=row.get("run_id"),
        snapshot_id=row.get("snapshot_id"),
    )
    if not hmac.compare_digest(submitted_tuple_digest, actual_tuple_digest):
        raise InvalidReconcileMetricCursor("cursor_identity_mismatch")
    return (
        str(row.get("created_at") or ""),
        str(row.get("run_id") or ""),
        str(row.get("snapshot_id") or ""),
    )


def project_reconcile_run_metric_status(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the append-only metric status used by queue visibility.

    This raw fallback never trusts ledger presence. Window readers replace it
    only with a fully validated append-only terminalization overlay; unknown
    or malformed values remain visible as unresolved work.
    """
    raw_status = str(row.get("status") or "").strip().lower()
    known_statuses = (
        _RECONCILE_METRIC_TERMINAL_STATUSES
        | _RECONCILE_METRIC_NONTERMINAL_STATUSES
    )
    if raw_status in known_statuses:
        effective_status = raw_status
        reason_code = ""
    else:
        effective_status = "unknown"
        reason_code = (
            "missing_reconcile_status"
            if not raw_status
            else "unrecognized_reconcile_status"
        )
    return {
        "effective_status": effective_status,
        "is_terminal": effective_status in _RECONCILE_METRIC_TERMINAL_STATUSES,
        "status_reason_code": reason_code,
    }


def _project_reconcile_run_metric_with_overlay(
    conn: sqlite3.Connection,
    project_id: str,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    projected = dict(row)
    has_overlay = bool(projected.pop("_has_terminalization_overlay", 0))
    projected.pop("_reconcile_metric_rowid", None)
    status = project_reconcile_run_metric_status(projected)
    if has_overlay:
        overlay = reconcile_run_terminalization_overlay(
            conn,
            project_id,
            run_id=str(projected.get("run_id") or ""),
            snapshot_id=str(projected.get("snapshot_id") or ""),
        )
        status = {
            key: overlay[key]
            for key in ("effective_status", "is_terminal", "status_reason_code")
        }
    return {**projected, **status}


def list_reconcile_run_metrics_window(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    strategy: str = "",
    nonterminal_limit: int = _RECONCILE_METRIC_WINDOW_MAX,
    cursor: str = "",
) -> dict[str, Any]:
    """Return a bounded latest sample plus a keyset nonterminal page.

    ``limit`` retains its historical meaning as the latest-row sample size.
    Effective nonterminal work is independently page-bounded and exposes an
    opaque, scope-bound continuation cursor. No exact remaining count is
    computed because that would turn a queue read into unbounded DB work.
    """
    ensure_schema(conn)
    params: list[Any] = [project_id]
    where_sql = "m.project_id=?"
    if strategy:
        where_sql += " AND m.strategy=?"
        params.append(strategy)
    sample_limit = max(1, min(int(limit or 50), 1000))
    page_limit = max(
        1,
        min(int(nonterminal_limit or _RECONCILE_METRIC_WINDOW_MAX), 1000),
    )
    order_sql = " ORDER BY m.created_at DESC, m.run_id DESC, m.snapshot_id DESC"
    overlay_sql = """
        EXISTS (
          SELECT 1 FROM graph_reconcile_run_terminalizations AS t
          WHERE t.project_id=m.project_id
            AND t.source_run_id=m.run_id
            AND t.source_snapshot_id=m.snapshot_id
        ) AS _has_terminalization_overlay
    """
    latest_rows = conn.execute(
        f"""
        SELECT m.rowid AS _reconcile_metric_rowid, m.*, {overlay_sql}
        FROM reconcile_run_metrics AS m
        WHERE {where_sql}{order_sql} LIMIT ?
        """,
        [*params, sample_limit + 1],
    ).fetchall()
    latest_sample_truncated = len(latest_rows) > sample_limit
    latest_rows = latest_rows[:sample_limit]

    cursor_position = _reconcile_metric_cursor_position(
        conn,
        project_id,
        strategy=strategy,
        cursor=cursor,
    )
    nonterminal_where_sql = where_sql
    nonterminal_params = list(params)
    if cursor_position is not None:
        created_at, run_id, snapshot_id = cursor_position
        nonterminal_where_sql += (
            " AND (m.created_at < ?"
            " OR (m.created_at = ? AND m.run_id < ?)"
            " OR (m.created_at = ? AND m.run_id = ? AND m.snapshot_id < ?))"
        )
        nonterminal_params.extend(
            [created_at, created_at, run_id, created_at, run_id, snapshot_id]
        )
    scan_limit = min(
        max(page_limit + 1, _RECONCILE_METRIC_OVERLAY_SCAN_MIN),
        _RECONCILE_METRIC_OVERLAY_SCAN_MAX,
    )
    raw_nonterminal_rows = conn.execute(
        f"""
        SELECT m.rowid AS _reconcile_metric_rowid, m.*, {overlay_sql}
        FROM reconcile_run_metrics AS m
        WHERE {nonterminal_where_sql}
          AND LOWER(TRIM(m.status)) NOT IN (
            'candidate_ready', 'complete', 'failed', 'terminalized_stale'
          )
        {order_sql}
        LIMIT ?
        """,
        [*nonterminal_params, scan_limit + 1],
    ).fetchall()
    projection_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def project(row: Mapping[str, Any]) -> dict[str, Any]:
        identity = (
            str(row.get("project_id") or ""),
            str(row.get("run_id") or ""),
            str(row.get("snapshot_id") or ""),
        )
        if identity not in projection_cache:
            projection_cache[identity] = _project_reconcile_run_metric_with_overlay(
                conn, project_id, row
            )
        return projection_cache[identity]

    effective_nonterminal_rows: list[dict[str, Any]] = []
    cursor_anchor: Mapping[str, Any] | None = None
    has_more = False
    for raw_row in raw_nonterminal_rows[:scan_limit]:
        projected = project(dict(raw_row))
        if not projected["is_terminal"]:
            if len(effective_nonterminal_rows) >= page_limit:
                has_more = True
                break
            effective_nonterminal_rows.append(projected)
        cursor_anchor = dict(raw_row)
    else:
        has_more = len(raw_nonterminal_rows) > scan_limit
    next_cursor = (
        _reconcile_metric_next_cursor(
            cursor_anchor,
            project_id=project_id,
            strategy=strategy,
        )
        if has_more and cursor_anchor is not None
        else ""
    )
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in latest_rows:
        projected = project(dict(row))
        identity = (
            str(projected.get("project_id") or ""),
            str(projected.get("run_id") or ""),
            str(projected.get("snapshot_id") or ""),
        )
        selected.setdefault(identity, projected)
    for projected in effective_nonterminal_rows:
        identity = (
            str(projected.get("project_id") or ""),
            str(projected.get("run_id") or ""),
            str(projected.get("snapshot_id") or ""),
        )
        selected.setdefault(identity, projected)
    rows = sorted(
        selected.values(),
        key=lambda row: (
            str(row.get("created_at") or ""),
            str(row.get("run_id") or ""),
            str(row.get("snapshot_id") or ""),
        ),
        reverse=True,
    )
    return {
        "schema_version": "reconcile_run_metrics.window.v1",
        "semantics": "latest_sample_plus_effective_nonterminal_page",
        "rows": rows,
        "returned_count": len(rows),
        "latest_sample_limit": sample_limit,
        "latest_sample_count": len(latest_rows),
        "latest_sample_truncated": latest_sample_truncated,
        "nonterminal_page_limit": page_limit,
        "nonterminal_page_count": len(effective_nonterminal_rows),
        "has_more": has_more,
        "truncated": bool(latest_sample_truncated or has_more),
        "next_cursor": next_cursor,
        "cursor_applied": bool(str(cursor or "").strip()),
        "remaining_count_claimed": False,
        "effective_nonterminal_completeness": (
            "partial" if has_more else "complete"
        ),
        "continuation_scope": "effective_nonterminal_only",
        "latest_sample_repeated_on_continuation": True,
        "terminal_history_semantics": "latest_sample_only",
    }


def list_reconcile_run_metrics(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    strategy: str = "",
) -> list[dict[str, Any]]:
    """Compatibility list facade that fails closed on a partial queue."""
    window = list_reconcile_run_metrics_window(
        conn,
        project_id,
        limit=limit,
        strategy=strategy,
    )
    if window["has_more"]:
        raise ReconcileMetricWindowOverflow(window)
    return list(window["rows"])


_FULL_REBUILD_STRATEGIES = {"full_rebuild_fallback", "legacy_full_like", "full"}


def _fallback_next_action(fallback_reason: str) -> str:
    reason = str(fallback_reason or "").strip()
    if reason == "source_function_identity_changed":
        return "run_full_reconcile; function identities changed, so incremental dependency facts may be stale"
    if reason == "ruleset_change_requires_rule_aware_reconcile":
        return "run_full_reconcile; graph rule or interpretation inputs changed"
    if reason == "inventory_status_change_requires_full_rebuild":
        return "inspect inventory status changes, then run full reconcile if the status change is expected"
    if reason == "source_typed_relation_asset_unknown":
        return "file or fix the missing typed-relation asset binding before retrying scope reconcile"
    if reason:
        return "review fallback_reason and retry with full reconcile if the changed graph contract is expected"
    return "review reconcile trace; fallback reason was not recorded"


def summarize_reconcile_run_metrics(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    window = list_reconcile_run_metrics_window(
        conn,
        project_id,
        limit=limit,
        nonterminal_limit=limit,
    )
    rows = list(window["rows"])
    buckets: dict[str, dict[str, Any]] = {}
    fallback_reasons: dict[str, dict[str, Any]] = {}
    latest_full_rebuild_fallback: dict[str, Any] = {}
    for row in rows:
        strategy = str(row.get("strategy") or "unknown")
        bucket = buckets.setdefault(
            strategy,
            {"count": 0, "total_elapsed_ms": 0, "min_elapsed_ms": 0, "max_elapsed_ms": 0},
        )
        elapsed = int(row.get("elapsed_ms") or 0)
        bucket["count"] += 1
        bucket["total_elapsed_ms"] += elapsed
        bucket["min_elapsed_ms"] = elapsed if not bucket["min_elapsed_ms"] else min(bucket["min_elapsed_ms"], elapsed)
        bucket["max_elapsed_ms"] = max(bucket["max_elapsed_ms"], elapsed)
        fallback_reason = str(row.get("fallback_reason") or "")
        if strategy in _FULL_REBUILD_STRATEGIES and fallback_reason:
            reason_bucket = fallback_reasons.setdefault(
                fallback_reason,
                {
                    "count": 0,
                    "total_elapsed_ms": 0,
                    "latest_created_at": "",
                    "latest_run_id": "",
                    "operator_next_action": _fallback_next_action(fallback_reason),
                },
            )
            reason_bucket["count"] += 1
            reason_bucket["total_elapsed_ms"] += elapsed
            if not reason_bucket["latest_created_at"]:
                reason_bucket["latest_created_at"] = str(row.get("created_at") or "")
                reason_bucket["latest_run_id"] = str(row.get("run_id") or "")
            if not latest_full_rebuild_fallback:
                latest_full_rebuild_fallback = {
                    "run_id": str(row.get("run_id") or ""),
                    "snapshot_id": str(row.get("snapshot_id") or ""),
                    "commit_sha": str(row.get("commit_sha") or ""),
                    "strategy": strategy,
                    "graph_delta_mode": str(row.get("graph_delta_mode") or ""),
                    "fallback_reason": fallback_reason,
                    "elapsed_ms": elapsed,
                    "created_at": str(row.get("created_at") or ""),
                    "operator_next_action": _fallback_next_action(fallback_reason),
                }
    for bucket in buckets.values():
        count = int(bucket.get("count") or 0)
        bucket["avg_elapsed_ms"] = round(float(bucket["total_elapsed_ms"]) / count, 2) if count else 0
    for bucket in fallback_reasons.values():
        count = int(bucket.get("count") or 0)
        bucket["avg_elapsed_ms"] = round(float(bucket["total_elapsed_ms"]) / count, 2) if count else 0

    incremental = buckets.get("incremental_graph_delta") or {}
    full_candidates = [
        bucket for name, bucket in buckets.items()
        if name in _FULL_REBUILD_STRATEGIES
    ]
    full_count = sum(int(bucket.get("count") or 0) for bucket in full_candidates)
    full_total = sum(int(bucket.get("total_elapsed_ms") or 0) for bucket in full_candidates)
    incremental_avg = float(incremental.get("avg_elapsed_ms") or 0)
    full_avg = (float(full_total) / full_count) if full_count else 0.0
    speedup = round(full_avg / incremental_avg, 2) if full_avg and incremental_avg else 0
    reduction_pct = round((1 - (incremental_avg / full_avg)) * 100, 1) if full_avg and incremental_avg else 0
    return {
        "project_id": project_id,
        "sample_count": len(rows),
        "by_strategy": buckets,
        "fallback_reasons": fallback_reasons,
        "latest_full_rebuild_fallback": latest_full_rebuild_fallback,
        "speedup": {
            "incremental_avg_ms": round(incremental_avg, 2),
            "full_avg_ms": round(full_avg, 2),
            "speedup_x": speedup,
            "elapsed_reduction_pct": reduction_pct,
            "full_sample_count": full_count,
            "incremental_sample_count": int(incremental.get("count") or 0),
        },
    }


def strict_graph_ready(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    target_commit: str,
) -> dict[str, Any]:
    status = graph_governance_status(conn, project_id)
    graph_commit = status.get("materialized_graph_baseline_commit") or ""
    ok = bool(target_commit and graph_commit == target_commit)
    reason = ""
    if not graph_commit:
        reason = "no_active_graph_snapshot"
    elif not target_commit:
        reason = "missing_target_commit"
    elif graph_commit != target_commit:
        reason = "graph_snapshot_commit_mismatch"
    return {
        "ok": ok,
        "reason": reason,
        "target_commit": target_commit,
        **status,
    }


def import_existing_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
    activate: bool = False,
    expected_old_snapshot_id: str | None = None,
    extra_graph_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    source = select_existing_graph_source(
        conn,
        project_id,
        extra_graph_paths=extra_graph_paths,
    )
    if not source:
        raise FileNotFoundError(f"no non-empty graph source found for project {project_id}")

    selected_commit = _resolve_import_commit(conn, project_id, commit_sha)
    sid = snapshot_id or snapshot_id_for("imported", selected_commit)
    source_notes = {
        "source_kind": source["source_kind"],
        "source_path": source["source_path"],
        "source_ref": source.get("source_ref", ""),
        "source_stats": source["stats"],
        "selected_commit": selected_commit,
    }
    snapshot = create_graph_snapshot(
        conn,
        project_id,
        snapshot_id=sid,
        commit_sha=selected_commit,
        snapshot_kind="imported",
        graph_json=source["graph_json"],
        file_inventory=[],
        drift_ledger=[],
        created_by=created_by,
        notes=_json(source_notes),
    )
    counts = index_graph_snapshot(
        conn,
        project_id,
        sid,
        nodes=_graph_nodes(source["graph_json"]),
        edges=_graph_edges(source["graph_json"]),
    )
    result = {
        **snapshot,
        "source": {k: v for k, v in source.items() if k != "graph_json"},
        "index_counts": counts,
        "activation": None,
    }
    if activate:
        result["activation"] = activate_graph_snapshot(
            conn,
            project_id,
            sid,
            expected_old_snapshot_id=expected_old_snapshot_id,
        )
    return result


def record_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    commit_sha: str,
    path: str,
    drift_type: str,
    target_symbol: str = "",
    node_id: str = "",
    status: str = "open",
    evidence: dict[str, Any] | None = None,
) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO graph_drift_ledger
          (project_id, snapshot_id, commit_sha, path, node_id, target_symbol,
           drift_type, status, evidence_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            snapshot_id,
            commit_sha,
            path,
            node_id,
            target_symbol,
            drift_type,
            status,
            _json(evidence or {}),
            utc_now(),
        ),
    )


def list_graph_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str = "",
    status: str = "",
    drift_type: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = """
        SELECT project_id, snapshot_id, commit_sha, path, node_id,
               target_symbol, drift_type, status, evidence_json, updated_at
        FROM graph_drift_ledger
        WHERE project_id = ?
    """
    if snapshot_id:
        sql += " AND snapshot_id = ?"
        params.append(snapshot_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if drift_type:
        sql += " AND drift_type = ?"
        params.append(drift_type)
    sql += " ORDER BY updated_at DESC, path, drift_type, target_symbol LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "project_id": row["project_id"],
            "snapshot_id": row["snapshot_id"],
            "commit_sha": row["commit_sha"],
            "path": row["path"],
            "node_id": row["node_id"],
            "target_symbol": row["target_symbol"],
            "drift_type": row["drift_type"],
            "status": row["status"],
            "evidence": _decode_json(row["evidence_json"], {}),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_graph_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    path: str,
    drift_type: str,
    target_symbol: str | None = None,
) -> dict[str, Any]:
    """Fetch one drift row. If target_symbol is omitted, the match must be unique."""
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id, path, drift_type]
    sql = """
        SELECT project_id, snapshot_id, commit_sha, path, node_id,
               target_symbol, drift_type, status, evidence_json, updated_at
        FROM graph_drift_ledger
        WHERE project_id = ?
          AND snapshot_id = ?
          AND path = ?
          AND drift_type = ?
    """
    if target_symbol is not None:
        sql += " AND target_symbol = ?"
        params.append(target_symbol)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        raise KeyError(f"graph drift row not found: {snapshot_id}/{path}/{drift_type}")
    if target_symbol is None and len(rows) > 1:
        raise ValueError("multiple drift rows match; target_symbol is required")
    row = rows[0]
    return {
        "project_id": row["project_id"],
        "snapshot_id": row["snapshot_id"],
        "commit_sha": row["commit_sha"],
        "path": row["path"],
        "node_id": row["node_id"],
        "target_symbol": row["target_symbol"],
        "drift_type": row["drift_type"],
        "status": row["status"],
        "evidence": _decode_json(row["evidence_json"], {}),
        "updated_at": row["updated_at"],
    }


def update_graph_drift_status(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    path: str,
    drift_type: str,
    target_symbol: str = "",
    status: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update one drift row status while preserving/augmenting its evidence."""
    row = get_graph_drift(
        conn,
        project_id,
        snapshot_id=snapshot_id,
        path=path,
        drift_type=drift_type,
        target_symbol=target_symbol,
    )
    merged_evidence = dict(row.get("evidence") or {})
    merged_evidence.update(evidence or {})
    now = utc_now()
    conn.execute(
        """
        UPDATE graph_drift_ledger
        SET status = ?,
            evidence_json = ?,
            updated_at = ?
        WHERE project_id = ?
          AND snapshot_id = ?
          AND path = ?
          AND drift_type = ?
          AND target_symbol = ?
        """,
        (
            status,
            _json(merged_evidence),
            now,
            project_id,
            snapshot_id,
            path,
            drift_type,
            target_symbol,
        ),
    )
    row["status"] = status
    row["evidence"] = merged_evidence
    row["updated_at"] = now
    return row


def queue_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    parent_commit_sha: str = "",
    ref_name: str = "",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
    status: str = PENDING_STATUS_QUEUED,
    snapshot_id: str = "",
    evidence: dict[str, Any] | None = None,
    force_requeue: bool = False,
) -> dict[str, Any]:
    ensure_schema(conn)
    if status not in ALLOWED_PENDING_STATUSES:
        raise ValueError(f"invalid pending scope reconcile status: {status}")
    now = utc_now()
    force_flag = 1 if force_requeue else 0
    identity = normalize_pending_scope_identity(
        ref_name=ref_name,
        branch_ref=branch_ref,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
    )
    conn.execute(
        """
        INSERT INTO pending_scope_reconcile
          (project_id, ref_name, branch_ref, worktree_id, worktree_path,
           commit_sha, parent_commit_sha, queued_at, status, retry_count,
           snapshot_id, evidence_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(project_id, ref_name, worktree_id, commit_sha) DO UPDATE SET
          queued_at = CASE
            WHEN ? = 1 THEN excluded.queued_at
            ELSE pending_scope_reconcile.queued_at
          END,
          branch_ref = excluded.branch_ref,
          worktree_path = excluded.worktree_path,
          parent_commit_sha = CASE
            WHEN pending_scope_reconcile.parent_commit_sha = '' THEN excluded.parent_commit_sha
            ELSE pending_scope_reconcile.parent_commit_sha
          END,
          status = CASE
            WHEN ? = 1 THEN excluded.status
            WHEN pending_scope_reconcile.status IN ('materialized', 'waived')
            THEN pending_scope_reconcile.status
            ELSE excluded.status
          END,
          retry_count = CASE
            WHEN ? = 1 THEN pending_scope_reconcile.retry_count + 1
            ELSE pending_scope_reconcile.retry_count
          END,
          snapshot_id = CASE
            WHEN ? = 1 THEN excluded.snapshot_id
            WHEN excluded.snapshot_id != '' THEN excluded.snapshot_id
            ELSE pending_scope_reconcile.snapshot_id
          END,
          evidence_json = excluded.evidence_json
        """,
        (
            project_id,
            identity["ref_name"],
            identity["branch_ref"],
            identity["worktree_id"],
            identity["worktree_path"],
            commit_sha,
            parent_commit_sha,
            now,
            status,
            snapshot_id,
            _json({
                "ref_name": identity["ref_name"],
                "branch_ref": identity["branch_ref"],
                "worktree_id": identity["worktree_id"],
                "worktree_path": identity["worktree_path"],
                **(evidence or {}),
                **({"force_requeue": True, "forced_at": now} if force_requeue else {}),
            }),
            force_flag,
            force_flag,
            force_flag,
            force_flag,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM pending_scope_reconcile
        WHERE project_id = ? AND ref_name = ? AND worktree_id = ? AND commit_sha = ?
        """,
        (project_id, identity["ref_name"], identity["worktree_id"], commit_sha),
    ).fetchone()
    return dict(row)


def waive_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_shas: Iterable[str] | None = None,
    ref_name: str | None = None,
    branch_ref: str | None = None,
    worktree_id: str | None = None,
    worktree_path: str | None = None,
    snapshot_id: str = "",
    actor: str = "observer",
    reason: str = "",
    evidence: dict[str, Any] | None = None,
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Mark retryable pending scope rows as waived with explicit evidence."""
    if not schema_ready:
        ensure_schema(conn)
    selected = [
        str(commit or "").strip()
        for commit in (commit_shas or [])
        if str(commit or "").strip()
    ]
    params: list[Any] = [project_id]
    sql = """
        SELECT commit_sha FROM pending_scope_reconcile
        WHERE project_id = ?
          AND status IN (?, ?, ?)
    """
    params.extend([PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED])
    if selected:
        placeholders = ",".join("?" for _ in selected)
        sql += f" AND commit_sha IN ({placeholders})"
        params.extend(selected)
    identity: dict[str, str] | None = None
    if any(value is not None for value in (ref_name, branch_ref, worktree_id, worktree_path)):
        identity = normalize_pending_scope_identity(
            ref_name=str(ref_name or ""),
            branch_ref=str(branch_ref or ""),
            worktree_id=str(worktree_id or ""),
            worktree_path=str(worktree_path or ""),
        )
        sql += " AND ref_name = ? AND worktree_id = ?"
        params.extend([identity["ref_name"], identity["worktree_id"]])
        if branch_ref is not None:
            sql += " AND branch_ref = ?"
            params.append(identity["branch_ref"])
    sql += " ORDER BY queued_at, commit_sha"
    rows = conn.execute(sql, params).fetchall()
    targets = [row["commit_sha"] for row in rows]
    if not targets:
        return {
            "project_id": project_id,
            "waived_count": 0,
            "commit_shas": [],
            "snapshot_id": snapshot_id,
        }

    waiver_evidence = {
        "source": "pending_scope_waiver",
        "actor": actor,
        "reason": reason,
        "snapshot_id": snapshot_id,
        "commit_shas": targets,
        **(identity or {}),
        **(evidence or {}),
    }
    placeholders = ",".join("?" for _ in targets)
    update_filters = ""
    update_filter_values: list[Any] = []
    if identity is not None:
        update_filters += " AND ref_name = ? AND worktree_id = ?"
        update_filter_values.extend([identity["ref_name"], identity["worktree_id"]])
    cur = conn.execute(
        f"""
        UPDATE pending_scope_reconcile
        SET status = ?,
            snapshot_id = CASE WHEN ? != '' THEN ? ELSE snapshot_id END,
            evidence_json = ?
        WHERE project_id = ?
          AND commit_sha IN ({placeholders})
          {update_filters}
          AND status IN (?, ?, ?)
        """,
        (
            PENDING_STATUS_WAIVED,
            snapshot_id,
            snapshot_id,
            _json(waiver_evidence),
            project_id,
            *targets,
            *update_filter_values,
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ),
    )
    return {
        "project_id": project_id,
        "waived_count": int(cur.rowcount or 0),
        "commit_shas": targets,
        "snapshot_id": snapshot_id,
        **(identity or {}),
    }


def mark_pending_scope_reconcile_failed(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    ref_name: str | None = None,
    branch_ref: str | None = None,
    worktree_id: str | None = None,
    worktree_path: str | None = None,
    actor: str = "observer",
    reason: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move a queued/running pending-scope row to failed with recovery evidence."""
    ensure_schema(conn)
    commit = str(commit_sha or "").strip()
    if not commit:
        return {"project_id": project_id, "updated_count": 0, "commit_sha": ""}
    identity: dict[str, str] | None = None
    select_sql = "SELECT * FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?"
    select_params: list[Any] = [project_id, commit]
    if any(value is not None for value in (ref_name, branch_ref, worktree_id, worktree_path)):
        identity = normalize_pending_scope_identity(
            ref_name=str(ref_name or ""),
            branch_ref=str(branch_ref or ""),
            worktree_id=str(worktree_id or ""),
            worktree_path=str(worktree_path or ""),
        )
        select_sql += " AND ref_name=? AND worktree_id=?"
        select_params.extend([identity["ref_name"], identity["worktree_id"]])
        if branch_ref is not None:
            select_sql += " AND branch_ref=?"
            select_params.append(identity["branch_ref"])
    row = conn.execute(select_sql, select_params).fetchone()
    previous = dict(row) if row else {}
    failure_evidence = {
        "source": "pending_scope_failure",
        "actor": actor,
        "reason": reason,
        "commit_sha": commit,
        **(identity or {}),
        "previous_status": previous.get("status", ""),
        "previous_evidence": _decode_json(previous.get("evidence_json"), {}),
        "recoverable": True,
        "recovery_action": "force_requeue_pending_scope",
        **(evidence or {}),
    }
    update_filters = ""
    update_filter_values: list[Any] = []
    if identity is not None:
        update_filters += " AND ref_name=? AND worktree_id=?"
        update_filter_values.extend([identity["ref_name"], identity["worktree_id"]])
    cur = conn.execute(
        f"""
        UPDATE pending_scope_reconcile
        SET status=?, evidence_json=?
        WHERE project_id=? AND commit_sha=? {update_filters} AND status IN (?, ?, ?)
        """,
        (
            PENDING_STATUS_FAILED,
            _json(failure_evidence),
            project_id,
            commit,
            *update_filter_values,
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ),
    )
    return {
        "project_id": project_id,
        "updated_count": int(cur.rowcount or 0),
        "commit_sha": commit,
        "status": PENDING_STATUS_FAILED,
        "evidence": failure_evidence,
        **(identity or {}),
    }


def recover_stale_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    max_running_seconds: int = 1800,
    actor: str = "observer",
) -> dict[str, Any]:
    """Fail old running rows so dashboard Update Graph can requeue them."""
    ensure_schema(conn)
    cutoff_seconds = max(0, int(max_running_seconds or 0))
    now_text = utc_now()
    rows = conn.execute(
        """
        SELECT * FROM pending_scope_reconcile
        WHERE project_id=? AND status=?
        ORDER BY queued_at, ref_name, worktree_id, commit_sha
        """,
        (project_id, PENDING_STATUS_RUNNING),
    ).fetchall()
    recovered: list[str] = []
    recovered_rows: list[dict[str, str]] = []
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        queued_at = str(row["queued_at"] or "")
        try:
            queued_dt = datetime.strptime(queued_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_seconds = int((now_dt - queued_dt).total_seconds())
        except Exception:
            age_seconds = cutoff_seconds + 1
        if age_seconds < cutoff_seconds:
            continue
        commit = str(row["commit_sha"] or "")
        mark_pending_scope_reconcile_failed(
            conn,
            project_id,
            commit_sha=commit,
            ref_name=str(row["ref_name"] or ""),
            branch_ref=str(row["branch_ref"] or ""),
            worktree_id=str(row["worktree_id"] or ""),
            worktree_path=str(row["worktree_path"] or ""),
            actor=actor,
            reason="stale running pending-scope row exceeded recovery threshold",
            evidence={
                "source": "pending_scope_stale_running_recovery",
                "queued_at": queued_at,
                "recovered_at": now_text,
                "age_seconds": age_seconds,
                "max_running_seconds": cutoff_seconds,
            },
        )
        recovered.append(commit)
        recovered_rows.append({
            "commit_sha": commit,
            "ref_name": str(row["ref_name"] or ""),
            "branch_ref": str(row["branch_ref"] or ""),
            "worktree_id": str(row["worktree_id"] or ""),
            "worktree_path": str(row["worktree_path"] or ""),
        })
    return {
        "project_id": project_id,
        "recovered_count": len(recovered),
        "commit_shas": recovered,
        "recovered_rows": recovered_rows,
        "max_running_seconds": cutoff_seconds,
    }


__all__ = [
    "ALLOWED_PENDING_STATUSES",
    "ALLOWED_SNAPSHOT_STATUSES",
    "GRAPH_SNAPSHOT_SCHEMA_SQL",
    "GraphSnapshotBuildClaimConflictError",
    "GraphSnapshotConflictError",
    "ManagerGenerationCertificateConflictError",
    "ReconcileRunTerminalizationProofError",
    "InvalidReconcileMetricCursor",
    "ReconcileMetricWindowOverflow",
    "acquire_current_full_build_claim",
    "activate_graph_snapshot",
    "backfill_reconcile_run_metrics_from_snapshots",
    "build_graph_rollback_epoch_state",
    "create_graph_snapshot",
    "current_full_active_terminal_tuple",
    "current_full_candidate_resume_tuple",
    "current_full_candidate_tuple_from_db",
    "ensure_schema",
    "ensure_reconcile_metric_physical_identity_migration",
    "export_graph_snapshot_cache",
    "finalize_graph_snapshot",
    "get_active_graph_snapshot",
    "get_graph_drift",
    "get_graph_snapshot",
    "get_latest_scan_baseline",
    "graph_governance_status",
    "graph_payload_edges",
    "index_graph_snapshot",
    "list_reconcile_run_metrics",
    "list_reconcile_run_metrics_window",
    "manager_generation_certificate_public_receipt",
    "list_graph_snapshot_edges",
    "list_graph_snapshot_files",
    "list_graph_snapshot_nodes",
    "list_graph_snapshots",
    "list_graph_ref_events",
    "list_graph_drift",
    "normalize_pending_scope_identity",
    "graph_payload_stats",
    "import_existing_graph_snapshot",
    "invalidate_semantic_jobs_for_rollback_epoch",
    "abandon_graph_snapshot",
    "list_pending_scope_reconcile",
    "mark_pending_scope_reconcile_failed",
    "queue_pending_scope_reconcile",
    "record_reconcile_run_metric",
    "record_reconcile_run_terminalization",
    "reconcile_run_terminalization_overlay",
    "reconcile_run_terminalization_proof",
    "reconcile_run_terminalization_safe_receipt",
    "record_manager_generation_certificate",
    "record_graph_ref_event",
    "recover_stale_pending_scope_reconcile",
    "current_manager_generation_certificate",
    "record_drift",
    "select_existing_graph_source",
    "snapshot_materialization_provenance",
    "snapshot_companion_dir",
    "snapshot_graph_path",
    "snapshot_id_for",
    "strict_graph_ready",
    "summarize_reconcile_run_metrics",
    "summarize_file_inventory_rows",
    "terminalize_current_full_build_claim",
    "update_graph_drift_status",
    "waive_pending_scope_reconcile",
    "write_companion_files",
]
