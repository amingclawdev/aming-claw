"""Compatibility entrypoint for Phase Z v2 symbol cluster enrichment."""
from __future__ import annotations

from typing import Any, Mapping

from agent.governance.ai_cluster_processor import (  # noqa: F401
    ClusterReport,
    process_cluster_with_ai,
)

_AST_SCAN_STEP_NAMES = {
    "production_module_parsing",
    "ast_candidate_scanning",
    "ast_parse",
    "ast_parsing",
}
_DFS_SCAN_STEP_NAMES = {
    "dfs_coloring",
    "dfs_candidate_scanning",
    "dfs_scan",
}
_PARALLEL_TRUE_KEYS = {
    "parallel",
    "parallelized",
    "used_parallel",
    "parallel_scanning",
    "parallel_candidate_scanning",
}
_WORKER_COUNT_KEYS = {
    "worker_count",
    "workers",
    "max_workers",
    "process_count",
    "thread_count",
}


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    if isinstance(value, int):
        return value != 0
    return None


def _iter_phase_steps(phase_trace: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(phase_trace, Mapping):
        return []
    steps = phase_trace.get("steps")
    if not isinstance(steps, list):
        return []
    return [dict(step) for step in steps if isinstance(step, Mapping)]


def _find_step(steps: list[dict[str, Any]], names: set[str]) -> dict[str, Any]:
    for step in steps:
        if str(step.get("name") or "") in names:
            return step
    for step in steps:
        name = str(step.get("name") or "").lower()
        if any(candidate in name for candidate in names):
            return step
    return {}


def _parallel_evidence(metrics: Mapping[str, Any]) -> dict[str, Any]:
    worker_count = 0
    worker_key = ""
    for key in _WORKER_COUNT_KEYS:
        count = _coerce_int(metrics.get(key), 0)
        if count:
            worker_count = count
            worker_key = key
            break

    for key in _PARALLEL_TRUE_KEYS:
        if key in metrics:
            value = _boolish(metrics.get(key))
            if value is not None:
                return {
                    "parallelized": value,
                    "evidence": key,
                    "worker_count": worker_count,
                    "worker_count_key": worker_key,
                }

    if worker_count > 1:
        return {
            "parallelized": True,
            "evidence": worker_key,
            "worker_count": worker_count,
            "worker_count_key": worker_key,
        }

    return {
        "parallelized": False,
        "evidence": "not_reported",
        "worker_count": worker_count,
        "worker_count_key": worker_key,
    }


def _scan_summary(step: Mapping[str, Any], *, scan_kind: str) -> dict[str, Any]:
    metrics = step.get("metrics") if isinstance(step.get("metrics"), Mapping) else {}
    metrics = dict(metrics or {})
    parallel = _parallel_evidence(metrics)
    return {
        "scan_kind": scan_kind,
        "step_name": str(step.get("name") or ""),
        "elapsed_ms": _coerce_int(step.get("elapsed_ms"), 0),
        "parallelized": bool(parallel["parallelized"]),
        "parallel_evidence": parallel["evidence"],
        "worker_count": int(parallel["worker_count"] or 0),
        "module_count": _coerce_int(metrics.get("module_count"), 0),
        "function_count": _coerce_int(metrics.get("function_count"), 0),
        "entry_count": _coerce_int(metrics.get("entry_count"), 0),
        "colored_function_count": _coerce_int(metrics.get("colored_function_count"), 0),
        "candidate_count": _coerce_int(
            metrics.get("candidate_count")
            or metrics.get("feature_cluster_count")
            or metrics.get("node_count")
            or metrics.get("function_count"),
            0,
        ),
    }


def summarize_candidate_scanning_observability(
    phase_trace: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Summarize AST/DFS candidate-scan observability from phase trace metrics.

    The helper is intentionally evidence-only. It reports parallelization only
    when phase metrics include an explicit parallel flag or a worker count above
    one; otherwise it marks the scan as serial/not reported.
    """

    steps = _iter_phase_steps(phase_trace)
    ast_step = _find_step(steps, _AST_SCAN_STEP_NAMES)
    dfs_step = _find_step(steps, _DFS_SCAN_STEP_NAMES)
    ast_summary = _scan_summary(ast_step, scan_kind="ast_candidate_scan") if ast_step else {
        "scan_kind": "ast_candidate_scan",
        "step_name": "",
        "elapsed_ms": 0,
        "parallelized": False,
        "parallel_evidence": "missing_phase_step",
        "worker_count": 0,
        "module_count": 0,
        "function_count": 0,
        "entry_count": 0,
        "colored_function_count": 0,
        "candidate_count": 0,
    }
    dfs_summary = _scan_summary(dfs_step, scan_kind="dfs_candidate_scan") if dfs_step else {
        "scan_kind": "dfs_candidate_scan",
        "step_name": "",
        "elapsed_ms": 0,
        "parallelized": False,
        "parallel_evidence": "missing_phase_step",
        "worker_count": 0,
        "module_count": 0,
        "function_count": 0,
        "entry_count": 0,
        "colored_function_count": 0,
        "candidate_count": 0,
    }
    return {
        "schema_version": "symbol_cluster.candidate_scanning_observability.v1",
        "phase_trace_run_id": str(phase_trace.get("run_id") or "") if isinstance(phase_trace, Mapping) else "",
        "phase_step_count": len(steps),
        "ast_candidate_scanning": ast_summary,
        "dfs_candidate_scanning": dfs_summary,
        "any_parallelized": bool(ast_summary["parallelized"] or dfs_summary["parallelized"]),
    }


__all__ = [
    "ClusterReport",
    "process_cluster_with_ai",
    "summarize_candidate_scanning_observability",
]
