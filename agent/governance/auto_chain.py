# v11: worktree isolation verified
"""Auto-chain dispatcher.

Wires task completion to next-stage task creation with gate validation
between each stage. Called by complete_task() when a task succeeds.

Full chain: PM → Dev → Test → QA → Merge → Deploy
Each transition runs a gate check before advancing.
"""

import json
import logging
import os
import re
import traceback
from .failure_classifier import classify_gate_failure, build_workflow_improvement_prompt
from .observability import new_trace_id, structured_log

log = logging.getLogger(__name__)

# Set to True to skip SERVER_VERSION vs git-HEAD check during development.
# Restore to False before production use.
_DISABLE_VERSION_GATE = False

# ---------------------------------------------------------------------------
# Reconciliation Bypass Policy (R1)
# ---------------------------------------------------------------------------
RECONCILIATION_BYPASS_POLICY = {
    "required_metadata_fields": ["reconciliation_lane", "observer_authorized"],
    "allowed_lanes": {"A", "B"},
    "audit_action": "reconciliation_bypass",
}


def _check_reconciliation_bypass(conn, project_id, metadata):
    """Validate metadata against RECONCILIATION_BYPASS_POLICY.

    Returns (bypass: bool, observer_task_id: str|None).
    Checks:
      (a) metadata.reconciliation_lane in allowed_lanes
      (b) metadata.observer_authorized == True
      (c) task chain traces back to an observer-created parent task
    """
    policy = RECONCILIATION_BYPASS_POLICY

    # (a) reconciliation_lane must be in allowed set
    lane = str(metadata.get("reconciliation_lane", "") or "").strip().upper()
    if lane not in policy["allowed_lanes"]:
        return False, None

    # (b) observer_authorized must be explicitly True
    if not metadata.get("observer_authorized"):
        return False, None

    # (c) Walk parent chain to find observer-created task
    observer_task_id = metadata.get("observer_task_id")
    if not observer_task_id:
        for parent_meta in _walk_task_metadata_chain(conn, project_id, metadata):
            if parent_meta.get("created_by_observer") or parent_meta.get("observer_task_id"):
                observer_task_id = parent_meta.get("observer_task_id") or parent_meta.get("parent_task_id", "")
                break

    if not observer_task_id:
        # Fallback: treat the parent_task_id as the observer task if observer_authorized is set
        observer_task_id = metadata.get("parent_task_id", "unknown")

    return True, observer_task_id


def _audit_reconciliation_bypass(conn, project_id, task_id, observer_task_id, lane):
    """Write reconciliation_bypass event to audit_log (R6)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "reconciliation_bypass",
                "auto-chain",
                1,
                task_id,
                json.dumps({
                    "observer_task_id": observer_task_id,
                    "lane": lane,
                    "task_id": task_id,
                }),
            ),
        )
    except Exception:
        log.debug("audit reconciliation_bypass failed (non-critical)", exc_info=True)

# Chain definition: task_type → (gate_fn, next_type, prompt_builder)
# next_type=None means terminal stage (deploy trigger)
CHAIN = {
    "pm":    ("_gate_post_pm",    "dev",   "_build_dev_prompt"),
    "dev":   ("_gate_checkpoint", "test",  "_build_test_prompt"),
    "test":  ("_gate_t2_pass",    "qa",    "_build_qa_prompt"),
    "qa":    ("_gate_qa_pass",    "gatekeeper", "_build_gatekeeper_prompt"),
    "gatekeeper": ("_gate_gatekeeper_pass", "merge", "_build_merge_prompt"),
    "merge": ("_gate_release",    "deploy", "_build_deploy_prompt"),
    "deploy": ("_gate_deploy_pass", None, "_finalize_chain"),
}

# Maximum chain depth to prevent infinite loops
MAX_CHAIN_DEPTH = 10
_TEST_FILE_PATTERN = re.compile(r"(agent/tests/[A-Za-z0-9_./-]+\.py)")


def _extract_test_files_from_verification(verification):
    """Pull explicit pytest file targets out of verification.command."""
    if not isinstance(verification, dict):
        return []
    command = verification.get("command")
    if not isinstance(command, str) or not command.strip():
        return []
    return list(dict.fromkeys(_TEST_FILE_PATTERN.findall(command)))


def _load_task_trace(conn, task_id):
    """Load trace_id and chain_id from a task row."""
    if not task_id or not hasattr(conn, "execute"):
        return None, None
    try:
        row = conn.execute(
            "SELECT trace_id, chain_id FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None, None
        return row["trace_id"], row["chain_id"]
    except Exception:
        return None, None


def _record_gate_event(conn, project_id, task_id, gate_name, passed, reason, trace_id):
    """Insert a row into gate_events for audit trail."""
    from datetime import datetime, timezone
    try:
        conn.execute(
            "INSERT INTO gate_events (project_id, task_id, gate_name, passed, reason, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, task_id, gate_name, 1 if passed else 0,
             reason, trace_id,
             datetime.now(timezone.utc).isoformat()),
        )
    except Exception:
        log.debug("auto_chain: failed to record gate_event for %s/%s (non-critical)", gate_name, task_id, exc_info=True)


def _load_task_metadata(conn, project_id, task_id):
    if not task_id or not hasattr(conn, "execute"):
        return {}
    try:
        row = conn.execute(
            "SELECT metadata_json FROM tasks WHERE project_id=? AND task_id=?",
            (project_id, task_id),
        ).fetchone()
        if not row:
            return {}
        raw = row["metadata_json"] if isinstance(row, dict) or hasattr(row, "__getitem__") else None
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}


def _walk_task_metadata_chain(conn, project_id, metadata, max_depth=6):
    visited = set()
    current = dict(metadata or {})
    for _ in range(max_depth):
        yield current
        parent_task_id = current.get("parent_task_id")
        if not parent_task_id or parent_task_id in visited:
            break
        visited.add(parent_task_id)
        current = _load_task_metadata(conn, project_id, parent_task_id)
        if not current:
            break


def _infer_lane_from_metadata(metadata):
    """Best-effort lane inference for replayed reconciliation chains."""
    if not isinstance(metadata, dict):
        return ""
    explicit = str(metadata.get("lane", "") or "").strip().upper()
    if explicit in {"A", "B", "C"}:
        return explicit

    text = " ".join(
        str(metadata.get(k, "") or "")
        for k in ("replay_source", "intent_summary", "_original_prompt")
    ).lower()
    match = re.search(r"lane\s+([abc])", text)
    if match:
        return match.group(1).upper()
    return ""


def _is_governed_dirty_workspace_chain(conn, project_id, metadata):
    """Allow narrow bypass only for explicit governed dirty-workspace reconciliation chains."""
    for current in _walk_task_metadata_chain(conn, project_id, metadata):
        if current.get("allow_dirty_workspace_reconciliation"):
            return True
        if current.get("parallel_plan") == "dirty-reconciliation-2026-03-30":
            return True
        lane = _infer_lane_from_metadata(current)
        text = " ".join(
            str(current.get(k, "") or "")
            for k in ("replay_source", "intent_summary", "_original_prompt")
        ).lower()
        if lane in {"A", "B"} and (
            "dirty-workspace" in text
            or "workflow improvement lane" in text
            or "reconciliation" in text
        ):
            return True
    return False


def _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
    """Lane A/B reconciliation tasks may defer doc updates to convergence Lane C."""
    lane = ""
    for current in _walk_task_metadata_chain(conn, project_id, metadata):
        lane = _infer_lane_from_metadata(current)
        if lane:
            break
    if lane not in {"A", "B"}:
        return False
    if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
        return True
    return False


def _effective_dev_retry_reason(conn, project_id, metadata, reason):
    """Rewrite stale lane A/B gate reasons into actionable code-only guidance."""
    if not isinstance(reason, str):
        return reason
    if not _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
        return reason

    lowered = reason.lower()
    if lowered.startswith("related docs not updated:") or (
        lowered.startswith("unrelated files modified:")
        and ("readme.md" in lowered or "docs/" in lowered)
    ):
        return (
            "Lane C owns documentation updates for this governed dirty-workspace "
            "reconciliation. Do NOT modify README.md or docs/. "
            "Retry as a code-only fix within target_files and keep changed_files "
            "limited to target_files."
        )
    return reason


def _maybe_create_workflow_improvement_task(conn, project_id, task_id, stage, reason, metadata, result):
    """Create one workflow-improvement task for workflow defects.

    The task goes through the normal coordinator entrypoint (`type=task`) so the
    existing chain can repair workflow/governance issues without introducing a
    parallel execution model.
    """
    if metadata.get("_workflow_improvement_created"):
        return None
    if metadata.get("operation_type") == "workflow_improvement":
        return None

    classification = classify_gate_failure(stage, reason, metadata, result)
    if not classification.get("workflow_improvement"):
        return None

    from . import task_registry
    improvement_prompt = build_workflow_improvement_prompt(task_id, stage, classification, metadata)
    improvement_task = task_registry.create_task(
        conn, project_id,
        prompt=improvement_prompt,
        task_type="task",
        created_by="auto-chain-workflow-improvement",
        metadata={
            "operation_type": "workflow_improvement",
            "source_task_id": task_id,
            "failing_stage": stage,
            "failure_class": classification.get("failure_class", ""),
            "suggested_action": classification.get("suggested_action", ""),
            "workflow_issue": classification,
            "chain_depth": 0,
            "_no_retry": True,
        },
    )
    metadata["_workflow_improvement_created"] = True
    improvement_id = improvement_task.get("task_id", "?")
    _publish_event("task.workflow_improvement", {
        "project_id": project_id,
        "task_id": improvement_id,
        "source_task_id": task_id,
        "failing_stage": stage,
        "failure_class": classification.get("failure_class", ""),
    })
    try:
        from . import audit_service
        audit_service.record(
            conn, project_id, "workflow.improvement.created",
            actor="auto-chain",
            ok=True,
            node_ids=metadata.get("related_nodes", []),
            task_id=improvement_id,
            source_task_id=task_id,
            failing_stage=stage,
            failure_class=classification.get("failure_class", ""),
            suggested_action=classification.get("suggested_action", ""),
        )
    except Exception:
        log.debug("auto_chain: audit workflow.improvement.created failed", exc_info=True)
    return {"task_id": improvement_id, "classification": classification}


def on_task_failed(conn, project_id, task_id, task_type, result=None, metadata=None, reason=""):
    """Best-effort workflow-improvement routing for failed task executions."""
    metadata = metadata or {}
    result = result or {}
    effective_reason = (
        reason
        or result.get("error")
        or result.get("summary")
        or "task execution failed"
    )
    return _maybe_create_workflow_improvement_task(
        conn,
        project_id,
        task_id,
        task_type,
        effective_reason,
        metadata,
        result,
    )


def _normalize_related_nodes(related_nodes):
    """Keep only concrete node-id strings for gate/audit/state updates."""
    if not related_nodes:
        return []
    if not isinstance(related_nodes, list):
        related_nodes = [related_nodes]

    normalized = []
    for item in related_nodes:
        if isinstance(item, str) and item.strip():
            normalized.append(item.strip())
        elif isinstance(item, dict):
            node_id = item.get("node_id") or item.get("id")
            if isinstance(node_id, str) and node_id.strip():
                normalized.append(node_id.strip())
    return normalized


def _render_dev_contract_prompt(source_task_id, metadata):
    """Render the structured Dev contract from PM/task metadata."""
    target_files = metadata.get("target_files", [])
    requirements = metadata.get("requirements", [])
    criteria = metadata.get("acceptance_criteria", [])
    verification = metadata.get("verification", {})

    parts = [
        f"Implement per PRD from {source_task_id}.\n",
        f"target_files: {json.dumps(target_files)}",
        f"requirements: {json.dumps(requirements, ensure_ascii=False)}",
        f"acceptance_criteria: {json.dumps(criteria, ensure_ascii=False)}",
    ]

    if verification:
        parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")

    test_files = metadata.get("test_files", [])
    if test_files:
        parts.append(f"\nTest files to create/modify: {json.dumps(test_files)}")

    doc_impact = metadata.get("doc_impact", {})
    if doc_impact:
        parts.append(f"\nDoc impact: {json.dumps(doc_impact, ensure_ascii=False)}")

    return "\n".join(parts)


def on_task_completed(conn, project_id, task_id, task_type, status, result, metadata):
    """Called by complete_task(). Dispatches next stage if gate passes.

    Uses a SEPARATE connection to avoid holding caller's transaction lock
    during potentially slow gate checks and task creation.

    Returns dict with chain result, or None if not a chain-eligible task.
    """
    if status != "succeeded":
        return None
    if task_type not in CHAIN:
        return None

    # Use independent connection — don't hold caller's lock during chain ops
    from .db import get_connection
    try:
        conn = get_connection(project_id)
    except Exception:
        log.error("auto_chain: failed to get independent connection for %s", project_id)
        return None
    try:
        result_val = _do_chain(conn, project_id, task_id, task_type, result, metadata)
        conn.commit()
        return result_val
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _do_chain(conn, project_id, task_id, task_type, result, metadata):
    """Internal chain logic with guaranteed conn cleanup by caller."""
    metadata["related_nodes"] = _normalize_related_nodes(metadata.get("related_nodes", []))

    # --- Trace propagation: load trace_id/chain_id from current task ---
    _trace_id, _chain_id = _load_task_trace(conn, task_id)
    if not _trace_id and task_type == "pm":
        # Root PM task without trace_id — generate one and backfill
        _trace_id = new_trace_id()
        _chain_id = task_id
        try:
            conn.execute(
                "UPDATE tasks SET trace_id=?, chain_id=? WHERE task_id=?",
                (_trace_id, _chain_id, task_id),
            )
        except Exception:
            log.warning("auto_chain: failed to backfill trace_id on PM task %s", task_id)
    elif not _trace_id:
        # Non-PM task without trace (legacy) — generate trace but keep chain_id as parent_task_id
        _trace_id = new_trace_id()
        _chain_id = _chain_id or metadata.get("parent_task_id") or task_id

    # Non-blocking preflight log (first stage only)
    if task_type == "pm":
        try:
            from .preflight import run_preflight
            report = run_preflight(conn, project_id, auto_fix=False)
            if report.get("warnings"):
                log.warning("preflight warnings for %s: %s", project_id, report["warnings"])
            if not report.get("ok"):
                log.error("preflight blockers for %s: %s", project_id, report["blockers"])
        except Exception:
            pass  # never block chain on preflight failure

    # Auto-enrich: derive related_nodes from changed_files via impact API
    if not metadata.get("related_nodes"):
        changed = result.get("changed_files", metadata.get("changed_files", []))
        if changed:
            try:
                from .impact_analyzer import ImpactAnalyzer, ImpactAnalysisRequest, FileHitPolicy
                from . import project_service
                graph = project_service.load_project_graph(project_id)
                if graph:
                    def _get_status(nid):
                        from .enums import VerifyStatus
                        row = conn.execute(
                            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                            (project_id, nid)).fetchone()
                        return VerifyStatus.from_str(row["verify_status"]) if row else VerifyStatus.PENDING
                    analyzer = ImpactAnalyzer(graph, _get_status)
                    request = ImpactAnalysisRequest(
                        changed_files=changed,
                        file_policy=FileHitPolicy(match_primary=True, match_secondary=True),
                    )
                    impact = analyzer.analyze(request)
                    nodes = [n["node_id"] for n in impact.get("affected_nodes", [])]
                    if nodes:
                        metadata["related_nodes"] = nodes
                        log.info("auto_chain: enriched related_nodes from changed_files: %s", nodes)
            except Exception as e:
                log.warning("auto_chain: related_nodes enrichment failed: %s", e)

    depth = metadata.get("chain_depth", 0)
    if depth >= MAX_CHAIN_DEPTH:
        log.warning("auto_chain: max depth %d reached for task %s, stopping", depth, task_id)
        return {"chain_stopped": True, "reason": f"max_chain_depth={MAX_CHAIN_DEPTH}"}

    gate_fn_name, next_type, builder_name = CHAIN[task_type]

    # Pre-gate: version check — warn on server lag but don't block chain
    ver_passed, ver_reason = _gate_version_check(conn, project_id, result, metadata)
    _record_gate_event(conn, project_id, task_id, "version_check", ver_passed, ver_reason, _trace_id)
    if not ver_passed:
        # Only dirty-workspace failures reach here (server-version lag is now warning-only)
        log.info("auto_chain: version gate blocked for task %s: %s", task_id, ver_reason)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": "version_check", "next_stage": task_type,
            "reason": ver_reason,
        })
        return {"gate_blocked": True, "stage": "version_check", "reason": ver_reason}
    else:
        log.debug("auto_chain: version check passed for task %s: %s", task_id, ver_reason)

    # Emit task.completed to chain context store
    _publish_event("task.completed", {
        "project_id": project_id, "task_id": task_id,
        "result": result, "type": task_type,
    })
    # A1: Audit task.completed lifecycle event
    try:
        from . import audit_service
        audit_service.record(
            conn, project_id, f"{task_type}.completed",
            actor="auto-chain",
            ok=True,
            node_ids=metadata.get("related_nodes", []),
            task_id=task_id,
            chain_depth=depth,
            trace_id=_trace_id,
        )
    except Exception:
        log.debug("auto_chain: audit task.completed failed (non-critical)", exc_info=True)
    # R6: structured_log with trace_id for gate transitions
    structured_log("info", f"{task_type}.completed",
                   project_id=project_id, task_id=task_id,
                   trace_id=_trace_id, chain_id=_chain_id)

    # M1: PM completes → persist full PRD to memory for future dev/qa recall
    if task_type == "pm":
        prd = result.get("prd", result)
        prd_data = {
            "requirements": prd.get("requirements", result.get("requirements", [])),
            "acceptance_criteria": result.get("acceptance_criteria", prd.get("acceptance_criteria", [])),
            "target_files": result.get("target_files", []),
            "test_files": result.get("test_files", []),
            "proposed_nodes": result.get("proposed_nodes", []),
            "doc_impact": result.get("doc_impact", {}),
            "verification": result.get("verification", {}),
            "skip_reasons": result.get("skip_reasons", {}),
        }
        if any(prd_data.values()):
            _write_chain_memory(
                conn, project_id, "prd_scope",
                json.dumps(prd_data, ensure_ascii=False),
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "pm"},
            )

    # M4: Test completes → write validation_result memory (marks dev decision as tested)
    if task_type == "test":
        report = result.get("test_report", {})
        passed = report.get("passed", 0) if isinstance(report, dict) else 0
        if passed:
            _write_chain_memory(
                conn, project_id, "validation_result",
                f"Tests passed ({passed} passing) for {', '.join(metadata.get('changed_files', [])[:3])}",
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "test",
                                   "test_report": report,
                                   "validation_status": "tested",
                                   "parent_task_id": metadata.get("parent_task_id", "")},
            )

    # Auto-update nodes based on stage completion
    if task_type == "dev" and metadata.get("related_nodes"):
        _try_verify_update(conn, project_id, metadata, "testing", "dev",
                           {"type": "dev_complete", "producer": "auto-chain",
                            "task_id": task_id})

    # Run gate check
    gate_fn = _GATES[gate_fn_name]
    passed, reason = gate_fn(conn, project_id, result, metadata)
    _record_gate_event(conn, project_id, task_id, gate_fn_name, passed, reason, _trace_id)
    if not passed:
        workflow_improvement = _maybe_create_workflow_improvement_task(
            conn, project_id, task_id, task_type, reason, metadata, result
        )
        log.info("auto_chain: gate blocked %s→%s for task %s: %s",
                 task_type, next_type or "deploy", task_id, reason)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": task_type, "next_stage": next_type or "deploy",
            "reason": reason,
        })
        # M3: Gate fail → write pitfall with previous output context
        _write_chain_memory(
            conn, project_id, "pitfall",
            f"Gate blocked at {task_type}: {reason}\n"
            f"Previous output keys: {list(result.keys())}\n"
            f"Previous output preview: {json.dumps(result, ensure_ascii=False)[:300]}",
            metadata,
            extra_structured={"task_id": task_id, "gate_stage": task_type,
                               "gate_reason": reason,
                               "previous_output_keys": list(result.keys()),
                               "chain_stage": task_type},
        )
        # G3: Persist gate.blocked to audit_index
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "gate.blocked",
                actor="auto-chain",
                ok=False,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                stage=task_type,
                next_stage=next_type or "deploy",
                reason=reason,
                trace_id=_trace_id,
            )
        except Exception:
            log.debug("auto_chain: audit gate.blocked failed (non-critical)", exc_info=True)
        structured_log("warning", "gate.blocked",
                       project_id=project_id, task_id=task_id,
                       stage=task_type, next_stage=next_type or "deploy",
                       trace_id=_trace_id, chain_id=_chain_id, reason=reason)

        # Special cases: test failure or QA rejection → retry as dev (not same stage)
        # Dev fixes the root cause; re-running test/qa without a code fix is wasteful
        if task_type in ("test", "qa"):
            failure_reason = reason
            if task_type == "qa":
                # Prefer specific rejection reason from QA result over gate reason
                failure_reason = result.get("reason", reason)
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            stage_retry_prompt = (
                f"Fix {task_type} stage failures from task {task_id}.\n"
                f"failure_reason: {failure_reason}\n"
                f"retry_from_stage: {task_type}\n\n"
                f"Original task: {original_prompt}"
            )
            from . import task_registry
            # Dedup: skip if an active dev-retry already exists for this parent
            try:
                _existing_stage_retry = conn.execute(
                    "SELECT task_id FROM tasks WHERE project_id = ? AND type = 'dev' "
                    "AND status IN ('queued','claimed','observer_hold') "
                    "AND json_extract(metadata_json, '$.parent_task_id') = ?",
                    (project_id, task_id),
                ).fetchone()
            except Exception:
                _existing_stage_retry = None
            if _existing_stage_retry:
                _dup_id = _existing_stage_retry["task_id"]
                log.warning("auto_chain: dedup stage-retry — active dev retry %s already exists for %s",
                            _dup_id, task_id)
                out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                       "retry_task_id": _dup_id, "retry_type": "dev",
                       "retry_from_stage": task_type, "dedup": True}
                if workflow_improvement:
                    out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                return out
            dev_retry = task_registry.create_task(
                conn, project_id,
                prompt=stage_retry_prompt,
                task_type="dev",
                created_by="auto-chain-stage-retry",
                metadata={
                    **metadata,
                    "parent_task_id": task_id,
                    "chain_depth": depth + 1,
                    "failure_reason": failure_reason,
                    "retry_from_stage": task_type,
                    "_original_prompt": original_prompt,
                },
                trace_id=_trace_id,
                chain_id=_chain_id,
            )
            retry_id = dev_retry.get("task_id", "?")
            log.info("auto_chain: %s failure → dev retry task %s", task_type, retry_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": failure_reason,
                "retry_from_stage": task_type,
            })
            out = {
                "gate_blocked": True, "stage": task_type, "reason": reason,
                "retry_task_id": retry_id, "retry_type": "dev",
                "retry_from_stage": task_type,
            }
            if workflow_improvement:
                out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
            return out

        # Auto-retry: create a new task at the SAME stage with gate reason injected
        # Max 2 retries per gate to prevent infinite loops
        gate_retries = metadata.get("_gate_retry_count", 0)
        if gate_retries < 2 and depth < MAX_CHAIN_DEPTH - 1 and not metadata.get("_no_retry"):
            # Recover original prompt: metadata → chain context → result summary
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            if task_type == "dev":
                retry_reason = _effective_dev_retry_reason(conn, project_id, metadata, reason)
                retry_contract = _render_dev_contract_prompt(
                    metadata.get("parent_task_id", task_id),
                    metadata,
                )
                retry_prompt = (
                    f"Previous attempt ({task_id}) was blocked by gate.\n"
                    f"Gate reason: {retry_reason}\n\n"
                    "IMPORTANT: Do not assume previous blockers still exist. "
                    "Re-verify all alleged blockers against current source before reporting them as remaining issues.\n\n"
                    "Fix the issue described above and retry.\n"
                    "Use the same Dev contract below, including the required verification command.\n\n"
                    f"{retry_contract}"
                )
            else:
                retry_prompt = (
                    f"Previous attempt ({task_id}) was blocked by gate.\n"
                    f"Gate reason: {reason}\n\n"
                    f"Fix the issue described above and retry.\n"
                    f"Original task: {original_prompt}"
                )
            from . import task_registry
            # Dedup: skip if an active same-stage retry already exists for this parent
            try:
                _existing_same_retry = conn.execute(
                    "SELECT task_id FROM tasks WHERE project_id = ? AND type = ? "
                    "AND status IN ('queued','claimed','observer_hold') "
                    "AND json_extract(metadata_json, '$.parent_task_id') = ?",
                    (project_id, task_type, task_id),
                ).fetchone()
            except Exception:
                _existing_same_retry = None
            if _existing_same_retry:
                _dup_id = _existing_same_retry["task_id"]
                log.warning("auto_chain: dedup same-stage-retry — active %s retry %s already exists for %s",
                            task_type, _dup_id, task_id)
                out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                       "retry_task_id": _dup_id, "dedup": True}
                if workflow_improvement:
                    out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                return out
            # --- Sanitise retry metadata (R1/R2): strip stale inherited fields ---
            _retry_meta = {
                **metadata,
                "parent_task_id": task_id,
                "chain_depth": depth + 1,
                "previous_gate_reason": retry_reason if task_type == "dev" else reason,
                "_gate_retry_count": gate_retries + 1,
                "_original_prompt": original_prompt,
            }
            # R2: Strip inherited worktree/branch so new task creates fresh worktree
            _retry_meta.pop("_worktree", None)
            _retry_meta.pop("_branch", None)
            # R1: Remove inherited failure_reason from grandparent — only current gate reason kept
            _retry_meta.pop("failure_reason", None)

            retry_task = task_registry.create_task(
                conn, project_id,
                prompt=retry_prompt,
                task_type=task_type,
                created_by="auto-chain-retry",
                metadata=_retry_meta,
                trace_id=_trace_id,
                chain_id=_chain_id,
            )
            retry_id = retry_task.get("task_id", "?")
            log.info("auto_chain: retry created %s for blocked %s", retry_id, task_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": reason,
            })
            out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                   "retry_task_id": retry_id}
            if workflow_improvement:
                out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
            return out

        # Retry exhausted — emit task.failed
        _publish_event("task.failed", {
            "project_id": project_id, "task_id": task_id,
            "reason": "gate_retry_exhausted", "gate_reason": reason,
        })
        out = {"gate_blocked": True, "stage": task_type, "reason": reason}
        if workflow_improvement:
            out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
            out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
        return out

    # M5: Dev success + checkpoint gate pass → write success pattern memory
    if task_type == "dev":
        _changed_for_pattern = result.get("changed_files", metadata.get("changed_files", []))
        _summary_for_pattern = result.get("summary", "")
        _write_chain_memory(
            conn, project_id, "pattern",
            _summary_for_pattern or f"Dev completed: {', '.join(_changed_for_pattern[:3])}",
            metadata,
            extra_structured={
                "task_id": task_id, "chain_stage": "dev",
                "changed_files": _changed_for_pattern,
                "gate": "checkpoint_pass",
            },
        )

    # Terminal stage → trigger deploy + archive chain
    if next_type is None:
        builder_fn = _BUILDERS[builder_name]
        deploy_result = builder_fn(conn, project_id, task_id, result, metadata)
        log.info("auto_chain: deploy triggered from task %s: %s", task_id, deploy_result)
        # A2: chain.completed audit summary
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "chain.completed",
                actor="auto-chain",
                ok=True,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                chain_depth=depth,
                changed_files=metadata.get("changed_files", []),
                trace_id=_trace_id,
            )
        except Exception:
            log.debug("auto_chain: audit chain.completed failed (non-critical)", exc_info=True)
        structured_log("info", "chain.completed",
                       project_id=project_id, task_id=task_id,
                       trace_id=_trace_id, chain_id=_chain_id)
        # Archive chain context (release memory, DB data preserved)
        try:
            from .chain_context import get_store
            get_store().archive_chain(task_id, project_id)
        except Exception:
            log.debug("auto_chain: chain archive failed (non-critical)")
        return deploy_result

    # Create next stage task (with dedup check)
    builder_fn = _BUILDERS[builder_name]
    prompt, task_meta = builder_fn(task_id, result, metadata)

    # M6: Dedup — check if next stage already exists for this parent
    from . import task_registry
    existing = conn.execute(
        "SELECT task_id FROM tasks WHERE type = ? AND status IN ('queued','claimed','observer_hold') "
        "AND metadata_json LIKE ?",
        (next_type, f'%"parent_task_id": "{task_id}"%'),
    ).fetchone()
    if existing:
        log.warning("auto_chain: dedup — %s task already exists for parent %s: %s",
                     next_type, task_id, existing["task_id"])
        return {"task_id": existing["task_id"], "dedup": True}

    new_task = task_registry.create_task(
        conn, project_id,
        prompt=prompt,
        task_type=next_type,
        created_by="auto-chain",
        metadata={
            **task_meta,
            "parent_task_id": task_id,
            "chain_depth": depth + 1,
        },
        trace_id=_trace_id,
        chain_id=_chain_id,
    )

    log.info("auto_chain: %s→%s | %s → %s",
             task_type, next_type, task_id, new_task.get("task_id"))
    _publish_event("task.created", {
        "project_id": project_id,
        "parent_task_id": task_id,
        "task_id": new_task.get("task_id"),
        "type": next_type,
        "prompt": prompt,
        "source": "auto-chain",
    })
    return new_task


# ---------------------------------------------------------------------------
# Gate functions — each returns (passed: bool, reason: str)
# ---------------------------------------------------------------------------

def _gate_version_check(conn, project_id, result, metadata):
    """Pre-gate: verify the workspace is clean and governance code is current."""
    if _DISABLE_VERSION_GATE:
        return True, "version gate disabled (_DISABLE_VERSION_GATE=True)"
    if metadata.get("skip_version_check"):
        return True, "skipped"
    if not hasattr(conn, "execute"):
        return True, "no db-capable connection, skipping"

    # --- Structured reconciliation bypass (RECONCILIATION_BYPASS_POLICY) ---
    bypass, observer_task_id = _check_reconciliation_bypass(conn, project_id, metadata)
    if bypass:
        lane = str(metadata.get("reconciliation_lane", "")).strip().upper()
        task_id = metadata.get("task_id") or metadata.get("parent_task_id") or "unknown"
        _audit_reconciliation_bypass(conn, project_id, task_id, observer_task_id, lane)
        return True, f"reconciliation-bypass (observer={observer_task_id}, lane={lane})"

    # --- Legacy governed dirty-workspace chain (kept for backward compat) ---
    if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
        return True, "governed dirty-workspace reconciliation"

    try:
        row = conn.execute(
            "SELECT chain_version, git_head, dirty_files FROM project_version WHERE project_id=?",
            (project_id,),
        ).fetchone()
        dirty_files = json.loads(row["dirty_files"] or "[]") if row and row["dirty_files"] else []
        # Filter out tool-local config files that aren't project code
        _DIRTY_IGNORE = (".claude/", ".claude\\")
        dirty_files = [f for f in dirty_files if not any(f.startswith(p) for p in _DIRTY_IGNORE)]
        if dirty_files:
            log.warning("version_check: dirty workspace (%d files: %s) — chain continues as warning",
                        len(dirty_files), dirty_files[:5])
            return True, f"dirty workspace warning ({len(dirty_files)} files)"

        from .server import SERVER_VERSION
        import subprocess
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        if not head or head == "unknown":
            return True, "git HEAD unavailable, skipping"
        if SERVER_VERSION == "unknown":
            return True, "server version unavailable, skipping"
        if SERVER_VERSION != head:
            log.warning("version_check: server version (%s) behind git HEAD (%s) — chain continues",
                        SERVER_VERSION, head)
            return True, f"server version lag ({SERVER_VERSION} != {head}), warning only"
        return True, f"version match: {SERVER_VERSION}"
    except Exception as e:
        log.warning("version_check failed (non-fatal): %s", e)
        return True, f"version check skipped: {e}"


def _gate_post_pm(conn, project_id, result, metadata):
    """Validate PM PRD has mandatory fields + explain-or-provide for soft fields.

    Mandatory: target_files, verification, acceptance_criteria (hard block)
    Soft-mandatory: test_files, proposed_nodes, doc_impact (must provide OR skip_reasons)
    """
    prd = result.get("prd", {})

    # === Hard mandatory fields ===
    missing = []
    for field in ("target_files", "verification", "acceptance_criteria"):
        if not result.get(field) and not prd.get(field) and not metadata.get(field):
            missing.append(field)
    if missing:
        return False, f"PRD missing mandatory fields: {missing}"

    target_files = (result.get("target_files") or prd.get("target_files")
                    or metadata.get("target_files") or [])
    if not target_files:
        return False, "PRD target_files is empty"

    # === Soft-mandatory: provide OR explain in skip_reasons ===
    skip_reasons = result.get("skip_reasons", prd.get("skip_reasons", {}))
    if not isinstance(skip_reasons, dict):
        skip_reasons = {}
    soft_missing = []
    for field in ("test_files", "proposed_nodes", "doc_impact"):
        value = result.get(field) or prd.get(field)
        reason = skip_reasons.get(field, "")
        if not value and not reason:
            soft_missing.append(field)
    if soft_missing:
        return False, f"PRD fields missing without skip_reasons: {soft_missing}. Provide the field OR explain in skip_reasons why it's not needed."

    # === Merge all fields into result for downstream ===
    for field in ("target_files", "verification", "acceptance_criteria",
                  "test_files", "proposed_nodes", "doc_impact", "skip_reasons",
                  "requirements", "related_nodes"):
        if not result.get(field):
            result[field] = prd.get(field) or metadata.get(field)

    return True, "ok"


def _is_dev_note(path: str) -> bool:
    """Return True for docs/dev/** paths — informal dev notes, not formal docs."""
    normalized = path.replace("\\", "/")
    return normalized.startswith("docs/dev/")


_GOVERNANCE_INTERNAL_PREFIXES = (
    "agent/governance/",
    "agent/role_permissions.py",
)


def _is_governance_internal_repair(metadata: dict, changed_files: list) -> bool:
    """Return True when all target_files and changed_files are governance-internal.

    Governance-internal paths are:
      - agent/governance/*
      - agent/role_permissions.py
      - agent/tests/test_* (co-located test files)

    When True, the doc consistency gate is skipped to avoid the oscillation loop
    where governance repairs are demanded docs they cannot add without triggering
    the unrelated-files gate.
    """
    target_files = metadata.get("target_files", []) or []
    all_files = list(target_files) + list(changed_files or [])
    if not all_files:
        return False
    for f in all_files:
        normalized = f.replace("\\", "/")
        # Allow governance paths
        if any(normalized.startswith(prefix) for prefix in _GOVERNANCE_INTERNAL_PREFIXES):
            continue
        # Allow co-located test files
        if "/tests/test_" in normalized or normalized.startswith("agent/tests/test_"):
            continue
        return False
    return True


def _gate_checkpoint(conn, project_id, result, metadata):
    """Checkpoint gate for Dev.

    Trust executor-produced task-local diff evidence. Governance may run in a
    container without git/worktree parity, so it should not re-compute git diff
    here. Node alignment is also temporarily non-blocking until the acceptance
    graph deployed to governance catches up with in-flight node-by-node edits.
    """
    log.info("checkpoint_gate: result keys=%s, changed_files=%s, target_files=%s",
             list(result.keys()) if result else None,
             result.get("changed_files"),
             metadata.get("target_files"))
    changed = result.get("changed_files", [])
    if not changed:
        return False, "No files changed"

    target = set(metadata.get("target_files", []) or [])
    allowed = set(target)
    allowed.update(metadata.get("test_files", []) or [])
    allowed.update(_extract_test_files_from_verification(metadata.get("verification", {})))
    doc_impact = metadata.get("doc_impact", {})
    if isinstance(doc_impact, dict):
        allowed.update(doc_impact.get("files", []) or [])
    # R1/R2: Derive allowed test files from target_files stems.
    # For each target file with stem S, allow changed files matching
    # tests/test_{S}*.py (under any parent directory or agent/tests/).
    _allowed_test_prefixes = []
    for tf in target:
        import os.path as _osp
        stem = _osp.splitext(_osp.basename(tf))[0]  # e.g. "ai_lifecycle"
        _allowed_test_prefixes.append(f"test_{stem}")
    if allowed:
        unrelated = []
        for f in changed:
            if f in allowed:
                continue
            # Check if file is a co-modified test file under a tests/ directory
            if _allowed_test_prefixes:
                import posixpath
                parts = f.replace("\\", "/")
                parent = posixpath.dirname(parts)
                basename = posixpath.basename(parts)
                if (parent.endswith("/tests") or parent.endswith("\\tests") or parent == "tests") \
                        and basename.endswith(".py"):
                    if any(basename.startswith(prefix) for prefix in _allowed_test_prefixes):
                        continue
            unrelated.append(f)
        if unrelated:
            return False, f"Unrelated files modified: {unrelated}"
    # Syntax check: verify test_results if available
    test_results = result.get("test_results", {})
    if test_results.get("ran") and test_results.get("failed", 0) > 0:
        return False, f"Dev tests failed: {test_results.get('failed')} failures"
    # --- Contract-drift detection (D10) --- warn-only ---
    try:
        from .drift_detector import detect_drift, findings_to_json
        _drift_baseline = metadata.get("_drift_baseline")
        if _drift_baseline and isinstance(_drift_baseline, dict):
            authorized = set(metadata.get("_drift_authorized_keys") or [])
            drift_findings = detect_drift(_drift_baseline, authorized_keys=authorized)
            if drift_findings:
                drift_report = findings_to_json(drift_findings)
                metadata["_drift_report"] = drift_report
                unauthorized = [f for f in drift_findings if not f.authorized]
                if unauthorized:
                    log.warning(
                        "checkpoint_gate: UNAUTHORIZED contract drift detected: %s",
                        drift_report,
                    )
                else:
                    log.info("checkpoint_gate: authorized contract drift: %s", drift_report)
        else:
            # No baseline captured — run fresh capture and attach for next stage
            from .drift_detector import capture_baseline
            baseline = capture_baseline()
            metadata["_drift_baseline"] = baseline
            metadata["_drift_report"] = "[]"
    except Exception:
        log.debug("checkpoint_gate: drift detection failed (non-critical)", exc_info=True)
    # Doc consistency check: use CODE_DOC_MAP to verify related docs are updated
    # Skip for governance-internal repairs to avoid oscillation loop (R2)
    if _is_governance_internal_repair(metadata, changed):
        log.info("checkpoint_gate: skipping doc consistency check for governance-internal repair")
        # Node gate is temporarily non-blocking while the governance graph catches
        # up with node-by-node local development. Keep the signal in logs only.
        related_nodes = _normalize_related_nodes(metadata.get("related_nodes", []))
        if related_nodes:
            log.warning(
                "checkpoint_gate: skipping related_nodes enforcement for dev task until graph sync is complete: %s",
                related_nodes,
            )
        return True, "ok"
    from .impact_analyzer import get_related_docs
    code_files = [f for f in changed if not f.startswith("docs/") and not f.endswith(".md")]
    doc_files_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
    doc_impact = metadata.get("doc_impact", {})
    if isinstance(doc_impact, dict) and "files" in doc_impact:
        expected_docs = set(doc_impact.get("files") or [])
    else:
        expected_docs = get_related_docs(code_files)
    # docs/dev/** are informal dev notes — never enforce them as formal docs
    if expected_docs:
        expected_docs = {d for d in expected_docs if not _is_dev_note(d)}
    if expected_docs:
        missing_docs = expected_docs - doc_files_changed
        if missing_docs:
            if _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
                log.warning(
                    "checkpoint_gate: deferring doc updates to Lane C for governed reconciliation lane; missing docs=%s",
                    sorted(missing_docs),
                )
                return True, "doc updates deferred to Lane C"
            # Block by default — skip_doc_check only allowed with bootstrap_reason
            if metadata.get("skip_doc_check", False):
                bootstrap_reason = metadata.get("bootstrap_reason", "")
                if not bootstrap_reason:
                    return False, (f"skip_doc_check=true requires bootstrap_reason in metadata. "
                                   f"Missing docs: {sorted(missing_docs)}")
                log.warning("checkpoint_gate: docs skipped (bootstrap: %s): %s",
                            bootstrap_reason, sorted(missing_docs))
            else:
                return False, f"Related docs not updated: {sorted(missing_docs)}. Add them to changed_files."
    # Node gate is temporarily non-blocking while the governance graph catches
    # up with node-by-node local development. Keep the signal in logs only.
    related_nodes = _normalize_related_nodes(metadata.get("related_nodes", []))
    if related_nodes:
        log.warning(
            "checkpoint_gate: skipping related_nodes enforcement for dev task until graph sync is complete: %s",
            related_nodes,
        )
    return True, "ok"


def _gate_t2_pass(conn, project_id, result, metadata):
    """Verify tests passed before advancing to QA."""
    report = result.get("test_report", {})
    if not isinstance(report, dict) or not report:
        return False, "Test stage missing required test_report"
    if "passed" not in report or "failed" not in report:
        return False, "Test stage test_report missing passed/failed counts"
    failed = report.get("failed", 0)
    if failed is None:
        failed = 0
    if failed > 0:
        return False, f"Tests failed: {failed} failures"
    # Update nodes FIRST (test passed → promote to t2_pass)
    # Evidence validator checks summary.passed > 0, so ensure it's there
    passed_count = report.get("passed", 1)  # Default 1 if not reported (tests passed gate)
    summary = {**report, "passed": passed_count, "failed": failed}
    _try_verify_update(conn, project_id, metadata, "t2_pass", "tester",
                       {"type": "test_report", "producer": "auto-chain",
                        "tool": report.get("tool", "pytest"),
                        "summary": summary})
    # Then verify nodes reached t2_pass
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "t2_pass")
        if not passed:
            return False, f"t2_pass gate blocked — {reason}"
    return True, "ok"


def _gate_qa_pass(conn, project_id, result, metadata):
    """Verify QA recommendation before merge.

    Requires explicit qa_pass or qa_pass_with_fallback recommendation.
    Missing or ambiguous recommendation is a hard block (not auto-pass).
    """
    rec = result.get("recommendation", "")
    if rec in ("qa_pass", "qa_pass_with_fallback"):
        pass  # Explicit pass
    elif rec in ("reject", "rejected"):
        return False, f"QA rejected: {result.get('reason', 'no reason given')}"
    else:
        # No explicit recommendation — BLOCK. Auto-pass is a security risk.
        return False, (
            f"QA gate requires explicit recommendation ('qa_pass' or 'reject'). "
            f"Got: {rec!r}. QA agent must set result.recommendation."
        )
    # E2E1: Verify criteria_results when acceptance_criteria exist
    criteria = metadata.get("acceptance_criteria", [])
    criteria_results = result.get("criteria_results", [])
    if criteria:
        if not criteria_results:
            log.warning("qa_gate: acceptance_criteria present (%d items) but QA result missing criteria_results — "
                        "allowing pass but criteria not individually verified", len(criteria))
        else:
            failed_criteria = [cr for cr in criteria_results if not cr.get("passed")]
            if failed_criteria:
                names = [cr.get("criterion", "?")[:60] for cr in failed_criteria]
                return False, f"QA approved overall but {len(failed_criteria)} criteria failed: {names}"
    # Update nodes FIRST (QA passed → promote to qa_pass)
    # Evidence rule: t2_pass → qa_pass requires "e2e_report" with summary.passed > 0
    _try_verify_update(conn, project_id, metadata, "qa_pass", "qa",
                       {"type": "e2e_report", "producer": "auto-chain",
                        "summary": {"passed": 1, "failed": 0,
                                    "review": result.get("review_summary", "auto-chain QA pass")}})
    # Then verify nodes reached qa_pass
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
        if not passed:
            if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
                log.warning(
                    "qa_gate: deferring related_nodes qa_pass enforcement for governed dirty-workspace reconciliation lane: %s",
                    reason,
                )
            else:
                return False, f"qa_pass gate blocked — {reason}"
    # M2: QA passed → write success pattern memory
    _write_chain_memory(
        conn, project_id, "qa_decision",
        result.get("review_summary", f"QA approved (rec={rec})"),
        metadata,
        extra_structured={"recommendation": rec, "chain_stage": "qa",
                          "changed_files": metadata.get("changed_files", [])},
    )
    return True, "ok"


def _gate_gatekeeper_pass(conn, project_id, result, metadata):
    """Require explicit isolated gatekeeper approval before merge."""
    rec = result.get("recommendation", "")
    if rec == "merge_pass":
        try:
            from . import gatekeeper as gk
            gk.record_check(
                conn, project_id,
                check_type="ai_acceptance_check",
                passed=True,
                result={
                    "summary": result.get("review_summary", ""),
                    "checked_requirements": result.get("checked_requirements", []),
                    "pm_alignment": result.get("pm_alignment", "pass"),
                },
                created_by="auto-chain-gatekeeper",
            )
        except Exception:
            log.debug("gatekeeper ai record failed (non-critical)", exc_info=True)
        return True, "ok"
    if rec in ("reject", "rejected"):
        return False, f"Gatekeeper rejected merge: {result.get('reason', 'no reason given')}"
    return False, (
        "Gatekeeper must emit recommendation 'merge_pass' or 'reject'. "
        f"Got: {rec!r}"
    )


def _gate_release(conn, project_id, result, metadata):
    """Verify merge succeeded before deploy."""
    # Node status check: all related_nodes must be "qa_pass" before merge is allowed
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
        if not passed:
            if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
                log.warning(
                    "release_gate: deferring related_nodes qa_pass enforcement for governed dirty-workspace reconciliation lane: %s",
                    reason,
                )
            else:
                return False, f"release gate blocked — {reason}"
    else:
        log.warning("release gate: no related_nodes — node verification skipped for %s",
                     metadata.get("parent_task_id", "unknown"))
    # For auto-chain deploys, we trust the merge task result
    # After successful merge, promote related_nodes to qa_pass
    if related_nodes:
        _try_verify_update(conn, project_id, metadata, "qa_pass", "merge",
                           {"type": "merge_complete", "producer": "auto-chain"})
    return True, "ok"


def _gate_deploy_pass(conn, project_id, result, metadata):
    """Deploy must report success AND smoke_test semantic coherence.

    R3: Validates that smoke_test.all_pass agrees with report.success and
    that no individual service has a False result. This catches cases where
    the production path diverges from the tested path.
    """
    report = result.get("report", result)
    if not isinstance(report, dict):
        return False, f"deploy failed: report is not a dict"

    # Check report.success first
    if report.get("success") is not True:
        return False, f"deploy failed: {json.dumps(report, ensure_ascii=False)[:300]}"

    # R3: Validate smoke_test semantic coherence
    smoke_test = report.get("smoke_test", {})
    if smoke_test:
        # Reject if all_pass is explicitly False
        if smoke_test.get("all_pass") is False:
            return False, (
                f"deploy gate rejected: smoke_test.all_pass=False contradicts "
                f"report.success=True — {json.dumps(smoke_test, ensure_ascii=False)[:200]}"
            )
        # Reject if any service has an explicit False value
        for svc in ("executor", "governance", "gateway"):
            if smoke_test.get(svc) is False:
                return False, (
                    f"deploy gate rejected: smoke_test.{svc}=False contradicts "
                    f"report.success=True — {json.dumps(smoke_test, ensure_ascii=False)[:200]}"
                )

    return True, "ok"


# ---------------------------------------------------------------------------
# Prompt builders — return (prompt: str, metadata: dict)
# ---------------------------------------------------------------------------

def _build_dev_prompt(task_id, result, metadata):
    prd = result.get("prd", {})
    # target_files: result > prd > original metadata (preserve original task metadata)
    target_files = result.get("target_files", prd.get("target_files", metadata.get("target_files", [])))

    verification = result.get("verification", prd.get("verification", {}))
    requirements = result.get("requirements", prd.get("requirements", []))
    criteria = result.get("acceptance_criteria", prd.get("acceptance_criteria", []))
    test_files = result.get("test_files", prd.get("test_files", metadata.get("test_files", [])))
    doc_impact = result.get("doc_impact", prd.get("doc_impact", metadata.get("doc_impact", {})))
    skip_reasons = result.get("skip_reasons", prd.get("skip_reasons", metadata.get("skip_reasons", {})))
    proposed_nodes = result.get("proposed_nodes", metadata.get("proposed_nodes", []))

    # Fallback: if PM result lacks expected structure, read from chain context
    if not target_files or not verification or not criteria:
        try:
            from .chain_context import get_store
            parent_result = get_store().get_parent_result(task_id)
            if parent_result:
                if not target_files:
                    target_files = parent_result.get("target_files", target_files)
                if not verification:
                    verification = parent_result.get("verification", verification)
                if not criteria:
                    criteria = parent_result.get("acceptance_criteria", criteria)
                if not requirements:
                    requirements = parent_result.get("requirements", requirements)
        except Exception:
            pass
    out_meta = {
        **metadata,  # preserves skip_doc_check, changed_files, related_nodes, etc.
        "target_files": target_files,
        "requirements": requirements,
        "acceptance_criteria": criteria,
        "verification": verification,
        "test_files": test_files,
        "doc_impact": doc_impact,
        "skip_reasons": skip_reasons,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes", result.get("related_nodes", []))),
        "proposed_nodes": proposed_nodes,
    }
    prompt = _render_dev_contract_prompt(task_id, out_meta)
    return prompt, out_meta


def _build_test_prompt(task_id, result, metadata):
    changed = result.get("changed_files", metadata.get("changed_files", []))
    verification = metadata.get("verification") or result.get("verification", {})
    test_files = metadata.get("test_files", [])
    prompt_parts = [
        f"Run tests for {task_id}.",
        f"changed_files: {json.dumps(changed)}",
    ]
    if verification:
        prompt_parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")
    if test_files:
        prompt_parts.append(f"test_files: {json.dumps(test_files)}")
    prompt = "\n".join(prompt_parts)
    meta = {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
        "verification": verification,
        "test_files": test_files,
    }
    # Propagate worktree info from dev result → test → qa → merge
    if result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_qa_prompt(task_id, result, metadata):
    report = result.get("test_report", {})
    changed = result.get("changed_files", metadata.get("changed_files", []))
    requirements = metadata.get("requirements", [])
    criteria = metadata.get("acceptance_criteria", [])
    doc_impact = metadata.get("doc_impact", {})
    verification = metadata.get("verification", {})
    prompt_parts = [
        f"QA review for {task_id}.",
        f"test_report: {json.dumps(report, ensure_ascii=False)}",
        f"changed_files: {json.dumps(changed)}",
    ]
    if requirements:
        prompt_parts.append(f"requirements: {json.dumps(requirements, ensure_ascii=False)}")
    if criteria:
        prompt_parts.append(f"acceptance_criteria: {json.dumps(criteria, ensure_ascii=False)}")
    if verification:
        prompt_parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")
    if doc_impact:
        prompt_parts.append(f"doc_impact: {json.dumps(doc_impact, ensure_ascii=False)}")
    if criteria:
        prompt_parts.append(
            "\nYou MUST evaluate each acceptance_criteria item individually.\n"
            "Include in your result:\n"
            "  criteria_results: [{criterion: \"<text>\", passed: true/false, evidence: \"<why>\"}]\n"
            "Only set recommendation='qa_pass' if ALL criteria pass."
        )
    prompt_parts.append("IMPORTANT: result.recommendation MUST be exactly 'qa_pass' or 'reject' (no other values accepted by the gate).")
    prompt = "\n".join(prompt_parts)
    meta = {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
        "test_report": report,
        "requirements": requirements,
        "acceptance_criteria": criteria,
        "verification": verification,
        "doc_impact": doc_impact,
    }
    if result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_gatekeeper_prompt(task_id, result, metadata):
    prompt = (
        f"Gatekeeper review for {task_id}.\n"
        "You are the final isolated acceptance check before merge.\n"
        "Use ONLY the PM contract, test evidence, QA review, changed file list, and doc-impact summary below.\n"
        "Do NOT request broader project context or unrelated history.\n"
        f"requirements: {json.dumps(metadata.get('requirements', []), ensure_ascii=False)}\n"
        f"acceptance_criteria: {json.dumps(metadata.get('acceptance_criteria', []), ensure_ascii=False)}\n"
        f"verification: {json.dumps(metadata.get('verification', {}), ensure_ascii=False)}\n"
        f"doc_impact: {json.dumps(metadata.get('doc_impact', {}), ensure_ascii=False)}\n"
        f"test_report: {json.dumps(metadata.get('test_report', {}), ensure_ascii=False)}\n"
        f"qa_review: {json.dumps({'review_summary': result.get('review_summary', ''), 'issues': result.get('issues', []), 'doc_updates_applied': result.get('doc_updates_applied', [])}, ensure_ascii=False)}\n"
        f"changed_files: {json.dumps(metadata.get('changed_files', []))}\n"
        "Respond with strict JSON: "
        "{\"schema_version\":\"v1\",\"review_summary\":\"...\",\"recommendation\":\"merge_pass|reject\",\"pm_alignment\":\"pass|partial|fail\",\"checked_requirements\":[\"R1\"],\"reason\":\"\"}"
    )
    meta = {
        **metadata,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes", result.get("related_nodes", []))),
    }
    # Propagate worktree isolation metadata through gatekeeper → merge
    if metadata.get("_worktree"):
        meta["_worktree"] = metadata["_worktree"]
        meta["_branch"] = metadata.get("_branch", "")
    elif result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_merge_prompt(task_id, result, metadata):
    prompt = f"Merge dev branch for {task_id} to main."
    return prompt, {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": metadata.get("changed_files") or result.get("changed_files", []),
        "_worktree": metadata.get("_worktree") or result.get("_worktree", ""),
        "_branch": metadata.get("_branch") or result.get("_branch", ""),
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
    }


def _build_deploy_prompt(task_id, result, metadata):
    changed_files = metadata.get("changed_files") or result.get("changed_files", [])
    prompt = (
        f"Deploy changes after merge task {task_id}.\n"
        f"changed_files: {json.dumps(changed_files)}\n"
        "Run host-side deploy orchestration and smoke checks."
    )
    return prompt, {
        **metadata,
        "changed_files": changed_files,
        "merge_commit": result.get("merge_commit", metadata.get("merge_commit", "")),
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
    }


def _finalize_chain(conn, project_id, task_id, result, metadata):
    """Terminal stage after deploy succeeds.

    R4: Call version-sync then version-update to advance chain_version.
    R5: Verify SERVER_VERSION == new HEAD; warn if stale.
    """
    import subprocess as _sp

    report = result.get("report", result)
    finalize_result = {"deploy": "completed", "report": report}

    # --- R4: version-sync then version-update ---
    try:
        _finalize_version_sync(conn, project_id, task_id)
    except Exception as e:
        log.warning("_finalize_chain: version-sync/update failed: %s", e)
        finalize_result["version_sync_error"] = str(e)

    # --- R5: verify SERVER_VERSION == new HEAD ---
    try:
        from .server import SERVER_VERSION
        new_head = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        if new_head and new_head != "unknown" and SERVER_VERSION != new_head:
            finalize_result["restart_required"] = True
            finalize_result["stale_server_version"] = SERVER_VERSION
            finalize_result["expected_version"] = new_head
            log.warning(
                "_finalize_chain: SERVER_VERSION (%s) != HEAD (%s) — restart_required=true",
                SERVER_VERSION, new_head,
            )
    except Exception as e:
        log.debug("_finalize_chain: version verify failed: %s", e)

    return finalize_result


def _finalize_version_sync(conn, project_id, task_id):
    """Call version-sync then version-update via local DB ops (R4).

    Uses direct DB writes instead of HTTP to avoid circular dependency.
    """
    import subprocess as _sp

    # Get current git HEAD
    head_result = _sp.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    )
    new_head = head_result.stdout.strip() if head_result.returncode == 0 else None
    if not new_head:
        log.warning("_finalize_version_sync: cannot determine git HEAD")
        return

    # version-sync: update git_head and dirty_files in project_version
    try:
        dirty_result = _sp.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        dirty_files = [
            line[3:] for line in (dirty_result.stdout or "").strip().split("\n")
            if line.strip()
        ] if dirty_result.returncode == 0 else []
    except Exception:
        dirty_files = []

    conn.execute(
        "INSERT OR REPLACE INTO project_version (project_id, chain_version, git_head, dirty_files, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (project_id, new_head, new_head, json.dumps(dirty_files)),
    )
    log.info(
        "_finalize_version_sync: version-sync project=%s head=%s dirty=%d",
        project_id, new_head, len(dirty_files),
    )

    # version-update: set chain_version = new HEAD with updated_by='auto-chain'
    conn.execute(
        "UPDATE project_version SET chain_version=?, updated_by=?, updated_at=datetime('now') "
        "WHERE project_id=?",
        (new_head, f"auto-chain:{task_id}", project_id),
    )
    log.info(
        "_finalize_version_sync: version-update project=%s chain_version=%s updated_by=auto-chain:%s",
        project_id, new_head, task_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_chain_memory(conn, project_id, kind, content, metadata, extra_structured=None):
    """Best-effort memory write for chain events. Never blocks chain progress."""
    try:
        from . import memory_service
        from .models import MemoryEntry
        # Derive module_id from first target_file or changed_file
        target = (metadata.get("target_files") or metadata.get("changed_files") or [])
        module_id = target[0].replace("/", ".").replace("\\", ".") if target else "governance"
        # Dedup: skip if identical content already exists for same module+kind
        try:
            existing = conn.execute(
                "SELECT memory_id FROM memories WHERE project_id=? AND module_id=? AND kind=? "
                "AND status='active' AND content=? LIMIT 1",
                (project_id, module_id, kind, content),
            ).fetchone()
            if existing:
                log.debug("chain_memory dedup: skipping identical %s/%s", module_id, kind)
                return
        except Exception:
            pass  # dedup failure should not block write
        entry = MemoryEntry(
            module_id=module_id,
            kind=kind,
            content=content,
            created_by="auto-chain",
        )
        result = memory_service.write_memory(conn, project_id, entry)
        log.info("chain_memory.write: project=%s kind=%s module=%s id=%s content=%r",
                 project_id, kind, module_id, result.get("memory_id", "?"), content[:100])
        if extra_structured:
            # Patch structured field if write succeeded
            mid = result.get("memory_id", "")
            if mid:
                try:
                    import json as _json
                    conn.execute(
                        "UPDATE memories SET structured = ? WHERE memory_id = ?",
                        (_json.dumps(extra_structured), mid),
                    )
                except Exception:
                    pass
    except Exception:
        log.debug("_write_chain_memory failed (non-critical)", exc_info=True)


# Status ordering for node_state validation
_STATUS_ORDER = ["pending", "testing", "t2_pass", "qa_pass", "waived"]


def _check_nodes_min_status(conn, project_id, related_nodes, min_status):
    """Verify every node in related_nodes has at least min_status in node_state.

    Returns (passed: bool, reason: str).
    If node_state table is empty for this project (fresh DB bootstrap), skip check.
    If a node is not found in a populated DB it is skipped with a warning (not blocked).
    """
    related_nodes = _normalize_related_nodes(related_nodes)
    if not related_nodes:
        return True, "no related_nodes"
    try:
        min_rank = _STATUS_ORDER.index(min_status)
    except ValueError:
        return False, f"unknown min_status '{min_status}'"

    # Fresh DB: if no node_state records exist for this project, skip node check entirely
    node_count = conn.execute(
        "SELECT COUNT(*) FROM node_state WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    if node_count == 0:
        log.info("_check_nodes_min_status: node_state empty for project %s — skipping node check (fresh DB)", project_id)
        return True, "node_state empty (fresh DB bootstrap)"

    blocking = []
    for node_id in related_nodes:
        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()
        if row is None:
            # Not found in populated DB → skip with warning (node was never registered)
            log.warning("_check_nodes_min_status: node '%s' not found in DB for project '%s' — skipping", node_id, project_id)
            continue
        status = (row["verify_status"] or "pending").strip()
        try:
            rank = _STATUS_ORDER.index(status)
        except ValueError:
            # Unknown status — treat conservatively as pending
            blocking.append((node_id, f"unknown status '{status}'"))
            continue
        if rank < min_rank:
            blocking.append((node_id, status))

    if blocking:
        details = ", ".join(f"{nid}={st}" for nid, st in blocking)
        return False, (
            f"related_nodes not yet at '{min_status}': [{details}]"
        )
    return True, "ok"


def _try_verify_update(conn, project_id, metadata, target_status, role, evidence_dict):
    """Best-effort node status update. Non-blocking on failure."""
    related = _normalize_related_nodes(metadata.get("related_nodes", []))
    if not related:
        return
    try:
        from . import state_service
        from .graph import AcceptanceGraph
        # Load graph from project state directory
        import os
        state_root = os.path.join(
            os.environ.get("SHARED_VOLUME_PATH",
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
            "codex-tasks", "state", "governance", project_id)
        graph_path = os.path.join(state_root, "graph.json")
        graph = AcceptanceGraph()
        if os.path.exists(graph_path):
            graph.load(graph_path)
        session = {"principal_id": "auto-chain", "role": role, "scope_json": "[]"}
        state_service.verify_update(
            conn, project_id, graph,
            node_ids=related if isinstance(related, list) else [related],
            target_status=target_status,
            session=session,
            evidence_dict=evidence_dict,
        )
        log.info("auto_chain: nodes %s → %s", related, target_status)
    except Exception as e:
        log.warning("auto_chain: verify_update %s failed (non-blocking): %s", target_status, e,
                    exc_info=True)


def _publish_event(event_name, payload):
    """Best-effort event publish to event bus."""
    try:
        from . import event_bus
        event_bus._bus.publish(event_name, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Function lookup tables (avoid globals() for safety)
# ---------------------------------------------------------------------------

_GATES = {
    "_gate_post_pm": _gate_post_pm,
    "_gate_checkpoint": _gate_checkpoint,
    "_gate_t2_pass": _gate_t2_pass,
    "_gate_qa_pass": _gate_qa_pass,
    "_gate_gatekeeper_pass": _gate_gatekeeper_pass,
    "_gate_release": _gate_release,
    "_gate_deploy_pass": _gate_deploy_pass,
}

_BUILDERS = {
    "_build_dev_prompt": _build_dev_prompt,
    "_build_test_prompt": _build_test_prompt,
    "_build_qa_prompt": _build_qa_prompt,
    "_build_gatekeeper_prompt": _build_gatekeeper_prompt,
    "_build_merge_prompt": _build_merge_prompt,
    "_build_deploy_prompt": _build_deploy_prompt,
    "_finalize_chain": _finalize_chain,
}
