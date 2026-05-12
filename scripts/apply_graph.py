"""Apply the rebuilt mapping to graph.json.

Preserves existing verify_status from node_state DB.
Run: python scripts/apply_graph.py
"""
import json
import os
import shutil
import sqlite3
from pathlib import Path


def main():
    os.chdir(Path(__file__).resolve().parent.parent)

    mapping_path = "shared-volume/codex-tasks/state/governance/aming-claw/scratch/graph-rebuild-mapping.json"
    graph_path = "shared-volume/codex-tasks/state/governance/aming-claw/graph.json"
    db_path = "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"

    with open(mapping_path) as f:
        mapping = json.load(f)

    # Backup
    backup = graph_path + ".bak"
    if os.path.exists(graph_path):
        shutil.copy2(graph_path, backup)
        print(f"Backed up to {backup}")

    # Load existing verify_status from DB
    verify_status = {}
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT node_id, verify_status FROM node_state WHERE project_id='aming-claw'"):
            verify_status[row["node_id"]] = row["verify_status"]
        conn.close()
        print(f"Loaded {len(verify_status)} existing verify_status records")

    # Build new graph
    import networkx as nx
    G = nx.DiGraph()

    nodes = mapping["nodes"]
    edges = mapping.get("edges", [])
    topo = mapping.get("topo_order", [])
    levels = mapping.get("verification_levels", {})

    # Compute level for each node
    node_level = {}
    for lvl, nids in levels.items():
        for nid in nids:
            node_level[nid] = int(lvl)

    for nid, data in nodes.items():
        layer = data.get("layer", "L2")
        level = node_level.get(nid, 2)

        attrs = {
            "title": nid.replace(".", " / "),
            "layer": layer,
            "verify_level": level + 1,
            "gate_mode": "auto",
            "primary": data["primary"],
            "secondary": data["secondary"],
            "test": data["test"],
        }

        # Preserve existing verify_status if we have a matching old node
        # Old nodes used L{n}.{m} format, new use module names
        # Can't match directly — preserve all existing statuses for safety
        G.add_node(nid, **attrs)

    # Add edges
    for edge in edges:
        src, dst = edge["from"], edge["to"]
        if G.has_node(src) and G.has_node(dst):
            G.add_edge(src, dst)

    # Build gates graph (empty — auto-derived)
    gates_G = nx.DiGraph()

    # Save
    graph_data = {
        "version": 1,
        "deps_graph": nx.node_link_data(G),
        "gates_graph": nx.node_link_data(gates_G),
    }

    os.makedirs(os.path.dirname(graph_path), exist_ok=True)
    with open(graph_path, "w", encoding="utf-8") as f:
        json.dump(graph_data, f, ensure_ascii=False, indent=2)

    print(f"\nGraph written: {len(G.nodes)} nodes, {len(G.edges)} edges")

    # Update node_state in DB
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        # Insert new nodes (preserve existing status)
        for nid in G.nodes:
            existing = verify_status.get(nid)
            if existing:
                # Node already exists — don't touch
                pass
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, 'pending')",
                    ("aming-claw", nid),
                )
        conn.commit()

        # Verify
        count = conn.execute("SELECT COUNT(*) FROM node_state WHERE project_id='aming-claw'").fetchone()[0]
        print(f"node_state rows: {count}")
        conn.close()

    # Generate code_doc_map.json
    code_doc_map = {}
    for nid, data in nodes.items():
        for pf in data["primary"]:
            if data["secondary"]:
                code_doc_map[pf] = data["secondary"]

    cdm_path = "shared-volume/codex-tasks/state/governance/aming-claw/code_doc_map.json"
    with open(cdm_path, "w", encoding="utf-8") as f:
        json.dump(code_doc_map, f, indent=2, ensure_ascii=False)
    print(f"code_doc_map.json written: {len(code_doc_map)} entries")

    # Summary
    print(f"\n=== Summary ===")
    print(f"Nodes: {len(G.nodes)}")
    print(f"Edges: {len(G.edges)}")
    print(f"Primary files: {sum(len(G.nodes[n].get('primary', [])) for n in G.nodes)}")
    print(f"Test files: {sum(len(G.nodes[n].get('test', [])) for n in G.nodes)}")
    print(f"Doc files: {sum(len(G.nodes[n].get('secondary', [])) for n in G.nodes)}")


if __name__ == "__main__":
    main()
