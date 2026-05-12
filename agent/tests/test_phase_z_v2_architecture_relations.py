"""Architecture typed-relation extraction for Phase Z v2."""
from __future__ import annotations

import os

from agent.governance.reconcile_phases.phase_z_v2 import (
    apply_dependency_patches,
    build_candidate_coverage_ledger,
    build_rebase_candidate_graph,
    build_graph_v2_from_symbols,
    extract_typed_relations,
    parse_production_modules,
    validate_dependency_patches,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_extracts_state_route_task_event_and_artifact_relations(tmp_path):
    project = tmp_path / "project"
    _write(
        str(project / "agent" / "governance" / "server.py"),
        "from agent.governance.db import create_user\n\n"
        "@route('POST', '/api/users')\n"
        "def handle_user(ctx):\n"
        "    create_task('pm')\n"
        "    return create_user(ctx.body)\n",
    )
    _write(
        str(project / "agent" / "governance" / "db.py"),
        "SCHEMA = '''CREATE TABLE IF NOT EXISTS users (id TEXT);'''\n\n"
        "def create_user(body):\n"
        "    conn.execute('INSERT INTO users (id) VALUES (?)', (body['id'],))\n"
        "    return conn.execute('SELECT id FROM users').fetchone()\n",
    )
    _write(
        str(project / "agent" / "governance" / "auto_chain.py"),
        "def apply(ctx):\n"
        "    ctx.store._persist_event(event_type='graph.delta.applied')\n"
        "    path = 'graph.rebase.overlay.json'\n"
        "    Path(path).write_text('{}')\n",
    )

    modules = parse_production_modules(str(project))
    relations = extract_typed_relations(str(project), modules)
    triples = {
        (rel["source_module"], rel["relation_type"], rel["target"])
        for rel in relations
    }

    assert ("agent.governance.db", "owns_state", "users") in triples
    assert ("agent.governance.db", "writes_state", "users") in triples
    assert ("agent.governance.db", "reads_state", "users") in triples
    assert ("agent.governance.server", "http_route", "POST /api/users") in triples
    assert ("agent.governance.server", "creates_task", "governance_task") in triples
    assert ("agent.governance.auto_chain", "emits_event", "graph.delta.applied") in triples
    assert ("agent.governance.auto_chain", "writes_artifact", "graph.rebase.overlay.json") in triples


def test_l7_metadata_persists_function_line_index(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / "agent" / "governance" / "db.py"),
        "def sqlite_write_lock():\n"
        "    return 1\n\n"
        "class DecisionValidator:\n"
        "    def validate(self):\n"
        "        value = sqlite_write_lock()\n"
        "        return value\n\n"
        "async def run_async():\n"
        "    return 2\n",
    )

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    node = next(
        node for node in result["nodes"]
        if node["module"] == "agent.governance.db"
    )
    assert node["function_lines"] == {
        "sqlite_write_lock": [1, 2],
        "DecisionValidator.validate": [5, 7],
        "run_async": [9, 10],
    }

    candidate = build_rebase_candidate_graph(
        str(project),
        result,
        session_id="session-lines-test",
        run_id=result["run_id"],
    )
    graph = candidate["deps_graph"]
    l7_node = next(
        graph_node for graph_node in graph["nodes"]
        if graph_node["layer"] == "L7"
        and graph_node["title"] == "agent.governance.db"
    )
    metadata = l7_node["metadata"]
    assert metadata["function_lines"] == node["function_lines"]
    assert metadata["functions"] == node["functions"]
    assert metadata["function_count"] == len(metadata["function_lines"])


def test_graph_excluded_nested_project_does_not_bind_docs_or_tests(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / ".aming-claw.yaml"),
        "\n".join([
            "version: 2",
            "project_id: parent-project",
            "language: python",
            "graph:",
            "  nested_projects:",
            "    mode: exclude",
            "    roots:",
            "      - examples/dashboard-e2e-demo",
            "",
        ]),
    )
    _write(
        str(project / "src" / "app.py"),
        "def app():\n"
        "    return 'parent'\n",
    )
    _write(
        str(project / "examples" / "dashboard-e2e-demo" / "README.md"),
        "# Demo\n\nThis mentions src/app.py and src.app.\n",
    )
    _write(
        str(project / "examples" / "dashboard-e2e-demo" / "tests" / "test_app.py"),
        "from src.app import app\n\n"
        "def test_app():\n"
        "    assert app() == 'parent'\n",
    )

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    app_node = next(node for node in result["nodes"] if node["module"] == "src.app")
    assert app_node["test_coverage"]["test_files"] == []
    assert app_node["doc_coverage"]["doc_files"] == []
    assert all(
        "examples/dashboard-e2e-demo" not in str(value).replace("\\", "/")
        for node in result["nodes"]
        for value in (
            [node.get("primary_file")]
            + (node.get("test_coverage") or {}).get("test_files", [])
            + (node.get("doc_coverage") or {}).get("doc_files", [])
        )
    )

    candidate = build_rebase_candidate_graph(
        str(project),
        result,
        session_id="session-exclude-demo",
        run_id=result["run_id"],
    )
    l7_node = next(
        graph_node for graph_node in candidate["deps_graph"]["nodes"]
        if graph_node["layer"] == "L7" and graph_node["title"] == "src.app"
    )
    assert l7_node["primary"] == ["src/app.py"]
    assert l7_node["secondary"] == []
    assert l7_node["test"] == []


def test_architecture_graph_promotes_state_and_workflow_parents(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / "agent" / "governance" / "reconcile_batch_memory.py"),
        "SCHEMA = '''CREATE TABLE IF NOT EXISTS reconcile_batch_memory (id TEXT);'''\n"
        "def record_pm_decision(conn):\n"
        "    conn.execute('UPDATE reconcile_batch_memory SET id=?', ('x',))\n",
    )
    _write(
        str(project / "agent" / "governance" / "auto_chain.py"),
        "from agent.governance.reconcile_batch_memory import record_pm_decision\n\n"
        "def run_chain(conn):\n"
        "    record_pm_decision(conn)\n"
        "    conn.execute('SELECT id FROM reconcile_batch_memory').fetchall()\n"
        "    create_task('pm')\n"
        "    _persist_event(event_type='graph.delta.applied')\n",
    )
    _write(
        str(project / "agent" / "governance" / "reconcile_phases" / "phase_z_v2.py"),
        "def scan():\n"
        "    return run_chain()\n",
    )

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    assert result["typed_relations"]
    arch = result["architecture_graph"]
    titles = {node["title"] for node in arch["nodes"]}
    assert "Reconcile Graph Rebase" in titles
    assert "Memory System" in titles
    assert "Standard Chain Runtime" in titles
    assert any(link["type"] == "contains" for link in arch["links"])
    assert any(link["type"] in {"owns_state", "writes_state"} for link in arch["links"])

    batch_node = next(
        node for node in result["nodes"]
        if node["module"] == "agent.governance.reconcile_batch_memory"
    )
    assert "state" in batch_node["architecture_signals"]["roles"]

    candidate = build_rebase_candidate_graph(
        str(project),
        result,
        session_id="session-test",
        run_id=result["run_id"],
    )
    hierarchy = candidate["hierarchy_graph"]
    evidence = candidate["evidence_graph"]
    graph = candidate["deps_graph"]
    assert graph["nodes"]
    assert graph["links"]
    assert hierarchy["links"]
    assert evidence["links"]
    assert all(link["type"] == "contains" for link in hierarchy["links"])
    assert all(link["type"] != "contains" for link in graph["links"])
    layers = {node["layer"] for node in graph["nodes"]}
    assert {"L1", "L2", "L3", "L4", "L7"}.issubset(layers)
    ids = {node["id"] for node in graph["nodes"]}
    all_links = graph["links"] + hierarchy["links"] + evidence["links"]
    assert all(link["source"] in ids and link["target"] in ids for link in all_links)
    by_id = {node["id"]: node for node in graph["nodes"]}
    module_id = {
        node["metadata"].get("module"): node["id"]
        for node in graph["nodes"]
        if node["layer"] == "L7"
    }
    batch_id = module_id["agent.governance.reconcile_batch_memory"]
    chain_id = module_id["agent.governance.auto_chain"]
    assert any(
        link["source"] == batch_id
        and link["target"] == chain_id
        and link["type"] == "depends_on"
        for link in graph["links"]
    )
    assert any(
        by_id[link["source"]]["layer"] == "L3"
        and by_id[link["target"]]["layer"] == "L3"
        and link["type"] == "depends_on"
        for link in graph["links"]
    )
    assert any(
        by_id[link["source"]]["layer"] == "L4"
        and by_id[link["target"]]["layer"] == "L7"
        and link["type"] == "reads_state"
        for link in graph["links"]
    )
    assert all(
        any(parent_link["type"] == "contains" and parent_link["target"] == node["id"]
            for parent_link in hierarchy["links"])
        for node in graph["nodes"]
        if node["layer"] == "L4"
    )
    assert by_id[chain_id]["metadata"]["hierarchy_parent"] not in by_id[chain_id]["_deps"]
    assert any(
        by_id[link["source"]]["layer"] == "L7"
        and by_id[link["target"]]["layer"] == "L4"
        and link["type"] == "emits_event"
        for link in evidence["links"]
    )
    assert all(link["type"] != "emits_event" for link in graph["links"])
    assert candidate["architecture_summary"]["same_layer_dependency_count"] > 0
    assert candidate["architecture_summary"]["aggregate_dependency_skipped_count"] > 0
    ledger = build_candidate_coverage_ledger(str(project), result, candidate)
    assert ledger["summary"]["total_files"] >= 3
    assert "source_covered_by_candidate" in ledger["summary"]["by_coverage_status"]


def test_filetree_fallback_covers_non_python_and_root_sources(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "dbservice" / "index.js"), "export function start() { return 1 }\n")
    _write(str(project / "start_governance.py"), "def main():\n    return 0\n")

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    by_primary = {
        node["primary_file"].replace("\\", "/"): node
        for node in result["nodes"]
    }
    primaries = set(by_primary)
    assert any(path.endswith("agent/service.py") for path in primaries)
    assert "dbservice/index.js" in primaries
    assert by_primary["dbservice/index.js"]["language"] == "javascript"
    assert by_primary["dbservice/index.js"]["source_kind"] == "adapter_static"
    assert by_primary["dbservice/index.js"]["function_count"] == 1
    assert any(path.endswith("start_governance.py") for path in primaries)


def test_js_ts_adapter_edges_api_relations_tests_config_and_ignores(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / "web" / "src" / "api" / "client.ts"),
        "export function getNodes() {\n"
        "  return fetch('/api/graph-governance/aming-claw/status')\n"
        "}\n",
    )
    _write(
        str(project / "web" / "src" / "hooks" / "useNodes.ts"),
        "import { getNodes } from '../api/client'\n"
        "export const useNodes = () => getNodes()\n",
    )
    _write(
        str(project / "web" / "src" / "App.tsx"),
        "import { useNodes } from './hooks/useNodes'\n"
        "import axios from 'axios'\n"
        "export default function App() {\n"
        "  axios.post('/api/graph-governance/aming-claw/query', {})\n"
        "  return useNodes()\n"
        "}\n",
    )
    _write(str(project / "web" / "src" / "App.test.tsx"), "import { App } from './App'\n")
    _write(str(project / "web" / "src" / "vite-env.d.ts"), "/// <reference types=\"vite/client\" />\n")
    _write(str(project / "web" / "package.json"), "{\"name\":\"dashboard\"}\n")
    _write(str(project / "web" / "tsconfig.json"), "{\"compilerOptions\":{}}\n")
    _write(str(project / "web" / "vite.config.ts"), "export default {}\n")
    _write(str(project / "web" / "package-lock.json"), "{}\n")
    _write(str(project / "web" / "node_modules" / "pkg" / "index.js"), "ignored();\n")

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    nodes_by_module = {node["module"]: node for node in result["nodes"]}
    assert nodes_by_module["web.src.api.client"]["source_kind"] == "adapter_static"
    assert nodes_by_module["web.src.api.client"]["language"] == "typescript"
    assert nodes_by_module["web.src.App"]["function_count"] == 1
    assert "web.src.App.test" not in nodes_by_module
    assert "web.src.vite-env.d" not in nodes_by_module
    assert "web.vite.config" not in nodes_by_module
    assert not any("node_modules" in node["primary_file"].replace("\\", "/") for node in result["nodes"])

    dep_edges = {
        (edge["source_module"], edge["target_module"], edge["relation_type"])
        for edge in result["module_dependency_edges"]
    }
    assert ("web.src.api.client", "web.src.hooks.useNodes", "imports_module") in dep_edges
    assert ("web.src.hooks.useNodes", "web.src.App", "imports_module") in dep_edges

    api_relations = {
        (rel["source_module"], rel["relation_type"], rel["target"], rel["target_kind"])
        for rel in result["typed_relations"]
    }
    assert (
        "web.src.api.client",
        "calls_api",
        "/api/graph-governance/aming-claw/status",
        "interface",
    ) in api_relations
    assert (
        "web.src.App",
        "calls_api",
        "/api/graph-governance/aming-claw/query",
        "interface",
    ) in api_relations

    rows = {row["path"]: row for row in result["file_inventory"]}
    assert rows["web/src/App.test.tsx"]["file_kind"] == "test"
    assert rows["web/package.json"]["file_kind"] == "config"
    assert rows["web/tsconfig.json"]["file_kind"] == "config"
    assert rows["web/vite.config.ts"]["file_kind"] == "config"
    assert rows["web/package-lock.json"]["file_kind"] == "generated"
    assert rows["web/package-lock.json"]["scan_status"] == "ignored"
    assert "web/node_modules/pkg/index.js" not in rows


def test_config_files_materialize_as_first_class_graph_relations(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(
        str(project / "agent" / "governance" / "role_config.py"),
        "def load_role_config():\n    return 'ok'\n",
    )
    _write(
        str(project / "agent" / "governance" / "reconcile_semantic_config.py"),
        "def load_semantic_enrichment_config():\n    return 'ok'\n",
    )
    _write(
        str(project / "agent" / "pipeline_config.py"),
        "def resolve_role_config():\n    return 'ok'\n",
    )
    _write(str(project / "config" / "roles" / "default" / "pm.yaml"), "role: pm\n")
    _write(
        str(project / "config" / "reconcile" / "semantic_enrichment.yaml"),
        "analyzer: reconcile_semantic\n",
    )
    _write(str(project / "agent" / "pipeline_config.yaml.example"), "pipeline: {}\n")
    _write(str(project / ".env.example"), "TOKEN=\n")

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )

    config_rows = {
        row["path"]: row
        for row in result["file_inventory"]
        if row["path"].endswith((".yaml", ".example"))
    }
    assert config_rows["config/roles/default/pm.yaml"]["scan_status"] == "config_attached"
    assert config_rows["config/reconcile/semantic_enrichment.yaml"]["scan_status"] == "config_attached"
    assert config_rows["agent/pipeline_config.yaml.example"]["scan_status"] == "config_attached"
    assert config_rows[".env.example"]["scan_status"] == "pending_decision"

    triples = {
        (rel["source_module"], rel["relation_type"], rel["target"], rel["target_kind"])
        for rel in result["typed_relations"]
    }
    assert (
        "agent.governance.role_config",
        "configures_role",
        "config/roles/default/pm.yaml",
        "config",
    ) in triples
    assert (
        "agent.governance.reconcile_semantic_config",
        "configures_analyzer",
        "config/reconcile/semantic_enrichment.yaml",
        "config",
    ) in triples
    assert (
        "agent.pipeline_config",
        "configures_model_routing",
        "agent/pipeline_config.yaml.example",
        "config",
    ) in triples

    candidate = build_rebase_candidate_graph(
        str(project),
        result,
        session_id="session-config-test",
        run_id=result["run_id"],
    )
    graph = candidate["deps_graph"]
    by_title = {node["title"]: node for node in graph["nodes"]}
    role_config_node = by_title["agent.governance.role_config"]
    assert "config/roles/default/pm.yaml" in role_config_node["config"]
    assert "config/roles/default/pm.yaml" in role_config_node["metadata"]["config_files"]
    config_assets = [
        node for node in graph["nodes"]
        if node["layer"] == "L4" and node["metadata"].get("asset_key", "").startswith("config:")
    ]
    assert any(node["title"] == "config/roles/default/pm.yaml" for node in config_assets)
    config_asset_id = next(
        node["id"] for node in config_assets
        if node["title"] == "config/roles/default/pm.yaml"
    )
    assert any(
        link["source"] == config_asset_id
        and link["target"] == role_config_node["id"]
        and link["type"] == "configures_role"
        for link in graph["links"]
    )
    ledger = build_candidate_coverage_ledger(str(project), result, candidate)
    by_path = {row["path"]: row for row in ledger["rows"]}
    assert by_path["config/roles/default/pm.yaml"]["coverage_status"] == "config_attached"
    assert by_path[".env.example"]["coverage_status"] == "config_pending_semantic_classification"
    assert by_path[".env.example"]["recommended_chain_action"] == "semantic_config_classification"


def test_root_python_source_gets_symbol_profile_without_fallback(tmp_path):
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(str(project / "app.py"), "def main():\n    return 0\n")
    _write(str(project / "agent" / "service.py"), "def run():\n    return main()\n")

    modules = parse_production_modules(str(project))
    assert "app" in modules

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
    )
    app_node = next(node for node in result["nodes"] if node["module"] == "app")
    assert app_node["functions"] == ["app::main"]


def test_dependency_patch_validation_rejects_noise_and_cycles():
    candidate = {
        "deps_graph": {
            "nodes": [
                {"id": "L7.a", "layer": "L7", "_deps": [], "metadata": {}},
                {"id": "L7.b", "layer": "L7", "_deps": [], "metadata": {}},
                {"id": "L4.table", "layer": "L4", "_deps": [], "metadata": {"aggregate_asset": False}},
                {"id": "L4.bucket", "layer": "L4", "_deps": [], "metadata": {"aggregate_asset": True}},
            ],
            "links": [
                {"source": "L7.a", "target": "L7.b", "type": "depends_on"},
            ],
        },
        "architecture_summary": {},
    }

    invalid = validate_dependency_patches(
        candidate,
        [
            {
                "patch_id": "bad-aggregate",
                "op": "add_dependency",
                "source": "L4.bucket",
                "target": "L7.a",
                "edge_type": "reads_state",
                "reason": "bucket is too coarse",
                "evidence": ["manual review"],
            },
            {
                "patch_id": "bad-direction",
                "op": "add_dependency",
                "source": "L7.a",
                "target": "L4.table",
                "edge_type": "reads_state",
                "reason": "direction is wrong",
                "evidence": ["manual review"],
            },
            {
                "patch_id": "bad-cycle",
                "op": "add_dependency",
                "source": "L7.b",
                "target": "L7.a",
                "edge_type": "depends_on",
                "reason": "would create cycle",
                "evidence": ["manual review"],
            },
            {
                "patch_id": "bad-evidence",
                "op": "add_dependency",
                "source": "L4.table",
                "target": "L7.a",
                "edge_type": "reads_state",
                "reason": "",
                "evidence": [],
            },
        ],
    )

    assert not invalid["ok"]
    errors_by_id = {item["patch_id"]: set(item["errors"]) for item in invalid["rejected"]}
    assert "aggregate_asset_not_allowed" in errors_by_id["bad-aggregate"]
    assert "invalid_dependency_direction" in errors_by_id["bad-direction"]
    assert "cycle_introduced" in errors_by_id["bad-cycle"]
    assert "missing_reason_or_evidence" in errors_by_id["bad-evidence"]

    applied = apply_dependency_patches(
        candidate,
        [
            {
                "patch_id": "good-state-read",
                "op": "add_dependency",
                "source": "L4.table",
                "target": "L7.a",
                "edge_type": "reads_state",
                "reason": "L7.a reads concrete state table",
                "evidence": ["SQL SELECT table"],
                "confidence": "high",
            }
        ],
        qa_actor="qa-test",
    )

    assert applied["ok"]
    updated = applied["candidate"]
    assert any(
        link["source"] == "L4.table"
        and link["target"] == "L7.a"
        and link["type"] == "reads_state"
        and link["metadata"]["edge_kind"] == "qa_dependency_patch"
        for link in updated["deps_graph"]["links"]
    )
    node_a = next(node for node in updated["deps_graph"]["nodes"] if node["id"] == "L7.a")
    assert "L4.table" in node_a["_deps"]
    assert updated["architecture_summary"]["dependency_patch_review"]["accepted_count"] == 1
