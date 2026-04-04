"""Replay validation tests for all 9 chain stage contract boundaries.

Each test validates a specific contract boundary using only static fixture data
from agent.tests.fixtures.replay_data — zero external dependencies.
"""

from agent.governance.artifacts import ROLE_ARTIFACT_SCHEMAS, validate_role_artifact
from agent.governance.failure_classifier import classify_gate_failure
from agent.tests.fixtures.replay_data import (
    COORDINATOR_ROUTING,
    DEV_OUTPUT,
    FAILURE_CLASSIFICATION,
    GATEKEEPER_REPORT,
    MEMORY_WRITE,
    MERGE_REPORT,
    PM_OUTPUT,
    QA_REPORT,
    TEST_REPORT,
)

KNOWN_TASK_TYPES = {"pm", "dev", "test", "qa", "gatekeeper", "merge", "coordinator"}


def test_replay_pm_output():
    """Case 1: Validate PM output against ROLE_ARTIFACT_SCHEMAS['pm'] + PRD fields."""
    # Validate via schema
    result = validate_role_artifact("pm", PM_OUTPUT)
    assert result["pass"] is True, f"PM validation failed: {result}"
    assert result["missing_fields"] == []

    # Validate required_fields are present and non-empty
    for field in ROLE_ARTIFACT_SCHEMAS["pm"]["required_fields"]:
        assert field in PM_OUTPUT, f"Missing PM field: {field}"
        assert PM_OUTPUT[field], f"Empty PM field: {field}"

    # Validate PRD-level fields
    for prd_field in ("target_files", "acceptance_criteria", "verification"):
        assert prd_field in PM_OUTPUT, f"Missing PRD field: {prd_field}"
        assert PM_OUTPUT[prd_field], f"Empty PRD field: {prd_field}"


def test_replay_dev_output():
    """Case 2: Validate Dev output against ROLE_ARTIFACT_SCHEMAS['dev']."""
    result = validate_role_artifact("dev", DEV_OUTPUT)
    assert result["pass"] is True, f"Dev validation failed: {result}"
    assert result["missing_fields"] == []

    for field in ROLE_ARTIFACT_SCHEMAS["dev"]["required_fields"]:
        assert field in DEV_OUTPUT, f"Missing Dev field: {field}"
        assert DEV_OUTPUT[field], f"Empty Dev field: {field}"


def test_replay_test_gate():
    """Case 3: Validate Test gate against ROLE_ARTIFACT_SCHEMAS['tester']."""
    result = validate_role_artifact("tester", TEST_REPORT)
    assert result["pass"] is True, f"Tester validation failed: {result}"
    assert result["missing_fields"] == []

    for field in ROLE_ARTIFACT_SCHEMAS["tester"]["required_fields"]:
        assert field in TEST_REPORT, f"Missing Tester field: {field}"
        assert TEST_REPORT[field], f"Empty Tester field: {field}"


def test_replay_qa_gate():
    """Case 4: Validate QA gate against ROLE_ARTIFACT_SCHEMAS['qa'] + extras."""
    result = validate_role_artifact("qa", QA_REPORT)
    assert result["pass"] is True, f"QA validation failed: {result}"
    assert result["missing_fields"] == []

    for field in ROLE_ARTIFACT_SCHEMAS["qa"]["required_fields"]:
        assert field in QA_REPORT, f"Missing QA field: {field}"
        assert QA_REPORT[field], f"Empty QA field: {field}"

    # Extra fields: recommendation and criteria_results
    assert "recommendation" in QA_REPORT and QA_REPORT["recommendation"]
    assert "criteria_results" in QA_REPORT and QA_REPORT["criteria_results"]


def test_replay_gatekeeper_gate():
    """Case 5: Validate Gatekeeper PM alignment check — verdict + alignment."""
    assert "verdict" in GATEKEEPER_REPORT, "Missing gatekeeper verdict"
    assert GATEKEEPER_REPORT["verdict"], "Empty gatekeeper verdict"

    assert "alignment" in GATEKEEPER_REPORT, "Missing gatekeeper alignment"
    assert GATEKEEPER_REPORT["alignment"], "Empty gatekeeper alignment"

    # alignment should contain sub-fields
    alignment = GATEKEEPER_REPORT["alignment"]
    for key in ("goal_met", "acceptance_criteria_met", "scope_respected"):
        assert key in alignment, f"Missing alignment sub-field: {key}"


def test_replay_merge_gate():
    """Case 6: Validate Merge gate — version + clean worktree."""
    assert "version" in MERGE_REPORT and MERGE_REPORT["version"]
    assert "dirty_files" in MERGE_REPORT
    assert MERGE_REPORT["dirty_files"] == [], "dirty_files must be empty for clean worktree"
    assert "merge_commit" in MERGE_REPORT and MERGE_REPORT["merge_commit"]


def test_replay_coordinator_routing():
    """Case 7: Validate Coordinator routing — task_type is known."""
    assert "task_type" in COORDINATOR_ROUTING, "Missing task_type field"
    task_type = COORDINATOR_ROUTING["task_type"]
    assert task_type, "Empty task_type"
    assert task_type in KNOWN_TASK_TYPES, f"Unknown task_type: {task_type}"


def test_replay_failure_classifier():
    """Case 8: Call classify_gate_failure with fixture and validate output keys."""
    output = classify_gate_failure(
        stage=FAILURE_CLASSIFICATION["stage"],
        reason=FAILURE_CLASSIFICATION["reason"],
        metadata=FAILURE_CLASSIFICATION["metadata"],
        result=FAILURE_CLASSIFICATION["result"],
    )

    required_keys = {"stage", "reason", "failure_class", "suggested_action", "issue_summary"}
    for key in required_keys:
        assert key in output, f"Missing failure classifier output key: {key}"
        assert output[key], f"Empty failure classifier output key: {key}"


def test_replay_memory_write():
    """Case 9: Validate memory write payload schema."""
    for field in ("module", "kind", "content"):
        assert field in MEMORY_WRITE, f"Missing memory write field: {field}"
        assert MEMORY_WRITE[field], f"Empty memory write field: {field}"
