"""CR2 — phase_z + ai_cluster_processor wiring tests.

Covers exactly the four behavioral cases mandated by the PRD (R8):

1. ``test_phase_z_invokes_processor_per_cluster``     — AC7
2. ``test_cache_hit_skips_llm_call``                  — AC8
3. ``test_ai_unavailable_produces_complete_phase_z_output`` — AC9
4. ``test_cluster_report_attached_to_cluster_payload``      — AC10/AC11
   (also exercises AC2: ClusterReport.to_dict carries ``cluster_fingerprint``)
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure the agent package is importable from the repo root.
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance import ai_cluster_processor as ACP  # noqa: E402
from agent.governance.ai_cluster_processor import (  # noqa: E402
    ClusterReport,
    _compute_cache_key,
)
from agent.governance.llm_cache import LLMCache  # noqa: E402
from agent.governance.reconcile_phases import phase_z as PZ  # noqa: E402
from agent.governance.reconcile_phases.cluster_grouper import (  # noqa: E402
    ClusterGroup,
    FeatureNode,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _Ctx:
    """Tiny stand-in for ReconcileContext."""

    def __init__(self, workspace: str, scratch: str):
        self.workspace_path = workspace
        self.scratch_dir = scratch
        self.project_id = "aming-claw-test"
        self.graph = {"nodes": {}}


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sc = tmp_path / "scratch"
    sc.mkdir()
    return _Ctx(str(ws), str(sc))


def _stub_generate_graph(_workspace):
    """Minimal candidate-graph stub so phase_z's diff path is fast and empty."""
    return {"nodes": {}}


def _make_cluster_groups(n: int):
    """Construct *n* ClusterGroup instances with stable, distinct fingerprints."""
    groups = []
    for i in range(n):
        entry = FeatureNode(
            qname=f"agent.foo.fn_{i}",
            module="agent.foo",
            primary_files=[f"agent/foo/fn_{i}.py"],
        )
        groups.append(ClusterGroup(
            entries=[entry],
            primary_files=[f"agent/foo/fn_{i}.py"],
            secondary_files=[],
            cluster_fingerprint=f"fingerprint_{i:04d}_abcdef",
        ))
    return groups


def _patch_phase_z_inputs(monkeypatch, groups):
    """Stub generate_graph + group_deltas_by_cluster so phase_z sees *groups*."""
    monkeypatch.setattr(
        "agent.governance.graph_generator.generate_graph",
        _stub_generate_graph,
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_phases.cluster_grouper.group_deltas_by_cluster",
        lambda *_a, **_kw: groups,
    )


# ---------------------------------------------------------------------------
# AC7
# ---------------------------------------------------------------------------


def test_phase_z_invokes_processor_per_cluster(monkeypatch, ctx):
    """N clusters → exactly N invocations of process_cluster_with_ai."""
    groups = _make_cluster_groups(3)
    _patch_phase_z_inputs(monkeypatch, groups)

    counter = {"calls": 0}
    seen_fingerprints = []

    def fake_processor(*, cluster, entry, workspace, use_ai, cache):
        counter["calls"] += 1
        seen_fingerprints.append(getattr(entry, "qname", ""))
        return ClusterReport(
            feature_name=getattr(entry, "qname", ""),
            purpose="ok",
            expected_test_files=[],
            expected_doc_sections=[],
            dead_code_candidates=[],
            missing_tests=[],
            gap_explanation=None,
            doc_validation=None,
            enrichment_status="ai_complete",
        )

    monkeypatch.setattr(ACP, "process_cluster_with_ai", fake_processor)

    out = PZ.phase_z_run(ctx, enable_llm_enrichment=True)

    assert counter["calls"] == 3
    assert isinstance(out["cluster_groups"], list)
    assert len(out["cluster_groups"]) == 3
    # Every entry should have routed its first FeatureNode through the processor.
    assert sorted(seen_fingerprints) == [
        "agent.foo.fn_0",
        "agent.foo.fn_1",
        "agent.foo.fn_2",
    ]


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------


def test_cache_hit_skips_llm_call(monkeypatch, ctx):
    """Pre-populated cache → counting fake ai_call sees zero invocations."""
    fn = FeatureNode(
        qname="agent.foo.cached_fn",
        module="agent.foo",
        primary_files=["agent/foo/cached_fn.py"],
    )
    group = ClusterGroup(
        entries=[fn],
        primary_files=["agent/foo/cached_fn.py"],
        secondary_files=[],
        cluster_fingerprint="cache_hit_fp",
    )
    _patch_phase_z_inputs(monkeypatch, [group])

    # Seed the cache phase_z will instantiate (rooted at scratch_dir).
    cache = LLMCache(ctx.scratch_dir)
    key = _compute_cache_key([fn])
    cached = ClusterReport(
        feature_name="cached-name",
        purpose="cached-purpose",
        expected_test_files=[],
        expected_doc_sections=[],
        dead_code_candidates=[],
        missing_tests=[],
        gap_explanation=None,
        doc_validation=None,
        enrichment_status="ai_complete",
    )
    cache.put(key, cached)

    # Counting fake ai_call: phase_z does not pass an explicit ai_call, so
    # process_cluster_with_ai falls back to module-level _default_ai_call.
    # If the cache short-circuits correctly, this counter must stay at zero.
    call_count = {"n": 0}

    def counting_ai_call(stage, payload):
        call_count["n"] += 1
        return {}

    monkeypatch.setattr(ACP, "_default_ai_call", counting_ai_call)

    out = PZ.phase_z_run(ctx, enable_llm_enrichment=True)

    assert call_count["n"] == 0
    assert len(out["cluster_groups"]) == 1
    payload = out["cluster_groups"][0]
    assert payload["report"]["enrichment_status"] == "ai_complete"
    assert payload["report"]["feature_name"] == "cached-name"
    # Fingerprint stamped from source ClusterGroup.
    assert payload["report"]["cluster_fingerprint"] == "cache_hit_fp"


# ---------------------------------------------------------------------------
# AC9
# ---------------------------------------------------------------------------


def test_ai_unavailable_produces_complete_phase_z_output(monkeypatch, ctx):
    """ai_call raising still yields a complete phase_z return value (no exception)."""
    groups = _make_cluster_groups(2)
    _patch_phase_z_inputs(monkeypatch, groups)

    def boom(stage, payload):
        raise RuntimeError("503 Service Unavailable")

    # process_cluster_with_ai uses _default_ai_call when caller passes none.
    monkeypatch.setattr(ACP, "_default_ai_call", boom)

    # Must not raise.
    out = PZ.phase_z_run(ctx, enable_llm_enrichment=True)

    assert isinstance(out["cluster_groups"], list)
    assert len(out["cluster_groups"]) == 2
    for payload in out["cluster_groups"]:
        assert payload["report"]["enrichment_status"] == "ai_unavailable"


# ---------------------------------------------------------------------------
# AC10 + AC11 (also exercises AC2)
# ---------------------------------------------------------------------------


def test_cluster_report_attached_to_cluster_payload(monkeypatch, ctx):
    """Each cluster_groups item carries a report dict joined by cluster_fingerprint.

    Also confirms AC2 — ClusterReport.to_dict() exposes the new
    ``cluster_fingerprint`` key — and AC11 — phase_z_run still returns the
    canonical top-level keys (deltas, epic_groups, artifacts, backlog_rows,
    cluster_groups).
    """
    # AC2: the dataclass surfaces cluster_fingerprint in its serialized form.
    sentinel = ClusterReport(
        feature_name="x",
        purpose=None,
        cluster_fingerprint="ac2_check",
    )
    sentinel_dict = sentinel.to_dict()
    assert "cluster_fingerprint" in sentinel_dict
    assert sentinel_dict["cluster_fingerprint"] == "ac2_check"

    groups = _make_cluster_groups(2)
    _patch_phase_z_inputs(monkeypatch, groups)

    # Use enable_llm_enrichment=False — process_cluster_with_ai short-circuits
    # to the placeholder, which keeps the test deterministic and offline.
    out = PZ.phase_z_run(ctx, enable_llm_enrichment=False)

    # AC11: top-level keys are intact.
    for key in ("deltas", "epic_groups", "artifacts", "backlog_rows", "cluster_groups"):
        assert key in out, f"missing top-level key: {key}"
    assert isinstance(out["cluster_groups"], list)
    assert len(out["cluster_groups"]) == 2

    # AC10: each item exposes the documented payload shape and joins on fingerprint.
    expected_fingerprints = {g.cluster_fingerprint for g in groups}
    seen_fingerprints = set()
    for payload in out["cluster_groups"]:
        for k in ("cluster_fingerprint", "entries", "primary_files", "secondary_files", "report"):
            assert k in payload, f"missing payload key: {k}"
        assert isinstance(payload["report"], dict)
        # The report must be joinable back to its source ClusterGroup.
        assert payload["report"].get("cluster_fingerprint") == payload["cluster_fingerprint"]
        assert payload["cluster_fingerprint"] in expected_fingerprints
        seen_fingerprints.add(payload["cluster_fingerprint"])

    assert seen_fingerprints == expected_fingerprints
