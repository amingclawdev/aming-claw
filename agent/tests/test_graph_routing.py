"""Tests for graph-driven routing logic (Roadmap §5.5).

Covers AC1-AC9 from the PRD: node-level gate enforcement, dynamic routing,
skip logic, verify_requires ordering, and backward compatibility.
"""

import json
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

import sys
import os

# Ensure agent package is importable
_agent_dir = os.path.join(os.path.dirname(__file__), "..")
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def _make_in_memory_db():
    """Create an in-memory SQLite DB with minimal schema for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            action TEXT,
            actor TEXT,
            ok INTEGER,
            ts TEXT,
            task_id TEXT,
            details_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_state (
            project_id TEXT,
            node_id TEXT,
            verify_status TEXT DEFAULT 'pending',
            PRIMARY KEY (project_id, node_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT,
            type TEXT,
            status TEXT DEFAULT 'queued',
            metadata_json TEXT,
            trace_id TEXT,
            chain_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            task_id TEXT,
            gate_name TEXT,
            passed INTEGER,
            reason TEXT,
            trace_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            git_head TEXT,
            dirty_files TEXT,
            updated_at TEXT,
            updated_by TEXT,
            max_subtasks INTEGER DEFAULT 5
        )
    """)
    conn.commit()
    return conn


def _make_graph_with_nodes(nodes_config):
    """Create an AcceptanceGraph with configured nodes.

    nodes_config: list of dicts with keys:
      id, gate_mode, verify_level, primary, gates, verify_requires
    """
    from governance.graph import AcceptanceGraph
    from governance.models import NodeDef

    graph = AcceptanceGraph()
    for nc in nodes_config:
        node_def = NodeDef(
            id=nc["id"],
            title=nc.get("title", nc["id"]),
            layer=nc.get("layer", "L1"),
            verify_level=nc.get("verify_level", 1),
            gate_mode=nc.get("gate_mode", "auto"),
            primary=nc.get("primary", []),
            secondary=nc.get("secondary", []),
            test=nc.get("test", []),
            gates=nc.get("gates", []),
            verify_requires=nc.get("verify_requires", []),
        )
        graph.G.add_node(node_def.id, **node_def.to_dict())
    return graph


# ---------------------------------------------------------------------------
# AC1: gate_mode=skip bypasses QA/gatekeeper
# ---------------------------------------------------------------------------

class TestAC1_GateModeSkip:
    """Node with gate_mode=skip bypasses QA/gatekeeper stages."""

    def test_skip_node_bypasses_qa_gatekeeper(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 1,
             "primary": ["agent/foo.py"]},
        ])

        next_stage, skipped, policies = dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1"]},
            "tr-test123",
            graph=graph,
        )

        assert "qa" in skipped
        assert "gatekeeper" in skipped
        assert next_stage == "test"  # test still runs (verify_level=1)

    def test_skip_node_audit_entry(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 1,
             "primary": ["agent/foo.py"]},
        ])

        dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1"]},
            "tr-test123",
            graph=graph,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_skip'"
        ).fetchall()
        assert len(rows) >= 1
        details = json.loads(rows[0]["details_json"])
        assert details["gate_mode"] == "skip"
        assert "skip" in json.dumps(details)  # AC1: details_json contains 'gate_mode' and 'skip'


# ---------------------------------------------------------------------------
# AC2: verify_level=0 skips test stage
# ---------------------------------------------------------------------------

class TestAC2_VerifyLevelZero:
    """Node with verify_level=0 skips test stage."""

    def test_verify_level_zero_skips_test(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 0,
             "primary": ["agent/foo.py"]},
        ])

        next_stage, skipped, policies = dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1"]},
            "tr-test456",
            graph=graph,
        )

        assert "test" in skipped
        assert next_stage == "merge"  # skip everything but merge

    def test_verify_level_zero_audit_entry(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "auto", "verify_level": 0,
             "primary": ["agent/foo.py"]},
        ])

        dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1"]},
            "tr-test456",
            graph=graph,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_skip'"
        ).fetchall()
        has_verify_level = False
        for row in rows:
            details = json.loads(row["details_json"])
            if "verify_level" in details:
                has_verify_level = True
                assert details["verify_level"] == 0
        assert has_verify_level


# ---------------------------------------------------------------------------
# AC3: Node-specific gates
# ---------------------------------------------------------------------------

class TestAC3_NodeSpecificGates:
    """Node with custom gates: only those named gates are checked."""

    def test_get_node_specific_gates(self):
        from governance.gate_policy import get_node_specific_gates
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gates": [
                {"node_id": "L0.1", "min_status": "qa_pass", "policy": "default"}
            ]},
        ])

        gates = get_node_specific_gates(graph, "L1.1")
        assert len(gates) == 1
        assert gates[0].node_id == "L0.1"

    def test_no_graph_returns_empty_gates(self):
        from governance.gate_policy import get_node_specific_gates
        gates = get_node_specific_gates(None, "L1.1")
        assert gates == []


# ---------------------------------------------------------------------------
# AC4: verify_requires ordering
# ---------------------------------------------------------------------------

class TestAC4_VerifyRequires:
    """verify_requires enforced: B blocked until A is verified."""

    def test_verify_requires_blocks_when_not_satisfied(self):
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            ("test-proj", "L1.1", "pending"),
        )
        conn.commit()

        satisfied, blocking = _check_verify_requires_satisfied(
            conn, "test-proj", ["L1.1"]
        )
        assert not satisfied
        assert "L1.1" in blocking

    def test_verify_requires_passes_when_satisfied(self):
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            ("test-proj", "L1.1", "qa_pass"),
        )
        conn.commit()

        satisfied, blocking = _check_verify_requires_satisfied(
            conn, "test-proj", ["L1.1"]
        )
        assert satisfied
        assert blocking == []

    def test_verify_requires_t2_pass_also_satisfies(self):
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            ("test-proj", "L1.1", "t2_pass"),
        )
        conn.commit()

        satisfied, blocking = _check_verify_requires_satisfied(
            conn, "test-proj", ["L1.1"]
        )
        assert satisfied

    def test_dispatch_blocked_by_verify_requires(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            ("test-proj", "L1.1", "pending"),
        )
        conn.commit()

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "auto", "verify_level": 1,
             "primary": ["agent/foo.py"]},
            {"id": "L1.2", "gate_mode": "skip", "verify_level": 1,
             "primary": ["agent/bar.py"],
             "verify_requires": ["L1.1"]},
        ])

        next_stage, skipped, policies = dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/bar.py"]},
            {"related_nodes": ["L1.2"]},
            "tr-test789",
            graph=graph,
        )
        assert next_stage == "blocked"


# ---------------------------------------------------------------------------
# AC5: No graph → linear CHAIN fallback
# ---------------------------------------------------------------------------

class TestAC5_NoGraphFallback:
    """When AcceptanceGraph is None, use existing linear CHAIN."""

    def test_no_graph_returns_none_signal(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()

        next_stage, skipped, policies = dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1"]},
            "tr-test-none",
            graph=None,
        )

        # None signals caller to use CHAIN dict
        assert next_stage is None
        assert skipped == []
        assert policies == []

    def test_no_graph_audit_entry_written(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()

        dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {}, {}, "tr-test-none", graph=None,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_decision'"
        ).fetchall()
        assert len(rows) >= 1
        details = json.loads(rows[0]["details_json"])
        assert details["routing_mode"] == "linear_chain"
        assert "trace_id" in details


# ---------------------------------------------------------------------------
# AC6: Default policies → same as linear chain
# ---------------------------------------------------------------------------

class TestAC6_DefaultPoliciesFallback:
    """All nodes auto + verify_level>=1 → same linear chain."""

    def test_all_default_policies_returns_none(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "auto", "verify_level": 1,
             "primary": ["agent/foo.py"]},
            {"id": "L1.2", "gate_mode": "auto", "verify_level": 2,
             "primary": ["agent/bar.py"]},
        ])

        next_stage, skipped, policies = dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1", "L1.2"]},
            "tr-test-default",
            graph=graph,
        )

        # Returns None → caller uses CHAIN dict (same linear sequence)
        assert next_stage is None


# ---------------------------------------------------------------------------
# AC7: Routing decision audit
# ---------------------------------------------------------------------------

class TestAC7_RoutingAudit:
    """Every routing decision writes to audit_log with trace_id."""

    def test_audit_entry_has_trace_id(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 1,
             "primary": ["agent/foo.py"]},
        ])

        dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1"]},
            "tr-audit-test",
            graph=graph,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_decision'"
        ).fetchall()
        assert len(rows) >= 1
        for row in rows:
            details = json.loads(row["details_json"])
            assert "trace_id" in details
            assert details["trace_id"] == "tr-audit-test"


# ---------------------------------------------------------------------------
# AC8: impact_analyzer returns gate_mode and verify_level
# ---------------------------------------------------------------------------

class TestAC8_ImpactAnalyzerPolicies:
    """impact_analyzer.analyze() returns affected nodes with policies."""

    def test_analyze_returns_gate_mode_and_verify_level(self):
        from governance.impact_analyzer import ImpactAnalyzer, ImpactAnalysisRequest, FileHitPolicy
        from governance.enums import VerifyStatus

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 2,
             "primary": ["agent/foo.py"]},
        ])

        def get_status(nid):
            return VerifyStatus.PENDING

        analyzer = ImpactAnalyzer(graph, get_status)
        result = analyzer.analyze(ImpactAnalysisRequest(
            changed_files=["agent/foo.py"],
            file_policy=FileHitPolicy(match_primary=True),
        ))

        assert len(result["affected_nodes"]) >= 1
        node_info = result["affected_nodes"][0]
        assert node_info["gate_mode"] == "skip"
        assert node_info["verify_level"] == 2


# ---------------------------------------------------------------------------
# AC9: Mixed graph routing
# ---------------------------------------------------------------------------

class TestAC9_MixedGraphRouting:
    """Mixed skip/auto nodes produce correct routing."""

    def test_mixed_skip_and_auto_stages(self):
        from governance.auto_chain import _derive_chain_stages_from_policies

        policies = [
            {"node_id": "L1.1", "gate_mode": "skip", "verify_level": 1},
            {"node_id": "L1.2", "gate_mode": "auto", "verify_level": 1},
        ]

        stages = _derive_chain_stages_from_policies(policies)

        # auto node needs qa and gatekeeper, so they're included
        assert "test" in stages
        assert "qa" in stages
        assert "gatekeeper" in stages
        assert "merge" in stages

    def test_all_skip_nodes_bypass_qa_gatekeeper(self):
        from governance.auto_chain import _derive_chain_stages_from_policies

        policies = [
            {"node_id": "L1.1", "gate_mode": "skip", "verify_level": 1},
            {"node_id": "L1.2", "gate_mode": "skip", "verify_level": 1},
        ]

        stages = _derive_chain_stages_from_policies(policies)
        assert "qa" not in stages
        assert "gatekeeper" not in stages
        assert "test" in stages
        assert "merge" in stages

    def test_mixed_audit_entries(self):
        from governance.auto_chain import dispatch_next_stage
        conn = _make_in_memory_db()
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 1,
             "primary": ["agent/foo.py"]},
            {"id": "L1.2", "gate_mode": "auto", "verify_level": 1,
             "primary": ["agent/bar.py"]},
        ])

        dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py", "agent/bar.py"]},
            {"related_nodes": ["L1.1", "L1.2"]},
            "tr-mixed",
            graph=graph,
        )
        conn.commit()

        # Should have routing_skip for L1.1 (skip mode)
        skip_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_skip'"
        ).fetchall()
        assert len(skip_rows) >= 1
        skip_details = json.loads(skip_rows[0]["details_json"])
        assert skip_details["gate_mode"] == "skip"

        # Should have routing_decision
        decision_rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_decision'"
        ).fetchall()
        assert len(decision_rows) >= 1


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestDeriveChainStages:
    """Test _derive_chain_stages_from_policies helper."""

    def test_empty_policies_returns_full_chain(self):
        from governance.auto_chain import _derive_chain_stages_from_policies
        stages = _derive_chain_stages_from_policies([])
        assert stages == ["test", "qa", "gatekeeper", "merge"]

    def test_all_skip_verify_zero(self):
        from governance.auto_chain import _derive_chain_stages_from_policies
        policies = [
            {"node_id": "L1.1", "gate_mode": "skip", "verify_level": 0},
        ]
        stages = _derive_chain_stages_from_policies(policies)
        assert stages == ["merge"]


class TestGetNodeRoutingPolicy:
    """Test graph.get_node_routing_policy."""

    def test_returns_policy_dict(self):
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 3,
             "verify_requires": ["L0.1"]},
        ])
        policy = graph.get_node_routing_policy("L1.1")
        assert policy["gate_mode"] == "skip"
        assert policy["verify_level"] == 3
        assert policy["verify_requires"] == ["L0.1"]

    def test_nonexistent_node_raises(self):
        from governance.errors import NodeNotFoundError
        graph = _make_graph_with_nodes([])
        with pytest.raises(NodeNotFoundError):
            graph.get_node_routing_policy("L99.99")

    def test_get_routing_policies_for_nodes(self):
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "gate_mode": "skip", "verify_level": 2},
            {"id": "L1.2", "gate_mode": "auto", "verify_level": 1},
        ])
        policies = graph.get_routing_policies_for_nodes(["L1.1", "L1.2", "L99.99"])
        assert len(policies) == 2  # L99.99 skipped
        assert policies[0]["gate_mode"] == "skip"
        assert policies[1]["gate_mode"] == "auto"
