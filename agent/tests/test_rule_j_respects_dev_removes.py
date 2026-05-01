"""Tests for PR1e: Rule J respects dev_delta.removes + filesystem truth.

Background:
    PR1c wired prd_declarations through _emit_or_infer_graph_delta to
    _infer_graph_delta so that PM-declared file deletions/remaps suppress
    phantom Rule J creates. PR1e extends Rule J's `unbound_src` filter with
    two additional truth sources:
      1. dev_delta.removes — primaries of nodes the Dev declared as removed
      2. Filesystem truth — files no longer present on disk

    All three filter clauses (PM declarations + dev removes + filesystem) are
    AND-conjuncted to the existing predicate, strictly NARROWING the set of
    files that Rule J fuzzy-binds or proposes new L7 nodes for.

This module covers:
  - test_dev_removes_skips_rule_j: dev_delta.removes node id resolves to
    a primary path which Rule J then must skip.
  - test_filesystem_deleted_skips_rule_j: file removed from disk → no Rule J.
  - test_backward_compat_no_removes: with no signals, Rule J still fires for
    real files (regression guard for PR1c behavior).
  - test_combined_filter_pm_declarations_plus_dev_removes: PM declares the
    file as unmapped AND dev declares the node as removed; no double-skip
    error and no phantom create.
  - test_dev_removes_missing_node_does_not_raise: AC8 — dev_delta.removes
    referencing a non-existent node id must silently swallow the lookup error.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

# Use agent.governance.* path so the production code's relative
# `from . import project_service` resolves to the SAME module object that
# our patch("agent.governance.project_service.load_project_graph") targets.
from agent.governance.auto_chain import (  # noqa: E402
    _file_deleted_in_worktree,
    _infer_graph_delta,
)


def _make_graph(nodes_dict):
    """Create a mock AcceptanceGraph with given nodes.

    nodes_dict: {node_id: {primary: [...], title: ..., ...}}
    """
    graph = MagicMock()
    graph.list_nodes.return_value = list(nodes_dict.keys())

    def get_node(nid):
        if nid not in nodes_dict:
            raise KeyError(nid)
        return dict(nodes_dict[nid])

    graph.get_node.side_effect = get_node
    return graph


class _RuleJBaseTest(unittest.TestCase):
    """Shared helpers."""

    def _call_infer(self, pm_nodes, changed_files, dev_delta=None,
                    graph_nodes=None, prd_declarations=None,
                    file_exists=True):
        """Invoke _infer_graph_delta with mocked graph + os.path.exists."""
        if graph_nodes is None:
            graph_nodes = {}

        graph = _make_graph(graph_nodes)

        # Always treat changed files as still present unless overridden
        def _fake_exists(p):
            if isinstance(file_exists, dict):
                norm = str(p).replace("\\", "/")
                # Tail match on the relative path
                for k, v in file_exists.items():
                    if norm.endswith(k):
                        return v
                return True
            return bool(file_exists)

        with patch("agent.governance.project_service.load_project_graph",
                   return_value=graph), \
             patch("os.path.exists", side_effect=_fake_exists):
            dev_result = {"project_id": "aming-claw", "task_id": "test-task"}
            return _infer_graph_delta(
                pm_nodes, changed_files, dev_delta, dev_result,
                prd_declarations=prd_declarations,
            )


class TestFileDeletedHelper(unittest.TestCase):
    """Unit-level tests for _file_deleted_in_worktree (AC2)."""

    def test_existing_file_returns_false(self):
        with patch("os.path.exists", return_value=True):
            self.assertFalse(_file_deleted_in_worktree("agent/foo.py", {}))

    def test_missing_file_returns_true(self):
        with patch("os.path.exists", return_value=False):
            self.assertTrue(_file_deleted_in_worktree("agent/gone.py", {}))

    def test_exception_returns_false_defensively(self):
        def _raise(_p):
            raise OSError("simulated filesystem error")

        with patch("os.path.exists", side_effect=_raise):
            # Must NOT raise; defensive default = "not deleted"
            self.assertFalse(_file_deleted_in_worktree("agent/x.py", {}))


class TestDevRemovesSkipsRuleJ(_RuleJBaseTest):
    """AC5(a): dev_delta.removes suppresses Rule J for the removed node's primary."""

    def test_dev_removes_skips_rule_j(self):
        """Dev removes L7.42 (primary=agent/governance/old.py); Rule J must
        not propose any new node or secondary-bind for that file."""
        graph_nodes = {
            "L7.42": {
                "primary": ["agent/governance/old.py"],
                "title": "Old Module",
            },
            "L7.5": {
                "primary": ["agent/governance/unrelated.py"],
                "title": "Unrelated",
            },
        }
        dev_delta = {
            "creates": [],
            "updates": [],
            "links": [],
            "removes": ["L7.42"],
        }
        delta, rule_hits, _, _ = self._call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/old.py"],
            dev_delta=dev_delta,
            graph_nodes=graph_nodes,
        )

        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        self.assertEqual(
            rule_j_hits, [],
            f"Rule J fired despite dev_delta.removes: {rule_j_hits!r}",
        )
        # Also: no auto-binding create for the removed file
        new_creates = [
            c for c in delta.get("creates", [])
            if c.get("created_by") == "autochain-new-file-binding"
            and "agent/governance/old.py" in (
                c.get("primary") if isinstance(c.get("primary"), list)
                else [c.get("primary", "")]
            )
        ]
        self.assertEqual(new_creates, [])


class TestDevRemovesDictForm(_RuleJBaseTest):
    """dev_delta.removes accepts {'node_id': '...'} dict form."""

    def test_dev_removes_dict_entry(self):
        graph_nodes = {
            "L7.42": {"primary": ["agent/governance/old.py"], "title": "Old"},
        }
        dev_delta = {
            "creates": [], "updates": [], "links": [],
            "removes": [{"node_id": "L7.42"}],
        }
        _delta, rule_hits, _, _ = self._call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/old.py"],
            dev_delta=dev_delta,
            graph_nodes=graph_nodes,
        )
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        self.assertEqual(rule_j_hits, [])


class TestDevRemovesMissingNode(_RuleJBaseTest):
    """AC8: dev_delta.removes referencing a non-existent node must not raise."""

    def test_dev_removes_missing_node_does_not_raise(self):
        graph_nodes = {
            "L7.5": {"primary": ["agent/foo.py"], "title": "Foo"},
        }
        dev_delta = {
            "creates": [], "updates": [], "links": [],
            "removes": ["L7.NONEXISTENT"],
        }
        # Must NOT raise even though L7.NONEXISTENT is missing
        delta, rule_hits, _, _ = self._call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/brand_new.py"],
            dev_delta=dev_delta,
            graph_nodes=graph_nodes,
        )
        # Rule J should still fire for the unrelated brand_new.py
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        self.assertGreaterEqual(len(rule_j_hits), 1)


class TestFilesystemDeletedSkipsRuleJ(_RuleJBaseTest):
    """AC5(b): file no longer on disk → Rule J skips it."""

    def test_filesystem_deleted_skips_rule_j(self):
        graph_nodes = {
            "L7.5": {"primary": ["agent/governance/other.py"], "title": "Other"},
        }
        # File gone from disk:
        delta, rule_hits, _, _ = self._call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/ghost.py"],
            graph_nodes=graph_nodes,
            file_exists={"agent/governance/ghost.py": False},
        )
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        self.assertEqual(
            rule_j_hits, [],
            f"Rule J fired despite ghost.py absent from filesystem: {rule_j_hits!r}",
        )
        # No autochain-new-file-binding create for ghost.py
        new_creates = [
            c for c in delta.get("creates", [])
            if c.get("created_by") == "autochain-new-file-binding"
        ]
        for c in new_creates:
            primary = c.get("primary", [])
            if isinstance(primary, str):
                primary = [primary]
            self.assertNotIn("agent/governance/ghost.py", primary)


class TestBackwardCompatNoRemoves(_RuleJBaseTest):
    """AC5(c): With no PM declarations, no dev removes, and file present,
    Rule J STILL fires (regression guard against over-aggressive filtering)."""

    def test_backward_compat_no_removes(self):
        # Empty graph + brand new file present on disk → Rule J fires
        delta, rule_hits, _, _ = self._call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/brand_new_module.py"],
            dev_delta=None,
            graph_nodes={},
            prd_declarations=None,
            file_exists=True,
        )
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        self.assertEqual(
            len(rule_j_hits), 1,
            f"Rule J should still fire when no signals are provided: hits={rule_j_hits!r}",
        )
        new_creates = [
            c for c in delta.get("creates", [])
            if c.get("created_by") == "autochain-new-file-binding"
        ]
        self.assertEqual(len(new_creates), 1)
        self.assertEqual(
            new_creates[0]["primary"],
            ["agent/governance/brand_new_module.py"],
        )


class TestCombinedFilterPmDeclarationsPlusDevRemoves(_RuleJBaseTest):
    """AC5(d): PM declares the file as unmapped AND dev declares the node
    as removed. Both signals point to the same file — must not error and
    must produce zero phantom creates."""

    def test_combined_filter_pm_declarations_plus_dev_removes(self):
        graph_nodes = {
            "L7.42": {
                "primary": ["agent/governance/migration_state_machine.py"],
                "title": "Migration State Machine",
            },
        }
        dev_delta = {
            "creates": [], "updates": [], "links": [],
            "removes": ["L7.42"],
        }
        prd_declarations = {
            "removed_nodes": [],
            "unmapped_files": ["agent/governance/migration_state_machine.py"],
            "renamed_nodes": [],
            "remapped_files": [],
        }
        delta, rule_hits, _, _ = self._call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/migration_state_machine.py"],
            dev_delta=dev_delta,
            graph_nodes=graph_nodes,
            prd_declarations=prd_declarations,
            # File deleted from disk too — all 3 signals agree
            file_exists={"migration_state_machine.py": False},
        )
        # No Rule J hit in either secondary_bind or new_l7_node form
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        self.assertEqual(rule_j_hits, [])
        # No phantom create at all
        positive_creates = [
            c for c in delta.get("creates", [])
            if c.get("op") != "remove_node"
            and c.get("source") != "pm_declaration"
            and c.get("created_by") == "autochain-new-file-binding"
        ]
        self.assertEqual(positive_creates, [])


if __name__ == "__main__":
    unittest.main()
