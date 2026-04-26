"""Tests for Phase K path-prefix attribution scoring.

Covers AC-PP-1..4: path prefix extraction from HTTP URLs,
correct service attribution via /api/<svc>/ prefix, and
fallback to keyword scoring when no prefix is present.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, FrozenSet, Set
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs (same as test_phase_k.py)
# ---------------------------------------------------------------------------

class FakeResolvedScope:
    def __init__(self, file_dict):
        self.file_set = file_dict
        self.node_set = frozenset()
        self.commit_set = frozenset()

    def files(self):
        return set(self.file_set.keys())

    def is_empty(self):
        return len(self.file_set) == 0


class FakeCtx:
    def __init__(self, workspace):
        self.project_id = "aming-claw"
        self.workspace_path = workspace


@pytest.fixture()
def tmp_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _write(ws, relpath, content):
    p = ws / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return relpath


# ---------------------------------------------------------------------------
# AC-PP-1: /api/manager/ prefix → manager_http_server wins over governance
# ---------------------------------------------------------------------------

class TestPathPrefixManagerWins:
    def test_score_path_prefix_manager_gt_governance(self):
        """AC-PP-1: score_path_prefix_match for manager_http_server > governance
        when URL has /api/manager/ prefix."""
        from agent.governance.reconcile_phases.phase_k import (
            score_path_prefix_match, ServicePortContract,
        )
        doc = (
            "## Redeploy\n"
            "\n"
            "```bash\n"
            "curl http://localhost:40007/api/manager/redeploy/governance\n"
            "```\n"
        )
        offset = doc.index("localhost:40007")

        sp_mgr = ServicePortContract("manager_http_server", 40101, "MANAGER_HTTP_PORT", "manager_http_server.py", 1)
        sp_gov = ServicePortContract("governance", 40000, "GOVERNANCE_PORT", "server.py", 1)

        score_mgr = score_path_prefix_match(sp_mgr, doc, offset)
        score_gov = score_path_prefix_match(sp_gov, doc, offset)

        assert score_mgr > score_gov, f"manager={score_mgr} should > governance={score_gov}"
        assert score_mgr >= 5.0  # exact/alias match
        assert score_gov == 0.0  # governance is NOT in /api/manager/ prefix

    def test_run_emits_drift_attributed_to_manager(self, tmp_workspace):
        """AC-PP-1: Phase K run() emits doc_value_drift attributed to MANAGER_HTTP_PORT."""
        ws = tmp_workspace
        _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        _write(ws, "docs/api/governance-api.md", """\
            # API Reference

            ## Redeploy

            ```bash
            curl http://localhost:40007/api/manager/redeploy/governance
            ```
        """)

        scope = FakeResolvedScope({
            "start_governance.py": None,
            "agent/manager_http_server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)
        drift = [d for d in results if d.type == "doc_value_drift" and d.doc_value == 40007]
        assert len(drift) >= 1, f"Expected drift for 40007, got {results}"
        assert drift[0].contract_id == "MANAGER_HTTP_PORT"
        assert drift[0].code_value == 40101


# ---------------------------------------------------------------------------
# AC-PP-2: /api/governance/ prefix → governance wins even with 'service_manager' in path
# ---------------------------------------------------------------------------

class TestPathPrefixGovernanceWins:
    def test_score_path_prefix_governance_gt_manager(self):
        """AC-PP-2: /api/governance/ prefix → governance score > manager score."""
        from agent.governance.reconcile_phases.phase_k import (
            score_path_prefix_match, ServicePortContract,
        )
        doc = (
            "## Redeploy\n"
            "\n"
            "```bash\n"
            "curl http://localhost:40006/api/governance/redeploy/service_manager\n"
            "```\n"
        )
        offset = doc.index("localhost:40006")

        sp_gov = ServicePortContract("governance", 40000, "GOVERNANCE_PORT", "server.py", 1)
        sp_mgr = ServicePortContract("manager_http_server", 40101, "MANAGER_HTTP_PORT", "manager_http_server.py", 1)

        score_gov = score_path_prefix_match(sp_gov, doc, offset)
        score_mgr = score_path_prefix_match(sp_mgr, doc, offset)

        assert score_gov > score_mgr, f"governance={score_gov} should > manager={score_mgr}"
        assert score_gov >= 5.0

    def test_run_emits_drift_attributed_to_governance(self, tmp_workspace):
        """AC-PP-2: Phase K run() attributes drift to GOVERNANCE_PORT."""
        ws = tmp_workspace
        _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        _write(ws, "docs/api/governance-api.md", """\
            # API Reference

            ## Redeploy

            ```bash
            curl http://localhost:40006/api/governance/redeploy/service_manager
            ```
        """)

        scope = FakeResolvedScope({
            "start_governance.py": None,
            "agent/manager_http_server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)
        drift = [d for d in results if d.type == "doc_value_drift" and d.doc_value == 40006]
        assert len(drift) >= 1, f"Expected drift for 40006, got {results}"
        assert drift[0].contract_id == "GOVERNANCE_PORT"
        assert drift[0].code_value == 40000


# ---------------------------------------------------------------------------
# AC-PP-3: No /api/<svc>/ path → score 0.0, fallback to keyword scoring
# ---------------------------------------------------------------------------

class TestNoPathPrefix:
    def test_no_http_prefix_returns_zero(self):
        """AC-PP-3: localhost:40000 with no nearby /api/<svc>/ → 0.0."""
        from agent.governance.reconcile_phases.phase_k import (
            score_path_prefix_match, ServicePortContract,
        )
        doc = (
            "## Server\n"
            "\n"
            "The governance server listens on localhost:40000.\n"
            "\n"
            "It handles health checks.\n"
        )
        offset = doc.index("localhost:40000")

        sp = ServicePortContract("governance", 40000, "GOVERNANCE_PORT", "server.py", 1)
        score = score_path_prefix_match(sp, doc, offset)
        assert score == 0.0

    def test_bare_api_mention_not_counted(self):
        """AC-PP-3: bare /api/governance/ mention (not in http URL) → 0.0."""
        from agent.governance.reconcile_phases.phase_k import (
            score_path_prefix_match, ServicePortContract,
        )
        doc = (
            "## Notes\n"
            "\n"
            "The endpoint /api/governance/health is important.\n"
            "Server runs on localhost:40000.\n"
        )
        offset = doc.index("localhost:40000")

        sp = ServicePortContract("governance", 40000, "GOVERNANCE_PORT", "server.py", 1)
        score = score_path_prefix_match(sp, doc, offset)
        assert score == 0.0


# ---------------------------------------------------------------------------
# AC-PP-4: Regression — existing tests still pass (verified by running
# test_phase_k.py and test_phase_k_extract.py alongside this file)
# ---------------------------------------------------------------------------

class TestRegressionPathPrefix:
    def test_keyword_score_unchanged(self):
        """R5: keyword scoring still works when no path prefix present."""
        from agent.governance.reconcile_phases.phase_k import (
            score_service_port_match, ServicePortContract,
        )
        sp = ServicePortContract("governance", 40000, "GOVERNANCE_PORT", "server.py", 1)
        doc = "Line1\nGOVERNANCE_PORT is set here\nlocalhost:40006\nLine4\n"
        offset = doc.index("localhost:40006")
        score = score_service_port_match(sp, doc, offset)
        # Should still get +3.0 from constant_name match (existing behavior)
        assert score >= 3.0

    def test_path_prefix_additive_with_keyword(self):
        """R2/R5: path_prefix_score is additive on top of keyword_score."""
        from agent.governance.reconcile_phases.phase_k import (
            score_service_port_match, ServicePortContract,
        )
        sp = ServicePortContract("governance", 40000, "GOVERNANCE_PORT", "server.py", 1)
        doc = (
            "GOVERNANCE_PORT is set here\n"
            "curl http://localhost:40006/api/governance/health\n"
            "Line3\n"
        )
        offset = doc.index("localhost:40006")
        score = score_service_port_match(sp, doc, offset)
        # +3.0 constant_name + 5.0 path_prefix + possibly more
        assert score >= 8.0
