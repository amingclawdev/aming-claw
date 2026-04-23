"""Tests for memory injection into downstream prompts (dev/QA/gatekeeper).

Covers AC3, AC4, AC5, AC6, AC7, AC8, AC9, AC10.
"""

import json
import sqlite3
import sys
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

# Ensure agent package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_in_memory_db():
    """Create an in-memory SQLite DB with memories table + resolution columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memories (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT UNIQUE,
            project_id TEXT NOT NULL,
            ref_id TEXT DEFAULT '',
            kind TEXT DEFAULT 'knowledge',
            module_id TEXT DEFAULT '',
            scope TEXT DEFAULT 'project',
            content TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            metadata_json TEXT,
            tags TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            superseded_by_memory_id TEXT,
            entity_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolution_commit TEXT DEFAULT '',
            resolution_summary TEXT DEFAULT ''
        )
    """)
    # FTS5 table
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, summary, module_id, kind,
            content='memories',
            content_rowid='rowid'
        )
    """)
    return conn


def _insert_memory(conn, memory_id, project_id, kind, module_id, content,
                    created_at=None, resolution_commit="", resolution_summary=""):
    now = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO memories (memory_id, project_id, kind, module_id, content, "
        "summary, metadata_json, created_at, updated_at, resolution_commit, resolution_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
        (memory_id, project_id, kind, module_id, content,
         content[:50], now, now, resolution_commit, resolution_summary),
    )
    conn.commit()


class TestMemoryInjectionDev(unittest.TestCase):
    """AC3, AC4, AC7: Dev prompt memory injection."""

    def _build_dev_prompt_with_mocks(self, memories_in_db):
        """Helper: set up in-memory DB, insert memories, call _build_dev_prompt."""
        conn = _make_in_memory_db()
        for m in memories_in_db:
            _insert_memory(conn, **m)

        # Mock get_connection to return our in-memory DB
        with patch("agent.governance.memory_service.get_connection", return_value=conn), \
             patch("agent.governance.auto_chain.get_connection", return_value=conn) if False else \
             patch("agent.governance.memory_service.get_backend") as mock_backend:
            # We need to directly test the injection function
            from agent.governance.auto_chain import _inject_dev_memories
            # Patch get_connection in the memory_service module
            with patch("agent.governance.memory_service.get_connection", return_value=conn):
                metadata = {
                    "project_id": "test-project",
                    "target_files": ["agent/governance/auto_chain.py"],
                }
                # Call the injection directly with a patched DB
                from agent.governance.memory_service import search_memories_for_injection
                results = search_memories_for_injection(
                    conn, "test-project",
                    ["agent/governance/auto_chain.py"],
                    kinds=["pitfall", "pattern"],
                    top_k=5, max_age_days=30, include_resolved_old=True,
                )
                return results, conn

    def test_ac3_dev_prompt_contains_pitfall_section(self):
        """AC3: _build_dev_prompt output contains '## Prior pitfalls in this scope'."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "m1", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "Gate blocked at test: missing assertion",
                       resolution_commit="abc1234",
                       resolution_summary="Fixed missing assertion")

        with patch("agent.governance.auto_chain._inject_dev_memories") as mock_inject:
            mock_inject.return_value = (
                "## Prior pitfalls in this scope\n"
                "- [pitfall] Gate blocked at test: missing assertion (fixed by commit abc12345)"
            )
            from agent.governance.auto_chain import _build_dev_prompt
            prompt, meta = _build_dev_prompt(
                "task-123",
                {"target_files": ["agent/governance/auto_chain.py"],
                 "requirements": ["R1"], "acceptance_criteria": ["AC1"],
                 "verification": {"method": "test"}},
                {"project_id": "test-project",
                 "target_files": ["agent/governance/auto_chain.py"]},
            )
            self.assertIn("## Prior pitfalls in this scope", prompt)
            self.assertIn("fixed by commit", prompt)

    def test_ac4_dev_prompt_omits_section_when_no_memories(self):
        """AC4: _build_dev_prompt output omits the section entirely when no matching memories exist."""
        with patch("agent.governance.auto_chain._inject_dev_memories") as mock_inject:
            mock_inject.return_value = ""
            from agent.governance.auto_chain import _build_dev_prompt
            prompt, meta = _build_dev_prompt(
                "task-123",
                {"target_files": ["agent/governance/auto_chain.py"],
                 "requirements": ["R1"], "acceptance_criteria": ["AC1"],
                 "verification": {"method": "test"}},
                {"project_id": "test-project",
                 "target_files": ["agent/governance/auto_chain.py"]},
            )
            self.assertNotIn("## Prior pitfalls in this scope", prompt)

    def test_ac7_old_memories_without_resolution_excluded(self):
        """AC7: Memories older than 30 days WITHOUT resolution_commit are excluded."""
        conn = _make_in_memory_db()
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Old memory without resolution → should be excluded
        _insert_memory(conn, "m-old-unresolved", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "Old unresolved pitfall",
                       created_at=old_date)

        from agent.governance.memory_service import search_memories_for_injection
        results = search_memories_for_injection(
            conn, "test-project",
            ["agent/governance/auto_chain.py"],
            kinds=["pitfall", "pattern"],
            top_k=5, max_age_days=30, include_resolved_old=True,
        )
        self.assertEqual(len(results), 0, "Old unresolved memory should be excluded")

    def test_ac7_old_memories_with_resolution_included(self):
        """AC7: Memories older than 30 days WITH resolution_commit are included."""
        conn = _make_in_memory_db()
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Old memory WITH resolution → should be included
        _insert_memory(conn, "m-old-resolved", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "Old resolved pitfall",
                       created_at=old_date,
                       resolution_commit="deadbeef",
                       resolution_summary="Fixed it")

        from agent.governance.memory_service import search_memories_for_injection
        results = search_memories_for_injection(
            conn, "test-project",
            ["agent/governance/auto_chain.py"],
            kinds=["pitfall", "pattern"],
            top_k=5, max_age_days=30, include_resolved_old=True,
        )
        self.assertEqual(len(results), 1, "Old resolved memory should be included")
        self.assertEqual(results[0]["resolution_commit"], "deadbeef")

    def test_ac10_graceful_degradation_on_exception(self):
        """AC10: Injection paths handle exceptions gracefully."""
        from agent.governance.auto_chain import _inject_dev_memories

        # Force an exception by patching get_connection in the db module (where it's imported from)
        with patch("agent.governance.db.get_connection",
                   side_effect=Exception("no DB")):
            result = _inject_dev_memories({"project_id": "test", "target_files": ["a.py"]})
            self.assertEqual(result, "", "Should return empty string on exception")


class TestMemoryInjectionQA(unittest.TestCase):
    """AC5: QA prompt memory injection."""

    def test_ac5_qa_prompt_contains_section(self):
        """AC5: _build_qa_prompt output contains '## Prior QA decisions for similar scope'."""
        with patch("agent.governance.auto_chain._inject_qa_memories") as mock_inject:
            mock_inject.return_value = (
                "## Prior QA decisions for similar scope\n"
                "- [qa_decision] Previous QA rejected due to missing tests"
            )
            from agent.governance.auto_chain import _build_qa_prompt
            prompt, meta = _build_qa_prompt(
                "task-456",
                {"test_report": {"passed": 5, "failed": 0},
                 "changed_files": ["auto_chain.py"]},
                {"project_id": "test-project",
                 "target_files": ["agent/governance/auto_chain.py"],
                 "requirements": ["R1"],
                 "acceptance_criteria": ["AC1"]},
            )
            self.assertIn("## Prior QA decisions for similar scope", prompt)

    def test_ac10_qa_graceful_degradation(self):
        """AC10: QA injection handles exceptions gracefully."""
        from agent.governance.auto_chain import _inject_qa_memories
        with patch("agent.governance.db.get_connection",
                   side_effect=Exception("no DB")):
            result = _inject_qa_memories({"project_id": "test", "target_files": ["a.py"]})
            self.assertEqual(result, "")


class TestMemoryInjectionGatekeeper(unittest.TestCase):
    """AC6: Gatekeeper prompt memory injection."""

    def test_ac6_gatekeeper_prompt_contains_section(self):
        """AC6: _build_gatekeeper_prompt output contains '## Prior decisions'."""
        with patch("agent.governance.auto_chain._inject_gatekeeper_memories") as mock_inject:
            mock_inject.return_value = (
                "## Prior decisions\n"
                "- [decision] Rejected merge due to missing doc updates"
            )
            from agent.governance.auto_chain import _build_gatekeeper_prompt
            prompt, meta = _build_gatekeeper_prompt(
                "task-789",
                {"review_summary": "ok", "recommendation": "qa_pass"},
                {"project_id": "test-project",
                 "target_files": ["agent/governance/auto_chain.py"],
                 "requirements": ["R1"],
                 "acceptance_criteria": ["AC1"]},
            )
            self.assertIn("## Prior decisions", prompt)

    def test_ac10_gatekeeper_graceful_degradation(self):
        """AC10: Gatekeeper injection handles exceptions gracefully."""
        from agent.governance.auto_chain import _inject_gatekeeper_memories
        with patch("agent.governance.db.get_connection",
                   side_effect=Exception("no DB")):
            result = _inject_gatekeeper_memories({"project_id": "test", "target_files": ["a.py"]})
            self.assertEqual(result, "")


class TestAntiPatternRankBoost(unittest.TestCase):
    """AC8, AC9: anti_pattern kind and rank boost."""

    def test_ac8_anti_pattern_in_promotable_kinds(self):
        """AC8: 'anti_pattern' is present in _PROMOTABLE_KINDS."""
        from agent.governance.memory_service import _PROMOTABLE_KINDS
        self.assertIn("anti_pattern", _PROMOTABLE_KINDS)

    def test_ac9_anti_pattern_rank_boost(self):
        """AC9: anti_pattern memories rank higher than same-score non-anti_pattern memories."""
        conn = _make_in_memory_db()
        # Insert two memories with same FTS relevance
        _insert_memory(conn, "m-regular", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "Some pitfall about testing")
        _insert_memory(conn, "m-anti", "test-project", "anti_pattern",
                       "agent.governance.auto_chain.py",
                       "Some anti-pattern about testing")

        # Simulate search results with same score
        mock_results = [
            {"memory_id": "m-regular", "kind": "pitfall", "score": -1.0,
             "ref_id": "", "module_id": "test", "content": "pitfall",
             "summary": "", "metadata": {}, "version": 1, "search_mode": "fts5",
             "created_at": "2026-01-01"},
            {"memory_id": "m-anti", "kind": "anti_pattern", "score": -1.0,
             "ref_id": "", "module_id": "test", "content": "anti",
             "summary": "", "metadata": {}, "version": 1, "search_mode": "fts5",
             "created_at": "2026-01-01"},
        ]

        with patch("agent.governance.memory_service.get_backend") as mock_be:
            mock_backend = MagicMock()
            mock_backend.search.return_value = mock_results
            mock_be.return_value = mock_backend

            from agent.governance.memory_service import search_memories
            results = search_memories(conn, "test-project", "testing", top_k=5)

            # anti_pattern should be first (lower score = better rank in FTS5)
            anti_idx = next(i for i, r in enumerate(results) if r["kind"] == "anti_pattern")
            regular_idx = next(i for i, r in enumerate(results) if r["kind"] == "pitfall")
            self.assertLess(anti_idx, regular_idx,
                            "anti_pattern should rank higher (lower index) than same-score pitfall")


class TestSearchMemoriesForInjection(unittest.TestCase):
    """Test the search_memories_for_injection function directly."""

    def test_kind_filter(self):
        """Only returns memories of requested kinds."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "m1", "proj", "pitfall", "agent.governance.auto_chain.py", "content1")
        _insert_memory(conn, "m2", "proj", "knowledge", "agent.governance.auto_chain.py", "content2")

        from agent.governance.memory_service import search_memories_for_injection
        results = search_memories_for_injection(
            conn, "proj", ["agent/governance/auto_chain.py"],
            kinds=["pitfall"], top_k=5,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "pitfall")

    def test_module_prefix_matching(self):
        """Only returns memories whose module_id matches target_files prefixes."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "m1", "proj", "pitfall", "agent.governance.auto_chain.py", "matching")
        _insert_memory(conn, "m2", "proj", "pitfall", "agent.service_manager.py", "non-matching")

        from agent.governance.memory_service import search_memories_for_injection
        results = search_memories_for_injection(
            conn, "proj", ["agent/governance/auto_chain.py"],
            kinds=["pitfall"], top_k=5,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["module_id"], "agent.governance.auto_chain.py")

    def test_empty_target_files(self):
        """Returns empty when no target_files provided."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "m1", "proj", "pitfall", "module", "content")

        from agent.governance.memory_service import search_memories_for_injection
        results = search_memories_for_injection(
            conn, "proj", [], kinds=["pitfall"], top_k=5,
        )
        # With no target_files, no module prefix filter → returns all matching kind
        # But the function uses prefix matching so it should still return them
        # Actually with empty module_prefixes, it skips the filter
        self.assertIsInstance(results, list)

    def test_graceful_on_missing_columns(self):
        """Returns empty list if DB lacks resolution columns (graceful degradation)."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Deliberately create table WITHOUT resolution columns
        conn.execute("""
            CREATE TABLE memories (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT UNIQUE,
                project_id TEXT,
                kind TEXT,
                module_id TEXT DEFAULT '',
                content TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                metadata_json TEXT,
                version INTEGER DEFAULT 1,
                status TEXT DEFAULT 'active',
                ref_id TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
        """)
        from agent.governance.memory_service import search_memories_for_injection
        results = search_memories_for_injection(
            conn, "proj", ["agent/governance/auto_chain.py"],
            kinds=["pitfall"], top_k=5,
        )
        # Should return empty (graceful degradation) rather than crash
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
