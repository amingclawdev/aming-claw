from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent.governance import ai_output_intake
from agent.governance.asset_binding_proposals import (
    attach_asset_binding_self_precheck,
    build_asset_binding_candidate,
    precheck_asset_binding_proposal,
)
from agent.governance.db import _ensure_schema
from agent.governance.errors import ValidationError
from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
)


PID = "asset-binding-proposal-test"


def _tmp_project(files: dict[str, str]) -> str:
    root = tempfile.mkdtemp()
    for rel, content in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def test_precheck_rejects_weak_materialize_and_accepts_subagent_self_precheck() -> None:
    weak_materialize = build_asset_binding_candidate(
        asset_kind="doc",
        asset_path="docs/ref.md",
        target_node_id="L7.1",
        evidence_kind="path_reference",
        evidence=[{"path": "docs/ref.md", "matched": "agent/service.py"}],
        operation="materialize_binding",
    )

    rejected = precheck_asset_binding_proposal(weak_materialize, mode="server_gate")
    assert rejected["ok"] is False
    assert "weak_evidence_cannot_materialize" in rejected["errors"]
    assert "doc_materialization_requires_review_or_hint" in rejected["errors"]

    subagent_proposal = {
        "schema_version": "asset_binding_proposal.v1",
        "operation": "propose_binding",
        "asset_kind": "doc",
        "asset_path": "docs/ref.md",
        "target_node_id": "L7.1",
        "evidence_kind": "path_reference",
        "evidence": [{"path": "docs/ref.md", "matched": "agent/service.py"}],
        "proposed_by": "mf_subagent",
    }
    missing_self_precheck = precheck_asset_binding_proposal(
        subagent_proposal,
        mode="server_gate",
    )
    assert missing_self_precheck["ok"] is False
    assert "self_precheck_required" in missing_self_precheck["errors"]

    with_self_precheck = attach_asset_binding_self_precheck(subagent_proposal)
    accepted = precheck_asset_binding_proposal(with_self_precheck, mode="server_gate")
    assert accepted["ok"] is True
    assert accepted["decision"] == "review_required"


def test_ai_output_intake_routes_asset_binding_proposals_to_review_pending() -> None:
    conn = _conn()
    proposal = attach_asset_binding_self_precheck({
        "schema_version": "asset_binding_proposal.v1",
        "operation": "propose_binding",
        "asset_kind": "doc",
        "asset_path": "docs/ref.md",
        "target_node_id": "L7.1",
        "evidence_kind": "path_reference",
        "evidence": [{"path": "docs/ref.md", "matched": "agent/service.py"}],
        "proposed_by": "mf_subagent",
    })
    self_precheck = proposal.pop("self_precheck")

    result = ai_output_intake.submit_ai_output(
        conn,
        PID,
        {
            "task_type": "asset_binding_proposal",
            "snapshot_id": "scope-test",
            "target_type": "node",
            "target_id": "L7.1",
            "producer": "mf_subagent",
            "payload": proposal,
            "self_precheck": self_precheck,
            "graph_query_trace_ids": ["gqt-test"],
        },
        actor="mf_subagent",
    )
    conn.commit()

    assert result["ok"] is True
    assert result["route_status"] == "review_pending"
    output = ai_output_intake.get_ai_output(conn, PID, result["output_id"])
    assert output is not None
    assert output["metadata"]["asset_binding_precheck"]["ok"] is True


def test_ai_output_intake_rejects_weak_direct_graph_mutation() -> None:
    conn = _conn()
    proposal = attach_asset_binding_self_precheck({
        "schema_version": "asset_binding_proposal.v1",
        "operation": "materialize_binding",
        "asset_kind": "doc",
        "asset_path": "docs/ref.md",
        "target_node_id": "L7.1",
        "evidence_kind": "path_reference",
        "evidence": [{"path": "docs/ref.md", "matched": "agent/service.py"}],
        "proposed_by": "mf_subagent",
    })
    self_precheck = proposal.pop("self_precheck")

    with pytest.raises(ValidationError, match="asset binding proposal failed precheck"):
        ai_output_intake.submit_ai_output(
            conn,
            PID,
            {
                "task_type": "asset_binding_proposal",
                "snapshot_id": "scope-test",
                "target_type": "node",
                "target_id": "L7.1",
                "producer": "mf_subagent",
                "payload": proposal,
                "self_precheck": self_precheck,
            },
            actor="mf_subagent",
        )


def test_doc_path_matches_become_candidates_not_trusted_bindings() -> None:
    project = _tmp_project({
        "agent/mymod.py": "def hello():\n    return 'ok'\n",
        "docs/ref.md": "# Ref\nSee agent/mymod.py for details.\n",
    })

    result = build_graph_v2_from_symbols(project, dry_run=True)
    candidate = build_rebase_candidate_graph(
        project,
        result,
        session_id="asset-binding-proposal",
        run_id=result["run_id"],
    )
    graph_node = next(
        node for node in candidate["deps_graph"]["nodes"]
        if node["layer"] == "L7" and node["title"] == "agent.mymod"
    )

    assert graph_node["secondary"] == []
    metadata = graph_node["metadata"]
    assert metadata["candidate_doc_files"] == ["docs/ref.md"]
    proposal = metadata["asset_binding_candidates"][0]
    assert proposal["operation"] == "propose_binding"
    assert proposal["asset_kind"] == "doc"
    assert proposal["self_precheck"]["ok"] is True
    assert proposal["self_precheck"]["decision"] == "review_required"

    rows = {row["path"]: row for row in result["file_inventory"]}
    assert rows["docs/ref.md"]["scan_status"] == "orphan"


def test_source_controlled_governance_hint_materializes_doc_binding() -> None:
    hint = {
        "binding": {
            "role": "doc",
            "path": "docs/ref.md",
            "target_module": "agent.mymod",
        }
    }
    project = _tmp_project({
        "agent/mymod.py": "def hello():\n    return 'ok'\n",
        "docs/ref.md": (
            "<!-- governance-hint "
            + json.dumps(hint, sort_keys=True)
            + " -->\n# Ref\nSee agent/mymod.py for details.\n"
        ),
    })

    result = build_graph_v2_from_symbols(project, dry_run=True)
    candidate = build_rebase_candidate_graph(
        project,
        result,
        session_id="asset-binding-hint",
        run_id=result["run_id"],
    )
    graph_node = next(
        node for node in candidate["deps_graph"]["nodes"]
        if node["layer"] == "L7" and node["title"] == "agent.mymod"
    )

    assert graph_node["secondary"] == ["docs/ref.md"]
    assert graph_node["metadata"]["candidate_doc_files"] == []
    assert candidate["architecture_summary"]["governance_hint_bindings"]["applied_count"] == 1
