"""PB-006 Governance Hint rollback delta oracle tests."""

from __future__ import annotations

from agent.governance.governance_hints import (
    diff_governance_hint_bindings,
    parse_governance_hint_bindings,
)


def _hints(target: str, *, role: str = "doc"):
    return parse_governance_hint_bindings(
        "<!-- governance-hint\n"
        f'{{"attach_to_node":{{"target_node_id":"{target}","role":"{role}"}}}}\n'
        "-->\n# Notes\n",
        source_path="docs/orphan.md",
    )


def test_pb006_hint_added_delta_is_invertible() -> None:
    summary = diff_governance_hint_bindings([], _hints("L7.service"), target_commit="H1")

    assert summary["by_type"] == {"hint_added": 1}
    delta = summary["deltas"][0]
    assert delta["delta_type"] == "hint_added"
    assert delta["path"] == "docs/orphan.md"
    assert delta["previous"] is None
    assert delta["current"]["target_node_id"] == "L7.service"
    assert delta["inverse_action"] == "remove_binding"


def test_pb006_hint_changed_delta_carries_previous_and_current_binding() -> None:
    summary = diff_governance_hint_bindings(
        _hints("L7.service"),
        _hints("L7.repository", role="test"),
        source_commit="H1",
        target_commit="H2",
    )

    assert summary["by_type"] == {"hint_changed": 1}
    delta = summary["deltas"][0]
    assert delta["delta_type"] == "hint_changed"
    assert delta["previous"]["target_node_id"] == "L7.service"
    assert delta["previous"]["field"] == "secondary"
    assert delta["current"]["target_node_id"] == "L7.repository"
    assert delta["current"]["field"] == "test"
    assert delta["inverse_action"] == "restore_previous_binding"


def test_pb006_hint_removed_delta_can_restore_prior_binding() -> None:
    summary = diff_governance_hint_bindings(
        _hints("L7.service"),
        [],
        source_commit="H2",
        target_commit="H3",
    )

    assert summary["by_type"] == {"hint_removed": 1}
    delta = summary["deltas"][0]
    assert delta["delta_type"] == "hint_removed"
    assert delta["previous"]["target_node_id"] == "L7.service"
    assert delta["current"] is None
    assert delta["inverse_action"] == "restore_binding"


def test_pb006_rollback_restored_delta_records_epoch() -> None:
    summary = diff_governance_hint_bindings(
        [],
        _hints("L7.service"),
        rollback_epoch="rollback-001",
        source_commit="H3",
        target_commit="H1",
    )

    assert summary["rollback_epoch"] == "rollback-001"
    assert summary["by_type"] == {"hint_rollback_restored": 1}
    delta = summary["deltas"][0]
    assert delta["delta_type"] == "hint_rollback_restored"
    assert delta["rollback_epoch"] == "rollback-001"
    assert delta["current"]["target_node_id"] == "L7.service"
    assert delta["inverse_action"] == "remove_restored_binding"
