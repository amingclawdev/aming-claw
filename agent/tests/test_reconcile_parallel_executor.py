from __future__ import annotations

from agent.governance.reconcile_parallel_executor import (
    default_worker_count,
    resolve_worker_count,
    run_reconcile_tasks,
)


def _double(value: int) -> int:
    return value * 2


def test_default_worker_count_caps_by_task_count_and_cpu_count():
    assert default_worker_count(0, cpu_count=8) == 1
    assert default_worker_count(1, cpu_count=8) == 1
    assert default_worker_count(2, cpu_count=8) == 2
    assert default_worker_count(8, cpu_count=4) == 3
    assert default_worker_count(8, cpu_count=None) >= 1


def test_resolve_worker_count_honors_env_disable_and_caps_to_tasks():
    worker_count, details = resolve_worker_count(
        5,
        env={"AMING_RECONCILE_WORKERS": "off"},
        cpu_count=8,
    )

    assert worker_count == 1
    assert details["worker_count_source"] == "environment"
    assert details["configuration_reason"] == "configured_serial"

    worker_count, details = resolve_worker_count(
        2,
        max_workers=99,
        env={},
        cpu_count=8,
    )

    assert worker_count == 2
    assert details["requested_worker_count"] == 99
    assert details["worker_count_source"] == "explicit"


def test_resolve_worker_count_records_invalid_env_override_and_uses_default():
    worker_count, details = resolve_worker_count(
        5,
        env={"AMING_RECONCILE_WORKERS": "many"},
        cpu_count=4,
    )

    assert worker_count == 3
    assert details["worker_count_source"] == "default_cpu_cap"
    assert details["configuration_reason"] == "invalid_worker_override"
    assert details["env_value"] == "many"


def test_run_reconcile_tasks_preserves_order_with_process_pool_factory():
    class FakePool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def map(self, worker, tasks):
            task_list = list(tasks)
            assert task_list == [3, 1, 2]
            return [worker(task) for task in task_list]

    result = run_reconcile_tasks(
        [3, 1, 2],
        _double,
        cpu_count=8,
        process_pool_factory=FakePool,
    )

    assert result["results"] == [6, 2, 4]
    observability = result["observability"]
    assert observability["strategy"] == "parallel_process_pool"
    assert observability["worker_count"] == 3
    assert observability["deterministic_order"] == "input_order"


def test_run_reconcile_tasks_falls_back_to_serial_when_process_pool_fails():
    class FailingPool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def map(self, worker, tasks):
            raise RuntimeError("pool unavailable")

    result = run_reconcile_tasks(
        [2, 4],
        _double,
        cpu_count=8,
        process_pool_factory=FailingPool,
    )

    assert result["results"] == [4, 8]
    observability = result["observability"]
    assert observability["strategy"] == "serial_fallback"
    assert observability["fallback_reason"] == "process_pool_failed"
    assert observability["fallback_error_type"] == "RuntimeError"
    assert "pool unavailable" in observability["fallback_error"]
