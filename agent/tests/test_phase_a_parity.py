"""Tests for Phase A parity with reconcile.phase_diff (AC1.1, AC1.3, AC2.1-AC2.4)."""
from __future__ import annotations

import types
from dataclasses import asdict, fields
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AC2.1: import succeeds
# ---------------------------------------------------------------------------

def test_import_reconcile_phases():
    from agent.governance.reconcile_phases import (
        ReconcileContext, PhaseBase, phase_a, Discrepancy,
    )
    assert ReconcileContext is not None
    assert PhaseBase is not None
    assert phase_a is not None
    assert Discrepancy is not None


# ---------------------------------------------------------------------------
# AC2.2: Discrepancy fields
# ---------------------------------------------------------------------------

def test_discrepancy_fields():
    from agent.governance.reconcile_phases import Discrepancy
    names = {f.name for f in fields(Discrepancy)}
    assert names == {"type", "node_id", "field", "detail", "confidence"}
    d = Discrepancy(type="x", node_id=None, field=None, detail="d", confidence="low")
    assert d.type == "x"
    assert d.node_id is None


# ---------------------------------------------------------------------------
# AC2.3: PhaseBase ABC
# ---------------------------------------------------------------------------

def test_phase_base_abstract():
    from agent.governance.reconcile_phases import PhaseBase, ReconcileContext, Discrepancy
    import abc

    assert hasattr(PhaseBase, "run")
    assert getattr(PhaseBase.run, "__isabstractmethod__", False)

    # Concrete subclass must implement run
    class Good(PhaseBase):
        def run(self, ctx):
            return []

    g = Good()
    assert g.run(None) == []

    # Missing run should raise TypeError
    with pytest.raises(TypeError):
        class Bad(PhaseBase):
            pass
        Bad()


# ---------------------------------------------------------------------------
# AC1.1: phase_a parity with phase_diff
# ---------------------------------------------------------------------------

def _make_fake_graph(nodes: dict):
    """Build a mock AcceptanceGraph from {node_id: {primary, secondary, test}}."""
    g = MagicMock()
    g.list_nodes.return_value = list(nodes.keys())
    g.node_count.return_value = len(nodes)

    def get_node(nid):
        return nodes[nid]

    g.get_node = get_node
    return g


@pytest.fixture
def simple_scenario():
    """Graph with 2 nodes, one stale ref, one orphan, unmapped files."""
    nodes = {
        "L1.1": {
            "primary": ["agent/foo.py"],
            "secondary": ["docs/foo.md"],
            "test": [],
        },
        "L1.2": {
            "primary": ["agent/gone.py"],  # not in filesystem → orphan
            "secondary": [],
            "test": [],
        },
    }
    graph = _make_fake_graph(nodes)
    file_set = {"agent/foo.py", "agent/bar.py"}  # bar.py is unmapped
    file_metadata = {p: {"path": p, "type": "source"} for p in file_set}
    return graph, file_set, file_metadata


def test_phase_a_parity_counts(simple_scenario):
    """Phase A discrepancy counts match DiffReport field counts."""
    from agent.governance.reconcile import phase_diff
    from agent.governance.reconcile_phases import phase_a, Discrepancy
    from agent.governance.reconcile_phases.context import ReconcileContext

    graph, file_set, file_metadata = simple_scenario

    # Direct phase_diff
    diff = phase_diff(graph, file_set, file_metadata)

    # Phase A via context
    ctx = MagicMock(spec=ReconcileContext)
    ctx.graph = graph
    ctx.file_set = file_set
    ctx.file_metadata = file_metadata

    discs = phase_a.run(ctx)

    # Count by type
    counts = {}
    for d in discs:
        assert isinstance(d, Discrepancy)
        counts[d.type] = counts.get(d.type, 0) + 1

    assert counts.get("stale_ref", 0) == len(diff.stale_refs)
    assert counts.get("orphan_node", 0) == len(diff.orphan_nodes)
    assert counts.get("unmapped_file", 0) == len(diff.unmapped_files)
    assert counts.get("stale_doc_ref", 0) == len(diff.stale_doc_refs)
    assert counts.get("unmapped_doc", 0) == len(diff.unmapped_docs)


def test_phase_a_stale_ref_fields(simple_scenario):
    """Stale ref discrepancies carry node_id and field."""
    from agent.governance.reconcile import phase_diff
    from agent.governance.reconcile_phases import phase_a

    graph, file_set, file_metadata = simple_scenario
    diff = phase_diff(graph, file_set, file_metadata)

    ctx = MagicMock()
    ctx.graph = graph
    ctx.file_set = file_set
    ctx.file_metadata = file_metadata

    discs = phase_a.run(ctx)
    stale = [d for d in discs if d.type == "stale_ref"]

    for d, ref in zip(stale, diff.stale_refs):
        assert d.node_id == ref.node_id
        assert d.field == ref.field
        assert d.confidence == ref.confidence


# ---------------------------------------------------------------------------
# AC1.3: reconcile_project dry_run unchanged
# ---------------------------------------------------------------------------

def test_reconcile_dry_run_structure():
    """reconcile_project(dry_run=True) returns the same DiffReport shape."""
    from agent.governance.reconcile import reconcile_project, DiffReport
    from dataclasses import asdict, fields as dc_fields

    nodes = {
        "L1.1": {"primary": ["agent/a.py"], "secondary": [], "test": []},
    }
    graph = _make_fake_graph(nodes)
    fs = {"agent/a.py"}
    meta = {"agent/a.py": {"path": "agent/a.py", "type": "source"}}

    with patch("agent.governance.reconcile.phase_scan", return_value=(fs, meta)), \
         patch("agent.governance.reconcile.load_project_graph", return_value=graph):
        result = reconcile_project("test", ".", dry_run=True)

    assert result["dry_run"] is True
    diff = result["diff"]
    expected_keys = {f.name for f in dc_fields(DiffReport)}
    assert expected_keys.issubset(set(diff.keys()))
