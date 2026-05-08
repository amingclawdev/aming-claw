"""State-only routing for reconcile semantic feedback.

This module turns semantic ``open_issues`` into auditable feedback items.  It
does not mutate project files or activate graph snapshots; callers decide
whether a feedback item becomes a graph-only correction or a project backlog
row.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import graph_snapshot_store as store


FEEDBACK_EVENTS_NAME = "reconcile-feedback-events.jsonl"
FEEDBACK_STATE_NAME = "reconcile-feedback-state.json"

KIND_GRAPH_CORRECTION = "graph_correction"
KIND_PROJECT_IMPROVEMENT = "project_improvement"
KIND_STATUS_OBSERVATION = "status_observation"
KIND_NEEDS_OBSERVER_DECISION = "needs_observer_decision"
KIND_FALSE_POSITIVE = "false_positive"

STATUS_CATEGORY_STALE_TEST = "stale_test_expectation"
STATUS_CATEGORY_DOC_DRIFT = "doc_drift"
STATUS_CATEGORY_COVERAGE_GAP = "coverage_gap"
STATUS_CATEGORY_PROJECT_REGRESSION = "project_regression"
STATUS_CATEGORY_ORPHAN_REVIEW = "orphan_review"
STATUS_CATEGORY_FALSE_POSITIVE = "false_positive"
STATUS_CATEGORY_NEEDS_HUMAN = "needs_human_signoff"

STATUS_OBSERVATION_CATEGORIES = {
    STATUS_CATEGORY_STALE_TEST,
    STATUS_CATEGORY_DOC_DRIFT,
    STATUS_CATEGORY_COVERAGE_GAP,
    STATUS_CATEGORY_PROJECT_REGRESSION,
    STATUS_CATEGORY_ORPHAN_REVIEW,
    STATUS_CATEGORY_FALSE_POSITIVE,
    STATUS_CATEGORY_NEEDS_HUMAN,
}

STATUS_CLASSIFIED = "classified"
STATUS_REVIEWED = "reviewed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_BACKLOG_FILED = "backlog_filed"
STATUS_NEEDS_HUMAN_SIGNOFF = "needs_human_signoff"

REVIEW_DECISIONS = {
    KIND_GRAPH_CORRECTION,
    KIND_PROJECT_IMPROVEMENT,
    KIND_STATUS_OBSERVATION,
    KIND_FALSE_POSITIVE,
    "needs_human_signoff",
}

ReviewerAiCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(payload), encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(_json(row) + "\n")


def feedback_base_dir(project_id: str, snapshot_id: str) -> Path:
    return store.snapshot_companion_dir(project_id, snapshot_id) / "semantic-enrichment"


def feedback_state_path(project_id: str, snapshot_id: str) -> Path:
    return feedback_base_dir(project_id, snapshot_id) / FEEDBACK_STATE_NAME


def feedback_events_path(project_id: str, snapshot_id: str) -> Path:
    return feedback_base_dir(project_id, snapshot_id) / FEEDBACK_EVENTS_NAME


def semantic_graph_state_path(project_id: str, snapshot_id: str) -> Path:
    return feedback_base_dir(project_id, snapshot_id) / "semantic-graph-state.json"


def _new_state(project_id: str, snapshot_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "items": {},
        "updated_at": _utc_now(),
    }


def load_feedback_state(project_id: str, snapshot_id: str) -> dict[str, Any]:
    state = _read_json(feedback_state_path(project_id, snapshot_id), {})
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        state = _new_state(project_id, snapshot_id)
    state.setdefault("schema_version", 1)
    state.setdefault("project_id", project_id)
    state.setdefault("snapshot_id", snapshot_id)
    state.setdefault("items", {})
    return state


def save_feedback_state(project_id: str, snapshot_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    _write_json(feedback_state_path(project_id, snapshot_id), state)


def list_feedback_items(
    project_id: str,
    snapshot_id: str,
    *,
    feedback_kind: str = "",
    status: str = "",
    node_id: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    state = load_feedback_state(project_id, snapshot_id)
    items = list((state.get("items") or {}).values())
    if feedback_kind:
        items = [item for item in items if item.get("feedback_kind") == feedback_kind]
    if status:
        items = [item for item in items if item.get("status") == status]
    if node_id:
        items = [item for item in items if node_id in (item.get("source_node_ids") or [])]
    items.sort(key=lambda item: (str(item.get("priority") or "P3"), str(item.get("feedback_id") or "")))
    if limit is not None and limit >= 0:
        items = items[: int(limit)]
    return items


def _priority_rank(priority: Any) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(str(priority or "P3").upper(), 4)


def _feedback_lane(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "").strip()
    kind = str(item.get("final_feedback_kind") or item.get("feedback_kind") or "").strip()
    if status in {STATUS_REVIEWED, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_BACKLOG_FILED}:
        return "resolved"
    if status == STATUS_NEEDS_HUMAN_SIGNOFF or item.get("requires_human_signoff") or kind == KIND_NEEDS_OBSERVER_DECISION:
        return "review_required"
    if kind == KIND_PROJECT_IMPROVEMENT:
        return "candidate_backlog"
    if kind == KIND_STATUS_OBSERVATION:
        return "status_only"
    if kind == KIND_FALSE_POSITIVE:
        return "resolved"
    return "graph_patch_candidate"


def _lane_rank(lane: str) -> int:
    return {
        "review_required": 0,
        "candidate_backlog": 1,
        "graph_patch_candidate": 2,
        "status_only": 3,
        "resolved": 4,
    }.get(lane, 5)


def _queue_group_key(item: dict[str, Any], lane: str, *, group_by: str = "target") -> str:
    nodes = _source_nodes(item)
    node_key = ",".join(nodes) if nodes else ""
    category = str(
        item.get("reviewed_status_observation_category")
        or item.get("status_observation_category")
        or ""
    )
    if group_by in {"feature", "node", "source_node"} and node_key:
        return "|".join([lane, node_key, category])
    parts = [
        lane,
        node_key,
        str(item.get("target_type") or ""),
        str(item.get("target_id") or ""),
        str(item.get("issue_type") or ""),
        category,
    ]
    return "|".join(parts)


def _queue_action_hint(lane: str) -> str:
    if lane == "review_required":
        return "review_required_before_action"
    if lane == "candidate_backlog":
        return "review_then_file_backlog"
    if lane == "graph_patch_candidate":
        return "review_then_apply_graph_correction"
    if lane == "status_only":
        return "display_until_user_requests_action"
    return "no_action"


def build_feedback_review_queue(
    project_id: str,
    snapshot_id: str,
    *,
    feedback_kind: str = "",
    status: str = "",
    node_id: str = "",
    source_round: str = "",
    lane: str = "",
    group_by: str = "target",
    include_status_observations: bool = False,
    include_resolved: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a dashboard-safe, grouped projection over raw feedback items.

    Raw feedback remains append-only in ``reconcile-feedback-state.json``.  This
    view collapses repeated suggestions by node/target/type and hides
    status-only observations by default so semantic expansion can be reviewed in
    human-sized chunks.
    """
    raw_items = list_feedback_items(
        project_id,
        snapshot_id,
        feedback_kind=feedback_kind,
        status=status,
        node_id=node_id,
        limit=None,
    )
    if source_round:
        raw_items = [
            item for item in raw_items
            if str(item.get("source_round") or "") == str(source_round)
        ]
    group_by = str(group_by or "target").strip().lower()
    if group_by not in {"target", "feature", "node", "source_node"}:
        group_by = "target"

    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_lane_all: dict[str, int] = {}
    hidden_status = 0
    hidden_resolved = 0
    groups: dict[str, dict[str, Any]] = {}

    for item in raw_items:
        kind = str(item.get("feedback_kind") or "")
        item_status = str(item.get("status") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_status[item_status] = by_status.get(item_status, 0) + 1
        item_lane = _feedback_lane(item)
        by_lane_all[item_lane] = by_lane_all.get(item_lane, 0) + 1
        if lane and item_lane != lane:
            continue
        if item_lane == "status_only" and not include_status_observations:
            hidden_status += 1
            continue
        if item_lane == "resolved" and not include_resolved:
            hidden_resolved += 1
            continue

        key = _queue_group_key(item, item_lane, group_by=group_by)
        group = groups.get(key)
        nodes = _source_nodes(item)
        priority = str(item.get("priority") or "P3").upper()
        target_id = str(item.get("target_id") or "")
        target_type = str(item.get("target_type") or "")
        issue_type = str(item.get("issue_type") or "")
        if group is None:
            group = {
                "queue_id": f"fq-{_short_hash({'snapshot_id': snapshot_id, 'key': key})}",
                "lane": item_lane,
                "action_hint": _queue_action_hint(item_lane),
                "priority": priority,
                "source_node_ids": nodes,
                "target_type": target_type,
                "target_id": target_id,
                "issue_type": issue_type,
                "target_ids": [],
                "target_count": 0,
                "target_type_counts": {},
                "issue_type_counts": {},
                "status_observation_category": str(
                    item.get("reviewed_status_observation_category")
                    or item.get("status_observation_category")
                    or ""
                ),
                "representative_feedback_id": str(item.get("feedback_id") or ""),
                "representative_issue": str(item.get("issue") or ""),
                "feedback_ids": [],
                "item_count": 0,
                "suppressed_count": 0,
                "requires_human_signoff": bool(item.get("requires_human_signoff")),
                "confidence": float(item.get("confidence") or 0.0),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
            groups[key] = group
        else:
            group["source_node_ids"] = sorted(set(_string_list(group.get("source_node_ids")) + nodes))
            if _priority_rank(priority) < _priority_rank(group.get("priority")):
                group["priority"] = priority
                group["representative_feedback_id"] = str(item.get("feedback_id") or "")
                group["representative_issue"] = str(item.get("issue") or "")
                group["confidence"] = float(item.get("confidence") or 0.0)
        group["feedback_ids"].append(str(item.get("feedback_id") or ""))
        if target_id and target_id not in group["target_ids"]:
            group["target_ids"].append(target_id)
        group["target_count"] = len(group["target_ids"])
        if target_type:
            counts = group["target_type_counts"]
            counts[target_type] = int(counts.get(target_type) or 0) + 1
        if issue_type:
            counts = group["issue_type_counts"]
            counts[issue_type] = int(counts.get(issue_type) or 0) + 1
        group["item_count"] = int(group.get("item_count") or 0) + 1
        group["suppressed_count"] = max(0, int(group["item_count"]) - 1)
        group["requires_human_signoff"] = bool(group.get("requires_human_signoff") or item.get("requires_human_signoff"))
        group["updated_at"] = max(str(group.get("updated_at") or ""), str(item.get("updated_at") or ""))

    grouped = list(groups.values())
    grouped.sort(
        key=lambda group: (
            _lane_rank(str(group.get("lane") or "")),
            _priority_rank(group.get("priority")),
            -int(group.get("item_count") or 0),
            str(group.get("queue_id") or ""),
        )
    )
    if limit is not None and limit >= 0:
        grouped = grouped[: int(limit)]

    by_lane_visible: dict[str, int] = {}
    for group in grouped:
        group_lane = str(group.get("lane") or "")
        by_lane_visible[group_lane] = by_lane_visible.get(group_lane, 0) + 1

    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "group_by": group_by,
        "summary": {
            "raw_count": len(raw_items),
            "visible_group_count": len(grouped),
            "visible_item_count": sum(int(group.get("item_count") or 0) for group in grouped),
            "hidden_status_observation_count": hidden_status,
            "hidden_resolved_count": hidden_resolved,
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": dict(sorted(by_status.items())),
            "by_lane_all_items": dict(sorted(by_lane_all.items())),
            "by_lane_visible_groups": dict(sorted(by_lane_visible.items())),
        },
        "groups": grouped,
        "count": len(grouped),
    }


def _short_hash(payload: Any, length: int = 10) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:length]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return _json(value)
    except Exception:
        return str(value)


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw if str(item or "").strip()]
    return [str(raw)]


def _issue_type(issue: dict[str, Any]) -> str:
    return str(issue.get("type") or issue.get("kind") or "").strip()


def _issue_summary(issue: dict[str, Any]) -> str:
    return str(issue.get("summary") or issue.get("issue") or issue.get("detail") or "").strip()


def _source_nodes(issue: dict[str, Any]) -> list[str]:
    nodes = _string_list(issue.get("source_node_ids") or issue.get("node_ids") or issue.get("nodes"))
    source_node_id = str(issue.get("source_node_id") or "").strip()
    if source_node_id and source_node_id not in nodes:
        nodes.insert(0, source_node_id)
    node_id = str(issue.get("node_id") or "").strip()
    if node_id and node_id not in nodes:
        nodes.insert(0, node_id)
    return nodes


def _round_number(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text.startswith("round-"):
        text = text.split("round-", 1)[1]
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _issue_round(issue: dict[str, Any]) -> int | None:
    for key in ("feedback_round", "source_round", "round"):
        number = _round_number(issue.get(key))
        if number is not None:
            return number
    return None


def _select_semantic_state_issues(
    semantic_state: dict[str, Any],
    *,
    source_round: str | int = "",
    node_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    requested_round = _round_number(source_round)
    requested_nodes = {str(node_id).strip() for node_id in (node_ids or []) if str(node_id).strip()}
    node_semantics = semantic_state.get("node_semantics")
    if isinstance(node_semantics, dict) and (requested_round is not None or requested_nodes):
        selected: list[dict[str, Any]] = []
        for node_id, raw_entry in sorted(node_semantics.items()):
            if not isinstance(raw_entry, dict):
                continue
            node_id = str(node_id)
            if requested_nodes and node_id not in requested_nodes:
                continue
            entry_round = _round_number(raw_entry.get("feedback_round"))
            if requested_round is not None and entry_round != requested_round:
                continue
            for raw_issue in raw_entry.get("open_issues") or []:
                if not isinstance(raw_issue, dict):
                    continue
                issue = dict(raw_issue)
                if not _source_nodes(issue):
                    issue["node_id"] = node_id
                selected.append(issue)
        return selected

    raw_issues = semantic_state.get("open_issues")
    if not isinstance(raw_issues, list):
        return []
    selected = []
    for raw_issue in raw_issues:
        if not isinstance(raw_issue, dict):
            continue
        issue_nodes = set(_source_nodes(raw_issue))
        if requested_nodes and not issue_nodes.intersection(requested_nodes):
            continue
        issue_round = _issue_round(raw_issue)
        if requested_round is not None and issue_round is not None and issue_round != requested_round:
            continue
        selected.append(raw_issue)
    return selected


def _semantic_state_feedback_rounds(semantic_state: dict[str, Any]) -> list[int]:
    rounds: set[int] = set()
    node_semantics = semantic_state.get("node_semantics")
    if isinstance(node_semantics, dict):
        for raw_entry in node_semantics.values():
            if not isinstance(raw_entry, dict) or not raw_entry.get("open_issues"):
                continue
            entry_round = _round_number(raw_entry.get("feedback_round"))
            if entry_round is not None:
                rounds.add(entry_round)
    raw_issues = semantic_state.get("open_issues")
    if isinstance(raw_issues, list):
        for raw_issue in raw_issues:
            if not isinstance(raw_issue, dict):
                continue
            issue_round = _issue_round(raw_issue)
            if issue_round is not None:
                rounds.add(issue_round)
    return sorted(rounds)


def _round_label(round_number: int) -> str:
    return f"round-{round_number:03d}"


def _target_type(issue_type: str, summary: str, reason: str) -> str:
    text = f"{issue_type} {reason} {summary}".lower()
    if "relation" in text or "edge" in text or "dependency" in text:
        return "edge"
    if "doc" in text:
        return "doc"
    if "test" in text:
        return "test"
    if "config" in text or "yaml" in text or "env" in text:
        return "config"
    if "split" in text or "merge" in text or "dead" in text or "delete" in text:
        return "node"
    return "node"


def _confidence(issue_type: str, summary: str) -> float:
    text = f"{issue_type} {summary}".lower()
    if "confidence" in text:
        if "high" in text:
            return 0.8
        if "low" in text:
            return 0.35
    if "already present" in text or "mis-extraction" in text:
        return 0.75
    if "verify" in text or "consider" in text or "likely" in text:
        return 0.45
    return 0.6


def _priority(feedback_kind: str, issue_type: str, summary: str) -> str:
    text = f"{issue_type} {summary}".lower()
    if "p0" in text:
        return "P0"
    if "p1" in text:
        return "P1"
    if feedback_kind == KIND_NEEDS_OBSERVER_DECISION:
        return "P1"
    if "mis-extraction" in text or "false" in text:
        return "P1"
    if "missing_test_binding" in text or "missing_doc_binding" in text:
        return "P2"
    if feedback_kind == KIND_STATUS_OBSERVATION:
        return "P3"
    return "P2" if feedback_kind == KIND_PROJECT_IMPROVEMENT else "P3"


def infer_status_observation_category(item: dict[str, Any]) -> str:
    """Suggest a review category for a status-only graph/file observation."""
    issue_type = str(item.get("issue_type") or item.get("type") or "").lower()
    text = " ".join([
        issue_type,
        str(item.get("issue") or item.get("summary") or ""),
        str((item.get("evidence") or {}).get("reason") or ""),
    ]).lower()
    if "failed_test" in issue_type or "regression" in text or "test failure" in text:
        return STATUS_CATEGORY_PROJECT_REGRESSION
    if "stale_test" in issue_type or "test_expectation" in issue_type:
        return STATUS_CATEGORY_STALE_TEST
    if "doc_drift" in issue_type or "stale_doc" in issue_type:
        return STATUS_CATEGORY_DOC_DRIFT
    if (
        "missing_doc" in text
        or "missing_test" in text
        or "coverage" in text
    ):
        return STATUS_CATEGORY_COVERAGE_GAP
    if "orphan" in issue_type or "pending_file_decision" in issue_type or "unmapped" in issue_type:
        return STATUS_CATEGORY_ORPHAN_REVIEW
    if "false" in text or "ignore" in text:
        return STATUS_CATEGORY_FALSE_POSITIVE
    return STATUS_CATEGORY_NEEDS_HUMAN


def classify_open_issue(issue: dict[str, Any]) -> str:
    """Route one semantic open issue into a feedback lane.

    The classifier is intentionally conservative: structural split/merge/delete
    suggestions require observer or reviewer confirmation before they become
    graph deltas or project backlog rows.
    """
    issue_type = _issue_type(issue)
    reason = str(issue.get("reason") or "").strip()
    summary = _issue_summary(issue)
    text = f"{issue_type} {reason} {summary}".lower()

    uncertain = (
        "split_suggestions" in reason
        or "merge_suggestions" in reason
        or "dead_code_candidates" in reason
        or re.search(r"\b(consider|verify|confirm|audit|whether|if no|if zero|possible|likely)\b", text)
        or "two separate" in text
        or "source of truth" in text
        or "safe to delete" in text
    )
    if uncertain:
        return KIND_NEEDS_OBSERVER_DECISION

    graph_tokens = {
        "add_relation",
        "add_typed_relation",
        "graph_relation",
        "typed_relation",
        "feature_relation",
        "missing_relation",
        "intra_module_relation",
        "doc_binding",
        "add_doc_binding",
        "doc_binding_addition",
        "doc_link",
        "test_binding",
        "test_binding_realign",
        "test_link",
        "config_binding",
        "config_binding_addition",
        "prune_test_list",
        "remove_secondary_doc_refs",
        "review_typed_relation",
        "reclassify_role",
        "tighten_role",
    }
    if issue_type in graph_tokens or "relation" in text or "edge" in text:
        return KIND_GRAPH_CORRECTION

    if (
        "missing_test_binding" in text
        or "missing_doc_binding" in text
        or "missing_config_binding" in text
        or "coverage" in text
        or "drift" in text
        or "orphan" in text
        or "pending_decision" in text
        or "low confidence" in text
        or "low-confidence" in text
        or "weak test" in text
        or "weak doc" in text
        or "missing doc" in text
        or "missing test" in text
    ):
        return KIND_STATUS_OBSERVATION

    if (
        "add explicit" in text
        or "document " in text
        or "unit test" in text
        or "implement " in text
        or "refactor " in text
    ):
        return KIND_PROJECT_IMPROVEMENT

    return KIND_GRAPH_CORRECTION


def normalize_open_issue(
    issue: dict[str, Any],
    *,
    project_id: str,
    snapshot_id: str,
    source_round: str | int = "",
    created_by: str = "system",
    feedback_kind: str = "",
) -> dict[str, Any]:
    if not isinstance(issue, dict):
        raise ValueError("open issue must be an object")
    issue_type = _issue_type(issue)
    reason = str(issue.get("reason") or "").strip()
    summary = _issue_summary(issue)
    nodes = _source_nodes(issue)
    kind = feedback_kind or classify_open_issue(issue)
    seed = {
        "snapshot_id": snapshot_id,
        "source_round": str(source_round),
        "nodes": nodes,
        "type": issue_type,
        "reason": reason,
        "summary": summary,
    }
    feedback_id = str(issue.get("feedback_id") or issue.get("id") or f"rf-{_short_hash(seed)}")
    target_id = str(issue.get("target") or issue.get("target_id") or (nodes[0] if nodes else "")).strip()
    now = _utc_now()
    return {
        "feedback_id": feedback_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "source_snapshot_id": snapshot_id,
        "source_round": str(source_round),
        "source_node_ids": nodes,
        "feedback_kind": kind,
        "final_feedback_kind": "",
        "status": STATUS_CLASSIFIED,
        "target_type": _target_type(issue_type, summary, reason),
        "target_id": target_id,
        "paths": _string_list(issue.get("paths") or issue.get("path")),
        "issue_type": issue_type,
        "issue": summary,
        "status_observation_category": (
            infer_status_observation_category({
                "issue_type": issue_type,
                "issue": summary,
                "evidence": {"reason": reason},
            })
            if kind == KIND_STATUS_OBSERVATION
            else ""
        ),
        "reviewed_status_observation_category": "",
        "evidence": {
            "reason": reason,
            "raw_issue": issue,
        },
        "suggested_action": summary,
        "confidence": _confidence(issue_type, summary),
        "priority": _priority(kind, issue_type, summary),
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "reviewer_decision": "",
        "reviewer_rationale": "",
        "reviewer_model": "",
        "reviewer_confidence": 0.0,
        "requires_human_signoff": kind == KIND_NEEDS_OBSERVER_DECISION,
        "accepted_by": "",
        "accepted_at": "",
        "backlog_bug_id": "",
    }


def _upsert_items(
    project_id: str,
    snapshot_id: str,
    items: list[dict[str, Any]],
    *,
    event_type: str,
    actor: str,
) -> dict[str, Any]:
    state = load_feedback_state(project_id, snapshot_id)
    existing = state.setdefault("items", {})
    now = _utc_now()
    events: list[dict[str, Any]] = []
    created = 0
    updated = 0
    for item in items:
        fid = str(item.get("feedback_id") or "")
        if not fid:
            fid = f"rf-{uuid.uuid4().hex[:10]}"
            item["feedback_id"] = fid
        previous = existing.get(fid)
        merged = {**(previous or {}), **item, "updated_at": now}
        if (
            event_type == "feedback.classified"
            and previous
            and previous.get("status") not in {"", STATUS_CLASSIFIED}
        ):
            merged["status"] = previous.get("status")
        existing[fid] = merged
        if previous:
            updated += 1
        else:
            created += 1
        events.append({
            "event_id": f"rfe-{uuid.uuid4().hex[:10]}",
            "event_type": event_type,
            "feedback_id": fid,
            "actor": actor,
            "created_at": now,
            "item": merged,
        })
    save_feedback_state(project_id, snapshot_id, state)
    _append_jsonl(feedback_events_path(project_id, snapshot_id), events)
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "created": created,
        "updated": updated,
        "count": len(items),
        "state_path": str(feedback_state_path(project_id, snapshot_id)),
        "events_path": str(feedback_events_path(project_id, snapshot_id)),
        "items": [existing[str(item["feedback_id"])] for item in items],
    }


def classify_semantic_open_issues(
    project_id: str,
    snapshot_id: str,
    *,
    source_round: str | int = "",
    created_by: str = "system",
    issues: list[dict[str, Any]] | None = None,
    feedback_kind: str = "",
    limit: int | None = None,
    node_ids: list[str] | None = None,
) -> dict[str, Any]:
    raw_issues = issues
    if raw_issues is None:
        semantic_state = _read_json(semantic_graph_state_path(project_id, snapshot_id), {})
        raw_issues = (
            _select_semantic_state_issues(
                semantic_state,
                source_round=source_round,
                node_ids=node_ids,
            )
            if isinstance(semantic_state, dict)
            else []
        )
    if not isinstance(raw_issues, list):
        raw_issues = []
    selected = [item for item in raw_issues if isinstance(item, dict)]
    if node_ids:
        requested_nodes = {str(node_id).strip() for node_id in node_ids if str(node_id).strip()}
        selected = [
            item for item in selected
            if not requested_nodes or set(_source_nodes(item)).intersection(requested_nodes)
        ]
    if limit is not None and limit >= 0:
        selected = selected[: int(limit)]
    items = [
        normalize_open_issue(
            issue,
            project_id=project_id,
            snapshot_id=snapshot_id,
            source_round=source_round,
            created_by=created_by,
            feedback_kind=feedback_kind,
        )
        for issue in selected
    ]
    result = _upsert_items(
        project_id,
        snapshot_id,
        items,
        event_type="feedback.classified",
        actor=created_by,
    )
    result["summary"] = feedback_summary(project_id, snapshot_id)
    return result


def classify_semantic_state_rounds(
    project_id: str,
    snapshot_id: str,
    *,
    created_by: str = "system",
    source_rounds: list[str | int] | None = None,
    limit_per_round: int | None = None,
) -> dict[str, Any]:
    """Classify all round-scoped semantic graph-state issues for a snapshot."""
    semantic_state = _read_json(semantic_graph_state_path(project_id, snapshot_id), {})
    if not isinstance(semantic_state, dict):
        semantic_state = {}
    rounds = [_round_number(raw_round) for raw_round in (source_rounds or [])]
    if not source_rounds:
        rounds = _semantic_state_feedback_rounds(semantic_state)
    normalized_rounds = sorted({round_number for round_number in rounds if round_number is not None})

    results: list[dict[str, Any]] = []
    created = 0
    updated = 0
    total = 0
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for round_number in normalized_rounds:
        round_label = _round_label(round_number)
        result = classify_semantic_open_issues(
            project_id,
            snapshot_id,
            source_round=round_label,
            created_by=created_by,
            limit=limit_per_round,
        )
        results.append({
            "source_round": round_label,
            "created": result.get("created", 0),
            "updated": result.get("updated", 0),
            "count": result.get("count", 0),
            "summary": result.get("summary", {}),
        })
        created += int(result.get("created") or 0)
        updated += int(result.get("updated") or 0)
        total += int(result.get("count") or 0)
        for item in result.get("items") or []:
            kind = str(item.get("feedback_kind") or "")
            status = str(item.get("status") or "")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1

    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "rounds": [_round_label(round_number) for round_number in normalized_rounds],
        "round_count": len(normalized_rounds),
        "created": created,
        "updated": updated,
        "count": total,
        "summary": {
            "count": total,
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": dict(sorted(by_status.items())),
        },
        "results": results,
        "state_path": str(feedback_state_path(project_id, snapshot_id)),
        "events_path": str(feedback_events_path(project_id, snapshot_id)),
    }


def feedback_summary(project_id: str, snapshot_id: str) -> dict[str, Any]:
    items = list_feedback_items(project_id, snapshot_id)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_status_category: dict[str, int] = {}
    for item in items:
        by_kind[str(item.get("feedback_kind") or "")] = by_kind.get(str(item.get("feedback_kind") or ""), 0) + 1
        by_status[str(item.get("status") or "")] = by_status.get(str(item.get("status") or ""), 0) + 1
        if item.get("feedback_kind") == KIND_STATUS_OBSERVATION:
            category = str(
                item.get("reviewed_status_observation_category")
                or item.get("status_observation_category")
                or ""
            )
            if category:
                by_status_category[category] = by_status_category.get(category, 0) + 1
    return {
        "count": len(items),
        "by_kind": by_kind,
        "by_status": by_status,
        "by_status_observation_category": by_status_category,
    }


def _parse_ai_review(ai_result: dict[str, Any]) -> dict[str, Any]:
    decision = str(
        ai_result.get("decision")
        or ai_result.get("reviewer_decision")
        or ai_result.get("final_feedback_kind")
        or ""
    ).strip()
    if decision not in REVIEW_DECISIONS:
        decision = "needs_human_signoff"
    category = str(
        ai_result.get("status_observation_category")
        or ai_result.get("observation_category")
        or ai_result.get("category")
        or ""
    ).strip()
    if category and category not in STATUS_OBSERVATION_CATEGORIES:
        category = STATUS_CATEGORY_NEEDS_HUMAN
    return {
        "decision": decision,
        "status_observation_category": category,
        "rationale": str(ai_result.get("rationale") or ai_result.get("reviewer_rationale") or ""),
        "confidence": float(ai_result.get("confidence") or ai_result.get("reviewer_confidence") or 0.0),
        "model": _as_text(ai_result.get("_ai_route") or ai_result.get("model") or ""),
        "raw": ai_result,
    }


def review_feedback_item(
    project_id: str,
    snapshot_id: str,
    feedback_id: str,
    *,
    decision: str = "",
    rationale: str = "",
    confidence: float | None = None,
    status_observation_category: str = "",
    actor: str = "observer",
    accept: bool = False,
    ai_call: ReviewerAiCall | None = None,
) -> dict[str, Any]:
    state = load_feedback_state(project_id, snapshot_id)
    item = dict((state.get("items") or {}).get(feedback_id) or {})
    if not item:
        raise KeyError(f"feedback item not found: {feedback_id}")

    ai_review: dict[str, Any] = {}
    if not decision and ai_call is not None:
        payload = {
            "instructions": {
                "reviewer": "reconcile_feedback_reviewer",
                "mutate_project_files": False,
                "allowed_decisions": sorted(REVIEW_DECISIONS),
                "status_observation_categories": sorted(STATUS_OBSERVATION_CATEGORIES),
                "decision_meaning": {
                    KIND_GRAPH_CORRECTION: "Only graph/semantic state should change.",
                    KIND_PROJECT_IMPROVEMENT: "Project code/docs/tests likely need a backlog item.",
                    KIND_STATUS_OBSERVATION: "Keep this as visible graph/file status until a user chooses an action.",
                    KIND_FALSE_POSITIVE: "Close the feedback without action.",
                    "needs_human_signoff": "Evidence is insufficient; user or observer must decide.",
                },
                "status_observation_category_meaning": {
                    STATUS_CATEGORY_STALE_TEST: "A test likely asserts an old contract and may need update after user approval.",
                    STATUS_CATEGORY_DOC_DRIFT: "A linked document may be stale relative to changed graph/code state.",
                    STATUS_CATEGORY_COVERAGE_GAP: "A node/file is missing doc/test/config coverage or graph attachment.",
                    STATUS_CATEGORY_PROJECT_REGRESSION: "The observation may indicate a product/code behavior regression.",
                    STATUS_CATEGORY_ORPHAN_REVIEW: "An orphan or pending file needs keep/attach/delete review.",
                    STATUS_CATEGORY_FALSE_POSITIVE: "The observation should be closed without action.",
                    STATUS_CATEGORY_NEEDS_HUMAN: "Evidence is insufficient for automatic routing.",
                },
                "output_contract": (
                    "Return JSON with decision, optional status_observation_category, "
                    "rationale, confidence, and model. Do not mutate project files."
                ),
            },
            "feedback": item,
        }
        ai_review = _parse_ai_review(ai_call("reconcile_feedback_review", payload) or {})
        decision = ai_review["decision"]
        status_observation_category = (
            status_observation_category
            or ai_review.get("status_observation_category", "")
        )
        rationale = rationale or ai_review["rationale"]
        confidence = confidence if confidence is not None else ai_review["confidence"]

    if not decision:
        if item.get("feedback_kind") == KIND_NEEDS_OBSERVER_DECISION:
            decision = "needs_human_signoff"
        else:
            decision = item.get("feedback_kind") or "needs_human_signoff"
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"invalid reviewer decision: {decision}")
    if status_observation_category and status_observation_category not in STATUS_OBSERVATION_CATEGORIES:
        raise ValueError(f"invalid status_observation_category: {status_observation_category}")
    if decision == KIND_FALSE_POSITIVE:
        status_observation_category = STATUS_CATEGORY_FALSE_POSITIVE
    if item.get("feedback_kind") == KIND_STATUS_OBSERVATION and not status_observation_category:
        status_observation_category = infer_status_observation_category(item)
    now = _utc_now()
    item.update({
        "reviewer_decision": decision,
        "reviewed_status_observation_category": status_observation_category,
        "reviewer_rationale": rationale,
        "reviewer_confidence": float(confidence if confidence is not None else item.get("confidence") or 0.0),
        "reviewer_model": ai_review.get("model") or item.get("reviewer_model") or "",
        "reviewed_by": actor,
        "reviewed_at": now,
        "updated_at": now,
    })
    if decision == KIND_FALSE_POSITIVE:
        item["status"] = STATUS_REJECTED
        item["final_feedback_kind"] = KIND_FALSE_POSITIVE
        item["requires_human_signoff"] = False
    elif decision == "needs_human_signoff":
        item["status"] = STATUS_NEEDS_HUMAN_SIGNOFF
        item["requires_human_signoff"] = True
    else:
        item["final_feedback_kind"] = decision
        item["requires_human_signoff"] = False
        item["status"] = STATUS_ACCEPTED if accept else STATUS_REVIEWED
        if accept:
            item["accepted_by"] = actor
            item["accepted_at"] = now

    return _upsert_items(
        project_id,
        snapshot_id,
        [item],
        event_type="feedback.reviewed",
        actor=actor,
    )


def build_project_improvement_backlog(
    project_id: str,
    snapshot_id: str,
    feedback_id: str,
    *,
    bug_id: str = "",
    actor: str = "observer",
    allow_status_observation: bool = False,
) -> dict[str, Any]:
    state = load_feedback_state(project_id, snapshot_id)
    item = dict((state.get("items") or {}).get(feedback_id) or {})
    if not item:
        raise KeyError(f"feedback item not found: {feedback_id}")
    final_kind = item.get("final_feedback_kind") or item.get("feedback_kind")
    if final_kind != KIND_PROJECT_IMPROVEMENT:
        if not (allow_status_observation and final_kind == KIND_STATUS_OBSERVATION):
            raise ValueError(f"feedback item is not project_improvement: {feedback_id}")
    nodes = item.get("source_node_ids") or []
    suffix = _short_hash({"snapshot_id": snapshot_id, "feedback_id": feedback_id}, 8)
    bug = bug_id or f"OPT-BACKLOG-FEEDBACK-{snapshot_id[:12]}-{suffix}"
    paths = item.get("paths") or []
    title_node = f" {nodes[0]}" if nodes else ""
    payload = {
        "actor": actor,
        "title": (
            f"Project improvement from reconcile feedback{title_node}"
            if final_kind == KIND_PROJECT_IMPROVEMENT
            else f"User-requested backlog from reconcile status{title_node}"
        ),
        "status": "OPEN",
        "priority": item.get("priority") or "P2",
        "target_files": paths,
        "test_files": [],
        "acceptance_criteria": [
            "Backlog row records source semantic feedback and snapshot provenance.",
            "Implementation updates project files only through chain or authorized MF.",
            "Scope reconcile updates graph state after the project change lands.",
        ],
        "details_md": (
            "Filed from reconcile feedback.\n\n"
            f"snapshot_id: {snapshot_id}\n"
            f"feedback_id: {feedback_id}\n"
            f"nodes: {', '.join(nodes)}\n"
            f"issue: {item.get('issue') or ''}\n\n"
            f"reviewer_decision: {item.get('reviewer_decision') or item.get('feedback_kind')}\n"
            f"status_observation_category: {item.get('reviewed_status_observation_category') or item.get('status_observation_category') or ''}\n"
            f"reviewer_rationale: {item.get('reviewer_rationale') or ''}\n"
        ),
        "provenance_paths": [
            str(feedback_state_path(project_id, snapshot_id)).replace("\\", "/"),
            f"graph_snapshot:{snapshot_id}",
            f"reconcile_feedback:{feedback_id}",
        ],
        "chain_trigger_json": {
            "source": "reconcile_feedback",
            "snapshot_id": snapshot_id,
            "feedback_id": feedback_id,
            "feedback_kind": final_kind,
            "status_observation_category": (
                item.get("reviewed_status_observation_category")
                or item.get("status_observation_category")
                or ""
            ),
            "source_node_ids": nodes,
        },
        "force_admit": True,
    }
    return {"bug_id": bug, "payload": payload, "feedback": item}


def mark_feedback_backlog_filed(
    project_id: str,
    snapshot_id: str,
    feedback_id: str,
    *,
    bug_id: str,
    actor: str = "observer",
) -> dict[str, Any]:
    state = load_feedback_state(project_id, snapshot_id)
    item = dict((state.get("items") or {}).get(feedback_id) or {})
    if not item:
        raise KeyError(f"feedback item not found: {feedback_id}")
    item["status"] = STATUS_BACKLOG_FILED
    item["backlog_bug_id"] = bug_id
    item["updated_at"] = _utc_now()
    return _upsert_items(
        project_id,
        snapshot_id,
        [item],
        event_type="feedback.backlog_filed",
        actor=actor,
    )


__all__ = [
    "FEEDBACK_EVENTS_NAME",
    "FEEDBACK_STATE_NAME",
    "KIND_GRAPH_CORRECTION",
    "KIND_PROJECT_IMPROVEMENT",
    "KIND_STATUS_OBSERVATION",
    "KIND_NEEDS_OBSERVER_DECISION",
    "KIND_FALSE_POSITIVE",
    "STATUS_OBSERVATION_CATEGORIES",
    "STATUS_CATEGORY_STALE_TEST",
    "STATUS_CATEGORY_DOC_DRIFT",
    "STATUS_CATEGORY_COVERAGE_GAP",
    "STATUS_CATEGORY_PROJECT_REGRESSION",
    "STATUS_CATEGORY_ORPHAN_REVIEW",
    "STATUS_CATEGORY_FALSE_POSITIVE",
    "STATUS_CATEGORY_NEEDS_HUMAN",
    "classify_open_issue",
    "classify_semantic_open_issues",
    "classify_semantic_state_rounds",
    "build_feedback_review_queue",
    "infer_status_observation_category",
    "list_feedback_items",
    "load_feedback_state",
    "review_feedback_item",
    "build_project_improvement_backlog",
    "mark_feedback_backlog_filed",
    "feedback_summary",
    "feedback_state_path",
    "feedback_events_path",
]
