"""Tests for Phase Z — Baseline Discovery.

Covers AC-Z1 through AC-Z12.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from agent.governance.reconcile_phases.phase_z import (
    CONFIDENCE_HIGH_THRESHOLD,
    Delta,
    EpicGroup,
    _classify_epic,
    _classify_graph_only_node,
    _compute_candidate_confidence,
    _detect_drift,
    file_epic_backlog_row,
    group_into_epics,
    phase_z_run,
    three_way_diff,
    write_candidate_artifact,
    write_diff_report,
)
from agent.governance.reconcile_phases.phase_z_llm import (
    HAIKU_CONFIDENCE_THRESHOLD,
    PROMPT_VERSION,
    _cache_dir,
    _cache_key,
    _read_cache,
    _write_cache,
    call_haiku,
    call_sonnet,
    enrich_deltas,
    identify_ambiguous_clusters,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    workspace_path: str = ".",
    scratch_dir: str = "",
    project_id: str = "aming-claw",
    graph: dict = None,
    api_base: str = "http://localhost:40000",
):
    """Create a minimal context object for phase_z_run."""
    if graph is None:
        graph = {"nodes": {}}
    if not scratch_dir:
        scratch_dir = tempfile.mkdtemp()
    return SimpleNamespace(
        workspace_path=workspace_path,
        scratch_dir=scratch_dir,
        project_id=project_id,
        graph=graph,
        api_base=api_base,
        prefer_symbol_clusters=False,
    )


def _make_existing_graph(**nodes):
    """Build existing graph dict: _make_existing_graph(L1={"primary": [...]})."""
    return {"nodes": nodes}


def _make_candidate_graph(**nodes):
    return {"nodes": nodes}


# ===== AC-Z1: defaults apply_backlog=False =====

class TestACZ1DefaultBacklog:
    """AC-Z1: phase_z_run defaults apply_backlog=False."""

    @mock.patch("agent.governance.graph_generator.generate_graph")
    @mock.patch("agent.governance.reconcile_phases.phase_z.file_epic_backlog_row")
    def test_default_no_backlog(self, mock_file, mock_gen):
        mock_gen.return_value = {"nodes": {}}
        scratch = tempfile.mkdtemp()
        try:
            ctx = _make_ctx(scratch_dir=scratch)
            phase_z_run(ctx)
            mock_file.assert_not_called()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @mock.patch("agent.governance.graph_generator.generate_graph")
    @mock.patch("agent.governance.reconcile_phases.phase_z.file_epic_backlog_row")
    def test_backlog_filed_when_true(self, mock_file, mock_gen):
        """Backlog rows filed when apply_backlog=True."""
        mock_gen.return_value = {"nodes": {
            "new_node": {"primary": ["agent/governance/foo.py", "agent/governance/bar.py", "agent/governance/baz.py"], "test": ["tests/test_foo.py"], "secondary": ["docs/foo.md"]},
        }}
        mock_file.return_value = "OPT-BACKLOG-PHASE-Z-EPIC-GOVERNANCE-2026-04-25"
        scratch = tempfile.mkdtemp()
        try:
            ctx = _make_ctx(scratch_dir=scratch, graph={"nodes": {}})
            result = phase_z_run(ctx, apply_backlog=True)
            assert len(result["backlog_rows"]) >= 1
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ===== AC-Z2: graph.json unchanged =====

class TestACZ2GraphUnchanged:
    """AC-Z2: graph.json sha256 unchanged after phase_z_run."""

    @mock.patch("agent.governance.graph_generator.generate_graph")
    def test_graph_json_unchanged(self, mock_gen):
        mock_gen.return_value = {"nodes": {}}
        scratch = tempfile.mkdtemp()
        try:
            # Create a fake graph.json
            graph_path = os.path.join(scratch, "graph.json")
            graph_data = {"nodes": {"L1": {"primary": ["a.py"]}}, "version": 1}
            with open(graph_path, "w") as f:
                json.dump(graph_data, f)
            sha_before = hashlib.sha256(open(graph_path, "rb").read()).hexdigest()

            ctx = _make_ctx(scratch_dir=scratch, graph=graph_data)
            phase_z_run(ctx)

            sha_after = hashlib.sha256(open(graph_path, "rb").read()).hexdigest()
            assert sha_before == sha_after, "graph.json must not be modified by phase_z_run"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ===== AC-Z3: save_graph_atomic and init_node_states never called =====

class TestACZ3NoSaveNoInit:
    """AC-Z3: save_graph_atomic and init_node_states are never called."""

    @mock.patch("agent.governance.graph_generator.generate_graph")
    @mock.patch("agent.governance.graph_generator.save_graph_atomic")
    @mock.patch("agent.governance.state_service.init_node_states")
    def test_no_save_no_init(self, mock_init, mock_save, mock_gen):
        mock_gen.return_value = {"nodes": {}}
        scratch = tempfile.mkdtemp()
        try:
            ctx = _make_ctx(scratch_dir=scratch)
            phase_z_run(ctx)
            mock_save.assert_not_called()
            mock_init.assert_not_called()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ===== AC-Z4: generate_graph called exactly once =====

class TestACZ4GenerateGraphOnce:
    """AC-Z4: graph_generator.generate_graph called exactly once."""

    @mock.patch("agent.governance.graph_generator.generate_graph")
    def test_called_once(self, mock_gen):
        mock_gen.return_value = {"nodes": {}}
        scratch = tempfile.mkdtemp()
        try:
            ctx = _make_ctx(scratch_dir=scratch)
            phase_z_run(ctx)
            assert mock_gen.call_count == 1
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ===== AC-Z5: LLM enrichment cascade + caching =====

class TestACZ5LLMEnrichment:
    """AC-Z5: Haiku first, Sonnet on low conf, cache prevents re-calls."""

    def test_haiku_first_sonnet_on_low_conf(self):
        """call_haiku invoked first; call_sonnet only when conf < 0.6."""
        deltas = [
            Delta("missing_node_high_conf", "N1", "candidate", 0.9,
                  "test", files=["a.py"]),
        ]
        cd = _cache_dir()
        if os.path.exists(cd):
            shutil.rmtree(cd)
        with mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_haiku") as mh, \
             mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_sonnet") as ms:
            # Haiku returns low confidence → should escalate to Sonnet
            mh.return_value = {"confidence": 0.3, "classification": "unknown", "model": "haiku"}
            ms.return_value = {"confidence": 0.9, "classification": "code", "model": "sonnet"}

            result = enrich_deltas(deltas)
            mh.assert_called_once()
            ms.assert_called_once()

        if os.path.exists(cd):
            shutil.rmtree(cd)

    def test_haiku_sufficient_no_sonnet(self):
        """When Haiku confidence >= 0.6, Sonnet is NOT called."""
        deltas = [
            Delta("missing_node_high_conf", "N1", "candidate", 0.9,
                  "test", files=["b.py"]),
        ]
        cd = _cache_dir()
        if os.path.exists(cd):
            shutil.rmtree(cd)
        with mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_haiku") as mh, \
             mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_sonnet") as ms:
            mh.return_value = {"confidence": 0.8, "classification": "code", "model": "haiku"}

            result = enrich_deltas(deltas)
            mh.assert_called_once()
            ms.assert_not_called()

        if os.path.exists(cd):
            shutil.rmtree(cd)

    def test_cache_prevents_re_calls(self):
        """Re-run on unchanged code produces 0 LLM calls (all cached)."""
        deltas1 = [
            Delta("missing_node_high_conf", "N1", "candidate", 0.9,
                  "test", files=["c.py"]),
        ]
        deltas2 = [
            Delta("missing_node_high_conf", "N1", "candidate", 0.9,
                  "test", files=["c.py"]),
        ]
        cd = _cache_dir()
        if os.path.exists(cd):
            shutil.rmtree(cd)

        with mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_haiku") as mh, \
             mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_sonnet") as ms:
            mh.return_value = {"confidence": 0.8, "classification": "code", "model": "haiku"}

            # First run — should call haiku
            enrich_deltas(deltas1)
            assert mh.call_count == 1

            # Second run — should use cache, 0 new calls
            mh.reset_mock()
            ms.reset_mock()
            enrich_deltas(deltas2)
            mh.assert_not_called()
            ms.assert_not_called()

        if os.path.exists(cd):
            shutil.rmtree(cd)

    def test_cache_key_uses_sha_and_version(self):
        """Cache key = sha256(file_sha + prompt_version)."""
        file_sha = "abc123"
        key = _cache_key(file_sha)
        expected = hashlib.sha256((file_sha + PROMPT_VERSION).encode()).hexdigest()
        assert key == expected


# ===== AC-Z6: graph_only_node sub_types =====

class TestACZ6GraphOnlySubTypes:
    """AC-Z6: graph_only_node entries have valid sub_type."""

    def test_sub_types_present(self):
        existing = _make_existing_graph(
            L1={"primary": ["nonexistent.py"], "created_by": "chain-rule"},
            L2={"primary": ["also_gone.py"]},
            L3={"primary": ["readme.md"]},
            L4={"primary": ["something.py"], "created_by": "manual"},
        )
        candidate = _make_candidate_graph()  # empty
        deltas = three_way_diff(existing, candidate)
        valid_sub_types = {"experiential", "stale_candidate", "policy_node", "manual_review"}
        graph_only = [d for d in deltas if d.delta_type == "graph_only_node"]
        assert len(graph_only) == 4
        for d in graph_only:
            assert d.sub_type in valid_sub_types, f"{d.node_id} has invalid sub_type: {d.sub_type}"


# ===== AC-Z7: Fixture produces correct node structure =====

class TestACZ7FixtureCandidateNode:
    """AC-Z7: 3 src + 2 doc + 1 test → correct candidate structure."""

    @mock.patch("agent.governance.graph_generator.generate_graph")
    def test_fixture_candidate_structure(self, mock_gen):
        """Fixture with 3 src + 2 doc + 1 test files produces correct structure."""
        mock_gen.return_value = {"nodes": {
            "candidate_1": {
                "primary": ["src/a.py", "src/b.py", "src/c.py"],
                "test": ["tests/test_a.py"],
                "secondary": ["docs/a.md", "docs/b.md"],
            },
        }}
        scratch = tempfile.mkdtemp()
        try:
            ctx = _make_ctx(scratch_dir=scratch, graph={"nodes": {}})
            result = phase_z_run(ctx)
            deltas = result["deltas"]
            high_conf = [d for d in deltas if d.delta_type == "missing_node_high_conf"]
            assert len(high_conf) == 1
            node_data = high_conf[0].metadata
            assert len(node_data["primary"]) == 3
            assert len(node_data["test"]) == 1
            assert len(node_data["secondary"]) == 2
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ===== AC-Z8: Deterministic confidence scoring =====

class TestACZ8Deterministic:
    """AC-Z8: Same input produces identical deltas on repeated runs."""

    def test_deterministic_deltas(self):
        existing = _make_existing_graph(
            A={"primary": ["a.py"], "created_by": "chain-rule"},
        )
        candidate = _make_candidate_graph(
            B={"primary": ["b.py", "c.py", "d.py"], "test": ["test_b.py"], "secondary": ["docs/b.md"]},
        )
        deltas1 = three_way_diff(existing, candidate)
        deltas2 = three_way_diff(existing, candidate)

        assert len(deltas1) == len(deltas2)
        for d1, d2 in zip(deltas1, deltas2):
            assert d1.delta_type == d2.delta_type
            assert d1.node_id == d2.node_id
            assert d1.confidence == d2.confidence
            assert d1.action == d2.action
            assert d1.sub_type == d2.sub_type


# ===== AC-Z9: At most 7 backlog rows =====

class TestACZ9MaxSevenBacklogRows:
    """AC-Z9: 100 high-conf candidates → at most 7 backlog rows."""

    def test_max_seven_epic_groups(self):
        deltas = []
        paths = [
            "agent/governance/x.py",  # governance
            "agent/server/y.py",      # api-server
            "agent/executor/z.py",    # executor
            "scripts/s.py",           # scripts
            "agent/tests/t.py",       # tests
            "docs/d.md",              # docs
            "random/u.py",            # uncategorized
        ]
        # Create 100 high-conf candidates spread across categories
        for i in range(100):
            path = paths[i % len(paths)]
            deltas.append(Delta(
                delta_type="missing_node_high_conf",
                node_id=f"N{i}",
                action="candidate",
                confidence=0.95,
                detail="test",
                files=[path],
            ))
        groups = group_into_epics(deltas)
        assert len(groups) <= 7


# ===== AC-Z10: Backlog row metadata =====

class TestACZ10BacklogMetadata:
    """AC-Z10: Each backlog row has correct metadata."""

    @mock.patch("agent.governance.reconcile_phases.phase_z.urllib.request.urlopen")
    def test_backlog_row_metadata(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        valid_epics = {"governance", "api-server", "executor", "scripts", "tests", "docs", "uncategorized"}
        for epic in valid_epics:
            candidates = [Delta("missing_node_high_conf", "N1", "candidate", 0.9, "test", files=["x.py"])]
            result = file_epic_backlog_row("aming-claw", epic, candidates)
            assert result is not None
            # Verify the POST payload
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["metadata"]["operator_id"] == "reconcile-v3-phase-z"
            assert payload["metadata"]["epic"] == epic
            assert payload["metadata"]["epic"] in valid_epics
            mock_urlopen.reset_mock()


# ===== AC-Z11: Sub-type classification rules =====

class TestACZ11SubTypeRules:
    """AC-Z11: Classification rules for graph_only_node sub_type."""

    def test_chain_rule_experiential(self):
        sub = _classify_graph_only_node("L1", {"created_by": "chain-rule", "primary": ["a.py"]})
        assert sub == "experiential"

    def test_deleted_files_stale_candidate(self):
        sub = _classify_graph_only_node("L2", {"primary": ["nonexistent_file_xyz.py"]})
        assert sub == "stale_candidate"

    def test_all_md_policy_node(self):
        sub = _classify_graph_only_node("L3", {"primary": ["policy.md", "rules.md"]})
        assert sub == "policy_node"

    def test_fallback_manual_review(self):
        # Create a temp file so it exists
        fd, tmpf_path = tempfile.mkstemp(suffix=".py")
        os.close(fd)
        try:
            sub = _classify_graph_only_node("L4", {"primary": [tmpf_path], "created_by": "manual"})
            assert sub == "manual_review"
        finally:
            os.unlink(tmpf_path)


# ===== AC-Z12: No LLM calls when enrichment disabled =====

class TestACZ12NoLLMWhenDisabled:
    """AC-Z12: enable_llm_enrichment=False → zero LLM calls."""

    @mock.patch("agent.governance.graph_generator.generate_graph")
    @mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_haiku")
    @mock.patch("agent.governance.reconcile_phases.phase_z_llm.call_sonnet")
    def test_no_llm_calls(self, mock_sonnet, mock_haiku, mock_gen):
        mock_gen.return_value = {"nodes": {"N1": {"primary": ["x.py"]}}}
        scratch = tempfile.mkdtemp()
        try:
            ctx = _make_ctx(scratch_dir=scratch, graph={"nodes": {}})
            phase_z_run(ctx, enable_llm_enrichment=False)
            mock_haiku.assert_not_called()
            mock_sonnet.assert_not_called()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ===== Additional coverage =====

class TestThreeWayDiff:
    """Extra three_way_diff coverage."""

    def test_drift_detection(self):
        existing = _make_existing_graph(
            L1={"primary": ["a.py"], "secondary": ["b.py"]},
        )
        candidate = _make_candidate_graph(
            L1={"primary": ["a.py", "c.py"], "secondary": ["b.py"]},
        )
        deltas = three_way_diff(existing, candidate)
        drift = [d for d in deltas if d.delta_type == "drift"]
        assert len(drift) == 1
        assert "primary" in drift[0].metadata["drift_fields"]

    def test_empty_graphs(self):
        deltas = three_way_diff({"nodes": {}}, {"nodes": {}})
        assert deltas == []

    def test_high_vs_low_confidence(self):
        candidate = _make_candidate_graph(
            N1={"primary": ["a.py", "b.py", "c.py"], "test": ["t.py"], "secondary": ["d.md"]},
            N2={"primary": ["x.py"]},
        )
        deltas = three_way_diff({"nodes": {}}, candidate)
        high = [d for d in deltas if d.delta_type == "missing_node_high_conf"]
        low = [d for d in deltas if d.delta_type == "missing_node_low_conf"]
        assert len(high) == 1  # N1 has enough signals
        assert len(low) == 1   # N2 is minimal


class TestGroupIntoEpics:
    """Extra epic grouping coverage."""

    def test_all_seven_buckets(self):
        paths_to_epic = {
            "agent/governance/a.py": "governance",
            "agent/server/b.py": "api-server",
            "agent/executor/c.py": "executor",
            "scripts/d.py": "scripts",
            "agent/tests/e.py": "tests",
            "docs/f.md": "docs",
            "random/g.py": "uncategorized",
        }
        deltas = []
        for path, _ in paths_to_epic.items():
            deltas.append(Delta("missing_node_high_conf", path, "candidate", 0.9, "t", files=[path]))
        groups = group_into_epics(deltas)
        assert set(groups.keys()) == set(paths_to_epic.values())


class TestWriteArtifacts:
    """Test artifact writers."""

    def test_write_candidate_artifact(self):
        scratch = tempfile.mkdtemp()
        try:
            path = write_candidate_artifact(scratch, {"nodes": {"L1": {}}})
            assert os.path.exists(path)
            assert "phase_z_candidate_graph" in path
            with open(path) as f:
                data = json.load(f)
            assert "nodes" in data
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_write_diff_report(self):
        scratch = tempfile.mkdtemp()
        try:
            deltas = [Delta("graph_only_node", "L1", "report_only", 1.0, "test", sub_type="experiential")]
            groups = {"governance": EpicGroup("governance", [])}
            path = write_diff_report(scratch, deltas, groups)
            assert os.path.exists(path)
            with open(path) as f:
                data = json.load(f)
            assert data["delta_count"] == 1
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class TestPhaseZImportInInit:
    """Verify phase_z is exported from reconcile_phases."""

    def test_phase_z_in_all(self):
        from agent.governance.reconcile_phases import __all__
        assert "phase_z" in __all__

    def test_phase_z_importable(self):
        from agent.governance.reconcile_phases import phase_z
        assert hasattr(phase_z, "phase_z_run")
