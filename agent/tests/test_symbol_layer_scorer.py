"""Tests for symbol_layer_scorer.py — 6-signal composite scorer."""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types

import pytest

# Load phase_z_v2 + symbol_layer_scorer directly (Python 3.9 compat)
_here = os.path.dirname(os.path.abspath(__file__))
_gov = os.path.join(_here, "..", "governance")

# Ensure parent packages exist in sys.modules
for _name in [
    "agent", "agent.governance", "agent.governance.reconcile_phases"
]:
    if _name not in sys.modules:
        _pkg = types.ModuleType(_name)
        _pkg.__path__ = []
        _pkg.__package__ = _name
        sys.modules[_name] = _pkg

# Load phase_z_v2
_pz_path = os.path.join(_gov, "reconcile_phases", "phase_z_v2.py")
_pz_spec = importlib.util.spec_from_file_location(
    "agent.governance.reconcile_phases.phase_z_v2", _pz_path,
    submodule_search_locations=[]
)
_pz_mod = importlib.util.module_from_spec(_pz_spec)
sys.modules["agent.governance.reconcile_phases.phase_z_v2"] = _pz_mod
_pz_spec.loader.exec_module(_pz_mod)

# Load symbol_layer_scorer
_sls_path = os.path.join(_gov, "symbol_layer_scorer.py")
_sls_spec = importlib.util.spec_from_file_location(
    "agent.governance.symbol_layer_scorer", _sls_path,
    submodule_search_locations=[]
)
_sls_mod = importlib.util.module_from_spec(_sls_spec)
sys.modules["agent.governance.symbol_layer_scorer"] = _sls_mod
_sls_spec.loader.exec_module(_sls_mod)

CallGraph = _pz_mod.CallGraph
FunctionMeta = _pz_mod.FunctionMeta
ModuleInfo = _pz_mod.ModuleInfo
DEFAULT_WEIGHTS = _sls_mod.DEFAULT_WEIGHTS
compute_calibration = _sls_mod.compute_calibration
score_function_layer = _sls_mod.score_function_layer


class TestDefaultWeights:
    """AC3: DEFAULT_WEIGHTS has exactly 6 keys with correct values."""

    def test_six_keys(self):
        assert len(DEFAULT_WEIGHTS) == 6

    def test_key_names(self):
        expected = {"in_degree", "color_count", "fan_out", "import_depth",
                    "path_hint", "entry_signal"}
        assert set(DEFAULT_WEIGHTS.keys()) == expected

    def test_values(self):
        assert DEFAULT_WEIGHTS["in_degree"] == 0.35
        assert DEFAULT_WEIGHTS["color_count"] == 0.30
        assert DEFAULT_WEIGHTS["fan_out"] == 0.15
        assert DEFAULT_WEIGHTS["import_depth"] == 0.10
        assert DEFAULT_WEIGHTS["path_hint"] == 0.10
        assert DEFAULT_WEIGHTS["entry_signal"] == 0.10

    def test_sum(self):
        total = sum(DEFAULT_WEIGHTS.values())
        assert abs(total - 1.10) < 0.001


class TestComputeCalibration:
    """R6: compute_calibration with optional color_count_map."""

    def test_backward_compat_no_color_count(self):
        """Works without color_count_map parameter."""
        cg = CallGraph(edges={"a": ["b"]}, all_functions={})
        mods = [ModuleInfo(path="x.py", module_name="x")]
        cal = compute_calibration(cg, mods)
        assert cal["max_color_count"] == 0
        assert cal["color_count_map"] == {}

    def test_with_color_count_map(self):
        cg = CallGraph(edges={"a": ["b"]}, all_functions={})
        mods = [ModuleInfo(path="x.py", module_name="x")]
        ccm = {"b": 5, "a": 3}
        cal = compute_calibration(cg, mods, color_count_map=ccm)
        assert cal["max_color_count"] == 5
        assert cal["color_count_map"] == ccm


class TestScoreFunctionLayer:
    """AC4, AC5: score_function_layer with color_count signal."""

    def _make_calibration(self, color_count_map=None):
        ccm = color_count_map or {}
        return {
            "top_5pct_in_deg": 10,
            "max_in_deg": 20,
            "max_fan_out": 10,
            "max_import_depth": 5,
            "in_degree_counts": {"shared_util": 15, "foundation": 15},
            "color_count_map": ccm,
            "max_color_count": max(ccm.values()) if ccm else 0,
        }

    def test_high_color_count_not_l0(self):
        """AC4: function reachable from 5+ entries with high in_degree -> NOT L0."""
        ccm = {"shared_util": 8}
        cal = self._make_calibration(ccm)
        info = FunctionMeta(
            module="agent.utils", name="shared_util",
            qualified_name="agent.utils::shared_util",
            lineno=1, end_lineno=5, calls=["x", "y"],  # fan_out=2 (<=3)
        )
        result = score_function_layer("shared_util", info, cal)
        # High color_count (8/8=1.0) should prevent L0
        assert result["layer"] != "L0"

    def test_signals_contains_color_count(self):
        """AC5: return dict includes signals with color_count float."""
        ccm = {"fn": 3}
        cal = self._make_calibration(ccm)
        info = FunctionMeta(
            module="agent.mod", name="fn", qualified_name="agent.mod::fn",
            lineno=1, end_lineno=5, calls=[],
        )
        result = score_function_layer("fn", info, cal)
        assert "signals" in result
        assert "color_count" in result["signals"]
        assert isinstance(result["signals"]["color_count"], float)

    def test_backward_compat_no_color_count_in_calibration(self):
        """AC5/R8: works when color_count_map absent from calibration."""
        cal = {
            "top_5pct_in_deg": 10,
            "max_in_deg": 20,
            "max_fan_out": 10,
            "max_import_depth": 5,
            "in_degree_counts": {},
        }
        info = FunctionMeta(
            module="agent.mod", name="fn", qualified_name="agent.mod::fn",
            lineno=1, end_lineno=5, calls=[],
        )
        result = score_function_layer("fn", info, cal)
        assert result["signals"]["color_count"] == 0.0
        assert "layer" in result

    def test_result_has_candidate_and_score(self):
        """AC5: candidate and score fields remain populated."""
        cal = self._make_calibration({})
        info = FunctionMeta(
            module="agent.mod", name="fn", qualified_name="agent.mod::fn",
            lineno=1, end_lineno=5, calls=[],
        )
        result = score_function_layer("fn", info, cal)
        assert "candidate" in result
        assert "score" in result

    def test_high_color_high_indeg_is_l1_or_higher(self):
        """AC4: synthetic — fn reachable from 5+ entries, high in_degree -> L1+."""
        ccm = {"util_fn": 7}
        cal = {
            "top_5pct_in_deg": 10,
            "max_in_deg": 20,
            "max_fan_out": 10,
            "max_import_depth": 5,
            "in_degree_counts": {"util_fn": 18},
            "color_count_map": ccm,
            "max_color_count": 7,
        }
        info = FunctionMeta(
            module="agent.utils", name="util_fn",
            qualified_name="agent.utils::util_fn",
            lineno=1, end_lineno=5, calls=["a"],  # fan_out=1 (<=3)
        )
        result = score_function_layer("util_fn", info, cal)
        layer_num = int(result["layer"][1:])
        assert layer_num >= 1, f"Expected L1+, got {result['layer']}"
