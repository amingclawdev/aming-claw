import sqlite3

from agent.governance.graph_correction_patches import (
    accept_patch,
    annotate_graph_node_roles,
    annotate_graph_relationship_metrics,
    apply_correction_patches,
    create_patch,
    ensure_schema,
    list_replayable_patches,
    persist_node_migrations,
    record_patch_apply_report,
)


PID = "test-project"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _graph():
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L3.1",
                    "layer": "L3",
                    "title": "Governance",
                    "metadata": {"children": ["L7.1", "L7.2"]},
                },
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "agent.governance",
                    "primary": ["agent/governance/__init__.py"],
                    "secondary": ["docs/governance/reconcile-workflow.md"],
                    "test": ["agent/tests/test_graph_generator.py"],
                    "metadata": {
                        "function_count": 0,
                        "typed_relations": [],
                        "hierarchy_parent": "L3.1",
                    },
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "agent.governance.server",
                    "primary": ["agent/governance/server.py"],
                    "metadata": {
                        "function_count": 8,
                        "typed_relations": [],
                        "hierarchy_parent": "L3.1",
                    },
                },
            ],
            "edges": [
                {"src": "L3.1", "dst": "L7.1", "edge_type": "contains", "direction": "hierarchy"},
                {"src": "L3.1", "dst": "L7.2", "edge_type": "contains", "direction": "hierarchy"},
            ],
        }
    }


def test_file_role_annotation_marks_package_marker_and_coverage_noise():
    result = annotate_graph_node_roles(_graph())
    nodes = {node["id"]: node for node in result["graph"]["deps_graph"]["nodes"]}

    marker = nodes["L7.1"]["metadata"]
    assert marker["file_role"] == "package_marker"
    assert marker["exclude_as_feature"] is True
    assert "coverage_noise_candidate" in marker["quality_flags"]
    assert result["report"]["role_counts"]["package_marker"] == 1


def test_file_role_annotation_excludes_ts_contracts_and_entrypoint_support():
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.types",
                    "layer": "L7",
                    "title": "frontend.dashboard.src.types",
                    "primary": ["frontend/dashboard/src/types.ts"],
                    "metadata": {
                        "function_count": 0,
                        "typed_relations": [{"relation_type": "uses_task_metadata"}],
                    },
                },
                {
                    "id": "L7.main",
                    "layer": "L7",
                    "title": "frontend.dashboard.src.main",
                    "primary": ["frontend/dashboard/src/main.tsx"],
                    "metadata": {"function_count": 0, "typed_relations": []},
                },
            ],
            "edges": [],
        }
    }

    result = annotate_graph_node_roles(graph)
    nodes = {node["id"]: node for node in result["graph"]["deps_graph"]["nodes"]}

    type_meta = nodes["L7.types"]["metadata"]
    assert type_meta["file_role"] == "type_contract"
    assert type_meta["exclude_as_feature"] is True
    assert "coverage_noise_candidate" in type_meta["quality_flags"]

    main_meta = nodes["L7.main"]["metadata"]
    assert main_meta["file_role"] == "entrypoint_support"
    assert main_meta["exclude_as_feature"] is True
    assert "coverage_noise_candidate" in main_meta["quality_flags"]


def test_relationship_metrics_exclude_hierarchy_edges_from_fan_in_out():
    graph = _graph()
    graph["deps_graph"]["edges"].append(
        {
            "src": "L7.2",
            "dst": "L7.1",
            "edge_type": "depends_on",
            "direction": "dependency",
        }
    )

    result = annotate_graph_relationship_metrics(graph)
    nodes = {node["id"]: node for node in result["graph"]["deps_graph"]["nodes"]}

    assert nodes["L3.1"]["metadata"]["graph_metrics"]["hierarchy_out"] == 2
    assert nodes["L3.1"]["metadata"]["graph_metrics"]["fan_out"] == 0
    assert nodes["L7.1"]["metadata"]["graph_metrics"]["fan_in"] == 1
    assert nodes["L7.2"]["metadata"]["graph_metrics"]["fan_out"] == 1


def test_accepted_patch_replays_and_records_migration():
    conn = _conn()
    create_patch(
        conn,
        PID,
        patch_id="patch-package-marker",
        patch_type="mark_package_marker",
        target_node_id="L7.1",
        patch_json={
            "target_node_id": "L7.1",
            "semantic_policy": "drop_leaf_semantic_keep_evidence",
            "feedback_policy": "move_open_feedback_to_parent",
            "doc_test_policy": "recalculate_coverage",
        },
        evidence={"reason": "empty package initializer"},
        created_by="ai-review",
    )
    assert accept_patch(conn, PID, "patch-package-marker", accepted_by="observer")
    patches = list_replayable_patches(conn, PID)

    result = apply_correction_patches(
        _graph(),
        patches,
        from_snapshot_id="full-old",
        to_snapshot_id="full-new",
    )
    nodes = {node["id"]: node for node in result["graph"]["deps_graph"]["nodes"]}

    assert result["report"]["applied_count"] == 1
    assert nodes["L7.1"]["metadata"]["file_role"] == "package_marker"
    assert nodes["L7.1"]["metadata"]["exclude_as_feature"] is True
    assert result["report"]["migrations"][0]["old_node_id"] == "L7.1"

    migration_count = persist_node_migrations(
        conn,
        PID,
        from_snapshot_id="full-old",
        to_snapshot_id="full-new",
        migrations=result["report"]["migrations"],
    )
    apply_counts = record_patch_apply_report(
        conn,
        PID,
        snapshot_id="full-new",
        report=result["report"],
    )
    conn.commit()

    assert migration_count == 1
    assert apply_counts == {"applied": 1, "stale": 0}
    row = conn.execute(
        "SELECT last_apply_status, applied_snapshot_id FROM graph_correction_patches WHERE patch_id=?",
        ("patch-package-marker",),
    ).fetchone()
    assert row["last_apply_status"] == "applied"
    assert row["applied_snapshot_id"] == "full-new"


def test_missing_patch_target_is_marked_stale():
    conn = _conn()
    create_patch(
        conn,
        PID,
        patch_id="patch-missing",
        patch_type="mark_package_marker",
        target_node_id="L7.missing",
        patch_json={"target_node_id": "L7.missing"},
        evidence={"reason": "stale node id"},
    )
    assert accept_patch(conn, PID, "patch-missing", accepted_by="observer")

    patches = list_replayable_patches(conn, PID)
    result = apply_correction_patches(_graph(), patches, to_snapshot_id="full-new")
    assert result["report"]["stale_count"] == 1

    counts = record_patch_apply_report(conn, PID, snapshot_id="full-new", report=result["report"])
    conn.commit()
    assert counts == {"applied": 0, "stale": 1}
    row = conn.execute(
        "SELECT status, last_apply_status FROM graph_correction_patches WHERE patch_id=?",
        ("patch-missing",),
    ).fetchone()
    assert row["status"] == "stale"
    assert row["last_apply_status"] == "target_node_missing"


def test_add_edge_patch_self_edge_is_marked_stale():
    conn = _conn()
    create_patch(
        conn,
        PID,
        patch_id="patch-self-edge",
        patch_type="add_edge",
        target_node_id="L7.1",
        patch_json={
            "edge": {
                "src": "L7.1",
                "dst": "L7.1",
                "edge_type": "depends_on",
                "direction": "dependency",
            }
        },
        evidence={"reason": "invalid semantic suggestion"},
    )
    assert accept_patch(conn, PID, "patch-self-edge", accepted_by="observer")

    patches = list_replayable_patches(conn, PID)
    result = apply_correction_patches(_graph(), patches, to_snapshot_id="full-new")

    assert result["report"]["applied_count"] == 0
    assert result["report"]["stale_count"] == 1
    assert result["report"]["stale"][0]["status"] == "self_edge_not_allowed"
    edges = result["graph"]["deps_graph"]["edges"]
    assert not any(edge.get("src") == "L7.1" and edge.get("dst") == "L7.1" for edge in edges)

    counts = record_patch_apply_report(conn, PID, snapshot_id="full-new", report=result["report"])
    conn.commit()
    assert counts == {"applied": 0, "stale": 1}
    row = conn.execute(
        "SELECT status, last_apply_status FROM graph_correction_patches WHERE patch_id=?",
        ("patch-self-edge",),
    ).fetchone()
    assert row["status"] == "stale"
    assert row["last_apply_status"] == "self_edge_not_allowed"


def test_add_edge_patch_missing_endpoint_is_marked_stale():
    conn = _conn()
    create_patch(
        conn,
        PID,
        patch_id="patch-empty-edge",
        patch_type="add_edge",
        target_node_id="L7.1",
        patch_json={"edge": {"src": "L7.1", "dst": ""}},
        evidence={"reason": "incomplete semantic suggestion"},
    )
    assert accept_patch(conn, PID, "patch-empty-edge", accepted_by="observer")

    result = apply_correction_patches(
        _graph(),
        list_replayable_patches(conn, PID),
        to_snapshot_id="full-new",
    )

    assert result["report"]["applied_count"] == 0
    assert result["report"]["stale_count"] == 1
    assert result["report"]["stale"][0]["status"] == "edge_endpoint_empty"
