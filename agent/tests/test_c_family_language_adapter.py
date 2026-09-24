from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from agent.governance.compilation_context import action_for_file, load_compilation_actions
from agent.governance.language_adapters import CFamilyAdapter, adapter_for_path, capability_for_path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "agent" / "tests" / "fixtures" / "c_family_macos"
CLANG = Path("/Library/Developer/CommandLineTools/usr/bin/clang")
CLANGXX = Path("/Library/Developer/CommandLineTools/usr/bin/clang++")
SDK = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk")


@pytest.fixture()
def c_family_runtime(tmp_path: Path) -> dict[str, Path]:
    project = tmp_path / "fixture"
    shutil.copytree(FIXTURE, project)
    template = (project / "compile_commands.json.in").read_text(encoding="utf-8")
    (project / "compile_commands.json").write_text(
        template.replace("@FIXTURE_ROOT@", str(project))
        .replace("@CLANGXX@", str(CLANGXX))
        .replace("@SDKROOT@", str(SDK)),
        encoding="utf-8",
    )
    helper = tmp_path / "aming-claw-clang-indexer"
    completed = subprocess.run(
        [
            str(CLANGXX),
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-pedantic",
            "-isysroot",
            str(SDK),
            str(ROOT / "tools" / "clang-indexer" / "main.cc"),
            "-o",
            str(helper),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    version = subprocess.run([str(helper), "--version"], text=True, capture_output=True, check=True)
    assert json.loads(version.stdout)["schema_version"] == "aming_claw.cfamily_clang_index.v1"
    return {"project": project, "helper": helper}


def _adapter(runtime: dict[str, Path], name: str) -> CFamilyAdapter:
    actions = load_compilation_actions(runtime["project"])
    action = action_for_file(actions, runtime["project"] / name)
    assert action is not None
    return CFamilyAdapter(action, helper_path=str(runtime["helper"]), clang_path=str(CLANG))


def test_compilation_actions_preserve_profile_dependency_and_same_directory_identity(c_family_runtime):
    nested = c_family_runtime["project"] / "nested-generated.h"
    nested.write_text("#define NESTED_GENERATED_VALUE 1\n", encoding="utf-8")
    header = c_family_runtime["project"] / "overlay.h"
    header.write_text('#include "nested-generated.h"\n' + header.read_text(encoding="utf-8"), encoding="utf-8")
    actions = load_compilation_actions(c_family_runtime["project"])
    assert len(actions) == 5
    overlay = action_for_file(actions, c_family_runtime["project"] / "overlay.cc")
    mac = action_for_file(actions, c_family_runtime["project"] / "overlay_mac.mm")
    assert overlay is not None and mac is not None
    assert overlay.compilation_action_id != mac.compilation_action_id
    assert overlay.profile_id != mac.profile_id
    assert overlay.language == "cpp"
    assert mac.language == "objective-cpp"
    assert any(path.endswith("/overlay.h") and digest.startswith("sha256:") for path, digest in overlay.dependency_hashes)
    assert any(path.endswith("/nested-generated.h") and digest.startswith("sha256:") for path, digest in overlay.dependency_hashes)


def test_registry_selects_clang_adapter_only_for_complete_context(c_family_runtime):
    action = action_for_file(load_compilation_actions(c_family_runtime["project"]), c_family_runtime["project"] / "overlay.cc")
    assert action is not None
    context = {**action.as_dict(), "helper_path": str(c_family_runtime["helper"]), "clang_path": str(CLANG)}
    selected = adapter_for_path(action.file, compilation_context=context)
    assert isinstance(selected, CFamilyAdapter)
    capability = capability_for_path(action.file, compilation_context=context).as_dict()
    assert capability["configuration_selected"] is True
    assert capability["parser_available"] is True
    assert capability["semantic_available"] is True
    assert capability["status"] == "semantic_available"
    unavailable = capability_for_path(action.file, compilation_context=action.as_dict()).as_dict()
    assert unavailable["configuration_selected"] is True
    assert unavailable["semantic_available"] is False
    assert unavailable["status"] == "unsupported"


def test_clang_action_runs_once_and_preserves_overloads_calls_includes_and_macro_state(c_family_runtime):
    adapter = _adapter(c_family_runtime, "overlay.cc")
    symbols = adapter.parse_symbols("", "")
    relations = adapter.extract_relations("", "", symbols=symbols, imports=adapter.parse_imports("", ""))
    result = adapter.analyze_action()
    assert result["status"] == "ok", result["diagnostics"]
    assert adapter.analysis_runs == 1
    render = [row for row in symbols if row["name"] == "render"]
    assert len({row["signature"] for row in render}) == 2
    assert len({row["symbol_id"] for row in render}) == 2
    assert any(row["relation_type"] == "calls" and row["target_name"] == "selected_feature" for row in relations)
    assert any(row["relation_type"] == "includes" and row["target_file"].endswith("/overlay.h") and row["resolution"] == "resolved" for row in relations)
    assert not any(row["relation_type"] == "includes" and row["target_name"] == "inactive_only.h" for row in relations)
    assert any(row["relation_type"] == "inherits" and row["target_qualified_name"] == "overlay::Base" for row in relations)
    assert any(row["relation_type"] == "overrides" and row["resolution"] == "potential" for row in relations)
    assert result["macro_analysis"]["state"] == "clang_preprocessing_applied_relationships_partially_collected"
    assert "OVERLAY_FEATURE=1" in result["macro_analysis"]["macro_refs"]
    assert not any(row["name"] == "unselected_feature" for row in symbols)
    selected = [row for row in symbols if row["name"] == "selected_feature"]
    assert {row["is_definition"] for row in selected} == {False, True}
    assert len({row["symbol_id"] for row in selected}) == 1
    assert {Path(row["file"]).name for row in selected} == {"overlay.h", "overlay.cc"}
    assert all(Path(row["file"]).resolve().is_relative_to(c_family_runtime["project"]) for row in symbols)
    identities = {(row["file"], row["translation_unit_id"], row["profile_id"]) for row in result["occurrences"]}
    assert all(file and tu and profile for file, tu, profile in identities)


def test_objective_cpp_sdk_and_failed_sdk_are_structured(c_family_runtime):
    mac = _adapter(c_family_runtime, "overlay_mac.mm").analyze_action()
    assert mac["status"] == "ok", mac["diagnostics"]
    assert mac["action"]["language"] == "objective-cpp"
    assert mac["action"]["sdk"] == str(SDK.resolve())
    assert any(row["relation_type"] == "includes" and row["target_name"] == "AppKit.h" and row["resolution"] == "resolved" for row in mac["relations"])
    assert not any(
        str(row.get("file") or "").startswith(str(SDK))
        for row in mac["symbols"]
    )
    assert not any(
        str(row.get("source_file") or "").startswith(str(SDK))
        for row in mac["relations"]
    )
    sdk_identities = {
        "NSJSONSerialization",
        "isValidJSONObject:",
        "NSDateComponents",
        "isValidDateInCalendar:",
    }
    assert not any(
        (row.get("name") in sdk_identities or row.get("qualified_name") in sdk_identities)
        and Path(row.get("file") or "").name == "overlay_mac.mm"
        for row in mac["symbols"]
    )
    assert any(
        row["qualified_name"] == "overlay::TextView"
        and Path(row["file"]).name == "overlay.h"
        for row in mac["symbols"]
    )

    failed = _adapter(c_family_runtime, "sdk_failure.mm").analyze_action()
    assert failed["status"] == "failed"
    assert failed["reason"] == "clang_failed"
    assert failed["symbols"] == []
    assert failed["relations"] == []
    assert failed["diagnostics"]


def test_test_translation_units_keep_separate_direct_and_unbound_facts(c_family_runtime):
    bound = _adapter(c_family_runtime, "overlay_test.cc").analyze_action()
    unbound = _adapter(c_family_runtime, "unbound_test.cc").analyze_action()
    assert bound["status"] == unbound["status"] == "ok"
    assert bound["files"][0]["role"] == "test"
    assert unbound["files"][0]["role"] == "test"
    assert any(row["relation_type"] == "calls" and row["target_name"] == "render" for row in bound["relations"])
    assert not any(row["relation_type"] == "calls" and row["resolution"] == "resolved" for row in unbound["relations"])


def test_exact_internal_linkage_member_locations_and_angled_dependencies(c_family_runtime):
    project = c_family_runtime["project"] / "exact-semantics"
    include = project / "include"
    include.mkdir(parents=True)
    sources = {
        "a.cc": "static int helper(){return 1;}\nint from_a(){return helper();}\n",
        "b.cc": "static int helper(){return 2;}\nint from_b(){return helper();}\n",
        "counter.cc": (
            "struct Counter { int value(){return 1;} };\n"
            "int read(Counter& counter){return counter.value();}\n"
        ),
        "selected.cc": "#include <generated.h>\nint selected(){return CHOICE;}\n",
    }
    for name, source in sources.items():
        (project / name).write_text(source, encoding="utf-8")
    generated = include / "generated.h"
    generated.write_text("#define CHOICE 1\n", encoding="utf-8")
    entries = [
        {
            "directory": str(project),
            "file": str(project / name),
            "arguments": [str(CLANGXX), "-std=c++17", "-I", str(include), "-c", str(project / name)],
        }
        for name in sources
    ]
    (project / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")

    actions = load_compilation_actions(project)
    analyses = {
        Path(action.file).name: CFamilyAdapter(
            action,
            helper_path=str(c_family_runtime["helper"]),
            clang_path=str(CLANG),
        ).analyze_action()
        for action in actions
    }
    helpers = [
        symbol
        for name in ("a.cc", "b.cc")
        for symbol in analyses[name]["symbols"]
        if symbol["name"] == "helper" and symbol["is_definition"]
    ]
    assert len(helpers) == 2
    assert {symbol["linkage"] for symbol in helpers} == {"internal"}
    assert len({symbol["symbol_id"] for symbol in helpers}) == 2
    for name, caller in (("a.cc", "from_a"), ("b.cc", "from_b")):
        helper = next(symbol for symbol in analyses[name]["symbols"] if symbol["name"] == "helper")
        assert any(
            relation["source_name"] == caller
            and relation["target_symbol_id"] == helper["symbol_id"]
            for relation in analyses[name]["relations"]
            if relation["relation_type"] == "calls"
        )

    counter = analyses["counter.cc"]
    method = next(symbol for symbol in counter["symbols"] if symbol["qualified_name"] == "Counter::value")
    member_call = next(
        relation for relation in counter["relations"]
        if relation["relation_type"] == "calls" and relation["target_symbol_id"] == method["symbol_id"]
    )
    assert method["lineno"] == 1
    assert method["qualified_name"] == "Counter::value"
    assert method["canonical_decl_id"]
    assert method["definition_clang_id"]
    assert member_call["line"] == 2
    assert member_call["resolution"] == "resolved"
    selected_before = next(action for action in actions if Path(action.file).name == "selected.cc")
    assert any(path == str(generated.resolve()) for path, _digest in selected_before.dependency_hashes)
    generated.write_text("#define CHOICE 2\n", encoding="utf-8")
    selected_after = action_for_file(load_compilation_actions(project), project / "selected.cc")
    assert selected_after is not None
    assert selected_after.compilation_action_id != selected_before.compilation_action_id


def test_out_of_class_reference_store_owner_converges_across_header_and_definition(c_family_runtime):
    project = c_family_runtime["project"] / "reference-store"
    project.mkdir()
    header = project / "reference_store.h"
    source = project / "reference_store.cc"
    header.write_text(
        "namespace seethis::core {\n"
        "class ReferenceStore { public: static int Lookup(int); };\n"
        "}\n",
        encoding="utf-8",
    )
    source.write_text(
        '#include "reference_store.h"\n'
        "int seethis::core::ReferenceStore::Lookup(int value) { return value; }\n",
        encoding="utf-8",
    )
    (project / "compile_commands.json").write_text(
        json.dumps([{
            "directory": str(project),
            "file": str(source),
            "arguments": [str(CLANGXX), "-std=c++17", "-I", str(project), "-c", str(source)],
        }]),
        encoding="utf-8",
    )
    action = action_for_file(load_compilation_actions(project), source)
    assert action is not None
    result = CFamilyAdapter(
        action,
        helper_path=str(c_family_runtime["helper"]),
        clang_path=str(CLANG),
    ).analyze_action()
    assert result["status"] == "ok", result["diagnostics"]
    lookup = [
        row for row in result["symbols"]
        if row["qualified_name"] == "seethis::core::ReferenceStore::Lookup"
    ]
    assert {Path(row["file"]).name for row in lookup} == {"reference_store.h", "reference_store.cc"}
    assert len({row["symbol_id"] for row in lookup}) == 1
    definition = next(row for row in lookup if row["is_definition"])
    declaration = next(row for row in lookup if not row["is_definition"])
    assert definition["canonical_decl_id"] == declaration["canonical_decl_id"]
    assert declaration["definition_clang_id"] == definition["clang_id"]
    assert definition["previous_decl_id"] == declaration["clang_id"]
