"""Tests for G8: Auto-populate related_nodes in _gate_post_pm.

Covers AC2 (graph lookup populates related_nodes), AC3 (skip when already set),
AC4 (graph failure is non-critical).

Also covers QA gate verify_update failure propagation (AC5 from PRD task-1775870604).
"""

import json
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

import sys
import os

_agent_dir = os.path.join(os.path.dirname(__file__), "..")
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


# _make_in_memory_db removed — use the shared `isolated_gov_db` fixture from conftest.py


def _valid_prd_result(**overrides):
    """Return a minimal valid PRD result dict for _gate_post_pm."""
    base = {
        "requirements": ["R1: something"],
        "acceptance_criteria": ["AC1: something"],
        "target_files": ["agent/governance/auto_chain.py"],
        "related_nodes": [],
        "verification": {"method": "automated test", "command": "pytest"},
        "prd": {"feature": "test feature", "background": "bg", "scope": "s", "risk": "low"},
        "test_files": ["tests/test_something.py"],
        "proposed_nodes": ["L1.1"],
        "doc_impact": {"files": ["docs/test.md"], "changes": ["test"]},
    }
    base.update(overrides)
    return base


def _make_mock_graph(node_map):
    """Create a mock AcceptanceGraph with given node_id -> primary mapping."""
    mock_graph_cls = MagicMock()
    mock_instance = MagicMock()
    mock_graph_cls.return_value = mock_instance
    mock_instance.list_nodes.return_value = list(node_map.keys())

    def get_node(nid):
        return {"primary": node_map[nid]}

    mock_instance.get_node.side_effect = get_node
    return mock_graph_cls, mock_instance


def _seed_project_version(conn, project_id="test-proj"):
    """Insert a project_version row into the given connection."""
    conn.execute(
        "INSERT INTO project_version (project_id, chain_version, git_head, updated_at, updated_by) VALUES (?,?,?, datetime('now'), 'test')",
        (project_id, "abc123", "abc123"),
    )
    conn.commit()


class TestG8RelatedNodes:
    """AC2: Graph lookup populates related_nodes when empty."""

    def test_populates_from_graph_when_empty(self, isolated_gov_db):
        """AC2: related_nodes populated from graph primary match."""
        from governance.auto_chain import _gate_post_pm

        conn = isolated_gov_db
        _seed_project_version(conn)

        result = _valid_prd_result(related_nodes=[])

        mock_graph_cls, _ = _make_mock_graph({
            "L9.12": ["agent/governance/auto_chain.py"],
            "L1.1": ["agent/governance/other.py"],
        })

        with patch("governance.auto_chain.AcceptanceGraph", mock_graph_cls, create=True), \
             patch("governance.auto_chain.os.path.exists", return_value=True), \
             patch.dict("os.environ", {"SHARED_VOLUME_PATH": "/tmp/sv"}):
            # Patch the import inside the function
            with patch.dict("sys.modules", {"governance.graph": MagicMock(AcceptanceGraph=mock_graph_cls)}):
                passed, reason = _gate_post_pm(conn, "test-proj", result, {})

        assert result["related_nodes"] == ["L9.12"]

    def test_skips_when_already_set(self, isolated_gov_db):
        """AC3: related_nodes not overwritten when already provided."""
        from governance.auto_chain import _gate_post_pm

        conn = isolated_gov_db
        _seed_project_version(conn)

        result = _valid_prd_result(related_nodes=["L9.12"])

        mock_graph_cls, mock_instance = _make_mock_graph({
            "L9.12": ["agent/governance/auto_chain.py"],
            "L5.5": ["agent/governance/auto_chain.py"],
        })

        with patch("governance.auto_chain.os.path.exists", return_value=True), \
             patch.dict("os.environ", {"SHARED_VOLUME_PATH": "/tmp/sv"}):
            passed, reason = _gate_post_pm(conn, "test-proj", result, {})

        # related_nodes should stay as originally provided
        assert result["related_nodes"] == ["L9.12"]
        # Graph should NOT have been loaded
        mock_instance.load.assert_not_called()

    def test_graph_failure_non_critical(self, isolated_gov_db):
        """AC4: Graph failure doesn't block the gate."""
        from governance.auto_chain import _gate_post_pm

        conn = isolated_gov_db
        _seed_project_version(conn)

        result = _valid_prd_result(related_nodes=[])

        # Use a side_effect that returns False only for graph.json paths
        original_exists = os.path.exists

        def selective_exists(path):
            if "graph.json" in str(path):
                return False
            return original_exists(path)

        with patch("governance.auto_chain.os.path.exists", side_effect=selective_exists), \
             patch.dict("os.environ", {"SHARED_VOLUME_PATH": "/tmp/nonexistent"}):
            passed, reason = _gate_post_pm(conn, "test-proj", result, {})

        # Gate should pass (True) for valid PRD
        assert passed is True
        # related_nodes stays empty (falsy)
        assert not result.get("related_nodes")

    def test_graph_exception_during_load(self, isolated_gov_db):
        """AC4 extended: Exception during graph.load() is caught silently."""
        from governance.auto_chain import _gate_post_pm

        conn = isolated_gov_db
        _seed_project_version(conn)

        result = _valid_prd_result(related_nodes=[])

        mock_graph_cls = MagicMock()
        mock_instance = MagicMock()
        mock_graph_cls.return_value = mock_instance
        mock_instance.load.side_effect = Exception("Corrupt graph.json")

        original_exists = os.path.exists

        def selective_exists(path):
            if "graph.json" in str(path):
                return True
            return original_exists(path)

        with patch("governance.auto_chain.os.path.exists", side_effect=selective_exists), \
             patch.dict("os.environ", {"SHARED_VOLUME_PATH": "/tmp/sv"}), \
             patch.dict("sys.modules", {"governance.graph": MagicMock(AcceptanceGraph=mock_graph_cls)}):
            passed, reason = _gate_post_pm(conn, "test-proj", result, {})

        assert passed is True
        assert not result.get("related_nodes")


class TestQAGateVerifyUpdateFailure:
    """AC5: _gate_qa_pass propagates _try_verify_update failure reason."""

    def test_verify_update_exception_surfaces_in_gate_reason(self, isolated_gov_db):
        """When verify_update raises, _gate_qa_pass returns failure with the exception text."""
        from governance.auto_chain import _gate_qa_pass

        conn = isolated_gov_db
        _seed_project_version(conn)

        result = {"recommendation": "qa_pass", "review_summary": "looks good"}
        metadata = {"related_nodes": ["L1.3"]}

        error_msg = "Evidence validation: e2e_report requires passed>0"
        with patch(
            "governance.auto_chain._try_verify_update",
            return_value=(False, f"verify_update failed for nodes ['L1.3']: {error_msg}"),
        ):
            passed, reason = _gate_qa_pass(conn, "test-proj", result, metadata)

        assert passed is False
        assert "verify_update failed" in reason
        assert error_msg in reason
        assert "L1.3" in reason

    def test_verify_update_success_proceeds_to_check_nodes(self, isolated_gov_db):
        """When verify_update succeeds, _gate_qa_pass proceeds to _check_nodes_min_status."""
        from governance.auto_chain import _gate_qa_pass

        conn = isolated_gov_db
        _seed_project_version(conn)
        # Insert node at qa_pass so _check_nodes_min_status passes
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, updated_at) VALUES (?,?,?, datetime('now'))",
            ("test-proj", "L1.3", "qa_pass"),
        )
        conn.commit()

        result = {"recommendation": "qa_pass", "review_summary": "looks good"}
        metadata = {"related_nodes": ["L1.3"]}

        with patch(
            "governance.auto_chain._try_verify_update",
            return_value=(True, ""),
        ):
            passed, reason = _gate_qa_pass(conn, "test-proj", result, metadata)

        assert passed is True

    def test_try_verify_update_returns_true_on_success(self, isolated_gov_db):
        """AC1: _try_verify_update returns (True, '') on success."""
        from governance.auto_chain import _try_verify_update

        conn = isolated_gov_db
        metadata = {"related_nodes": ["L1.3"]}

        mock_state_service = MagicMock()
        mock_state_service.verify_update.return_value = None  # success

        with patch.dict("sys.modules", {
            "governance.state_service": mock_state_service,
            "governance.graph": MagicMock(AcceptanceGraph=MagicMock()),
        }), patch("governance.auto_chain.os.path.exists", return_value=False):
            ok, err = _try_verify_update(conn, "test-proj", metadata, "qa_pass", "qa", {"type": "e2e_report"})

        assert ok is True
        assert err == ""

    def test_try_verify_update_returns_false_on_exception(self, isolated_gov_db):
        """AC1/AC4: _try_verify_update returns (False, error_msg) on exception and logs warning."""
        from governance.auto_chain import _try_verify_update

        conn = isolated_gov_db
        metadata = {"related_nodes": ["L1.3"]}

        mock_state_service = MagicMock()
        mock_state_service.verify_update.side_effect = ValueError("RBAC denied")

        with patch.dict("sys.modules", {
            "governance.state_service": mock_state_service,
            "governance.graph": MagicMock(AcceptanceGraph=MagicMock()),
        }), patch("governance.auto_chain.os.path.exists", return_value=False):
            ok, err = _try_verify_update(conn, "test-proj", metadata, "qa_pass", "qa", {"type": "e2e_report"})

        assert ok is False
        assert "verify_update failed" in err
        assert "RBAC denied" in err
        assert "L1.3" in err

    def test_try_verify_update_no_related_nodes(self, isolated_gov_db):
        """_try_verify_update returns (True, '') when no related_nodes."""
        from governance.auto_chain import _try_verify_update

        conn = isolated_gov_db
        metadata = {"related_nodes": []}

        ok, err = _try_verify_update(conn, "test-proj", metadata, "qa_pass", "qa", {})
        assert ok is True
        assert err == ""
