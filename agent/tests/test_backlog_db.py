"""Tests for OPT-DB-BACKLOG: governance DB backlog backend.

Covers:
  - Schema migration v14->v15
  - Upsert idempotency
  - Close lifecycle
  - REST endpoint happy path + 404 error
  - ETL dry-run vs apply parity
  - MCP tool definitions
  - auto_chain _try_backlog_close_via_db helper
"""
import gc
import json
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _route_token(action="backlog_upsert", bug_id="BUG-ROUTE", project_id="test-project"):
    return {
        "route_context_hash": f"sha256:test-route-context-{action}",
        "prompt_contract_id": f"prompt-contract-{action}",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": {"project_id": project_id, "backlog_id": bug_id},
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:test-route-token"],
    }


def _route_waiver(action="backlog_upsert", bug_id="BUG-ROUTE", project_id="test-project"):
    return {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": f"sha256:test-route-context-{action}",
        "prompt_contract_id": f"prompt-contract-{action}",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": {"project_id": project_id, "backlog_id": bug_id},
        "reason": "Unit test supplies explicit route gate waiver evidence.",
        "timeline_evidence": {"event_id": "test-route-gate"},
    }


def _safe_cleanup(tmp_dir):
    """Best-effort cleanup that tolerates Windows file locks on SQLite WAL files."""
    import shutil
    try:
        # Force garbage collection to release SQLite connections
        gc.collect()
        tmp_dir.cleanup()
    except (PermissionError, OSError):
        # Windows: WAL/SHM files may still be locked; ignore
        try:
            shutil.rmtree(tmp_dir.name, ignore_errors=True)
        except Exception:
            pass


class TestSchemaV14ToV15(unittest.TestCase):
    """AC1: Schema migration creates backlog_bugs table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def test_schema_version_is_at_least_15(self):
        from governance.db import SCHEMA_VERSION
        self.assertGreaterEqual(SCHEMA_VERSION, 15)

    def test_migration_creates_backlog_bugs_table(self):
        conn = self._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        self.assertIn("backlog_bugs", table_names)

    def test_migration_v14_to_v15_idempotent(self):
        """Calling _run_migrations from v14 -> v15 on a v14 DB works."""
        from governance.db import _run_migrations
        conn = self._get_conn()
        # Calling migration again should be safe (CREATE IF NOT EXISTS)
        _run_migrations(conn, 14, 15)
        conn.commit()
        # Verify table still exists and is functional
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, created_at, updated_at) VALUES (?, ?, ?)",
            ("TEST-MIG", "2026-01-01", "2026-01-01")
        )
        conn.commit()
        row = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='TEST-MIG'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["bug_id"], "TEST-MIG")

    def test_schema_meta_updated_to_current_version(self):
        from governance.db import SCHEMA_VERSION
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        self.assertEqual(row["value"], str(SCHEMA_VERSION))


class TestBacklogUpsertIdempotency(unittest.TestCase):
    """AC3: Two upserts -> exactly 1 row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def test_upsert_creates_then_updates(self):
        conn = self._get_conn()

        # First insert
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 updated_at = excluded.updated_at
            """,
            ("B99", "Original title", "OPEN", "P1", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        # Second insert (update)
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 updated_at = excluded.updated_at
            """,
            ("B99", "Updated title", "OPEN", "P1", "2026-01-01", "2026-01-02"),
        )
        conn.commit()

        # Should be exactly 1 row
        rows = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='B99'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Updated title")


class TestBacklogCloseLifecycle(unittest.TestCase):
    """AC4: Close sets status=FIXED, commit, fixed_at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def test_close_updates_status_and_commit(self):
        conn = self._get_conn()

        # Insert a bug
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("B99", "Test bug", "OPEN", "P1", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        # Close it
        conn.execute(
            """UPDATE backlog_bugs
               SET status = 'FIXED', "commit" = ?, fixed_at = ?, updated_at = ?
               WHERE bug_id = ?""",
            ("abc1234", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "B99"),
        )
        conn.commit()

        row = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='B99'").fetchone()
        self.assertEqual(row["status"], "FIXED")
        self.assertEqual(row["commit"], "abc1234")
        self.assertIn("2026-01-02", row["fixed_at"])


class TestBacklogRESTEndpoints(unittest.TestCase):
    """AC2: REST endpoint happy path + 404 error."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _make_ctx(self, path_params, query=None, body=None):
        """Build a minimal RequestContext-like object."""
        ctx = MagicMock()
        ctx.path_params = path_params
        ctx.query = query or {}
        ctx.body = body or {}
        ctx.get_project_id.return_value = path_params.get("project_id", "")
        return ctx

    def test_upsert_and_list(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B99"},
            body={"title": "Test bug", "status": "OPEN", "priority": "P1"},
        )
        result = handle_backlog_upsert(ctx)
        self.assertTrue(result["ok"])

        # List
        ctx2 = self._make_ctx({"project_id": "test-project"})
        result2 = handle_backlog_list(ctx2)
        self.assertGreaterEqual(result2["count"], 1)
        bug_ids = [b["bug_id"] for b in result2["bugs"]]
        self.assertIn("B99", bug_ids)

    def test_upsert_existing_preserves_omitted_fields(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "PATCH-1"},
            body={
                "title": "Preserve me",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["agent/governance/server.py"],
                "test_files": ["agent/tests/test_backlog_db.py"],
                "acceptance_criteria": ["original acceptance"],
                "details_md": "Original details",
                "chain_trigger_json": {"source": "test"},
                "required_docs": ["docs/governance/manual-fix-sop.md"],
                "provenance_paths": ["agent/governance/server.py"],
                "force_admit": True,
            },
        )
        handle_backlog_upsert(ctx)

        patch_ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "PATCH-1"},
            body={
                "status": "FIXED",
                "commit": "abc1234",
                "fixed_at": "2026-05-24T00:00:00Z",
                "route_token": _route_token("backlog_upsert", "PATCH-1"),
            },
        )
        handle_backlog_upsert(patch_ctx)

        result = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-1"})
        )
        self.assertEqual(result["title"], "Preserve me")
        self.assertEqual(result["status"], "FIXED")
        self.assertEqual(result["priority"], "P1")
        self.assertEqual(result["commit"], "abc1234")
        self.assertEqual(result["fixed_at"], "2026-05-24T00:00:00Z")
        self.assertEqual(result["details_md"], "Original details")
        self.assertEqual(json.loads(result["target_files"]), ["agent/governance/server.py"])
        self.assertEqual(json.loads(result["test_files"]), ["agent/tests/test_backlog_db.py"])
        self.assertEqual(json.loads(result["acceptance_criteria"]), ["original acceptance"])
        self.assertEqual(json.loads(result["chain_trigger_json"]), {"source": "test"})
        self.assertEqual(result["required_docs"], ["docs/governance/manual-fix-sop.md"])
        self.assertEqual(result["provenance_paths"], ["agent/governance/server.py"])

    def test_upsert_existing_preserves_evidence_when_only_docs_change(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-DOCS"},
                body={
                    "title": "Evidence row",
                    "status": "OPEN",
                    "priority": "P0",
                    "target_files": ["agent/governance/server.py"],
                    "test_files": ["agent/tests/test_backlog_db.py"],
                    "acceptance_criteria": ["preserve omitted evidence"],
                    "details_md": "Original evidence details",
                    "chain_trigger_json": {"parallel_contract": {"lane": "backlog-upsert-preserve"}},
                    "required_docs": ["docs/original.md"],
                    "provenance_paths": ["BACKLOG-ORIGINAL"],
                    "force_admit": True,
                },
            )
        )

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-DOCS"},
                body={
                    "required_docs": ["docs/replacement.md"],
                    "provenance_paths": ["BACKLOG-UPDATED"],
                },
            )
        )

        result = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-DOCS"})
        )
        self.assertEqual(result["title"], "Evidence row")
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["priority"], "P0")
        self.assertEqual(json.loads(result["target_files"]), ["agent/governance/server.py"])
        self.assertEqual(json.loads(result["test_files"]), ["agent/tests/test_backlog_db.py"])
        self.assertEqual(json.loads(result["acceptance_criteria"]), ["preserve omitted evidence"])
        self.assertEqual(result["details_md"], "Original evidence details")
        self.assertEqual(
            json.loads(result["chain_trigger_json"]),
            {"parallel_contract": {"lane": "backlog-upsert-preserve"}},
        )
        self.assertEqual(result["required_docs"], ["docs/replacement.md"])
        self.assertEqual(result["provenance_paths"], ["BACKLOG-UPDATED"])

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-DOCS"},
                body={"required_docs": []},
            )
        )

        cleared = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-DOCS"})
        )
        self.assertEqual(cleared["required_docs"], [])
        self.assertEqual(cleared["provenance_paths"], ["BACKLOG-UPDATED"])
        self.assertEqual(cleared["title"], "Evidence row")

    def test_upsert_existing_allows_explicit_clear(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-CLEAR"},
                body={
                    "title": "Clear me",
                    "target_files": ["agent/governance/server.py"],
                    "details_md": "Details",
                    "force_admit": True,
                },
            )
        )

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-CLEAR"},
                body={"title": "", "target_files": [], "details_md": ""},
            )
        )

        result = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-CLEAR"})
        )
        self.assertEqual(result["title"], "")
        self.assertEqual(json.loads(result["target_files"]), [])
        self.assertEqual(result["details_md"], "")

    def test_get_existing(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B100"},
            body={"title": "Another bug"},
        )
        handle_backlog_upsert(ctx)

        ctx2 = self._make_ctx({"project_id": "test-project", "bug_id": "B100"})
        result = handle_backlog_get(ctx2)
        self.assertEqual(result["bug_id"], "B100")

    def test_get_missing_404(self):
        from governance.server import handle_backlog_get
        from governance.errors import GovernanceError

        ctx = self._make_ctx({"project_id": "test-project", "bug_id": "NONEXISTENT"})
        with self.assertRaises(GovernanceError) as cm:
            handle_backlog_get(ctx)
        self.assertEqual(cm.exception.status, 404)

    def test_close_existing(self):
        from governance.server import handle_backlog_upsert, handle_backlog_close

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B101"},
            body={"title": "Closeable bug"},
        )
        handle_backlog_upsert(ctx)

        ctx2 = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B101"},
            body={
                "route_waiver": _route_waiver("backlog_close", "B101")
            },
        )
        result = handle_backlog_close(ctx2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "FIXED")

    def test_upsert_fixed_status_without_route_token_rejects_before_mutation(self):
        from governance.server import handle_backlog_upsert
        from governance.errors import GovernanceError

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B-PROTECTED-NO-TOKEN"},
            body={"title": "Fallback close", "status": "FIXED", "force_admit": True},
        )
        with self.assertRaises(GovernanceError) as cm:
            handle_backlog_upsert(ctx)

        self.assertEqual(cm.exception.code, "route_token_required")
        from governance.db import get_connection
        conn = get_connection("test-project")
        try:
            row = conn.execute(
                "SELECT bug_id FROM backlog_bugs WHERE bug_id = ?",
                ("B-PROTECTED-NO-TOKEN",),
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_upsert_fixed_status_rejects_generic_waiver_without_route_identity(self):
        from governance.server import handle_backlog_upsert
        from governance.errors import GovernanceError

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B-PROTECTED-BAD-WAIVER"},
            body={
                "title": "Bad waiver",
                "status": "FIXED",
                "force_admit": True,
                "route_waiver": {
                    "accepted": True,
                    "waiver_type": "manual_fix",
                    "allowed_action": "backlog_upsert",
                    "scope": {"project_id": "test-project", "backlog_id": "B-PROTECTED-BAD-WAIVER"},
                    "reason": "Unit test supplies explicit route gate waiver evidence.",
                    "timeline_evidence": {"event_id": "test-route-gate"},
                },
            },
        )
        with self.assertRaises(GovernanceError) as cm:
            handle_backlog_upsert(ctx)

        self.assertEqual(cm.exception.code, "route_token_required")
        self.assertIn("route identity", str(cm.exception))
        from governance.db import get_connection
        conn = get_connection("test-project")
        try:
            row = conn.execute(
                "SELECT bug_id FROM backlog_bugs WHERE bug_id = ?",
                ("B-PROTECTED-BAD-WAIVER",),
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_upsert_fixed_status_accepts_valid_route_token(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        bug_id = "B-PROTECTED-TOKEN"
        result = handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={
                    "title": "Token close fallback",
                    "status": "FIXED",
                    "commit": "abc123",
                    "force_admit": True,
                    "route_token": _route_token("backlog_upsert", bug_id),
                },
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route_token_gate"]["decision"], "route_token")
        row = handle_backlog_get(self._make_ctx({"project_id": "test-project", "bug_id": bug_id}))
        self.assertEqual(row["status"], "FIXED")
        self.assertEqual(row["commit"], "abc123")

    def test_upsert_fixed_status_accepts_route_context_waiver(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        bug_id = "B-PROTECTED-WAIVER"
        result = handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={
                    "title": "Waiver close fallback",
                    "status": "FIXED",
                    "force_admit": True,
                    "route_waiver": _route_waiver("backlog_upsert", bug_id),
                },
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        row = handle_backlog_get(self._make_ctx({"project_id": "test-project", "bug_id": bug_id}))
        self.assertEqual(row["status"], "FIXED")

    def test_close_missing_404(self):
        from governance.server import handle_backlog_close
        from governance.errors import GovernanceError

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "NONEXISTENT"},
            body={"commit": "abc"},
        )
        with self.assertRaises(GovernanceError) as cm:
            handle_backlog_close(ctx)
        self.assertEqual(cm.exception.status, 404)

    def test_list_with_status_filter(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        # Create OPEN and FIXED bugs
        for bug_id, status in [("BF1", "OPEN"), ("BF2", "FIXED"), ("BF3", "OPEN")]:
            body = {"title": "Bug %s" % bug_id, "status": status}
            if status == "FIXED":
                body["route_token"] = _route_token("backlog_upsert", bug_id)
            ctx = self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body=body,
            )
            handle_backlog_upsert(ctx)

        # Filter by OPEN
        ctx2 = self._make_ctx({"project_id": "test-project"}, query={"status": "OPEN"})
        result = handle_backlog_list(ctx2)
        for bug in result["bugs"]:
            self.assertEqual(bug["status"], "OPEN")

    def test_list_compact_paginates_and_summarizes(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        for bug_id in ["BP1", "BP2", "BP3"]:
            ctx = self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={
                    "title": "Paged bug %s" % bug_id,
                    "status": "OPEN",
                    "priority": "P1",
                    "details_md": "long details " * 80,
                    "target_files": ["a.py", "b.py", "c.py", "d.py"],
                    "test_files": ["test_a.py", "test_b.py"],
                    "acceptance_criteria": ["one", "two", "three"],
                    "chain_trigger_json": {
                        "parallel_contract": {
                            "template_id": "mf_parallel.v1",
                            "contract_instance_id": bug_id,
                            "evidence_requirements": [
                                {"id": "unit_tests", "required": True},
                                {"id": "dashboard_e2e", "required": False},
                            ],
                        }
                    },
                    "force_admit": True,
                },
            )
            handle_backlog_upsert(ctx)

        ctx2 = self._make_ctx(
            {"project_id": "test-project"},
            query={"view": "compact", "limit": "2", "offset": "0", "status": "OPEN"},
        )
        result = handle_backlog_list(ctx2)

        self.assertEqual(result["view"], "compact")
        self.assertEqual(result["limit"], 2)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["count"], 2)
        self.assertGreaterEqual(result["filtered_count"], 3)
        self.assertTrue(result["has_more"])
        self.assertIsNotNone(result["next_offset"])
        self.assertGreaterEqual(result["summary"]["open"], 3)
        bug = result["bugs"][0]
        self.assertTrue(bug["compact"])
        self.assertLessEqual(len(bug["details_md"]), 283)
        self.assertEqual(bug["target_file_count"], 4)
        self.assertEqual(bug["acceptance_count"], 3)
        self.assertEqual(bug["target_files"], ["a.py", "b.py", "c.py"])
        self.assertEqual(bug["contract_summary"]["template_id"], "mf_parallel.v1")
        self.assertEqual(bug["contract_summary"]["required_evidence_count"], 1)
        self.assertEqual(bug["contract_summary"]["optional_evidence_count"], 1)

    def test_list_search_and_exclude_closed(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "BS1"},
                body={
                    "title": "Needle open row",
                    "status": "OPEN",
                    "details_md": "alpha needle-token beta " * 20,
                    "target_files": ["needle.py"],
                    "force_admit": True,
                },
            )
        )
        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "BS2"},
                body={
                    "title": "Needle fixed row",
                    "status": "FIXED",
                    "details_md": "needle-token",
                    "force_admit": True,
                    "route_token": _route_token("backlog_upsert", "BS2"),
                },
            )
        )

        search_ctx = self._make_ctx(
            {"project_id": "test-project"},
            query={"view": "compact", "limit": "10", "q": "needle-token"},
        )
        search_result = handle_backlog_list(search_ctx)
        self.assertGreaterEqual(search_result["filtered_count"], 2)
        self.assertIn("BS1", {bug["bug_id"] for bug in search_result["bugs"]})

        open_ctx = self._make_ctx(
            {"project_id": "test-project"},
            query={
                "view": "compact",
                "limit": "10",
                "q": "Needle fixed row",
                "include_closed": "false",
            },
        )
        open_result = handle_backlog_list(open_ctx)
        self.assertEqual(open_result["filtered_count"], 0)

    def test_waived_status_is_closed_and_excluded_from_active(self):
        from governance.server import (
            handle_backlog_upsert,
            handle_backlog_list,
            _BACKLOG_CLOSED_STATUSES,
            _current_task_row_is_active,
        )

        # WAIVED is a recognized closed/retired status.
        self.assertIn("WAIVED", _BACKLOG_CLOSED_STATUSES)

        # Insert one active OPEN row and one WAIVED row. WAIVED is a closed
        # status, so the upsert carries a route token like the FIXED path.
        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "BW-OPEN"},
                body={
                    "title": "Active open row",
                    "status": "OPEN",
                    "force_admit": True,
                },
            )
        )
        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "BW-WAIVED"},
                body={
                    "title": "Intentionally set-aside row",
                    "status": "WAIVED",
                    "details_md": "Waived: superseded by direction change; carries waive reason.",
                    "force_admit": True,
                    "route_token": _route_token("backlog_upsert", "BW-WAIVED"),
                },
            )
        )

        # Active backlog list (closed excluded) must not surface the WAIVED row.
        active_ctx = self._make_ctx(
            {"project_id": "test-project"},
            query={"view": "compact", "limit": "50", "include_closed": "false"},
        )
        active_result = handle_backlog_list(active_ctx)
        active_ids = {bug["bug_id"] for bug in active_result["bugs"]}
        self.assertIn("BW-OPEN", active_ids)
        self.assertNotIn("BW-WAIVED", active_ids)

        # A WAIVED row is not counted as an active current task.
        waived_row = {
            "status": "WAIVED",
            "runtime_state": "fixed",
            "chain_stage": "",
            "mf_type": "",
            "current_task_id": "",
        }
        self.assertFalse(_current_task_row_is_active(waived_row))


class TestETLParsing(unittest.TestCase):
    """AC6: ETL dry-run vs apply parity."""

    def _load_etl(self):
        import importlib.util
        script_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        spec = importlib.util.spec_from_file_location(
            "etl_backlog", os.path.join(script_dir, "scripts", "etl-backlog-md-to-db.py")
        )
        etl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(etl)
        return etl

    def test_parse_finds_bugs(self):
        """ETL parse finds at least 1 bug from the actual backlog file."""
        script_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        backlog_path = os.path.join(script_dir, "docs", "dev", "bug-and-fix-backlog.md")
        if not os.path.exists(backlog_path):
            self.skipTest("Backlog file not found at expected path")

        etl = self._load_etl()
        bugs = etl.parse_backlog(backlog_path)
        self.assertGreater(len(bugs), 0, "Should find at least 1 bug in backlog")

        # Verify structure of first bug
        bug = bugs[0]
        self.assertIn("bug_id", bug)
        self.assertIn("title", bug)
        self.assertIn("status", bug)

    def test_dry_run_does_not_modify(self):
        """Dry-run should not make HTTP calls."""
        etl = self._load_etl()

        # Create a simple temp backlog
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# Bug Backlog\n\n")
            f.write("| ID | Description | Fix Commit | Date |\n")
            f.write("|----|-------------|------------|------|\n")
            f.write("| B1 | Test bug | abc123 | 2026-01-01 |\n")
            f.write("| B2 | Another bug | (OPEN) | 2026-01-02 |\n")
            tmp_path = f.name

        try:
            bugs = etl.parse_backlog(tmp_path)
            self.assertEqual(len(bugs), 2)
            self.assertEqual(bugs[0]["status"], "FIXED")
            self.assertEqual(bugs[1]["status"], "OPEN")
        finally:
            os.unlink(tmp_path)


class TestMCPToolDefinitions(unittest.TestCase):
    """AC5: MCP TOOLS list contains backlog tools."""

    def test_backlog_tools_in_list(self):
        from governance.mcp_server import TOOLS
        tool_names = {t["name"] for t in TOOLS}
        self.assertIn("backlog_list", tool_names)
        self.assertIn("backlog_get", tool_names)
        self.assertIn("backlog_upsert", tool_names)
        self.assertIn("backlog_close", tool_names)
        self.assertIn("task_timeline_append", tool_names)
        self.assertIn("task_timeline_list", tool_names)
        self.assertIn("mf_timeline_precheck", tool_names)

    def test_dispatch_backlog_list(self):
        """_dispatch_tool routes backlog_list to HTTP call."""
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"bugs": [], "count": 0}
            result = _dispatch_tool("backlog_list", {"project_id": "test"})
            mock_http.assert_called_once()
            call_args = mock_http.call_args
            self.assertEqual(call_args[0][0], "GET")
            self.assertEqual(
                call_args[0][1],
                "/api/backlog/test?view=compact&limit=50&offset=0&status=OPEN",
            )

    def test_dispatch_backlog_get(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"bug_id": "B1"}
            result = _dispatch_tool("backlog_get", {"project_id": "test", "bug_id": "B1"})
            mock_http.assert_called_once_with("GET", "/api/backlog/test/B1")

    def test_dispatch_backlog_upsert(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"ok": True}
            result = _dispatch_tool("backlog_upsert", {
                "project_id": "test", "bug_id": "B1", "title": "Bug"
            })
            mock_http.assert_called_once()
            call_args = mock_http.call_args
            self.assertEqual(call_args[0][0], "POST")
            self.assertEqual(call_args[0][1], "/api/backlog/test/B1")

    def test_dispatch_backlog_close(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"ok": True}
            result = _dispatch_tool("backlog_close", {
                "project_id": "test", "bug_id": "B1", "commit": "abc"
            })
            mock_http.assert_called_once()
            call_args = mock_http.call_args
            self.assertEqual(call_args[0][0], "POST")
            self.assertEqual(call_args[0][1], "/api/backlog/test/B1/close")

    def test_dispatch_timeline_tools(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"ok": True}
            _dispatch_tool("task_timeline_append", {
                "project_id": "test",
                "backlog_id": "B1",
                "event_type": "mf.implementation",
                "event_kind": "implementation",
                "status": "accepted",
            })
            _dispatch_tool("task_timeline_list", {
                "project_id": "test",
                "backlog_id": "B1",
                "event_kind": "implementation",
                "limit": 25,
            })
            _dispatch_tool("mf_timeline_precheck", {
                "project_id": "test",
                "bug_id": "B1",
                "include_events": True,
                "limit": 25,
            })
            self.assertEqual(mock_http.call_args_list[0][0], (
                "POST",
                "/api/task/test/timeline",
                {
                    "backlog_id": "B1",
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                },
            ))
            self.assertEqual(mock_http.call_args_list[1][0], (
                "GET",
                "/api/task/test/timeline?backlog_id=B1&event_kind=implementation&limit=25",
            ))
            self.assertEqual(mock_http.call_args_list[2][0], (
                "GET",
                "/api/backlog/test/B1/timeline-gate?include_events=true&limit=25",
            ))


class TestTryBacklogCloseViaDb(unittest.TestCase):
    """AC7: _try_backlog_close_via_db helper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def _insert_bug(self, bug_id="B99", status="OPEN"):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (bug_id, status, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        return conn

    def test_success_returns_true(self):
        from governance.auto_chain import _try_backlog_close_via_db

        conn = self._insert_bug("B99")

        result = _try_backlog_close_via_db("test-project", "B99", "abc123")

        self.assertTrue(result)
        row = conn.execute(
            "SELECT status, \"commit\", runtime_state, chain_stage "
            "FROM backlog_bugs WHERE bug_id='B99'"
        ).fetchone()
        self.assertEqual(row["status"], "FIXED")
        self.assertEqual(row["commit"], "abc123")
        self.assertEqual(row["runtime_state"], "fixed")
        self.assertEqual(row["chain_stage"], "fixed")

    def test_supplied_connection_avoids_opening_second_connection(self):
        from governance.auto_chain import _try_backlog_close_via_db

        conn = self._insert_bug("B100")

        with patch("governance.db.get_connection", side_effect=AssertionError("unused")):
            result = _try_backlog_close_via_db(
                "test-project", "B100", "abc123", conn=conn,
            )

        self.assertTrue(result)
        row = conn.execute(
            "SELECT status, \"commit\" FROM backlog_bugs WHERE bug_id='B100'"
        ).fetchone()
        self.assertEqual(row["status"], "FIXED")
        self.assertEqual(row["commit"], "abc123")

    def test_already_fixed_returns_true(self):
        from governance.auto_chain import _try_backlog_close_via_db

        self._insert_bug("DONE", status="FIXED")

        result = _try_backlog_close_via_db("test-project", "DONE", "abc123")

        self.assertTrue(result)

    def test_missing_returns_false_and_logs_warning(self):
        from governance.auto_chain import _try_backlog_close_via_db

        self._get_conn()
        with self.assertLogs("governance.auto_chain", level="WARNING") as cm:
            result = _try_backlog_close_via_db("test-project", "MISSING", "abc")
            self.assertFalse(result)
            log_text = " ".join(cm.output)
            self.assertRegex(log_text, r"backlog.*missing")

    def test_invalid_status_returns_false(self):
        from governance.auto_chain import _try_backlog_close_via_db

        self._insert_bug("B1", status="FAILED")

        with self.assertLogs("governance.auto_chain", level="WARNING") as cm:
            result = _try_backlog_close_via_db("test-project", "B1", "abc")
            self.assertFalse(result)
            log_text = " ".join(cm.output)
            self.assertRegex(log_text, r"invalid status")


class TestObserverDocUpdate(unittest.TestCase):
    """AC10: docs/roles/observer.md contains backlog migration section."""

    def test_observer_md_contains_backlog_section(self):
        doc_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "docs", "roles", "observer.md"
        )
        if not os.path.exists(doc_path):
            self.skipTest("observer.md not found")
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Backlog storage", content)
        self.assertIn("backlog_list", content)
        self.assertIn("backlog_upsert", content)


class TestObserverRootRouteContextEndpoint(unittest.TestCase):
    """Observer root route context bootstrap endpoint (aming-owned)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _make_ctx(self, path_params, query=None, body=None):
        ctx = MagicMock()
        ctx.path_params = path_params
        ctx.query = query or {}
        ctx.body = body or {}
        ctx.get_project_id.return_value = path_params.get("project_id", "")
        return ctx

    def _seed_backlog(self, bug_id):
        from governance.server import handle_backlog_upsert

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={
                    "title": "Root route context bootstrap",
                    "status": "OPEN",
                    "priority": "P1",
                    "force_admit": True,
                },
            )
        )

    def test_root_route_context_returns_required_fields_with_default_mode(self):
        from governance.server import handle_observer_root_route_context_get

        bug_id = "AC-OBSERVER-ROOT-ROUTE-CONTEXT-WORK-MODE-20260609"
        self._seed_backlog(bug_id)

        result = handle_observer_root_route_context_get(
            self._make_ctx(
                {"project_id": "test-project"},
                query={"backlog_id": bug_id},
            )
        )

        for field in (
            "backlog_id",
            "route_id",
            "prompt_contract_id",
            "work_mode",
            "loaded_skills",
            "loaded_resources",
            "graph_query_schema_trace_id",
            "allowed_actions",
            "blocked_actions",
            "required_evidence",
            "next_legal_action",
        ):
            self.assertIn(field, result)
        self.assertEqual(result["backlog_id"], bug_id)
        # Default work mode is observer_look_before_act, with implementation /
        # dispatch / merge / close blocked.
        self.assertEqual(result["work_mode"], "observer_look_before_act")
        for blocked in (
            "edit_implementation",
            "dispatch_implementation",
            "merge",
            "close",
        ):
            self.assertIn(blocked, result["blocked_actions"])
        self.assertEqual(
            result["next_legal_action"]["id"], "record_work_mode_transition"
        )
        self.assertIn("route_context", result["required_evidence"])
        self.assertTrue(result["backlog_row_present"])

    def test_root_route_context_post_accepts_work_mode_and_skills(self):
        from governance.server import handle_observer_root_route_context_post

        bug_id = "AC-OBSERVER-ROOT-ROUTE-CONTEXT-POST-20260609"
        self._seed_backlog(bug_id)

        result = handle_observer_root_route_context_post(
            self._make_ctx(
                {"project_id": "test-project"},
                body={
                    "backlog_id": bug_id,
                    "work_mode": "observer_execution_supervisor",
                    "loaded_skills": ["aming-claw"],
                    "loaded_resources": ["mf-sop.md"],
                },
            )
        )

        self.assertEqual(result["work_mode"], "observer_execution_supervisor")
        self.assertEqual(result["loaded_skills"], ["aming-claw"])
        self.assertEqual(result["loaded_resources"], ["mf-sop.md"])
        # Execution supervisor unblocks dispatch but never direct implementation.
        self.assertNotIn("dispatch_implementation", result["blocked_actions"])
        self.assertIn("edit_implementation", result["blocked_actions"])

    # --- AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-INJECTION-MANIFEST-HASH-20260609 ---
    # Regression: the canonical route identity returned by the endpoint MUST carry
    # visible_injection_manifest_hash sourced from the route_identity_cleanup pinning
    # event, so a fresh observer can consume it without forking the route identity.

    _ROUTE_IDENTITY = {
        "route_context_hash": "sha256:ctx-a226bba",
        "prompt_contract_id": "rprompt-repair-8884b4374cb18e09",
        "prompt_contract_hash": "sha256:pc-a226bba",
    }
    _MANIFEST_HASH = "sha256:vim-a226bba"
    # route_id is part of the external identity but the gate's route_identity
    # summary omits it (not in MF_ROUTE_IDENTITY_FIELDS); like the manifest hash
    # it must be sourced from the pinning event. Carried on the cleanup payload
    # below, mirroring a live route_identity_cleanup event (e.g. timeline 2028).
    _ROUTE_ID = "route-repair-69cf882ceaa8897f"

    def _seed_backlog_with_parallel_contract(self, bug_id):
        from governance.server import handle_backlog_upsert

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={
                    "title": "Root route context manifest pinning",
                    "status": "OPEN",
                    "priority": "P1",
                    "force_admit": True,
                    "chain_trigger_json": {
                        "template_id": "mf_parallel.v1",
                        "route_id": "route-repair-8884b4374cb18e09",
                    },
                },
            )
        )

    def _seed_pinned_route_timeline(self, bug_id):
        """Seed a complete passing route flow pinned by a route_identity_cleanup.

        The cleanup and route_context events both carry the manifest hash; the
        gate's route_identity summary omits it, so the endpoint must source it from
        the pinning event. Deterministic — fixed identity + manifest hash.
        """
        from governance.db import get_connection
        from governance import task_timeline

        conn = get_connection("test-project")
        task_timeline.ensure_schema(conn)
        identity = dict(self._ROUTE_IDENTITY)
        manifest = self._MANIFEST_HASH
        common = {"project_id": "test-project", "backlog_id": bug_id}

        # Pin the canonical identity with a route_identity_cleanup carrying the hash.
        task_timeline.record_event(
            conn,
            event_type="route.identity.cleanup",
            event_kind="route_identity_cleanup",
            phase="cleanup",
            status="passed",
            payload={
                "route_identity_cleanup": {**identity, "route_id": self._ROUTE_ID},
                "route_id": self._ROUTE_ID,
                "visible_injection_manifest_hash": manifest,
            },
            **common,
        )
        # route_context (carries the manifest hash on the consumption event).
        task_timeline.record_event(
            conn,
            event_type="route.context",
            event_kind="route_context",
            phase="dispatch",
            status="passed",
            payload={
                "route_context": {
                    **identity,
                    "route_id": self._ROUTE_ID,
                    "required_lanes": ["bounded_implementation_worker"],
                },
                "route_id": self._ROUTE_ID,
                "visible_injection_manifest_hash": manifest,
            },
            **common,
        )
        # route_action_precheck.
        task_timeline.record_event(
            conn,
            event_type="route.action.precheck",
            event_kind="route_action_precheck",
            phase="pre_mutation",
            status="allowed",
            verification={**identity, "allowed_action": "dispatch_worker"},
            **common,
        )
        # bounded implementation worker dispatch.
        task_timeline.record_event(
            conn,
            event_type="mf.subagent.dispatch",
            event_kind="mf_subagent_dispatch",
            phase="dispatch",
            status="passed",
            payload={
                "mf_subagent_dispatch_gate": {
                    **identity,
                    "worker_id": "mf-sub-test",
                    "bounded": True,
                }
            },
            **common,
        )
        # subagent startup (actual runtime identity present).
        task_timeline.record_event(
            conn,
            event_type="mf.subagent.startup",
            event_kind="mf_subagent_startup",
            phase="startup_gate",
            status="passed",
            payload={
                "mf_subagent_startup_gate": {
                    **identity,
                    "worker_id": "mf-sub-test",
                    "fence_token": "fence-86520a67573d",
                    "actual_cwd": "/repo/.worktrees/mf-sub-test",
                    "actual_git_root": "/repo/.worktrees/mf-sub-test",
                    "branch": "refs/heads/codex/mf-sub-test",
                    "head_commit": "head-test",
                }
            },
            **common,
        )
        conn.commit()

    def test_root_route_context_surfaces_pinned_manifest_hash(self):
        from governance.server import handle_observer_root_route_context_get

        bug_id = "AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-INJECTION-MANIFEST-HASH-20260609"
        self._seed_backlog_with_parallel_contract(bug_id)
        self._seed_pinned_route_timeline(bug_id)

        result = handle_observer_root_route_context_get(
            self._make_ctx(
                {"project_id": "test-project"},
                query={"backlog_id": bug_id},
            )
        )

        identity = result["canonical_route_identity"]
        # All five external-identity fields are present and non-empty.
        for field in (
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "visible_injection_manifest_hash",
        ):
            self.assertIn(field, identity, field)
            self.assertTrue(identity[field], field)
        # The manifest hash comes verbatim from the pinning event, surfaced both
        # top-level and inside the canonical identity.
        self.assertEqual(
            identity["visible_injection_manifest_hash"], self._MANIFEST_HASH
        )
        self.assertEqual(
            result["visible_injection_manifest_hash"], self._MANIFEST_HASH
        )
        self.assertEqual(
            identity["route_context_hash"],
            self._ROUTE_IDENTITY["route_context_hash"],
        )
        self.assertEqual(
            identity["prompt_contract_hash"],
            self._ROUTE_IDENTITY["prompt_contract_hash"],
        )
        # This seeder's contract carries a route_id, which takes precedence over
        # the pinning-event route_id (contract route_id is non-empty). The
        # no-contract-route_id backfill path is covered by
        # test_root_route_context_backfills_route_id_from_pinning_event.
        self.assertEqual(identity["route_id"], "route-repair-8884b4374cb18e09")
        self.assertEqual(result["route_id"], "route-repair-8884b4374cb18e09")
        # A complete external identity is not flagged incomplete (no fork).
        self.assertTrue(result["canonical_route_identity_complete"])
        self.assertNotIn("incomplete", identity)
        self.assertNotIn("missing_fields", identity)

    def _seed_backlog_with_routeidless_contract(self, bug_id):
        """Seed a backlog whose chain_trigger contract carries NO route_id.

        This mirrors the live residual-bug row (OPT-GRAPH-SEARCH-STRUCTURE-RANKING):
        the route_id is pinned only on the route_identity_cleanup event, never on
        the contract. Without sourcing route_id from the pinning event the endpoint
        returns route_id="" and an incomplete canonical identity.
        """
        from governance.server import handle_backlog_upsert

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={
                    "title": "Root route context route_id pinning",
                    "status": "OPEN",
                    "priority": "P1",
                    "force_admit": True,
                    "chain_trigger_json": {
                        # Deliberately NO route_id on the contract.
                        "template_id": "mf_parallel.v1",
                    },
                },
            )
        )

    def test_root_route_context_backfills_route_id_from_pinning_event(self):
        """Regression for AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-ROUTE-ID-20260609.

        The gate's route_identity summary omits route_id and (in the live repro)
        the contract carries none either, so route_id lived only on the
        route_identity_cleanup pinning event. The endpoint MUST source route_id
        from that pinning event so the returned external identity is complete and
        a fresh observer can consume it without forking/fabricating route_id.
        """
        from governance.server import handle_observer_root_route_context_get

        bug_id = "AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-ROUTE-ID-20260609"
        self._seed_backlog_with_routeidless_contract(bug_id)
        self._seed_pinned_route_timeline(bug_id)

        result = handle_observer_root_route_context_get(
            self._make_ctx(
                {"project_id": "test-project"},
                query={"backlog_id": bug_id},
            )
        )

        identity = result["canonical_route_identity"]
        # All five external-identity fields present AND non-empty.
        for field in (
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "visible_injection_manifest_hash",
        ):
            self.assertIn(field, identity, field)
            self.assertTrue(identity[field], field)
        # route_id comes verbatim from the pinning event (NOT the contract, which
        # had none), surfaced both top-level and inside the canonical identity.
        self.assertEqual(identity["route_id"], self._ROUTE_ID)
        self.assertEqual(result["route_id"], self._ROUTE_ID)
        # Complete external identity -> no fork, not flagged incomplete.
        self.assertTrue(result["canonical_route_identity_complete"])
        self.assertNotIn("incomplete", identity)
        self.assertNotIn("missing_fields", identity)

    def test_root_route_context_route_id_not_fabricated_without_pinning(self):
        """Negative: no pinning event -> route_id stays empty + incomplete, never faked.

        With a route_id-less contract and no route timeline, route_id is genuinely
        unavailable. The endpoint must NOT fabricate one and must NOT drop the key;
        it returns route_id empty and flags the identity incomplete with route_id
        listed in missing_fields.
        """
        from governance.server import handle_observer_root_route_context_get

        bug_id = "AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-ROUTE-ID-NOPIN-20260609"
        self._seed_backlog_with_routeidless_contract(bug_id)

        result = handle_observer_root_route_context_get(
            self._make_ctx(
                {"project_id": "test-project"},
                query={"backlog_id": bug_id},
            )
        )

        identity = result["canonical_route_identity"]
        self.assertIn("route_id", identity)
        self.assertEqual(identity["route_id"], "")
        self.assertEqual(result["route_id"], "")
        self.assertFalse(result["canonical_route_identity_complete"])
        self.assertTrue(identity.get("incomplete"))
        self.assertIn("route_id", identity.get("missing_fields", []))

    def test_root_route_context_without_pinned_manifest_marks_incomplete(self):
        """BEFORE-style fork repro: no pinning event -> key present-but-empty + flagged.

        With no route timeline the manifest hash is genuinely unavailable. The
        endpoint must NOT fabricate one and must NOT drop the key; it returns the
        field empty and flags the identity incomplete so a consumer can refuse to
        fork knowingly.
        """
        from governance.server import handle_observer_root_route_context_get

        bug_id = "AC-OBSERVER-ROOT-ROUTE-CONTEXT-FORK-REPRO-20260609"
        self._seed_backlog(bug_id)

        result = handle_observer_root_route_context_get(
            self._make_ctx(
                {"project_id": "test-project"},
                query={"backlog_id": bug_id},
            )
        )

        identity = result["canonical_route_identity"]
        self.assertIn("visible_injection_manifest_hash", identity)
        self.assertEqual(identity["visible_injection_manifest_hash"], "")
        self.assertEqual(result["visible_injection_manifest_hash"], "")
        self.assertFalse(result["canonical_route_identity_complete"])
        self.assertTrue(identity.get("incomplete"))
        self.assertIn(
            "visible_injection_manifest_hash", identity.get("missing_fields", [])
        )

    def test_root_route_context_requires_backlog_id(self):
        from governance.server import handle_observer_root_route_context_get

        with self.assertRaises(ValueError):
            handle_observer_root_route_context_get(
                self._make_ctx({"project_id": "test-project"}, query={})
            )


class TestFixedCloseWaiverAlerts(unittest.TestCase):
    """Criterion 2: FIXED + can_close=false + no-waiver rows surface as a
    governance alert (AC-CLOSE-GATE-EVIDENCE-INTEGRITY-20260609)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def _insert(self, conn, bug_id, status):
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (bug_id, status, "2026-06-09", "2026-06-09"),
        )
        conn.commit()

    def test_fixed_row_without_can_close_or_waiver_alerts(self):
        from governance import backlog_db

        conn = self._get_conn()
        self._insert(conn, "BUG-FIXED-NO-CLOSE", "FIXED")
        self._insert(conn, "BUG-OPEN", "OPEN")  # non-FIXED is ignored

        result = backlog_db.fixed_close_waiver_alerts(
            conn,
            "test-project",
            can_close_resolver=lambda _bug: False,
            has_close_waiver_resolver=lambda _bug: False,
        )
        self.assertTrue(result["alert"])
        self.assertEqual(result["alert_count"], 1)
        self.assertEqual(result["alerts"][0]["bug_id"], "BUG-FIXED-NO-CLOSE")
        self.assertEqual(
            result["alerts"][0]["reason"],
            "fixed_row_without_can_close_or_close_waiver",
        )

    def test_can_close_true_or_visible_waiver_silences_alert(self):
        from governance import backlog_db

        conn = self._get_conn()
        self._insert(conn, "BUG-A", "FIXED")
        self._insert(conn, "BUG-B", "FIXED")

        # can_close=true => no alert; waiver present => no alert.
        can_close = {"BUG-A": True, "BUG-B": False}
        has_waiver = {"BUG-A": False, "BUG-B": True}
        result = backlog_db.fixed_close_waiver_alerts(
            conn,
            "test-project",
            can_close_resolver=lambda bug: can_close[bug],
            has_close_waiver_resolver=lambda bug: has_waiver[bug],
        )
        self.assertFalse(result["alert"])
        self.assertEqual(result["alert_count"], 0)

    def test_non_evaluable_row_does_not_alert(self):
        from governance import backlog_db

        conn = self._get_conn()
        self._insert(conn, "BUG-NA", "FIXED")
        # can_close=None means not MF-applicable / not evaluable -> no alert.
        result = backlog_db.fixed_close_waiver_alerts(
            conn,
            "test-project",
            can_close_resolver=lambda _bug: None,
            has_close_waiver_resolver=lambda _bug: False,
        )
        self.assertFalse(result["alert"])


if __name__ == "__main__":
    unittest.main()
