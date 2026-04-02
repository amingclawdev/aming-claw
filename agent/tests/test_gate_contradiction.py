"""Tests for governance gate contradiction loop fix.

Verifies that _is_governance_internal_repair correctly identifies governance-
internal tasks and that _gate_checkpoint skips the doc consistency check for
them, while preserving doc enforcement for product-facing code changes.

Regression tests for the oscillation scenario where:
1. Doc gate demands docs for agent/governance/* changes
2. Dev adds docs -> unrelated-files gate rejects them
3. Dev removes docs -> doc gate demands them again (infinite loop)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
import pytest

# Ensure agent package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent.governance.auto_chain import (
    _is_governance_internal_repair,
    _gate_checkpoint,
)


def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with minimal schema for gate tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL DEFAULT 'test',
            status TEXT NOT NULL DEFAULT 'queued',
            execution_status TEXT NOT NULL DEFAULT 'queued',
            notification_status TEXT NOT NULL DEFAULT 'none',
            type TEXT NOT NULL DEFAULT 'task',
            prompt TEXT NOT NULL DEFAULT '',
            related_nodes TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 5,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            result_json TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            parent_task_id TEXT,
            retry_round INTEGER NOT NULL DEFAULT 0,
            assigned_to TEXT,
            fence_token TEXT,
            lease_expires_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT NOT NULL DEFAULT '',
            git_head TEXT NOT NULL DEFAULT '',
            dirty_files TEXT NOT NULL DEFAULT '[]',
            git_synced_at TEXT,
            observer_mode INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE nodes (
            node_id TEXT PRIMARY KEY,
            project_id TEXT,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            level TEXT NOT NULL DEFAULT 'L1',
            parent_id TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            verified_by TEXT,
            verified_at TEXT
        );
        INSERT INTO projects (project_id, name) VALUES ('test', 'test');
        INSERT INTO project_version (project_id, chain_version, git_head)
            VALUES ('test', 'abc123', 'abc123');
    """)
    return conn


# ---------------------------------------------------------------------------
# Test _is_governance_internal_repair predicate (AC1)
# ---------------------------------------------------------------------------

class TestIsGovernanceInternalRepair:
    """Tests for the _is_governance_internal_repair predicate."""

    def test_governance_files_only(self):
        """All files under agent/governance/ -> True."""
        metadata = {"target_files": ["agent/governance/auto_chain.py"]}
        changed = ["agent/governance/auto_chain.py"]
        assert _is_governance_internal_repair(metadata, changed) is True

    def test_governance_with_test_files(self):
        """Governance files + co-located test files -> True."""
        metadata = {"target_files": ["agent/governance/auto_chain.py"]}
        changed = [
            "agent/governance/auto_chain.py",
            "agent/tests/test_gate_contradiction.py",
        ]
        assert _is_governance_internal_repair(metadata, changed) is True

    def test_role_permissions(self):
        """agent/role_permissions.py is governance-internal."""
        metadata = {"target_files": ["agent/role_permissions.py"]}
        changed = ["agent/role_permissions.py"]
        assert _is_governance_internal_repair(metadata, changed) is True

    def test_product_code_not_governance(self):
        """Product code (telegram_gateway) is NOT governance-internal."""
        metadata = {"target_files": ["agent/telegram_gateway/gateway.py"]}
        changed = ["agent/telegram_gateway/gateway.py"]
        assert _is_governance_internal_repair(metadata, changed) is False

    def test_mixed_governance_and_product(self):
        """Mix of governance + product files -> False."""
        metadata = {"target_files": ["agent/governance/auto_chain.py",
                                      "agent/telegram_gateway/gateway.py"]}
        changed = ["agent/governance/auto_chain.py",
                    "agent/telegram_gateway/gateway.py"]
        assert _is_governance_internal_repair(metadata, changed) is False

    def test_empty_files(self):
        """No files at all -> False."""
        assert _is_governance_internal_repair({}, []) is False
        assert _is_governance_internal_repair({"target_files": []}, []) is False

    def test_doc_files_not_governance(self):
        """Doc files are NOT governance-internal."""
        metadata = {"target_files": ["agent/governance/auto_chain.py"]}
        changed = ["agent/governance/auto_chain.py",
                    "docs/p0-3-design.md"]
        assert _is_governance_internal_repair(metadata, changed) is False


# ---------------------------------------------------------------------------
# Test _gate_checkpoint integration (AC2, AC3, AC4)
# ---------------------------------------------------------------------------

class TestGateCheckpointGovernanceExemption:
    """Integration tests for _gate_checkpoint with governance exemption."""

    def test_governance_internal_passes_without_docs(self):
        """AC2: Governance-internal task passes without doc changes."""
        conn = _make_db()
        result = {
            "changed_files": ["agent/governance/auto_chain.py"],
            "test_results": {"ran": True, "passed": 5, "failed": 0},
        }
        metadata = {
            "target_files": ["agent/governance/auto_chain.py"],
        }
        passed, reason = _gate_checkpoint(conn, "test", result, metadata)
        assert passed is True, f"Expected pass but got: {reason}"

    def test_product_code_demands_docs(self):
        """AC3: Product code (telegram_gateway) still demands doc updates."""
        conn = _make_db()
        result = {
            "changed_files": ["agent/telegram_gateway/gateway.py"],
            "test_results": {"ran": True, "passed": 3, "failed": 0},
        }
        metadata = {
            "target_files": ["agent/telegram_gateway/gateway.py"],
        }
        passed, reason = _gate_checkpoint(conn, "test", result, metadata)
        assert passed is False
        assert "Related docs not updated" in reason

    def test_unrelated_files_still_enforced_for_governance(self):
        """AC4: Unrelated code files rejected even for governance tasks."""
        conn = _make_db()
        result = {
            "changed_files": [
                "agent/governance/auto_chain.py",
                "agent/telegram_gateway/gateway.py",  # unrelated!
            ],
            "test_results": {"ran": True, "passed": 5, "failed": 0},
        }
        metadata = {
            "target_files": ["agent/governance/auto_chain.py"],
        }
        passed, reason = _gate_checkpoint(conn, "test", result, metadata)
        assert passed is False
        assert "Unrelated files modified" in reason

    def test_oscillation_scenario_no_loop(self):
        """Exact contradiction scenario: governance task should pass without
        adding docs, and should NOT trigger unrelated-files for doc files
        because docs are never added in the first place.

        Previously this would oscillate:
        1. Doc gate: "add docs/p0-3-design.md" -> dev adds it
        2. Unrelated-files gate: "docs/p0-3-design.md is unrelated" -> dev removes it
        3. Goto 1 (infinite loop)

        Now: governance-internal tasks skip doc gate entirely, so step 1 never
        fires and no docs are added, preventing the loop.
        """
        conn = _make_db()
        # Simulate a governance repair that changes only governance files
        result = {
            "changed_files": [
                "agent/governance/auto_chain.py",
                "agent/governance/impact_analyzer.py",
            ],
            "test_results": {"ran": True, "passed": 10, "failed": 0},
        }
        metadata = {
            "target_files": [
                "agent/governance/auto_chain.py",
                "agent/governance/impact_analyzer.py",
            ],
        }
        passed, reason = _gate_checkpoint(conn, "test", result, metadata)
        assert passed is True, f"Governance repair should pass but got: {reason}"
        # Verify no doc demand in the reason
        assert "docs not updated" not in reason.lower()

    def test_governance_with_test_files_passes(self):
        """Governance task with co-located test files also passes."""
        conn = _make_db()
        result = {
            "changed_files": [
                "agent/governance/auto_chain.py",
                "agent/tests/test_gate_contradiction.py",
            ],
            "test_results": {"ran": True, "passed": 8, "failed": 0},
        }
        metadata = {
            "target_files": ["agent/governance/auto_chain.py"],
            "test_files": ["agent/tests/test_gate_contradiction.py"],
        }
        passed, reason = _gate_checkpoint(conn, "test", result, metadata)
        assert passed is True, f"Expected pass but got: {reason}"
