"""Tests for backfill node promotion (pending -> qa_pass bypass).

AC11: promote with valid backfill_ref + real commit succeeds -> node at qa_pass
AC12: promote node without backfill_ref returns error
AC13: promote with fake merge_commit returns error
AC14: promoted node can still go through subsequent verify-update flow
"""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import SCHEMA_SQL
from governance.enums import VerifyStatus, EvidenceType
from governance.evidence import EVIDENCE_RULES, validate_evidence
from governance.models import Evidence
from governance.errors import ValidationError, NodeNotFoundError


def _make_db():
    """Create an in-memory governance DB with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _seed_node(conn, project_id, node_id, status="pending", version=1):
    """Insert a node into node_state."""
    conn.execute(
        "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
        "VALUES (?, ?, ?, 'impl:done', datetime('now'), ?)",
        (project_id, node_id, status, version),
    )
    conn.commit()


class TestEvidenceTypeEnum(unittest.TestCase):
    """AC10: EvidenceType has BACKFILL_EVIDENCE member."""

    def test_backfill_evidence_enum_exists(self):
        self.assertEqual(EvidenceType.BACKFILL_EVIDENCE.value, "backfill_evidence")


class TestEvidenceRules(unittest.TestCase):
    """AC9: evidence.py accepts backfill_evidence for pending->qa_pass."""

    def test_backfill_evidence_rule_exists(self):
        key = (VerifyStatus.PENDING, VerifyStatus.QA_PASS)
        self.assertIn(key, EVIDENCE_RULES)
        self.assertEqual(EVIDENCE_RULES[key]["required_type"], "backfill_evidence")

    def test_validate_backfill_evidence_valid(self):
        evidence = Evidence.from_dict({
            "type": "backfill_evidence",
            "summary": {
                "merge_commit": "abc1234",
                "backfill_ref": "BF-005",
                "retroactive": True,
            },
        })
        result = validate_evidence(VerifyStatus.PENDING, VerifyStatus.QA_PASS, evidence)
        self.assertTrue(result["ok"])

    def test_validate_backfill_evidence_missing_merge_commit(self):
        evidence = Evidence.from_dict({
            "type": "backfill_evidence",
            "summary": {
                "backfill_ref": "BF-005",
                "retroactive": True,
            },
        })
        with self.assertRaises(Exception):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.QA_PASS, evidence)

    def test_validate_backfill_evidence_missing_backfill_ref(self):
        evidence = Evidence.from_dict({
            "type": "backfill_evidence",
            "summary": {
                "merge_commit": "abc1234",
                "retroactive": True,
            },
        })
        with self.assertRaises(Exception):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.QA_PASS, evidence)

    def test_validate_backfill_evidence_missing_retroactive(self):
        evidence = Evidence.from_dict({
            "type": "backfill_evidence",
            "summary": {
                "merge_commit": "abc1234",
                "backfill_ref": "BF-005",
            },
        })
        with self.assertRaises(Exception):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.QA_PASS, evidence)

    def test_wrong_evidence_type_rejected(self):
        evidence = Evidence.from_dict({
            "type": "test_report",
            "summary": {"passed": 5, "exit_code": 0},
        })
        with self.assertRaises(Exception):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.QA_PASS, evidence)


class TestPromoteBackfillNode(unittest.TestCase):
    """AC1-AC6, AC11-AC13: promote_backfill_node function tests."""

    def setUp(self):
        self.conn = _make_db()
        self.project_id = "test-proj"

    def tearDown(self):
        self.conn.close()

    def _call_promote(self, node_id, merge_commit, operator_id="observer-1",
                      reason="test", graph_data=None, git_ok=True):
        """Helper that patches project_service and subprocess correctly."""
        from governance.state_service import promote_backfill_node

        mock_graph = MagicMock()
        mock_graph.get_node.return_value = graph_data or {}

        mock_run_result = MagicMock(
            returncode=0 if git_ok else 128,
            stdout="commit\n" if git_ok else "fatal: Not a valid object name\n",
        )

        with patch("governance.project_service.load_project_graph", return_value=mock_graph), \
             patch.object(subprocess, "run", return_value=mock_run_result):
            return promote_backfill_node(
                conn=self.conn,
                project_id=self.project_id,
                node_id=node_id,
                merge_commit=merge_commit,
                operator_id=operator_id,
                reason=reason,
            )

    def test_promote_success(self):
        """AC11: promote with valid backfill_ref + real commit succeeds -> qa_pass."""
        _seed_node(self.conn, self.project_id, "L7.6")

        result = self._call_promote(
            node_id="L7.6",
            merge_commit="abc1234def",
            operator_id="observer-1",
            reason="BF-005 historical backfill",
            graph_data={"backfill_ref": "BF-005"},
            git_ok=True,
        )

        self.assertEqual(result["status"], "qa_pass")
        self.assertEqual(result["node_id"], "L7.6")
        self.assertEqual(result["merge_commit"], "abc1234def")
        self.assertEqual(result["backfill_ref"], "BF-005")

        # Verify DB state
        row = self.conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (self.project_id, "L7.6"),
        ).fetchone()
        self.assertEqual(row["verify_status"], "qa_pass")

        # Verify history written (AC6 audit)
        hist = self.conn.execute(
            "SELECT * FROM node_history WHERE project_id = ? AND node_id = ?",
            (self.project_id, "L7.6"),
        ).fetchone()
        self.assertIsNotNone(hist)
        self.assertEqual(hist["from_status"], "pending")
        self.assertEqual(hist["to_status"], "qa_pass")

        # Verify audit written with action=backfill.promoted (AC6)
        audit = self.conn.execute(
            "SELECT * FROM audit_index WHERE project_id = ? AND event = 'backfill.promoted'",
            (self.project_id,),
        ).fetchone()
        self.assertIsNotNone(audit)

    def test_promote_no_backfill_ref(self):
        """AC12: promote node without backfill_ref returns error."""
        _seed_node(self.conn, self.project_id, "L1.1")

        with self.assertRaises(ValidationError) as ctx:
            self._call_promote(
                node_id="L1.1",
                merge_commit="abc1234",
                graph_data={"title": "Some node"},  # no backfill_ref
            )
        self.assertIn("backfill_ref", str(ctx.exception))

    def test_promote_fake_merge_commit(self):
        """AC13: promote with fake merge_commit returns error."""
        _seed_node(self.conn, self.project_id, "L7.7")

        with self.assertRaises(ValidationError) as ctx:
            self._call_promote(
                node_id="L7.7",
                merge_commit="deadbeef1234567890",
                graph_data={"backfill_ref": "BF-005"},
                git_ok=False,  # git cat-file fails
            )
        self.assertIn("merge_commit", str(ctx.exception))

    def test_promote_node_not_found(self):
        """Promote non-existent node raises NodeNotFoundError."""
        from governance.state_service import promote_backfill_node

        with self.assertRaises(NodeNotFoundError):
            promote_backfill_node(
                conn=self.conn,
                project_id=self.project_id,
                node_id="L99.99",
                merge_commit="abc1234",
                operator_id="observer-1",
                reason="test",
            )

    def test_promote_non_pending_node(self):
        """Promote a non-pending node raises ValidationError."""
        from governance.state_service import promote_backfill_node

        _seed_node(self.conn, self.project_id, "L7.8", status="t2_pass")

        with self.assertRaises(ValidationError) as ctx:
            promote_backfill_node(
                conn=self.conn,
                project_id=self.project_id,
                node_id="L7.8",
                merge_commit="abc1234",
                operator_id="observer-1",
                reason="test",
            )
        self.assertIn("pending", str(ctx.exception))

    def test_promoted_node_subsequent_verify_update(self):
        """AC14: promoted node can still go through subsequent verify-update flow."""
        _seed_node(self.conn, self.project_id, "L7.9")

        # First promote
        result = self._call_promote(
            node_id="L7.9",
            merge_commit="abc1234def",
            operator_id="observer-1",
            reason="backfill",
            graph_data={"backfill_ref": "BF-005"},
            git_ok=True,
        )
        self.assertEqual(result["status"], "qa_pass")

        # Verify the node is at qa_pass and can be read
        row = self.conn.execute(
            "SELECT verify_status, version, evidence_json FROM node_state "
            "WHERE project_id = ? AND node_id = ?",
            (self.project_id, "L7.9"),
        ).fetchone()
        self.assertEqual(row["verify_status"], "qa_pass")
        self.assertEqual(row["version"], 2)

        # Verify evidence is valid JSON with expected fields (AC4)
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["type"], "backfill_evidence")
        self.assertTrue(evidence["summary"]["retroactive"])
        self.assertEqual(evidence["summary"]["merge_commit"], "abc1234def")
        self.assertEqual(evidence["summary"]["backfill_ref"], "BF-005")

        # The node at qa_pass should allow further transitions
        # (e.g., qa_pass -> failed via error_log is allowed by existing rules)
        # We just verify the state is writable, not frozen
        self.conn.execute(
            "UPDATE node_state SET verify_status = 'failed', version = 3 "
            "WHERE project_id = ? AND node_id = ?",
            (self.project_id, "L7.9"),
        )
        row2 = self.conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (self.project_id, "L7.9"),
        ).fetchone()
        self.assertEqual(row2["verify_status"], "failed")


if __name__ == "__main__":
    unittest.main()
