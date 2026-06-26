"""Tests for Phase Z v2 PR1 — symbol-level topology infrastructure.

11 tests covering:
- parse_production_modules (AC1)
- build_call_graph with import-aware resolution (AC2, AC6)
- tarjan_scc (AC3)
- handle_cycle (AC4, AC5)
"""
from __future__ import annotations

import os
import tempfile
import textwrap
from typing import Dict, List

import pytest

from agent.governance.reconcile_phases import phase_z_v2 as phase_z_v2_module
from agent.governance.reconcile_phases.phase_z_v2 import (
    EXCLUDE_DIRS,
    CallGraph,
    FunctionMeta,
    ModuleInfo,
    build_function_call_facts,
    build_function_call_facts_parallel,
    build_call_graph,
    enrich_nodes_with_function_call_facts,
    handle_cycle,
    parse_production_modules,
    register_short_name_cross_module_policy,
    tarjan_scc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp: str, files: Dict[str, str]) -> str:
    """Create a temporary project structure.

    files: {relative_path: source_code}
    Returns project root path.
    """
    for rel_path, source in files.items():
        full = os.path.join(tmp, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(source))
    return tmp


def _make_modules(module_specs: Dict[str, Dict]) -> Dict[str, ModuleInfo]:
    """Build ModuleInfo dict from specs for unit testing graph/cycle logic.

    module_specs: {
        "mod_a": {
            "import_map": {"foo": "mod_b.foo"},
            "functions": [
                {"name": "func1", "calls": ["foo"], "decorators": [], "is_entry": False},
            ]
        }
    }
    """
    modules: Dict[str, ModuleInfo] = {}
    for mod_name, spec in module_specs.items():
        funcs = []
        for fspec in spec.get("functions", []):
            qname = f"{mod_name}::{fspec['name']}"
            funcs.append(FunctionMeta(
                module=mod_name,
                name=fspec["name"],
                qualified_name=qname,
                lineno=1,
                end_lineno=10,
                decorators=fspec.get("decorators", []),
                calls=fspec.get("calls", []),
                is_entry=fspec.get("is_entry", False),
            ))
        modules[mod_name] = ModuleInfo(
            path=f"/fake/{mod_name.replace('.', '/')}.py",
            module_name=mod_name,
            import_map=spec.get("import_map", {}),
            functions=funcs,
            language=spec.get("language", ""),
        )
    return modules


def test_coverage_scan_cache_matches_direct_lookup(tmp_path):
    project = _make_project(str(tmp_path), {
        "agent/__init__.py": "",
        "agent/service.py": """\
            def run():
                return 1
        """,
        "tests/test_service.py": """\
            from agent.service import run

            def test_run():
                assert run() == 1
        """,
        "docs/service.md": "See agent/service.py for details.\n",
    })
    primary = os.path.join(project, "agent", "service.py")

    cache = phase_z_v2_module.build_coverage_scan_cache(project)

    assert phase_z_v2_module.find_test_coverage(
        project,
        primary,
        coverage_scan_cache=cache,
    ) == phase_z_v2_module.find_test_coverage(project, primary)
    assert phase_z_v2_module.find_doc_coverage(
        project,
        primary,
        coverage_scan_cache=cache,
    ) == phase_z_v2_module.find_doc_coverage(project, primary)


def test_extract_typed_relations_parallel_matches_serial_with_fake_pool(tmp_path):
    project = _make_project(str(tmp_path), {
        "agent/__init__.py": "",
        "agent/service.py": """\
            CREATE_TABLE_SQL = "CREATE TABLE tasks (id TEXT)"
            INSERT_SQL = "INSERT INTO tasks VALUES (?)"

            def create_task():
                return "task.created"
        """,
        "agent/api.py": """\
            def create_task_route():
                return "/api/task"
        """,
    })
    modules = parse_production_modules(project)

    class FakePool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def map(self, worker, tasks):
            task_list = list(tasks)
            module_names = [task["module"]["module_name"] for task in task_list]
            assert module_names == sorted(module_names)
            assert {"agent.api", "agent.service"}.issubset(set(module_names))
            return [worker(task) for task in task_list]

    serial = phase_z_v2_module.extract_typed_relations(project, modules)
    parallel, execution = phase_z_v2_module.extract_typed_relations_parallel(
        project,
        modules,
        cpu_count=8,
        process_pool_factory=FakePool,
    )

    assert parallel == serial
    assert execution["strategy"] == "parallel_process_pool"
    assert execution["parallelized"] is True
    assert execution["worker_count"] >= 2
    assert execution["module_count"] >= 2


def test_extract_typed_relations_parallel_falls_back_when_pool_fails(tmp_path):
    project = _make_project(str(tmp_path), {
        "agent/__init__.py": "",
        "agent/service.py": """\
            CREATE_TABLE_SQL = "CREATE TABLE tasks (id TEXT)"
            INSERT_SQL = "INSERT INTO tasks VALUES (?)"
        """,
        "agent/api.py": """\
            def create_task_route():
                return "/api/task"
        """,
    })
    modules = parse_production_modules(project)

    class FailingPool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def map(self, worker, tasks):
            raise RuntimeError("typed relation pool unavailable")

    serial = phase_z_v2_module.extract_typed_relations(project, modules)
    fallback, execution = phase_z_v2_module.extract_typed_relations_parallel(
        project,
        modules,
        cpu_count=8,
        process_pool_factory=FailingPool,
    )

    assert fallback == serial
    assert execution["strategy"] == "serial_fallback"
    assert execution["parallelized"] is False
    assert execution["fallback_reason"] == "process_pool_failed"
    assert execution["fallback_error_type"] == "RuntimeError"


def test_parse_production_modules_parallel_matches_serial_with_fake_pool(tmp_path):
    project = _make_project(str(tmp_path), {
        "agent/__init__.py": "",
        "agent/service.py": "def run():\n    return 1\n",
        "agent/worker.py": "def work():\n    return 2\n",
        "tests/test_service.py": "def test_run():\n    assert True\n",
    })

    class FakePool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def map(self, worker, tasks):
            task_list = list(tasks)
            rel_paths = [task["rel_path"] for task in task_list]
            assert rel_paths == sorted(rel_paths)
            assert "tests/test_service.py" not in rel_paths
            return [worker(task) for task in task_list]

    serial = parse_production_modules(project)
    parallel, execution = phase_z_v2_module.parse_production_modules_parallel(
        project,
        cpu_count=8,
        process_pool_factory=FakePool,
    )

    assert sorted(parallel) == sorted(serial)
    assert {
        name: [func.qualified_name for func in module.functions]
        for name, module in parallel.items()
    } == {
        name: [func.qualified_name for func in module.functions]
        for name, module in serial.items()
    }
    assert execution["strategy"] == "parallel_process_pool"
    assert execution["parallelized"] is True
    assert execution["source_file_count"] >= 2


def test_parse_production_modules_parallel_falls_back_when_pool_fails(tmp_path):
    project = _make_project(str(tmp_path), {
        "agent/__init__.py": "",
        "agent/service.py": "def run():\n    return 1\n",
        "agent/worker.py": "def work():\n    return 2\n",
    })

    class FailingPool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def map(self, worker, tasks):
            raise RuntimeError("parser pool unavailable")

    serial = parse_production_modules(project)
    fallback, execution = phase_z_v2_module.parse_production_modules_parallel(
        project,
        cpu_count=8,
        process_pool_factory=FailingPool,
    )

    assert sorted(fallback) == sorted(serial)
    assert execution["strategy"] == "serial_fallback"
    assert execution["parallelized"] is False
    assert execution["fallback_reason"] == "process_pool_failed"
    assert execution["fallback_error_type"] == "RuntimeError"


# ===========================================================================
# AC1: parse_production_modules
# ===========================================================================

class TestParseProductionModules:
    """AC1: parse_production_modules walks agent/ + scripts/ excluding EXCLUDE_DIRS."""

    def test_parse_production_modules_extracts_functions(self, tmp_path):
        """Core AC1 test: functions are extracted with correct metadata."""
        project = _make_project(str(tmp_path), {
            "agent/__init__.py": "",
            "agent/core.py": """\
                import os
                from agent.utils import helper

                def main_func():
                    helper()
                    os.path.exists("x")

                def _private():
                    pass
            """,
            "agent/utils.py": """\
                def helper():
                    return 42
            """,
            "scripts/deploy.py": """\
                def run_deploy():
                    print("deploying")
            """,
            # This should be excluded
            "agent/__pycache__/cached.py": """\
                def should_not_appear():
                    pass
            """,
        })

        modules = parse_production_modules(project, prod_dirs=("agent", "scripts"))

        # Should find agent.core, agent.utils, scripts.deploy (+ agent.__init__)
        assert "agent.core" in modules
        assert "agent.utils" in modules
        assert "scripts.deploy" in modules

        # __pycache__ should be excluded
        for mod_name in modules:
            assert "__pycache__" not in mod_name

        # Check function extraction
        core = modules["agent.core"]
        func_names = [f.name for f in core.functions]
        assert "main_func" in func_names
        assert "_private" in func_names

        # Check import map
        assert "helper" in core.import_map
        assert core.import_map["helper"] == "agent.utils.helper"

        # Check function metadata
        main_func = next(f for f in core.functions if f.name == "main_func")
        assert main_func.lineno > 0
        assert main_func.end_lineno >= main_func.lineno
        assert "helper" in main_func.calls

    def test_exclude_dirs_are_skipped(self, tmp_path):
        """Verify EXCLUDE_DIRS constant contains required entries and filtering works."""
        required = {
            "__pycache__", ".git", "node_modules", ".venv", "venv",
            ".claude", ".worktrees", "shared-volume", "runtime",
            ".mypy_cache", ".pytest_cache", "build", "dist",
        }
        assert required.issubset(EXCLUDE_DIRS)

        # Create files in excluded dirs
        project = _make_project(str(tmp_path), {
            "agent/good.py": "def good(): pass\n",
            "agent/.venv/bad.py": "def bad(): pass\n",
            "agent/node_modules/bad2.py": "def bad2(): pass\n",
        })

        modules = parse_production_modules(project, prod_dirs=("agent",))
        mod_names = list(modules.keys())
        assert any("good" in m for m in mod_names)
        assert not any(".venv" in m for m in mod_names)
        assert not any("node_modules" in m for m in mod_names)


# ===========================================================================
# AC2 + AC6: build_call_graph with import-aware resolution
# ===========================================================================

class TestBuildCallGraph:
    """AC2: Import-aware resolution. AC6: weak_edges structure."""

    def test_build_call_graph_ambiguous_call(self):
        """AC2+AC6: Ambiguous call produces weak_edge with candidates."""
        # Three modules each define a function called "get"
        modules = _make_modules({
            "pkg.mod_a": {
                "import_map": {},
                "functions": [
                    {"name": "get", "calls": []},
                ],
            },
            "pkg.mod_b": {
                "import_map": {},
                "functions": [
                    {"name": "get", "calls": []},
                ],
            },
            "pkg.mod_c": {
                "import_map": {},
                "functions": [
                    {"name": "get", "calls": []},
                ],
            },
            "pkg.mod_caller": {
                "import_map": {},  # No import — ambiguous!
                "functions": [
                    {"name": "do_stuff", "calls": ["get"]},
                ],
            },
        })

        graph = build_call_graph(modules)

        # Should produce a weak edge because "get" is ambiguous (3 candidates)
        assert len(graph.weak_edges) >= 1
        we = graph.weak_edges[0]

        # AC6: weak_edges entries contain required keys
        assert hasattr(we, "caller")
        assert hasattr(we, "target")
        assert hasattr(we, "candidates")
        assert hasattr(we, "reason")

        assert we.caller == "pkg.mod_caller::do_stuff"
        assert we.target == "get"
        assert len(we.candidates) == 3
        assert "ambiguous" in we.reason

    def test_build_call_graph_does_not_cross_namespace_on_short_name(self):
        """Unqualified fallback must not connect frontend helpers to backend calls."""
        modules = _make_modules({
            "agent.governance.server": {
                "language": "python",
                "import_map": {},
                "functions": [
                    {"name": "handle", "calls": ["info"]},
                ],
            },
            "frontend.dashboard.scripts.e2e_semantic": {
                "language": "javascript",
                "import_map": {},
                "functions": [
                    {"name": "info", "calls": []},
                ],
            },
        })

        graph = build_call_graph(modules)

        assert graph.edges.get("agent.governance.server::handle") == []
        assert graph.weak_edges == []

    def test_build_call_graph_does_not_cross_module_js_ts_local_closures(self):
        """TS/JS short-name fallback policy is registry/config driven."""
        modules = _make_modules({
            "frontend.dashboard.src.components.InspectorDrawer": {
                "language": "typescript",
                "import_map": {},
                "functions": [
                    {"name": "InspectorDrawer", "calls": []},
                    {"name": "setTab", "calls": []},
                    {"name": "score", "calls": []},
                    {"name": "importantChildrenOf", "calls": ["score"]},
                ],
            },
            "frontend.dashboard.src.components.ActionPanel": {
                "language": "typescript",
                "import_map": {},
                "functions": [
                    {"name": "ActionPanel", "calls": ["setTab"]},
                ],
            },
            "frontend.dashboard.src.components.FocusCard": {
                "language": "typescript",
                "import_map": {},
                "functions": [
                    {"name": "NodeFocusCard", "calls": ["score"]},
                ],
            },
        })

        graph = build_call_graph(modules)

        assert (
            graph.edges.get("frontend.dashboard.src.components.ActionPanel::ActionPanel")
            == []
        )
        assert (
            graph.edges.get("frontend.dashboard.src.components.FocusCard::NodeFocusCard")
            == []
        )
        assert (
            "frontend.dashboard.src.components.InspectorDrawer::score"
            in graph.edges.get(
                "frontend.dashboard.src.components.InspectorDrawer::importantChildrenOf",
                [],
            )
        )
        assert graph.weak_edges == []

        permissive_graph = build_call_graph(
            modules,
            call_resolution_rules={"typescript": "same_namespace_fallback"},
        )
        assert (
            "frontend.dashboard.src.components.InspectorDrawer::setTab"
            in permissive_graph.edges.get(
                "frontend.dashboard.src.components.ActionPanel::ActionPanel",
                [],
            )
        )
        assert (
            "frontend.dashboard.src.components.InspectorDrawer::score"
            in permissive_graph.edges.get(
                "frontend.dashboard.src.components.FocusCard::NodeFocusCard",
                [],
            )
        )

    def test_build_call_graph_import_resolved(self):
        """Import map resolves call to correct target — not naive match."""
        modules = _make_modules({
            "mod_a": {
                "import_map": {},
                "functions": [
                    {"name": "helper", "calls": []},
                ],
            },
            "mod_b": {
                "import_map": {},
                "functions": [
                    {"name": "helper", "calls": []},
                ],
            },
            "mod_caller": {
                "import_map": {"helper": "mod_a.helper"},
                "functions": [
                    {"name": "work", "calls": ["helper"]},
                ],
            },
        })

        graph = build_call_graph(modules)

        # Should resolve to mod_a::helper via import map, not ambiguous
        assert len(graph.weak_edges) == 0
        assert "mod_a::helper" in graph.edges.get("mod_caller::work", [])
        assert "mod_b::helper" not in graph.edges.get("mod_caller::work", [])

    def test_build_call_graph_uses_registered_short_name_policy(self):
        """Call resolution rules can use newly registered policy handlers."""

        @register_short_name_cross_module_policy("test_no_cross_module_short_names")
        def _deny_cross_module_short_names(context):
            return False

        modules = _make_modules({
            "pkg.helpers": {
                "language": "python",
                "functions": [
                    {"name": "helper", "calls": []},
                ],
            },
            "pkg.worker": {
                "language": "python",
                "functions": [
                    {"name": "run", "calls": ["helper"]},
                ],
            },
        })

        default_graph = build_call_graph(modules)
        configured_graph = build_call_graph(
            modules,
            call_resolution_rules={"python": "test_no_cross_module_short_names"},
        )

        assert "pkg.helpers::helper" in default_graph.edges.get("pkg.worker::run", [])
        assert configured_graph.edges.get("pkg.worker::run") == []

    def test_build_function_call_facts_persists_callers_and_callees(self):
        """MVP graph metadata can carry function-level call evidence."""
        modules = _make_modules({
            "mod_a": {
                "functions": [
                    {"name": "helper", "calls": []},
                ],
            },
            "mod_b": {
                "import_map": {"helper": "mod_a.helper"},
                "functions": [
                    {"name": "work", "calls": ["helper"]},
                ],
            },
        })
        graph = build_call_graph(modules)

        facts = build_function_call_facts(modules, graph)
        nodes = [
            {"module": "mod_a", "node_id": "mod_a"},
            {"module": "mod_b", "node_id": "mod_b"},
        ]
        enrich_nodes_with_function_call_facts(nodes, facts)

        assert nodes[1]["function_calls"][0]["caller"] == "mod_b::work"
        assert nodes[1]["function_calls"][0]["callee"] == "mod_a::helper"
        assert nodes[0]["function_called_by"][0]["caller"] == "mod_b::work"
        assert nodes[0]["function_called_by_count"] == 1

    def test_build_function_call_facts_parallel_matches_serial_with_fake_pool(self):
        """Parallel worker partitioning preserves the serial fact contract."""

        class FakePool:
            def __init__(self, max_workers: int):
                self.max_workers = max_workers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def map(self, worker, tasks):
                task_list = list(tasks)
                assert [task["module_name"] for task in task_list] == ["mod_b", "mod_c"]
                return [worker(task) for task in task_list]

        modules = _make_modules({
            "mod_a": {
                "functions": [
                    {"name": "helper", "calls": []},
                ],
            },
            "mod_b": {
                "import_map": {"helper": "mod_a.helper"},
                "functions": [
                    {"name": "work", "calls": ["helper"]},
                ],
            },
            "mod_c": {
                "import_map": {"helper": "mod_a.helper"},
                "functions": [
                    {"name": "run", "calls": ["helper"]},
                ],
            },
        })
        graph = build_call_graph(modules)

        serial = build_function_call_facts(modules, graph)
        parallel, observability = build_function_call_facts_parallel(
            modules,
            graph,
            cpu_count=8,
            process_pool_factory=FakePool,
        )

        assert parallel == serial
        assert observability["strategy"] == "parallel_process_pool"
        assert observability["parallelized"] is True
        assert observability["worker_count"] == 2
        assert observability["task_partition"] == "caller_module"

    def test_build_function_call_facts_parallel_falls_back_to_serial_on_pool_failure(self):
        """Process-pool failure uses shared serial fallback without changing facts."""

        class FailingPool:
            def __init__(self, max_workers: int):
                self.max_workers = max_workers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def map(self, worker, tasks):
                raise RuntimeError("pool unavailable")

        modules = _make_modules({
            "mod_a": {
                "functions": [
                    {"name": "helper", "calls": []},
                ],
            },
            "mod_b": {
                "import_map": {"helper": "mod_a.helper"},
                "functions": [
                    {"name": "work", "calls": ["helper"]},
                ],
            },
            "mod_c": {
                "import_map": {"helper": "mod_a.helper"},
                "functions": [
                    {"name": "run", "calls": ["helper"]},
                ],
            },
        })
        graph = build_call_graph(modules)

        serial = build_function_call_facts(modules, graph)
        fallback, observability = build_function_call_facts_parallel(
            modules,
            graph,
            cpu_count=8,
            process_pool_factory=FailingPool,
        )

        assert fallback == serial
        assert observability["strategy"] == "serial_fallback"
        assert observability["parallelized"] is False
        assert observability["fallback_reason"] == "process_pool_failed"
        assert observability["fallback_error_type"] == "RuntimeError"

    def test_build_graph_v2_records_function_call_fact_parallel_observability(self, tmp_path, monkeypatch):
        """Full graph build exposes function-call fact executor details in phase trace."""

        class FakePool:
            def __init__(self, max_workers: int):
                self.max_workers = max_workers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def map(self, worker, tasks):
                return [worker(task) for task in list(tasks)]

        def fake_dfs_color_from_entries_parallel(edges, entries, track_distance=False, **kwargs):
            color_sets, color_count_map = phase_z_v2_module.dfs_color_from_entries(
                edges,
                entries,
                track_distance=track_distance,
            )
            return color_sets, color_count_map, {
                "strategy": "serial",
                "worker_count": 1,
                "parallelized": False,
            }

        original_parallel = phase_z_v2_module.build_function_call_facts_parallel

        def fake_build_function_call_facts_parallel(modules, call_graph, **kwargs):
            return original_parallel(
                modules,
                call_graph,
                graph_enrich_config_rules=kwargs.get("graph_enrich_config_rules"),
                cpu_count=8,
                process_pool_factory=FakePool,
            )

        monkeypatch.setattr(
            phase_z_v2_module,
            "dfs_color_from_entries_parallel",
            fake_dfs_color_from_entries_parallel,
        )
        monkeypatch.setattr(
            phase_z_v2_module,
            "build_function_call_facts_parallel",
            fake_build_function_call_facts_parallel,
        )
        project = _make_project(str(tmp_path), {
            "agent/__init__.py": "",
            "agent/a.py": """\
                from agent.b import helper

                def work():
                    helper()
            """,
            "agent/c.py": """\
                from agent.b import helper

                def run():
                    helper()
            """,
            "agent/b.py": """\
                def helper():
                    return 1
            """,
        })

        result = phase_z_v2_module.build_graph_v2_from_symbols(
            project,
            dry_run=True,
            scratch_dir=str(tmp_path / "scratch"),
            run_id="test-function-call-facts-parallel",
        )

        function_step = next(
            step
            for step in result["phase_trace"]["steps"]
            if step["name"] == "function_call_facts"
        )
        metrics = function_step["metrics"]
        assert metrics["strategy"] == "parallel_process_pool"
        assert metrics["parallelized"] is True
        assert metrics["worker_count"] == 2
        assert metrics["fallback_reason"] == ""
        assert metrics["executor"]["label"] == "phase_z_v2_function_call_facts"
        assert metrics["executor"]["task_partition"] == "caller_module"


# ===========================================================================
# AC3: Tarjan SCC
# ===========================================================================

class TestTarjanSCC:
    """AC3: tarjan_scc correctness on standard graph fixtures."""

    def test_tarjan_scc_finds_cycle(self):
        """A→B→A returns an SCC containing both A and B."""
        graph = {
            "A": ["B"],
            "B": ["A"],
        }
        sccs = tarjan_scc(graph)

        # Find the SCC that contains both A and B
        cycle_sccs = [s for s in sccs if len(s) >= 2]
        assert len(cycle_sccs) == 1
        assert set(cycle_sccs[0]) == {"A", "B"}

    def test_tarjan_scc_no_false_singletons(self):
        """Nodes in a cycle must NOT appear as separate singletons."""
        graph = {
            "A": ["B"],
            "B": ["A"],
            "C": [],  # True singleton
        }
        sccs = tarjan_scc(graph)

        # A and B should be in the same SCC
        for scc in sccs:
            if "A" in scc:
                assert "B" in scc, "A and B must be in the same SCC"

        # C should be a singleton
        singleton_c = [s for s in sccs if s == ["C"]]
        assert len(singleton_c) == 1

    def test_tarjan_scc_complex_graph(self):
        """Multiple SCCs in a more complex graph."""
        graph = {
            "A": ["B"],
            "B": ["C"],
            "C": ["A"],  # A-B-C cycle
            "D": ["E"],
            "E": ["D"],  # D-E cycle
            "F": [],     # singleton
        }
        sccs = tarjan_scc(graph)

        scc_sets = [set(s) for s in sccs]
        assert {"A", "B", "C"} in scc_sets
        assert {"D", "E"} in scc_sets
        assert {"F"} in scc_sets


# ===========================================================================
# AC4: handle_cycle — auto_break for false-positive shapes
# ===========================================================================

class TestHandleCycleAutoBreak:
    """AC4: auto_break for false-positive shapes."""

    def test_handle_cycle_init_false_positive_auto_breaks(self):
        """__init__ functions in a cycle → auto_break."""
        scc = ["mod_a::MyClass.__init__", "mod_a::OtherClass.__init__"]
        all_functions = {
            "mod_a::MyClass.__init__": FunctionMeta(
                module="mod_a", name="MyClass.__init__",
                qualified_name="mod_a::MyClass.__init__",
                lineno=1, end_lineno=5, calls=["OtherClass.__init__"],
            ),
            "mod_a::OtherClass.__init__": FunctionMeta(
                module="mod_a", name="OtherClass.__init__",
                qualified_name="mod_a::OtherClass.__init__",
                lineno=10, end_lineno=15, calls=["MyClass.__init__"],
            ),
        }
        edges = {
            "mod_a::MyClass.__init__": ["mod_a::OtherClass.__init__"],
            "mod_a::OtherClass.__init__": ["mod_a::MyClass.__init__"],
        }

        decision = handle_cycle(scc, all_functions, edges)
        assert decision.action == "auto_break"
        assert "false positive" in decision.reason.lower() or "init" in decision.reason.lower()

    def test_handle_cycle_same_module_size_2_auto_breaks(self):
        """Same-module size-2 cycle → auto_break."""
        scc = ["mod_a::func1", "mod_a::func2"]
        all_functions = {
            "mod_a::func1": FunctionMeta(
                module="mod_a", name="func1",
                qualified_name="mod_a::func1",
                lineno=1, end_lineno=5, calls=["func2"],
            ),
            "mod_a::func2": FunctionMeta(
                module="mod_a", name="func2",
                qualified_name="mod_a::func2",
                lineno=10, end_lineno=15, calls=["func1"],
            ),
        }
        edges = {
            "mod_a::func1": ["mod_a::func2"],
            "mod_a::func2": ["mod_a::func1"],
        }

        decision = handle_cycle(scc, all_functions, edges)
        assert decision.action == "auto_break"
        assert "same-module" in decision.reason.lower() or "size-2" in decision.reason.lower()


# ===========================================================================
# AC5: handle_cycle — block_for_observer
# ===========================================================================

class TestHandleCycleBlock:
    """AC5: block_for_observer for cross-module or size>=3."""

    def test_handle_cycle_cross_module_blocks(self):
        """Cross-module cycle → block_for_observer."""
        scc = ["mod_a::func1", "mod_b::func2"]
        all_functions = {
            "mod_a::func1": FunctionMeta(
                module="mod_a", name="func1",
                qualified_name="mod_a::func1",
                lineno=1, end_lineno=5, calls=["func2"],
            ),
            "mod_b::func2": FunctionMeta(
                module="mod_b", name="func2",
                qualified_name="mod_b::func2",
                lineno=1, end_lineno=5, calls=["func1"],
            ),
        }
        edges = {
            "mod_a::func1": ["mod_b::func2"],
            "mod_b::func2": ["mod_a::func1"],
        }

        decision = handle_cycle(scc, all_functions, edges)
        assert decision.action == "block_for_observer"

    def test_handle_cycle_size_3_blocks(self):
        """Size-3 same-module cycle → block_for_observer."""
        scc = ["mod_a::f1", "mod_a::f2", "mod_a::f3"]
        all_functions = {
            "mod_a::f1": FunctionMeta(
                module="mod_a", name="f1",
                qualified_name="mod_a::f1",
                lineno=1, end_lineno=5, calls=["f2"],
            ),
            "mod_a::f2": FunctionMeta(
                module="mod_a", name="f2",
                qualified_name="mod_a::f2",
                lineno=10, end_lineno=15, calls=["f3"],
            ),
            "mod_a::f3": FunctionMeta(
                module="mod_a", name="f3",
                qualified_name="mod_a::f3",
                lineno=20, end_lineno=25, calls=["f1"],
            ),
        }
        edges = {
            "mod_a::f1": ["mod_a::f2"],
            "mod_a::f2": ["mod_a::f3"],
            "mod_a::f3": ["mod_a::f1"],
        }

        decision = handle_cycle(scc, all_functions, edges)
        assert decision.action == "block_for_observer"
