from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance.reconcile_semantic_enrichment import (
    append_review_feedback,
    load_review_feedback,
    run_semantic_enrichment,
)
from agent.governance.db import _ensure_schema


PID = "semantic-enrichment-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    yield c
    c.close()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _graph(node_id: str = "L7.1") -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": node_id,
                    "layer": "L7",
                    "title": "Backlog Runtime",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/backlog_runtime.py"],
                    "secondary": ["docs/dev/backlog-runtime.md"],
                    "test": ["agent/tests/test_backlog_runtime.py"],
                    "metadata": {
                        "subsystem": "backlog",
                        "functions": [
                            {
                                "name": "claim_next",
                                "path": "agent/governance/backlog_runtime.py",
                                "lineno": 12,
                            }
                        ],
                    },
                }
            ],
            "edges": [],
        }
    }


def _create_snapshot(conn: sqlite3.Connection, project: Path, *, snapshot_kind: str = "full") -> None:
    _write(
        project / "agent" / "governance" / "backlog_runtime.py",
        "def claim_next():\n    return 'task'\n",
    )
    _write(project / "docs" / "dev" / "backlog-runtime.md", "# Backlog Runtime\n")
    _write(project / "agent" / "tests" / "test_backlog_runtime.py", "def test_claim_next():\n    assert True\n")
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=f"{snapshot_kind}-semantic-test",
        commit_sha="abc1234",
        snapshot_kind=snapshot_kind,
        graph_json=_graph(),
        notes=json.dumps({"state_only": True}),
    )
    conn.commit()


def test_semantic_enrichment_uses_feedback_on_retry(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append({"stage": stage, "payload": payload})
        feedback_ids = [item["feedback_id"] for item in payload["review_feedback"]]
        return {
            "feature_name": "Backlog Runtime State Flow",
            "semantic_summary": "Owns backlog task state transitions.",
            "intent": "stateful backlog runtime",
            "domain_label": "state",
            "applied_feedback_ids": feedback_ids,
            "doc_coverage_review": {"bound": True, "action": "keep"},
            "test_coverage_review": {"bound": True, "action": "keep"},
        }

    first = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
    )

    assert first["summary"]["ai_complete_count"] == 1
    assert first["semantic_index"]["features"][0]["feature_name"] == "Backlog Runtime State Flow"
    assert Path(first["semantic_index_path"]).exists()
    assert seen_payloads[0]["stage"] == "reconcile_semantic_feature"
    assert seen_payloads[0]["payload"]["instructions"]["mutate_project_files"] is False
    assert seen_payloads[0]["payload"]["instructions"]["analyzer"] == "reconcile_semantic"
    assert seen_payloads[0]["payload"]["instructions"]["prompt_template"]
    assert seen_payloads[0]["payload"]["feature"]["source_excerpt"]

    second = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        feedback_items={
            "feedback_id": "fb-doc-1",
            "target_type": "node",
            "target_id": "L7.1",
            "priority": "P1",
            "issue": "Feature name is too runtime-specific.",
            "expected_change": "Mention persisted backlog state.",
        },
        use_ai=True,
        ai_call=fake_ai,
        created_by="reviewer",
    )

    assert second["feedback_round"] == 1
    feature = second["semantic_index"]["features"][0]
    assert feature["applied_feedback_ids"] == ["fb-doc-1"]
    assert feature["unresolved_feedback_ids"] == []
    assert load_review_feedback(PID, "full-semantic-test")[0]["feedback_id"] == "fb-doc-1"
    assert seen_payloads[-1]["payload"]["review_feedback"][0]["expected_change"] == "Mention persisted backlog state."
    notes = json.loads(
        conn.execute(
            "SELECT notes FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
            (PID, "full-semantic-test"),
        ).fetchone()["notes"]
    )
    assert notes["semantic_enrichment"]["latest_round"] == 1
    assert notes["semantic_feedback"]["feedback_count"] == 1


def test_semantic_enrichment_is_snapshot_kind_agnostic(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, snapshot_kind="scope")

    result = run_semantic_enrichment(
        conn,
        PID,
        "scope-semantic-test",
        project,
        use_ai=False,
        created_by="test",
    )

    assert result["semantic_index"]["snapshot_kind"] == "scope"
    assert result["semantic_index"]["features"][0]["enrichment_status"] == "heuristic"
    assert result["summary"]["quality_flag_counts"]["missing_symbol_refs"] == 1


def test_append_review_feedback_normalizes_append_only_items(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)

    result = append_review_feedback(
        conn,
        PID,
        "full-semantic-test",
        {
            "target_type": "path",
            "path": "agent/governance/backlog_runtime.py",
            "issue": "Needs clearer state ownership.",
        },
        created_by="observer",
    )

    assert result["added_count"] == 1
    feedback = load_review_feedback(PID, "full-semantic-test")
    assert len(feedback) == 1
    assert feedback[0]["target_id"] == "agent/governance/backlog_runtime.py"
    assert feedback[0]["created_by"] == "observer"


def test_semantic_enrichment_uses_project_config_override(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    override_path = project / ".aming-claw" / "reconcile" / "semantic_enrichment.yaml"
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        "\n".join(
            [
                'model: "gpt-test-semantic"',
                "use_ai_default: true",
                "input_policy:",
                "  max_excerpt_chars: 8",
                "prompt_template: |-",
                "  Project-specific semantic analyzer prompt.",
            ]
        ),
        encoding="utf-8",
    )
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append(payload)
        return {"feature_name": "Configured Semantic Feature"}

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=None,
        ai_call=fake_ai,
        created_by="test",
    )

    feature = result["semantic_index"]["features"][0]
    assert feature["feature_name"] == "Configured Semantic Feature"
    assert result["semantic_index"]["semantic_config"]["model"] == "gpt-test-semantic"
    assert seen_payloads[0]["instructions"]["model"] == "gpt-test-semantic"
    assert seen_payloads[0]["instructions"]["prompt_template"] == "Project-specific semantic analyzer prompt."
    excerpt = seen_payloads[0]["feature"]["source_excerpt"]["agent/governance/backlog_runtime.py"]
    assert len(excerpt) <= 8
