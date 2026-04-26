"""Tests for scan_codebase directory exclusion (Phase A false-positive fix).

Validates that _DEFAULT_EXCLUDE in graph_generator.py correctly skips
.claude, .worktrees, shared-volume, and runtime directories.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agent.governance.graph_generator import _DEFAULT_EXCLUDE, scan_codebase


# ---------------------------------------------------------------------------
# Test 1-4: Each new excluded directory is in _DEFAULT_EXCLUDE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dirname", [".claude", ".worktrees", "shared-volume", "runtime"])
def test_default_exclude_contains_new_dirs(dirname):
    """Each new directory name must appear in the _DEFAULT_EXCLUDE set."""
    assert dirname in _DEFAULT_EXCLUDE, f"{dirname!r} missing from _DEFAULT_EXCLUDE"


# ---------------------------------------------------------------------------
# Test 5: scan_codebase skips excluded directories
# ---------------------------------------------------------------------------

def test_scan_codebase_skips_excluded_dirs():
    """scan_codebase must not return files inside excluded directories."""
    with tempfile.TemporaryDirectory() as tmp:
        # Create files inside each excluded directory
        for dirname in (".claude", ".worktrees", "shared-volume", "runtime"):
            d = Path(tmp) / dirname
            d.mkdir()
            (d / "should_not_appear.py").write_text("x = 1\n")

        # Create a legitimate source file that SHOULD appear
        (Path(tmp) / "main.py").write_text("print('hello')\n")

        results = scan_codebase(tmp)
        paths = {r["path"] for r in results}

        # Legitimate file present
        assert "main.py" in paths

        # No file from excluded dirs
        for dirname in (".claude", ".worktrees", "shared-volume", "runtime"):
            for p in paths:
                assert not p.startswith(dirname + "/"), (
                    f"File from excluded dir {dirname!r} leaked: {p}"
                )


# ---------------------------------------------------------------------------
# Test 6: Real agent source files are NOT over-excluded
# ---------------------------------------------------------------------------

def test_scan_still_includes_real_agent_files():
    """Ensure agent/**/*.py files are still found (no over-exclusion).

    Creates a mock workspace mimicking the real repo layout with an
    agent/ directory containing Python source files.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # Simulate agent source tree
        agent_dir = Path(tmp) / "agent" / "governance"
        agent_dir.mkdir(parents=True)
        (agent_dir / "graph_generator.py").write_text("# source\n")
        (agent_dir / "__init__.py").write_text("")

        # Also add an excluded dir to confirm it's skipped
        rt = Path(tmp) / "runtime" / "python"
        rt.mkdir(parents=True)
        (rt / "python.exe").write_text("")

        results = scan_codebase(tmp)
        paths = {r["path"] for r in results}

        # Agent files present
        assert any(p.startswith("agent/") for p in paths), (
            f"No agent/ files found in scan results: {paths}"
        )
        # Runtime excluded
        assert not any(p.startswith("runtime/") for p in paths), (
            f"runtime/ files should be excluded: {paths}"
        )


# ---------------------------------------------------------------------------
# Test 7: load_project_graph still works with shared-volume exclusion
# ---------------------------------------------------------------------------

def test_load_project_graph_with_shared_volume_excluded():
    """generate_graph should produce a valid graph even when
    shared-volume exists in the workspace (it must be excluded)."""
    from agent.governance.graph_generator import generate_graph

    with tempfile.TemporaryDirectory() as tmp:
        # Create shared-volume with many files (simulating governance DB etc.)
        sv = Path(tmp) / "shared-volume"
        sv.mkdir()
        for i in range(50):
            (sv / f"data_{i}.json").write_text("{}")

        # Create a real source file
        (Path(tmp) / "app.py").write_text("print('app')\n")

        graph = generate_graph(tmp)
        # Graph should have nodes but none from shared-volume
        node_files = set()
        for node in graph.get("nodes", []):
            node_files.update(node.get("files", []))

        for f in node_files:
            assert not f.startswith("shared-volume/"), (
                f"shared-volume file leaked into graph: {f}"
            )
