"""CR5 — gatekeeper overlay apply + 2-stage validation + 4-level state preservation.

Twelve tests exercise the apply_reconcile_cluster_to_overlay() path and its
helpers in agent/governance/auto_chain.py.  All tests are pure: tmp_path +
monkeypatch; no live network/db dependency; the deferred queue and chain
event emission are mocked.

Acceptance reference: see PRD AC1..AC14 in task-1777731172-800989.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from governance import auto_chain  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_ID = "p-cr5"


@pytest.fixture()
def gov_dir(tmp_path: Path) -> Path:
    d = tmp_path / "governance"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def graph_path(gov_dir: Path) -> Path:
    """A graph.json with two pre-existing nodes used as match candidates."""
    p = gov_dir / "graph.json"
    p.write_text(json.dumps({
        "L7.1": {
            "node_id": "L7.1",
            "primary": ["agent/governance/foo.py"],
            "secondary": ["agent/governance/foo_helpers.py"],
            "test": ["agent/tests/test_foo.py"],
            "parent_layer": "L7",
            "title": "foo",
            "verify_status": "qa_pass",
        },
        "L7.2": {
            "node_id": "L7.2",
            "primary": ["agent/governance/bar.py"],
            "secondary": [],
            "test": ["agent/tests/test_bar.py"],
            "parent_layer": "L7",
            "title": "bar",
            "verify_status": "t2_pass",
        },
    }, indent=2, sort_keys=True), encoding="utf-8")
    return p


@pytest.fixture()
def overlay_path(gov_dir: Path) -> Path:
    """Pre-seed an empty overlay (mimics reconcile_session.start_session)."""
    p = gov_dir / "graph.rebase.overlay.json"
    p.write_text(json.dumps({
        "session_id": "sess-1",
        "project_id": PROJECT_ID,
        "nodes": {},
    }, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _meta(**extra):
    base = {
        "operation_type": "reconcile-cluster",
        "session_id": "sess-1",
        "cluster_fingerprint": "cluster-test",
        "task_id": "t-merge-1",
        "chain_id": "root-1",
    }
    base.update(extra)
    return base


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------

def test_pm_no_proposed_nodes_fatal(graph_path: Path, overlay_path: Path):
    """PM PRD with operation_type='reconcile-cluster' and empty proposed_nodes
    is rejected with a FATAL preflight outcome — gate result NOT pass; reason
    mentions both 'reconcile-cluster' AND 'proposed_nodes'."""
    pm_prd = {"feature": "x", "proposed_nodes": []}
    dev_result = {"graph_delta": {"creates": []}}
    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-1",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is False
    assert res["fatal"] is True
    assert res["stage"] == "preflight_pm"
    assert "reconcile-cluster" in res["reason"]
    assert "proposed_nodes" in res["reason"]


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------

def test_dev_count_mismatch_fatal(graph_path: Path, overlay_path: Path):
    """FATAL when len(graph_delta.creates) != len(pm.proposed_nodes)."""
    pm_prd = {"proposed_nodes": [
        {"primary": ["agent/governance/new_a.py"], "parent_layer": "L7"},
        {"primary": ["agent/governance/new_b.py"], "parent_layer": "L7"},
    ]}
    dev_result = {"graph_delta": {"creates": [
        {"primary": ["agent/governance/new_a.py"], "parent_layer": "L7"},
    ]}}
    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-2",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is False
    assert res["fatal"] is True
    assert res["stage"] == "preflight_dev"
    # Count mismatch must show both numbers in the reason
    assert "1" in res["reason"] and "2" in res["reason"]


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------

def test_dev_primary_mismatch_fatal(graph_path: Path, overlay_path: Path):
    """FATAL when primaries don't match 1:1 even when counts match."""
    pm_prd = {"proposed_nodes": [
        {"primary": ["agent/governance/new_a.py"], "parent_layer": "L7"},
        {"primary": ["agent/governance/new_b.py"], "parent_layer": "L7"},
    ]}
    dev_result = {"graph_delta": {"creates": [
        {"primary": ["agent/governance/new_a.py"], "parent_layer": "L7"},
        {"primary": ["agent/governance/different_c.py"], "parent_layer": "L7"},
    ]}}
    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-3",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is False
    assert res["fatal"] is True
    assert res["stage"] == "preflight_dev"
    assert "primaries" in res["reason"]


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------

def test_gate_unresolvable_dep_block(graph_path: Path, overlay_path: Path):
    """Merge blocked when graph_delta entry has missing layer or unresolvable dep.

    Two scenarios — missing parent_layer and unresolvable dep — both block.
    """
    # Scenario A — missing parent_layer
    pm_prd = {"proposed_nodes": [
        {"primary": ["agent/governance/new_a.py"], "parent_layer": "L7"},
    ]}
    dev_result_missing_layer = {"graph_delta": {"creates": [
        {"primary": ["agent/governance/new_a.py"]},  # parent_layer absent
    ]}}
    res_a = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-4a",
        pm_prd=pm_prd,
        dev_result=dev_result_missing_layer,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res_a["applied"] is False
    assert res_a["fatal"] is True
    assert res_a["stage"] == "structural"

    # Scenario B — unresolvable dep
    dev_result_bad_dep = {"graph_delta": {"creates": [
        {
            "primary": ["agent/governance/new_a.py"],
            "parent_layer": "L7",
            "deps": ["L9.999"],  # nonexistent
        },
    ]}}
    res_b = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-4b",
        pm_prd=pm_prd,
        dev_result=dev_result_bad_dep,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res_b["applied"] is False
    assert res_b["fatal"] is True
    assert res_b["stage"] == "structural"
    assert "unresolvable" in res_b["reason"]


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------

def test_state_preservation_roundtrip_exact_match(graph_path: Path, overlay_path: Path):
    """exact_match transfers verify_status as-is and provenance is recorded."""
    # Re-use existing L7.1 (qa_pass) — proposed identical primary+parent_layer+secondary+test
    pm_prd = {"proposed_nodes": [
        {
            "primary": ["agent/governance/foo.py"],
            "parent_layer": "L7",
            "secondary": ["agent/governance/foo_helpers.py"],
            "test": ["agent/tests/test_foo.py"],
        },
    ]}
    dev_result = {"graph_delta": {"creates": pm_prd["proposed_nodes"]}}
    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-5",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is True, res
    assert len(res["state_transfers"]) == 1
    transfer = res["state_transfers"][0]
    assert transfer["tier"] == auto_chain.MATCH_TIER_EXACT == "exact_match"
    # Transferred as-is from L7.1's qa_pass
    assert transfer["matched_node_id"] == "L7.1"
    assert transfer["new_verify_status"] == "qa_pass"

    # Provenance recorded in overlay node metadata.rebased_from
    overlay_doc = json.loads(overlay_path.read_text(encoding="utf-8"))
    nid = transfer["node_id"]
    assert nid in overlay_doc["nodes"]
    rebased = overlay_doc["nodes"][nid]["metadata"]["rebased_from"]
    assert rebased["tier"] == "exact_match"
    assert rebased["matched_node_id"] == "L7.1"
    assert rebased["prior_verify_status"] == "qa_pass"


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------

def test_state_preservation_structural_match_demotes(graph_path: Path, overlay_path: Path):
    """structural_match demotes qa_pass / t2_pass / waived to pending."""
    # Same primary+parent_layer as L7.1 (which is qa_pass) but different secondary
    pm_prd = {"proposed_nodes": [
        {
            "primary": ["agent/governance/foo.py"],
            "parent_layer": "L7",
            "secondary": ["agent/governance/CHANGED_helpers.py"],
            "test": ["agent/tests/test_foo.py"],
        },
    ]}
    dev_result = {"graph_delta": {"creates": pm_prd["proposed_nodes"]}}
    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-6",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is True, res
    transfer = res["state_transfers"][0]
    assert transfer["tier"] == auto_chain.MATCH_TIER_STRUCTURAL == "structural_match"
    # qa_pass demoted to pending (proposal §4.6.1)
    assert transfer["prior_verify_status"] == "qa_pass"
    assert transfer["new_verify_status"] == "pending"

    # And confirm t2_pass + waived also demote via the apply_state_preservation API
    assert auto_chain.apply_state_preservation("structural_match", "qa_pass") == "pending"
    assert auto_chain.apply_state_preservation("structural_match", "t2_pass") == "pending"
    assert auto_chain.apply_state_preservation("structural_match", "waived") == "pending"
    # weak statuses still transfer through structural
    assert auto_chain.apply_state_preservation("structural_match", "pending") == "pending"


# ---------------------------------------------------------------------------
# AC7
# ---------------------------------------------------------------------------

def test_state_preservation_primary_only_no_transfer(graph_path: Path, overlay_path: Path):
    """primary_only_match never transfers state and still records provenance."""
    # Same primary as L7.1 but parent_layer changed (L7 → L6) — only primary matches
    pm_prd = {"proposed_nodes": [
        {
            "primary": ["agent/governance/foo.py"],
            "parent_layer": "L6",
            "secondary": ["agent/governance/foo_helpers.py"],
            "test": ["agent/tests/test_foo.py"],
        },
    ]}
    dev_result = {"graph_delta": {"creates": pm_prd["proposed_nodes"]}}
    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-7",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is True, res
    transfer = res["state_transfers"][0]
    assert transfer["tier"] == auto_chain.MATCH_TIER_PRIMARY_ONLY == "primary_only_match"
    # Never transfer — must be pending regardless of prior qa_pass
    assert transfer["prior_verify_status"] == "qa_pass"
    assert transfer["new_verify_status"] == "pending"

    # Provenance still recorded
    overlay_doc = json.loads(overlay_path.read_text(encoding="utf-8"))
    nid = transfer["node_id"]
    rebased = overlay_doc["nodes"][nid]["metadata"]["rebased_from"]
    assert rebased["tier"] == "primary_only_match"
    assert rebased["matched_node_id"] == "L7.1"


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------

def test_overlay_write_not_graph_json(graph_path: Path, overlay_path: Path):
    """Successful cluster apply writes graph.rebase.overlay.json and graph.json
    content is unchanged."""
    pm_prd = {"proposed_nodes": [
        {"primary": ["agent/governance/new_x.py"], "parent_layer": "L7"},
    ]}
    dev_result = {"graph_delta": {"creates": pm_prd["proposed_nodes"]}}

    graph_before = graph_path.read_bytes()
    overlay_before = overlay_path.read_bytes()

    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-8",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is True, res

    # graph.json byte-identical
    assert graph_path.read_bytes() == graph_before
    # overlay.json was written (and is different)
    assert overlay_path.read_bytes() != overlay_before
    # The overlay_path returned matches the canonical filename
    assert "graph.rebase.overlay.json" in res["overlay_path"]
    overlay_doc = json.loads(overlay_path.read_text(encoding="utf-8"))
    assert any(nid for nid in overlay_doc.get("nodes", {}).keys())


# ---------------------------------------------------------------------------
# AC9
# ---------------------------------------------------------------------------

def test_graph_json_immutable_during_session_assertion(graph_path: Path, overlay_path: Path):
    """Hash/byte comparison of graph.json before vs after the apply call returns
    identical bytes.  Repeated clusters in the same session must keep graph.json
    immutable."""
    h_before = _hash(graph_path)

    # Apply two consecutive clusters within the same "session"
    for i, primary in enumerate(["a", "b"]):
        pm_prd = {"proposed_nodes": [
            {"primary": [f"agent/governance/seq_{primary}.py"], "parent_layer": "L7"},
        ]}
        dev_result = {"graph_delta": {"creates": pm_prd["proposed_nodes"]}}
        res = auto_chain.apply_reconcile_cluster_to_overlay(
            conn=None,
            project_id=PROJECT_ID,
            task_id=f"t-merge-9-{i}",
            pm_prd=pm_prd,
            dev_result=dev_result,
            metadata=_meta(cluster_fingerprint=f"fp-{i}"),
            graph_path=graph_path,
            overlay_path=overlay_path,
        )
        assert res["applied"] is True, res

    h_after = _hash(graph_path)
    assert h_before == h_after, "graph.json must be byte-identical across a session"


# ---------------------------------------------------------------------------
# AC10
# ---------------------------------------------------------------------------

def test_allocator_respects_overlay_for_next_id(graph_path: Path, overlay_path: Path):
    """_allocate_cluster_next_id returns id strictly greater than max(graph.json
    ids ∪ overlay.json ids)."""
    # Pre-seed overlay with L7.5 (greater than graph max L7.2)
    overlay_doc = json.loads(overlay_path.read_text(encoding="utf-8"))
    overlay_doc["nodes"]["L7.5"] = {
        "node_id": "L7.5", "primary": ["agent/governance/seeded.py"],
        "parent_layer": "L7", "secondary": [], "test": [],
        "verify_status": "pending",
    }
    overlay_path.write_text(json.dumps(overlay_doc, indent=2, sort_keys=True))

    graph_nodes = auto_chain._cluster_load_graph_nodes(graph_path)
    overlay_nodes = auto_chain._cluster_load_overlay_nodes(overlay_path)

    nxt = auto_chain._allocate_cluster_next_id(graph_nodes, overlay_nodes, "L7")
    # graph contains L7.1, L7.2; overlay contains L7.5 → max is 5 → next is L7.6
    assert nxt == "L7.6"

    # And direct allocation through the apply path picks an id > 5 too
    pm_prd = {"proposed_nodes": [
        {"primary": ["agent/governance/new_alloc.py"], "parent_layer": "L7"},
    ]}
    dev_result = {"graph_delta": {"creates": pm_prd["proposed_nodes"]}}
    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-10",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(),
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res["applied"] is True, res
    nid = res["allocated_node_ids"][0]
    assert nid.startswith("L7.")
    assert int(nid.split(".")[1]) > 5


# ---------------------------------------------------------------------------
# AC11
# ---------------------------------------------------------------------------

def test_rollback_marks_failed_retryable(graph_path: Path, overlay_path: Path):
    """On a simulated merge failure the cluster is enqueued/marked
    failed_retryable and the overlay file is NOT cleared."""
    overlay_before = overlay_path.read_bytes()

    pm_prd = {"proposed_nodes": [
        {"primary": ["agent/governance/will_fail.py"], "parent_layer": "L7"},
    ]}
    dev_result = {"graph_delta": {"creates": pm_prd["proposed_nodes"]}}

    deferred_queue: list = []  # plain-list mock for the deferred queue

    res = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-11",
        pm_prd=pm_prd,
        dev_result=dev_result,
        metadata=_meta(cluster_fingerprint="fp-rollback"),
        graph_path=graph_path,
        overlay_path=overlay_path,
        deferred_queue=deferred_queue,
        simulate_failure=True,
    )
    assert res["applied"] is False
    assert res["rolled_back"] is True
    assert res["failed_retryable_marked"] is True
    assert res["overlay_cleared"] is False

    # Overlay file is untouched (R9: only session rollback clears overlay)
    assert overlay_path.exists()
    assert overlay_path.read_bytes() == overlay_before

    # deferred_queue mock has the entry
    assert any(
        e.get("cluster_fingerprint") == "fp-rollback"
        and e.get("status") == "failed_retryable"
        for e in deferred_queue
    )


# ---------------------------------------------------------------------------
# AC12
# ---------------------------------------------------------------------------

def test_non_cluster_unaffected(graph_path: Path, overlay_path: Path, monkeypatch):
    """Non-cluster (operation_type missing or != 'reconcile-cluster') merge
    follows the existing path with no overlay read/write and emits no
    'graph.delta.applied' event."""
    overlay_before_bytes = overlay_path.read_bytes()
    graph_before_bytes = graph_path.read_bytes()

    captured_events: list = []

    def fake_publish(event_name, payload):
        captured_events.append((event_name, payload))

    monkeypatch.setattr(auto_chain, "_publish_event", fake_publish)

    # Case A — operation_type missing
    res_a = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-12a",
        pm_prd={"proposed_nodes": []},
        dev_result={"graph_delta": {"creates": []}},
        metadata={"task_id": "t-merge-12a"},  # no operation_type
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res_a["applied"] is False
    assert res_a.get("skipped") == "non-cluster"

    # Case B — operation_type="workflow_improvement" (different op)
    res_b = auto_chain.apply_reconcile_cluster_to_overlay(
        conn=None,
        project_id=PROJECT_ID,
        task_id="t-merge-12b",
        pm_prd={"proposed_nodes": []},
        dev_result={"graph_delta": {"creates": []}},
        metadata={"task_id": "t-merge-12b", "operation_type": "workflow_improvement"},
        graph_path=graph_path,
        overlay_path=overlay_path,
    )
    assert res_b["applied"] is False
    assert res_b.get("skipped") == "non-cluster"

    # Files untouched
    assert graph_path.read_bytes() == graph_before_bytes
    assert overlay_path.read_bytes() == overlay_before_bytes

    # No graph.delta.applied event was emitted
    assert not any(e[0] == auto_chain.CHAIN_EVENT_GRAPH_DELTA_APPLIED for e in captured_events)
    assert auto_chain.CHAIN_EVENT_GRAPH_DELTA_APPLIED == "graph.delta.applied"


# ---------------------------------------------------------------------------
# Defensive sanity — grep-verifiable AC13 / AC14 tokens in source
# ---------------------------------------------------------------------------

def test_ac13_ac14_grep_tokens_present():
    """Ensure auto_chain.py source carries the literal AC13/AC14 tokens.

    Not strictly one of the named 12 tests, but cheap defensive coverage.
    """
    src = (Path(auto_chain.__file__)).read_text(encoding="utf-8")
    assert "reconcile-cluster" in src
    assert "graph.rebase.overlay.json" in src
    assert "graph.delta.applied" in src
    for tok in ("exact_match", "structural_match", "primary_only_match", "no_match"):
        assert tok in src, f"missing AC14 token: {tok}"
