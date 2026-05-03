"""Tests for scripts/phase-z-v2-calibrate.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script_module():
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "phase-z-v2-calibrate.py"
    spec = importlib.util.spec_from_file_location("phase_z_v2_calibrate", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_calibration_report_runs_multiple_iterations_and_samples_diff(monkeypatch, tmp_path):
    script = _load_script_module()
    calls = []

    def fake_build(project_root, dry_run=True, scratch_dir=None):
        calls.append((project_root, dry_run, scratch_dir))
        return {
            "status": "ok",
            "report_path": str(Path(scratch_dir) / "graph-v2.json"),
            "node_count": 1,
            "nodes": [
                {
                    "node_id": "agent.demo",
                    "primary_file": "agent/demo.py",
                    "module": "agent.demo",
                    "layer": "L5",
                    "functions": ["agent.demo::run"],
                }
            ],
            "feature_clusters": [
                {
                    "cluster_fingerprint": "abc",
                    "entries": ["agent.demo::run"],
                    "primary_files": ["agent/demo.py"],
                    "functions": ["agent.demo::run"],
                }
            ],
        }

    def fake_diff(project_root, nodes, graph_path=None):
        return {
            "graph_path": graph_path,
            "old_node_count": 2,
            "new_node_count": 1,
            "only_in_new": ["agent.demo", "agent.extra"],
            "only_in_old": ["L4.1"],
            "layer_changes": [],
            "primary_file_diff": {
                "matched": 1,
                "only_in_new": ["agent/demo.py", "agent/extra.py"],
                "only_in_old": ["agent/old.py"],
                "layer_changes": [{"primary_file": "agent/demo.py"}],
                "duplicates_in_old": {},
                "duplicates_in_new": {},
            },
        }

    monkeypatch.setattr(script, "build_graph_v2_from_symbols", fake_build)
    monkeypatch.setattr(script, "diff_against_existing_graph", fake_diff)

    report = script._build_calibration_report(
        project_root=str(tmp_path),
        iterations=3,
        graph_path="current-graph.json",
        scratch_dir=str(tmp_path / "scratch"),
        sample_size=1,
    )

    assert report["ok"] is True
    assert report["reproducible"] is True
    assert len(report["runs"]) == 3
    assert len(calls) == 3
    primary = report["disagreement_report"]["primary_file_diff"]
    assert primary["only_in_new"]["count"] == 2
    assert primary["only_in_new"]["sample"] == ["agent/demo.py"]
    assert primary["only_in_new"]["truncated"] is True
