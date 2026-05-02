"""Tests for Phase Z v2 SCC/indegree feature-cluster synthesis."""
from __future__ import annotations

import os
from types import SimpleNamespace

from agent.governance.reconcile_phases.phase_z import phase_z_run
from agent.governance.reconcile_phases.phase_z_v2 import build_graph_v2_from_symbols


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_indegree_root_synthesis_does_not_require_decorators(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / "agent" / "routes.py"),
        "def register_routes():\n"
        "    create_user()\n"
        "    delete_user()\n\n"
        "def create_user():\n"
        "    validate_user()\n"
        "    save_user()\n\n"
        "def delete_user():\n"
        "    validate_user()\n\n"
        "def validate_user():\n"
        "    return True\n\n"
        "def save_user():\n"
        "    return True\n",
    )

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    assert result["status"] == "ok"
    clusters = result["feature_clusters"]
    assert clusters
    assert any(
        "agent.routes::register_routes" in cluster["entries"]
        for cluster in clusters
    )
    assert any(
        "agent/routes.py" in cluster["primary_files"]
        for cluster in clusters
    )
    assert all(
        cluster["synthesis"]["strategy"] == "scc_indegree_root_dfs_filetree_coalesce"
        for cluster in clusters
    )


def test_cycle_root_is_preserved_as_cluster_input(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / "agent" / "cycle.py"),
        "def a():\n"
        "    b()\n\n"
        "def b():\n"
        "    a()\n",
    )

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    assert result["status"] == "ok"
    cycle_clusters = [
        cluster for cluster in result["feature_clusters"]
        if "agent/cycle.py" in cluster["primary_files"]
    ]
    assert cycle_clusters
    assert cycle_clusters[0]["synthesis"]["cycle_root_count"] == 1
    assert {
        "agent.cycle::a",
        "agent.cycle::b",
    }.issubset(set(cycle_clusters[0]["functions"]))


def test_tests_are_secondary_consumers_not_primary_roots(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / "agent" / "service.py"),
        "def service_entry():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
    )
    _write(
        str(project / "agent" / "tests" / "test_service.py"),
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
    )

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    assert result["status"] == "ok"
    clusters = result["feature_clusters"]
    assert clusters
    primary_files = {
        path for cluster in clusters for path in cluster["primary_files"]
    }
    secondary_files = {
        path for cluster in clusters for path in cluster["secondary_files"]
    }
    assert "agent/service.py" in primary_files
    assert "agent/tests/test_service.py" not in primary_files
    assert "agent/tests/test_service.py" in secondary_files


def test_phase_z_prefers_synthesized_feature_clusters(monkeypatch, tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for idx in range(12):
        _write(
            str(project / "agent" / "pkg" / f"feature_{idx}.py"),
            f"def entry_{idx}():\n"
            f"    return 'feature-{idx}'\n",
        )

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

    assert out["cluster_groups"]
    assert len(out["cluster_groups"]) < 12
    assert out["cluster_groups"][0]["synthesis"]["package_key"] == "agent/pkg"
    primary_files = {
        path
        for payload in out["cluster_groups"]
        for path in payload["primary_files"]
    }
    assert "agent/pkg/feature_0.py" in primary_files
