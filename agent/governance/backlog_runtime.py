"""Backlog-owned runtime state helpers.

The backlog row is the durable audit anchor for chain/MF execution.  Task rows
remain the execution log, but the current stage, active task, worktree, and
observer bypass policy are mirrored here so service restarts do not erase the
operator-facing state.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


GRAPH_BYPASS_MODES = {"bypass", "off", "disabled", "manual_fix", "manual-fix"}
MF_TYPE_CHAIN_RESCUE = "chain_rescue"
MF_TYPE_SYSTEM_RECOVERY = "system_recovery"
MF_TYPES = {MF_TYPE_CHAIN_RESCUE, MF_TYPE_SYSTEM_RECOVERY}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return {}


def extract_bypass_policy(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Collect explicit bypass policy keys from task/MF metadata."""
    metadata = metadata or {}
    policy: dict[str, Any] = {}

    for key in ("backlog_bypass_policy", "bypass_policy"):
        policy.update(parse_json_object(metadata.get(key)))

    for key in (
        "graph_governance",
        "graph_governance_mode",
        "bypass_graph_governance",
        "skip_graph_delta_validation",
        "skip_version_check",
        "skip_reason",
        "bypass_reason",
        "force_no_backlog",
        "force_reason",
        "observer_authorized",
        "mf_type",
        "mf_id",
        "reconcile_run_id",
        "allow_dirty_workspace_reconciliation",
    ):
        if key in metadata and metadata.get(key) not in (None, "", [], {}):
            policy[key] = metadata[key]

    if policy.get("bypass_graph_governance") is True:
        policy.setdefault("graph_governance", "bypass")
    if str(policy.get("graph_governance_mode", "")).lower() in GRAPH_BYPASS_MODES:
        policy.setdefault("graph_governance", "bypass")
    return policy


def normalize_mf_type(raw: Any = "", existing_policy: dict[str, Any] | None = None) -> str:
    """Return a supported MF type, defaulting to graph-governed chain rescue."""
    value = str(raw or "").strip().lower()
    if not value:
        value = str((existing_policy or {}).get("mf_type", "") or "").strip().lower()
    if value in {"system-recovery", "system recovery", "recovery"}:
        value = MF_TYPE_SYSTEM_RECOVERY
    if value in {"chain-rescue", "chain rescue", "rescue", "observer_mf"}:
        value = MF_TYPE_CHAIN_RESCUE
    return value if value in MF_TYPES else MF_TYPE_CHAIN_RESCUE


def build_mf_policy(
    mf_type: str,
    *,
    mf_id: str = "",
    observer_authorized: bool = True,
    reason: str = "",
    existing_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the durable backlog policy for an MF profile."""
    mf_type = normalize_mf_type(mf_type, existing_policy)
    policy = dict(existing_policy or {})
    policy["mf_type"] = mf_type
    if mf_id:
        policy["mf_id"] = mf_id
    policy["observer_authorized"] = bool(observer_authorized)

    if mf_type == MF_TYPE_SYSTEM_RECOVERY:
        policy["graph_governance"] = "bypass"
        policy["bypass_graph_governance"] = True
        policy.setdefault("bypass_reason", reason or "system recovery manual fix")
    else:
        policy["graph_governance"] = "enforce"
        policy["bypass_graph_governance"] = False
        policy.pop("skip_graph_delta_validation", None)
    return policy


def policy_json(policy: dict[str, Any] | None) -> str:
    if not policy:
        return "{}"
    return json.dumps(policy, ensure_ascii=False, sort_keys=True)


def merge_policy_into_metadata(metadata: dict[str, Any] | None, policy: dict[str, Any] | None) -> dict[str, Any]:
    """Attach backlog policy to task metadata while preserving explicit task keys."""
    merged = dict(metadata or {})
    policy = dict(policy or {})
    if not policy:
        return merged

    existing = parse_json_object(merged.get("backlog_bypass_policy"))
    combined = {**policy, **existing}
    merged["backlog_bypass_policy"] = combined

    if is_graph_governance_bypassed({"backlog_bypass_policy": combined}):
        merged.setdefault("bypass_graph_governance", True)
        merged.setdefault("skip_graph_delta_validation", True)
        merged.setdefault(
            "skip_reason",
            combined.get("skip_reason")
            or combined.get("bypass_reason")
            or "backlog policy: graph governance bypass",
        )
    return merged


def is_graph_governance_bypassed(metadata: dict[str, Any] | None) -> bool:
    policy = extract_bypass_policy(metadata)
    if policy.get("bypass_graph_governance") is True:
        return True
    if str(policy.get("graph_governance", "")).lower() in GRAPH_BYPASS_MODES:
        return True
    if str(policy.get("graph_governance_mode", "")).lower() in GRAPH_BYPASS_MODES:
        return True
    return False


def derive_runtime_state(stage: str, task_type: str = "", failure_reason: str = "") -> str:
    stage = stage or ""
    task_type = task_type or ""
    if failure_reason:
        return "blocked" if "blocked" in stage else "failed"
    if stage in {"manual_fix_planned", "manual-fix-planned"}:
        return "manual_fix_planned"
    if stage in {"manual_fix_in_progress", "manual-fix-in-progress"}:
        return "manual_fix_in_progress"
    if stage in {"manual_fix", "manual-fix", "fixed"}:
        return "fixed"
    if stage.endswith("_queued"):
        return "queued"
    if stage.endswith("_claimed"):
        return "claimed"
    if stage.endswith("_failed"):
        return "failed"
    if "blocked" in stage:
        return "blocked"
    if task_type == "merge" and stage == "merge_complete":
        return "merged"
    if task_type == "deploy" and stage == "deploy_complete":
        return "deployed"
    if stage.endswith("_complete"):
        return "in_chain"
    return "in_chain" if stage else ""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value not in (None, "", [], {}):
            return str(value)
    return ""


def update_backlog_runtime(
    conn,
    bug_id: str,
    stage: str,
    *,
    project_id: str = "",
    failure_reason: str = "",
    task_id: str = "",
    task_type: str = "",
    metadata: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    runtime_state: str = "",
    root_task_id: str = "",
    worktree_path: str = "",
    worktree_branch: str = "",
    bypass_policy: dict[str, Any] | None = None,
    mf_type: str = "",
    takeover: dict[str, Any] | None = None,
) -> None:
    """Best-effort mirror of chain/MF runtime state into backlog_bugs."""
    if not bug_id:
        return

    metadata = metadata or {}
    result = result or {}
    policy = dict(bypass_policy or {})
    policy.update(extract_bypass_policy(metadata))

    root_task_id = _first_nonempty(
        root_task_id,
        metadata.get("chain_id"),
        metadata.get("root_task_id"),
        task_id if task_type == "pm" else "",
        metadata.get("parent_task_id"),
    )
    worktree_path = _first_nonempty(
        worktree_path,
        result.get("_worktree"),
        result.get("worktree_path"),
        result.get("worktree"),
        metadata.get("_worktree"),
        metadata.get("worktree_path"),
        metadata.get("worktree"),
    )
    worktree_branch = _first_nonempty(
        worktree_branch,
        result.get("_branch"),
        result.get("worktree_branch"),
        result.get("branch"),
        metadata.get("_branch"),
        metadata.get("worktree_branch"),
        metadata.get("branch"),
    )
    runtime_state = runtime_state or derive_runtime_state(stage, task_type, failure_reason)
    now = utc_now()
    policy_raw = policy_json(policy)
    takeover_raw = policy_json(takeover)
    mf_type = normalize_mf_type(mf_type, policy) if mf_type or policy.get("mf_type") else ""
    chain_task_seed = root_task_id or task_id

    try:
        conn.execute(
            """UPDATE backlog_bugs SET
                 chain_stage = ?,
                 stage_updated_at = ?,
                 runtime_state = ?,
                 runtime_updated_at = ?,
                 current_task_id = CASE WHEN ? != '' THEN ? ELSE current_task_id END,
                 root_task_id = CASE WHEN ? != '' THEN ? ELSE root_task_id END,
                 chain_task_id = CASE
                     WHEN COALESCE(chain_task_id, '') = '' AND ? != '' THEN ?
                     ELSE chain_task_id
                 END,
                 worktree_path = CASE WHEN ? != '' THEN ? ELSE worktree_path END,
                 worktree_branch = CASE WHEN ? != '' THEN ? ELSE worktree_branch END,
                 bypass_policy_json = CASE WHEN ? != '{}' THEN ? ELSE bypass_policy_json END,
                 mf_type = CASE WHEN ? != '' THEN ? ELSE mf_type END,
                 takeover_json = CASE WHEN ? != '{}' THEN ? ELSE takeover_json END,
                 last_failure_reason = ?,
                 updated_at = ?
               WHERE bug_id = ?""",
            (
                stage,
                now,
                runtime_state,
                now,
                task_id,
                task_id,
                root_task_id,
                root_task_id,
                chain_task_seed,
                chain_task_seed,
                worktree_path,
                worktree_path,
                worktree_branch,
                worktree_branch,
                policy_raw,
                policy_raw,
                mf_type,
                mf_type,
                takeover_raw,
                takeover_raw,
                failure_reason,
                now,
                bug_id,
            ),
        )
    except sqlite3.OperationalError:
        # Compatibility for narrow unit-test schemas or a pre-migration DB.
        try:
            conn.execute(
                "UPDATE backlog_bugs SET chain_stage=?, stage_updated_at=?, last_failure_reason=? WHERE bug_id=?",
                (stage, now, failure_reason, bug_id),
            )
        except Exception:
            log.debug("backlog_runtime: legacy update failed for bug_id=%s", bug_id, exc_info=True)
    except Exception:
        log.debug("backlog_runtime: update failed for bug_id=%s project=%s", bug_id, project_id, exc_info=True)
