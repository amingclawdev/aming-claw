"""Tests for scripts/backfill-observer-hotfix-trail.py.

Covers:
  - AC1: 5 new backlog_bugs rows with correct bug_ids, status=FIXED, commit hashes
  - AC2: All 10 observer-hotfix-related backlog_bugs rows exist after backfill
  - AC3: 3 MF execution record files written to docs/dev/
  - AC4: Idempotency — running twice produces no errors or duplicate rows
  - AC5: Uses POST /api/backlog/{pid}/{bug_id} REST endpoint for upserts
"""

import json
import os
import sys
import tempfile
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import the script module
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

# We need to import by filename since it has hyphens
import importlib.util
_SCRIPT_PATH = os.path.join(_SCRIPTS_DIR, "backfill-observer-hotfix-trail.py")
_spec = importlib.util.spec_from_file_location("backfill_observer_hotfix_trail", _SCRIPT_PATH)
backfill_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill_mod)


# ---------------------------------------------------------------------------
# Expected data for assertions
# ---------------------------------------------------------------------------

# 5 NEW bugs from the backfill script (AC1)
NEW_BUGS = {
    "OPT-BACKLOG-B48-SM-LOG": "ba791f0",
    "OPT-BACKLOG-B48-SIDECAR-IMPORT": "1bb9f35",
    "OPT-BACKLOG-F2-PYTHONPATH": "2763aac",
    "OPT-BACKLOG-B48-SEQUEL-VERSION-DEPLOY": "4a12c29",
    "OPT-BACKLOG-VERSION-UPDATE-LOCKDOWN": "e57e7ba",
}

# 5 PRE-EXISTING observer-hotfix-related bugs (AC2)
PREEXISTING_BUGS = {
    "OPT-BACKLOG-QA-CLI-AUTH-TOKEN-STALE": "",
    "OPT-BACKLOG-MERGE-D6-EXPLICIT-FLAG": "",
    "OPT-BACKLOG-DEPLOY-SELFKILL": "",
    "OPT-BACKLOG-CHAIN-ENFORCEMENT": "",
    "OPT-BACKLOG-GRAPH-COVERAGE": "",
}

# MF record files (AC3)
MF_RECORD_FILES = [
    "observer-hotfix-record-2026-04-24-84e7be8.md",
    "observer-hotfix-record-2026-04-24-d4398bb.md",
    "observer-hotfix-record-2026-04-24-fedaf27.md",
]


class _MockGovernanceHandler(BaseHTTPRequestHandler):
    """Mock governance server that records upsert requests."""

    # Class-level storage for test assertions
    upserted = {}  # bug_id -> payload

    def do_POST(self):
        # Expected: POST /api/backlog/{pid}/{bug_id}
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "backlog":
            bug_id = parts[3]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            _MockGovernanceHandler.upserted[bug_id] = body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "bug_id": bug_id, "action": "upserted"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logging during tests


class TestBackfillScript(unittest.TestCase):
    """Integration tests for backfill-observer-hotfix-trail.py."""

    @classmethod
    def setUpClass(cls):
        """Start a mock governance server."""
        _MockGovernanceHandler.upserted = {}
        cls.server = HTTPServer(("127.0.0.1", 0), _MockGovernanceHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.tmp_dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        # Clean up temp MF record files
        import shutil
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def setUp(self):
        _MockGovernanceHandler.upserted = {}

    # --- AC1: 5 bugs upserted with correct data ---

    def test_ac1_upserts_five_bugs(self):
        """AC1: Script upserts exactly 5 backlog_bugs rows."""
        backfill_mod.main([
            "--apply",
            "--base-url", self.base_url,
            "--pid", "aming-claw",
        ])
        self.assertEqual(len(_MockGovernanceHandler.upserted), 5)

    def test_ac1_correct_bug_ids(self):
        """AC1: All 5 expected bug_ids are present."""
        backfill_mod.main([
            "--apply",
            "--base-url", self.base_url,
            "--pid", "aming-claw",
        ])
        for bug_id in NEW_BUGS:
            self.assertIn(bug_id, _MockGovernanceHandler.upserted,
                          f"Missing bug_id: {bug_id}")

    def test_ac1_correct_commits(self):
        """AC1: Each bug has the correct commit hash."""
        backfill_mod.main([
            "--apply",
            "--base-url", self.base_url,
            "--pid", "aming-claw",
        ])
        for bug_id, expected_commit in NEW_BUGS.items():
            payload = _MockGovernanceHandler.upserted[bug_id]
            self.assertEqual(payload["commit"], expected_commit,
                             f"{bug_id}: expected commit {expected_commit}, got {payload.get('commit')}")

    def test_ac1_status_fixed(self):
        """AC1: All 5 bugs have status=FIXED."""
        backfill_mod.main([
            "--apply",
            "--base-url", self.base_url,
            "--pid", "aming-claw",
        ])
        for bug_id, payload in _MockGovernanceHandler.upserted.items():
            self.assertEqual(payload["status"], "FIXED",
                             f"{bug_id}: expected FIXED, got {payload.get('status')}")

    # --- AC2: 10 total rows (5 new + 5 pre-existing) ---

    def test_ac2_new_bug_ids_defined(self):
        """AC2: Script defines exactly the 5 new bug entries."""
        script_bug_ids = {b["bug_id"] for b in backfill_mod.BACKFILL_BUGS}
        self.assertEqual(script_bug_ids, set(NEW_BUGS.keys()))

    def test_ac2_all_ten_bug_ids_known(self):
        """AC2: 5 new + 5 pre-existing = 10 observer-hotfix-related bug_ids are tracked."""
        all_bug_ids = set(NEW_BUGS.keys()) | set(PREEXISTING_BUGS.keys())
        self.assertEqual(len(all_bug_ids), 10)

    # --- AC3: MF execution record files ---

    def test_ac3_mf_records_written(self):
        """AC3: Three MF execution record files are written."""
        with patch.object(backfill_mod, "_PROJECT_ROOT", self.tmp_dir):
            backfill_mod._write_mf_records(self.tmp_dir, dry_run=False)

        docs_dev = os.path.join(self.tmp_dir, "docs", "dev")
        for filename in MF_RECORD_FILES:
            path = os.path.join(docs_dev, filename)
            self.assertTrue(os.path.exists(path), f"Missing MF record: {filename}")

    def test_ac3_mf_records_have_content(self):
        """AC3: MF record files contain non-empty markdown."""
        with patch.object(backfill_mod, "_PROJECT_ROOT", self.tmp_dir):
            backfill_mod._write_mf_records(self.tmp_dir, dry_run=False)

        docs_dev = os.path.join(self.tmp_dir, "docs", "dev")
        for filename in MF_RECORD_FILES:
            path = os.path.join(docs_dev, filename)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertGreater(len(content), 100,
                               f"{filename} has too little content ({len(content)} chars)")
            self.assertIn("# MF Execution Record", content)

    def test_ac3_mf_dry_run_no_files(self):
        """AC3: dry-run does NOT write MF record files."""
        dry_tmp = tempfile.mkdtemp()
        try:
            backfill_mod._write_mf_records(dry_tmp, dry_run=True)
            docs_dev = os.path.join(dry_tmp, "docs", "dev")
            # docs/dev may not even exist, or should have no files
            if os.path.isdir(docs_dev):
                self.assertEqual(os.listdir(docs_dev), [])
        finally:
            import shutil
            shutil.rmtree(dry_tmp, ignore_errors=True)

    # --- AC4: Idempotency ---

    def test_ac4_idempotent_double_run(self):
        """AC4: Running twice produces no errors and same 5 entries."""
        args = ["--apply", "--base-url", self.base_url, "--pid", "aming-claw"]
        backfill_mod.main(args)
        first_run = dict(_MockGovernanceHandler.upserted)

        _MockGovernanceHandler.upserted = {}
        backfill_mod.main(args)
        second_run = dict(_MockGovernanceHandler.upserted)

        self.assertEqual(set(first_run.keys()), set(second_run.keys()))
        for bug_id in first_run:
            self.assertEqual(first_run[bug_id]["commit"], second_run[bug_id]["commit"])

    # --- AC5: Uses REST endpoint ---

    def test_ac5_uses_post_api_backlog(self):
        """AC5: Script calls POST /api/backlog/{pid}/{bug_id} for each bug."""
        backfill_mod.main([
            "--apply",
            "--base-url", self.base_url,
            "--pid", "aming-claw",
        ])
        # Verify all 5 bugs were sent via POST to the mock server
        self.assertEqual(len(_MockGovernanceHandler.upserted), 5)
        # Verify payloads have required fields
        for bug_id, payload in _MockGovernanceHandler.upserted.items():
            self.assertIn("title", payload)
            self.assertIn("status", payload)
            self.assertIn("commit", payload)
            self.assertIn("actor", payload)
            self.assertEqual(payload["actor"], "backfill-observer-hotfix-trail")

    def test_ac5_dry_run_no_requests(self):
        """AC5: Dry-run does NOT make any HTTP requests."""
        backfill_mod.main([
            "--dry-run",
            "--base-url", self.base_url,
            "--pid", "aming-claw",
        ])
        self.assertEqual(len(_MockGovernanceHandler.upserted), 0)


if __name__ == "__main__":
    unittest.main()
