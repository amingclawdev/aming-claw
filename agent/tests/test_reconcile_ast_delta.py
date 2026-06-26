from __future__ import annotations

import json

from agent.governance.reconcile_parallel_executor import run_reconcile_tasks
from agent.governance.reconcile_ast_delta import (
    function_signature_delta,
    python_source_function_signatures,
    source_file_function_delta,
)


def _json_roundtrip(value):
    return json.loads(json.dumps(value, sort_keys=True))


def _signature(source: str):
    result = python_source_function_signatures(
        source,
        module_name="agent.example",
        path="agent/example.py",
    )
    assert result["ok"] is True
    return result["functions"]


def _source_delta_worker(task):
    return source_file_function_delta(**task)


class _FakePool:
    def __init__(self, max_workers: int):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def map(self, worker, tasks):
        return [worker(task) for task in tasks]


def test_source_function_delta_reports_added_and_removed_identities_deterministically():
    before = """
def zebra(value):
    return value

def beta(value):
    return value + 1
"""
    after = """
def alpha(value):
    return value

def beta(value):
    return value + 2
"""

    result = source_file_function_delta(
        before_source=before,
        after_source=after,
        module_name="agent.example",
        path="agent/example.py",
    )
    result = _json_roundtrip(result)

    assert result["ok"] is True
    delta = result["delta"]
    assert delta["stable_identity"] is False
    assert delta["requires_full_rebuild"] is True
    assert delta["reason"] == "source_function_identity_changed"
    assert delta["added_functions"] == ["agent.example::alpha"]
    assert delta["removed_functions"] == ["agent.example::zebra"]
    assert delta["unchanged_functions"] == ["agent.example::beta"]


def test_source_function_delta_reports_same_identity_signature_changes():
    before = """
def run(value: int, *, verbose: bool = False) -> int:
    return value
"""
    after = """
def run(value: int, limit: int = 10, *, verbose: bool = False) -> int:
    return value + limit
"""

    result = _json_roundtrip(
        source_file_function_delta(
            before_source=before,
            after_source=after,
            module_name="agent.example",
            path="agent/example.py",
        )
    )
    delta = result["delta"]

    assert delta["stable_identity"] is True
    assert delta["stable_signatures"] is False
    assert delta["requires_full_rebuild"] is True
    assert delta["reason"] == "source_function_signature_changed"
    assert delta["changed_functions"] == ["agent.example::run"]
    assert delta["signature_changes"][0]["qualified_name"] == "agent.example::run"
    assert delta["signature_changes"][0]["before_signature_hash"].startswith("sha256:")
    assert delta["signature_changes"][0]["after_signature_hash"].startswith("sha256:")
    assert (
        delta["signature_changes"][0]["before_signature_hash"]
        != delta["signature_changes"][0]["after_signature_hash"]
    )


def test_line_only_moves_do_not_require_full_rebuild():
    before = """
def first():
    return 1

def moved(value):
    return value
"""
    after = """
def inserted():
    return "not compared"

def first():
    return 1

def moved(value):
    return value
"""

    before_signature = [
        item for item in _signature(before)
        if item["qualified_name"] in {"agent.example::first", "agent.example::moved"}
    ]
    after_signature = [
        item for item in _signature(after)
        if item["qualified_name"] in {"agent.example::first", "agent.example::moved"}
    ]
    delta = _json_roundtrip(
        function_signature_delta(
            before_signature=before_signature,
            after_signature=after_signature,
        )
    )

    assert delta["stable_identity"] is True
    assert delta["stable_signatures"] is True
    assert delta["requires_full_rebuild"] is False
    assert delta["reason"] == "source_function_signature_stable"
    assert delta["line_range_changed"] is True
    assert [
        item["qualified_name"] for item in delta["line_range_changes"]
    ] == ["agent.example::first", "agent.example::moved"]


def test_python_source_function_signatures_include_methods_async_and_annotations():
    source = """
class Service:
    @classmethod
    async def build(cls, name: str = "default") -> "Service":
        return cls()

def run(*items: str, dry_run: bool = False, **metadata: object) -> None:
    return None
"""

    result = _json_roundtrip(
        python_source_function_signatures(
            source,
            module_name="agent.example",
            path="agent/example.py",
        )
    )

    assert result["ok"] is True
    assert [item["qualified_name"] for item in result["functions"]] == [
        "agent.example::Service.build",
        "agent.example::run",
    ]
    build = result["functions"][0]
    assert build["kind"] == "async_function"
    assert build["signature"]["args"][1]["name"] == "name"
    assert build["signature"]["returns"] == "Constant(value='Service')"
    assert build["decorators"] == ["Name(id='classmethod', ctx=Load())"]


def test_parse_failure_is_json_serializable_and_does_not_raise():
    result = _json_roundtrip(
        source_file_function_delta(
            before_source="def ok():\n    return 1\n",
            after_source="def broken(:\n",
            module_name="agent.example",
            path="agent/example.py",
        )
    )

    assert result["ok"] is False
    assert result["reason"] == "source_ast_parse_failed"
    assert result["before"]["ok"] is True
    assert result["after"]["ok"] is False
    assert result["after"]["parse_error"]["type"] == "SyntaxError"


def test_source_delta_parallel_worker_matches_serial_results():
    tasks = [
        {
            "before_source": "def run(value):\n    return value\n",
            "after_source": "def run(value):\n    return value + 1\n",
            "module_name": "agent.example",
            "path": "agent/example.py",
        },
        {
            "before_source": "def alpha(value):\n    return value\n",
            "after_source": "def alpha(value, limit=1):\n    return value + limit\n",
            "module_name": "agent.example",
            "path": "agent/example.py",
        },
    ]

    serial = [_json_roundtrip(_source_delta_worker(task)) for task in tasks]
    parallel = run_reconcile_tasks(
        tasks,
        _source_delta_worker,
        label="ast_delta",
        cpu_count=8,
        process_pool_factory=_FakePool,
    )

    assert _json_roundtrip(parallel["results"]) == serial
    assert parallel["observability"]["strategy"] == "parallel_process_pool"
    assert parallel["observability"]["deterministic_order"] == "input_order"
