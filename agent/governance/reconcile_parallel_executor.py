"""Bounded pure-compute executor helpers for reconcile.

The helpers in this module intentionally do not know about governance DB state.
Callers provide JSON-serializable task payloads and a top-level worker callable;
results are returned in the same order as the task payloads.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable, Iterable, Mapping


ENV_WORKERS = "AMING_RECONCILE_WORKERS"


def default_worker_count(task_count: int, *, cpu_count: int | None = None) -> int:
    """Return the conservative default cap for process-pool workers."""
    count = max(0, int(task_count or 0))
    if count <= 1:
        return 1
    cpus = cpu_count if cpu_count is not None else os.cpu_count()
    cpu_cap = max(1, int(cpus or 1) - 1)
    return max(1, min(count, cpu_cap))


def _parse_worker_override(value: Any) -> tuple[int | None, str]:
    text = str(value or "").strip().lower()
    if not text:
        return None, ""
    if text in {"serial", "disabled", "disable", "false", "off", "no"}:
        return 1, "configured_serial"
    try:
        parsed = int(text)
    except ValueError:
        return None, "invalid_worker_override"
    if parsed <= 1:
        return 1, "configured_serial"
    return parsed, ""


def resolve_worker_count(
    task_count: int,
    *,
    max_workers: int | None = None,
    env: Mapping[str, str] | None = None,
    env_var: str = ENV_WORKERS,
    cpu_count: int | None = None,
) -> tuple[int, dict[str, Any]]:
    """Resolve bounded worker count plus observability metadata."""
    count = max(0, int(task_count or 0))
    default_count = default_worker_count(count, cpu_count=cpu_count)
    environ = os.environ if env is None else env
    override_value = environ.get(env_var, "") if environ is not None else ""
    override_count, override_reason = _parse_worker_override(override_value)
    requested = max_workers if max_workers is not None else override_count
    if requested is None:
        worker_count = default_count
        source = "default_cpu_cap"
    else:
        worker_count = max(1, int(requested or 1))
        source = "explicit" if max_workers is not None else "environment"
    worker_count = max(1, min(count or 1, worker_count))
    details = {
        "env_var": env_var,
        "env_value": str(override_value or ""),
        "task_count": count,
        "cpu_count": cpu_count if cpu_count is not None else os.cpu_count(),
        "default_worker_count": default_count,
        "requested_worker_count": requested,
        "worker_count": worker_count,
        "worker_count_source": source,
        "configuration_reason": override_reason,
    }
    return worker_count, details


def _serial_map(worker: Callable[[Any], Any], tasks: list[Any]) -> list[Any]:
    return [worker(task) for task in tasks]


def run_reconcile_tasks(
    tasks: Iterable[Any],
    worker: Callable[[Any], Any],
    *,
    label: str = "reconcile_parallel",
    max_workers: int | None = None,
    env: Mapping[str, str] | None = None,
    env_var: str = ENV_WORKERS,
    cpu_count: int | None = None,
    process_pool_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run pure reconcile tasks with ordered process-pool results.

    Process-pool failures fall back to serial execution. If the serial fallback
    also fails, the original serial exception is allowed to surface to the
    caller because there is no safe compute result to reduce.
    """
    task_list = list(tasks)
    worker_count, worker_details = resolve_worker_count(
        len(task_list),
        max_workers=max_workers,
        env=env,
        env_var=env_var,
        cpu_count=cpu_count,
    )
    observability: dict[str, Any] = {
        "schema_version": "reconcile_parallel_executor.v1",
        "label": label,
        "strategy": "serial",
        "fallback_reason": "",
        "fallback_error_type": "",
        "fallback_error": "",
        "deterministic_order": "input_order",
        **worker_details,
    }
    if len(task_list) <= 1:
        observability["fallback_reason"] = "single_task"
        return {"results": _serial_map(worker, task_list), "observability": observability}
    if worker_count <= 1:
        if not observability.get("fallback_reason"):
            observability["fallback_reason"] = observability.get("configuration_reason") or "worker_count_one"
        return {"results": _serial_map(worker, task_list), "observability": observability}

    pool_factory = process_pool_factory or ProcessPoolExecutor
    try:
        with pool_factory(max_workers=worker_count) as pool:
            results = list(pool.map(worker, task_list))
        observability["strategy"] = "parallel_process_pool"
        return {"results": results, "observability": observability}
    except Exception as exc:
        observability["strategy"] = "serial_fallback"
        observability["fallback_reason"] = "process_pool_failed"
        observability["fallback_error_type"] = type(exc).__name__
        observability["fallback_error"] = str(exc)
        return {"results": _serial_map(worker, task_list), "observability": observability}
