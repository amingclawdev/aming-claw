from __future__ import annotations

from agent.governance.graph_rule_fingerprint import build_graph_rule_fingerprint
from agent.governance.governance_hints import mutate_governance_hint_text
from agent.tests.fixtures.rule_fingerprint_project import (
    RULE_FINGERPRINT_SCENARIO_ID,
    apply_config_change,
    apply_hint_change,
    create_rule_fingerprint_fixture_project,
    rollback_config_change,
    rollback_hint_change,
)


def test_generated_project_rule_fingerprint_tracks_config_and_hint_rollback(tmp_path):
    assert RULE_FINGERPRINT_SCENARIO_ID == "RULE-FINGERPRINT-ROLLBACK-001"
    fixture = create_rule_fingerprint_fixture_project(tmp_path)

    anchor = build_graph_rule_fingerprint(fixture.root)

    apply_config_change(fixture)
    config_changed = build_graph_rule_fingerprint(fixture.root)
    assert config_changed["fingerprint"] != anchor["fingerprint"]
    assert config_changed["components"]["semantic_enrichment_config"]["fingerprint"] != (
        anchor["components"]["semantic_enrichment_config"]["fingerprint"]
    )

    rollback_config_change(fixture)
    config_rolled_back = build_graph_rule_fingerprint(fixture.root)
    assert config_rolled_back["fingerprint"] == anchor["fingerprint"]

    apply_hint_change(fixture)
    hint_changed = build_graph_rule_fingerprint(fixture.root)
    assert hint_changed["fingerprint"] != anchor["fingerprint"]
    assert hint_changed["components"]["source_hints"]["hint_count"] == 1

    rollback_hint_change(fixture)
    hint_rolled_back = build_graph_rule_fingerprint(fixture.root)
    assert hint_rolled_back["fingerprint"] == anchor["fingerprint"]


def test_rule_fingerprint_tracks_normalized_json_governance_binding_envelope(tmp_path):
    project = tmp_path / "project"
    config = project / "config" / "service.json"
    config.parent.mkdir(parents=True)
    original = '{"business_value":1}\n'
    config.write_text(original, encoding="utf-8")
    anchor = build_graph_rule_fingerprint(project)

    attached = mutate_governance_hint_text(
        original,
        source_path="config/service.json",
        action="attach",
        event={
            "path": ".",
            "role": "config",
            "target_module": "service.registry",
        },
    )
    config.write_text(attached["text"], encoding="utf-8")
    changed = build_graph_rule_fingerprint(project)

    assert changed["fingerprint"] != anchor["fingerprint"]
    assert changed["components"]["governance_binding_hints"]["hint_count"] == 1
    assert changed["components"]["governance_binding_hints"]["hints"][0][
        "path"
    ] == "config/service.json"

    rolled_back = mutate_governance_hint_text(
        attached["text"],
        source_path="config/service.json",
        action="rollback",
        rollback_envelope=None,
    )
    config.write_text(rolled_back["text"], encoding="utf-8")
    restored = build_graph_rule_fingerprint(project)

    assert restored["fingerprint"] == anchor["fingerprint"]
