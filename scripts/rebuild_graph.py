"""Phase 1: Graph Rebuild — generate complete node mapping.

Run: python scripts/rebuild_graph.py
Output: docs/dev/graph-rebuild-mapping.json (for review before applying)
"""
import glob
import json
import os
from pathlib import Path
from collections import defaultdict


def scan_files():
    py = sorted(f.replace(os.sep, "/") for f in glob.glob("agent/**/*.py", recursive=True)
                if "__pycache__" not in f)
    docs = sorted(f.replace(os.sep, "/") for f in glob.glob("docs/**/*.md", recursive=True)
                  if "/dev/" not in f and "/archive/" not in f)
    tests = sorted(f.replace(os.sep, "/") for f in glob.glob("agent/tests/*.py", recursive=True)
                   if os.path.basename(f).startswith("test_"))
    yamls = sorted(f.replace(os.sep, "/") for f in glob.glob("config/**/*.yaml", recursive=True))
    source = [f for f in py if "/tests/" not in f]
    return source, docs, tests, yamls


def build_nodes(source_files):
    """Group source files into logical nodes by module."""
    nodes = {}

    # Governance modules
    GOV_MODULES = {
        "auto_chain": ["agent/governance/auto_chain.py"],
        "task_registry": ["agent/governance/task_registry.py"],
        "server": ["agent/governance/server.py"],
        "db": ["agent/governance/db.py"],
        "graph": ["agent/governance/graph.py", "agent/governance/graph_generator.py"],
        "state_service": ["agent/governance/state_service.py"],
        "memory": ["agent/governance/memory_backend.py", "agent/governance/memory_service.py"],
        "reconcile": ["agent/governance/reconcile.py"],
        "preflight": ["agent/governance/preflight.py"],
        "chain_context": ["agent/governance/chain_context.py"],
        "conflict_rules": ["agent/governance/conflict_rules.py"],
        "impact_analyzer": ["agent/governance/impact_analyzer.py", "agent/governance/coverage_check.py"],
        "gatekeeper": ["agent/governance/gatekeeper.py", "agent/governance/gate_policy.py"],
        "evidence": ["agent/governance/evidence.py"],
        "doc_policy": ["agent/governance/doc_policy.py", "agent/governance/doc_generator.py"],
        "observability": ["agent/governance/observability.py", "agent/governance/event_bus.py",
                          "agent/governance/audit_service.py", "agent/governance/outbox.py"],
        "models": ["agent/governance/models.py", "agent/governance/enums.py", "agent/governance/errors.py"],
        "services": ["agent/governance/role_service.py", "agent/governance/token_service.py",
                      "agent/governance/session_context.py", "agent/governance/project_service.py",
                      "agent/governance/idempotency.py"],
    }

    gov_files = [f for f in source_files if f.startswith("agent/governance/")]
    assigned = set()
    for files in GOV_MODULES.values():
        assigned.update(files)
    gov_misc = [f for f in gov_files if f not in assigned and not f.endswith("__init__.py")]
    if gov_misc:
        GOV_MODULES["gov_misc"] = gov_misc

    for mod_name, files in GOV_MODULES.items():
        existing = [f for f in files if os.path.exists(f)]
        if existing:
            nodes[f"governance.{mod_name}"] = {
                "primary": existing, "test": [], "secondary": [], "layer": "L2"
            }

    # Top-level agent modules
    TOP_MODULES = {
        "executor": ["agent/executor_worker.py", "agent/executor.py", "agent/executor_api.py"],
        "ai_lifecycle": ["agent/ai_lifecycle.py"],
        "deploy": ["agent/deploy_chain.py"],
        "service_manager": ["agent/service_manager.py"],
        "config": ["agent/config.py", "agent/project_config.py", "agent/pipeline_config.py",
                    "agent/role_permissions.py", "agent/role_config.py"],
        "context": ["agent/context_assembler.py", "agent/context_store.py"],
        "cli": ["agent/cli.py"],
    }
    assigned_top = set()
    for files in TOP_MODULES.values():
        assigned_top.update(files)

    top_files = [f for f in source_files if f.startswith("agent/") and f.count("/") == 1
                 and not f.endswith("__init__.py")]
    top_misc = [f for f in top_files if f not in assigned_top]
    if top_misc:
        TOP_MODULES["agent_misc"] = top_misc

    for mod_name, files in TOP_MODULES.items():
        existing = [f for f in files if os.path.exists(f)]
        if existing:
            layer = "L3" if mod_name in ("executor", "cli") else "L2"
            nodes[f"agent.{mod_name}"] = {
                "primary": existing, "test": [], "secondary": [], "layer": layer
            }

    # Telegram gateway
    gw_files = [f for f in source_files if f.startswith("agent/telegram_gateway/")
                and not f.endswith("__init__.py")]
    if gw_files:
        nodes["agent.gateway"] = {"primary": gw_files, "test": [], "secondary": [], "layer": "L3"}

    # MCP
    mcp_files = [f for f in source_files if f.startswith("agent/mcp/")
                 and not f.endswith("__init__.py")]
    if mcp_files:
        nodes["agent.mcp"] = {"primary": mcp_files, "test": [], "secondary": [], "layer": "L3"}

    return nodes


def match_tests(test_files, nodes):
    """Match test files to source nodes by stem matching."""
    test_mapping = {}
    unmatched = []

    for tf in test_files:
        basename = os.path.basename(tf)
        stem = basename.replace("test_", "").replace(".py", "")
        matched = False

        # Direct match: test_auto_chain_routing → auto_chain
        for nid, data in nodes.items():
            for pf in data["primary"]:
                pf_stem = os.path.basename(pf).replace(".py", "")
                if stem == pf_stem or stem.startswith(pf_stem + "_") or pf_stem.startswith(stem):
                    nodes[nid]["test"].append(tf)
                    test_mapping[tf] = nid
                    matched = True
                    break
            if matched:
                break

        if not matched:
            # Partial: any primary stem contained in test stem
            for nid, data in nodes.items():
                for pf in data["primary"]:
                    pf_stem = os.path.basename(pf).replace(".py", "")
                    if pf_stem in stem:
                        nodes[nid]["test"].append(tf)
                        test_mapping[tf] = nid
                        matched = True
                        break
                if matched:
                    break

        if not matched:
            unmatched.append(tf)

    return test_mapping, unmatched


def match_docs(doc_files, nodes):
    """Match doc files to source nodes."""
    MANUAL_MAP = {
        "docs/governance/auto-chain.md": "governance.auto_chain",
        "docs/governance/gates.md": "governance.gatekeeper",
        "docs/governance/acceptance-graph.md": "governance.graph",
        "docs/governance/memory.md": "governance.memory",
        "docs/governance/conflict-rules.md": "governance.conflict_rules",
        "docs/governance/version-control.md": "governance.state_service",
        "docs/governance/manual-fix-sop.md": "governance.reconcile",
        "docs/governance/design-spec-full.md": "governance.server",
        "docs/governance/prd-full.md": "governance.server",
        "docs/api/governance-api.md": "governance.server",
        "docs/api/executor-api.md": "agent.executor",
        "docs/architecture.md": "governance.server",
        "docs/deployment.md": "agent.deploy",
        "docs/onboarding.md": "governance.server",
        "docs/config/role-permissions.md": "agent.config",
        "docs/config/aming-claw-yaml.md": "agent.config",
        "docs/config/mcp-json.md": "agent.mcp",
        "docs/roles/coordinator.md": "agent.executor",
        "docs/roles/dev.md": "agent.executor",
        "docs/roles/pm.md": "agent.executor",
        "docs/roles/qa.md": "agent.executor",
        "docs/roles/tester.md": "agent.executor",
        "docs/roles/gatekeeper.md": "governance.gatekeeper",
        "docs/roles/observer.md": "governance.reconcile",
        "README.md": "governance.server",
    }

    doc_mapping = {}
    unmapped = []

    for df in doc_files:
        if df in MANUAL_MAP and MANUAL_MAP[df] in nodes:
            node = MANUAL_MAP[df]
            nodes[node]["secondary"].append(df)
            doc_mapping[df] = {"node": node, "method": "manual_map"}
        else:
            # Name matching
            stem = Path(df).stem.replace("-", "_")
            matched = False
            for nid in nodes:
                short = nid.split(".")[-1]
                if stem == short or short in stem or stem in short:
                    nodes[nid]["secondary"].append(df)
                    doc_mapping[df] = {"node": nid, "method": "inferred", "inferred": True}
                    matched = True
                    break
            if not matched:
                unmapped.append(df)

    return doc_mapping, unmapped


def main():
    os.chdir(Path(__file__).resolve().parent.parent)

    source_files, doc_files, test_files, yaml_files = scan_files()
    nodes = build_nodes(source_files)
    test_mapping, unmatched_tests = match_tests(test_files, nodes)
    doc_mapping, unmapped_docs = match_docs(doc_files, nodes)

    # Map YAML config files as secondary to owning nodes
    YAML_CONFIG_OWNERS = {
        "config/roles/": ["agent.executor", "agent.ai_lifecycle", "agent.config"],
        "config/": ["governance.gov_misc"],
    }
    for yf in yaml_files:
        for prefix, owner_nodes in YAML_CONFIG_OWNERS.items():
            if yf.startswith(prefix):
                for nid in owner_nodes:
                    if nid in nodes and yf not in nodes[nid]["secondary"]:
                        nodes[nid]["secondary"].append(yf)
                break

    # Also check README.md at root
    if os.path.exists("README.md") and "README.md" not in doc_mapping:
        if "governance.server" in nodes:
            nodes["governance.server"]["secondary"].append("README.md")
            doc_mapping["README.md"] = {"node": "governance.server", "method": "manual_map"}

    # Stats
    total_primary = sum(len(n["primary"]) for n in nodes.values())
    total_test = sum(len(n["test"]) for n in nodes.values())
    total_docs = sum(len(n["secondary"]) for n in nodes.values())

    print(f"=== Graph Rebuild Report ===")
    print(f"Nodes: {len(nodes)}")
    print(f"Source files mapped: {total_primary}/{len(source_files)}")
    print(f"Test files mapped: {len(test_mapping)}/{len(test_files)} ({len(unmatched_tests)} unmatched)")
    print(f"Doc files mapped: {len(doc_mapping)}/{len(doc_files)} ({len(unmapped_docs)} unmapped)")

    print(f"\n=== Nodes ===")
    for nid, data in sorted(nodes.items()):
        p = len(data["primary"])
        t = len(data["test"])
        s = len(data["secondary"])
        print(f"  {nid:35} primary={p:2}  test={t:2}  docs={s:2}  [{data['layer']}]")

    if unmatched_tests:
        print(f"\n=== Unmatched Tests ({len(unmatched_tests)}) ===")
        for tf in unmatched_tests:
            print(f"  {tf}")

    if unmapped_docs:
        print(f"\n=== Unmapped Docs ({len(unmapped_docs)}) ===")
        for df in unmapped_docs:
            print(f"  {df}")

    # Save for review
    output = {
        "nodes": {nid: data for nid, data in sorted(nodes.items())},
        "test_mapping": test_mapping,
        "doc_mapping": doc_mapping,
        "unmatched_tests": unmatched_tests,
        "unmapped_docs": unmapped_docs,
        "stats": {
            "total_nodes": len(nodes),
            "source_mapped": total_primary,
            "source_total": len(source_files),
            "tests_mapped": len(test_mapping),
            "tests_total": len(test_files),
            "docs_mapped": len(doc_mapping),
            "docs_total": len(doc_files),
        }
    }
    out_path = "docs/dev/graph-rebuild-mapping.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nMapping saved to {out_path}")


if __name__ == "__main__":
    main()
