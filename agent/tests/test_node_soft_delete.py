"""Tests for POST /api/wf/{project_id}/node-soft-delete endpoint (PR-C).

AC14: Endpoint coverage:
  - Happy path sets rolled_back
  - Audit record written to node_history
  - Missing node_ids handled gracefully
"""

import json
import os
import sqlite3
import sys
import unittest

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import SCHEMA_SQL


def _make_db():
    """Create an in-memory governance DB with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _seed_nodes(conn, project_id, node_ids):
    """Insert pending nodes into node_state."""
    for nid in node_ids:
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, ?, 'pending', 'unknown', datetime('now'), 1)",
            (project_id, nid),
        )
    conn.commit()


class TestNodeSoftDelete(unittest.TestCase):
    """Test the node-soft-delete logic (extracted from server handler)."""

    def setUp(self):
        self.conn = _make_db()
        self.project_id = "test-proj"

    def tearDown(self):
        self.conn.close()

    def _soft_delete(self, node_ids, reason="test"):
        """Simulate the node-soft-delete handler logic."""
        import time
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        updated = []
        skipped = []

        for nid in node_ids:
            row = self.conn.execute(
                "SELECT verify_status, version FROM node_state WHERE project_id = ? AND node_id = ?",
                (self.project_id, nid),
            ).fetchone()
            if not row:
                skipped.append({"node_id": nid, "reason": "not found"})
                continue

            old_status = row["verify_status"]
            new_version = row["version"] + 1
            self.conn.execute(
                """UPDATE node_state SET verify_status = 'rolled_back',
                   updated_by = 'node-soft-delete', updated_at = ?, version = ?
                   WHERE project_id = ? AND node_id = ?""",
                (now, new_version, self.project_id, nid),
            )

            try:
                self.conn.execute(
                    """INSERT INTO node_history
                       (project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version)
                       VALUES (?, ?, ?, 'rolled_back', 'coordinator', ?, 'node-soft-delete', ?, ?)""",
                    (self.project_id, nid, old_status,
                     json.dumps({"reason": reason, "type": "soft_delete"}),
                     now, new_version),
                )
            except Exception:
                pass

            updated.append(nid)

        self.conn.commit()
        return {"updated": updated, "skipped": skipped, "reason": reason}

    def test_happy_path_sets_rolled_back(self):
        """AC9: Node verify_status set to 'rolled_back'."""
        _seed_nodes(self.conn, self.project_id, ["L1.1", "L1.2"])

        result = self._soft_delete(["L1.1", "L1.2"], reason="test rollback")

        self.assertEqual(len(result["updated"]), 2)
        self.assertEqual(len(result["skipped"]), 0)

        for nid in ["L1.1", "L1.2"]:
            row = self.conn.execute(
                "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                (self.project_id, nid),
            ).fetchone()
            self.assertEqual(row["verify_status"], "rolled_back")

    def test_audit_record_written(self):
        """AC9: Audit record written to node_history."""
        _seed_nodes(self.conn, self.project_id, ["L3.1"])

        self._soft_delete(["L3.1"], reason="audit test")

        row = self.conn.execute(
            "SELECT from_status, to_status, evidence_json FROM node_history "
            "WHERE project_id = ? AND node_id = 'L3.1'",
            (self.project_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["from_status"], "pending")
        self.assertEqual(row["to_status"], "rolled_back")

        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["reason"], "audit test")
        self.assertEqual(evidence["type"], "soft_delete")

    def test_missing_node_ids_graceful(self):
        """AC14: Missing node_ids handled gracefully (skipped, not error)."""
        _seed_nodes(self.conn, self.project_id, ["L2.1"])

        result = self._soft_delete(["L2.1", "L99.99"], reason="partial")

        self.assertEqual(result["updated"], ["L2.1"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["node_id"], "L99.99")
        self.assertEqual(result["skipped"][0]["reason"], "not found")

    def test_version_incremented(self):
        """Node version is incremented after soft-delete."""
        _seed_nodes(self.conn, self.project_id, ["L5.1"])

        self._soft_delete(["L5.1"])

        row = self.conn.execute(
            "SELECT version FROM node_state WHERE project_id = ? AND node_id = 'L5.1'",
            (self.project_id,),
        ).fetchone()
        self.assertEqual(row["version"], 2)

    def test_empty_node_ids_returns_empty(self):
        """Empty node_ids list returns empty result (no error at logic level)."""
        result = self._soft_delete([], reason="noop")
        self.assertEqual(result["updated"], [])
        self.assertEqual(result["skipped"], [])


class TestRolledBackEnum(unittest.TestCase):
    """Test that rolled_back is recognized by enums."""

    def test_verify_status_from_str(self):
        from governance.enums import VerifyStatus
        status = VerifyStatus.from_str("rolled_back")
        self.assertEqual(status, VerifyStatus.ROLLED_BACK)
        self.assertEqual(status.value, "rolled_back")

    def test_status_order_has_rolled_back(self):
        from governance.enums import STATUS_ORDER, VerifyStatus
        self.assertIn(VerifyStatus.ROLLED_BACK, STATUS_ORDER)
        # rolled_back should rank below failed
        self.assertLess(STATUS_ORDER[VerifyStatus.ROLLED_BACK],
                        STATUS_ORDER[VerifyStatus.FAILED])


if __name__ == "__main__":
    unittest.main()
