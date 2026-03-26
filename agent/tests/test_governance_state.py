"""Tests for governance state service."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import networkx  # noqa: F401
    _has_networkx = True
except ImportError:
    _has_networkx = False

from governance.db import get_connection, close_connection
if _has_networkx:
    from governance.graph import AcceptanceGraph
from governance.models import NodeDef
from governance import state_service
from governance.enums import VerifyStatus
from governance.errors import (
    PermissionDeniedError, ForbiddenTransitionError,
    InvalidEvidenceError, NodeNotFoundError, ReleaseBlockedError,
)
from governance.redis_client import reset_redis


@unittest.skipUnless(_has_networkx, "networkx not installed")
class TestStateService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        reset_redis()

        # Setup graph
        self.graph = AcceptanceGraph()
        self.graph.add_node(NodeDef(id="L0.1", layer="L0", verify_level=1))
        self.graph.add_node(NodeDef(id="L0.2", layer="L0", verify_level=2))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1", verify_level=2), deps=["L0.1"])

        # Setup DB
        self.project_id = "test-project"
        self.conn = get_connection(self.project_id)
        state_service.init_node_states(self.conn, self.project_id, self.graph)
        self.conn.commit()

        # Mock session
        self.tester_session = {
            "session_id": "ses-test",
            "principal_id": "tester-001",
            "project_id": self.project_id,
            "role": "tester",
            "scope": [],
        }
        self.qa_session = {
            "session_id": "ses-qa",
            "principal_id": "qa-001",
            "project_id": self.project_id,
            "role": "qa",
            "scope": [],
        }
        self.dev_session = {
            "session_id": "ses-dev",
            "principal_id": "dev-001",
            "project_id": self.project_id,
            "role": "dev",
            "scope": [],
        }
        self.coord_session = {
            "session_id": "ses-coord",
            "principal_id": "coord-001",
            "project_id": self.project_id,
            "role": "coordinator",
            "scope": [],
        }

    def tearDown(self):
        close_connection(self.conn)
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_init_node_states(self):
        state = state_service.get_node_status(self.conn, self.project_id, "L0.1")
        self.assertIsNotNone(state)
        self.assertEqual(state["verify_status"], "pending")

    def test_verify_update_tester_t2_pass(self):
        evidence = {
            "type": "test_report",
            "summary": {"passed": 50, "failed": 0, "exit_code": 0},
        }
        result = state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "t2_pass", self.tester_session, evidence,
        )
        self.assertIn("L0.1", result["updated_nodes"])

        state = state_service.get_node_status(self.conn, self.project_id, "L0.1")
        self.assertEqual(state["verify_status"], "t2_pass")

    def test_verify_update_qa_pass(self):
        # First move to t2_pass
        evidence_t2 = {"type": "test_report", "summary": {"passed": 50, "failed": 0, "exit_code": 0}}
        state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "t2_pass", self.tester_session, evidence_t2,
        )
        # Then QA pass
        evidence_qa = {"type": "e2e_report", "summary": {"passed": 14}}
        result = state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "qa_pass", self.qa_session, evidence_qa,
        )
        state = state_service.get_node_status(self.conn, self.project_id, "L0.1")
        self.assertEqual(state["verify_status"], "qa_pass")

    def test_forbidden_skip_t2(self):
        evidence = {"type": "e2e_report", "summary": {"passed": 14}}
        with self.assertRaises(ForbiddenTransitionError):
            state_service.verify_update(
                self.conn, self.project_id, self.graph,
                ["L0.1"], "qa_pass", self.qa_session, evidence,
            )

    def test_permission_denied_dev_t2(self):
        evidence = {"type": "test_report", "summary": {"passed": 50, "failed": 0, "exit_code": 0}}
        with self.assertRaises(PermissionDeniedError):
            state_service.verify_update(
                self.conn, self.project_id, self.graph,
                ["L0.1"], "t2_pass", self.dev_session, evidence,
            )

    def test_invalid_evidence(self):
        evidence = {"type": "error_log", "summary": {"error": "something"}}
        with self.assertRaises(InvalidEvidenceError):
            state_service.verify_update(
                self.conn, self.project_id, self.graph,
                ["L0.1"], "t2_pass", self.tester_session, evidence,
            )

    def test_mark_failed_and_recover(self):
        # Move to t2_pass
        evidence_t2 = {"type": "test_report", "summary": {"passed": 50, "failed": 0, "exit_code": 0}}
        state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "t2_pass", self.tester_session, evidence_t2,
        )
        # Mark failed
        evidence_fail = {"type": "error_log", "summary": {"error": "regression bug"}}
        state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "failed", self.dev_session, evidence_fail,
        )
        state = state_service.get_node_status(self.conn, self.project_id, "L0.1")
        self.assertEqual(state["verify_status"], "failed")

        # Recover
        evidence_fix = {"type": "commit_ref", "summary": {"commit_hash": "a1b2c3d4e5f6a"}}
        state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "pending", self.dev_session, evidence_fix,
        )
        state = state_service.get_node_status(self.conn, self.project_id, "L0.1")
        self.assertEqual(state["verify_status"], "pending")

    def test_waive_node(self):
        evidence = {"type": "manual_review", "summary": {"reason": "approved"}}
        state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "waived", self.coord_session, evidence,
        )
        state = state_service.get_node_status(self.conn, self.project_id, "L0.1")
        self.assertEqual(state["verify_status"], "waived")

    def test_get_summary(self):
        summary = state_service.get_summary(self.conn, self.project_id)
        self.assertEqual(summary["total_nodes"], 3)
        self.assertIn("pending", summary["by_status"])

    def test_release_gate_all_pending_blocked(self):
        with self.assertRaises(ReleaseBlockedError):
            state_service.release_gate(self.conn, self.project_id, self.graph)

    def test_snapshot_and_rollback(self):
        # Create snapshot at initial state
        v1 = state_service.create_snapshot(self.conn, self.project_id)
        self.conn.commit()

        # Change state
        evidence_t2 = {"type": "test_report", "summary": {"passed": 50, "failed": 0, "exit_code": 0}}
        state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "t2_pass", self.tester_session, evidence_t2,
        )
        self.conn.commit()

        # Rollback
        result = state_service.rollback(self.conn, self.project_id, v1, self.coord_session)
        self.conn.commit()
        self.assertGreaterEqual(result["nodes_affected"], 0)

        # Verify rolled back
        state = state_service.get_node_status(self.conn, self.project_id, "L0.1")
        self.assertEqual(state["verify_status"], "pending")

    def test_downstream_tracking(self):
        evidence = {"type": "test_report", "summary": {"passed": 50, "failed": 0, "exit_code": 0}}
        result = state_service.verify_update(
            self.conn, self.project_id, self.graph,
            ["L0.1"], "t2_pass", self.tester_session, evidence,
        )
        # L1.1 depends on L0.1, should be in affected_downstream
        self.assertIn("L1.1", result["affected_downstream"])


if __name__ == "__main__":
    unittest.main()
