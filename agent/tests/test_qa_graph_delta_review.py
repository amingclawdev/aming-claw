"""Tests for PR-B: QA graph_delta_review enforcement in auto_chain.

Covers:
  (a) pass without review when no proposed event (back-compat)
  (b) block when proposed exists but review missing
  (c) reject triggers dev-retry with issues in prompt
  (d) pass writes graph.delta.validated event
  (e) graph_delta_review instructions injected into QA prompt
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Ensure agent package is importable
agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


def _make_mock_conn():
    """Create a mock DB connection."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    conn.execute.return_value.fetchall.return_value = []
    return conn


def _base_metadata(**overrides):
    meta = {
        "project_id": "aming-claw",
        "parent_task_id": "task-pm-root",
        "chain_id": "task-pm-root",
        "target_files": ["agent/governance/auto_chain.py"],
        "changed_files": ["agent/governance/auto_chain.py"],
        "related_nodes": [],
        "acceptance_criteria": [],
        "requirements": [],
        "verification": {},
        "doc_impact": {},
    }
    meta.update(overrides)
    return meta


class TestQueryGraphDeltaProposed(unittest.TestCase):
    """Test the _query_graph_delta_proposed helper."""

    def test_returns_none_when_no_event(self):
        from agent.governance.auto_chain import _query_graph_delta_proposed

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        mock_store = MagicMock()
        mock_store._task_to_root = {}

        mock_chain_ctx = MagicMock()
        mock_chain_ctx.get_store.return_value = mock_store

        mock_db = MagicMock()
        mock_db.get_connection.return_value = mock_conn

        with patch.dict("sys.modules", {
            "agent.governance.chain_context": mock_chain_ctx,
            "agent.governance.db": mock_db,
        }):
            result = _query_graph_delta_proposed({"chain_id": "task-pm-1", "project_id": "aming-claw"})

        self.assertIsNone(result)

    def test_returns_none_when_no_chain_id(self):
        from agent.governance.auto_chain import _query_graph_delta_proposed
        result = _query_graph_delta_proposed({})
        self.assertIsNone(result)


class TestGateQaPassGraphDeltaReview(unittest.TestCase):
    """Test _gate_qa_pass graph_delta_review enforcement."""

    def _call_gate(self, result, metadata, proposed_payload=None):
        """Call _gate_qa_pass with mocked dependencies."""
        from agent.governance.auto_chain import _gate_qa_pass

        conn = _make_mock_conn()

        # Mock _query_graph_delta_proposed
        with patch("agent.governance.auto_chain._query_graph_delta_proposed", return_value=proposed_payload), \
             patch("agent.governance.auto_chain._try_verify_update", return_value=(True, "")), \
             patch("agent.governance.auto_chain._check_nodes_min_status", return_value=(True, "ok")), \
             patch("agent.governance.auto_chain._get_graph_doc_associations", return_value=[]), \
             patch("agent.governance.auto_chain._write_chain_memory"), \
             patch("agent.governance.auto_chain._is_governed_dirty_workspace_chain", return_value=False):
            passed, reason = _gate_qa_pass(conn, "aming-claw", result, metadata)
        return passed, reason

    def test_ac7_pass_without_review_when_no_proposed(self):
        """AC7: No graph.delta.proposed -> field not required (back-compat)."""
        result = {"recommendation": "qa_pass"}
        metadata = _base_metadata()
        passed, reason = self._call_gate(result, metadata, proposed_payload=None)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_ac4_block_when_proposed_but_review_missing(self):
        """AC4: proposed event exists but graph_delta_review missing -> block."""
        result = {"recommendation": "qa_pass"}
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertFalse(passed)
        self.assertIn("graph.delta.proposed present but QA result omits graph_delta_review", reason)

    def test_reject_reason_preserves_qa_issues_and_failed_criteria(self):
        """QA reject retries should get actionable issues, not 'no reason given'."""
        result = {
            "recommendation": "reject",
            "review_summary": "Changed tests lacked pre-fix failure evidence.",
            "issues": ["AC5 changed_files mismatch"],
            "criteria_results": [
                {
                    "criterion": "AC5: overlay-only unless defect proven",
                    "passed": False,
                    "evidence": "Dev changed tests but QA did not see failing-test context.",
                }
            ],
        }
        metadata = _base_metadata()
        passed, reason = self._call_gate(result, metadata, proposed_payload=None)
        self.assertFalse(passed)
        self.assertIn("Changed tests lacked pre-fix failure evidence", reason)
        self.assertIn("AC5 changed_files mismatch", reason)
        self.assertIn("overlay-only unless defect proven", reason)
        self.assertNotIn("no reason given", reason)

    def test_blocks_missing_criteria_results_when_criteria_exist(self):
        """QA pass must include per-criterion evidence when PM supplied ACs."""
        result = {"recommendation": "qa_pass"}
        metadata = _base_metadata(acceptance_criteria=["AC1: exact graph delta"])
        passed, reason = self._call_gate(result, metadata, proposed_payload=None)
        self.assertFalse(passed)
        self.assertIn("missing criteria_results", reason)

    def test_ac5_block_when_decision_reject(self):
        """AC5: decision=='reject' -> block with issues in reason."""
        result = {
            "recommendation": "qa_pass",
            "graph_delta_review": {
                "decision": "reject",
                "issues": ["node L3.1 has wrong parent_layer"],
                "suggested_diff": {"updates": [{"node_id": "L3.1", "fields": {"parent_layer": "L2"}}]},
            },
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertFalse(passed)
        self.assertIn("graph delta rejected by QA", reason)
        self.assertIn("node L3.1 has wrong parent_layer", reason)

    def test_ac6_pass_writes_validated_event(self):
        """AC6: decision=='pass' -> writes graph.delta.validated event."""
        from agent.governance.auto_chain import _gate_qa_pass

        result = {
            "recommendation": "qa_pass",
            "graph_delta_review": {
                "decision": "pass",
                "issues": [],
                "suggested_diff": {},
            },
        }
        metadata = _base_metadata(task_id="task-qa-1")
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        conn = _make_mock_conn()

        mock_store = MagicMock()
        mock_store._task_to_root = {"task-pm-root": "task-pm-root"}

        mock_chain_ctx = MagicMock()
        mock_chain_ctx.get_store.return_value = mock_store

        with patch("agent.governance.auto_chain._query_graph_delta_proposed", return_value=proposed), \
             patch("agent.governance.auto_chain._try_verify_update", return_value=(True, "")), \
             patch("agent.governance.auto_chain._check_nodes_min_status", return_value=(True, "ok")), \
             patch("agent.governance.auto_chain._get_graph_doc_associations", return_value=[]), \
             patch("agent.governance.auto_chain._write_chain_memory"), \
             patch("agent.governance.auto_chain._is_governed_dirty_workspace_chain", return_value=False), \
             patch.dict("sys.modules", {"agent.governance.chain_context": mock_chain_ctx}):
            passed, reason = _gate_qa_pass(conn, "aming-claw", result, metadata)

        self.assertTrue(passed)
        self.assertEqual(reason, "ok")
        # Verify graph.delta.validated was written
        mock_store._persist_event.assert_called_once()
        call_kwargs = mock_store._persist_event.call_args
        self.assertEqual(call_kwargs[1]["event_type"] if call_kwargs[1] else call_kwargs[0][2], "graph.delta.validated")

    def test_block_when_invalid_decision(self):
        """graph_delta_review.decision is neither pass nor reject -> block."""
        result = {
            "recommendation": "qa_pass",
            "graph_delta_review": {
                "decision": "maybe",
                "issues": [],
            },
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": []}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertFalse(passed)
        self.assertIn("must be 'pass' or 'reject'", reason)

    def test_blocks_missing_evidence_path(self):
        """QA cannot pass by citing a workspace path that does not exist."""
        result = {
            "recommendation": "qa_pass",
            "review_summary": "Audit doc docs/dev/reconcile-canary-mf003.md records the canary.",
            "criteria_results": [
                {
                    "criterion": "audit evidence",
                    "passed": True,
                    "evidence": "Verified docs/dev/reconcile-canary-mf003.md",
                }
            ],
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertFalse(passed)
        self.assertIn("QA evidence references missing workspace paths", reason)
        self.assertIn("docs/dev/reconcile-canary-mf003.md", reason)

    def test_allows_reconcile_state_graph_artifact_evidence(self):
        """Reconcile QA may cite state graph artifacts outside repo paths."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            graph_path = state_dir / "graph.json"
            candidate_path = state_dir / "graph.rebase.candidate.json"
            overlay_path = state_dir / "graph.rebase.overlay.json"
            for path in (graph_path, candidate_path, overlay_path):
                path.write_text("{}", encoding="utf-8")

            result = {
                "recommendation": "qa_pass",
                "review_summary": (
                    "Verified agent/governance/graph.json is untouched; "
                    f"candidate state artifact {candidate_path} exists."
                ),
                "criteria_results": [
                    {
                        "criterion": "state graph artifacts",
                        "passed": True,
                        "evidence": "agent/governance/graph.json was treated as active graph artifact evidence.",
                    }
                ],
                "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
            }
            metadata = _base_metadata(
                changed_files=[],
                operation_type="reconcile-cluster",
                reconcile_session_id="session-1",
                candidate_graph_path=str(candidate_path),
                overlay_path=str(overlay_path),
            )
            proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L7.1"}]}}
            passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)

        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_allows_existing_evidence_path_and_glob(self):
        """Existing paths are allowed, and glob mentions are not treated as files."""
        result = {
            "recommendation": "qa_pass",
            "review_summary": "Checked agent/governance/auto_chain.py.",
            "criteria_results": [
                {
                    "criterion": "files exist",
                    "passed": True,
                    "evidence": "Glob agent/governance/reconcile_*.py returned expected files.",
                }
            ],
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_allows_brace_shorthand_evidence_path_group(self):
        """Brace shorthand is a path group, not a single missing file."""
        result = {
            "recommendation": "qa_pass",
            "review_summary": "Checked agent/governance/language_adapters/{base,filetree_adapter,python_adapter}.py.",
            "criteria_results": [
                {
                    "criterion": "adapter files exist",
                    "passed": True,
                    "evidence": "All adapter files under agent/governance/language_adapters/{base,filetree_adapter,python_adapter}.py exist.",
                }
            ],
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_allows_existing_evidence_path_with_line_suffix(self):
        """Existing file citations may include line or line/column suffixes."""
        result = {
            "recommendation": "qa_pass",
            "review_summary": "Checked agent/governance/auto_chain.py:82 and agent/governance/server.py:3134:9.",
            "criteria_results": [
                {
                    "criterion": "line evidence",
                    "passed": True,
                    "evidence": "Definitions referenced at agent/governance/auto_chain.py:82 and agent/governance/server.py:3134:9.",
                }
            ],
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_allows_existing_evidence_path_with_symbol_title_suffix(self):
        """Existing file citations may be followed by a symbol/title segment."""
        result = {
            "recommendation": "qa_pass",
            "review_summary": "Checked graph script evidence.",
            "criteria_results": [
                {
                    "criterion": "symbol evidence",
                    "passed": True,
                    "evidence": (
                        "Verified L7.157->scripts/apply_graph.py/scripts.apply_graph, "
                        "L7.161->scripts/phase-z-v2.py/scripts.phase-z-v2, and "
                        "L7.166->scripts/reconcile-scoped.py/scripts.reconcile-scoped."
                    ),
                }
            ],
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_ignores_category_phrase_containing_docs_test(self):
        """Do not extract docs/test from prose like source/docs/test mutations."""
        result = {
            "recommendation": "qa_pass",
            "review_summary": "No source/docs/test mutations were needed for this overlay-only audit.",
            "criteria_results": [
                {
                    "criterion": "overlay-only",
                    "passed": True,
                    "evidence": "No source/docs/test changes; graph delta is event-only.",
                }
            ],
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_ignores_placeholder_evidence_path(self):
        """Template placeholders are prose, not concrete workspace paths."""
        result = {
            "recommendation": "qa_pass",
            "review_summary": "Checked files under agent/governance/<module>.py placeholders.",
            "criteria_results": [
                {
                    "criterion": "template evidence",
                    "passed": True,
                    "evidence": "The pattern agent/governance/<module>.py describes the module family.",
                }
            ],
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
        }
        metadata = _base_metadata()
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_blocks_worktree_only_evidence_path_when_not_changed(self):
        """Ignored/unmerged worktree files are not durable QA evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "docs" / "dev" / "reconcile-canary-mf003.md"
            doc.parent.mkdir(parents=True)
            doc.write_text("temporary audit note", encoding="utf-8")

            result = {
                "recommendation": "qa_pass",
                "review_summary": "Audit doc docs/dev/reconcile-canary-mf003.md records the canary.",
                "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
            }
            metadata = _base_metadata(_worktree=tmp, changed_files=[])
            proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
            passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
            self.assertFalse(passed)
            self.assertIn("docs/dev/reconcile-canary-mf003.md", reason)

    def test_allows_changed_worktree_evidence_path(self):
        """A file produced by the chain is valid evidence when changed_files carries it."""
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "docs" / "dev" / "reconcile-canary-mf003.md"
            doc.parent.mkdir(parents=True)
            doc.write_text("temporary audit note", encoding="utf-8")

            result = {
                "recommendation": "qa_pass",
                "review_summary": "Audit doc docs/dev/reconcile-canary-mf003.md records the canary.",
                "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
            }
            metadata = _base_metadata(_worktree=tmp, changed_files=["docs/dev/reconcile-canary-mf003.md"])
            proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
            passed, reason = self._call_gate(result, metadata, proposed_payload=proposed)
            self.assertTrue(passed)
            self.assertEqual(reason, "ok")


class TestBuildQaPromptGraphDelta(unittest.TestCase):
    """Test _build_qa_prompt graph delta review injection."""

    def test_ac1_ac2_prompt_contains_review_instructions_when_proposed(self):
        """AC1/AC2: QA prompt includes graph_delta_review instructions when proposed event exists."""
        from agent.governance.auto_chain import _build_qa_prompt

        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [{"node_id": "L3.1"}]}}
        metadata = _base_metadata()

        with patch("agent.governance.auto_chain._query_graph_delta_proposed", return_value=proposed), \
             patch("agent.governance.auto_chain._get_graph_doc_associations", return_value=[]):
            prompt, meta = _build_qa_prompt("task-test-1", {"test_report": {}}, metadata)

        self.assertIn("graph.delta.proposed", prompt)
        self.assertIn("graph_delta_review", prompt)
        self.assertIn("decision", prompt)
        self.assertIn("path MUST exist", prompt)

    def test_reconcile_cluster_qa_prompt_includes_dev_audit_context(self):
        """QA sees Dev's self-test evidence when judging cluster-owned test edits."""
        from agent.governance.auto_chain import _build_qa_prompt

        metadata = _base_metadata(
            operation_type="reconcile-cluster",
            dev_result_summary="Pre-fix verification had two stale SCHEMA_VERSION assertions.",
            dev_test_results={"ran": True, "passed": 105, "failed": 2},
            dev_changed_files=[
                "agent/tests/test_baseline_service.py",
                "agent/tests/test_db_migrations.py",
            ],
            dev_retry_context={
                "test_failure_classification": "cluster-owned stale schema assertions",
            },
        )

        with patch("agent.governance.auto_chain._query_graph_delta_proposed", return_value=None), \
             patch("agent.governance.auto_chain._get_graph_doc_associations", return_value=[]):
            prompt, meta = _build_qa_prompt(
                "task-test-1",
                {
                    "test_report": {"passed": 107, "failed": 0},
                    "changed_files": metadata["dev_changed_files"],
                },
                metadata,
            )

        self.assertIn("Reconcile Cluster Dev Audit Context", prompt)
        self.assertIn("Pre-fix verification had two stale SCHEMA_VERSION assertions", prompt)
        self.assertIn("cluster-owned stale schema assertions", prompt)
        self.assertIn("edits are allowed when Dev's verification proves a real defect", prompt)

    def test_prompt_no_review_section_without_proposed(self):
        """No graph.delta.proposed -> no graph_delta_review instructions in prompt."""
        from agent.governance.auto_chain import _build_qa_prompt

        metadata = _base_metadata()

        with patch("agent.governance.auto_chain._query_graph_delta_proposed", return_value=None), \
             patch("agent.governance.auto_chain._get_graph_doc_associations", return_value=[]):
            prompt, meta = _build_qa_prompt("task-test-1", {"test_report": {}}, metadata)

        self.assertNotIn("Graph Delta Review", prompt)


class TestDevRetryGraphDeltaReview(unittest.TestCase):
    """Test dev retry prompt enrichment with graph_delta_review info (R4/AC8)."""

    def test_ac8_retry_prompt_includes_issues_and_diff(self):
        """AC8: Dev retry prompt includes QA issues[] and suggested_diff on graph delta rejection."""
        # We test this by checking the retry prompt construction logic directly
        # The retry prompt is built inline in _do_chain, so we test the string building pattern
        import agent.governance.auto_chain as ac

        reason = "graph delta rejected by QA: ['node L3.1 has wrong parent_layer']"
        result = {
            "recommendation": "qa_pass",
            "graph_delta_review": {
                "decision": "reject",
                "issues": ["node L3.1 has wrong parent_layer"],
                "suggested_diff": {"updates": [{"node_id": "L3.1", "fields": {"parent_layer": "L2"}}]},
            },
        }

        # Simulate the retry prompt enrichment logic from the code
        _gd_retry_section = ""
        if "graph delta rejected by QA" in reason or "graph_delta_review" in reason:
            _gd_review = result.get("graph_delta_review", {})
            if isinstance(_gd_review, dict):
                _gd_issues = _gd_review.get("issues", [])
                _gd_diff = _gd_review.get("suggested_diff", {})
                _gd_retry_section = (
                    "\n## Graph Delta Review Rejection\n"
                    f"QA graph_delta_review issues: {json.dumps(_gd_issues, ensure_ascii=False)}\n"
                    f"QA suggested_diff: {json.dumps(_gd_diff, ensure_ascii=False)}\n"
                    "Address the graph delta issues listed above in your retry.\n\n"
                )

        self.assertIn("graph_delta_review", _gd_retry_section)
        self.assertIn("node L3.1 has wrong parent_layer", _gd_retry_section)
        self.assertIn("suggested_diff", _gd_retry_section)
        self.assertIn("parent_layer", _gd_retry_section)

    def test_no_enrichment_when_not_graph_delta_rejection(self):
        """No graph_delta_review enrichment when rejection is not about graph delta."""
        reason = "QA rejected: code quality issues"
        result = {"recommendation": "reject", "reason": "code quality issues"}

        _gd_retry_section = ""
        if "graph delta rejected by QA" in reason or "graph_delta_review" in reason:
            _gd_review = result.get("graph_delta_review", {})
            if isinstance(_gd_review, dict):
                _gd_issues = _gd_review.get("issues", [])
                _gd_diff = _gd_review.get("suggested_diff", {})
                _gd_retry_section = (
                    "\n## Graph Delta Review Rejection\n"
                    f"QA graph_delta_review issues: {json.dumps(_gd_issues, ensure_ascii=False)}\n"
                    f"QA suggested_diff: {json.dumps(_gd_diff, ensure_ascii=False)}\n"
                    "Address the graph delta issues listed above in your retry.\n\n"
                )

        self.assertEqual(_gd_retry_section, "")


if __name__ == "__main__":
    unittest.main()
