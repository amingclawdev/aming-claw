"""Known-good fixture data for all 9 chain stage contract replay tests.

Each fixture dict represents the expected output of a specific chain stage,
used to validate contract boundaries without any external dependencies.
"""

# Case 1: PM output — ROLE_ARTIFACT_SCHEMAS['pm'] required_fields + PRD-level fields
PM_OUTPUT = {
    "goal": "Implement replay validation test suite for phase 3",
    "acceptance_criteria": [
        "AC1: 9 test functions exist",
        "AC2: All tests pass",
        "AC3: No network dependencies",
    ],
    "fail_conditions": [
        "Any test imports network libraries",
        "Tests take longer than 30 seconds",
    ],
    "target_files": [
        "agent/tests/test_replay_validation.py",
        "agent/tests/fixtures/replay_data.py",
    ],
    "verification": {
        "method": "automated test",
        "command": "pytest agent/tests/test_replay_validation.py -v",
    },
}

# Case 2: Dev output — ROLE_ARTIFACT_SCHEMAS['dev'] required_fields
DEV_OUTPUT = {
    "implementation_summary": "Created replay validation tests and fixture data for all 9 chain stages",
    "changed_files": [
        "agent/tests/test_replay_validation.py",
        "agent/tests/fixtures/replay_data.py",
        "agent/tests/fixtures/__init__.py",
    ],
    "commit_hash": "abc1234def5678",
}

# Case 3: Test gate — ROLE_ARTIFACT_SCHEMAS['tester'] required_fields as test_report
TEST_REPORT = {
    "tests_executed": [
        "test_replay_pm_output",
        "test_replay_dev_output",
        "test_replay_test_gate",
        "test_replay_qa_gate",
        "test_replay_gatekeeper_gate",
        "test_replay_merge_gate",
        "test_replay_coordinator_routing",
        "test_replay_failure_classifier",
        "test_replay_memory_write",
    ],
    "result_summary": "9 passed, 0 failed, 0 errors",
    "recommendation": "approve",
}

# Case 4: QA gate — ROLE_ARTIFACT_SCHEMAS['qa'] required_fields + extras
QA_REPORT = {
    "scenarios_checked": [
        "All 9 contract boundaries validated",
        "No network imports detected",
        "Runtime under 30 seconds",
    ],
    "verdict": "pass",
    "recommendation": "approve for merge",
    "criteria_results": {
        "AC1": "pass",
        "AC2": "pass",
        "AC3": "pass",
        "AC4": "pass",
    },
}

# Case 5: Gatekeeper gate — PM alignment check with verdict + alignment
GATEKEEPER_REPORT = {
    "verdict": "aligned",
    "alignment": {
        "goal_met": True,
        "acceptance_criteria_met": True,
        "scope_respected": True,
    },
}

# Case 6: Merge gate — version + clean worktree fields
MERGE_REPORT = {
    "version": "c07e605",
    "dirty_files": [],
    "merge_commit": "def7890abc1234",
}

# Case 7: Coordinator routing — task type is one of known types
COORDINATOR_ROUTING = {
    "task_type": "dev",
}

# Case 8: Failure classifier — input for classify_gate_failure
FAILURE_CLASSIFICATION = {
    "stage": "qa",
    "reason": "Missing required field: verdict",
    "metadata": {"task_id": "task-test-001"},
    "result": {"pass": False},
}

# Case 9: Memory write — payload schema
MEMORY_WRITE = {
    "module": "agent.governance.artifacts",
    "kind": "pitfall",
    "content": "validate_role_artifact returns pass=True for unknown roles with no schema",
}
