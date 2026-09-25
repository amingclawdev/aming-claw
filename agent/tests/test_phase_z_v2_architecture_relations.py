"""Architecture typed-relation extraction for Phase Z v2."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from agent.governance import db
from agent.governance import graph_events
from agent.governance import graph_query_trace
from agent.governance import graph_snapshot_store as snapshot_store
from agent.governance.reconcile_phases.phase_z_v2 import (
    CallGraph,
    FunctionMeta,
    ModuleInfo,
    _analyze_c_family_project,
    _c_family_modules,
    aggregate_functions_into_nodes,
    apply_dependency_patches,
    build_candidate_coverage_ledger,
    build_call_graph,
    build_function_call_facts,
    build_module_dependency_edges,
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


def test_c_family_full_chain_keeps_compile_identity_and_attaches_non_architecture_assets(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    project = tmp_path / "c-family-project"
    shutil.copytree(root / "agent" / "tests" / "fixtures" / "c_family_macos", project)
    sdk = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk")
    clang = Path("/Library/Developer/CommandLineTools/usr/bin/clang")
    clangxx = Path("/Library/Developer/CommandLineTools/usr/bin/clang++")
    template = (project / "compile_commands.json.in").read_text(encoding="utf-8")
    (project / "compile_commands.json").write_text(
        template.replace("@FIXTURE_ROOT@", str(project))
        .replace("@CLANGXX@", str(clangxx))
        .replace("@SDKROOT@", str(sdk)),
        encoding="utf-8",
    )
    helper = tmp_path / "aming-claw-clang-indexer"
    subprocess.run(
        [str(clangxx), "-std=c++17", "-isysroot", str(sdk), str(root / "tools" / "clang-indexer" / "main.cc"), "-o", str(helper)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("AC_CFAMILY_CLANG_INDEXER", str(helper))
    monkeypatch.setenv("AC_CFAMILY_CLANG", str(clang))

    result = build_graph_v2_from_symbols(str(project), dry_run=True, scratch_dir=str(tmp_path / "scratch"))
    analysis = result["c_family_analysis"]
    assert analysis["status"] == "partial"  # the deliberate SDK failure remains structured
    assert len(analysis["actions"]) == 5
    assert any(row["analysis_status"] == "failed" for row in analysis["diagnostics"])
    overlay_node = next(node for node in result["nodes"] if node["primary_file"] == "overlay.cc")
    assert overlay_node["source_kind"] == "clang_ast"
    assert overlay_node["node_id"] == "overlay"
    assert len(overlay_node["c_family_compilation_identities"]) == 1
    assert all(
        identity[key]
        for identity in overlay_node["c_family_compilation_identities"]
        for key in ("compilation_action_id", "translation_unit_id", "profile_id")
    )
    assert len([name for name in overlay_node["functions"] if "::render" in name]) == 2
    assert "overlay_test.cc" in overlay_node["test_coverage"]["test_files"]
    assert all(node["primary_file"] not in {"overlay_test.cc", "unbound_test.cc"} for node in result["nodes"])
    assert any(row["test_file"] == "unbound_test.cc" for row in result["c_family_test_bindings"]["unbound"])
    assert any(row["relation_type"] == "calls" and row["target_name"] == "selected_feature" for row in analysis["relations"])
    assert not any(row["relation_type"] == "includes" and row["target_name"] == "inactive_only.h" for row in analysis["relations"])
    assert any(
        row["relation_type"] == "calls_module"
        and row["source_module"] == "overlay"
        and row["target_module"] == "overlay_mac"
        and "clang calls" in row["evidence"]
        for row in result["module_dependency_edges"]
    )
    assert any(
        row["relation_type"] == "includes_module"
        and row["source_module"] == "overlay"
        and row["target_module"] == "overlay_mac"
        for row in result["module_dependency_edges"]
    )
    assert any(
        row["relation_type"] == "references_module"
        and row["source_module"] == "overlay"
        and row["target_module"] == "overlay_mac"
        for row in result["module_dependency_edges"]
    )

    monkeypatch.setattr(db, "_governance_root", lambda: tmp_path / "state")
    monkeypatch.setattr(db, "classify_graph_activation_connection", lambda _conn: {
        "runtime_plane": "stable",
        "active_graph_activation_allowed": True,
        "project_id": "c-family-full-chain",
        "classification_reason": "test_verified_stable_connection",
    })
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._ensure_schema(conn)
    graph_events.ensure_schema(conn)
    snapshot_store.ensure_schema(conn)
    graph_query_trace.ensure_schema(conn)
    snapshot = snapshot_store.create_graph_snapshot(
        conn,
        "c-family-full-chain",
        snapshot_id="c-family-full-chain",
        commit_sha="fixture-candidate",
        snapshot_kind="full",
        graph_json=result,
        file_inventory=[],
        c_family_analysis=analysis,
    )
    snapshot_store.index_c_family_analysis(
        conn, "c-family-full-chain", snapshot["snapshot_id"], analysis,
    )
    snapshot_store.activate_graph_snapshot(
        conn, "c-family-full-chain", snapshot["snapshot_id"],
    )
    conn.commit()

    candidate = build_rebase_candidate_graph(str(project), result)
    graph_nodes = candidate["deps_graph"]["nodes"]
    graph_edges = snapshot_store.graph_payload_edges({"deps_graph": candidate["deps_graph"]})
    indexed = snapshot_store.index_graph_snapshot(
        conn,
        "c-family-full-chain",
        snapshot["snapshot_id"],
        nodes=graph_nodes,
        edges=graph_edges,
    )
    conn.commit()
    assert indexed["nodes"] == len(graph_nodes)
    assert indexed["edges"] == len(graph_edges)
    overlay_graph_node = next(node for node in graph_nodes if "overlay.cc" in node.get("primary", []))
    overlay_mac_graph_node = next(node for node in graph_nodes if "overlay_mac.mm" in node.get("primary", []))
    indexed_edges = conn.execute(
        "SELECT src, dst, edge_type, direction, evidence_json "
        "FROM graph_edges_index WHERE project_id=? AND snapshot_id=?",
        ("c-family-full-chain", snapshot["snapshot_id"]),
    ).fetchall()
    module_edge = next(
        row for row in indexed_edges
        if row["src"] == overlay_graph_node["id"]
        and row["dst"] == overlay_mac_graph_node["id"]
        and row["edge_type"] == "depends_on"
    )
    assert module_edge["direction"] == "dependency"
    assert "clang calls" in module_edge["evidence_json"]
    assert "clang includes" in module_edge["evidence_json"]
    assert "clang references" in module_edge["evidence_json"]
    assert "calls_module" in module_edge["evidence_json"]
    assert "includes_module" in module_edge["evidence_json"]
    assert "references_module" in module_edge["evidence_json"]
    impact_query = graph_query_trace.traced_query(
        conn,
        "c-family-full-chain",
        snapshot["snapshot_id"],
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="get_neighbors",
        args={"node_id": overlay_mac_graph_node["id"], "direction": "in", "include_edge_semantic": True},
        project_root=project,
    )
    impact_edges = impact_query["result"]["edges"]
    assert any(
        edge["src"] == overlay_graph_node["id"]
        and edge["dst"] == overlay_mac_graph_node["id"]
        and edge["edge_type"] == "depends_on"
        and "clang calls" in json.dumps(edge["evidence"])
        for edge in impact_edges
    )

    selected_symbols = [row for row in analysis["symbols"] if row["name"] == "selected_feature"]
    selected_id = selected_symbols[0]["symbol_id"]
    assert len(selected_symbols) >= 2
    assert {row["qualified_name"] for row in selected_symbols} == {"overlay::selected_feature"}
    assert {Path(row["file"]).name for row in selected_symbols} == {"overlay.h", "overlay.cc"}
    selected_definition = next(row for row in selected_symbols if row["is_definition"])
    same_tu_selected = [
        row for row in selected_symbols
        if row["translation_unit_id"] == selected_definition["translation_unit_id"]
    ]
    assert {Path(row["file"]).name for row in same_tu_selected} == {"overlay.h", "overlay.cc"}
    assert len({row["symbol_id"] for row in same_tu_selected}) == 1
    assert all(
        row["definition_clang_id"] == row["clang_id"]
        for row in same_tu_selected
        if row["is_definition"]
    )
    occurrence_query = graph_query_trace.traced_query(
        conn,
        "c-family-full-chain",
        snapshot["snapshot_id"],
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="c_family_occurrences",
        args={"symbol_id": selected_id},
        project_root=project,
    )
    assert {row["role"] for row in occurrence_query["result"]["occurrences"]} == {"declaration", "definition"}
    assert {Path(row["file"]).name for row in occurrence_query["result"]["occurrences"]} == {"overlay.h", "overlay.cc"}
    function_index = graph_query_trace.traced_query(
        conn, "c-family-full-chain", snapshot["snapshot_id"],
        actor="observer", query_source="observer", query_purpose="prompt_context_build",
        tool="function_index", args={"query": "selected_feature"}, project_root=project,
    )["result"]["matches"]
    assert len(function_index) == 1
    assert function_index[0]["primary_file"] == "overlay.cc"
    function_callers = graph_query_trace.traced_query(
        conn, "c-family-full-chain", snapshot["snapshot_id"],
        actor="observer", query_source="observer", query_purpose="prompt_context_build",
        tool="function_callers", args={"query": "selected_feature"}, project_root=project,
    )["result"]["matches"]
    assert any(
        row["caller_file"] == "overlay.cc" and row["callee_file"] == "overlay.cc"
        and row["confidence"] == "strong"
        for row in function_callers
    )
    function_callees = graph_query_trace.traced_query(
        conn, "c-family-full-chain", snapshot["snapshot_id"],
        actor="observer", query_source="observer", query_purpose="prompt_context_build",
        tool="function_callees", args={"query": "render"}, project_root=project,
    )["result"]["matches"]
    assert any(row["callee_short"].startswith("selected_feature") for row in function_callees)

    selected_call = next(
        row for row in analysis["relations"]
        if row["relation_type"] == "calls" and row["target_symbol_id"] == selected_id
    )
    outgoing = graph_query_trace.traced_query(
        conn,
        "c-family-full-chain",
        snapshot["snapshot_id"],
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="c_family_relations",
        args={"symbol_id": selected_call["source_symbol_id"], "direction": "out", "relation_type": "calls"},
        project_root=project,
    )
    incoming = graph_query_trace.traced_query(
        conn,
        "c-family-full-chain",
        snapshot["snapshot_id"],
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="c_family_relations",
        args={"symbol_id": selected_id, "direction": "in", "relation_type": "calls"},
        project_root=project,
    )
    assert selected_call in outgoing["result"]["relations"]
    assert selected_call in incoming["result"]["relations"]
    external = next(row for row in analysis["relations"] if row["relation_type"] == "inherits")
    external_query = graph_query_trace.traced_query(
        conn,
        "c-family-full-chain",
        snapshot["snapshot_id"],
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="c_family_relations",
        args={"symbol_id": external["source_symbol_id"], "direction": "out", "relation_type": "inherits"},
        project_root=project,
    )
    assert external_query["result"]["relations"][0]["resolution"] == "external"
    assert any(row["resolution"] == "potential" for row in analysis["relations"] if row["relation_type"] == "overrides")
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_c_family_compilation_actions WHERE status='failed'"
    ).fetchone()[0] == 1
    conn.close()

    assert "overlay_test.cc" in overlay_graph_node["test"]
    assert "docs/overlay.md" in overlay_graph_node["secondary"]
    assert {"config/build.yaml", "config/settings.json"}.issubset(set(overlay_graph_node["config"]))
    assert "docs/unbound.md" not in overlay_graph_node["secondary"]
    assert "config/unbound.json" not in overlay_graph_node["config"]
    assert not any("overlay_test.cc" in node.get("primary", []) for node in graph_nodes)


def test_c_family_modules_keep_extension_identity_and_merge_same_file_actions(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    header = project / "overlay.h"
    source = project / "overlay.cc"
    header.write_text("int render(int);\n", encoding="utf-8")
    source.write_text("int render(int value) { return value; }\n", encoding="utf-8")

    def result(path: Path, action_id: str, symbol_id: str):
        return {
            "status": "ok",
            "action": {"file": str(path), "language": "cpp", "compilation_action_id": action_id},
            "files": [{"file": str(path), "role": "source"}],
            "symbols": [{
                "compilation_action_id": action_id,
                "symbol_id": symbol_id,
                "name": "render",
                "kind": "FunctionDecl",
                "signature": "int (int)",
                "lineno": 1,
                "end_lineno": 1,
                "is_definition": path.suffix == ".cc",
            }],
            "relations": [],
        }

    modules = _c_family_modules(str(project), {
        "results": [
            result(header, "header-action", "header-symbol"),
            result(source, "source-action-a", "source-symbol"),
            result(source, "source-action-b", "source-symbol"),
        ],
    })
    assert set(modules) == {"overlay__h", "overlay__cc"}
    assert {
        row["compilation_action_id"]
        for row in modules["overlay__cc"].adapter_symbols
    } == {"source-action-a", "source-action-b"}


def test_c_family_profile_excludes_vendor_actions_and_module_owners(tmp_path, monkeypatch):
    from agent.governance.language_adapters.c_family_adapter import CFamilyAdapter
    from agent.governance.project_profile import discover_project_profile

    project = tmp_path / "project"
    source = project / "src" / "core.cc"
    vendor_source = project / "vendor" / "ignored.cc"
    vendor_header = project / "vendor" / "ignored.h"
    generated = project / "generated" / "choice.h"
    generated_source = project / "generated" / "choice.cc"
    for path in (source, vendor_source, vendor_header, generated, generated_source):
        _write(path, "int value() { return 1; }\n")
    _write(project / ".aming-claw.yaml", (
        "version: 2\nproject_id: c-family-profile\nlanguage: cpp\n"
        "graph:\n  exclude_paths:\n    - vendor\n"
    ))
    entries = [
        {"directory": str(project), "file": str(path), "arguments": ["clang++", "-c", str(path)]}
        for path in (source, vendor_source, generated_source)
    ]
    (project / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
    analyzed_files = []

    def fake_analysis(adapter):
        analyzed_files.append(adapter.action["file"])
        return {
            "status": "ok", "action": dict(adapter.action),
            "files": [{"file": adapter.action["file"], "role": "source"}],
            "symbols": [{
                "symbol_id": "vendor-value", "name": "value", "qualified_name": "value",
                "kind": "FunctionDecl", "file": str(vendor_header), "lineno": 1,
                "is_definition": True,
            }],
            "relations": [
                {"relation_type": "includes", "source_file": str(source), "target_file": str(vendor_header)},
                {"relation_type": "includes", "source_file": str(source), "target_file": str(generated)},
            ],
            "macro_analysis": {}, "diagnostics": [],
        }

    monkeypatch.setattr(CFamilyAdapter, "analyze_action", fake_analysis)
    profile = discover_project_profile(str(project))
    assert profile.is_excluded_path(str(vendor_source))
    analysis = _analyze_c_family_project(str(project), profile=profile)
    assert analyzed_files == [str(source)]
    assert [action["file"] for action in analysis["actions"]] == [str(source)]
    assert any(row["target_file"] == str(generated) for row in analysis["relations"])

    modules = _c_family_modules(str(project), analysis, profile=profile)
    assert set(modules) == {"src.core"}
    assert not modules["src.core"].functions
    assert not build_module_dependency_edges(modules, CallGraph())


def test_c_family_physical_definition_owner_resolves_header_declaration_aliases(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    header = project / "src" / "reference.h"
    owner = project / "src" / "reference_store.cc"
    caller = project / "src" / "mark_controller.cc"
    header.write_text("bool ReferenceVisible();\n", encoding="utf-8")
    owner.write_text("bool Predicate() { return true; }\nbool ReferenceVisible() { return Predicate(); }\n", encoding="utf-8")
    caller.write_text("bool VisibleJob() { return ReferenceVisible(); }\n", encoding="utf-8")

    def symbol(symbol_id, name, file, line, definition):
        return {
            "symbol_id": symbol_id, "name": name, "qualified_name": f"seethis::{name}",
            "kind": "FunctionDecl", "signature": "bool ()", "file": str(file),
            "lineno": line, "end_lineno": line, "is_definition": definition,
            "linkage": "external",
        }

    def action(path, action_id, symbols, relations):
        return {
            "status": "ok", "action": {"file": str(path), "language": "cpp", "compilation_action_id": action_id},
            "files": [{"file": str(path), "role": "source"}],
            "symbols": symbols, "relations": relations,
        }

    analysis = {"results": [
        action(owner, "owner-action", [
            symbol("definition-id", "ReferenceVisible", owner, 2, True),
            symbol("predicate-id", "Predicate", owner, 1, True),
            symbol("definition-id", "ReferenceVisible", header, 1, False),
        ], [{
            "relation_type": "calls", "source_symbol_id": "definition-id",
            "target_symbol_id": "predicate-id", "source_file": str(owner),
        }]),
        action(caller, "caller-action", [
            symbol("visible-job-id", "VisibleJob", caller, 1, True),
            symbol("declaration-alias-id", "ReferenceVisible", header, 1, False),
        ], [{
            "relation_type": "calls", "source_symbol_id": "visible-job-id",
            "target_symbol_id": "declaration-alias-id", "source_file": str(caller),
        }]),
    ]}
    modules = _c_family_modules(str(project), analysis)
    assert set(modules) == {"src.reference_store", "src.mark_controller"}
    assert [function.name for function in modules["src.reference_store"].functions].count("ReferenceVisible") == 1
    assert not any(function.name == "ReferenceVisible" for function in modules["src.mark_controller"].functions)
    assert any(
        symbol["symbol_id"] == "declaration-alias-id" and not symbol["is_definition"]
        for symbol in modules["src.mark_controller"].adapter_symbols
    )
    visible = next(function for function in modules["src.reference_store"].functions if function.name == "ReferenceVisible")
    assert set(visible.adapter_symbol_ids) == {"definition-id", "declaration-alias-id"}

    call_graph = build_call_graph(modules)
    job = modules["src.mark_controller"].functions[0]
    predicate = next(function for function in modules["src.reference_store"].functions if function.name == "Predicate")
    assert call_graph.edges[job.qualified_name] == [visible.qualified_name]
    assert call_graph.edges[visible.qualified_name] == [predicate.qualified_name]
    assert call_graph.weak_edges == []
    facts = build_function_call_facts(modules, call_graph)
    assert [(row["caller_file"], row["callee_file"], row["confidence"]) for row in facts["src.reference_store"]["called_by"] if row["callee"] == visible.qualified_name] == [
        ("src/mark_controller.cc", "src/reference_store.cc", "strong")
    ]
    assert [(row["callee"], row["confidence"]) for row in facts["src.reference_store"]["calls"]] == [
        (predicate.qualified_name, "strong")
    ]


def test_c_family_header_definitions_keep_physical_owner_aliases_and_scoped_identity(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    header = project / "src" / "shared.h"
    source_a = project / "src" / "a.cc"
    source_b = project / "src" / "b.cc"
    header.write_text(
        "inline int Shared() { return 1; }\n"
        "static int Local() { return 1; }\n"
        "int alpha_render(int);\n"
        "int beta_render(int);\n"
        "int alpha_render(double);\n",
        encoding="utf-8",
    )
    source_a.write_text("int RunA() { return Shared(); }\n", encoding="utf-8")
    source_b.write_text("int RunB() { return Shared(); }\n", encoding="utf-8")

    def symbol(symbol_id, name, qualified, signature, path, line, *, linkage="external", tu=""):
        return {
            "symbol_id": symbol_id, "name": name, "qualified_name": qualified,
            "kind": "FunctionDecl", "signature": signature, "file": str(path),
            "lineno": line, "end_lineno": line, "is_definition": True,
            "linkage": linkage, "translation_unit_id": tu,
        }

    def action(path, action_id, tu, inline_id, static_id, run_id, extra):
        return {
            "status": "ok", "action": {"file": str(path), "language": "cpp", "compilation_action_id": action_id},
            "files": [{"file": str(path), "role": "source"}],
            "symbols": [
                symbol(inline_id, "Shared", "api::Shared", "int ()", header, 1),
                symbol(static_id, "Local", "api::Local", "int ()", header, 2, linkage="internal", tu=tu),
                symbol(run_id, f"Run{tu[-1].upper()}", f"api::Run{tu[-1].upper()}", "int ()", path, 1),
                symbol(f"aux-id-{tu[-1]}", f"Aux{tu[-1].upper()}", f"api::Aux{tu[-1].upper()}", "int ()", path, 1),
                *extra,
            ],
            "relations": [{
                "relation_type": "calls", "source_symbol_id": run_id,
                "target_symbol_id": inline_id, "source_file": str(path),
            }, {
                "relation_type": "calls", "source_symbol_id": run_id,
                "target_symbol_id": static_id, "source_file": str(path),
            }, {
                "relation_type": "calls", "source_symbol_id": inline_id,
                "target_symbol_id": f"aux-id-{tu[-1]}", "source_file": str(header),
            }],
        }

    shared_a = [
        symbol("alpha-id", "Render", "alpha::Render", "int (int)", header, 3),
        symbol("beta-id", "Render", "beta::Render", "int (int)", header, 4),
        symbol("overload-id", "Render", "alpha::Render", "int (double)", header, 5),
    ]
    modules = _c_family_modules(str(project), {"results": [
        action(source_a, "action-a", "tu-a", "inline-id-a", "static-id-a", "run-id-a", shared_a),
        action(source_b, "action-b", "tu-b", "inline-id-b", "static-id-b", "run-id-b", []),
    ]})
    assert set(modules) == {"src.shared", "src.a", "src.b"}
    header_module = modules["src.shared"]
    assert header_module.path == "src/shared.h"
    assert all(function.module == "src.shared" for function in header_module.functions)
    shared = [function for function in header_module.functions if function.name == "Shared"]
    assert len(shared) == 1
    assert set(shared[0].adapter_symbol_ids) == {"inline-id-a", "inline-id-b"}
    locals_ = [function for function in header_module.functions if function.name == "Local"]
    assert len(locals_) == 2
    assert locals_[0].qualified_name != locals_[1].qualified_name
    renders = [function for function in header_module.functions if function.name == "Render"]
    assert len(renders) == 3
    assert len({function.qualified_name for function in renders}) == 3
    assert all("Render" not in function.qualified_name for function in modules["src.a"].functions)
    assert all("Render" not in function.qualified_name for function in modules["src.b"].functions)

    header_node = next(node for node in aggregate_functions_into_nodes(modules, {}) if node["module"] == "src.shared")
    assert header_node["primary_file"] == "src/shared.h"
    assert "Render [int (int)]" not in header_node["function_lines"]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    snapshot_store.index_graph_snapshot(conn, "header-owner", "fixture", nodes=[{
        "id": "shared", "layer": "L7", "title": "Shared header", "kind": "module",
        "primary": [header_node["primary_file"]],
        "metadata": {
            "module": header_node["module"],
            "functions": header_node["functions"],
            "function_lines": header_node["function_lines"],
        },
    }], edges=[])
    alpha = graph_query_trace.run_tool(conn, "header-owner", "fixture", tool="function_index", args={"query": "alpha::Render [int (int)]"})
    beta = graph_query_trace.run_tool(conn, "header-owner", "fixture", tool="function_index", args={"query": "beta::Render [int (int)]"})
    assert [(row["primary_file"], row["line_start"]) for row in alpha["matches"]] == [("src/shared.h", 3)]
    assert [(row["primary_file"], row["line_start"]) for row in beta["matches"]] == [("src/shared.h", 4)]
    conn.close()

    call_graph = build_call_graph(modules)
    for module_name, inline_id, static_id in (("src.a", "inline-id-a", "static-id-a"), ("src.b", "inline-id-b", "static-id-b")):
        runner = next(function for function in modules[module_name].functions if function.name.startswith("Run"))
        targets = call_graph.edges[runner.qualified_name]
        assert shared[0].qualified_name in targets
        assert next(function for function in locals_ if static_id in function.adapter_symbol_ids).qualified_name in targets
        assert len(targets) == 2
    assert call_graph.weak_edges == []
    assert {call_graph.all_functions[target].name for target in call_graph.edges[shared[0].qualified_name]} == {"AuxA", "AuxB"}
    facts = build_function_call_facts(modules, call_graph)
    assert len(facts["src.shared"]["called_by"]) == 4
    assert len(facts["src.shared"]["calls"]) == 2
    assert {row["caller_file"] for row in facts["src.shared"]["called_by"]} == {"src/a.cc", "src/b.cc"}
    assert {row["callee_file"] for row in facts["src.shared"]["called_by"]} == {"src/shared.h"}
    dependency_edges = build_module_dependency_edges(modules, call_graph)
    assert {edge["target_module"] for edge in dependency_edges if edge["source_module"] == "src.shared" and edge["relation_type"] == "calls_module"} == {"src.a", "src.b"}
    assert not any(edge["source_module"] == "src.a" and edge["target_module"] == "src.b" for edge in dependency_edges)


def test_c_family_dependency_owner_collisions_remain_unresolved_or_weak():
    def function(module, name, signature, symbol_id):
        return FunctionMeta(
            module=module,
            name=name,
            qualified_name=f"{module}::{name} [{signature}]",
            lineno=1,
            end_lineno=1,
            adapter_symbol_id=symbol_id,
            adapter_qualified_name=f"api::{name}",
        )

    modules = {
        "owner_int": ModuleInfo(
            path="src/owner_int.cc", module_name="owner_int", language="cpp", source_kind="clang_ast",
            adapter_symbols=[{
                "symbol_id": "int-id", "qualified_name": "api::lookup", "signature": "int (int)",
                "linkage": "external", "translation_unit_id": "tu-int", "is_definition": True,
                "kind": "FunctionDecl",
            }],
        ),
        "owner_double": ModuleInfo(
            path="src/owner_double.cc", module_name="owner_double", language="cpp", source_kind="clang_ast",
            adapter_symbols=[{
                "symbol_id": "double-id", "qualified_name": "api::lookup", "signature": "double (double)",
                "linkage": "external", "translation_unit_id": "tu-double", "is_definition": True,
                "kind": "FunctionDecl",
            }],
        ),
        "caller": ModuleInfo(
            path="src/caller.cc", module_name="caller", language="cpp", source_kind="clang_ast",
            adapter_relations=[{
                "relation_type": "calls", "source_file": "src/caller.cc", "line": 7,
                "target_symbol_id": "unmatched-external-decl", "target_qualified_name": "api::lookup",
                "target_name": "lookup", "evidence": "clang calls unresolved overloaded api::lookup",
            }],
        ),
        "include_caller": ModuleInfo(
            path="src/use.cc", module_name="include_caller", language="cpp", source_kind="clang_ast",
            adapter_relations=[{
                "relation_type": "includes", "source_file": "src/use.cc", "line": 1,
                "target_file": "/project/include/foo.h", "target_name": "foo.h",
                "evidence": "clang includes exact /project/include/foo.h",
            }],
        ),
        "a_foo": ModuleInfo(path="a/foo.cc", module_name="a_foo", language="cpp", source_kind="clang_ast"),
        "b_foo": ModuleInfo(path="b/foo.mm", module_name="b_foo", language="objective-cpp", source_kind="clang_ast"),
    }
    dependency_edges = build_module_dependency_edges(modules, CallGraph())
    assert not any(edge["relation_type"] == "calls_module" for edge in dependency_edges)
    assert not any(edge["relation_type"] == "includes_module" for edge in dependency_edges)

    duplicate_id = "header-inline-symbol"
    call_modules = {
        "tu_a": ModuleInfo(
            path="src/a.cc", module_name="tu_a", language="cpp", source_kind="clang_ast",
            functions=[function("tu_a", "inline_value", "int ()", duplicate_id)],
        ),
        "tu_b": ModuleInfo(
            path="src/b.cc", module_name="tu_b", language="cpp", source_kind="clang_ast",
            functions=[function("tu_b", "inline_value", "int ()", duplicate_id)],
        ),
        "caller": ModuleInfo(
            path="src/caller.cc", module_name="caller", language="cpp", source_kind="clang_ast",
            functions=[FunctionMeta(
                module="caller", name="run", qualified_name="caller::run [int ()]", lineno=1,
                end_lineno=1, calls=[duplicate_id],
            )],
        ),
    }
    call_graph = build_call_graph(call_modules)
    assert call_graph.edges["caller::run [int ()]"] == []
    assert len(call_graph.weak_edges) == 1
    assert set(call_graph.weak_edges[0].candidates) == {
        "tu_a::inline_value [int ()]", "tu_b::inline_value [int ()]",
    }


def test_c_family_test_binding_uses_exact_qualified_symbol(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    project = tmp_path / "exact-test-binding"
    project.mkdir()
    sources = {
        "a.cc": "namespace A { int render(int value){return value;} }\n",
        "b.cc": "namespace B { int render(int value){return value + 1;} }\n",
        "a_test.cc": "namespace A { int render(int); }\nint test_a(){return A::render(1);}\n",
        "both_test.cc": (
            "namespace A { int render(int); }\n"
            "namespace B { int render(int); }\n"
            "int test_both(){return A::render(1) + B::render(2);}\n"
        ),
    }
    for name, source in sources.items():
        (project / name).write_text(source, encoding="utf-8")
    clang = Path("/Library/Developer/CommandLineTools/usr/bin/clang")
    clangxx = Path("/Library/Developer/CommandLineTools/usr/bin/clang++")
    sdk = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk")
    (project / "compile_commands.json").write_text(json.dumps([
        {
            "directory": str(project),
            "file": str(project / name),
            "arguments": [str(clangxx), "-std=c++17", "-isysroot", str(sdk), "-c", str(project / name)],
        }
        for name in sources
    ]), encoding="utf-8")
    helper = tmp_path / "aming-claw-clang-indexer"
    subprocess.run(
        [str(clangxx), "-std=c++17", "-isysroot", str(sdk), str(root / "tools" / "clang-indexer" / "main.cc"), "-o", str(helper)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("AC_CFAMILY_CLANG_INDEXER", str(helper))
    monkeypatch.setenv("AC_CFAMILY_CLANG", str(clang))

    result = build_graph_v2_from_symbols(str(project), dry_run=True, scratch_dir=str(tmp_path / "scratch-exact"))
    a_node = next(node for node in result["nodes"] if node["primary_file"] == "a.cc")
    b_node = next(node for node in result["nodes"] if node["primary_file"] == "b.cc")
    assert a_node["test_coverage"]["test_files"] == ["a_test.cc", "both_test.cc"]
    assert "a_test.cc" not in b_node.get("test_coverage", {}).get("test_files", [])
    assert "both_test.cc" in b_node["test_coverage"]["test_files"]
    binding = next(row for row in result["c_family_test_bindings"]["bound"] if row["test_file"] == "a_test.cc")
    a_render = next(
        symbol for symbol in result["c_family_analysis"]["symbols"]
        if symbol["qualified_name"] == "A::render" and symbol["is_definition"]
    )
    assert binding["node_id"] == a_node["node_id"]
    assert binding["target_symbol_id"] == a_render["symbol_id"]
    multi_target = [
        row for row in result["c_family_test_bindings"]["bound"]
        if row["test_file"] == "both_test.cc"
    ]
    b_render = next(
        symbol for symbol in result["c_family_analysis"]["symbols"]
        if symbol["qualified_name"] == "B::render" and symbol["is_definition"]
    )
    assert {
        (row["node_id"], row["target_symbol_id"])
        for row in multi_target
    } == {
        (a_node["node_id"], a_render["symbol_id"]),
        (b_node["node_id"], b_render["symbol_id"]),
    }
    assert all(row["relation_id"] and row["source_symbol_id"] for row in multi_target)


def test_python_from_import_lookup_preserves_calls_and_imports_module_edges(tmp_path):
    project = tmp_path / "python-import-project"
    _write(
        str(project / ".aming-claw.yaml"),
        "\n".join([
            "version: 2",
            "project_id: python-import-project",
            "language: python",
            "source_roots:",
            "  - src",
            "",
        ]),
    )
    _write(str(project / "src" / "store.py"), "def lookup():\n    return 1\n")
    _write(
        str(project / "src" / "consumer.py"),
        "from src.store import lookup\n\n"
        "def consume():\n    return lookup()\n",
    )

    result = build_graph_v2_from_symbols(
        str(project), dry_run=True, scratch_dir=str(tmp_path / "scratch-python-import"),
    )
    edges = {
        (row["source_module"], row["target_module"], row["relation_type"]): row
        for row in result["module_dependency_edges"]
    }
    assert ("src.store", "src.consumer", "calls_module") in edges
    assert ("src.store", "src.consumer", "imports_module") in edges
    assert "lookup" in edges[("src.store", "src.consumer", "calls_module")]["evidence"]
    assert "src.store.lookup" in edges[("src.store", "src.consumer", "imports_module")]["evidence"]


def test_extracts_state_route_task_event_and_artifact_relations(tmp_path):
    project = tmp_path / "project"
    _write(
        str(project / ".aming-claw.yaml"),
        "\n".join([
            "version: 2",
            "project_id: artifact-test",
            "language: python",
            "graph:",
            "  exclude_paths:",
            "    - docs/dev",
            "",
        ]),
    )
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
        "    Path(path).write_text('{}')\n"
        "    Path('docs/dev/graph-rebuild-mapping.json').write_text('{}')\n"
        "    Path('shared-volume/codex-tasks/state/governance/aming-claw/scratch/graph-rebuild-mapping.json').write_text('{}')\n",
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
    assert ("agent.governance.auto_chain", "writes_artifact", "docs/dev/graph-rebuild-mapping.json") not in triples
    assert not any(
        target.endswith("graph-rebuild-mapping.json")
        for _source, relation_type, target in triples
        if relation_type == "writes_artifact"
    )


def test_graph_enrich_config_suppresses_string_literal_event_false_positive(tmp_path):
    project = tmp_path / "project"
    _write(
        str(project / "agent" / "governance" / "reconcile_semantic_ai.py"),
        "def _resolve_cli_binary():\n"
        "    event_type = 'semantic.worker.checked'\n"
        "    emit(event_type)\n"
        "    return ['claude.cmd', 'claude.exe', 'codex.cmd', 'codex.ps1']\n",
    )

    modules = parse_production_modules(str(project))
    rules = {
        "emits_event.string_literal.exclude_cli_executable_names": {
            "op": "tighten_rule",
            "edge": "emits_event",
            "source_evidence": "string_literal",
            "action": "reject",
            "downgrade_to": "ignore",
            "when": {
                "all": [
                    {"predicate": "source_evidence_is", "value": "string_literal"},
                    {
                        "predicate": "raw_target_in",
                        "values": ["claude.cmd", "claude.exe", "codex.cmd", "codex.ps1"],
                    },
                ]
            },
        }
    }

    baseline = extract_typed_relations(str(project), modules)
    filtered = extract_typed_relations(
        str(project),
        modules,
        graph_enrich_config_rules=rules,
    )
    baseline_events = {
        rel["target"]
        for rel in baseline
        if rel["relation_type"] == "emits_event"
    }
    filtered_events = {
        rel["target"]
        for rel in filtered
        if rel["relation_type"] == "emits_event"
    }

    assert {"claude.cmd", "claude.exe", "codex.cmd", "codex.ps1"} <= baseline_events
    assert "semantic.worker.checked" in filtered_events
    assert not ({"claude.cmd", "claude.exe", "codex.cmd", "codex.ps1"} & filtered_events)


def test_eventbus_subscribe_and_publish_materialize_directional_event_relations(tmp_path):
    project = tmp_path / "project"
    _write(
        str(project / "agent" / "governance" / "semantic_worker.py"),
        "def register(bus):\n"
        "    bus.subscribe('semantic_job.enqueued', on_semantic_job_enqueued)\n"
        "    bus.subscribe('system.startup', on_governance_startup)\n\n"
        "def requeue(event_bus):\n"
        "    event_bus.publish('semantic_job.enqueued', {'project_id': 'demo'})\n",
    )
    _write(
        str(project / "agent" / "governance" / "server.py"),
        "from agent.governance import event_bus\n\n"
        "def queue_job():\n"
        "    event_bus.publish('semantic_job.enqueued', {'project_id': 'demo'})\n",
    )

    modules = parse_production_modules(str(project))
    relations = extract_typed_relations(str(project), modules)
    triples = {
        (rel["source_module"], rel["relation_type"], rel["target"])
        for rel in relations
    }
    by_triple = {
        (rel["source_module"], rel["relation_type"], rel["target"]): rel
        for rel in relations
    }

    assert ("agent.governance.semantic_worker", "consumes_event", "semantic_job.enqueued") in triples
    assert ("agent.governance.semantic_worker", "consumes_event", "system.startup") in triples
    assert ("agent.governance.semantic_worker", "emits_event", "semantic_job.enqueued") in triples
    assert ("agent.governance.semantic_worker", "emits_event", "system.startup") not in triples
    assert ("agent.governance.server", "emits_event", "semantic_job.enqueued") in triples
    assert (
        by_triple[("agent.governance.semantic_worker", "consumes_event", "system.startup")]["evidence"]
        == "EventBus.subscribe"
    )
    assert (
        by_triple[("agent.governance.server", "emits_event", "semantic_job.enqueued")]["evidence"]
        == "EventBus.publish"
    )


def test_legacy_graph_rebuild_scripts_keep_mapping_out_of_docs_dev():
    repo_root = Path(__file__).resolve().parents[2]
    shared_mapping = (
        "shared-volume/codex-tasks/state/governance/aming-claw/"
        "scratch/graph-rebuild-mapping.json"
    )

    rebuild_source = (repo_root / "scripts" / "rebuild_graph.py").read_text(encoding="utf-8")
    apply_source = (repo_root / "scripts" / "apply_graph.py").read_text(encoding="utf-8")

    assert shared_mapping in rebuild_source
    assert shared_mapping in apply_source
    assert "docs/dev/graph-rebuild-mapping.json" not in rebuild_source
    assert "docs/dev/graph-rebuild-mapping.json" not in apply_source


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

    candidate = build_rebase_candidate_graph(
        str(project),
        result,
        session_id="session-js-ts-api-test",
        run_id=result["run_id"],
    )
    graph = candidate["deps_graph"]
    by_title = {node["title"]: node for node in graph["nodes"]}
    assert by_title["Interface Contracts"]["layer"] == "L3"
    endpoint_assets = {
        node["title"]: node
        for node in graph["nodes"]
        if node.get("layer") == "L4"
        and (node.get("metadata") or {}).get("kind") == "interface"
    }
    assert "/api/graph-governance/aming-claw/status" in endpoint_assets
    assert "/api/graph-governance/aming-claw/query" in endpoint_assets

    api_client_id = by_title["web.src.api.client"]["id"]
    app_id = by_title["web.src.App"]["id"]
    status_id = endpoint_assets["/api/graph-governance/aming-claw/status"]["id"]
    query_id = endpoint_assets["/api/graph-governance/aming-claw/query"]["id"]
    assert any(
        link["source"] == api_client_id
        and link["target"] == status_id
        and link["type"] == "calls_api"
        for link in graph["links"]
    )
    assert any(
        link["source"] == app_id
        and link["target"] == query_id
        and link["type"] == "calls_api"
        for link in graph["links"]
    )

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
