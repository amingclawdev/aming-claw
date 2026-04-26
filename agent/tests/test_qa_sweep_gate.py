"""Tests for QA Sweep Phase 5 — _qa_sweep_gate and _qa_sweep_skip_rule.

Covers AC1-AC7 acceptance criteria:
  1. disabled-by-default
  2. clean-pass
  3. high-drift-fail
  4. skip-tests-only
  5. docs-only-phases
  6. docs-plus-code-full
  7. code-only-full
  8. orchestrator-error-non-blocking
  9. failure-spawns-corrective-pm
"""

import os
import types
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers: import the functions under test
# ---------------------------------------------------------------------------

def _import_auto_chain():
    """Import auto_chain module with mocked heavy deps."""
    import importlib
    import agent.governance.auto_chain as mod
    return mod


@pytest.fixture(autouse=True)
def _reset_sweep_flag(monkeypatch):
    """Ensure QA_SWEEP_ENABLED is reset between tests."""
    monkeypatch.delenv("QA_SWEEP_ENABLED", raising=False)


# ---------------------------------------------------------------------------
# _qa_sweep_skip_rule tests (embedded in gate tests below)
# ---------------------------------------------------------------------------

class TestQaSweepSkipRule:
    """Directly test the classifier."""

    def test_tests_only(self):
        from agent.governance.auto_chain import _qa_sweep_skip_rule
        assert _qa_sweep_skip_rule(["agent/tests/test_foo.py", "test_bar.py"]) == "tests_only"

    def test_docs_only(self):
        from agent.governance.auto_chain import _qa_sweep_skip_rule
        assert _qa_sweep_skip_rule(["README.md", "docs/guide.md"]) == "docs_only"

    def test_docs_plus_code(self):
        from agent.governance.auto_chain import _qa_sweep_skip_rule
        assert _qa_sweep_skip_rule(["README.md", "agent/foo.py"]) == "docs_plus_code"

    def test_code_only(self):
        from agent.governance.auto_chain import _qa_sweep_skip_rule
        assert _qa_sweep_skip_rule(["agent/foo.py", "agent/bar.py"]) == "code_only"

    def test_empty(self):
        from agent.governance.auto_chain import _qa_sweep_skip_rule
        assert _qa_sweep_skip_rule([]) == "unknown"


# ---------------------------------------------------------------------------
# _qa_sweep_gate tests
# ---------------------------------------------------------------------------

def _make_conn():
    """Create a minimal mock connection for spawn_corrective_pm."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {
        "task_id": "qa-123",
        "status": "succeeded",
        "retry_round": 0,
        "metadata_json": '{"qa_corrective_round": 0}',
    }
    return conn


class TestQaSweepGate:

    # AC1: disabled-by-default
    def test_disabled_by_default(self):
        """When QA_SWEEP_ENABLED is unset, gate returns True with disabled message."""
        from agent.governance.auto_chain import _qa_sweep_gate
        conn = _make_conn()
        ok, msg, res = _qa_sweep_gate(conn, "proj", "qa-1", {}, {})
        assert ok is True
        assert "qa_sweep disabled" in msg
        assert res is None

    # AC1 variant: explicitly false
    def test_disabled_explicit_false(self, monkeypatch):
        """When QA_SWEEP_ENABLED='false', still disabled."""
        # The flag is evaluated at import time, so we patch the module-level var
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", False)
        ok, msg, res = mod._qa_sweep_gate(_make_conn(), "proj", "qa-1", {}, {})
        assert ok is True
        assert "disabled" in msg

    # AC2: clean-pass (no high-severity findings)
    def test_clean_pass(self, monkeypatch):
        """Orchestrator returns findings with no high severity → pass."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        mock_result = {"findings": [
            {"confidence": "low", "priority": "P2", "message": "minor"},
        ]}
        with patch(
            "agent.governance.reconcile_phases.orchestrator.run_commit_slice_orchestrated",
            return_value=mock_result,
        ):
            ok, msg, res = mod._qa_sweep_gate(
                _make_conn(), "proj", "qa-1",
                {"changed_files": ["agent/foo.py"], "commit": "abc123"},
                {},
            )
        assert ok is True
        assert "passed" in msg
        assert res == mock_result

    # AC5: high-drift-fail
    def test_high_drift_fail(self, monkeypatch):
        """High-severity findings → gate fails."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        mock_result = {"findings": [
            {"confidence": "high", "priority": "P0", "message": "critical drift"},
            {"confidence": "high", "priority": "P1", "message": "severe drift"},
            {"confidence": "low", "priority": "P0", "message": "low-conf"},
        ]}
        with patch(
            "agent.governance.reconcile_phases.orchestrator.run_commit_slice_orchestrated",
            return_value=mock_result,
        ), patch.object(mod, "spawn_corrective_pm", return_value="task-corrective-abc"):
            ok, msg, res = mod._qa_sweep_gate(
                _make_conn(), "proj", "qa-1",
                {"changed_files": ["agent/foo.py"], "commit": "abc123"},
                {},
            )
        assert ok is False
        assert "high-severity drift" in msg
        assert res == mock_result

    # AC3: skip-tests-only
    def test_skip_tests_only(self, monkeypatch):
        """Tests-only changes are skipped."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        ok, msg, res = mod._qa_sweep_gate(
            _make_conn(), "proj", "qa-1",
            {"changed_files": ["agent/tests/test_foo.py", "test_bar.py"]},
            {},
        )
        assert ok is True
        assert "tests-only" in msg
        assert res is None

    # AC2: docs-only-phases
    def test_docs_only_phases(self, monkeypatch):
        """Docs-only changes trigger phases=['K','D'], NOT skipped."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        captured_kwargs = {}

        def mock_orchestrator(project_id, workspace_path, commit, *, phases=None, dry_run=True, rename_map=None):
            captured_kwargs["phases"] = phases
            return {"findings": []}

        with patch(
            "agent.governance.reconcile_phases.orchestrator.run_commit_slice_orchestrated",
            side_effect=mock_orchestrator,
        ):
            ok, msg, res = mod._qa_sweep_gate(
                _make_conn(), "proj", "qa-1",
                {"changed_files": ["README.md", "docs/api.md"], "commit": "abc123"},
                {},
            )
        assert ok is True
        assert captured_kwargs["phases"] == ["K", "D"]

    # docs-plus-code → full phases (None)
    def test_docs_plus_code_full(self, monkeypatch):
        """Docs+code changes run full phases (phases=None)."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        captured_kwargs = {}

        def mock_orchestrator(project_id, workspace_path, commit, *, phases=None, dry_run=True, rename_map=None):
            captured_kwargs["phases"] = phases
            return {"findings": []}

        with patch(
            "agent.governance.reconcile_phases.orchestrator.run_commit_slice_orchestrated",
            side_effect=mock_orchestrator,
        ):
            ok, msg, res = mod._qa_sweep_gate(
                _make_conn(), "proj", "qa-1",
                {"changed_files": ["README.md", "agent/foo.py"], "commit": "abc123"},
                {},
            )
        assert ok is True
        assert captured_kwargs["phases"] is None

    # code-only → full phases (None)
    def test_code_only_full(self, monkeypatch):
        """Code-only changes run full phases (phases=None)."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        captured_kwargs = {}

        def mock_orchestrator(project_id, workspace_path, commit, *, phases=None, dry_run=True, rename_map=None):
            captured_kwargs["phases"] = phases
            return {"findings": []}

        with patch(
            "agent.governance.reconcile_phases.orchestrator.run_commit_slice_orchestrated",
            side_effect=mock_orchestrator,
        ):
            ok, msg, res = mod._qa_sweep_gate(
                _make_conn(), "proj", "qa-1",
                {"changed_files": ["agent/foo.py"], "commit": "abc123"},
                {},
            )
        assert ok is True
        assert captured_kwargs["phases"] is None

    # AC4: orchestrator-error-non-blocking
    def test_orchestrator_error_non_blocking(self, monkeypatch):
        """Orchestrator exception → non-blocking pass."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        with patch(
            "agent.governance.reconcile_phases.orchestrator.run_commit_slice_orchestrated",
            side_effect=RuntimeError("orchestrator crashed"),
        ):
            ok, msg, res = mod._qa_sweep_gate(
                _make_conn(), "proj", "qa-1",
                {"changed_files": ["agent/foo.py"], "commit": "abc123"},
                {},
            )
        assert ok is True
        assert "qa_sweep error (non-blocking)" in msg
        assert res is None

    # AC6: failure-spawns-corrective-pm
    def test_failure_spawns_corrective_pm(self, monkeypatch):
        """Gate failure invokes spawn_corrective_pm with correct args."""
        import agent.governance.auto_chain as mod
        monkeypatch.setattr(mod, "_QA_SWEEP_ENABLED", True)

        mock_result = {"findings": [
            {"confidence": "high", "priority": "P0", "message": "critical"},
        ]}

        with patch(
            "agent.governance.reconcile_phases.orchestrator.run_commit_slice_orchestrated",
            return_value=mock_result,
        ), patch.object(mod, "spawn_corrective_pm", return_value="task-corrective-xyz") as mock_spawn:
            ok, msg, res = mod._qa_sweep_gate(
                _make_conn(), "proj", "qa-42",
                {"changed_files": ["agent/foo.py"], "commit": "abc123", "task_id": "qa-42"},
                {},
            )

        assert ok is False
        mock_spawn.assert_called_once()
        call_args = mock_spawn.call_args
        assert call_args[0][1] == "proj"  # project_id
        assert call_args[0][2] == "qa-42"  # parent_qa_task_id
        assert call_args[1]["bug_id"] == "qa_sweep_drift" if call_args[1] else call_args[0][4] == "qa_sweep_drift"
