"""Tests for Phase 4: Conflict rule engine."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestConflictRules(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_conn(self, project_id="test-project"):
        from governance.db import get_connection
        return get_connection(project_id)

    def test_extract_operation_type(self):
        from governance.conflict_rules import extract_operation_type
        self.assertEqual(extract_operation_type("Add a new login page"), "add")
        self.assertEqual(extract_operation_type("Delete old auth module"), "delete")
        self.assertEqual(extract_operation_type("Refactor the database layer"), "refactor")
        self.assertEqual(extract_operation_type("Run all tests"), "test")
        self.assertEqual(extract_operation_type("修改用户界面"), "modify")
        self.assertEqual(extract_operation_type("添加新功能"), "add")
        self.assertEqual(extract_operation_type("删除旧代码"), "delete")
        self.assertEqual(extract_operation_type("Something generic"), "modify")  # default

    def test_compute_intent_hash(self):
        from governance.conflict_rules import compute_intent_hash
        h1 = compute_intent_hash("Add a login page")
        h2 = compute_intent_hash("add a login page")
        h3 = compute_intent_hash("  Add   a   login   page  ")
        # Same content, different casing/whitespace → same hash
        self.assertEqual(h1, h2)
        self.assertEqual(h1, h3)
        # Different content → different hash
        h4 = compute_intent_hash("Delete the login page")
        self.assertNotEqual(h1, h4)

    def test_new_task_no_conflicts(self):
        from governance.conflict_rules import check_conflicts, compute_intent_hash
        conn = self._get_conn()
        try:
            result = check_conflicts(
                conn, "test-project",
                target_files=["agent/auth.py"],
                operation_type="add",
                intent_hash=compute_intent_hash("Add JWT authentication"),
                prompt="Add JWT authentication",
            )
            self.assertEqual(result["decision"], "new")
        finally:
            conn.close()

    def test_duplicate_detection(self):
        """Same intent within 1h → duplicate."""
        from governance.conflict_rules import check_conflicts, compute_intent_hash
        from governance import task_registry
        conn = self._get_conn()
        try:
            # Create an existing task
            task_registry.create_task(
                conn, "test-project",
                prompt="Add JWT authentication",
                task_type="dev",
                created_by="test",
            )
            # Try to create a duplicate
            result = check_conflicts(
                conn, "test-project",
                target_files=["agent/auth.py"],
                operation_type="add",
                intent_hash=compute_intent_hash("Add JWT authentication"),
                prompt="Add JWT authentication",
            )
            self.assertEqual(result["decision"], "duplicate")
            self.assertIn("existing_task_id", result["details"])
        finally:
            conn.close()

    def test_conflict_same_file_opposite_op(self):
        """Same file + opposite operation → conflict."""
        from governance.conflict_rules import check_conflicts, compute_intent_hash
        from governance import task_registry
        conn = self._get_conn()
        try:
            # Create a task that adds to auth.py
            task_registry.create_task(
                conn, "test-project",
                prompt="Add new auth middleware",
                task_type="dev",
                created_by="test",
                metadata={"target_files": ["agent/auth.py"], "operation_type": "add"},
            )
            # Try to delete from the same file
            result = check_conflicts(
                conn, "test-project",
                target_files=["agent/auth.py"],
                operation_type="delete",
                intent_hash=compute_intent_hash("Delete auth middleware"),
                prompt="Delete auth middleware",
            )
            self.assertEqual(result["decision"], "conflict")
            self.assertIn("overlapping_files", result["details"])
        finally:
            conn.close()

    def test_dependency_queue(self):
        """Upstream dependency not succeeded → queue."""
        from governance.conflict_rules import check_conflicts, compute_intent_hash
        from governance import task_registry
        conn = self._get_conn()
        try:
            # Create upstream task (queued, not succeeded)
            upstream = task_registry.create_task(
                conn, "test-project",
                prompt="Setup database schema",
                task_type="dev",
                created_by="test",
            )
            upstream_id = upstream["task_id"]

            result = check_conflicts(
                conn, "test-project",
                target_files=["agent/models.py"],
                operation_type="add",
                intent_hash=compute_intent_hash("Add user model"),
                prompt="Add user model",
                depends_on=[upstream_id],
            )
            self.assertEqual(result["decision"], "queue")
            self.assertEqual(result["details"]["blocked_by"], upstream_id)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
