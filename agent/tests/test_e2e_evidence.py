from __future__ import annotations

import sqlite3

import pytest

from agent.governance import e2e_evidence
from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema


PID = "e2e-evidence-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    monkeypatch.setattr("agent.governance.e2e_evidence._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    yield c
    c.close()


def _graph(feature_hash: str) -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "src.api",
                    "primary": ["src/api.ts"],
                    "secondary": [],
                    "test": ["tests/smoke.test.mjs"],
                    "metadata": {"feature_hash": feature_hash},
                }
            ],
            "edges": [],
        }
    }


def _inventory(file_hash: str) -> list[dict]:
    return [
        {
            "path": "src/api.ts",
            "file_hash": file_hash,
            "sha256": file_hash.replace("sha256:", ""),
            "file_kind": "code",
            "scan_status": "scanned",
        },
        {
            "path": "tests/smoke.test.mjs",
            "file_hash": "sha256:test",
            "sha256": "test",
            "file_kind": "test",
            "scan_status": "scanned",
        },
    ]


def _snapshot(conn, snapshot_id: str, feature_hash: str, file_hash: str):
    snap = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha=snapshot_id,
        snapshot_kind="scope",
        graph_json=_graph(feature_hash),
        file_inventory=_inventory(file_hash),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot_id,
        nodes=_graph(feature_hash)["deps_graph"]["nodes"],
        edges=[],
    )
    return snap


def _config() -> dict:
    return {
        "auto_run": False,
        "default_timeout_sec": 900,
        "suites": {
            "dashboard.semantic.safe": {
                "label": "Dashboard semantic safe path",
                "command": "node e2e-trunk.mjs",
                "live_ai": False,
                "requires_human_approval": False,
                "trigger": {"paths": ["src/**"], "nodes": ["L7.1"], "tags": ["dashboard"]},
            }
        },
    }


def _suite_row(impact: dict, suite_id: str) -> dict:
    return next(row for row in impact["suites"] if row["suite_id"] == suite_id)


def test_e2e_evidence_records_hashes_and_marks_later_snapshot_stale(conn):
    _snapshot(conn, "scope-old", "sha256:feature-old", "sha256:file-old")
    conn.commit()

    recorded = e2e_evidence.record_e2e_evidence(
        conn,
        PID,
        "scope-old",
        {
            "suite_id": "dashboard.semantic.safe",
            "status": "passed",
            "run_id": "run-1",
            "covered_node_ids": ["L7.1"],
            "covered_files": ["src/api.ts"],
            "artifact_path": "/tmp/report.json",
        },
    )

    assert recorded["ok"] is True
    assert recorded["covered_node_count"] == 1
    current = e2e_evidence.plan_e2e_impact(conn, PID, "scope-old", _config())
    assert current["summary"]["current"] == 1
    assert current["suites"][0]["status"] == "current"
    assert current["suites"][0]["can_autorun"] is False
    assert current["suites"][0]["blocked_reason"] == ""

    _snapshot(conn, "scope-new", "sha256:feature-new", "sha256:file-new")
    conn.commit()
    stale = e2e_evidence.plan_e2e_impact(conn, PID, "scope-new", _config())

    assert stale["summary"]["stale"] == 1
    assert stale["suites"][0]["required"] is True
    reason_kinds = {reason["kind"] for reason in stale["suites"][0]["stale_reasons"]}
    assert "file_hash_changed" in reason_kinds
    assert "node_feature_hash_changed" in reason_kinds


def test_e2e_impact_marks_missing_suite_without_evidence(conn):
    _snapshot(conn, "scope-new", "sha256:feature-new", "sha256:file-new")
    conn.commit()

    impact = e2e_evidence.plan_e2e_impact(
        conn,
        PID,
        "scope-new",
        _config(),
        changed_files=["src/api.ts"],
    )

    assert impact["summary"]["missing"] == 1
    assert impact["suites"][0]["trigger_matched"] is True
    assert impact["suites"][0]["required"] is True


def test_e2e_impact_classifies_docker_live_ai_manual_as_blocked_not_autorun(conn):
    _snapshot(conn, "scope-docker", "sha256:feature", "sha256:file")
    conn.commit()

    impact = e2e_evidence.plan_e2e_impact(
        conn,
        PID,
        "scope-docker",
        {
            "auto_run": True,
            "default_timeout_sec": 900,
            "suites": {
                "docker.ai.install": {
                    "label": "Docker AI install audit",
                    "command": "docker/hn-install-audit/run-install-audit.sh --host both --cleanup",
                    "auto_run": True,
                    "live_ai": True,
                    "requires_human_approval": True,
                    "mutates_db": True,
                    "isolation_project": "dashboard-e2e-fixture",
                    "docker_ai_e2e": {"provider_id": "aming-claw-self-install"},
                    "trigger": {"tags": ["docker", "cleanup"]},
                }
            },
        },
    )

    row = _suite_row(impact, "docker.ai.install")
    assert row["can_autorun"] is False
    assert row["manual_approval_required"] is True
    assert row["execution_mode"] == "manual_approval"
    assert row["blocked_reason"] == "live_ai_requires_manual_approval"
    assert row["live_ai"] is True
    assert row["requires_human_approval"] is True
    assert {
        "docker",
        "live_ai",
        "manual_approval",
        "mutating_governance",
        "fixture",
        "cleanup",
    }.issubset(set(row["suite_classes"]))


def test_e2e_impact_classifies_fixture_static_production_and_source_only(conn):
    _snapshot(conn, "scope-route", "sha256:feature", "sha256:file")
    conn.commit()

    impact = e2e_evidence.plan_e2e_impact(
        conn,
        PID,
        "scope-route",
        {
            "auto_run": True,
            "default_timeout_sec": 900,
            "suites": {
                "dashboard.semantic.safe": {
                    "command": "node frontend/dashboard/scripts/e2e-trunk.mjs --reset --skip-dashboard",
                    "auto_run": False,
                    "live_ai": False,
                    "requires_human_approval": False,
                    "mutates_db": True,
                    "isolation_project": "dashboard-e2e-fixture",
                    "trigger": {"tags": ["dashboard", "semantic"]},
                },
                "dashboard.static.production": {
                    "command": "node frontend/dashboard/scripts/e2e-trunk.mjs --static-route --build-dashboard",
                    "auto_run": True,
                    "live_ai": False,
                    "requires_human_approval": False,
                    "mutates_db": False,
                    "isolation_project": PID,
                    "trigger": {"tags": ["dashboard", "static-route", "production"]},
                },
            },
        },
    )

    fixture_row = _suite_row(impact, "dashboard.semantic.safe")
    assert fixture_row["manual_approval_required"] is False
    assert fixture_row["execution_mode"] == "manual"
    assert {"fixture", "mutating_governance", "source_only"}.issubset(set(fixture_row["suite_classes"]))

    static_row = _suite_row(impact, "dashboard.static.production")
    assert static_row["manual_approval_required"] is False
    assert static_row["can_autorun"] is True
    assert static_row["execution_mode"] == "autorun"
    assert {"static", "production"}.issubset(set(static_row["suite_classes"]))
    assert "mutating_governance" not in static_row["suite_classes"]


def test_e2e_impact_distinguishes_structured_output_fixture_from_live_ai(conn):
    _snapshot(conn, "scope-ai-fixture", "sha256:feature", "sha256:file")
    conn.commit()

    impact = e2e_evidence.plan_e2e_impact(
        conn,
        PID,
        "scope-ai-fixture",
        {
            "auto_run": True,
            "default_timeout_sec": 900,
            "suites": {
                "service_router_ai_structured_output_fixture": {
                    "command": "node scripts/test-scenario-manager.mjs run service_router_ai_structured_output_fixture",
                    "auto_run": True,
                    "live_ai": False,
                    "requires_human_approval": False,
                    "mutates_db": False,
                    "isolation_project": "router-fixture",
                    "safety": {
                        "fixture_only": True,
                        "calls_models": False,
                    },
                    "execution_policy": {
                        "lane": "ai_structured_output_fixture",
                        "model_calls": "forbidden",
                    },
                    "trigger": {"tags": ["ai_structured_output", "fixture"]},
                }
            },
        },
    )

    row = _suite_row(impact, "service_router_ai_structured_output_fixture")
    assert row["live_ai"] is False
    assert row["can_autorun"] is True
    assert row["execution_mode"] == "autorun"
    assert {
        "ai_structured_output",
        "structured_output_fixture",
        "model_calls_forbidden",
        "fixture",
    }.issubset(set(row["suite_classes"]))
    assert "environment-check" not in row["suite_classes"]
    assert row["ai_evidence_policy"]["lane"] == "structured_output_fixture"
    assert row["ai_evidence_policy"]["model_calls_forbidden"] is True
    assert row["ai_evidence_policy"]["readiness_check"] is False
    assert row["ai_evidence_policy"]["invocation_evidence_required"] is False


def test_e2e_impact_classifies_live_ai_environment_as_manual_not_autorun(conn):
    _snapshot(conn, "scope-live-ai-env", "sha256:feature", "sha256:file")
    conn.commit()

    impact = e2e_evidence.plan_e2e_impact(
        conn,
        PID,
        "scope-live-ai-env",
        {
            "auto_run": True,
            "default_timeout_sec": 900,
            "suites": {
                "live_ai.environment.tester": {
                    "command": "node scripts/live-ai-environment-probe.mjs --role tester --allow-live-ai",
                    "auto_run": True,
                    "mutates_db": False,
                    "isolation_project": PID,
                    "live_ai_environment": {
                        "expected": {
                            "role": "tester",
                            "provider": "openai",
                            "model": "gpt-5.4",
                        }
                    },
                    "trigger": {"tags": ["live-ai", "environment-check", "ai-runtime"]},
                }
            },
        },
    )

    row = _suite_row(impact, "live_ai.environment.tester")
    assert row["live_ai"] is True
    assert row["can_autorun"] is False
    assert row["execution_mode"] == "manual_approval"
    assert row["manual_approval_required"] is True
    assert row["blocked_reason"] == "live_ai_requires_manual_approval"
    assert {
        "manual",
        "live_ai",
        "environment-check",
        "live_ai_environment",
        "requires_allow_live_ai",
        "explicit_allow_live_ai",
    }.issubset(set(row["suite_classes"]))
    assert row["ai_evidence_policy"] == {
        "lane": "live_ai_environment",
        "readiness_check": True,
        "invocation_evidence_required": True,
        "model_calls_forbidden": False,
        "requires_allow_live_ai": True,
        "allow_live_ai_flag_present": True,
        "sanitized_evidence_required": True,
        "expected_provider": "openai",
        "expected_model": "gpt-5.4",
        "expected_role": "tester",
    }
