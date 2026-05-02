"""Smoke coverage for Phase Z cluster production from symbol scan output."""
from __future__ import annotations

import os
from types import SimpleNamespace

from agent.governance.reconcile_phases.phase_z import (
    CONFIDENCE_HIGH_THRESHOLD,
    phase_z_run,
    three_way_diff,
)
from agent.governance.reconcile_phases.phase_z_v2 import build_graph_v2_from_symbols


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_symbol_project(tmp_path):
    project = tmp_path / "project"
    _write(
        str(project / "agent" / "alpha.py"),
        "def alpha_entry():\n    return alpha_leaf()\n\n"
        "def alpha_leaf():\n    return 'alpha'\n",
    )
    _write(
        str(project / "agent" / "beta.py"),
        "from agent.alpha import alpha_leaf\n\n"
        "def beta_entry():\n    return alpha_leaf()\n",
    )
    return project


def test_phase_z_falls_back_to_symbol_nodes_when_legacy_deltas_empty(monkeypatch, tmp_path):
    project = _make_symbol_project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    monkeypatch.setattr(
        "agent.governance.graph_generator.generate_graph",
        lambda _workspace: {"nodes": {}},
    )

    ctx = SimpleNamespace(
        workspace_path=str(project),
        scratch_dir=str(scratch),
        project_id="aming-claw-test",
        graph={"nodes": {}},
        prefer_symbol_clusters=True,
    )

    out = phase_z_run(ctx, enable_llm_enrichment=False, apply_backlog=False)

    assert out["deltas"] == []
    assert len(out["cluster_groups"]) >= 1
    primary_files = {
        path
        for payload in out["cluster_groups"]
        for path in payload["primary_files"]
    }
    assert "agent/alpha.py" in primary_files
    assert "agent/beta.py" in primary_files
    assert all(str(project) not in path for path in primary_files)


def test_build_graph_v2_dry_run_returns_nodes_for_downstream_cluster_grouper(tmp_path):
    project = _make_symbol_project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    assert result["status"] == "ok"
    assert result["node_count"] == len(result["nodes"])
    assert result["nodes"]
    assert os.path.dirname(result["report_path"]) == str(scratch)


def test_exact_high_confidence_threshold_is_candidate():
    candidate = {
        "nodes": {
            "exact": {
                "primary": ["a.py", "b.py", "c.py"],
                "test": ["test_a.py"],
            },
        },
    }

    deltas = three_way_diff({"nodes": {}}, candidate)

    assert len(deltas) == 1
    assert deltas[0].confidence == CONFIDENCE_HIGH_THRESHOLD
    assert deltas[0].delta_type == "missing_node_high_conf"
    assert deltas[0].action == "candidate"
