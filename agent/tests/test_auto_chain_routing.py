"""Tests for auto_chain routing integration (AC5, AC6, AC10).

Covers backward compatibility: CHAIN dict still works, linear routing
preserved when graph has default policies, and end-to-end dispatch.
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


# ---------------------------------------------------------------------------
# AC5: CHAIN dict backward compat
# ---------------------------------------------------------------------------

class TestAC5_ChainDictPreserved:
    """CHAIN dict is preserved and works when no graph is loaded."""

    def test_chain_dict_structure(self):
        from governance.auto_chain import CHAIN
        assert "pm" in CHAIN
        assert "dev" in CHAIN
        assert "test" in CHAIN
        assert "qa" in CHAIN
        assert "gatekeeper" in CHAIN
        assert "merge" in CHAIN
        assert "deploy" in CHAIN

    def test_chain_dict_pm_to_dev(self):
        from governance.auto_chain import CHAIN
        gate_fn, next_type, builder = CHAIN["pm"]
        assert next_type == "dev"

    def test_chain_dict_dev_to_test(self):
        from governance.auto_chain import CHAIN
        gate_fn, next_type, builder = CHAIN["dev"]
        assert next_type == "test"

    def test_chain_dict_test_to_qa(self):
        from governance.auto_chain import CHAIN
        gate_fn, next_type, builder = CHAIN["test"]
        assert next_type == "qa"

    def test_chain_dict_qa_to_gatekeeper(self):
        from governance.auto_chain import CHAIN
        gate_fn, next_type, builder = CHAIN["qa"]
        assert next_type == "gatekeeper"

    def test_chain_dict_gatekeeper_to_merge(self):
        from governance.auto_chain import CHAIN
        gate_fn, next_type, builder = CHAIN["gatekeeper"]
        assert next_type == "merge"

    def test_chain_dict_merge_to_deploy(self):
        from governance.auto_chain import CHAIN
        gate_fn, next_type, builder = CHAIN["merge"]
        assert next_type == "deploy"

    def test_chain_dict_deploy_terminal(self):
        from governance.auto_chain import CHAIN
        gate_fn, next_type, builder = CHAIN["deploy"]
        assert next_type is None


# ---------------------------------------------------------------------------
# AC6: Default graph policies → same linear chain
# ---------------------------------------------------------------------------

class TestAC6_DefaultGraphLinear:
    """Graph with default policies produces same linear chain."""

    def test_dispatch_returns_none_for_default_policies(self, isolated_gov_db):
        """dispatch_next_stage returns None when all policies are default,
        signaling the caller to use the linear CHAIN dict."""
        from governance.auto_chain import dispatch_next_stage
        from governance.graph import AcceptanceGraph
        from governance.models import NodeDef

        conn = isolated_gov_db
        graph = AcceptanceGraph()
        node = NodeDef(
            id="L1.1", title="Test", layer="L1",
            verify_level=1, gate_mode="auto",
            primary=["agent/foo.py"],
        )
        graph.G.add_node(node.id, **node.to_dict())

        next_stage, skipped, policies = dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {"changed_files": ["agent/foo.py"]},
            {"related_nodes": ["L1.1"]},
            "tr-default",
            graph=graph,
        )
        # None means "use CHAIN dict" which gives pm→dev→test→qa→gk→merge→deploy
        assert next_stage is None

    def test_linear_chain_sequence_matches_chain_dict(self):
        """Verify the full linear chain sequence matches CHAIN dict."""
        from governance.auto_chain import CHAIN

        # Walk CHAIN from pm to deploy
        sequence = []
        current = "pm"
        while current is not None:
            sequence.append(current)
            _, next_type, _ = CHAIN[current]
            current = next_type
        assert sequence == ["pm", "dev", "test", "qa", "gatekeeper", "merge", "deploy"]


# ---------------------------------------------------------------------------
# AC10: No regression in dispatch_next_stage
# ---------------------------------------------------------------------------

class TestAC10_NoRegression:
    """Existing chain works end-to-end (no regression)."""

    def test_chain_lookup_tables_exist(self):
        from governance.auto_chain import _GATES, _BUILDERS
        assert "_gate_post_pm" in _GATES
        assert "_gate_checkpoint" in _GATES
        assert "_gate_t2_pass" in _GATES
        assert "_gate_qa_pass" in _GATES
        assert "_gate_gatekeeper_pass" in _GATES
        assert "_gate_release" in _GATES
        assert "_gate_deploy_pass" in _GATES

        assert "_build_dev_prompt" in _BUILDERS
        assert "_build_test_prompt" in _BUILDERS
        assert "_build_qa_prompt" in _BUILDERS
        assert "_build_gatekeeper_prompt" in _BUILDERS
        assert "_build_merge_prompt" in _BUILDERS
        assert "_build_deploy_prompt" in _BUILDERS
        assert "_finalize_chain" in _BUILDERS

    def test_gate_functions_callable(self):
        from governance.auto_chain import _GATES
        for name, fn in _GATES.items():
            assert callable(fn), f"Gate {name} is not callable"

    def test_builder_functions_callable(self):
        from governance.auto_chain import _BUILDERS
        for name, fn in _BUILDERS.items():
            assert callable(fn), f"Builder {name} is not callable"

    def test_max_chain_depth_preserved(self):
        from governance.auto_chain import MAX_CHAIN_DEPTH
        assert MAX_CHAIN_DEPTH == 10

    def test_dispatch_next_stage_function_exists(self):
        from governance.auto_chain import dispatch_next_stage
        assert callable(dispatch_next_stage)


# ---------------------------------------------------------------------------
# AC7: Routing audit for linear chain too
# ---------------------------------------------------------------------------

class TestAC7_LinearChainAudit:
    """Linear chain routing also gets audit entries."""

    def test_no_graph_audit_written(self, isolated_gov_db):
        from governance.auto_chain import dispatch_next_stage
        conn = isolated_gov_db

        dispatch_next_stage(
            conn, "test-proj", "task-1", "dev",
            {}, {}, "tr-linear-audit", graph=None,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_decision'"
        ).fetchall()
        assert len(rows) >= 1
        details = json.loads(rows[0]["details_json"])
        assert details["routing_mode"] == "linear_chain"
        assert details["trace_id"] == "tr-linear-audit"

    def test_pre_dev_stage_audit(self, isolated_gov_db):
        """PM stage should also get audit entry."""
        from governance.auto_chain import dispatch_next_stage
        from governance.graph import AcceptanceGraph

        conn = isolated_gov_db
        graph = AcceptanceGraph()

        dispatch_next_stage(
            conn, "test-proj", "task-1", "pm",
            {}, {}, "tr-pm-audit", graph=graph,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='routing_decision'"
        ).fetchall()
        assert len(rows) >= 1
        details = json.loads(rows[0]["details_json"])
        assert details["current_stage"] == "pm"
        assert details["reason"] == "pre_dev_stage"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for routing logic."""

    def test_empty_verify_requires(self, isolated_gov_db):
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = isolated_gov_db
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", [])
        assert satisfied
        assert blocking == []

    def test_node_not_in_node_state_blocks(self, isolated_gov_db):
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = isolated_gov_db
        satisfied, blocking = _check_verify_requires_satisfied(
            conn, "test-proj", ["L99.99"]
        )
        assert not satisfied
        assert "L99.99" in blocking

    def test_derive_stages_merge_always_present(self):
        from governance.auto_chain import _derive_chain_stages_from_policies
        # Even with all skip+zero, merge is always included
        stages = _derive_chain_stages_from_policies([
            {"node_id": "L1.1", "gate_mode": "skip", "verify_level": 0}
        ])
        assert "merge" in stages

    def test_audit_routing_skip_safe_on_db_error(self):
        """_audit_routing_skip should not raise even if DB fails."""
        from governance.auto_chain import _audit_routing_skip
        # Pass a mock that raises on execute
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("DB error")
        # Should not raise
        _audit_routing_skip(mock_conn, "proj", "task", "tr-x", {"test": True})

    def test_audit_routing_decision_safe_on_db_error(self):
        """_audit_routing_decision should not raise even if DB fails."""
        from governance.auto_chain import _audit_routing_decision
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("DB error")
        _audit_routing_decision(mock_conn, "proj", "task", "tr-x", {"test": True})


# ---------------------------------------------------------------------------
# AC7-AC9: Version gate block visibility
# ---------------------------------------------------------------------------

class TestGateBlockVisibility:
    """Tests for B1/B6: auto_chain gate block visibility in responses."""

    @staticmethod
    def _seed_project_version(conn, project_id="test-proj"):
        """Insert a project_version row into the given connection."""
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, git_head, dirty_files, updated_at, updated_by) "
            "VALUES (?, 'abc123', 'abc123', '[]', datetime('now'), 'test')",
            (project_id,),
        )
        conn.commit()

    @patch("governance.auto_chain._gate_version_check")
    @patch("governance.auto_chain._publish_event")
    def test_ac7_gate_blocked_returns_gate_blocked_true(self, mock_pub, mock_gate, isolated_gov_db):
        """AC7: When _gate_version_check returns (False, reason), dispatch returns
        gate_blocked=True and dispatched is not True."""
        from governance.auto_chain import _do_chain

        conn = isolated_gov_db
        self._seed_project_version(conn)
        mock_gate.return_value = (False, "dirty workspace (2 files)")

        result = _do_chain(conn, "test-proj", "task-1", "dev", {}, {"chain_depth": 0})

        assert result["gate_blocked"] is True
        assert result.get("dispatched") is not True
        assert result.get("reason") == "dirty workspace (2 files)"

    @patch("governance.auto_chain._gate_version_check")
    @patch("governance.auto_chain._publish_event")
    def test_ac8_audit_log_inserted_on_gate_block(self, mock_pub, mock_gate, isolated_gov_db):
        """AC8: When gate blocks, audit_log row with action='auto_chain_gate_blocked'
        is inserted."""
        from governance.auto_chain import _do_chain

        conn = isolated_gov_db
        self._seed_project_version(conn)
        mock_gate.return_value = (False, "server version mismatch")

        _do_chain(conn, "test-proj", "task-1", "dev", {}, {"chain_depth": 0})
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='auto_chain_gate_blocked'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["project_id"] == "test-proj"
        assert row["task_id"] == "task-1"
        details = json.loads(row["details_json"])
        assert "gate_reason" in details
        assert details["gate_reason"] == "server version mismatch"

    @patch("governance.auto_chain._gate_version_check")
    @patch("governance.auto_chain._publish_event")
    def test_ac9_log_warning_on_gate_block(self, mock_pub, mock_gate, isolated_gov_db):
        """AC9: log.warning is called (not log.info) when gate blocks dispatch."""
        from governance.auto_chain import _do_chain

        conn = isolated_gov_db
        self._seed_project_version(conn)
        mock_gate.return_value = (False, "dirty workspace (3 files)")

        with patch("governance.auto_chain.log") as mock_log:
            _do_chain(conn, "test-proj", "task-1", "dev", {}, {"chain_depth": 0})

            # Verify log.warning was called with the gate block message
            warning_calls = mock_log.warning.call_args_list
            gate_block_warnings = [
                c for c in warning_calls
                if "version gate blocked" in str(c)
            ]
            assert len(gate_block_warnings) >= 1, (
                f"Expected log.warning with 'version gate blocked', "
                f"got warning calls: {warning_calls}"
            )
            # Verify it includes task_id and project_id
            call_str = str(gate_block_warnings[0])
            assert "task-1" in call_str
            assert "test-proj" in call_str


# ---------------------------------------------------------------------------
# B2: skip_version_check access control + audit trail
# ---------------------------------------------------------------------------

class TestVersionGateBypassAccessControl:
    """Tests for skip_version_check operator_id/bypass_reason validation."""

    @staticmethod
    def _seed_project_version(conn):
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, git_head, dirty_files, updated_at, updated_by) "
            "VALUES (?, 'abc123', 'abc123', '[]', datetime('now'), 'test')", ("test-proj",),
        )
        conn.commit()

    def test_bypass_rejected_when_operator_id_missing(self, isolated_gov_db):
        """skip_version_check is ignored when operator_id is missing."""
        from governance.auto_chain import _gate_version_check
        conn = isolated_gov_db
        self._seed_project_version(conn)

        metadata = {"skip_version_check": True, "bypass_reason": "testing"}
        with patch("governance.auto_chain.log") as mock_log, \
             patch("governance.auto_chain.SERVER_VERSION", "abc123", create=True), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123\n")
            passed, reason = _gate_version_check(conn, "test-proj", {}, metadata)

        # Should NOT have returned early with bypass — falls through to normal check
        # The warning should have been logged
        warning_calls = mock_log.warning.call_args_list
        skip_warnings = [c for c in warning_calls if "skip_version_check ignored" in str(c)]
        assert len(skip_warnings) >= 1, f"Expected skip_version_check warning, got: {warning_calls}"

    def test_bypass_rejected_when_bypass_reason_missing(self, isolated_gov_db):
        """skip_version_check is ignored when bypass_reason is missing."""
        from governance.auto_chain import _gate_version_check
        conn = isolated_gov_db
        self._seed_project_version(conn)

        metadata = {"skip_version_check": True, "operator_id": "admin"}
        with patch("governance.auto_chain.log") as mock_log, \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123\n")
            passed, reason = _gate_version_check(conn, "test-proj", {}, metadata)

        warning_calls = mock_log.warning.call_args_list
        skip_warnings = [c for c in warning_calls if "skip_version_check ignored" in str(c)]
        assert len(skip_warnings) >= 1, f"Expected skip_version_check warning, got: {warning_calls}"

    def test_bypass_rejected_when_both_missing(self, isolated_gov_db):
        """skip_version_check is ignored when both fields are missing."""
        from governance.auto_chain import _gate_version_check
        conn = isolated_gov_db
        self._seed_project_version(conn)

        metadata = {"skip_version_check": True}
        with patch("governance.auto_chain.log") as mock_log, \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123\n")
            passed, reason = _gate_version_check(conn, "test-proj", {}, metadata)

        warning_calls = mock_log.warning.call_args_list
        skip_warnings = [c for c in warning_calls if "skip_version_check ignored" in str(c)]
        assert len(skip_warnings) >= 1


class TestVersionGateBypassAudit:
    """Tests for version_gate_bypass audit trail."""

    def test_audit_row_inserted_on_valid_bypass(self, isolated_gov_db):
        """When operator_id and bypass_reason are valid, audit row is inserted."""
        from governance.auto_chain import _audit_version_gate_bypass
        conn = isolated_gov_db

        _audit_version_gate_bypass(conn, "test-proj", "task-1", "admin-user", "hotfix deploy", "dev")
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='version_gate_bypass'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["project_id"] == "test-proj"
        assert row["task_id"] == "task-1"
        assert row["actor"] == "admin-user"
        details = json.loads(row["details_json"])
        assert details["bypass_reason"] == "hotfix deploy"
        assert details["task_type"] == "dev"

    def test_valid_bypass_returns_true(self, isolated_gov_db):
        """_gate_version_check returns True when skip_version_check has valid credentials."""
        from governance.auto_chain import _gate_version_check
        conn = isolated_gov_db
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, git_head, dirty_files, updated_at, updated_by) "
            "VALUES (?, 'abc123', 'abc123', '[]', datetime('now'), 'test')", ("test-proj",),
        )
        conn.commit()

        metadata = {
            "skip_version_check": True,
            "operator_id": "admin",
            "bypass_reason": "emergency fix",
        }
        passed, reason = _gate_version_check(conn, "test-proj", {}, metadata)
        assert passed is True
        assert reason == "skipped (task metadata)"

        # Verify audit row was inserted
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action='version_gate_bypass'"
        ).fetchall()
        assert len(rows) == 1


class TestVersionGateBypassFrequency:
    """Tests for high bypass frequency warning."""

    def test_high_frequency_warning_logged(self, isolated_gov_db):
        """When >3 bypasses in 24h, a warning is logged."""
        from governance.auto_chain import _audit_version_gate_bypass
        conn = isolated_gov_db

        # Insert 3 existing bypass events
        for i in range(3):
            conn.execute(
                "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
                "VALUES (?, 'version_gate_bypass', 'prev-op', 1, datetime('now'), ?, '{}')",
                ("test-proj", f"task-prev-{i}"),
            )
        conn.commit()

        # 4th bypass should trigger the warning
        with patch("governance.auto_chain.log") as mock_log:
            _audit_version_gate_bypass(conn, "test-proj", "task-4", "admin", "test reason", "dev")

        warning_calls = mock_log.warning.call_args_list
        freq_warnings = [c for c in warning_calls if "high bypass frequency" in str(c)]
        assert len(freq_warnings) == 1, f"Expected 1 high frequency warning, got: {warning_calls}"

    def test_no_warning_at_3_or_fewer(self, isolated_gov_db):
        """When <=3 bypasses in 24h, no warning is logged."""
        from governance.auto_chain import _audit_version_gate_bypass
        conn = isolated_gov_db

        # Insert 2 existing bypass events
        for i in range(2):
            conn.execute(
                "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
                "VALUES (?, 'version_gate_bypass', 'prev-op', 1, datetime('now'), ?, '{}')",
                ("test-proj", f"task-prev-{i}"),
            )
        conn.commit()

        # 3rd bypass should NOT trigger the warning
        with patch("governance.auto_chain.log") as mock_log:
            _audit_version_gate_bypass(conn, "test-proj", "task-3", "admin", "test reason", "dev")

        warning_calls = mock_log.warning.call_args_list
        freq_warnings = [c for c in warning_calls if "high bypass frequency" in str(c)]
        assert len(freq_warnings) == 0, f"Expected no frequency warning, got: {warning_calls}"


# ---------------------------------------------------------------------------
# AC8c: Retry scope includes prior changed_files (B28a)
# ---------------------------------------------------------------------------

class TestAC8c_RetryScopeIncludesPriorChangedFiles:
    """get_retry_scope() returns union of target_files + test_files
    + doc_impact.files + accumulated changed_files from prior dev stages."""

    def test_retry_scope_includes_target_and_test_files(self):
        from governance.chain_context import ChainContextStore
        store = ChainContextStore()
        metadata = {
            "target_files": ["agent/foo.py", "agent/bar.py"],
            "test_files": ["agent/tests/test_foo.py"],
            "doc_impact": {"files": ["docs/api/foo.md"]},
        }
        scope = store.get_retry_scope("chain-1", "test-proj", metadata)
        assert "agent/foo.py" in scope
        assert "agent/bar.py" in scope
        assert "agent/tests/test_foo.py" in scope
        assert "docs/api/foo.md" in scope

    def test_retry_scope_includes_accumulated_changed_files(self):
        from governance.chain_context import ChainContextStore
        store = ChainContextStore()
        # Simulate a prior dev stage via event-based API
        # 1) Create root PM task
        store.on_task_created({
            "task_id": "task-root", "type": "pm",
            "project_id": "test-proj", "prompt": "PRD",
        })
        # 2) Create dev task as child
        store.on_task_created({
            "task_id": "task-dev1", "type": "dev",
            "parent_task_id": "task-root",
            "project_id": "test-proj", "prompt": "implement",
        })
        # 3) Complete dev task with changed_files
        store.on_task_completed({
            "task_id": "task-dev1", "type": "dev",
            "project_id": "test-proj",
            "result": {"changed_files": ["agent/extra.py", "agent/new_module.py"]},
        })
        # The chain_id is the root task_id
        chain_id = "task-root"
        metadata = {
            "target_files": ["agent/foo.py"],
            "test_files": [],
        }
        scope = store.get_retry_scope(chain_id, "test-proj", metadata)
        assert "agent/foo.py" in scope
        assert "agent/extra.py" in scope
        assert "agent/new_module.py" in scope

    def test_retry_scope_empty_metadata_fallback(self):
        from governance.chain_context import ChainContextStore
        store = ChainContextStore()
        scope = store.get_retry_scope("chain-empty", "test-proj", {})
        assert isinstance(scope, set)
        assert len(scope) == 0
