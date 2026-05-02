"""Phase Z v2 integration with project profile boundaries."""
from __future__ import annotations

import os

from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    parse_production_modules,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_parse_production_modules_excludes_tests_and_docs(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "agent" / "tests" / "test_service.py"), "def test_run():\n    assert True\n")
    _write(str(project / "tests" / "test_external.py"), "def test_external():\n    assert True\n")
    _write(str(project / "scripts" / "cli.py"), "def main():\n    return run()\n")
    _write(str(project / "docs" / "example.py"), "def doc_example():\n    pass\n")

    modules = parse_production_modules(str(project))

    assert "agent.service" in modules
    assert "scripts.cli" in modules
    assert "agent.tests.test_service" not in modules
    assert "tests.test_external" not in modules
    assert "docs.example" not in modules


def test_build_graph_v2_nodes_are_production_only(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "agent" / "tests" / "test_service.py"), "def test_run():\n    assert True\n")
    _write(str(project / "tests" / "test_external.py"), "def test_external():\n    assert True\n")

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    node_ids = {node["node_id"] for node in result["nodes"]}
    assert "agent.service" in node_ids
    assert "agent.tests.test_service" not in node_ids
    assert "tests.test_external" not in node_ids
