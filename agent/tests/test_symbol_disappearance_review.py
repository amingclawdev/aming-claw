"""Tests for agent.governance.symbol_disappearance_review.

Covers AC1, AC2, R1, R5 of the PR2 Atomic Swap + Disappearance Review PRD.
"""
from __future__ import annotations

import json
import os
import sys

# Ensure agent package is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.symbol_disappearance_review import (
    OBSERVER_DECISIONS,
    REMOVAL_REASONS,
    classify_removal,
    detect_governance_markers,
    diff_removed_nodes,
    require_observer_decision,
    review_report,
)


# ---------------------------------------------------------------------------
# Constant-shape tests
# ---------------------------------------------------------------------------

def test_removal_reasons_contains_all_five():
    expected = {
        "files_relocated",
        "files_deleted",
        "merged_into_other_node",
        "low_confidence_inference",
        "no_matching_call_topology",
    }
    assert set(REMOVAL_REASONS) == expected
    assert len(REMOVAL_REASONS) == 5


def test_observer_decisions_contains_all_four():
    expected = {
        "approve_removal",
        "map_to_new_node",
        "preserve_as_supplement",
        "block_swap",
    }
    assert set(OBSERVER_DECISIONS) == expected
    assert len(OBSERVER_DECISIONS) == 4


# ---------------------------------------------------------------------------
# diff_removed_nodes
# ---------------------------------------------------------------------------

def test_diff_removed_nodes_returns_present_in_old_absent_in_new():
    old_graph = {
        "nodes": [
            {"node_id": "L1.1", "title": "Alpha", "primary": ["a/old.py"]},
            {"node_id": "L1.2", "title": "Beta", "primary": ["b/keep.py"]},
            {"node_id": "L2.5", "title": "Gamma", "primary": ["c/gone.py"]},
        ]
    }
    new_graph = {
        "nodes": [
            {"node_id": "L1.2", "title": "Beta", "primary": ["b/keep.py"]},
        ]
    }
    removed = diff_removed_nodes(old_graph, new_graph)
    ids = [n["node_id"] for n in removed]
    assert ids == ["L1.1", "L2.5"]


def test_diff_removed_nodes_handles_dict_form_of_nodes():
    old_graph = {
        "nodes": {
            "L1.1": {"node_id": "L1.1", "primary": ["a.py"]},
            "L1.2": {"node_id": "L1.2", "primary": ["b.py"]},
        }
    }
    new_graph = {
        "nodes": {"L1.2": {"node_id": "L1.2", "primary": ["b.py"]}},
    }
    removed = diff_removed_nodes(old_graph, new_graph)
    assert [n["node_id"] for n in removed] == ["L1.1"]


# ---------------------------------------------------------------------------
# classify_removal
# ---------------------------------------------------------------------------

def test_classify_removal_files_relocated():
    """Same file basename appears under a different directory in the new graph."""
    node = {"node_id": "L1.1", "primary": ["agent/old/foo.py"]}
    old_graph = {"nodes": [node]}
    new_graph = {"nodes": [{"node_id": "L1.99", "primary": ["agent/new/foo.py"]}]}
    assert classify_removal(node, old_graph, new_graph) == "files_relocated"


def test_classify_removal_files_deleted_via_status_marker():
    node = {"node_id": "L1.1", "primary": ["agent/dead.py"], "status": "deleted"}
    old_graph = {"nodes": [node]}
    new_graph = {"nodes": []}
    assert classify_removal(node, old_graph, new_graph) == "files_deleted"


def test_classify_removal_files_deleted_via_removed_files_list():
    node = {"node_id": "L1.1", "primary": ["agent/dead.py"]}
    old_graph = {"nodes": [node]}
    new_graph = {"nodes": [], "removed_files": ["agent/dead.py"]}
    assert classify_removal(node, old_graph, new_graph) == "files_deleted"


def test_classify_removal_merged_into_other_node():
    node = {
        "node_id": "L1.1",
        "primary": ["agent/old.py"],
        "merged_into": "L1.2",
    }
    old_graph = {"nodes": [node]}
    new_graph = {
        "nodes": [{"node_id": "L1.2", "primary": ["agent/new.py"]}],
    }
    assert classify_removal(node, old_graph, new_graph) == "merged_into_other_node"


def test_classify_removal_low_confidence_inference():
    node = {"node_id": "L1.1", "primary": ["agent/foo.py"], "confidence": 0.4}
    old_graph = {"nodes": [node]}
    # nothing referencing the file in new graph + low confidence
    new_graph = {"nodes": []}
    assert classify_removal(node, old_graph, new_graph) == "low_confidence_inference"


def test_classify_removal_falls_back_to_no_matching_call_topology():
    node = {"node_id": "L1.1", "primary": ["agent/foo.py"]}
    old_graph = {"nodes": [node]}
    new_graph = {"nodes": []}
    assert classify_removal(node, old_graph, new_graph) == "no_matching_call_topology"


# ---------------------------------------------------------------------------
# require_observer_decision
# ---------------------------------------------------------------------------

def test_require_observer_decision_missing_when_no_decision():
    removed = [{"node_id": "L1.1"}, {"node_id": "L1.2"}]
    out = require_observer_decision(removed, decisions={"L1.1": "approve_removal"})
    assert out["ok"] is False
    assert out["missing"] == ["L1.2"]
    assert out["blocked"] == []


def test_require_observer_decision_blocked_when_block_swap():
    removed = [{"node_id": "L1.1"}, {"node_id": "L1.2"}]
    out = require_observer_decision(
        removed,
        decisions={"L1.1": "approve_removal", "L1.2": "block_swap"},
    )
    assert out["ok"] is False
    assert out["missing"] == []
    assert out["blocked"] == ["L1.2"]


def test_require_observer_decision_ok_when_all_valid_and_unblocked():
    removed = [{"node_id": "L1.1"}, {"node_id": "L1.2"}]
    out = require_observer_decision(
        removed,
        decisions={
            "L1.1": "approve_removal",
            "L1.2": "preserve_as_supplement",
        },
    )
    assert out == {"ok": True, "missing": [], "blocked": []}


def test_require_observer_decision_unknown_string_treated_as_missing():
    removed = [{"node_id": "L1.1"}]
    out = require_observer_decision(removed, decisions={"L1.1": "lol_typo"})
    assert out["ok"] is False
    assert "L1.1" in out["missing"]


# ---------------------------------------------------------------------------
# detect_governance_markers
# ---------------------------------------------------------------------------

def test_detect_governance_markers_b36_via_governance_markers_list():
    node = {"node_id": "L7.21", "governance_markers": ["b36_dangling_l7"]}
    out = detect_governance_markers(node)
    assert out["b36_dangling_l7"] is True
    assert out["legacy_waiver"] is False
    assert out["manual_carve_out"] is False


def test_detect_governance_markers_b36_via_layer_heuristic():
    """An L7 node with no qa_passed flag is treated as B36 dangling-L7."""
    node = {"node_id": "L7.99", "layer": "L7", "qa_passed": False}
    out = detect_governance_markers(node)
    assert out["b36_dangling_l7"] is True


def test_detect_governance_markers_legacy_waiver():
    node = {"node_id": "L1.1", "waiver": "exempt-2025-04-01"}
    out = detect_governance_markers(node)
    assert out["legacy_waiver"] is True


def test_detect_governance_markers_manual_carve_out():
    node = {"node_id": "L1.1", "manual_carve_out": True}
    out = detect_governance_markers(node)
    assert out["manual_carve_out"] is True


# ---------------------------------------------------------------------------
# Integration: review_report round trip is JSON-serialisable
# ---------------------------------------------------------------------------

def test_review_report_round_trip_is_json_serialisable():
    old_graph = {
        "nodes": [
            {"node_id": "L1.1", "title": "Alpha", "primary": ["a/old.py"]},
            {"node_id": "L1.2", "title": "Beta", "primary": ["b/keep.py"]},
            {"node_id": "L7.21", "title": "MigrationSM",
             "primary": ["agent/governance/migration_state_machine.py"],
             "status": "deleted"},
        ]
    }
    new_graph = {
        "nodes": [
            {"node_id": "L1.2", "title": "Beta", "primary": ["b/keep.py"]},
            {"node_id": "L7.30", "title": "AtomicSwap",
             "primary": ["agent/governance/symbol_swap.py"]},
        ]
    }
    decisions = {"L1.1": "approve_removal", "L7.21": "approve_removal"}
    report = review_report(old_graph, new_graph, decisions)

    # round-trip through json to assert serialisability
    serialised = json.dumps(report)
    deserialised = json.loads(serialised)
    assert deserialised["removed_count"] == 2
    item_ids = {item["node_id"] for item in deserialised["items"]}
    assert item_ids == {"L1.1", "L7.21"}
    assert deserialised["decision_status"]["ok"] is True
    assert set(deserialised["removal_reasons"]) == set(REMOVAL_REASONS)
    assert set(deserialised["observer_decisions"]) == set(OBSERVER_DECISIONS)
