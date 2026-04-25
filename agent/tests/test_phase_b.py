"""Tests for Phase B — PM proposed_nodes reconcile (AC3.1–AC3.4)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub graph + context (same pattern as test_phase_e.py)
# ---------------------------------------------------------------------------

class _StubGraph:
    """Minimal AcceptanceGraph stand-in."""

    def __init__(self, nodes):
        self._nodes = dict(nodes)

    def list_nodes(self):
        return list(self._nodes)

    def get_node(self, nid):
        return dict(self._nodes[nid])

    def update_node_attrs(self, nid, attrs):
        for k, v in attrs.items():
            self._nodes[nid][k] = v


class _StubCtx:
    """Minimal ReconcileContext stand-in with pm_events."""

    def __init__(self, project_id, graph, pm_events=None):
        self.project_id = project_id
        self.graph = graph
        self.pm_events = pm_events or []


# ---------------------------------------------------------------------------
# Fixtures: 10 PM events, 5 with proposed_nodes present, 5 missing
# ---------------------------------------------------------------------------

def _build_fixture():
    """Build graph with 5 existing nodes and 10 PM events (5 match, 5 missing)."""
    existing_nodes = {
        "L7.1": {
            "title": "Executor lifecycle monitor",
            "primary": ["agent/governance/executor.py"],
            "secondary": [],
            "test": [],
        },
        "L7.2": {
            "title": "Chain context event sourcing",
            "primary": ["agent/governance/chain_context.py"],
            "secondary": [],
            "test": [],
        },
        "L7.3": {
            "title": "Reconcile comprehensive phases",
            "primary": ["agent/governance/reconcile.py"],
            "secondary": [],
            "test": [],
        },
        "L7.4": {
            "title": "Memory backend local docker",
            "primary": ["agent/governance/memory_backend.py"],
            "secondary": [],
            "test": [],
        },
        "L7.5": {
            "title": "Conflict rules engine",
            "primary": ["agent/governance/conflict_rules.py"],
            "secondary": [],
            "test": [],
        },
    }

    pm_events = [
        # --- 5 that match existing nodes ---
        # Match by exact node_id (strategy 1)
        {
            "task_id": "pm-001",
            "proposed_nodes": [{"node_id": "L7.1", "title": "Executor lifecycle monitor",
                                "parent_layer": "L7", "primary": ["agent/governance/executor.py"]}],
        },
        # Match by exact node_id (strategy 1)
        {
            "task_id": "pm-002",
            "proposed_nodes": [{"node_id": "L7.2", "title": "Chain context",
                                "parent_layer": "L7", "primary": ["agent/governance/chain_context.py"]}],
        },
        # Match by title Jaccard (strategy 2) — same title tokens
        {
            "task_id": "pm-003",
            "proposed_nodes": [{"node_id": "", "title": "Reconcile comprehensive phases",
                                "parent_layer": "L7", "primary": ["agent/governance/new_reconcile.py"]}],
        },
        # Match by file overlap (strategy 3)
        {
            "task_id": "pm-004",
            "proposed_nodes": [{"node_id": "", "title": "Some random title",
                                "parent_layer": "", "primary": ["agent/governance/memory_backend.py"]}],
        },
        # Match by exact node_id (strategy 1)
        {
            "task_id": "pm-005",
            "proposed_nodes": [{"node_id": "L7.5", "title": "Conflict rules",
                                "parent_layer": "L7", "primary": ["agent/governance/conflict_rules.py"]}],
        },
        # --- 5 that are MISSING from node_state ---
        # Missing: brand new node_id not in graph
        {
            "task_id": "pm-006",
            "proposed_nodes": [{"node_id": "L7.99", "title": "New phase B handler",
                                "parent_layer": "L7", "primary": ["agent/governance/reconcile_phases/phase_b.py"]}],
        },
        # Missing: no node_id, title doesn't match any existing
        {
            "task_id": "pm-007",
            "proposed_nodes": [{"node_id": "", "title": "Telemetry exporter pipeline",
                                "parent_layer": "L8", "primary": ["agent/telemetry/exporter.py"]}],
        },
        # Missing: different layer, unique title
        {
            "task_id": "pm-008",
            "proposed_nodes": [{"node_id": "", "title": "Deploy canary validator",
                                "parent_layer": "L9", "primary": ["agent/deploy/canary.py"]}],
        },
        # Missing: no overlap with anything
        {
            "task_id": "pm-009",
            "proposed_nodes": [{"node_id": "", "title": "Webhook handler ingress",
                                "parent_layer": "L10", "primary": ["agent/webhooks/ingress.py"]}],
        },
        # Missing: explicit unknown id
        {
            "task_id": "pm-010",
            "proposed_nodes": [{"node_id": "L11.1", "title": "Notification service",
                                "parent_layer": "L11", "primary": ["agent/notifications/service.py"]}],
        },
    ]
    return existing_nodes, pm_events


# ---------------------------------------------------------------------------
# AC3.1 — phase_b.run finds all 5 missing nodes via 3-strategy match
# ---------------------------------------------------------------------------

class TestAC31FindMissing:
    def test_finds_all_five_missing(self):
        from agent.governance.reconcile_phases import phase_b

        nodes, pm_events = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, pm_events)

        results = phase_b.run(ctx)

        assert len(results) == 5, (
            f"Expected 5 discrepancies, got {len(results)}: "
            f"{[d.detail for d in results]}"
        )
        for d in results:
            assert d.type == "pm_proposed_not_in_node_state"

    def test_matched_nodes_not_emitted(self):
        """The 5 proposed_nodes that DO exist should not appear."""
        from agent.governance.reconcile_phases import phase_b

        nodes, pm_events = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, pm_events)

        results = phase_b.run(ctx)
        detail_str = " ".join(d.detail for d in results)

        # pm-001..005 should NOT appear (they matched)
        for tid in ["pm-001", "pm-002", "pm-003", "pm-004", "pm-005"]:
            assert tid not in detail_str, f"{tid} should have matched but was emitted"

        # pm-006..010 SHOULD appear
        for tid in ["pm-006", "pm-007", "pm-008", "pm-009", "pm-010"]:
            assert tid in detail_str, f"{tid} should be missing but was not emitted"


# ---------------------------------------------------------------------------
# AC3.2 — apply_phase_b_mutations with dry_run=False calls node-create
# ---------------------------------------------------------------------------

class TestAC32Mutations:
    def test_apply_calls_node_create(self):
        from agent.governance.reconcile_phases import phase_b

        nodes, pm_events = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, pm_events)

        results = phase_b.run(ctx)
        high_discs = [d for d in results if d.confidence == "high"]
        assert len(high_discs) > 0, "Need at least 1 high-conf discrepancy"

        mock_post = MagicMock(return_value=MagicMock(status_code=200))
        mutations = phase_b.apply_phase_b_mutations(
            ctx, high_discs, threshold="high", dry_run=False,
            _post_fn=mock_post,
        )

        assert len(mutations) == len(high_discs)
        assert mock_post.call_count == len(high_discs)

        for mutation in mutations:
            assert mutation["status"] == "applied"
            assert "backfill_ref" in mutation
            assert "pm_task_id" in mutation["backfill_ref"]

        # Verify URL pattern
        for call_args in mock_post.call_args_list:
            url = call_args[0][0]
            assert "/api/wf/aming-claw/node-create" in url
            payload = call_args[1]["json"]
            assert "backfill_ref" in payload
            assert "pm_task_id" in payload["backfill_ref"]

    def test_dry_run_does_not_call_post(self):
        from agent.governance.reconcile_phases import phase_b

        nodes, pm_events = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, pm_events)

        results = phase_b.run(ctx)
        high_discs = [d for d in results if d.confidence == "high"]

        mock_post = MagicMock()
        mutations = phase_b.apply_phase_b_mutations(
            ctx, high_discs, threshold="high", dry_run=True,
            _post_fn=mock_post,
        )

        assert mock_post.call_count == 0
        for m in mutations:
            assert m["status"] == "dry_run"


# ---------------------------------------------------------------------------
# AC3.3 — Phase E dedup suppression
# ---------------------------------------------------------------------------

class TestAC33PhaseEDedup:
    def test_phase_e_suppresses_overlap(self):
        from agent.governance.reconcile_phases import phase_b, Discrepancy

        nodes, pm_events = _build_fixture()
        graph = _StubGraph(nodes)

        # Run without Phase E suppression
        ctx_no_filter = _StubCtx("aming-claw", graph, pm_events)
        unfiltered = phase_b.run(ctx_no_filter)
        unfiltered_count = len(unfiltered)

        # Create Phase E discrepancy that binds one of the missing node's primary files
        # pm-006 proposes primary=["agent/governance/reconcile_phases/phase_b.py"]
        phase_e_disc = [
            Discrepancy(
                type="unmapped_high_conf_suggest",
                node_id="L7.3",
                field="secondary",
                detail="file=agent/governance/reconcile_phases/phase_b.py suggested_node=L7.3 field=secondary score=0.92 top2=0.40 gap=0.52",
                confidence="high",
            ),
        ]

        ctx_filtered = _StubCtx("aming-claw", graph, pm_events)
        filtered = phase_b.run(ctx_filtered, phase_e_discrepancies=phase_e_disc)
        filtered_count = len(filtered)

        assert filtered_count < unfiltered_count, (
            f"Phase E dedup should reduce count: unfiltered={unfiltered_count}, "
            f"filtered={filtered_count}"
        )


# ---------------------------------------------------------------------------
# AC3.4 — confidence_breakdown dict
# ---------------------------------------------------------------------------

class TestAC34ConfidenceBreakdown:
    def test_breakdown_dict_present(self):
        from agent.governance.reconcile_phases import phase_b

        nodes, pm_events = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, pm_events)

        results = phase_b.run(ctx)
        breakdown = results.confidence_breakdown

        assert isinstance(breakdown, dict)
        assert set(breakdown.keys()) == {"high", "medium", "low"}
        assert all(isinstance(v, int) for v in breakdown.values())

        # Sum should equal total discrepancies
        assert sum(breakdown.values()) == len(results)

    def test_breakdown_counts_match(self):
        from agent.governance.reconcile_phases import phase_b

        nodes, pm_events = _build_fixture()
        graph = _StubGraph(nodes)
        ctx = _StubCtx("aming-claw", graph, pm_events)

        results = phase_b.run(ctx)
        breakdown = results.confidence_breakdown

        # Manually count
        actual_high = sum(1 for d in results if d.confidence == "high")
        actual_medium = sum(1 for d in results if d.confidence == "medium")
        actual_low = sum(1 for d in results if d.confidence == "low")

        assert breakdown["high"] == actual_high
        assert breakdown["medium"] == actual_medium
        assert breakdown["low"] == actual_low


# ---------------------------------------------------------------------------
# Extra: allocate_next_id
# ---------------------------------------------------------------------------

class TestAllocateNextId:
    def test_basic(self):
        from agent.governance.reconcile_phases.phase_b import allocate_next_id
        assert allocate_next_id("L7", ["L7.1", "L7.2", "L7.5"]) == "L7.6"

    def test_empty(self):
        from agent.governance.reconcile_phases.phase_b import allocate_next_id
        assert allocate_next_id("L7", []) == "L7.1"

    def test_different_layers(self):
        from agent.governance.reconcile_phases.phase_b import allocate_next_id
        assert allocate_next_id("L8", ["L7.1", "L7.2", "L8.3"]) == "L8.4"
