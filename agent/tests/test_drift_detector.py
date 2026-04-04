"""Tests for agent.governance.drift_detector (D10 contract-drift detection)."""

import copy
import pytest


def test_capture_baseline_contains_required_keys():
    """AC2: baseline contains minimum required keys."""
    from agent.governance.drift_detector import capture_baseline

    baseline = capture_baseline()
    assert isinstance(baseline, dict)
    # Minimum required keys per AC2
    assert "ai_lifecycle::_CLAUDE_ROLE_TURN_CAPS" in baseline
    assert "ai_lifecycle::_COORDINATOR_HANG_TIMEOUT" in baseline
    assert "auto_chain::CHAIN" in baseline
    assert "auto_chain::_DISABLE_VERSION_GATE" in baseline
    # Additional R8 constants
    assert "ai_lifecycle::_HANG_TIMEOUT" in baseline
    assert "ai_lifecycle::_MAX_TIMEOUT" in baseline
    assert "auto_chain::RECONCILIATION_BYPASS_POLICY" in baseline
    assert "auto_chain::MAX_CHAIN_DEPTH" in baseline


def test_no_drift():
    """AC4: When no values changed, detect_drift returns an empty list."""
    from agent.governance.drift_detector import capture_baseline, detect_drift

    baseline = capture_baseline()
    findings = detect_drift(baseline)
    assert findings == []


def test_unauthorized_drift():
    """AC5: Changed value NOT in authorized_keys has authorized=False."""
    from agent.governance.drift_detector import capture_baseline, detect_drift

    baseline = capture_baseline()
    # Simulate a drift by mutating a baseline value
    mutated = copy.deepcopy(baseline)
    mutated["ai_lifecycle::_HANG_TIMEOUT"] = 9999

    findings = detect_drift(mutated, authorized_keys=set())
    assert len(findings) >= 1
    drift = [f for f in findings if f.changed_key == "ai_lifecycle::_HANG_TIMEOUT"]
    assert len(drift) == 1
    assert drift[0].authorized is False
    assert drift[0].old_value == 9999  # old is the mutated baseline
    # AC3: check structure
    assert hasattr(drift[0], "changed_key")
    assert hasattr(drift[0], "old_value")
    assert hasattr(drift[0], "new_value")
    assert hasattr(drift[0], "authorized")


def test_authorized_drift():
    """AC6: Changed value IS in authorized_keys has authorized=True."""
    from agent.governance.drift_detector import capture_baseline, detect_drift

    baseline = capture_baseline()
    mutated = copy.deepcopy(baseline)
    mutated["auto_chain::MAX_CHAIN_DEPTH"] = 999

    findings = detect_drift(
        mutated,
        authorized_keys={"auto_chain::MAX_CHAIN_DEPTH"},
    )
    drift = [f for f in findings if f.changed_key == "auto_chain::MAX_CHAIN_DEPTH"]
    assert len(drift) == 1
    assert drift[0].authorized is True


def test_drift_finding_structure():
    """AC3: Each finding contains changed_key, old_value, new_value, authorized."""
    from agent.governance.drift_detector import capture_baseline, detect_drift

    baseline = capture_baseline()
    mutated = copy.deepcopy(baseline)
    mutated["auto_chain::_DISABLE_VERSION_GATE"] = True  # flip it

    findings = detect_drift(mutated)
    assert len(findings) >= 1
    for f in findings:
        assert hasattr(f, "changed_key")
        assert hasattr(f, "old_value")
        assert hasattr(f, "new_value")
        assert hasattr(f, "authorized")


def test_findings_to_json():
    """Drift findings can be serialized to JSON for metadata storage."""
    from agent.governance.drift_detector import (
        DriftFinding,
        findings_to_json,
    )
    import json

    findings = [
        DriftFinding("mod::X", 1, 2, False),
        DriftFinding("mod::Y", "a", "b", True),
    ]
    result = json.loads(findings_to_json(findings))
    assert len(result) == 2
    assert result[0]["changed_key"] == "mod::X"
    assert result[0]["authorized"] is False
    assert result[1]["authorized"] is True


def test_sets_serialized_as_sorted_lists():
    """R2: sets should be converted to sorted lists for stable comparison."""
    from agent.governance.drift_detector import _serialize

    assert _serialize({3, 1, 2}) == ["1", "2", "3"]
    assert _serialize({"b", "a"}) == ["a", "b"]


def test_mixed_authorized_and_unauthorized():
    """Multiple drift findings with mixed authorization."""
    from agent.governance.drift_detector import capture_baseline, detect_drift

    baseline = capture_baseline()
    mutated = copy.deepcopy(baseline)
    mutated["ai_lifecycle::_HANG_TIMEOUT"] = 9999
    mutated["auto_chain::MAX_CHAIN_DEPTH"] = 999

    findings = detect_drift(
        mutated,
        authorized_keys={"auto_chain::MAX_CHAIN_DEPTH"},
    )
    by_key = {f.changed_key: f for f in findings}
    assert by_key["ai_lifecycle::_HANG_TIMEOUT"].authorized is False
    assert by_key["auto_chain::MAX_CHAIN_DEPTH"].authorized is True
