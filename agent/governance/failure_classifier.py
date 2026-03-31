"""Failure classification for workflow self-repair.

Keeps classification intentionally simple and heuristic-driven so the workflow
can distinguish business-task failures from governance/workflow failures.
"""

from __future__ import annotations


def classify_gate_failure(stage: str, reason: str, metadata: dict | None = None, result: dict | None = None) -> dict:
    metadata = metadata or {}
    result = result or {}
    text = f"{stage} {reason}".lower()

    classification = {
        "stage": stage,
        "reason": reason,
        "failure_class": "task_defect",
        "workflow_improvement": False,
        "observer_attention": False,
        "suggested_action": "retry_or_manual_review",
        "issue_summary": f"{stage} gate blocked: {reason}",
    }

    if "dirty workspace detected" in text:
        classification.update({
            "failure_class": "environment_defect",
            "workflow_improvement": False,
            "observer_attention": True,
            "suggested_action": "reconcile_dirty_workspace",
            "issue_summary": "Version gate blocked due to out-of-band dirty workspace state.",
        })
        return classification

    if any(token in text for token in (
        "node_state empty", "nodenotfound", "acceptance graph", "coverage-check", "uncovered",
        "graph", "related_nodes", "node gate",
    )):
        classification.update({
            "failure_class": "graph_defect",
            "workflow_improvement": True,
            "observer_attention": True,
            "suggested_action": "repair_graph_mapping_or_runtime_state",
            "issue_summary": f"Workflow graph/governance mismatch at {stage}: {reason}",
        })
        return classification

    if any(token in text for token in (
        "schema_version", "missing target_files", "missing acceptance_criteria",
        "missing verification", "missing test_report", "strict json", "contract",
        "prompt conflict", "contradictory prompt", "conflicting instructions",
        "impossible instruction",
        "no valid json", "validation error",
    )):
        classification.update({
            "failure_class": "contract_defect",
            "workflow_improvement": True,
            "observer_attention": False,
            "suggested_action": "repair_stage_contract",
            "issue_summary": f"Workflow contract defect at {stage}: {reason}",
        })
        return classification

    if any(token in text for token in (
        "reached max turns", "tool unavailable", "provider", "model", "timeout",
        "session timed out", "hung",
    )):
        classification.update({
            "failure_class": "provider_tool_defect",
            "workflow_improvement": True,
            "observer_attention": False,
            "suggested_action": "repair_provider_or_runtime_limits",
            "issue_summary": f"Workflow provider/tooling defect at {stage}: {reason}",
        })
        return classification

    if any(token in text for token in (
        "merge conflict", "tests failed", "qa rejected", "reject",
    )):
        classification.update({
            "failure_class": "task_defect",
            "workflow_improvement": False,
            "observer_attention": False,
            "suggested_action": "fix_business_task_and_retry",
            "issue_summary": f"Task implementation/test defect at {stage}: {reason}",
        })
        return classification

    if stage == "version_check":
        classification.update({
            "failure_class": "environment_defect",
            "workflow_improvement": False,
            "observer_attention": True,
            "suggested_action": "inspect_environment_blocker",
        })
        return classification

    return classification


def build_workflow_improvement_prompt(task_id: str, stage: str, classification: dict, metadata: dict | None = None) -> str:
    metadata = metadata or {}
    parts = [
        "Investigate and repair a workflow/governance defect exposed by a live task chain.",
        f"source_task_id: {task_id}",
        f"failing_stage: {stage}",
        f"failure_class: {classification.get('failure_class', '')}",
        f"suggested_action: {classification.get('suggested_action', '')}",
        f"issue_summary: {classification.get('issue_summary', '')}",
        f"gate_reason: {classification.get('reason', '')}",
    ]
    if metadata.get("related_nodes"):
        parts.append(f"related_nodes: {metadata.get('related_nodes')}")
    if metadata.get("target_files"):
        parts.append(f"target_files: {metadata.get('target_files')}")
    parts.append(
        "Treat this as a workflow-improvement task. First identify whether the defect is in governance contracts, "
        "graph/runtime state, gate semantics, or provider/tooling configuration. Then produce the minimal repair plan."
    )
    return "\n".join(parts)
