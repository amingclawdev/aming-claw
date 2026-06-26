import json

from agent.governance.reconcile_parallel_executor import run_reconcile_tasks
from agent.governance.reconcile_dependency_closure import (
    FAN_IN,
    FAN_OUT,
    build_dependency_adjacency,
    dependency_closure,
    dependency_graph_has_cycle,
    dependency_impact_closure,
    normalize_dependency_links,
    reduce_dependency_closures,
    stable_dependency_dfs,
)


def _impact_worker(task):
    return dependency_impact_closure(
        task["roots"],
        task["links"],
        edge_types=task.get("edge_types"),
        include_roots=task.get("include_roots", False),
    )


class _FakePool:
    def __init__(self, max_workers: int):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def map(self, worker, tasks):
        return [worker(task) for task in tasks]


def assert_json_serializable(payload):
    json.dumps(payload, sort_keys=True)


def test_stable_dependency_dfs_traverses_sorted_children_depth_first():
    links = [
        {"source": "A", "target": "C", "type": "depends_on"},
        {"source": "B", "target": "E", "type": "depends_on"},
        {"source": "A", "target": "B", "type": "depends_on"},
        {"source": "C", "target": "D", "type": "depends_on"},
        {"source": "B", "target": "D", "type": "depends_on"},
    ]

    adjacency = build_dependency_adjacency(reversed(links), direction=FAN_OUT)
    assert adjacency == {
        "A": ["B", "C"],
        "B": ["D", "E"],
        "C": ["D"],
    }
    assert stable_dependency_dfs("A", adjacency) == ["B", "D", "E", "C"]

    closure = dependency_closure(["A"], reversed(links), direction=FAN_OUT)
    assert closure["order"] == ["B", "D", "E", "C"]
    assert closure["by_root"] == {"A": ["B", "D", "E", "C"]}
    assert_json_serializable(closure)


def test_dependency_impact_closure_reports_fan_in_and_fan_out():
    links = [
        {"source": "worker", "target": "db", "type": "depends_on"},
        {"source": "api", "target": "cache", "type": "depends_on"},
        {"source": "ui", "target": "api", "type": "depends_on"},
        {"source": "api", "target": "db", "type": "depends_on"},
        {"source": "reporter", "target": "worker", "type": "depends_on"},
    ]

    db = dependency_impact_closure(["db"], links)
    assert db["fan_in"]["order"] == ["api", "ui", "worker", "reporter"]
    assert db["fan_out"]["order"] == []
    assert db["impacted_nodes"] == ["api", "reporter", "ui", "worker"]
    assert db["impacted_node_count"] == 4
    assert_json_serializable(db)

    api = dependency_impact_closure(["api"], links)
    assert api["fan_in"]["order"] == ["ui"]
    assert api["fan_out"]["order"] == ["cache", "db"]
    assert api["impacted_nodes"] == ["cache", "db", "ui"]


def test_reduce_dependency_closures_is_order_independent():
    links = [
        {"source": "api", "target": "db", "type": "depends_on"},
        {"source": "ui", "target": "api", "type": "depends_on"},
        {"source": "worker", "target": "db", "type": "depends_on"},
    ]
    fan_in = dependency_closure(["db"], links, direction=FAN_IN)
    fan_out = dependency_closure(["api"], links, direction=FAN_OUT)

    reduced = reduce_dependency_closures([fan_out, fan_in])
    reversed_reduced = reduce_dependency_closures([fan_in, fan_out])

    assert reduced == reversed_reduced
    assert reduced == {
        "schema_version": "dependency_closure_reduce.v1",
        "closure_count": 2,
        "directions": ["fan_in", "fan_out"],
        "roots": ["api", "db"],
        "reachable": ["api", "db", "ui", "worker"],
        "node_count": 4,
        "by_direction": {
            "fan_in": ["api", "ui", "worker"],
            "fan_out": ["db"],
        },
        "by_root": {
            "api": ["db"],
            "db": ["api", "ui", "worker"],
        },
    }
    assert_json_serializable(reduced)


def test_cycles_do_not_loop_and_are_detected():
    links = [
        {"source": "B", "target": "C", "type": "depends_on"},
        {"source": "C", "target": "A", "type": "depends_on"},
        {"source": "A", "target": "B", "type": "depends_on"},
    ]

    assert dependency_graph_has_cycle(links) is True
    assert dependency_closure(["A"], links, direction=FAN_OUT)["order"] == ["B", "C"]
    assert dependency_closure(["A"], links, direction=FAN_OUT, include_roots=True)["order"] == [
        "A",
        "B",
        "C",
    ]
    assert dependency_impact_closure(["A"], links)["has_cycle"] is True


def test_empty_inputs_return_empty_json_payloads():
    closure = dependency_closure([], [], direction=FAN_OUT)
    assert closure == {
        "schema_version": "dependency_closure.v1",
        "direction": "fan_out",
        "roots": [],
        "include_roots": False,
        "order": [],
        "reachable": [],
        "node_count": 0,
        "by_root": {},
        "adjacency": {},
    }

    reduced = reduce_dependency_closures([])
    assert reduced == {
        "schema_version": "dependency_closure_reduce.v1",
        "closure_count": 0,
        "directions": [],
        "roots": [],
        "reachable": [],
        "node_count": 0,
        "by_direction": {},
        "by_root": {},
    }
    assert dependency_impact_closure([], [])["impacted_nodes"] == []
    assert dependency_graph_has_cycle([]) is False
    assert_json_serializable(closure)
    assert_json_serializable(reduced)


def test_normalize_dependency_links_accepts_graph_shape_and_filters_edge_types():
    graph = {
        "deps_graph": {
            "links": [
                {"source": "A", "target": "B", "type": "depends_on"},
                {"source": "A", "target": "B", "type": "depends_on"},
                {"source": "B", "target": "C", "type": "reads_state"},
                {"source": "C", "target": "C", "type": "depends_on"},
                {"source": "", "target": "D", "type": "depends_on"},
            ]
        }
    }

    assert normalize_dependency_links(graph, edge_types=["depends_on"]) == [
        {"source": "A", "target": "B", "type": "depends_on"}
    ]
    assert normalize_dependency_links(graph) == [
        {"source": "A", "target": "B", "type": "depends_on"},
        {"source": "B", "target": "C", "type": "reads_state"},
    ]


def test_dependency_impact_parallel_worker_matches_serial_results():
    links = [
        {"source": "worker", "target": "db", "type": "depends_on"},
        {"source": "api", "target": "cache", "type": "depends_on"},
        {"source": "ui", "target": "api", "type": "depends_on"},
        {"source": "api", "target": "db", "type": "depends_on"},
        {"source": "reporter", "target": "worker", "type": "depends_on"},
    ]
    tasks = [
        {"roots": ["db"], "links": links},
        {"roots": ["api"], "links": list(reversed(links))},
    ]

    serial = [json.loads(json.dumps(_impact_worker(task), sort_keys=True)) for task in tasks]
    parallel = run_reconcile_tasks(
        tasks,
        _impact_worker,
        label="dependency_closure",
        cpu_count=8,
        process_pool_factory=_FakePool,
    )

    assert json.loads(json.dumps(parallel["results"], sort_keys=True)) == serial
    assert parallel["observability"]["strategy"] == "parallel_process_pool"
    assert parallel["observability"]["deterministic_order"] == "input_order"
