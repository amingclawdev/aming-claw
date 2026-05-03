"""Durable batch memory for cluster-driven reconcile PM decisions.

The scanner emits structural cluster candidates.  PM chain tasks use this
module to keep a session/batch-wide semantic map: accepted feature names,
file ownership, processed clusters, merge decisions, and open conflicts.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


BATCH_MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reconcile_batch_memory (
    project_id       TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    session_id       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active',
    memory_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_session
    ON reconcile_batch_memory (project_id, session_id);
CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_status
    ON reconcile_batch_memory (project_id, status);
"""

DECISION_TYPES = frozenset({
    "new_feature",
    "merge_into_existing_feature",
    "split",
    "orphan_dead_code",
    "defer",
})


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    conn.executescript(BATCH_MEMORY_SCHEMA_SQL)
    conn.commit()


def empty_memory(*, session_id: str = "", batch_id: str = "") -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "session_id": session_id or "",
        "batch_id": batch_id or "",
        "accepted_features": {},
        "file_ownership": {},
        "file_claims": {},
        "reserved_names": [],
        "open_conflicts": [],
        "processed_clusters": {},
        "merge_decisions": [],
    }


def _loads_memory(raw: str, *, session_id: str = "", batch_id: str = "") -> Dict[str, Any]:
    try:
        memory = json.loads(raw or "{}")
    except (TypeError, ValueError):
        memory = {}
    if not isinstance(memory, dict):
        memory = {}
    base = empty_memory(session_id=session_id, batch_id=batch_id)
    base.update(memory)
    for key, default in empty_memory(session_id=session_id, batch_id=batch_id).items():
        if key not in base:
            base[key] = copy.deepcopy(default)
    return base


def _json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    if row is None:
        return {}
    memory = _loads_memory(
        row["memory_json"],
        session_id=row["session_id"],
        batch_id=row["batch_id"],
    )
    return {
        "project_id": row["project_id"],
        "batch_id": row["batch_id"],
        "session_id": row["session_id"],
        "status": row["status"],
        "memory": memory,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
    }


def create_or_get_batch(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    session_id: str = "",
    batch_id: Optional[str] = None,
    created_by: str = "",
    initial_memory: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a batch memory row, or return the existing row idempotently."""
    ensure_schema(conn)
    bid = batch_id or f"rbatch-{uuid.uuid4().hex[:12]}"
    now = _utcnow()
    memory = empty_memory(session_id=session_id, batch_id=bid)
    if initial_memory:
        memory.update(initial_memory)
    memory["session_id"] = session_id or memory.get("session_id", "")
    memory["batch_id"] = bid

    conn.execute(
        "INSERT OR IGNORE INTO reconcile_batch_memory "
        "(project_id, batch_id, session_id, status, memory_json, "
        " created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
        (project_id, bid, session_id or "", _json(memory), now, now, created_by or ""),
    )
    conn.commit()
    return get_batch(conn, project_id, bid)


def get_batch(conn: sqlite3.Connection, project_id: str, batch_id: str) -> Dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM reconcile_batch_memory WHERE project_id=? AND batch_id=?",
        (project_id, batch_id),
    ).fetchone()
    return _row_to_dict(row) if row else {}


def record_pm_decision(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
    cluster_fingerprint: str,
    decision_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Record one PM decision and update the batch-wide semantic memory."""
    ensure_schema(conn)
    if not cluster_fingerprint:
        raise ValueError("cluster_fingerprint is required")
    row = conn.execute(
        "SELECT * FROM reconcile_batch_memory WHERE project_id=? AND batch_id=?",
        (project_id, batch_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"batch {batch_id!r} not found for project {project_id!r}")

    payload = dict(decision_payload or {})
    decision = str(payload.get("decision") or "").strip()
    if decision not in DECISION_TYPES:
        raise ValueError(f"decision must be one of {sorted(DECISION_TYPES)}, got: {decision!r}")

    memory = _loads_memory(
        row["memory_json"],
        session_id=row["session_id"],
        batch_id=row["batch_id"],
    )
    now = _utcnow()
    normalized = _normalize_decision(cluster_fingerprint, payload, now)

    memory["processed_clusters"][cluster_fingerprint] = normalized
    memory["merge_decisions"].append(normalized)

    if decision == "new_feature":
        _apply_feature_acceptance(memory, normalized)
    elif decision == "merge_into_existing_feature":
        _apply_feature_acceptance(memory, normalized, target_feature=normalized.get("target_feature"))
    elif decision == "split":
        _append_conflict(memory, normalized, "split_requested")
    elif decision in {"orphan_dead_code", "defer"}:
        _append_conflicts(memory, normalized)

    _dedupe_memory(memory)
    conn.execute(
        "UPDATE reconcile_batch_memory SET memory_json=?, updated_at=? "
        "WHERE project_id=? AND batch_id=?",
        (_json(memory), now, project_id, batch_id),
    )
    conn.commit()
    return get_batch(conn, project_id, batch_id)


def find_related_features(batch_or_memory: Dict[str, Any], cluster_payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return accepted features that overlap the cluster's file/test/doc hints."""
    memory = batch_or_memory.get("memory", batch_or_memory) if isinstance(batch_or_memory, dict) else {}
    if not isinstance(memory, dict):
        return []
    accepted = memory.get("accepted_features") or {}
    file_ownership = memory.get("file_ownership") or {}
    file_claims = memory.get("file_claims") or {}
    cluster_files = set(_cluster_files(cluster_payload or {}))

    matches: Dict[str, Dict[str, Any]] = {}
    for path in cluster_files:
        owner = file_ownership.get(path)
        if owner:
            match = matches.setdefault(owner, {
                "feature_name": owner,
                "reasons": [],
                "matching_files": [],
                "clusters": _str_list((accepted.get(owner) or {}).get("clusters")),
            })
            match["reasons"].append("file_ownership")
            match["matching_files"].append(path)
        for claim in file_claims.get(path) or []:
            if not isinstance(claim, dict):
                continue
            claimant = str(claim.get("feature_name") or "").strip()
            if not claimant or claimant == owner:
                continue
            match = matches.setdefault(claimant, {
                "feature_name": claimant,
                "reasons": [],
                "matching_files": [],
                "clusters": _str_list((accepted.get(claimant) or {}).get("clusters")),
            })
            match["reasons"].append("file_claim")
            match["matching_files"].append(path)

    for feature_name, feature in accepted.items():
        feature_files = set(
            _str_list(feature.get("owned_files"))
            + _str_list(feature.get("candidate_tests"))
            + _str_list(feature.get("candidate_docs"))
        )
        overlap = sorted(cluster_files & feature_files)
        if not overlap:
            continue
        match = matches.setdefault(feature_name, {
            "feature_name": feature_name,
            "reasons": [],
            "matching_files": [],
            "clusters": _str_list(feature.get("clusters")),
        })
        match["reasons"].append("file_overlap")
        match["matching_files"].extend(overlap)

    out = []
    for match in matches.values():
        match["reasons"] = sorted(set(_str_list(match.get("reasons"))))
        match["matching_files"] = sorted(set(_str_list(match.get("matching_files"))))
        out.append(match)
    out.sort(key=lambda item: item["feature_name"])
    return out


def _normalize_decision(cluster_fingerprint: str, payload: Dict[str, Any], now: str) -> Dict[str, Any]:
    feature_name = str(payload.get("feature_name") or "").strip()
    target_feature = str(payload.get("target_feature") or payload.get("merge_into") or "").strip()
    owned_files = _str_list(payload.get("owned_files") or payload.get("primary_files"))
    reserved_names = _str_list(payload.get("reserved_names"))
    candidate_tests = _str_list(payload.get("candidate_tests"))
    candidate_docs = _str_list(payload.get("candidate_docs"))
    conflicts = payload.get("conflicts") or payload.get("open_conflicts") or []
    if not isinstance(conflicts, list):
        conflicts = [conflicts]
    return {
        "cluster_fingerprint": cluster_fingerprint,
        "decision": str(payload.get("decision") or "").strip(),
        "feature_name": feature_name,
        "target_feature": target_feature,
        "purpose": payload.get("purpose"),
        "owned_files": owned_files,
        "candidate_tests": candidate_tests,
        "candidate_docs": candidate_docs,
        "reserved_names": reserved_names,
        "conflicts": conflicts,
        "reason": payload.get("reason") or payload.get("summary") or "",
        "decided_by": payload.get("decided_by") or payload.get("actor") or "pm",
        "decided_at": now,
    }


def _cluster_files(payload: Dict[str, Any]) -> list[str]:
    files = []
    for key in (
        "owned_files",
        "primary_files",
        "candidate_tests",
        "candidate_docs",
        "test_files",
        "doc_files",
    ):
        files.extend(_str_list(payload.get(key)))
    return sorted(set(files))


def _apply_feature_acceptance(
    memory: Dict[str, Any],
    decision: Dict[str, Any],
    *,
    target_feature: str = "",
) -> None:
    feature_name = target_feature or decision.get("feature_name") or decision.get("target_feature")
    if not feature_name:
        feature_name = f"cluster:{decision['cluster_fingerprint']}"
    feature = memory["accepted_features"].setdefault(
        feature_name,
        {
            "feature_name": feature_name,
            "purpose": decision.get("purpose"),
            "clusters": [],
            "owned_files": [],
            "shared_files": [],
            "candidate_tests": [],
            "candidate_docs": [],
        },
    )
    if decision["cluster_fingerprint"] not in feature["clusters"]:
        feature["clusters"].append(decision["cluster_fingerprint"])
    for key in ("owned_files", "candidate_tests", "candidate_docs"):
        feature[key] = sorted(set(_str_list(feature.get(key)) + _str_list(decision.get(key))))
    if decision.get("purpose") and not feature.get("purpose"):
        feature["purpose"] = decision["purpose"]
    for path in decision.get("owned_files") or []:
        _record_file_claim(memory, path, feature_name, decision)
    memory["reserved_names"].append(feature_name)
    memory["reserved_names"].extend(decision.get("reserved_names") or [])
    _append_conflicts(memory, decision)


def _record_file_claim(memory: Dict[str, Any], path: str, feature_name: str, decision: Dict[str, Any]) -> None:
    path = str(path or "").replace("\\", "/")
    if not path:
        return
    memory.setdefault("file_ownership", {})
    memory.setdefault("file_claims", {})
    memory.setdefault("open_conflicts", [])

    claims = memory["file_claims"].setdefault(path, [])
    claim = {
        "feature_name": feature_name,
        "cluster_fingerprint": decision.get("cluster_fingerprint", ""),
        "decision": decision.get("decision", ""),
        "decided_at": decision.get("decided_at", ""),
    }
    if not any(
        isinstance(existing, dict)
        and existing.get("feature_name") == claim["feature_name"]
        and existing.get("cluster_fingerprint") == claim["cluster_fingerprint"]
        for existing in claims
    ):
        claims.append(claim)

    owner = memory["file_ownership"].get(path)
    if not owner:
        memory["file_ownership"][path] = feature_name
        return
    if owner == feature_name:
        return

    feature = memory.get("accepted_features", {}).get(feature_name)
    if isinstance(feature, dict):
        feature["shared_files"] = sorted(set(_str_list(feature.get("shared_files")) + [path]))
    memory["open_conflicts"].append({
        "cluster_fingerprint": decision.get("cluster_fingerprint", ""),
        "reason": "shared_file_claim",
        "file": path,
        "owner_feature": owner,
        "claimant_feature": feature_name,
    })


def _append_conflicts(memory: Dict[str, Any], decision: Dict[str, Any]) -> None:
    for conflict in decision.get("conflicts") or []:
        if isinstance(conflict, dict):
            item = dict(conflict)
        else:
            item = {"reason": str(conflict)}
        item.setdefault("cluster_fingerprint", decision["cluster_fingerprint"])
        memory["open_conflicts"].append(item)


def _append_conflict(memory: Dict[str, Any], decision: Dict[str, Any], reason: str) -> None:
    memory["open_conflicts"].append({
        "cluster_fingerprint": decision["cluster_fingerprint"],
        "reason": reason,
    })
    _append_conflicts(memory, decision)


def _dedupe_memory(memory: Dict[str, Any]) -> None:
    memory["reserved_names"] = sorted(set(_str_list(memory.get("reserved_names"))))
    for feature in memory.get("accepted_features", {}).values():
        for key in ("clusters", "owned_files", "shared_files", "candidate_tests", "candidate_docs"):
            feature[key] = sorted(set(_str_list(feature.get(key))))
    deduped_claims = {}
    for path, claims in (memory.get("file_claims") or {}).items():
        claim_seen = set()
        claim_out = []
        for claim in claims or []:
            if not isinstance(claim, dict):
                continue
            marker = _json(claim)
            if marker in claim_seen:
                continue
            claim_seen.add(marker)
            claim_out.append(claim)
        deduped_claims[path] = sorted(
            claim_out,
            key=lambda item: (item.get("feature_name", ""), item.get("cluster_fingerprint", "")),
        )
    memory["file_claims"] = dict(sorted(deduped_claims.items()))
    seen_conflicts = set()
    deduped = []
    for conflict in memory.get("open_conflicts", []):
        try:
            marker = _json(conflict)
        except TypeError:
            marker = str(conflict)
        if marker in seen_conflicts:
            continue
        seen_conflicts.add(marker)
        deduped.append(conflict)
    memory["open_conflicts"] = deduped


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    try:
        return [str(item) for item in value if str(item)]
    except TypeError:
        return [str(value)] if str(value) else []


__all__ = [
    "DECISION_TYPES",
    "BATCH_MEMORY_SCHEMA_SQL",
    "ensure_schema",
    "empty_memory",
    "create_or_get_batch",
    "get_batch",
    "record_pm_decision",
    "find_related_features",
]
