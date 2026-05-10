"""Tests for reconcile project profile source/test/doc boundaries."""
from __future__ import annotations

import os

from agent.governance.project_profile import discover_project_profile


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_project_profile_discovers_python_boundaries(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    pass\n")
    _write(str(project / "agent" / "tests" / "test_service.py"), "def test_run():\n    pass\n")
    _write(str(project / "tests" / "test_external.py"), "def test_external():\n    pass\n")
    _write(str(project / "scripts" / "cli.py"), "def main():\n    pass\n")
    _write(str(project / "docs" / "service.md"), "# Service\n")
    _write(str(project / "runtime" / "generated.py"), "def generated():\n    pass\n")
    _write(str(project / "app.py"), "def app():\n    pass\n")
    _write(str(project / "pyproject.toml"), "[project]\nname='demo'\n")
    _write(str(project / "web" / "package.json"), "{\"name\":\"web\"}\n")
    _write(str(project / "web" / "src" / "index.js"), "export function main() {}\n")
    _write(str(project / "web" / "src" / "App.tsx"), "export function App() { return null }\n")
    _write(str(project / "web" / "src" / "App.test.tsx"), "test('app', () => {})\n")
    _write(str(project / "web" / "vite.config.ts"), "export default {}\n")
    _write(str(project / "web" / "node_modules" / "pkg" / "index.js"), "ignored();\n")
    _write(str(project / "web" / ".next" / "server.js"), "ignored();\n")

    profile = discover_project_profile(str(project))

    assert "python" in profile.languages
    assert "javascript" in profile.languages
    assert "agent" in profile.source_roots
    assert "." in profile.source_roots
    assert "scripts" in profile.source_roots
    assert "web" in profile.source_roots
    assert "agent/tests" in profile.test_roots
    assert "tests" in profile.test_roots
    assert "docs" in profile.doc_roots
    assert "runtime" in profile.exclude_roots

    assert profile.is_production_source_path("agent/service.py")
    assert profile.is_production_source_path("app.py")
    assert profile.is_production_source_path("web/src/index.js")
    assert profile.is_production_source_path("web/src/App.tsx")
    assert not profile.is_production_source_path("agent/tests/test_service.py")
    assert not profile.is_production_source_path("tests/test_external.py")
    assert not profile.is_production_source_path("web/src/App.test.tsx")
    assert not profile.is_production_source_path("web/vite.config.ts")
    assert not profile.is_production_source_path("web/node_modules/pkg/index.js")
    assert not profile.is_production_source_path("web/.next/server.js")
    assert not profile.is_production_source_path("docs/service.md")
    assert not profile.is_production_source_path("runtime/generated.py")
