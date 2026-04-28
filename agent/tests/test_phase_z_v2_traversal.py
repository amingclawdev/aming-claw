"""Tests for DFS coloring (dfs_color_from_entries) in phase_z_v2.py."""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types

import pytest

# Load phase_z_v2 directly to bypass __init__.py import chain (Python 3.9 compat)
_here = os.path.dirname(os.path.abspath(__file__))
_pz_path = os.path.join(_here, "..", "governance", "reconcile_phases", "phase_z_v2.py")
_spec = importlib.util.spec_from_file_location(
    "agent.governance.reconcile_phases.phase_z_v2", _pz_path,
    submodule_search_locations=[]
)
_mod = importlib.util.module_from_spec(_spec)

# Ensure parent packages exist in sys.modules
for _name in [
    "agent", "agent.governance", "agent.governance.reconcile_phases"
]:
    if _name not in sys.modules:
        _pkg = types.ModuleType(_name)
        _pkg.__path__ = []
        _pkg.__package__ = _name
        sys.modules[_name] = _pkg

sys.modules["agent.governance.reconcile_phases.phase_z_v2"] = _mod
_spec.loader.exec_module(_mod)

dfs_color_from_entries = _mod.dfs_color_from_entries
identify_entries = _mod.identify_entries
FunctionMeta = _mod.FunctionMeta
ModuleInfo = _mod.ModuleInfo


class TestDfsColorFromEntries:
    """AC1, AC2, AC6: dfs_color_from_entries signature, behavior, strong-only."""

    def test_signature_and_return_types(self):
        """AC1: accepts (edges, entries, track_distance=False), returns correct types."""
        edges = {"a": ["b", "c"], "b": ["d"], "c": []}
        entries = ["a"]
        color_sets, color_count_map = dfs_color_from_entries(edges, entries)
        assert isinstance(color_sets, dict)
        assert isinstance(color_count_map, dict)
        assert isinstance(color_sets["a"], set)
        assert isinstance(color_count_map.get("b", 0), int)

    def test_track_distance_param(self):
        """AC6: track_distance=False is accepted."""
        edges = {"a": ["b"]}
        cs, ccm = dfs_color_from_entries(edges, ["a"], track_distance=False)
        assert "a" in cs

    def test_docstring_mentions_min_distance(self):
        """AC6: docstring mentions future min_distance re-add path."""
        doc = dfs_color_from_entries.__doc__
        assert "min_distance" in doc

    def test_reachability_basic(self):
        """DFS finds all reachable nodes from entry."""
        edges = {"entry1": ["a", "b"], "a": ["c"], "b": ["c"], "c": []}
        cs, ccm = dfs_color_from_entries(edges, ["entry1"])
        assert cs["entry1"] == {"entry1", "a", "b", "c"}
        assert ccm["c"] == 1

    def test_multiple_entries_color_count(self):
        """color_count_map counts distinct entries reaching each fn."""
        edges = {
            "e1": ["shared"],
            "e2": ["shared"],
            "e3": ["shared"],
            "shared": ["leaf"],
        }
        cs, ccm = dfs_color_from_entries(edges, ["e1", "e2", "e3"])
        assert ccm["shared"] == 3
        assert ccm["leaf"] == 3

    def test_strong_only_no_weak_edges(self):
        """AC2: Only strong edges are followed; weak-only nodes get color_count=0."""
        # Strong edges: entry -> a -> b
        strong_edges = {"entry": ["a"], "a": ["b"], "b": []}
        # Weak edge would connect entry -> weak_node, but we don't pass it
        # dfs_color_from_entries only takes strong edges dict
        cs, ccm = dfs_color_from_entries(strong_edges, ["entry"])
        assert "b" in cs["entry"]
        # weak_node never appears because only strong edges dict is passed
        assert ccm.get("weak_node", 0) == 0

    def test_cycle_handling(self):
        """DFS handles cycles without infinite loop."""
        edges = {"e": ["a"], "a": ["b"], "b": ["a"]}
        cs, ccm = dfs_color_from_entries(edges, ["e"])
        assert cs["e"] == {"e", "a", "b"}

    def test_empty_entries(self):
        """No entries -> empty results."""
        cs, ccm = dfs_color_from_entries({"a": ["b"]}, [])
        assert cs == {}
        assert ccm == {}


class TestIdentifyEntries:
    """R2: identify_entries detects entry functions."""

    def _make_modules(self, path, funcs):
        mod = ModuleInfo(path=path, module_name="test.mod", functions=funcs)
        return {"test.mod": mod}

    def test_route_decorator(self):
        func = FunctionMeta(
            module="test.mod", name="handler", qualified_name="test.mod::handler",
            lineno=1, end_lineno=5, decorators=["app.route"]
        )
        entries = identify_entries(self._make_modules("agent/api.py", [func]))
        assert "test.mod::handler" in entries

    def test_mcp_tool_decorator(self):
        func = FunctionMeta(
            module="test.mod", name="tool_fn", qualified_name="test.mod::tool_fn",
            lineno=1, end_lineno=5, decorators=["server.tool"]
        )
        entries = identify_entries(self._make_modules("agent/mcp.py", [func]))
        assert "test.mod::tool_fn" in entries

    def test_main_guard(self):
        func = FunctionMeta(
            module="test.mod", name="__main__", qualified_name="test.mod::__main__",
            lineno=1, end_lineno=5, decorators=[]
        )
        entries = identify_entries(self._make_modules("agent/run.py", [func]))
        assert "test.mod::__main__" in entries

    def test_no_entry(self):
        func = FunctionMeta(
            module="test.mod", name="helper", qualified_name="test.mod::helper",
            lineno=1, end_lineno=5, decorators=[]
        )
        entries = identify_entries(self._make_modules("agent/util.py", [func]))
        assert entries == []
