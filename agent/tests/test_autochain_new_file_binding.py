"""Tests for Rule I (test file binding) and Rule J (src module proposal) in auto_chain._infer_graph_delta."""
import re
import pytest
from unittest.mock import patch, MagicMock


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


@pytest.fixture(autouse=True)
def mock_graph_load():
    """Patch project_service.load_project_graph for all tests."""
    with patch("agent.governance.project_service.load_project_graph") as mock_lpg:
        # Default: empty graph
        mock_lpg.return_value = _make_graph({})
        yield mock_lpg


def _call_infer(pm_nodes, changed_files, dev_delta=None, dev_result=None, graph_nodes=None, mock_lpg=None):
    """Helper to call _infer_graph_delta with optional graph setup."""
    from agent.governance.auto_chain import _infer_graph_delta

    if dev_result is None:
        dev_result = {"project_id": "aming-claw", "task_id": "test-task-1"}

    if graph_nodes is not None and mock_lpg is not None:
        mock_lpg.return_value = _make_graph(graph_nodes)

    delta, rule_hits, inferred_from, source = _infer_graph_delta(
        pm_nodes, changed_files, dev_delta, dev_result
    )
    return delta, rule_hits, inferred_from, source


class TestRuleI_TestFileBinding:
    """AC-A6-1: Test file binding to graph node via fuzzy matching."""

    def test_test_file_binds_to_matching_node(self, mock_graph_load):
        """AC-A6-1: test_foo.py binds to node with primary agent/foo.py."""
        graph_nodes = {
            "L7.5": {
                "primary": ["agent/foo.py"],
                "title": "Foo Module",
                "secondary": [],
            }
        }

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/tests/test_foo.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        # Should have an update entry for L7.5 with test field
        rule_i_hits = [h for h in rule_hits if h.get("rule") == "I"]
        assert len(rule_i_hits) == 1
        assert rule_i_hits[0]["bound_to"] == "L7.5"

        # Check updates contain test binding
        test_updates = [u for u in delta["updates"] if u["node_id"] == "L7.5"]
        assert len(test_updates) >= 1
        test_fields = test_updates[0]["fields"]
        assert "test" in test_fields
        assert "agent/tests/test_foo.py" in test_fields["test"]

    def test_test_file_no_match_warns_no_exception(self, mock_graph_load):
        """R6: No fuzzy match for test file logs warning but does not raise."""
        graph_nodes = {
            "L7.1": {
                "primary": ["agent/something_totally_different.py"],
                "title": "Unrelated Module",
            }
        }

        # Should not raise
        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/tests/test_xyz_unique.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        # No Rule I hit (no match >= 0.85)
        rule_i_hits = [h for h in rule_hits if h.get("rule") == "I"]
        assert len(rule_i_hits) == 0

    def test_test_file_already_covered_by_rule_a(self, mock_graph_load):
        """R3: Test file already in covered_primaries is skipped."""
        pm_nodes = [
            {
                "node_id": "L7.10",
                "title": "Test Node",
                "parent_layer": "L7",
                "primary": ["agent/tests/test_foo.py"],
                "deps": [],
                "description": "PM proposed",
            }
        ]

        graph_nodes = {
            "L7.5": {
                "primary": ["agent/foo.py"],
                "title": "Foo Module",
            }
        }

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=pm_nodes,
            changed_files=["agent/tests/test_foo.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        # Rule A should match, Rule I should NOT fire for this file
        rule_i_hits = [h for h in rule_hits if h.get("rule") == "I"]
        assert len(rule_i_hits) == 0


class TestRuleJ_SrcModuleBinding:
    """AC-A6-2, AC-A6-3: Src module proposal or secondary binding."""

    def test_new_src_module_creates_l7_node(self, mock_graph_load):
        """AC-A6-2: brand_new_module.py with no match -> new L7 node."""
        graph_nodes = {
            "L7.3": {
                "primary": ["agent/governance/existing.py"],
                "title": "Existing Module",
            }
        }

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/brand_new_module.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        # Should have a creates entry with created_by='autochain-new-file-binding'
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        assert len(rule_j_hits) == 1
        assert rule_j_hits[0]["action"] == "new_l7_node"

        new_creates = [
            c for c in delta["creates"]
            if c.get("created_by") == "autochain-new-file-binding"
        ]
        assert len(new_creates) == 1
        assert new_creates[0]["primary"] == ["agent/governance/brand_new_module.py"]
        assert new_creates[0]["node_id"] == "L7.4"  # max existing is L7.3 -> next is L7.4

    def test_existing_primary_no_duplicate_create(self, mock_graph_load):
        """AC-A6-3: File already in graph node primary -> Rule D update only, no duplicate create."""
        graph_nodes = {
            "L7.2": {
                "primary": ["agent/governance/foo.py"],
                "title": "Foo",
            }
        }

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/foo.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        # Rule D should fire
        rule_d_hits = [h for h in rule_hits if h.get("rule") == "D"]
        assert len(rule_d_hits) == 1

        # Rule J should NOT fire (file already covered by Rule D)
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J"]
        assert len(rule_j_hits) == 0

        # No creates with autochain-new-file-binding
        new_creates = [
            c for c in delta["creates"]
            if c.get("created_by") == "autochain-new-file-binding"
        ]
        assert len(new_creates) == 0

    def test_rule_b_covered_file_not_rebound(self, mock_graph_load):
        """AC-A6-4: File matched by Rule B via @route is NOT re-bound by Rule J."""
        graph_nodes = {
            "L7.1": {
                "primary": ["agent/other.py"],
                "title": "Other",
            }
        }

        # Mock file read for Rule B @route detection
        rel_path = "agent/governance/routed_module.py"
        route_content = '@app.route("/api/test")\ndef test_endpoint(): pass\n'

        import builtins
        original_open = builtins.open

        def patched_open(path, *args, **kwargs):
            if "routed_module.py" in str(path):
                from io import StringIO
                return StringIO(route_content)
            return original_open(path, *args, **kwargs)

        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=patched_open):
            delta, rule_hits, _, _ = _call_infer(
                pm_nodes=[],
                changed_files=[rel_path],
                mock_lpg=mock_graph_load,
                graph_nodes=graph_nodes,
            )

        # Rule B should have fired
        rule_b_hits = [h for h in rule_hits if h.get("rule") == "B"]
        assert len(rule_b_hits) >= 1

        # Rule J should NOT have fired for the same file (dedup)
        rule_j_hits = [h for h in rule_hits if h.get("rule") == "J" and h.get("src_file") == rel_path]
        assert len(rule_j_hits) == 0

    def test_allocate_next_id_increments(self, mock_graph_load):
        """R5: allocate_next_id finds max L7.N and returns L7.N+1."""
        graph_nodes = {
            "L7.1": {"primary": ["agent/a.py"], "title": "A"},
            "L7.5": {"primary": ["agent/b.py"], "title": "B"},
            "L7.10": {"primary": ["agent/c.py"], "title": "C"},
        }

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/brand_new.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        new_creates = [
            c for c in delta["creates"]
            if c.get("created_by") == "autochain-new-file-binding"
        ]
        assert len(new_creates) == 1
        assert new_creates[0]["node_id"] == "L7.11"

    def test_derive_title_from_path(self, mock_graph_load):
        """R5: derive_title_from_path uses stem.replace('_',' ').title()."""
        graph_nodes = {}

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/governance/my_cool_module.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        new_creates = [
            c for c in delta["creates"]
            if c.get("created_by") == "autochain-new-file-binding"
        ]
        assert len(new_creates) == 1
        assert new_creates[0]["title"] == "My Cool Module"


class TestRuleIJ_FuzzyScoring:
    """R5: Fuzzy scoring tests."""

    def test_stem_no_dir_match_below_threshold(self, mock_graph_load):
        """stem(0.5) + title(0.2) = 0.7 < 0.85 -> no bind for test file."""
        graph_nodes = {
            "L7.1": {
                "primary": ["agent/governance/foo.py"],
                "title": "Foo",
            }
        }

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/tests/test_foo.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        # stem match gives 0.5, title keyword gives 0.2 = 0.7 < 0.85
        rule_i_hits = [h for h in rule_hits if h.get("rule") == "I"]
        assert len(rule_i_hits) == 0

    def test_stem_match_in_same_dir_binds(self, mock_graph_load):
        """same_dir(0.4) + stem(0.5) = 0.9 >= 0.85 -> binds."""
        graph_nodes = {
            "L7.1": {
                "primary": ["agent/tests/foo.py"],
                "title": "Something Else",
            }
        }

        delta, rule_hits, _, _ = _call_infer(
            pm_nodes=[],
            changed_files=["agent/tests/test_foo.py"],
            mock_lpg=mock_graph_load,
            graph_nodes=graph_nodes,
        )

        rule_i_hits = [h for h in rule_hits if h.get("rule") == "I"]
        assert len(rule_i_hits) == 1
        assert rule_i_hits[0]["score"] >= 0.85
