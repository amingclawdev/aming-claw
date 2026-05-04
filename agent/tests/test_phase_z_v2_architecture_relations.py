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

    primaries = {node["primary_file"].replace("\\", "/") for node in result["nodes"]}
    assert any(path.endswith("agent/service.py") for path in primaries)
    assert "dbservice/index.js" in primaries
    assert any(path.endswith("start_governance.py") for path in primaries)
    fallback_clusters = [
        cluster for cluster in result["feature_clusters"]
        if cluster["synthesis"]["strategy"] == "filetree_fallback_source"
    ]
    assert fallback_clusters
    assert any("dbservice/index.js" in cluster["primary_files"] for cluster in fallback_clusters)


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
