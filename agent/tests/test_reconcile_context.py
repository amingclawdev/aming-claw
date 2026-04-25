"""Tests for ReconcileContext caching (AC1.2)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def test_context_scan_called_once():
    """AC1.2: phase_scan is invoked exactly once even when accessed multiple times."""
    from agent.governance.reconcile_phases.context import ReconcileContext
    from agent.governance.reconcile_phases import phase_a

    fake_files = {"agent/x.py"}
    fake_meta = {"agent/x.py": {"path": "agent/x.py", "type": "source"}}
    fake_graph = MagicMock()
    fake_graph.list_nodes.return_value = []
    fake_graph.node_count.return_value = 0

    with patch("agent.governance.reconcile_phases.context.phase_scan",
               return_value=(fake_files, fake_meta)) as mock_scan, \
         patch("agent.governance.reconcile_phases.context.load_project_graph",
               return_value=fake_graph):

        ctx = ReconcileContext("test", "/tmp/ws")

        # First access
        _ = ctx.file_set
        _ = ctx.file_metadata
        # Second access (should be cached)
        _ = ctx.file_set
        _ = ctx.file_metadata

        assert mock_scan.call_count == 1


def test_context_scan_once_across_phase_runs():
    """AC1.2: run phase_a twice on same Context, scan only once."""
    from agent.governance.reconcile_phases.context import ReconcileContext
    from agent.governance.reconcile_phases import phase_a

    fake_files = {"agent/y.py"}
    fake_meta = {"agent/y.py": {"path": "agent/y.py", "type": "source"}}
    fake_graph = MagicMock()
    fake_graph.list_nodes.return_value = ["L1.1"]
    fake_graph.node_count.return_value = 1
    fake_graph.get_node.return_value = {
        "primary": ["agent/y.py"], "secondary": [], "test": [],
    }

    with patch("agent.governance.reconcile_phases.context.phase_scan",
               return_value=(fake_files, fake_meta)) as mock_scan, \
         patch("agent.governance.reconcile_phases.context.load_project_graph",
               return_value=fake_graph):

        ctx = ReconcileContext("test", "/tmp/ws")
        phase_a.run(ctx)
        phase_a.run(ctx)

        assert mock_scan.call_count == 1


def test_context_graph_loaded_once():
    """Graph is loaded once via cached_property."""
    from agent.governance.reconcile_phases.context import ReconcileContext

    with patch("agent.governance.reconcile_phases.context.phase_scan",
               return_value=(set(), {})), \
         patch("agent.governance.reconcile_phases.context.load_project_graph",
               return_value=MagicMock()) as mock_load:

        ctx = ReconcileContext("test", "/tmp/ws")
        _ = ctx.graph
        _ = ctx.graph

        assert mock_load.call_count == 1
