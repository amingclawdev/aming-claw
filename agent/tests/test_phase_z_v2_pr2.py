"""Tests for Phase Z v2 PR2 — layer scorer + node aggregator.

6 scorer tests + 6 aggregator tests = 12 total.
"""
from __future__ import annotations

import os
import tempfile
from typing import Dict

import pytest

from agent.governance.reconcile_phases.phase_z_v2 import (
    CallGraph,
    FunctionMeta,
    ModuleInfo,
)
from agent.governance.symbol_layer_scorer import (
    DEFAULT_WEIGHTS,
    FOUNDATION_MIN_IN_DEG,
    FOUNDATION_TOP_PCT,
    compute_calibration,
    is_entrypoint,
    normalize,
    path_to_layer_hint,
    score_function_layer,
)
from agent.governance.symbol_node_aggregator import (
    SPLIT_MIN_FUNCTIONS,
    SPLIT_MIN_LAYER_TIERS,
    aggregate_functions_into_nodes,
    determine_dominant_layer,
    parse_region_hints,
    split_by_connected_components,
)


# ===================================================================
# Scorer tests (6)
# ===================================================================

class TestDefaultWeightsAndConstants:
    """AC1 + AC2: Verify constants are grep-verifiable."""

    def test_default_weights(self):
        assert set(DEFAULT_WEIGHTS.keys()) == {
            "in_degree", "color_count", "fan_out", "import_depth",
            "path_hint", "entry_signal"
        }
        assert DEFAULT_WEIGHTS["in_degree"] == 0.35
        assert DEFAULT_WEIGHTS["color_count"] == 0.30
        assert DEFAULT_WEIGHTS["fan_out"] == 0.15
        assert DEFAULT_WEIGHTS["import_depth"] == 0.10
        assert DEFAULT_WEIGHTS["path_hint"] == 0.10
        assert DEFAULT_WEIGHTS["entry_signal"] == 0.10
        # The multi-signal scorer intentionally keeps an entry-point boost
        # on top of the normalized structural weights.
        assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.10) < 1e-9

    def test_foundation_constants(self):
        assert FOUNDATION_TOP_PCT == 0.05
        assert FOUNDATION_MIN_IN_DEG == 10


class TestIsEntrypoint:
    """AC3: Entry-point detection."""

    def test_route_decorator(self):
        info = FunctionMeta(
            module="app", name="index", qualified_name="app::index",
            lineno=1, end_lineno=5, decorators=["app.route"]
        )
        assert is_entrypoint("app::index", info) is True

    def test_cli_decorator(self):
        info = FunctionMeta(
            module="cli", name="run", qualified_name="cli::run",
            lineno=1, end_lineno=5, decorators=["click.cli"]
        )
        assert is_entrypoint("cli::run", info) is True

    def test_main_guard(self):
        info = FunctionMeta(
            module="app", name="main", qualified_name="app::__main__",
            lineno=1, end_lineno=5
        )
        assert is_entrypoint("app::__main__", info) is True

    def test_mcp_handler(self):
        info = FunctionMeta(
            module="mcp", name="handle", qualified_name="mcp::handle",
            lineno=1, end_lineno=5, decorators=["server.tool"]
        )
        assert is_entrypoint("mcp::handle", info) is True

    def test_plain_function_not_entry(self):
        info = FunctionMeta(
            module="utils", name="helper", qualified_name="utils::helper",
            lineno=1, end_lineno=5
        )
        assert is_entrypoint("utils::helper", info) is False


class TestPathHint:
    """AC4: Path-based layer hints."""

    def test_governance_path(self):
        assert path_to_layer_hint("agent/governance/db.py") == 0.5

    def test_agent_path(self):
        assert path_to_layer_hint("agent/models.py") == 0.4

    def test_scripts_path(self):
        assert path_to_layer_hint("scripts/deploy.py") == 0.7


class TestNormalize:
    """AC covers normalize utility."""

    def test_basic(self):
        assert normalize(5, 10) == 0.5

    def test_inverse(self):
        assert normalize(0, 10, inverse=True) == 1.0
        assert normalize(10, 10, inverse=True) == 0.0

    def test_zero_max(self):
        assert normalize(5, 0) == 0.0
        assert normalize(5, 0, inverse=True) == 1.0


class TestScoreFunctionLayer:
    """AC2: Foundation logic + composite scoring."""

    def test_foundation_candidate_requires_both(self):
        """Foundation requires top 5% AND in_deg >= 10."""
        info = FunctionMeta(
            module="agent.governance.db", name="get_conn",
            qualified_name="agent.governance.db::get_conn",
            lineno=1, end_lineno=5, calls=["a", "b"]  # fan_out=2
        )
        cal = {
            "top_5pct_in_deg": 8,
            "max_in_deg": 50,
            "max_fan_out": 20,
            "max_import_depth": 5,
            "in_degree_counts": {"agent.governance.db::get_conn": 15},
        }
        result = score_function_layer(
            "agent.governance.db::get_conn", info, cal
        )
        assert result["candidate"] == "foundation"
        assert result["layer"] == "L0"

    def test_foundation_rejected_low_in_deg(self):
        """Even if top 5%, must have >= 10 in-degree."""
        info = FunctionMeta(
            module="agent.utils", name="helper",
            qualified_name="agent.utils::helper",
            lineno=1, end_lineno=5, calls=["a"]
        )
        cal = {
            "top_5pct_in_deg": 3,
            "max_in_deg": 10,
            "max_fan_out": 20,
            "max_import_depth": 5,
            "in_degree_counts": {"agent.utils::helper": 5},
        }
        result = score_function_layer(
            "agent.utils::helper", info, cal
        )
        # in_deg=5 < FOUNDATION_MIN_IN_DEG=10 → not foundation
        assert result["candidate"] != "foundation"


class TestComputeCalibration:
    """AC covers calibration computation."""

    def test_basic_calibration(self):
        cg = CallGraph(
            edges={
                "a::f1": ["a::f2", "a::f3"],
                "a::f2": ["a::f3"],
            },
            all_functions={},
        )
        modules = [
            ModuleInfo(path="a.py", module_name="a"),
            ModuleInfo(path="b.py", module_name="b.c.d"),
        ]
        cal = compute_calibration(cg, modules)
        assert cal["max_fan_out"] == 2
        assert cal["max_import_depth"] == 3  # b.c.d has depth 3
        assert cal["in_degree_counts"]["a::f3"] == 2


# ===================================================================
# Aggregator tests (6)
# ===================================================================

class TestAggregatorConstants:
    """AC5: Split thresholds."""

    def test_constants(self):
        assert SPLIT_MIN_FUNCTIONS == 50
        assert SPLIT_MIN_LAYER_TIERS == 3


class TestDetermineDominantLayer:
    """AC7: Mode layer + outliers."""

    def test_mode_and_outliers(self):
        layers = {
            "a::f1": {"layer": "L2"},
            "a::f2": {"layer": "L2"},
            "a::f3": {"layer": "L4"},
        }
        dominant, outliers = determine_dominant_layer(
            ["a::f1", "a::f2", "a::f3"], layers
        )
        assert dominant == "L2"
        assert outliers == ["a::f3"]


class TestParseRegionHints:
    """AC6: Region hint parsing."""

    def test_region_parsing(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("# region: Database\n")
            f.write("def connect(): pass\n")
            f.write("# endregion\n")
            f.write("# region: API\n")
            f.write("def serve(): pass\n")
            f.write("# endregion\n")
            f.flush()
            path = f.name

        try:
            regions = parse_region_hints(path)
            assert "Database" in regions
            assert "API" in regions
            assert len(regions) == 2
        finally:
            os.unlink(path)


class TestSplitByConnectedComponents:
    """AC covers CC splitting within a file."""

    def test_two_components(self):
        cg = CallGraph(
            edges={"a::f1": ["a::f2"], "a::f3": ["a::f4"]},
            all_functions={},
        )
        funcs = ["a::f1", "a::f2", "a::f3", "a::f4"]
        comps = split_by_connected_components(funcs, cg)
        assert len(comps) == 2
        flat = [f for c in comps for f in c]
        assert set(flat) == set(funcs)


class TestAggregateDefault:
    """AC5 + AC7: Default 1 node per file, no split."""

    def test_single_node_per_file(self):
        mod = ModuleInfo(
            path="agent/foo.py",
            module_name="agent.foo",
            functions=[
                FunctionMeta(
                    module="agent.foo", name=f"f{i}",
                    qualified_name=f"agent.foo::f{i}",
                    lineno=i * 10, end_lineno=i * 10 + 5,
                )
                for i in range(5)
            ],
        )
        layers = {
            f"agent.foo::f{i}": {"layer": "L2"} for i in range(5)
        }
        cg = CallGraph(edges={}, all_functions={})
        nodes = aggregate_functions_into_nodes([mod], layers, cg)
        assert len(nodes) == 1
        node = nodes[0]
        assert node["function_count"] == 5
        assert node["dominant_layer"] == "L2"
        assert node["outlier_functions"] == []
        assert node["split_reason"] is None
        assert "node_id_proposed" in node
        assert "primary" in node


class TestAggregateSplit:
    """AC5: Split triggers only when BOTH thresholds met."""

    def test_no_split_below_thresholds(self):
        """49 functions + 3 tiers → no split (below func threshold)."""
        funcs = [
            FunctionMeta(
                module="big", name=f"f{i}",
                qualified_name=f"big::f{i}",
                lineno=i, end_lineno=i + 1,
            )
            for i in range(49)
        ]
        mod = ModuleInfo(path="big.py", module_name="big", functions=funcs)
        layers = {}
        for i in range(49):
            tier = ["L1", "L3", "L5"][i % 3]
            layers[f"big::f{i}"] = {"layer": tier}
        cg = CallGraph(edges={}, all_functions={})
        nodes = aggregate_functions_into_nodes([mod], layers, cg)
        assert len(nodes) == 1
        assert nodes[0]["split_reason"] is None

    def test_split_when_both_met(self):
        """50 functions + 3 tiers → split."""
        funcs = [
            FunctionMeta(
                module="big", name=f"f{i}",
                qualified_name=f"big::f{i}",
                lineno=i + 1, end_lineno=i + 2,
            )
            for i in range(50)
        ]
        mod = ModuleInfo(path="big.py", module_name="big", functions=funcs)
        layers = {}
        for i in range(50):
            tier = ["L1", "L3", "L5"][i % 3]
            layers[f"big::f{i}"] = {"layer": tier}
        # No region hints → CC splitting
        cg = CallGraph(edges={}, all_functions={})
        nodes = aggregate_functions_into_nodes([mod], layers, cg)
        # Each function is its own CC (no edges), so many nodes
        assert len(nodes) > 1
        for n in nodes:
            assert n["split_reason"] == "connected_component"
            assert "dominant_layer" in n
            assert "outlier_functions" in n
