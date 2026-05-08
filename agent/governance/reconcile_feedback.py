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
    nodes = _string_list(issue.get("node_ids") or issue.get("nodes"))
    node_id = str(issue.get("node_id") or "").strip()
    if node_id and node_id not in nodes:
        nodes.insert(0, node_id)
    return nodes


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
) -> dict[str, Any]:
    raw_issues = issues
    if raw_issues is None:
        semantic_state = _read_json(semantic_graph_state_path(project_id, snapshot_id), {})
        raw_issues = semantic_state.get("open_issues") if isinstance(semantic_state, dict) else []
    if not isinstance(raw_issues, list):
        raw_issues = []
    selected = [item for item in raw_issues if isinstance(item, dict)]
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


def feedback_summary(project_id: str, snapshot_id: str) -> dict[str, Any]:
    items = list_feedback_items(project_id, snapshot_id)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for item in items:
        by_kind[str(item.get("feedback_kind") or "")] = by_kind.get(str(item.get("feedback_kind") or ""), 0) + 1
        by_status[str(item.get("status") or "")] = by_status.get(str(item.get("status") or ""), 0) + 1
    return {
        "count": len(items),
        "by_kind": by_kind,
        "by_status": by_status,
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
    return {
        "decision": decision,
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
                "decision_meaning": {
                    KIND_GRAPH_CORRECTION: "Only graph/semantic state should change.",
                    KIND_PROJECT_IMPROVEMENT: "Project code/docs/tests likely need a backlog item.",
                    KIND_STATUS_OBSERVATION: "Keep this as visible graph/file status until a user chooses an action.",
                    KIND_FALSE_POSITIVE: "Close the feedback without action.",
                    "needs_human_signoff": "Evidence is insufficient; user or observer must decide.",
                },
            },
            "feedback": item,
        }
        ai_review = _parse_ai_review(ai_call("reconcile_feedback_review", payload) or {})
        decision = ai_review["decision"]
        rationale = rationale or ai_review["rationale"]
        confidence = confidence if confidence is not None else ai_review["confidence"]

    if not decision:
        if item.get("feedback_kind") == KIND_NEEDS_OBSERVER_DECISION:
            decision = "needs_human_signoff"
        else:
            decision = item.get("feedback_kind") or "needs_human_signoff"
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"invalid reviewer decision: {decision}")
    now = _utc_now()
    item.update({
        "reviewer_decision": decision,
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
    "classify_open_issue",
    "classify_semantic_open_issues",
    "list_feedback_items",
    "load_feedback_state",
    "review_feedback_item",
    "build_project_improvement_backlog",
    "mark_feedback_backlog_filed",
    "feedback_summary",
    "feedback_state_path",
    "feedback_events_path",
]
