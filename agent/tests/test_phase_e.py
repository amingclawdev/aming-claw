"""Tests for Phase E — reverse fuzzy matcher (AC2.1–AC2.4)."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Stub graph + context so we don't need networkx / real project loading
# ---------------------------------------------------------------------------

class _StubGraph:
    """Minimal AcceptanceGraph stand-in."""

    def __init__(self, nodes: dict):
        self._nodes = nodes  # {nid: {title, primary, secondary, test, ...}}

    def list_nodes(self):
        return list(self._nodes)

    def get_node(self, nid):
        return dict(self._nodes[nid])

    def update_node_attrs(self, nid, attrs):
        for k, v in attrs.items():
            self._nodes[nid][k] = v


class _StubCtx:
    """Minimal ReconcileContext stand-in."""

    def __init__(self, project_id, graph, unmapped_files):
        self.project_id = project_id
        self.graph = graph
        self._unmapped_files = unmapped_files


def _build_fixture():
    """Seed graph with nodes that cover typical test-file matching."""
    nodes = {
        "L7.1": {
            "title": "Executor lifecycle monitor",
            "primary": ["agent/governance/executor.py", "agent/tests/conftest_executor.py"],
            "secondary": [],
            "test": [],
        },
        "L7.2": {
            "title": "Chain context event sourcing",
            "primary": ["agent/governance/chain_context.py", "agent/tests/conftest_chain.py"],
            "secondary": [],
            "test": [],
        },
        "L7.3": {
            "title": "Reconcile comprehensive phases",
            "primary": ["agent/governance/reconcile.py", "agent/tests/conftest_reconcile.py"],
            "secondary": [],
            "test": [],
        },
        "L7.4": {
            "title": "Memory backend local docker",
            "primary": ["agent/governance/memory_backend.py", "agent/tests/conftest_memory.py"],
            "secondary": [],
            "test": [],
        },
        "L7.5": {
            "title": "Conflict rules engine",
            "primary": ["agent/governance/conflict_rules.py", "agent/tests/conftest_conflict.py"],
            "secondary": [],
            "test": [],
        },
        "L7.6": {
            "title": "Preflight self check",
            "primary": ["agent/governance/preflight.py", "agent/tests/conftest_preflight.py"],
            "secondary": [],
            "test": [],
        },
        "L7.7": {
            "title": "Deploy chain finalize connection release",
            "primary": ["agent/deploy_chain.py", "agent/tests/conftest_deploy.py"],
            "secondary": [],
            "test": [],
        },
        "L7.8": {
            "title": "Graph acceptance DAG manager",
            "primary": ["agent/governance/graph.py", "agent/tests/conftest_graph.py"],
            "secondary": [],
            "test": [],
        },
    }
    # 8 test files that should match via same_dir + stem scoring
    test_files = [
        "agent/tests/test_finalize_chain_conn_release.py",
        "agent/tests/test_executor.py",
        "agent/tests/test_chain_context.py",
        "agent/tests/test_reconcile.py",
        "agent/tests/test_memory_backend.py",
        "agent/tests/test_conflict_rules.py",
        "agent/tests/test_preflight.py",
        "agent/tests/test_graph.py",
    ]
    return nodes, test_files


def _make_phase_a_discrepancies(unmapped_files):
    """Create Discrepancy objects matching Phase A output."""
    from agent.governance.reconcile_phases import Discrepancy
    return [
        Discrepancy(
            type="unmapped_file", node_id=None, field=None,
            detail=f, confidence="low",
        )
        for f in unmapped_files
    ]


# ---------------------------------------------------------------------------
# AC2.1 — high-conf binding for >= 6 of 8 test files
# ---------------------------------------------------------------------------

class TestAC21HighConf:
    def test_high_conf_for_majority(self):
        from agent.governance.reconcile_phases import phase_e, Discrepancy

        nodes, test_files = _build_fixture()
        graph = _StubGraph(nodes)

        # Patch phase_a.run to return our unmapped files
        fake_discrepancies = _make_phase_a_discrepancies(test_files)

        ctx = _StubCtx("aming-claw", graph, test_files)
        results = phase_e.run(ctx, _phase_a_fn=lambda c: fake_discrepancies)

        high = [r for r in results if r.type == "unmapped_high_conf_suggest"]
        assert len(high) >= 6, f"Expected >= 6 high-conf, got {len(high)}: {results}"


# ---------------------------------------------------------------------------
# AC2.2 — medium-conf when gap < GAP_THRESHOLD
# ---------------------------------------------------------------------------

class TestAC22MediumConf:
    def test_ambiguous_produces_medium(self):
        from agent.governance.reconcile_phases import phase_e, Discrepancy

        # Two nodes with identical scoring signals for the same file
        nodes = {
            "L9.1": {
                "title": "Alpha handler",
                "primary": ["agent/governance/alpha.py"],
                "secondary": [], "test": [],
            },
            "L9.2": {
                "title": "Alpha utils",
                "primary": ["agent/governance/alpha_utils.py"],
                "secondary": [], "test": [],
            },
        }
        # A source file in the same dir — both nodes get same_dir bonus
        ambig_file = "agent/governance/alpha_new.py"
        graph = _StubGraph(nodes)
        fake_disc = _make_phase_a_discrepancies([ambig_file])

        ctx = _StubCtx("aming-claw", graph, [ambig_file])
        results = phase_e.run(ctx, _phase_a_fn=lambda c: fake_disc)

        types = [r.type for r in results]
        assert "unmapped_high_conf_suggest" not in types, \
            "Should NOT be high-conf when two nodes tie"
        # Should be medium (both score 0.4+0.2=0.6 from same_dir + keyword 'alpha')
        medium = [r for r in results if r.type == "unmapped_medium_conf_suggest"]
        assert len(medium) == 1


# ---------------------------------------------------------------------------
# AC2.3 — mutation uses /api/wf/{pid}/node-update ONLY
# ---------------------------------------------------------------------------

class TestAC23ApiOnly:
    def test_apply_uses_node_update_api(self):
        from agent.governance.reconcile_phases import phase_e, Discrepancy

        nodes, test_files = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, test_files)

        # Build a fake high-conf discrepancy
        disc = Discrepancy(
            type="unmapped_high_conf_suggest",
            node_id="L7.1",
            field="test",
            detail="file=agent/tests/test_executor.py suggested_node=L7.1 field=test score=0.90 top2=0.40 gap=0.50",
            confidence="high",
        )

        mock_post = MagicMock(return_value=MagicMock(status_code=200))
        mutations = phase_e.apply_phase_e_mutations(
            ctx, [disc], threshold="high", dry_run=False,
            _post_fn=mock_post,
        )

        assert len(mutations) == 1
        assert mutations[0]["status"] == "applied"
        # Verify the URL is node-update
        called_url = mock_post.call_args[0][0]
        assert "/api/wf/aming-claw/node-update" in called_url


# ---------------------------------------------------------------------------
# AC2.4 — after apply, node attrs include bound file
# ---------------------------------------------------------------------------

class TestAC24ApplyBindsFile:
    def test_node_receives_file(self):
        from agent.governance.reconcile_phases import phase_e, Discrepancy

        nodes, _ = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, [])

        disc = Discrepancy(
            type="unmapped_high_conf_suggest",
            node_id="L7.2",
            field="test",
            detail="file=agent/tests/test_chain_context.py suggested_node=L7.2 field=test score=0.90 top2=0.40 gap=0.50",
            confidence="high",
        )

        mock_post = MagicMock(return_value=MagicMock(status_code=200))
        phase_e.apply_phase_e_mutations(
            ctx, [disc], threshold="high", dry_run=False,
            _post_fn=mock_post,
        )

        updated = graph.get_node("L7.2")
        assert "agent/tests/test_chain_context.py" in updated["test"]
