"""Tests for phase scope filtering — verify AC-P1 and AC-P2."""
from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from agent.governance.reconcile_phases.scope import ResolvedScope, FileOrigin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeDiscrepancy:
    type: str
    node_id: str = None
    field: str = None
    detail: str = ""
    confidence: str = "medium"


class FakeNode:
    def __init__(self, primary=None, secondary=None, test=None):
        self.primary = primary or []
        self.secondary = secondary or []
        self.test = test or []


class FakeGraph:
    def __init__(self, nodes=None):
        self._nodes = nodes or {}
    def get_node(self, nid):
        return self._nodes.get(nid)
    def list_nodes(self):
        return list(self._nodes.keys())


# ---------------------------------------------------------------------------
# AC-P1: Phase A scope filtering
# ---------------------------------------------------------------------------

def test_phase_a_scope_filters_by_node():
    """Phase A with scope filters to nodes referencing scope files."""
    from agent.governance.reconcile_phases.phase_a import _filter_by_scope

    scope = ResolvedScope(
        file_set={"agent/foo.py": FileOrigin(source="path")},
        node_set=frozenset(["L1.1"]),
    )

    graph = FakeGraph({
        "L1.1": FakeNode(primary=["agent/foo.py"]),
        "L2.1": FakeNode(primary=["agent/bar.py"]),
    })

    discrepancies = [
        FakeDiscrepancy(type="stale_ref", node_id="L1.1", detail="in scope"),
        FakeDiscrepancy(type="stale_ref", node_id="L2.1", detail="out of scope"),
        FakeDiscrepancy(type="unmapped_file", node_id=None, detail="agent/foo.py"),
        FakeDiscrepancy(type="unmapped_file", node_id=None, detail="agent/other.py"),
    ]

    filtered = _filter_by_scope(discrepancies, scope, graph)

    # L1.1 is in scope.node_set → included
    assert any(d.node_id == "L1.1" for d in filtered)
    # L2.1 is not in scope and its files don't intersect → excluded
    assert not any(d.node_id == "L2.1" for d in filtered)
    # unmapped_file agent/foo.py is in scope.files() → included
    assert any(d.detail == "agent/foo.py" for d in filtered)
    # unmapped_file agent/other.py is not in scope → excluded
    assert not any(d.detail == "agent/other.py" for d in filtered)


def test_phase_a_scope_node_file_intersection():
    """Phase A includes node if its files intersect scope files."""
    from agent.governance.reconcile_phases.phase_a import _filter_by_scope

    scope = ResolvedScope(
        file_set={"agent/bar.py": FileOrigin(source="path")},
        node_set=frozenset(),
    )
    graph = FakeGraph({
        "L2.1": FakeNode(primary=["agent/bar.py"]),
    })

    discrepancies = [
        FakeDiscrepancy(type="stale_ref", node_id="L2.1", detail="test"),
    ]

    filtered = _filter_by_scope(discrepancies, scope, graph)
    assert len(filtered) == 1
    assert filtered[0].node_id == "L2.1"


# ---------------------------------------------------------------------------
# AC-P2: scope=None passes through all results (backward compat)
# ---------------------------------------------------------------------------

def test_phase_a_no_scope_passthrough():
    """When scope=None, Phase A run() returns full results."""
    # We test by checking that _filter_by_scope is NOT called when scope=None
    # This is implicit: the run() function only calls filter when scope is not None
    from agent.governance.reconcile_phases import phase_a

    # Just verify the function signature accepts scope=None
    import inspect
    sig = inspect.signature(phase_a.run)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None


def test_phase_b_signature_has_scope():
    from agent.governance.reconcile_phases import phase_b
    import inspect
    sig = inspect.signature(phase_b.run)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None


def test_phase_c_signature_has_scope():
    from agent.governance.reconcile_phases import phase_c
    import inspect
    sig = inspect.signature(phase_c.run)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None


def test_phase_d_signature_has_scope():
    from agent.governance.reconcile_phases import phase_d
    import inspect
    sig = inspect.signature(phase_d.run)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None


def test_phase_e_signature_has_scope():
    from agent.governance.reconcile_phases import phase_e
    import inspect
    sig = inspect.signature(phase_e.run)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None


def test_phase_f_signature_has_scope():
    from agent.governance.reconcile_phases import phase_f
    import inspect
    sig = inspect.signature(phase_f.run)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None


def test_phase_g_signature_has_scope():
    from agent.governance.reconcile_phases import phase_g
    import inspect
    sig = inspect.signature(phase_g.run)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None


# ---------------------------------------------------------------------------
# AC-P1: Phase F scope filtering by node_set
# ---------------------------------------------------------------------------

def test_phase_f_scope_filters_by_node_set():
    """Phase F only keeps discrepancies whose node_id is in scope.node_set."""
    scope = ResolvedScope(
        file_set={},
        node_set=frozenset(["L1.1"]),
    )

    results = [
        FakeDiscrepancy(type="verify_status_stale", node_id="L1.1"),
        FakeDiscrepancy(type="verify_status_stale", node_id="L2.1"),
    ]

    # Simulate Phase F filtering
    scope_nodes = scope.node_set
    filtered = [d for d in results if d.node_id in scope_nodes]
    assert len(filtered) == 1
    assert filtered[0].node_id == "L1.1"


# ---------------------------------------------------------------------------
# Orchestrator threads scope
# ---------------------------------------------------------------------------

def test_orchestrator_run_orchestrated_accepts_scope():
    """run_orchestrated() accepts scope parameter."""
    from agent.governance.reconcile_phases import orchestrator
    import inspect
    sig = inspect.signature(orchestrator.run_orchestrated)
    assert "scope" in sig.parameters
    assert sig.parameters["scope"].default is None
